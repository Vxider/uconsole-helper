#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"

WLRCTL="${HOME}/.local/bin/wlrctl"
WINDOW_SPECS=(
  "title:QuickTerm"
  "app_id:lxterminal"
  "app_id:QuickTerm"
  "app_id:quickterm"
  "app_id:foot"
  "app_id:Alacritty"
  "app_id:alacritty"
  "app_id:kitty"
  "app_id:org.wezfurlong.wezterm"
  "app_id:org.gnome.Terminal"
  "app_id:org.kde.konsole"
  "app_id:xfce4-terminal"
  "app_id:xterm"
  "app_id:code"
  "app_id:Code"
  "app_id:code-oss"
  "app_id:VSCodium"
  "app_id:codium"
  "app_id:cursor"
  "app_id:Cursor"
  "title:Visual Studio Code"
  "title:Code"
  "title:Cursor"
)

target_window_is_active() {
  local spec
  local state

  [[ -x "${WLRCTL}" ]] || return 1

  for spec in "${WINDOW_SPECS[@]}"; do
    for state in "state:active" "state:activated" "state:focused"; do
      if "${WLRCTL}" window find "${spec}" "${state}" >/dev/null 2>&1; then
        return 0
      fi
    done
  done

  return 1
}

target_process_exists() {
  pgrep -f '(^|/)(lxterminal|x-terminal-emulator|foot|alacritty|kitty|wezterm-gui|gnome-terminal-server|konsole|xfce4-terminal|xterm|code|code-oss|codium|cursor)( |$)' >/dev/null 2>&1
}

fcitx5_is_chinese() {
  local state

  command -v fcitx5-remote >/dev/null 2>&1 || return 1
  state="$(fcitx5-remote 2>/dev/null || true)"
  [[ "${state}" == "2" ]]
}

target_window_is_active || target_process_exists || exit 0
fcitx5_is_chinese || exit 0

fcitx5-remote -c >/dev/null 2>&1 || true
