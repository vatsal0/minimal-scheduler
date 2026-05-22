#!/bin/bash
# One-shot installer for minimal-scheduler (multi-node). Idempotent.
#
# Order of operations:
#   0. Read nodes.txt and verify ssh works to every listed IP (FAIL FAST if not).
#   1. Create queue/ runtime tree (default: <repo>/queue, overridable via MINSCHED_QUEUE_DIR).
#   2. Append an ssh ControlMaster block for every node to the invoking user's
#      ~/.ssh/config, wrapped in `# >>> minimal-scheduler ssh ... >>>` markers.
#   3. Render + install systemd unit at /etc/systemd/system/gpu_queue.service,
#      then `systemctl enable --now gpu_queue`.
#   4. Append a block to the invoking user's ~/.bashrc that defines submit /
#      queue / log / cancel as bash functions and exports MINSCHED_QUEUE_DIR.
#      Wrapped in `# >>> minimal-scheduler shell helpers ... >>>` markers.
#
# Re-running skips any step that's already in place. Each marker block is
# rewritten cleanly if you delete it from the destination file.
#
# Usage:
#   sudo bash install.sh
#   sudo MINSCHED_PY=/mnt/vast/vatsal/miniconda3/bin/python bash install.sh
#
# Env overrides:
#   MINSCHED_PY         python interpreter (default: `which python3`)
#   MINSCHED_USER       user to run the daemon as (default: invoking user)
#   MINSCHED_QUEUE_DIR  queue runtime dir (default: <repo>/queue)
#   MINSCHED_NODES_FILE path to nodes.txt (default: <repo>/nodes.txt)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_TEMPLATE="$REPO_ROOT/gpu_queue.service"
UNIT_DST="/etc/systemd/system/gpu_queue.service"
BASHRC_BEGIN="# >>> minimal-scheduler shell helpers (submit/queue/log/cancel) >>>"
BASHRC_END="# <<< minimal-scheduler shell helpers <<<"
SSHCFG_BEGIN="# >>> minimal-scheduler ssh (ControlMaster) >>>"
SSHCFG_END="# <<< minimal-scheduler ssh <<<"

INVOKER="${SUDO_USER:-${USER:-$(id -un)}}"
RUN_AS_USER="${MINSCHED_USER:-$INVOKER}"
RUN_AS_HOME="$(getent passwd "$RUN_AS_USER" | cut -d: -f6)"
INVOKER_HOME="$(getent passwd "$INVOKER" | cut -d: -f6)"
NODES_FILE="${MINSCHED_NODES_FILE:-$REPO_ROOT/nodes.txt}"
QUEUE_DIR="${MINSCHED_QUEUE_DIR:-$REPO_ROOT/queue}"

if [[ -z "$RUN_AS_HOME" || -z "$INVOKER_HOME" ]]; then
    echo "[install] could not resolve home dirs for $RUN_AS_USER / $INVOKER" >&2
    exit 1
fi

if [[ -n "${MINSCHED_PY:-}" ]]; then
    PYTHON_BIN="$MINSCHED_PY"
else
    PYTHON_BIN="$(command -v python3 || true)"
    if [[ -z "$PYTHON_BIN" ]]; then
        echo "[install] no python3 found on PATH. Set MINSCHED_PY=/path/to/python." >&2
        exit 1
    fi
fi

if [[ "${EUID}" -ne 0 ]] && ! sudo -n true 2>/dev/null; then
    echo "[install] needs sudo. Re-run as root or with passwordless sudo." >&2
    echo "  sudo bash $0" >&2
    exit 1
fi
SUDO="sudo"
[[ "${EUID}" -eq 0 ]] && SUDO=""

# Always available "drop to invoker" wrapper, even when we're already root.
# Used for anything that needs the invoker's ~/.ssh, ~/.bashrc, etc.
AS_INVOKER="sudo -u $INVOKER"
[[ "${EUID}" -ne 0 && "$(id -un)" == "$INVOKER" ]] && AS_INVOKER=""

echo "[install] repo:     $REPO_ROOT"
echo "[install] python:   $PYTHON_BIN"
echo "[install] daemon:   runs as $RUN_AS_USER (home=$RUN_AS_HOME)"
echo "[install] queue:    $QUEUE_DIR"
echo "[install] shell:    installs helpers for $INVOKER"
echo "[install] nodes:    $NODES_FILE"

