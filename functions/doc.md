# functions/ — user-facing CLIs

Four small CLIs that all read `MINSCHED_QUEUE_DIR` (default `<repo>/queue`).
After `install.sh`, they're exposed as bash functions on the home node:
`submit`, `queue`, `log`, `cancel`. Remote nodes do not run these — they're
only ever invoked on the home node, which talks to remote nodes via ssh.

## File roles

- `submit.py` — Writes `<queue>/pending/<id>.json` and prints the id. The id
  is allocated under a flock'd `.next_id` counter so two concurrent submits
  don't collide. Refuses if your cwd (or `--cwd`) isn't under `/mnt/vast`
  (the executing node has to see it); override with `MINSCHED_SKIP_VAST_CHECK=1`.
  Reads `<repo>/.env` (gitignored secrets file, KEY=VAL lines) and merges
  it into the job's env; `--env KEY=VAL` overrides file values. Importable:
  `from functions.submit import submit(cmd, gpus, ...)`.
- `queue.py` — Reads `<queue>/state.json` and the three subdirs, renders
  bordered tables: per-node GPU usage; RUNNING with `node` column; PENDING;
  recent DONE. `--json` for machine-readable dump.
- `log.py` — Resolves a job id to its spec (under `pending/`, `running/`, or
  `done/`), reads the recorded `submit_cwd`, and cats `<submit_cwd>/job-<id>.{out,err}`.
  `-f` delegates to `tail -F` for rotation-safe follow.
- `cancel.py` — Pending: moves the spec straight to `done/` with
  `exit_code=-3 error="cancelled while pending"`. Running: touches
  `<queue>/cancel/<id>`; the daemon does the actual remote signal next tick.
  Never signals processes itself (remote pids aren't local pgids).

## Interactions with the daemon

These CLIs **never communicate with the daemon directly** — they're file-based
producers/consumers around the queue dir:

```
submit.py  ──writes──>  pending/<id>.json   ──read by──>  daemon
cancel.py  ──writes──>  cancel/<id>         ──read by──>  daemon
queue.py   ──reads──>   state.json + pending/ running/ done/
log.py     ──reads──>   submit_cwd recorded in spec
```

This is the whole interface contract. No sockets, no RPC. Means the CLIs
work even if the daemon is down (queue.py will just show stale state and
`cancel` markers will sit unprocessed until the daemon comes back up).

## Conventions

- Default queue dir = `<repo>/queue`. Set `MINSCHED_QUEUE_DIR` to override.
- All json writes go through `_atomic_write_json` (tmp file + rename), so
  the daemon never reads a half-written spec.
- `submit_cwd` (where you ran `submit`) and `cwd` (where the job runs) are
  both stored; `log` uses `submit_cwd`, daemon uses `cwd` when launching.
