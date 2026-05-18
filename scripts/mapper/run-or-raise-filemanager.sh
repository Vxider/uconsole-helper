#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"

WLRCTL="${HOME}/.local/bin/wlrctl"
FILEMANAGER_BIN="${HOME}/.local/bin/pcmanfm"
WINDOW_SPECS=(
  "app_id:pcmanfm"
  "app_id:Pcmanfm"
  "title:File Manager"
  "title:PCManFM"
)

if [[ ! -x "${FILEMANAGER_BIN}" ]]; then
  FILEMANAGER_BIN="$(command -v pcmanfm || command -v thunar || command -v nautilus || command -v dolphin || command -v nemo)"
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

exec "${FILEMANAGER_BIN}" >/dev/null 2>&1 &
