#!/usr/bin/env bash
set -euo pipefail

WLRCTL="${HOME}/.local/bin/wlrctl"
CHROMIUM_BIN="${HOME}/.local/bin/chromium"

if [[ ! -x "${CHROMIUM_BIN}" ]]; then
  CHROMIUM_BIN="$(command -v chromium)"
fi

if [[ -x "${WLRCTL}" ]]; then
  if "${WLRCTL}" window focus chromium >/dev/null 2>&1; then
    exit 0
  fi
fi

export GTK_IM_MODULE="${GTK_IM_MODULE:-fcitx}"
export QT_IM_MODULE="${QT_IM_MODULE:-fcitx}"
export XMODIFIERS="${XMODIFIERS:-@im=fcitx}"
export INPUT_METHOD="${INPUT_METHOD:-fcitx}"

exec "${CHROMIUM_BIN}" \
  --ozone-platform=wayland \
  --enable-wayland-ime \
  >/dev/null 2>&1 &
