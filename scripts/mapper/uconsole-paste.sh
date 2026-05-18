#!/usr/bin/env bash
set -euo pipefail

shortcut=${1:-ctrl_v}

command -v wl-copy >/dev/null 2>&1 || {
  echo "wl-copy is required" >&2
  exit 1
}

wl-copy
sleep "${UCONSOLE_PASTE_DELAY:-0.08}"

/usr/bin/python3 - "${shortcut}" <<'PY'
import sys
import time

from evdev import UInput, ecodes

shortcut = sys.argv[1]
keys = {
    "ctrl_v": (ecodes.KEY_LEFTCTRL, ecodes.KEY_V),
    "shift_insert": (ecodes.KEY_LEFTSHIFT, ecodes.KEY_INSERT),
}

if shortcut not in keys:
    raise SystemExit(f"unsupported paste shortcut: {shortcut}")

modifier, key = keys[shortcut]
capabilities = {ecodes.EV_KEY: [modifier, key]}

with UInput(capabilities, name="uconsole-paste-keyboard") as ui:
    ui.write(ecodes.EV_KEY, modifier, 1)
    ui.syn()
    time.sleep(0.03)
    ui.write(ecodes.EV_KEY, key, 1)
    ui.syn()
    time.sleep(0.03)
    ui.write(ecodes.EV_KEY, key, 0)
    ui.write(ecodes.EV_KEY, modifier, 0)
    ui.syn()
PY
