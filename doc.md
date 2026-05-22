# minimal-scheduler — implementation notes

Multi-node GPU job queue. One daemon on the home node schedules jobs across
nodes listed in `nodes.txt`. Every job is launched through ssh (even on the
home node), so launch/reap/cancel have a single code path.

## File roles

- `gpu_queue.py` — the daemon. Reads `nodes.txt`, owns `state.json`, polls
  `queue/pending/` and `queue/cancel/`, spawns one supervising ssh per job.
  Single-threaded, ~1s polling tick.
- `nodes.txt` — machine-local, gitignored. One IP per line in priority order
  (first = home node). Optional second column = GPU count override.
- `install.sh` / `uninstall.sh` — sudo-required, idempotent. `install.sh`
  preflights every IP in `nodes.txt` (ssh + nvidia-smi) before touching
  anything; aborts on failure.
- `gpu_queue.service` — systemd unit template. The daemon runs only on the
  home node; remote nodes have no scheduler-side install at all.
- `functions/` — the four CLIs (`submit`, `queue`, `log`, `cancel`). See
  `functions/doc.md`.
- `queue/` — runtime state (gitignored). Lives on `/mnt/vast` so all nodes
  see logs + spec files at the same paths.

## How a job flows through the system

1. User runs `submit --gpus N -- <cmd>` from a directory under `/mnt/vast`.
   `submit.py` validates the cwd, atomically writes `queue/pending/<id>.json`.
2. Daemon's tick spots the new pending file, sorts pending by `submitted_at`,
   walks nodes in `nodes.txt` order looking for one with ≥N free GPUs.
3. On a match, daemon builds a wrapper script and `Popen`s
   `ssh <ip> bash -lc 'setsid bash -c "<wrapper>"'`. Wrapper writes its own
   pid (= new session pgid) to `queue/running/<id>.remote_pid`, then `exec`s
   the user command.
4. Spec is moved `pending/ → running/` with `node`, `gpus`, `started_at`
   fields added. The supervising ssh stays connected for the job's lifetime.
5. On job exit, ssh exits with the same code. Next tick, daemon's
   `proc.poll()` returns non-None; daemon releases GPUs on `node`, writes
   the spec to `done/` with `exit_code` and `finished_at`.

## Cancellation

`cancel.py` never signals processes directly (remote pids aren't local pgids).
For a running job it touches `queue/cancel/<id>` and exits. The daemon's tick
picks up the marker, deletes it, looks up the job, and runs
`ssh <ip> kill -TERM -<remote_pid>` (negative number = pgid).

If the remote_pid isn't yet known (race between scheduler and wrapper write),
the daemon flags `cancel_pending=True` on the job and re-tries each tick.

## Recovery

Daemon startup:
- Every `running/*.json` is treated as orphaned (the supervising ssh died
  with the previous daemon process), moved to `done/` with `exit_code=-1`.
- Stale `remote_pid` files and stale cancel markers are deleted.
- GPU bookkeeping starts fresh — no in-flight jobs from the previous run.

The remote user process keeps running (it was setsid'd, so it survives ssh
disconnect). Clean it up manually if needed:
`ssh <ip> ps -eo pid,pgid,cmd | grep <something>`.

## Why ssh-for-everything

- Single launch path = simpler reap/cancel logic.
- Cancellation: the daemon never needs to distinguish local vs remote — it's
  always `ssh <ip> kill -<sig> -<pgid>`.
- ssh connection lifetime = job lifetime, so `Popen.poll()` is the only
  liveness signal needed. No polling remote pids.
- Cost: ~150ms ssh setup per launch (amortized with ControlMaster set up
  by `install.sh`).
