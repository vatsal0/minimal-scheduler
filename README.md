# minimal-scheduler

A minimal, multi-node, single-user GPU-aware job queue. Drop jobspec JSON
files into a directory; a single daemon (on the home node) picks them up and
launches each on the first node with enough free GPUs, all via ssh. Per-node
first-fit backfill. No external dependencies beyond the Python standard
library, ssh, and systemd.

## Files

| File | Role |
|---|---|
| `gpu_queue.py`      | The daemon. Reads `nodes.txt`, polls `<queue>/pending/`, and for each job picks the first node with enough free GPUs. Every job is launched through ssh — even on the home node — so launch/reap/cancel have one code path. |
| `nodes.txt`         | Machine-local config (gitignored). One IP per line, first = home node. `<ip> [gpu_count]`; gpu_count is autodetected if omitted. If missing, daemon falls back to single-node-this-host. |
| `install.sh`        | One-shot installer (sudo). Verifies ssh access to every node in `nodes.txt` **before** anything else; aborts on failure. Then: creates queue dir, writes ssh ControlMaster block to `~/.ssh/config`, renders + enables systemd unit, appends shell helpers to `~/.bashrc`. Each part is idempotent. |
| `uninstall.sh`      | Reverse of `install.sh` (sudo). Removes systemd unit, ssh block, and bashrc block. Leaves the queue dir alone but prints its path. |
| `gpu_queue.service` | systemd unit *template* (placeholders rendered by `install.sh`). The daemon lives on the home node only. |

## Subdirectories

`functions/` — user-facing CLIs. After `install.sh`, each is a bash function with the same name.

