"""Multi-node GPU-aware job queue daemon.

Watches `queue/pending/` for jobspec JSON files and launches each job on the
first node (in `nodes.txt` order) with enough free GPUs. Every job is started
through ssh — even local jobs — so launch/reap/cancel have one code path.

Jobspec schema (written by functions/submit.py):
    {
        "id":           int,
        "name":         str,
        "cmd":          [str, ...],
        "cwd":          str,
        "submit_cwd":   str,            # logs land here as job-<id>.{out,err}
        "gpus":         int,
        "submitted_at": float,
        "env":          {str: str},
    }

When a job starts, the daemon annotates the spec with:
    "node":       str,                  # ip from nodes.txt
    "gpus":       [int, ...],           # gpu ids on that node
    "remote_pid": int,                  # pid of the setsid'd user command
    "started_at": float,

Multi-node config (`nodes.txt`, repo root, gitignored):
    Lines are `<ip> [gpu_count]`. First line = home node. Blanks and
    `#`-comments ignored. If `gpu_count` is absent, daemon auto-detects via
    `ssh <ip> nvidia-smi -L`. If the file is missing or empty, falls back to
    home-node-only with autodetect.

Queue layout (under queue/, defaults to <repo>/queue, overridable via
MINSCHED_QUEUE_DIR):
    pending/<id>.json     queued, not running
    running/<id>.json     allocated; in flight
    running/<id>.remote_pid    written by ssh wrapper once user cmd is launched
    cancel/<id>           marker file dropped by functions/cancel.py
    done/<id>.json        terminal (success or failure)
    state.json            snapshot for queue
    .next_id              monotonic counter (managed by functions/submit.py)

Per-job logs are NOT under queue/. They go to <submit_cwd>/job-<id>.{out,err},
which must be on shared storage (/mnt/vast) so the executing node can write
and the user can read.

Stale running/* on daemon startup are marked failed (exit -1, "orphaned").

Usage:
    python gpu_queue.py                  # foreground
    python gpu_queue.py --queue-dir DIR  # override queue/ location
"""
import argparse
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_QUEUE_ENV = os.environ.get("MINSCHED_QUEUE_DIR")
REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_QUEUE_DIR = Path(_QUEUE_ENV) if _QUEUE_ENV else REPO_ROOT / "queue"
DEFAULT_NODES_FILE = REPO_ROOT / "nodes.txt"

POLL_INTERVAL_S = 1.0
REMOTE_PID_WAIT_S = 15.0  # max time to wait for the ssh wrapper to write remote_pid


def _atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    tmp.replace(path)


def _detect_local_gpus() -> int:
    """Count GPUs visible to `nvidia-smi -L` on this host."""
    try:
        out = subprocess.check_output(["nvidia-smi", "-L"], text=True)
        return sum(1 for line in out.splitlines() if line.strip().startswith("GPU "))
    except (FileNotFoundError, subprocess.CalledProcessError):
        return 0


def _detect_remote_gpus(ip: str) -> int:
    """Count GPUs on a remote host via ssh + nvidia-smi -L."""
    try:
        out = subprocess.check_output(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             ip, "nvidia-smi", "-L"],
            text=True, stderr=subprocess.DEVNULL,
        )
        return sum(1 for line in out.splitlines() if line.strip().startswith("GPU "))
    except (FileNotFoundError, subprocess.CalledProcessError):
        return 0


@dataclass
class Node:
    """A scheduling target. Order in self.nodes = nodes.txt order = priority."""
    ip: str
    total_gpus: int
    gpus_in_use: set = field(default_factory=set)

    def free_count(self) -> int:
        return self.total_gpus - len(self.gpus_in_use)

    def allocate(self, n: int) -> list:
        """Return n free gpu ids, mark them in-use. [] if not enough free."""
        if n > self.free_count():
            return []
        chosen = sorted(set(range(self.total_gpus)) - self.gpus_in_use)[:n]
        self.gpus_in_use.update(chosen)
        return chosen

    def release(self, gpus) -> None:
        for g in gpus:
            self.gpus_in_use.discard(g)


