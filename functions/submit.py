"""Submit a job to the GPU queue daemon.

Drops a jobspec JSON into queue/pending/<id>.json and returns the id.
The daemon (gpu_queue.py) picks it up on its next tick.

CLI:
    python functions/submit.py --gpus 4 --name myjob -- python train.py --foo bar
    python functions/submit.py --gpus 1 -- bash -lc 'echo hi'

Importable (with this repo on PYTHONPATH):
    from functions.submit import submit
    submit(cmd=["python", "..."], gpus=4, name="myjob", cwd="/path/to/workdir")
"""
import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

_QUEUE_ENV = os.environ.get("MINSCHED_QUEUE_DIR")
# Default to <repo>/queue. The home-dir fallback is only kept for the
# pre-multi-node single-node install; new installs always set MINSCHED_QUEUE_DIR.
DEFAULT_QUEUE_DIR = (
    Path(_QUEUE_ENV) if _QUEUE_ENV
    else Path(__file__).resolve().parent.parent / "queue"
)
# Multi-node jobs need submit_cwd to be visible on every executing node, so
# we require it under the shared NFS mount. Set MINSCHED_SKIP_VAST_CHECK=1 to
# skip (e.g. for a single-node install).
SHARED_FS_PREFIX = os.environ.get("MINSCHED_SHARED_FS", "/mnt/vast")
SKIP_VAST_CHECK = os.environ.get("MINSCHED_SKIP_VAST_CHECK") == "1"
# Default dotenv lives at <repo>/.env (gitignored). Override via MINSCHED_ENV_FILE.
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _parse_env_file(path: Path) -> dict:
    """Minimal KEY=VAL parser. Blank lines and `#` comments ignored.

    No shell expansion, no quote stripping beyond paired ' or ". Lines that
    don't look like KEY=VAL are skipped with a stderr warning.
    """
    out = {}
    if not path.exists():
        return out
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Allow `export KEY=VAL` for copy-paste convenience.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            sys.stderr.write(f"{path}:{n}: skipping malformed line: {raw!r}\n")
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k] = v
    return out


def _atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    tmp.replace(path)


def _next_id(queue_dir: Path) -> int:
    """Monotonic counter, file-locked so two qsubs can't collide."""
    counter = queue_dir / ".next_id"
    queue_dir.mkdir(parents=True, exist_ok=True)
    # Open with O_RDWR | O_CREAT, lock exclusive, read-modify-write.
    fd = os.open(counter, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 64).decode().strip()
        cur = int(raw) if raw else 0
        nxt = cur + 1
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(nxt).encode())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return nxt


def submit(cmd, gpus: int, name: str = None, cwd: str = None,
         env: dict = None, queue_dir: Path = DEFAULT_QUEUE_DIR,
         env_file: Path = DEFAULT_ENV_FILE) -> int:
    """Enqueue a job and return its id.

    Args:
        cmd:  argv list to run.
        gpus: number of GPUs the job needs.
        name: optional label (defaults to first cmd token).
        cwd:  working directory (defaults to the caller's cwd).
        env:  extra env vars on top of the daemon's inherited env.
        queue_dir: queue/ dir (default: <repo>/queue).
        env_file: dotenv file merged into env, with `env` (from --env)
                  taking precedence. Missing file = silently skip.
    """
    queue_dir = Path(queue_dir)
    pending = queue_dir / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    submit_cwd = os.path.realpath(os.getcwd())
    job_cwd = os.path.realpath(cwd) if cwd else submit_cwd
    if not SKIP_VAST_CHECK:
        # Both logs (submit_cwd) and the job's working dir must be visible
        # cross-node, since jobs may run on a remote host.
        for label, p in (("submit cwd", submit_cwd), ("job cwd", job_cwd)):
            if not p.startswith(SHARED_FS_PREFIX):
                raise SystemExit(
                    f"refusing to submit: {label} {p!r} is not under "
                    f"{SHARED_FS_PREFIX!r} — remote nodes won't see it.\n"
                    f"cd into {SHARED_FS_PREFIX} (or pass --cwd <path under it>) "
                    f"and try again. Set MINSCHED_SKIP_VAST_CHECK=1 to override."
                )
    # Merge dotenv into job env. Explicit --env wins (so per-submit overrides
    # are still possible without editing the file).
    merged_env = _parse_env_file(Path(env_file))
    merged_env.update(env or {})
    jid = _next_id(queue_dir)
    spec = {
        "id": jid,
        "name": name or (cmd[0] if cmd else f"job_{jid}"),
        "cmd": list(cmd),
        "cwd": job_cwd,
        # submit_cwd is always the caller's cwd at submit time — logs land
        # here regardless of --cwd. Daemon reads this when opening log files.
        "submit_cwd": submit_cwd,
        "gpus": int(gpus),
        "submitted_at": time.time(),
        "env": merged_env,
    }
    _atomic_write_json(pending / f"{jid}.json", spec)
    return jid


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gpus", type=int, required=True,
                   help="Number of GPUs required.")
    p.add_argument("--name", type=str, default=None,
                   help="Job label shown in queue.")
    p.add_argument("--cwd", type=str, default=None,
                   help="Working directory (default: caller's cwd).")
    p.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE_DIR)
    p.add_argument("--env", action="append", default=[],
                   metavar="KEY=VAL", help="Extra env var (repeatable). "
                   "Overrides values from the dotenv file.")
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE,
                   help=f"Dotenv file to merge into the job env "
                        f"(default: {DEFAULT_ENV_FILE}, gitignored).")
    p.add_argument("cmd", nargs=argparse.REMAINDER,
                   help="Command to run. Use '--' before the command.")
    args = p.parse_args()

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        p.error("no command supplied; pass it after '--'.")

    env = {}
    for kv in args.env:
        if "=" not in kv:
            p.error(f"--env {kv!r} must be KEY=VAL")
        k, v = kv.split("=", 1)
        env[k] = v

    jid = submit(cmd=cmd, gpus=args.gpus, name=args.name, cwd=args.cwd,
               env=env, queue_dir=args.queue_dir, env_file=args.env_file)
    print(f"submitted job {jid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