| File | Bash | Role |
|---|---|---|
| `functions/submit.py` | `submit` | Drops a jobspec JSON into `<queue>/pending/`. Refuses if your cwd isn't under `/mnt/vast` (executing node must see it). Records `submit_cwd` so logs land next to where you submitted. Merges `<repo>/.env` into the job's env (see [Job env vars](#job-env-vars-wandb-hf-tokens-etc)). Importable as `from functions.submit import submit`. |
| `functions/queue.py`  | `queue`  | Prints per-node GPU usage + running/pending/done tables. Shows which node each running job is on. |
| `functions/log.py`    | `log`    | Cats stdout from `<submit_cwd>/job-<id>.out`; `-f` follows; `--err` for stderr. |
| `functions/cancel.py` | `cancel` | Pending: moves spec to `<queue>/done/` directly. Running: drops a marker at `<queue>/cancel/<id>`; the daemon ssh's into the executing node next tick to SIGTERM the remote process group. |

## Where things live

- **Repo (this directory)**: lives on shared NFS (`/mnt/vast/...`) so every node sees the same code. Safe to symlink back into `~/` on the home node.
- **Queue runtime**: `<repo>/queue/` by default (overridable via `MINSCHED_QUEUE_DIR`). Contains `pending/ running/ done/ cancel/ state.json .next_id daemon.log` and short-lived `<id>.remote_pid` files in `running/`.
- **Per-job logs**: `<submit_cwd>/job-<id>.out` / `.err`. `submit_cwd` is validated to be under `/mnt/vast` so the executing node can write and you can read.
- **nodes.txt**: machine-local, gitignored. Lives at repo root.
- **.env**: gitignored secrets file at repo root. `submit` merges its contents into every job's env. Created as a stub by `install.sh` (mode 600).

## Install

```bash
# 1. Make sure passwordless ssh works from this host to every node in nodes.txt
#    (including this host itself — we always go via ssh, even for local jobs).
# 2. Create nodes.txt at the repo root.

cat > nodes.txt <<EOF
# first line = home node (where the daemon will run)
91.239.86.226
91.194.200.134
EOF

sudo bash install.sh
```

`install.sh` verifies ssh+nvidia-smi to every node first; if any fails the
script aborts before changing anything. Then it creates the queue dir, writes
the ssh ControlMaster block to `~/.ssh/config`, installs + enables the systemd
unit, and appends the shell-helpers block to `~/.bashrc`. Each part is
idempotent.

Pick a different python or queue location at install time:

```bash
sudo MINSCHED_PY=/mnt/vast/vatsal/miniconda3/bin/python \
     MINSCHED_QUEUE_DIR=/some/other/queue \
     bash install.sh
```

| | command |
|---|---|
| Status        | `sudo systemctl status gpu_queue` |
| Daemon log    | `journalctl -u gpu_queue -f` or `tail -f "$MINSCHED_QUEUE_DIR/daemon.log"` |
| Restart       | `sudo systemctl restart gpu_queue` |
| Stop          | `sudo systemctl stop gpu_queue` |
| Uninstall     | `sudo bash uninstall.sh` (or do it by hand: `sudo systemctl disable --now gpu_queue && sudo rm /etc/systemd/system/gpu_queue.service`, plus delete the marker block from `~/.bashrc`) |

`queue` renders timestamps in the IANA timezone set by `MINSCHED_TZ` (e.g. `America/New_York`); unset = system time (UTC on these nodes). Set it in your bashrc next to the other `MINSCHED_*` exports.

After install, `exec bash` (or open a new shell) to pick up the shell helpers:

```bash
submit --gpus 4 --name myrun -- python train.py --n_layer 16 ...
queue
log 17 -f
cancel 17                # cancel one job
cancel --all             # cancel everything pending + running
cancel --all --pending   # only pending
```

## Submit a job

```bash
# Shell function (after install):
submit --gpus 4 --name myrun -- python train.py --n_layer 16 ...

# Direct python (no shell helpers):
python functions/submit.py --gpus 4 --name myrun -- python train.py ...

# Or from a Python script (with this repo on PYTHONPATH):
from functions.submit import submit
submit(cmd=["python", "train.py", "--foo", "bar"], gpus=8, name="myrun")
```

## Job env vars (wandb, HF tokens, etc.)

Remote nodes don't inherit your shell env — they only see what's baked into
the job's spec. To make `WANDB_API_KEY` / `HF_TOKEN` / etc. available, put
them in `<repo>/.env`:

```bash
# /mnt/vast/vatsal/minimal-scheduler/.env  (gitignored, chmod 600)
WANDB_API_KEY=...
WANDB_PROJECT=my-project
HF_TOKEN=...
```

Format: one `KEY=VAL` per line. `#` comments and blank lines are ignored.
`export KEY=VAL` is also accepted (for copy-paste convenience). No shell
expansion — `$VAR` and `$(...)` are kept literal. Paired quotes around the
value are stripped.

Every `submit` reads this file and merges it into the job's `env`. Explicit
`--env KEY=VAL` flags override values from the file:

```bash
submit --gpus 4 --env WANDB_RUN_GROUP=ablation-1 -- python train.py
#                ↑ from CLI, overrides .env if WANDB_RUN_GROUP is also there
```

Point at a different file with `--env-file /path/to/other.env`. `install.sh`
creates a stub `.env` (mode 600) on a fresh install.

Logs land in the dir you ran `submit` from:

```
$ cd /tmp/experiment-42
$ submit --gpus 1 --name probe -- python probe.py
submitted job 17
$ ls
job-17.out  job-17.err  probe.py
```

## Watch what's happening

```bash
queue                                  # snapshot (or: python functions/queue.py)
watch -n 2 queue                       # auto-refresh
log 17 -f                             # follow job 17's stdout
log 17 --err                          # job 17's stderr
tail -f "$MINSCHED_QUEUE_DIR/daemon.log"   # daemon's own log
journalctl -u gpu_queue -f             # daemon log via systemd journal
```

## How scheduling works

Every 1s the daemon:
1. Reads `cancel/*` markers and sends SIGTERM to the corresponding remote pgids.
2. Reaps finished jobs (the supervising ssh has exited), frees their GPUs on
   the node they ran on.
3. Scans `<queue>/pending/` sorted by `submitted_at`.
4. For each pending job: walks `nodes.txt` top-to-bottom and picks the first
   node whose free GPUs ≥ the job's request. Launches there via ssh; otherwise
   tries the next pending job (first-fit backfill within node ordering).

Multi-node jobs aren't supported — a job that needs 12 GPUs on an 8-GPU node
just sits pending. Pack accordingly.

GPU assignment within a node is "lowest IDs first" from that node's free set,
so jobs prefer GPUs 0, 1, 2... per node.

## Launch mechanics

For every job the daemon Popens:

```
ssh <ip> bash -lc 'setsid bash -c "<wrapper script>"'
```

The wrapper `cd`s to the job's cwd, exports `CUDA_VISIBLE_DEVICES`, redirects
stdio to `<submit_cwd>/job-<id>.{out,err}`, writes its own pid (= the new
session's pgid) to `<queue>/running/<id>.remote_pid`, and `exec`s the user
command. SSH stays connected for the entire job lifetime; when the user
command exits, ssh exits with the same code, and the daemon's `Popen.poll()`
picks it up next tick.

Cancellation goes the other way around the same pipe: `cancel.py` drops a
file at `<queue>/cancel/<id>`, the daemon notices it and runs
`ssh <ip> kill -TERM -<remote_pid>` (negative = pgid).

## Recovery

If the daemon dies mid-job, ssh dies with it (the supervising ssh is the
daemon's child), and `setsid` keeps the remote command alive but unsupervised.
On restart the daemon marks stale `<queue>/running/*` as orphaned
(`exit_code=-1`) and clears stale cancel markers + remote_pid files. The
actual remote process keeps running until it finishes; find it with
`ssh <ip> ps -eo pid,pgid,cmd | grep ...` and kill it manually if needed.

## Why a system-level systemd unit (not `--user`)

`systemd --user` units require a user session bus
(`$DBUS_SESSION_BUS_ADDRESS`, `$XDG_RUNTIME_DIR`), which a plain `sudo -s`
root shell doesn't have — `systemctl --user daemon-reload` fails with
"Failed to connect to bus." A system-level unit with `User=<you>` avoids
that whole class of problem, survives reboot without `loginctl enable-linger`,
and behaves identically from the daemon's perspective.

## Queue layout (runtime)

```
$MINSCHED_QUEUE_DIR/                 (default: <repo>/queue)
  pending/<id>.json                  queued, not running
  running/<id>.json                  allocated; in flight
  running/<id>.remote_pid            written by ssh wrapper once user cmd starts
  cancel/<id>                        marker file, dropped by `cancel`
  done/<id>.json                     terminal (success or failure)
  state.json                         snapshot for `queue`
  .next_id                           monotonic counter
  daemon.log                         daemon stdout/stderr (via systemd)

<submit_cwd>/                        (where you ran submit; must be /mnt/vast)
  job-<id>.out                       job stdout
  job-<id>.err                       job stderr
```
