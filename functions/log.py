"""Tail or cat a queued job's log.

Logs are written to the dir where submit was called from (recorded in the
jobspec's `submit_cwd`), as `job-<id>.out` / `job-<id>.err`.

CLI:
    python functions/log.py <id>            # cat job-<id>.out
    python functions/log.py <id> --err      # cat job-<id>.err
    python functions/log.py <id> -f         # follow (like tail -f)
    python functions/log.py                 # newest running job (or newest done)
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_QUEUE_ENV = os.environ.get("MINSCHED_QUEUE_DIR")
DEFAULT_QUEUE_DIR = (
    Path(_QUEUE_ENV) if _QUEUE_ENV
    else Path(__file__).resolve().parent.parent / "queue"
)


def _resolve_id(queue_dir: Path, jid) -> int:
    if jid is not None:
        return int(jid)
    # default: newest running job, else newest done.
    running = sorted((queue_dir / "running").glob("*.json"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if running:
        return int(json.loads(running[0].read_text())["id"])
    done = sorted((queue_dir / "done").glob("*.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if done:
        return int(json.loads(done[0].read_text())["id"])
    sys.exit("no running or done jobs to tail")


def _find_spec(queue_dir: Path, jid: int) -> dict:
    """Look up a jobspec by id across pending/running/done. Returns None if absent."""
    for sub in ("running", "pending", "done"):
        p = queue_dir / sub / f"{jid}.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return None
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("id", nargs="?", default=None,
                   help="Job id (default: newest running, else newest done).")
    p.add_argument("--err", action="store_true", help="Show stderr instead of stdout.")
    p.add_argument("-f", "--follow", action="store_true",
                   help="Follow the log (like tail -f).")
    p.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE_DIR)
    args = p.parse_args()

    jid = _resolve_id(args.queue_dir, args.id)
    spec = _find_spec(args.queue_dir, jid)
    if spec is None:
        sys.exit(f"no jobspec found for job {jid}")
    # submit_cwd is where submit was called; fall back to cwd for any pre-existing
    # specs from an older submit.py that didn't record submit_cwd.
    log_dir = Path(spec.get("submit_cwd") or spec.get("cwd") or args.queue_dir)
    suffix = "err" if args.err else "out"
    log_path = log_dir / f"job-{jid}.{suffix}"
    if not log_path.exists():
        sys.exit(f"no log at {log_path}")

    if args.follow:
        # delegate to `tail -F` so we get rotation-safe behavior for free
        subprocess.call(["tail", "-F", "-n", "+1", str(log_path)])
    else:
        sys.stdout.buffer.write(log_path.read_bytes())


if __name__ == "__main__":
    main()
