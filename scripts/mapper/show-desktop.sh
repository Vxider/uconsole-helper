#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"

WLRCTL="${HOME}/.local/bin/wlrctl"
LOCK_DIR="${XDG_RUNTIME_DIR}/uconsole-helper-mapper-show-desktop.lock"
MAX_PASSES=20

if [[ ! -x "$WLRCTL" ]]; then
  exit 0
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCK_DIR"' EXIT

for (( pass = 0; pass < MAX_PASSES; pass++ )); do
  "$WLRCTL" toplevel minimize >/dev/null 2>&1 || break
  sleep 0.05
done