def _load_nodes(nodes_file: Path) -> list:
    """Parse nodes.txt. Returns ordered list of Node, first = home node.

    Lines: `<ip> [gpu_count]`. Blank lines and `#`-comments ignored.
    If file missing or yields no entries, fall back to single home-node from
    local nvidia-smi.
    """
    nodes = []
    if nodes_file.exists():
        for raw in nodes_file.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            ip = parts[0]
            if len(parts) >= 2:
                gpus = int(parts[1])
            else:
                gpus = None  # autodetect below
            nodes.append((ip, gpus))

    if not nodes:
        n = _detect_local_gpus()
        if n <= 0:
            raise RuntimeError("no nodes.txt and no local GPUs detected")
        # Synthetic local entry — uses ssh to localhost like every other node.
        nodes = [("127.0.0.1", n)]

    out = []
    for ip, gpus in nodes:
        if gpus is None:
            gpus = _detect_remote_gpus(ip)
            if gpus <= 0:
                raise RuntimeError(
                    f"could not detect gpu count for {ip}; "
                    f"add `<ip> <n>` to nodes.txt or fix ssh/nvidia-smi access"
                )
        out.append(Node(ip=ip, total_gpus=gpus))
    return out


class Job:
    """In-memory record of a running job."""
    __slots__ = ("spec", "spec_path", "proc", "node_ip", "gpus",
                 "stdout_f", "stderr_f", "started_at", "remote_pid",
                 "remote_pid_path", "cancel_pending")

    def __init__(self, spec, spec_path, proc, node_ip, gpus,
                 stdout_f, stderr_f, started_at, remote_pid_path):
        self.spec = spec
        self.spec_path = spec_path
        self.proc = proc                  # the ssh subprocess
        self.node_ip = node_ip
        self.gpus = gpus
        self.stdout_f = stdout_f
        self.stderr_f = stderr_f
        self.started_at = started_at
        self.remote_pid = None            # filled when wrapper writes the file
        self.remote_pid_path = remote_pid_path
        self.cancel_pending = False


def _build_job_script(spec: dict, gpus: list, remote_pid_path: Path,
                      stdout_path: Path, stderr_path: Path) -> str:
    """Return the bash script body to execute on the remote node.

    Layout:
      ssh stays connected to the supervisor bash (this script). The supervisor
      `setsid`s a child bash that runs the user command. That child is a new
      session leader → its pid == its pgid, suitable for `kill -<pgid>`.
      The supervisor writes the child pid to remote_pid_path, then waits.
      When the user cmd exits, the supervisor exits with the same code, and
      ssh returns that code to the daemon.

    Stdio: the user cmd's stdout/stderr go to log files on NFS. The
    supervisor's own stdio stays on the ssh pipe (silent in practice).
    """
    cwd = spec.get("cwd") or str(Path.home())
    user_env = spec.get("env") or {}
    env_exports = "\n".join(
        f"export {k}={shlex.quote(str(v))}" for k, v in user_env.items()
    )
    cmd_str = " ".join(shlex.quote(a) for a in spec["cmd"])
    gpus_csv = ",".join(str(g) for g in gpus)
    return (
        f"#!/bin/bash\n"
        f"set -u\n"
        f"cd {shlex.quote(cwd)}\n"
        f"export CUDA_VISIBLE_DEVICES={gpus_csv}\n"
        f"export GPU_QUEUE_JOB_ID={spec['id']}\n"
        f"{env_exports}\n"
        # setsid'd child writes its own pid (= pgid) into the file, then exec
        # the user command. We disown into the background so the supervisor
        # can wait on it as a job. Stdio is redirected on the setsid line
        # itself so we don't leak the supervisor's own output into job logs.
        f"setsid bash -c "
        f"{shlex.quote(f'echo $$ > {shlex.quote(str(remote_pid_path))}; exec {cmd_str}')} "
        f">>{shlex.quote(str(stdout_path))} "
        f"2>>{shlex.quote(str(stderr_path))} </dev/null &\n"
        f"CHILD=$!\n"
        f"wait $CHILD\n"
        f"exit $?\n"
    )


