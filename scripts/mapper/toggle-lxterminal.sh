#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export DISPLAY="${DISPLAY:-:0}"

TITLE="QuickTerm"
APP_NAME="quickterm"
APP_CLASS="QuickTerm"
WLRCTL="${HOME}/.local/bin/wlrctl"
STATE_DIR="${XDG_RUNTIME_DIR}/uconsole-helper-mapper"
WATCH_TOKEN_FILE="${STATE_DIR}/quickterm-focus-watch.token"
INVOKE_GUARD_FILE="/tmp/quickterm-toggle-${UID}.last"
MIN_TOGGLE_INTERVAL_MS="${MIN_TOGGLE_INTERVAL_MS:-900}"
AUTO_HIDE_ON_FOCUS_LOSS="${AUTO_HIDE_ON_FOCUS_LOSS:-yes}"
WINDOW_SPECS=(
  "app_id:lxterminal"
  "app_id:${APP_CLASS}"
  "app_id:${APP_NAME}"
  "app_id:${TITLE}"
  "title:${TITLE}"
)

now_millis() {
  date +%s%3N
}

ignore_repeated_invocation() {
  local now last

  now="$(now_millis)"

  if [[ -f "$INVOKE_GUARD_FILE" ]]; then
    last="$(cat "$INVOKE_GUARD_FILE" 2>/dev/null || true)"
    if [[ "$last" =~ ^[0-9]+$ ]] && (( now - last < MIN_TOGGLE_INTERVAL_MS )); then
      return 0
    fi
  fi

  printf '%s\n' "$now" >"$INVOKE_GUARD_FILE"
  return 1
}

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

if ignore_repeated_invocation; then
  exit 0
fi

if [[ -x "$WLRCTL" ]]; then
  if spec="$(find_window_spec)"; then
    if window_is_active "$spec"; then
      minimize_window "$spec"
      exit 0
    fi

    "$WLRCTL" toplevel activate "$spec" >/dev/null 2>&1 || true
    "$WLRCTL" toplevel focus "$spec" >/dev/null 2>&1 || true
    start_focus_watcher
    exit 0
  fi
fi

systemd-run --user --scope --quiet \
  --unit="quickterm-$(date +%s%3N)" \
  -E GTK_THEME=QuickTermTab10 \
  lxterminal \
    --no-remote \
    --name="${APP_NAME}" \
    --class="${APP_CLASS}" \
    --title="${TITLE}" \
  >/dev/null 2>&1 &

start_focus_watcher
