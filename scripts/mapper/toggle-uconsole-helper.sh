#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"

TITLE="uConsole Helper"
APP_ID="uconsole-helper"
WLRCTL="${HOME}/.local/bin/wlrctl"
SESSION_LAUNCHER="/usr/local/bin/uconsole-launch-in-session"
STATE_DIR="${XDG_RUNTIME_DIR}/uconsole-helper-mapper"
WATCH_TOKEN_FILE="${STATE_DIR}/uconsole-helper-focus-watch.token"
AUTO_HIDE_ON_FOCUS_LOSS="${AUTO_HIDE_ON_FOCUS_LOSS:-yes}"
AUTO_HIDE_FOCUS_WHITELIST="${AUTO_HIDE_FOCUS_WHITELIST:-title:uconsole voice}"
IFS=',' read -r -a FOCUS_LOSS_WHITELIST_SPECS <<<"$AUTO_HIDE_FOCUS_WHITELIST"
WINDOW_SPECS=(
  "app_id:${APP_ID}"
  "title:${TITLE}"
  "title:uConsole Helper"
  "title:uconsole-helper"
)
COMMAND=(
  "${HOME}/WorkSpace/uconsole-helper/run.sh"
)

find_window_spec() {
  local spec
  for spec in "${WINDOW_SPECS[@]}"; do
    if "$WLRCTL" window find "$spec" >/dev/null 2>&1; then
      printf '%s\n' "$spec"
      return 0
    fi
  done

  return 1
}

window_is_active() {
  local spec="${1}"
  local state

  for state in "state:active" "state:activated" "state:focused"; do
    if "$WLRCTL" window find "$spec" "$state" >/dev/null 2>&1; then
      return 0
    fi
  done

  return 1
}

whitelisted_window_is_active() {
  local spec

  for spec in "${FOCUS_LOSS_WHITELIST_SPECS[@]}"; do
    [[ -n "$spec" ]] || continue
    if window_is_active "$spec"; then
      return 0
    fi
  done

  return 1
}

minimize_window() {
  local spec="${1}"

  "$WLRCTL" toplevel minimize "$spec" >/dev/null 2>&1 \
    || "$WLRCTL" window minimize "$spec" >/dev/null 2>&1 \
    || true
}

watch_focus_loss() {
  local token="${1}"
  local spec
  local seen_active=0
  local startup_deadline=$((SECONDS + 15))

  mkdir -p "$STATE_DIR"

  while true; do
    [[ -f "$WATCH_TOKEN_FILE" ]] || exit 0
    [[ "$(cat "$WATCH_TOKEN_FILE")" == "$token" ]] || exit 0

    if spec="$(find_window_spec)"; then
      if window_is_active "$spec"; then
        seen_active=1
      elif whitelisted_window_is_active; then
        :
      elif [[ "$seen_active" -eq 1 ]]; then
        minimize_window "$spec"
        exit 0
      fi
    elif [[ "$seen_active" -eq 1 || "$SECONDS" -ge "$startup_deadline" ]]; then
      exit 0
    fi

    sleep 0.2
  done
}

start_focus_watcher() {
  local token

  [[ "$AUTO_HIDE_ON_FOCUS_LOSS" == "yes" ]] || return 0
  [[ -x "$WLRCTL" ]] || return 0

  mkdir -p "$STATE_DIR"
  token="$(date +%s)-$$"
  printf '%s\n' "$token" >"$WATCH_TOKEN_FILE"
  watch_focus_loss "$token" >/dev/null 2>&1 &
}

fullscreen_window() {
  local attempt
  local spec

  [[ -x "$WLRCTL" ]] || return 0

  for attempt in $(seq 1 20); do
    if spec="$(find_window_spec)"; then
      "$WLRCTL" window focus "$spec" >/dev/null 2>&1 || true
      "$WLRCTL" window fullscreen "$spec" >/dev/null 2>&1 || true
      return 0
    fi
    sleep 0.2
  done
}

if [[ -x "$WLRCTL" ]]; then
  if spec="$(find_window_spec)"; then
    if window_is_active "$spec"; then
      minimize_window "$spec"
      exit 0
    fi

    "$WLRCTL" toplevel activate "$spec" >/dev/null 2>&1 || true
    "$WLRCTL" toplevel focus "$spec" >/dev/null 2>&1 || true
    "$WLRCTL" toplevel fullscreen "$spec" >/dev/null 2>&1 || true
    start_focus_watcher
    exit 0
  fi
fi

if [[ -x "$SESSION_LAUNCHER" ]]; then
  "$SESSION_LAUNCHER" "${COMMAND[@]}" >/dev/null 2>&1 &
else
  "${COMMAND[@]}" >/dev/null 2>&1 &
fi
fullscreen_window &
start_focus_watcher
