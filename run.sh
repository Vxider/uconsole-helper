#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

exec env \
  XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}" \
  WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}" \
  GDK_BACKEND="${GDK_BACKEND:-wayland,x11}" \
  /usr/bin/python3 uconsole_helper_gui.py
