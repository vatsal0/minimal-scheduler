"""Print the GPU queue's current state.

Reads queue/state.json (daemon-maintained) plus the pending/, running/,
done/ directories. Shows running jobs, pending jobs, recent done jobs.

CLI:
    python functions/queue.py                # default view
    python functions/queue.py --done 20      # show last N done
    python functions/queue.py --json         # machine-readable
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


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:4.0f}s"
    if seconds < 3600:
        return f"{seconds/60:4.1f}m"
    if seconds < 86400:
        return f"{seconds/3600:4.1f}h"
    return f"{seconds/86400:4.1f}d"


def _load_specs(d: Path) -> list:
    specs = []
    for p in d.glob("*.json"):
        try:
            specs.append(json.loads(p.read_text()))
        except Exception:
            continue
    return specs


def gather(queue_dir: Path) -> dict:
    state_path = queue_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    pending = _load_specs(queue_dir / "pending")
    pending.sort(key=lambda s: (s.get("submitted_at", 0), s.get("id", 0)))
    running = _load_specs(queue_dir / "running")
    running.sort(key=lambda s: s.get("started_at", 0))
    done = _load_specs(queue_dir / "done")
    done.sort(key=lambda s: s.get("finished_at", 0), reverse=True)
    return {"state": state, "pending": pending, "running": running, "done": done}


def _fmt_gpus(gpus) -> str:
    """Compact list, no spaces between commas: [0,1,2,3]."""
    if not isinstance(gpus, (list, tuple)):
        return str(gpus)
    return "[" + ",".join(str(g) for g in gpus) + "]"


# Render all timestamps in this TZ (IANA name, e.g. "America/New_York").
# Empty/unset → system local time, which is UTC on these machines.
_TZ = None
_tz_name = os.environ.get("MINSCHED_TZ", "").strip()
if _tz_name:
    try:
        from zoneinfo import ZoneInfo
        _TZ = ZoneInfo(_tz_name)
    except Exception as e:
        sys.stderr.write(f"warning: MINSCHED_TZ={_tz_name!r} not loadable ({e}); "
                         f"falling back to system time\n")


def _localtime(ts: float):
    """time.struct_time in the configured TZ, or system local if unset."""
    if _TZ is None:
        return time.localtime(ts)
    # datetime → struct_time so the existing strftime calls keep working
    from datetime import datetime
    return datetime.fromtimestamp(ts, tz=_TZ).timetuple()


def _fmt_clock(ts: float) -> str:
    """Wall-clock HH:MM:SS in the configured TZ."""
    return time.strftime("%H:%M:%S", _localtime(ts))


def _fmt_timestamp(ts) -> str:
    """'YYYY-MM-DD HH:MM:SS' in the configured TZ."""
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M:%S", _localtime(ts))


def _fmt_duration(seconds) -> str:
    """Human-readable duration: '45s', '5m 30s', '2h 15m 30s', '1d 4h 30m'."""
    if seconds is None or seconds < 0:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m {s}s"
    d, h = divmod(h, 24)
    return f"{d}d {h}h {m}m"


def _fmt_status(rc) -> str:
    if rc is None:
        return "?"
    if rc == 0:
        return "success"
    return f"ERROR RC={rc}"


# --- pretty printer -----------------------------------------------------------
# ANSI on iff stdout is a TTY and NO_COLOR is unset. Pipes and --json stay plain.
_TTY = sys.stdout.isatty() and "NO_COLOR" not in os.environ
DIM, BOLD, CYAN, GREEN, RED, YELLOW = "2", "1", "36", "32", "31", "33"
NAME_MAX = 32  # truncate job names beyond this so tables don't blow up


def _ansi(code: str, s: str) -> str:
    if not _TTY or not code:
        return s
    return f"\033[{code}m{s}\033[0m"


def _box_table(title: str, headers, rows, aligns, col_pad: int = 2) -> str:
    """Render one bordered table with a colored title strip on top.

    `headers`: list[str].
    `rows`:    list[list], each cell is either `str` or `(text, ansi_code)`.
    `aligns`:  list[str], `'<'` or `'>'` per column.
    """
    def _txt(c):
        return c[0] if isinstance(c, tuple) else str(c)

    def _styled_cell(c, w, a):
        t = _txt(c)
        color = c[1] if isinstance(c, tuple) else ""
        if a == ">":
            return " " * (w - len(t)) + _ansi(color, t)
        return _ansi(color, t) + " " * (w - len(t))

    n = len(headers)
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(_txt(c)))

    sep = " " * col_pad
    body_width = sum(widths) + col_pad * (n - 1)
    # Guarantee the title fits across the top of the box.
    min_for_title = len(title) + 4
    if body_width < min_for_title:
        widths[-1] += (min_for_title - body_width)
        body_width = min_for_title
    inner = body_width + 2  # one-space pad on each side of the body

    fill_len = inner - len(title) - 3  # "╭─ " + title + " " + "─"*fill + "╮"
    top = (_ansi(DIM, "╭─ ")
           + _ansi(f"{BOLD};{CYAN}", title)
           + _ansi(DIM, " " + "─" * fill_len + "╮"))
    bot = _ansi(DIM, "╰" + "─" * inner + "╯")
    bar = _ansi(DIM, "│")

    # Header row + thin rule beneath.
    header_parts = []
    for h, w, a in zip(headers, widths, aligns):
        padded = h.rjust(w) if a == ">" else h.ljust(w)
        header_parts.append(_ansi(DIM, padded))
    header_line = bar + " " + sep.join(header_parts) + " " + bar
    rule_line = bar + " " + sep.join(_ansi(DIM, "─" * w) for w in widths) + " " + bar

    if rows:
        data_lines = [
            bar + " " + sep.join(_styled_cell(c, w, a) for c, w, a in zip(r, widths, aligns)) + " " + bar
            for r in rows
        ]
    else:
        empty_txt = "(none)"
        pad = body_width - len(empty_txt)
        data_lines = [bar + " " + _ansi(DIM, empty_txt) + " " * pad + " " + bar]

    return "\n".join([top, header_line, rule_line] + data_lines + [bot])


def render(snap: dict, done_n: int) -> str:
    state = snap["state"]
    now = time.time()
    lines = []

    # Daemon status: one row per node so multi-node is legible at a glance.
    if state:
        nodes = state.get("nodes", [])
        last = state.get("updated_at", 0)
        last_ago = f"{now - last:.0f}s ago" if last else "never"
        lines.append(_ansi(BOLD, "daemon  ") + f"updated {_ansi(DIM, last_ago)}")
        node_rows = []
        for n in nodes:
            total = n.get("total_gpus", 0)
            in_use = n.get("gpus_in_use", [])
            busy = len(in_use)
            node_rows.append([
                n.get("ip", "?"),
                f"{busy}/{total}",
                (_fmt_gpus(in_use) if in_use else "-", DIM),
            ])
        if node_rows:
            lines.append(_box_table(
                "NODES",
                ["ip", "busy", "in_use"],
                node_rows,
                ["<", ">", "<"],
            ))
    else:
        lines.append(_ansi(YELLOW, "daemon: state.json missing — is the daemon running?"))
    lines.append("")

    # RUNNING
    running_rows = []
    for s in snap["running"]:
        running_rows.append([
            (str(s["id"]), DIM),
            s.get("name", "?")[:NAME_MAX],
            s.get("node", "?"),
            _fmt_gpus(s.get("gpus", "-")),
            _fmt_clock(s.get("started_at", now)),
            (str(s.get("remote_pid", s.get("pid", "-"))), DIM),
        ])
    lines.append(_box_table(
        f"RUNNING ({len(snap['running'])})",
        ["id", "name", "node", "gpus", "start", "pid"],
        running_rows,
        [">", "<", "<", "<", ">", ">"],
    ))
    lines.append("")

    # PENDING
    pending_rows = []
    for s in snap["pending"]:
        wait = now - s.get("submitted_at", now)
        pending_rows.append([
            (str(s["id"]), DIM),
            s.get("name", "?")[:NAME_MAX],
            str(s.get("gpus", "?")),
            _fmt_age(wait),
        ])
    lines.append(_box_table(
        f"PENDING ({len(snap['pending'])})",
        ["id", "name", "gpus", "wait"],
        pending_rows,
        [">", "<", ">", ">"],
    ))
    lines.append("")

    # DONE
    recent = snap["done"][:done_n]
    done_rows = []
    for s in recent:
        rc = s.get("exit_code")
        status_color = "" if rc is None else (GREEN if rc == 0 else RED)
        started = s.get("started_at")
        finished = s.get("finished_at")
        runtime_s = (finished - started) if (started and finished) else None
        done_rows.append([
            (str(s["id"]), DIM),
            s.get("name", "?")[:NAME_MAX],
            (_fmt_status(rc), status_color),
            _fmt_timestamp(started),
            _fmt_timestamp(finished),
            _fmt_duration(runtime_s),
        ])
    lines.append(_box_table(
        f"DONE ({len(recent)})",
        ["id", "name", "status", "start", "end", "runtime"],
        done_rows,
        [">", "<", "<", "<", "<", ">"],
    ))

    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE_DIR)
    p.add_argument("--done", type=int, default=10,
                   help="Number of recent done jobs to show (default 10).")
    p.add_argument("--json", action="store_true",
                   help="Dump everything as JSON instead of the table.")
    args = p.parse_args()

    snap = gather(args.queue_dir)
    if args.json:
        print(json.dumps(snap, indent=2, sort_keys=True, default=str))
    else:
        print(render(snap, args.done))


if __name__ == "__main__":
    sys.exit(main())