class Daemon:
    def __init__(self, queue_dir: Path, nodes_file: Path):
        self.queue_dir = queue_dir
        self.pending_dir = queue_dir / "pending"
        self.running_dir = queue_dir / "running"
        self.done_dir = queue_dir / "done"
        self.cancel_dir = queue_dir / "cancel"
        self.state_path = queue_dir / "state.json"
        for d in [self.pending_dir, self.running_dir, self.done_dir, self.cancel_dir]:
            d.mkdir(parents=True, exist_ok=True)

        self.nodes_file = nodes_file
        self.nodes = _load_nodes(nodes_file)  # list[Node], priority order
        self.nodes_by_ip = {n.ip: n for n in self.nodes}
        self.running: dict = {}  # id -> Job
        self.shutdown_requested = False

    def _log(self, msg: str) -> None:
        logging.info(msg)

    # ----- pending scan / scheduling ------------------------------------------

    def _scan_pending(self) -> list:
        specs = []
        for p in self.pending_dir.glob("*.json"):
            try:
                spec = json.loads(p.read_text())
            except Exception as e:
                self._log(f"skipping unreadable jobspec {p.name}: {e}")
                continue
            specs.append((spec, p))
        specs.sort(key=lambda sp: (sp[0].get("submitted_at", 0), sp[0].get("id", 0)))
        return specs

    def _pick_node(self, need: int):
        """First-fit by nodes.txt order. Returns (Node, [gpu_ids]) or (None, [])."""
        for node in self.nodes:
            gpus = node.allocate(need)
            if gpus:
                return node, gpus
        return None, []

    def _max_node_gpus(self) -> int:
        return max((n.total_gpus for n in self.nodes), default=0)

    def _schedule(self) -> None:
        pending = self._scan_pending()
        max_gpus = self._max_node_gpus()
        for spec, spec_path in pending:
            need = int(spec.get("gpus", 1))
            if need < 0 or need > max_gpus:
                self._log(f"job {spec.get('id')} requests {need} gpus, "
                          f"no node has that many ({max_gpus} max) — failing")
                self._move_to_done(spec, spec_path, exit_code=-2,
                                   error=f"requested {need} gpus > {max_gpus} (largest node)")
                continue
            node, gpus = self._pick_node(need)
            if not gpus:
                continue  # backfill: maybe a smaller job below can fit
            try:
                job = self._launch(spec, spec_path, node, gpus)
                self.running[spec["id"]] = job
            except Exception as e:
                self._log(f"launch failed for job {spec.get('id')}: {e}")
                node.release(gpus)

    # ----- launch -------------------------------------------------------------

    def _launch(self, spec: dict, spec_path: Path, node: Node, gpus: list) -> Job:
        jid = spec["id"]
        # logs go to submit_cwd; both nodes must see this path (we enforce
        # /mnt/vast in submit.py, but fall back if older specs slip through).
        log_dir = Path(spec.get("submit_cwd") or spec.get("cwd") or self.queue_dir)
        stdout_path = log_dir / f"job-{jid}.out"
        stderr_path = log_dir / f"job-{jid}.err"
        # touch them so `log` works immediately, even before the remote shell opens them
        stdout_path.touch(exist_ok=True)
        stderr_path.touch(exist_ok=True)

        remote_pid_path = self.running_dir / f"{jid}.remote_pid"
        remote_pid_path.unlink(missing_ok=True)

        # Write the launch script as a file on NFS so the remote node can read
        # it directly. Avoids all the over-ssh stdin/quoting hazards.
        script = _build_job_script(spec, gpus, remote_pid_path, stdout_path, stderr_path)
        script_path = self.running_dir / f"{jid}.sh"
        script_path.write_text(script)
        script_path.chmod(0o700)

        ssh_cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            node.ip, "bash", str(script_path),
        ]
        # ssh's own stderr (connection errors) goes to job.err so users see it.
        stderr_f = open(stderr_path, "ab", buffering=0)
        proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.DEVNULL,
            stdout=stderr_f,
            stderr=stderr_f,
            start_new_session=True,
        )
        started_at = time.time()

        running_spec = dict(spec)
        running_spec.update(
            node=node.ip,
            gpus=gpus,
            started_at=started_at,
            ssh_pid=proc.pid,
        )
        running_path = self.running_dir / spec_path.name
        _atomic_write_json(running_path, running_spec)
        spec_path.unlink(missing_ok=True)

        self._log(f"launched job {jid} ({spec.get('name','?')}) on {node.ip} "
                  f"gpus={gpus} ssh_pid={proc.pid}")
        return Job(running_spec, running_path, proc, node.ip, gpus,
                   None, stderr_f, started_at, remote_pid_path)

    def _check_remote_pids(self) -> None:
        """For each running job missing a remote_pid, try to read it from the
        wrapper's pid file. If the file isn't there after REMOTE_PID_WAIT_S,
        and the ssh has already exited, the launch failed — caller's _reap
        will detect this and clean up."""
        for job in self.running.values():
            if job.remote_pid is not None:
                continue
            try:
                txt = job.remote_pid_path.read_text().strip()
                job.remote_pid = int(txt) if txt else None
                if job.remote_pid is not None:
                    # Persist to the on-disk spec so cancel can find it after a daemon restart
                    spec = json.loads(job.spec_path.read_text())
                    spec["remote_pid"] = job.remote_pid
                    _atomic_write_json(job.spec_path, spec)
            except (FileNotFoundError, ValueError):
                pass

    # ----- cancel -------------------------------------------------------------

    def _scan_cancel_requests(self) -> None:
        """Pick up `cancel/<id>` markers and signal the corresponding job."""
        for marker in list(self.cancel_dir.glob("*")):
            try:
                jid = int(marker.name)
            except ValueError:
                marker.unlink(missing_ok=True)
                continue
            job = self.running.get(jid)
            marker.unlink(missing_ok=True)
            if job is None:
                self._log(f"cancel for job {jid}: not running (already done?)")
                continue
            self._cancel_job(job)

    def _cancel_job(self, job: Job) -> None:
        """Send SIGTERM to the remote job's process group. If we don't have
        the remote_pid yet (race), mark for cancel-on-arrival."""
        if job.cancel_pending:
            return
        job.cancel_pending = True
        if job.remote_pid is None:
            self._log(f"cancel job {job.spec['id']}: remote_pid not yet known; will retry")
            return
        self._send_remote_signal(job, signal.SIGTERM)

    def _send_remote_signal(self, job: Job, sig) -> None:
        """ssh into the executing node and signal the remote pgid."""
        if job.remote_pid is None:
            return
        cmd = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            job.node_ip, "kill", f"-{int(sig)}", f"-{job.remote_pid}",
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, check=False)
            self._log(f"sent signal {int(sig)} to job {job.spec['id']} "
                      f"(remote_pid={job.remote_pid} on {job.node_ip})")
        except subprocess.TimeoutExpired:
            self._log(f"timeout signaling job {job.spec['id']} on {job.node_ip}")

    def _retry_pending_cancels(self) -> None:
        """For jobs that were cancelled before we knew their remote_pid,
        try again now that we may have it."""
        for job in self.running.values():
            if job.cancel_pending and job.remote_pid is not None:
                # Re-fire SIGTERM (idempotent — kill returns ESRCH if gone)
                self._send_remote_signal(job, signal.SIGTERM)
                # leave cancel_pending true; we re-fire each tick until the
                # job actually exits. Cheap (one ssh) and self-healing if the
                # remote process keeps trying to ignore TERM.

    # ----- reap ---------------------------------------------------------------

    def _reap(self) -> None:
        finished = []
        for jid, job in self.running.items():
            rc = job.proc.poll()
            if rc is None:
                continue
            # ssh has exited. If we never got a remote_pid AND ssh exited
            # quickly, that's a launch failure. If we did get one, ssh's exit
            # code is the user cmd's exit code.
            elapsed = time.time() - job.started_at
            if job.remote_pid is None and elapsed < REMOTE_PID_WAIT_S:
                self._log(f"job {jid}: ssh exited rc={rc} before wrapper ran; treating as launch failure")
                error = f"ssh launch failed (rc={rc})"
            else:
                error = None
            self._log(f"job {jid} finished rc={rc} on {job.node_ip}")
            try:
                job.stderr_f.close()
            except Exception:
                pass
            # Release GPUs on the right node
            node = self.nodes_by_ip.get(job.node_ip)
            if node is not None:
                node.release(job.gpus)
            done_spec = dict(job.spec)
            done_spec["finished_at"] = time.time()
            done_spec["exit_code"] = rc
            if error:
                done_spec["error"] = error
            if job.remote_pid is not None:
                done_spec["remote_pid"] = job.remote_pid
            _atomic_write_json(self.done_dir / job.spec_path.name, done_spec)
            job.spec_path.unlink(missing_ok=True)
            job.remote_pid_path.unlink(missing_ok=True)
            (self.running_dir / f"{jid}.sh").unlink(missing_ok=True)
            finished.append(jid)
        for jid in finished:
            del self.running[jid]

    def _move_to_done(self, spec: dict, spec_path: Path, exit_code: int, error: str) -> None:
        done_spec = dict(spec)
        done_spec["exit_code"] = exit_code
        done_spec["finished_at"] = time.time()
        done_spec["error"] = error
        _atomic_write_json(self.done_dir / spec_path.name, done_spec)
        spec_path.unlink(missing_ok=True)

    # ----- startup recovery ---------------------------------------------------

    def _recover_orphans(self) -> None:
        """Daemon restart: any leftover running/* is orphaned (ssh died with us)."""
        for p in self.running_dir.glob("*.json"):
            try:
                spec = json.loads(p.read_text())
            except Exception:
                p.unlink(missing_ok=True)
                continue
            spec["exit_code"] = -1
            spec["finished_at"] = time.time()
            spec["error"] = "orphaned: daemon restarted while job was running"
            _atomic_write_json(self.done_dir / p.name, spec)
            p.unlink(missing_ok=True)
            self._log(f"orphaned job {spec.get('id')} -> done/")
        # clear stale remote_pid files, launch scripts, and cancel markers
        for p in self.running_dir.glob("*.remote_pid"):
            p.unlink(missing_ok=True)
        for p in self.running_dir.glob("*.sh"):
            p.unlink(missing_ok=True)
        for p in self.cancel_dir.glob("*"):
            p.unlink(missing_ok=True)

    # ----- state snapshot -----------------------------------------------------

    def _write_state(self) -> None:
        snap = {
            "nodes": [
                {
                    "ip": n.ip,
                    "total_gpus": n.total_gpus,
                    "gpus_in_use": sorted(n.gpus_in_use),
                    "free_count": n.free_count(),
                }
                for n in self.nodes
            ],
            "running": [
                {
                    "id": jid,
                    "name": j.spec.get("name"),
                    "node": j.node_ip,
                    "gpus": j.gpus,
                    "remote_pid": j.remote_pid,
                    "started_at": j.started_at,
                    "elapsed_s": time.time() - j.started_at,
                }
                for jid, j in sorted(self.running.items())
            ],
            "pending_count": sum(1 for _ in self.pending_dir.glob("*.json")),
            "updated_at": time.time(),
        }
        _atomic_write_json(self.state_path, snap)

    # ----- signal handling ----------------------------------------------------

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):
            self.shutdown_requested = True
            self._log(f"signal {signum} received, shutting down")
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def _shutdown(self) -> None:
        """Signal every running job, then close out cleanly."""
        for jid, job in list(self.running.items()):
            self._send_remote_signal(job, signal.SIGTERM)
        deadline = time.time() + 30
        while self.running and time.time() < deadline:
            self._check_remote_pids()
            self._reap()
            time.sleep(0.5)
        # Anything still running: SIGKILL it, then kill the local ssh too.
        for jid, job in list(self.running.items()):
            self._send_remote_signal(job, signal.SIGKILL)
            try:
                os.killpg(os.getpgid(job.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self._reap()
        self._write_state()
        self._log("daemon exited")

    # ----- main loop ----------------------------------------------------------

    def run(self) -> None:
        self._install_signal_handlers()
        self._recover_orphans()
        node_summary = ", ".join(f"{n.ip}({n.total_gpus})" for n in self.nodes)
        self._log(f"daemon started: queue_dir={self.queue_dir} nodes=[{node_summary}]")
        while not self.shutdown_requested:
            self._check_remote_pids()
            self._scan_cancel_requests()
            self._retry_pending_cancels()
            self._reap()
            self._schedule()
            self._write_state()
            time.sleep(POLL_INTERVAL_S)
        self._shutdown()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE_DIR)
    p.add_argument("--nodes-file", type=Path, default=DEFAULT_NODES_FILE)
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    Daemon(args.queue_dir, args.nodes_file).run()


if __name__ == "__main__":
    main()
