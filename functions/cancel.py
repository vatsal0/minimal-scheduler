"""Cancel queued or running jobs.

Pending: spec is moved from queue/pending/ to queue/done/ directly (the
daemon hasn't touched it yet) with exit_code=-3 + error="cancelled".

Running: we drop a marker file at queue/cancel/<id>. The daemon picks it up
on its next tick and ssh's into the executing node to SIGTERM the remote
process group. We don't signal the child ourselves because for remote jobs
there's no local pid we can signal.

CLI:
    python functions/cancel.py 17                 # cancel one job
    python functions/cancel.py 17 18 19           # cancel several
    python functions/cancel.py --all              # cancel all pending + running
    python functions/cancel.py --all --pending    # all pending only
    python functions/cancel.py --all --running    # all running only
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

_QUEUE_ENV = os.environ.get("MINSCHED_QUEUE_DIR")
DEFAULT_QUEUE_DIR = (
    Path(_QUEUE_ENV) if _QUEUE_ENV
    else Path(__file__).resolve().parent.parent / "queue"
)


def _load_spec(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    tmp.replace(path)


def cancel_pending(jid: int, queue_dir: Path) -> bool:
    """Move queue/pending/<id>.json to queue/done/ with cancelled annotation."""
    src = queue_dir / "pending" / f"{jid}.json"
    if not src.exists():
        return False
    spec = _load_spec(src) or {"id": jid}
    spec = dict(spec)
    spec["exit_code"] = -3
    spec["finished_at"] = time.time()
    spec["error"] = "cancelled while pending"
    done_dir = queue_dir / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(done_dir / src.name, spec)
    src.unlink(missing_ok=True)
    return True


def cancel_running(jid: int, queue_dir: Path) -> bool:
    """Drop a cancel/<id> marker; daemon does the actual remote signal."""
    src = queue_dir / "running" / f"{jid}.json"
    if not src.exists():
        return False
    cancel_dir = queue_dir / "cancel"
    cancel_dir.mkdir(parents=True, exist_ok=True)
    # touch is idempotent — multiple cancels collapse into one marker
    (cancel_dir / str(jid)).touch()
    return True


def _ids_in_dir(d: Path) -> list:
    ids = []
    for p in d.glob("*.json"):
        try:
            ids.append(int(p.stem))
        except ValueError:
            continue
    return sorted(ids)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ids", nargs="*", type=int, help="Job IDs to cancel.")
    p.add_argument("--all", action="store_true",
                   help="Cancel everything (pending + running).")
    p.add_argument("--pending", action="store_true",
                   help="With --all: restrict to pending jobs.")
    p.add_argument("--running", action="store_true",
                   help="With --all: restrict to running jobs.")
    p.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE_DIR)
    args = p.parse_args()

    if args.all and args.ids:
        p.error("either pass job ids or --all, not both")
    if not args.all and not args.ids:
        p.error("pass at least one job id, or --all")
    if not args.all and (args.pending or args.running):
        p.error("--pending / --running only make sense with --all")

    pending_dir = args.queue_dir / "pending"
    running_dir = args.queue_dir / "running"

    if args.all:
        do_pending = args.pending or not args.running
        do_running = args.running or not args.pending
        pending_ids = _ids_in_dir(pending_dir) if do_pending else []
        running_ids = _ids_in_dir(running_dir) if do_running else []
    else:
        pending_ids = []
        running_ids = []
        for jid in args.ids:
            if (pending_dir / f"{jid}.json").exists():
                pending_ids.append(jid)
            elif (running_dir / f"{jid}.json").exists():
                running_ids.append(jid)
            else:
                print(f"job {jid}: not pending or running")

    for jid in pending_ids:
        if cancel_pending(jid, args.queue_dir):
            print(f"cancelled pending job {jid}")
        else:
            print(f"job {jid}: failed to cancel (not pending)")
    for jid in running_ids:
        if cancel_running(jid, args.queue_dir):
            print(f"cancel marker dropped for running job {jid} (daemon will signal next tick)")
        else:
            print(f"job {jid}: failed to cancel (not running)")


if __name__ == "__main__":
    sys.exit(main())
