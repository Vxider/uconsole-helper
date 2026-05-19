#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"
export GTK_IM_MODULE="${GTK_IM_MODULE:-fcitx}"
export QT_IM_MODULE="${QT_IM_MODULE:-fcitx}"
export XMODIFIERS="${XMODIFIERS:-@im=fcitx}"
export SDL_IM_MODULE="${SDL_IM_MODULE:-fcitx}"
export GLFW_IM_MODULE="${GLFW_IM_MODULE:-ibus}"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"

WLRCTL="${HOME}/.local/bin/wlrctl"
WINDOW_SPECS=(
  "app_id:com.tencent.WeChat"
  "title:微信"
  "title:WeChat"
)

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

exec flatpak run com.tencent.WeChat >/dev/null 2>&1 &
