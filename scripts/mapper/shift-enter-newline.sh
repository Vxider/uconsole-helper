#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"

WTYPE="${WTYPE:-$(command -v wtype)}"
WLRCTL="${HOME}/.local/bin/wlrctl"

if [[ -z "${WTYPE}" ]]; then
  exit 1
fi

quickterm_is_active() {
  local spec
  local specs=(
    "title:QuickTerm"
    "app_id:lxterminal"
    "app_id:QuickTerm"
    "app_id:quickterm"
  )

  [[ -x "${WLRCTL}" ]] || return 1

  for spec in "${specs[@]}"; do
    if "${WLRCTL}" window find "${spec}" "state:active" >/dev/null 2>&1; then
      return 0
    fi
  done

  return 1
}

if quickterm_is_active; then
  # Codex CLI uses Ctrl+J for multiline input in terminal UIs.
  exec "${WTYPE}" -M ctrl -k j -m ctrl
fi

exec "${WTYPE}" -M shift -k Return -m shift