# --- 0. read nodes.txt + verify ssh BEFORE we touch anything ---
echo
NODE_IPS=()
if [[ -f "$NODES_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        # strip comments + trim
        ip="${line%%#*}"
        ip="$(echo "$ip" | awk '{print $1}')"
        [[ -z "$ip" ]] && continue
        NODE_IPS+=("$ip")
    done < "$NODES_FILE"
fi

if [[ ${#NODE_IPS[@]} -eq 0 ]]; then
    echo "[install] nodes.txt missing or empty — installing as single-node (this host only)"
else
    echo "[install] verifying ssh access to ${#NODE_IPS[@]} node(s)..."
    BAD=()
    for ip in "${NODE_IPS[@]}"; do
        # BatchMode=yes: no password prompt — fail immediately if keys aren't set up.
        # Run as the *invoker* so the right ~/.ssh keys/known_hosts get consulted.
        if $AS_INVOKER ssh \
                -o BatchMode=yes \
                -o ConnectTimeout=10 \
                -o StrictHostKeyChecking=accept-new \
                "$ip" 'nvidia-smi -L >/dev/null && echo ok' >/dev/null 2>&1; then
            echo "  [ok]   $ip"
        else
            echo "  [FAIL] $ip"
            BAD+=("$ip")
        fi
    done
    if [[ ${#BAD[@]} -gt 0 ]]; then
        echo
        echo "[install] aborting: ssh+nvidia-smi failed for: ${BAD[*]}" >&2
        echo "[install]   - verify keys with: ssh-copy-id <ip> (or paste pubkey into authorized_keys)" >&2
        echo "[install]   - test by hand:    ssh -o BatchMode=yes <ip> nvidia-smi -L" >&2
        exit 1
    fi
    echo "[install] all nodes reachable."
fi

# --- 1. queue runtime dir ---
echo
if [[ -d "$QUEUE_DIR" ]]; then
    echo "[install] queue dir already exists at $QUEUE_DIR — ensuring subdirs + ownership"
else
    echo "[install] creating queue dir at $QUEUE_DIR (owned by $RUN_AS_USER)"
fi
$SUDO mkdir -p "$QUEUE_DIR/pending" "$QUEUE_DIR/running" "$QUEUE_DIR/done" "$QUEUE_DIR/cancel"
$SUDO chown -R "$RUN_AS_USER:$RUN_AS_USER" "$QUEUE_DIR"

# --- 2. ~/.ssh/config ControlMaster block ---
echo
if [[ ${#NODE_IPS[@]} -gt 0 ]]; then
    SSHCFG="$INVOKER_HOME/.ssh/config"
    $AS_INVOKER mkdir -p "$INVOKER_HOME/.ssh"
    $AS_INVOKER chmod 700 "$INVOKER_HOME/.ssh"
    $AS_INVOKER touch "$SSHCFG"
    $AS_INVOKER chmod 600 "$SSHCFG"
    # Strip any existing block (so re-runs can change the IP list cleanly).
    if grep -Fxq "$SSHCFG_BEGIN" "$SSHCFG"; then
        $AS_INVOKER sed -i "\|^${SSHCFG_BEGIN}\$|,\|^${SSHCFG_END}\$|d" "$SSHCFG"
    fi
    SSH_SNIPPET="$(mktemp)"
    {
        echo "$SSHCFG_BEGIN"
        echo "Host ${NODE_IPS[*]}"
        echo "    ControlMaster auto"
        echo "    ControlPath ~/.ssh/cm-%r@%h:%p"
        echo "    ControlPersist 10m"
        echo "    ServerAliveInterval 30"
        echo "    ServerAliveCountMax 3"
        echo "$SSHCFG_END"
    } > "$SSH_SNIPPET"
    chmod a+r "$SSH_SNIPPET"
    $AS_INVOKER bash -c "cat '$SSH_SNIPPET' >> '$SSHCFG'"
    rm -f "$SSH_SNIPPET"
    echo "[install] wrote ssh ControlMaster block for ${#NODE_IPS[@]} host(s) to $SSHCFG"
else
    echo "[install] no nodes.txt — skipping ssh config block"
fi

# --- 3. systemd unit ---
echo
if [[ -f "$UNIT_DST" ]]; then
    echo "[install] systemd unit already at $UNIT_DST — skipping render + enable"
    echo "[install]   (to reinstall: sudo rm $UNIT_DST && rerun)"
else
    echo "[install] rendering systemd unit -> $UNIT_DST"
    $SUDO bash -c "sed \
        -e 's|__REPO_ROOT__|$REPO_ROOT|g' \
        -e 's|__PYTHON__|$PYTHON_BIN|g' \
        -e 's|__USER__|$RUN_AS_USER|g' \
        -e 's|__USER_HOME__|$RUN_AS_HOME|g' \
        -e 's|__QUEUE_DIR__|$QUEUE_DIR|g' \
        '$UNIT_TEMPLATE' > '$UNIT_DST'"
    $SUDO chmod 644 "$UNIT_DST"
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now gpu_queue
    $SUDO systemctl --no-pager status gpu_queue | head -10 || true
fi

# --- 4. ~/.bashrc shell helpers ---
echo
RC="$INVOKER_HOME/.bashrc"
$AS_INVOKER touch "$RC"
if grep -Fxq "$BASHRC_BEGIN" "$RC"; then
    echo "[install] shell helpers already in $RC — skipping"
    echo "[install]   (to reinstall: delete the marker block in $RC, then rerun)"
else
    SNIPPET="$(mktemp)"
    cat > "$SNIPPET" <<EOF

$BASHRC_BEGIN
# Override any of these in your shell to point elsewhere.
export MINSCHED_QUEUE_DIR="\${MINSCHED_QUEUE_DIR:-$QUEUE_DIR}"
: "\${MINSCHED_REPO:=$REPO_ROOT}"
: "\${MINSCHED_PY:=$PYTHON_BIN}"
submit() { "\$MINSCHED_PY" "\$MINSCHED_REPO/functions/submit.py" "\$@"; }
queue()  { "\$MINSCHED_PY" "\$MINSCHED_REPO/functions/queue.py"  "\$@"; }
log()    { "\$MINSCHED_PY" "\$MINSCHED_REPO/functions/log.py"    "\$@"; }
cancel() { "\$MINSCHED_PY" "\$MINSCHED_REPO/functions/cancel.py" "\$@"; }
$BASHRC_END
EOF
    chmod a+r "$SNIPPET"
    $AS_INVOKER bash -c "cat '$SNIPPET' >> '$RC'"
    rm -f "$SNIPPET"
    echo "[install] appended shell helpers block to $RC"
    echo "[install]   open a new shell, or run: exec bash"
fi

echo
echo "[install] done."
