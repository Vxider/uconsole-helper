#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"

WLRCTL="${HOME}/.local/bin/wlrctl"
VSCODE_BIN="${HOME}/.local/bin/code"
WINDOW_SPECS=(
  "app_id:code"
  "app_id:Code"
  "title:Visual Studio Code"
  "title:Code"
)

if [[ ! -x "${VSCODE_BIN}" ]]; then
  VSCODE_BIN="$(command -v code || command -v codium || command -v code-oss || command -v cursor)"
fi

find_window_spec() {
  local spec
  for spec in "${WINDOW_SPECS[@]}"; do
    if "${WLRCTL}" window find "${spec}" >/dev/null 2>&1; then
      printf '%s\n' "${spec}"
      return 0
    fi
  done

  return 1
}

if [[ -x "${WLRCTL}" ]]; then
  if spec="$(find_window_spec)"; then
    "${WLRCTL}" toplevel activate "${spec}" >/dev/null 2>&1 || true
    "${WLRCTL}" toplevel focus "${spec}" >/dev/null 2>&1 || true
    "${WLRCTL}" window focus "${spec}" >/dev/null 2>&1 || true
    exit 0
  fi
fi

exec "${VSCODE_BIN}" >/dev/null 2>&1 &
