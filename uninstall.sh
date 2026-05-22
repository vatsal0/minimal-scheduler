#!/bin/bash
# Reverse of install.sh. Idempotent — anything that isn't installed is skipped.
#
# Removes:
#   1. systemd unit at /etc/systemd/system/gpu_queue.service (stop + disable + rm).
#   2. The `# >>> minimal-scheduler ... <<<` marker block from the invoking
#      user's ~/.bashrc.
#
# Does NOT remove the queue dir (it holds your job history / state.json /
# daemon.log). The script prints its path so you can `rm -rf` it yourself.
#
# Needs sudo for the systemd parts.
#
# Usage:
#   sudo bash uninstall.sh
#   sudo MINSCHED_USER=ubuntu bash uninstall.sh

set -euo pipefail

UNIT_DST="/etc/systemd/system/gpu_queue.service"
BASHRC_BEGIN="# >>> minimal-scheduler shell helpers (submit/queue/log/cancel) >>>"
BASHRC_END="# <<< minimal-scheduler shell helpers <<<"
SSHCFG_BEGIN="# >>> minimal-scheduler ssh (ControlMaster) >>>"
SSHCFG_END="# <<< minimal-scheduler ssh <<<"

INVOKER="${SUDO_USER:-${USER:-$(id -un)}}"
RUN_AS_USER="${MINSCHED_USER:-$INVOKER}"

if [[ "${EUID}" -ne 0 ]] && ! sudo -n true 2>/dev/null; then
    echo "[uninstall] needs sudo. Re-run as root or with passwordless sudo." >&2
    echo "  sudo bash $0" >&2
    exit 1
fi

SUDO="sudo"
[[ "${EUID}" -eq 0 ]] && SUDO=""

# --- 1. systemd unit ---
echo
if [[ -f "$UNIT_DST" ]]; then
    echo "[uninstall] stopping + disabling gpu_queue"
    $SUDO systemctl disable --now gpu_queue || true
    echo "[uninstall] removing $UNIT_DST"
    $SUDO rm -f "$UNIT_DST"
    $SUDO systemctl daemon-reload
else
    echo "[uninstall] no systemd unit at $UNIT_DST — skipping"
fi

# --- 2. ~/.bashrc shell helpers + ~/.ssh/config block ---
echo
INVOKER_HOME="$(getent passwd "$INVOKER" | cut -d: -f6)"
if [[ -z "$INVOKER_HOME" ]]; then
    echo "[uninstall] could not resolve home dir for $INVOKER — skipping bashrc/ssh edits" >&2
else
    RC="${INVOKER_HOME}/.bashrc"
    if [[ -f "$RC" ]] && grep -Fxq "$BASHRC_BEGIN" "$RC"; then
        BACKUP="$RC.bak.$(date +%s)"
        cp "$RC" "$BACKUP"
        $SUDO -u "$INVOKER" sed -i "\|^${BASHRC_BEGIN}\$|,\|^${BASHRC_END}\$|d" "$RC"
        echo "[uninstall] removed shell-helper block from $RC (backup: $BACKUP)"
    else
        echo "[uninstall] no shell-helper block in $RC — skipping"
    fi
    SSHCFG="$INVOKER_HOME/.ssh/config"
    if [[ -f "$SSHCFG" ]] && grep -Fxq "$SSHCFG_BEGIN" "$SSHCFG"; then
        BACKUP="$SSHCFG.bak.$(date +%s)"
        cp "$SSHCFG" "$BACKUP"
        $SUDO -u "$INVOKER" sed -i "\|^${SSHCFG_BEGIN}\$|,\|^${SSHCFG_END}\$|d" "$SSHCFG"
        echo "[uninstall] removed ssh block from $SSHCFG (backup: $BACKUP)"
    else
        echo "[uninstall] no ssh block in ${SSHCFG} — skipping"
    fi
fi

# --- 3. queue runtime dir: report only ---
echo
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUEUE_HINT="${MINSCHED_QUEUE_DIR:-$REPO_ROOT/queue}"
if [[ -d "$QUEUE_HINT" ]]; then
    echo "[uninstall] queue dir left in place at: $QUEUE_HINT"
    echo "[uninstall]   delete it yourself if you want a clean slate:"
    echo "[uninstall]     rm -rf '$QUEUE_HINT'"
else
    echo "[uninstall] no queue dir at $QUEUE_HINT — nothing to report"
fi

echo
echo "[uninstall] done."
