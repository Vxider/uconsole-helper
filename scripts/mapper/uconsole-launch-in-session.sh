#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: uconsole-launch-in-session <command> [args...]" >&2
  exit 1
fi

read_systemd_user_env() {
  local show_env_cmd=("$@")
  local line

  WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}
  DISPLAY=${DISPLAY:-}
  DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-}

  while IFS= read -r line; do
    case "${line}" in
      WAYLAND_DISPLAY=*)
        WAYLAND_DISPLAY=${line#WAYLAND_DISPLAY=}
        ;;
      DISPLAY=*)
        DISPLAY=${line#DISPLAY=}
        ;;
      DBUS_SESSION_BUS_ADDRESS=*)
        DBUS_SESSION_BUS_ADDRESS=${line#DBUS_SESSION_BUS_ADDRESS=}
        ;;
    esac
  done < <("${show_env_cmd[@]}" 2>/dev/null || true)
}

detect_user() {
  if [[ -n "${UCONSOLE_SESSION_USER:-}" ]]; then
    printf '%s\n' "${UCONSOLE_SESSION_USER}"
    return 0
  fi

  if command -v loginctl >/dev/null 2>&1; then
    local user
    user=$(
      loginctl list-sessions --no-legend 2>/dev/null \
        | awk '$3 != "" { print $3; exit }'
    )
    if [[ -n "${user}" ]]; then
      printf '%s\n' "${user}"
      return 0
    fi
  fi

  echo "unable to detect target session user" >&2
  return 1
}

read_user_env() {
  local user=$1
  local uid=$2

  if command -v runuser >/dev/null 2>&1; then
    read_systemd_user_env runuser -u "${user}" -- systemctl --user show-environment
  fi

  WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-0}
  DISPLAY=${DISPLAY:-:0}
  DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/${uid}/bus}
}

if [[ ${EUID} -ne 0 ]]; then
  CURRENT_UID=$(id -u)
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${CURRENT_UID}}"
  read_systemd_user_env systemctl --user show-environment
  export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
  export DISPLAY="${DISPLAY:-:0}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
  exec "$@"
fi

TARGET_USER=$(detect_user)
TARGET_UID=$(id -u "${TARGET_USER}")
export XDG_RUNTIME_DIR="/run/user/${TARGET_UID}"

read_user_env "${TARGET_USER}" "${TARGET_UID}"

exec runuser -u "${TARGET_USER}" -- env \
  XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR}" \
  WAYLAND_DISPLAY="${WAYLAND_DISPLAY}" \
  DISPLAY="${DISPLAY}" \
  DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS}" \
  "$@"
