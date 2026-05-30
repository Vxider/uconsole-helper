#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/install.sh [options]

Options:
  --desktop-only     install only the current-user desktop launcher and icon
  --service-only     install only the system background service
  --mapper-only      install only the current-user input mapper and idle services
  --no-start         install services but do not enable/start them
  -h, --help         show this help
EOF
}

INSTALL_DESKTOP=1
INSTALL_SERVICE=1
INSTALL_MAPPER=1
START_SERVICE=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --desktop-only)
      INSTALL_DESKTOP=1
      INSTALL_SERVICE=0
      INSTALL_MAPPER=0
      shift
      ;;
    --service-only)
      INSTALL_DESKTOP=0
      INSTALL_SERVICE=1
      INSTALL_MAPPER=0
      shift
      ;;
    --mapper-only)
      INSTALL_DESKTOP=0
      INSTALL_SERVICE=0
      INSTALL_MAPPER=1
      shift
      ;;
    --no-start)
      START_SERVICE=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

APP_NAME="uConsole Helper"
APP_ID="uconsole-helper.desktop"
ICON_NAME="uconsole-helper"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

install_desktop() {
  local desktop_dir="${XDG_DATA_HOME:-"${HOME}/.local/share"}/applications"
  local desktop_file="${desktop_dir}/${APP_ID}"
  local icon_dir="${XDG_DATA_HOME:-"${HOME}/.local/share"}/icons/hicolor/scalable/apps"
  local icon_file="${icon_dir}/${ICON_NAME}.svg"

  mkdir -p "${desktop_dir}" "${icon_dir}"
  chmod +x "${APP_DIR}/run.sh" "${APP_DIR}/uconsole_helper_dhcp.py" "${APP_DIR}/uconsole_helper_service.py"
  cp "${APP_DIR}/assets/uconsole-helper.svg" "${icon_file}"

  cat > "${desktop_file}" <<EOF
[Desktop Entry]
Type=Application
Name=${APP_NAME}
Comment=Run DHCP, scan LAN devices, and manage uConsole background tasks
Exec=${APP_DIR}/run.sh
Path=${APP_DIR}
Icon=${ICON_NAME}
Terminal=false
Categories=Network;Settings;
EOF

  chmod 0644 "${desktop_file}" "${icon_file}"
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${desktop_dir}" >/dev/null 2>&1 || true
  fi

  echo "Installed ${APP_NAME} launcher:"
  echo "  ${desktop_file}"
  echo "Installed icon:"
  echo "  ${icon_file}"
}

install_service() {
  local bin_file="/usr/local/bin/uconsole-helper-service"
  local config_dir="/etc/uconsole-helper"
  local config_file="${config_dir}/uconsole-helper.conf"
  local service_file="/etc/systemd/system/uconsole-helper.service"

  sudo install -m 0755 "${APP_DIR}/uconsole_helper_service.py" "${bin_file}"
  sudo install -d -m 0755 "${config_dir}"
  if [[ ! -f "${config_file}" ]]; then
    sudo install -m 0644 "${APP_DIR}/config/uconsole-helper.conf" "${config_file}"
  else
    echo "Keeping existing config: ${config_file}"
    ensure_config_key "${config_file}" "POWERSAVER_MODE" "balanced"
    ensure_config_key "${config_file}" "POWERSAVER_ECO_BATTERY_CPU_FREQ" "1500,1500"
    ensure_config_key "${config_file}" "POWERSAVER_ECO_AC_CPU_FREQ" "1500,1500"
    ensure_config_key "${config_file}" "POWERSAVER_ECO_BATTERY_SCREEN_TIMEOUT_SEC" "0"
    ensure_config_key "${config_file}" "POWERSAVER_ECO_AC_SCREEN_TIMEOUT_SEC" "0"
    ensure_config_key "${config_file}" "POWERSAVER_ECO_UNKNOWN_POWER_ACTION" "restore"
    ensure_config_key "${config_file}" "POWERSAVER_ECO_WWAN_POLICY" "ondemand"
    ensure_config_key "${config_file}" "POWERSAVER_ECO_SCREEN_MODE" "default"
    ensure_config_key "${config_file}" "POWERSAVER_ECO_AUTO_BRIGHTNESS" "0"
    ensure_config_key "${config_file}" "POWERSAVER_ECO_STAND_MODE" "0"
    ensure_config_key "${config_file}" "POWERSAVER_ECO_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC" "30"
    ensure_config_key "${config_file}" "POWERSAVER_ECO_AUTO_AC_PUTDOWN_TIMEOUT_SEC" "60"
    ensure_config_key "${config_file}" "POWERSAVER_BALANCED_BATTERY_CPU_FREQ" "1500,1500"
    ensure_config_key "${config_file}" "POWERSAVER_BALANCED_AC_CPU_FREQ" "restore"
    ensure_config_key "${config_file}" "POWERSAVER_BALANCED_BATTERY_SCREEN_TIMEOUT_SEC" "0"
    ensure_config_key "${config_file}" "POWERSAVER_BALANCED_AC_SCREEN_TIMEOUT_SEC" "0"
    ensure_config_key "${config_file}" "POWERSAVER_BALANCED_UNKNOWN_POWER_ACTION" "restore"
    ensure_config_key "${config_file}" "POWERSAVER_BALANCED_WWAN_POLICY" "ondemand"
    ensure_config_key "${config_file}" "POWERSAVER_BALANCED_SCREEN_MODE" "default"
    ensure_config_key "${config_file}" "POWERSAVER_BALANCED_AUTO_BRIGHTNESS" "0"
    ensure_config_key "${config_file}" "POWERSAVER_BALANCED_STAND_MODE" "0"
    ensure_config_key "${config_file}" "POWERSAVER_BALANCED_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC" "60"
    ensure_config_key "${config_file}" "POWERSAVER_BALANCED_AUTO_AC_PUTDOWN_TIMEOUT_SEC" "120"
    ensure_config_key "${config_file}" "POWERSAVER_PERFORMANCE_BATTERY_CPU_FREQ" "1500,2400"
    ensure_config_key "${config_file}" "POWERSAVER_PERFORMANCE_AC_CPU_FREQ" "restore"
    ensure_config_key "${config_file}" "POWERSAVER_PERFORMANCE_BATTERY_SCREEN_TIMEOUT_SEC" "0"
    ensure_config_key "${config_file}" "POWERSAVER_PERFORMANCE_AC_SCREEN_TIMEOUT_SEC" "0"
    ensure_config_key "${config_file}" "POWERSAVER_PERFORMANCE_UNKNOWN_POWER_ACTION" "restore"
    ensure_config_key "${config_file}" "POWERSAVER_PERFORMANCE_WWAN_POLICY" "ondemand"
    ensure_config_key "${config_file}" "POWERSAVER_PERFORMANCE_SCREEN_MODE" "default"
    ensure_config_key "${config_file}" "POWERSAVER_PERFORMANCE_AUTO_BRIGHTNESS" "0"
    ensure_config_key "${config_file}" "POWERSAVER_PERFORMANCE_STAND_MODE" "0"
    ensure_config_key "${config_file}" "POWERSAVER_PERFORMANCE_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC" "120"
    ensure_config_key "${config_file}" "POWERSAVER_PERFORMANCE_AUTO_AC_PUTDOWN_TIMEOUT_SEC" "300"
  fi
  sudo install -m 0644 "${APP_DIR}/services/uconsole-helper.service" "${service_file}"
  sudo systemctl daemon-reload
  if [[ "${START_SERVICE}" -eq 1 ]]; then
    sudo systemctl enable --now uconsole-helper.service
  fi

  local sudoers_file="/etc/sudoers.d/uconsole-helper-service-write-config"
  local sudoers_line="${USER} ALL=(root) NOPASSWD: ${bin_file} write-config"
  if [[ ! -f "${sudoers_file}" ]] || ! sudo grep -Fxq "${sudoers_line}" "${sudoers_file}"; then
    local tmp_sudoers
    tmp_sudoers="$(mktemp)"
    printf '%s\n' "${sudoers_line}" > "${tmp_sudoers}"
    sudo visudo -cf "${tmp_sudoers}" >/dev/null
    sudo install -m 0440 "${tmp_sudoers}" "${sudoers_file}"
    rm -f "${tmp_sudoers}"
  fi

  echo "Installed uConsole Helper background service:"
  echo "  ${service_file}"
  echo "Installed service runner:"
  echo "  ${bin_file}"
  echo "Config:"
  echo "  ${config_file}"
}

ensure_config_key() {
  local config_file="$1"
  local key="$2"
  local value="$3"
  if ! sudo grep -Eq "^${key}=" "${config_file}"; then
    printf '%s=%s\n' "${key}" "${value}" | sudo tee -a "${config_file}" >/dev/null
  fi
}

merge_default_rightshift_bindings() {
  local config_file="$1"
  local defaults_file="$2"

  if python3 - "$config_file" "$defaults_file" <<'PY'
import sys
import tomllib
from pathlib import Path

path = Path(sys.argv[1])
defaults_path = Path(sys.argv[2])

try:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    defaults = tomllib.loads(defaults_path.read_text(encoding="utf-8"))
except (OSError, tomllib.TOMLDecodeError) as exc:
    print(exc, file=sys.stderr)
    sys.exit(2)

existing_keys = {
    str(item.get("key", "")).lower()
    for item in data.get("rightshift", {}).get("bindings", [])
    if item.get("key")
}

missing = []
for item in defaults.get("rightshift", {}).get("bindings", []):
    key = str(item.get("key", "")).strip()
    command = str(item.get("command", "")).strip()
    if key and command and key.lower() not in existing_keys:
        missing.append((key, command))
        existing_keys.add(key.lower())

if not missing:
    sys.exit(0)

with path.open("a", encoding="utf-8") as handle:
    for key, command in missing:
        handle.write(
            "\n"
            "[[rightshift.bindings]]\n"
            f'key = "{key}"\n'
            f'command = "{command}"\n'
        )
        print(f"RightShift+{key.upper()} -> {command}")
sys.exit(1)
PY
  then
    return 0
  else
    local status=$?
    if [[ "${status}" -eq 1 ]]; then
      echo "Added missing default RightShift bindings to ${config_file}"
      return 0
    fi
    echo "warning: unable to update ${config_file}; keeping existing RightShift bindings" >&2
    return 0
  fi
}

install_mapper() {
  local mapper_app_dir="${HOME}/.local/share/uconsole-helper-mapper"
  local idle_app_dir="${HOME}/.local/share/uconsole-helper-idle"
  local bin_dir="${HOME}/.local/bin"
  local config_dir="${HOME}/.config/uconsole-helper-mapper"
  local systemd_dir="${HOME}/.config/systemd/user"
  local fcitx_lua_dir="${HOME}/.local/share/fcitx5/lua/imeapi/extensions"
  local python_bin="${PYTHON_BIN:-/usr/bin/python3}"

  mkdir -p "${mapper_app_dir}" "${idle_app_dir}" "${bin_dir}" "${config_dir}" "${systemd_dir}" "${fcitx_lua_dir}"

  if [[ ! -x "${python_bin}" ]]; then
    echo "Python interpreter not found: ${python_bin}" >&2
    exit 1
  fi
  if ! "${python_bin}" -c 'import evdev' >/dev/null 2>&1; then
    echo "Missing python3-evdev. Install it first:" >&2
    echo "  sudo apt update && sudo apt install -y python3-evdev" >&2
    exit 1
  fi
  if command -v apt-mark >/dev/null 2>&1; then
    sudo apt-mark manual python3-evdev >/dev/null 2>&1 || true
  fi
  if [[ ! -e /dev/uinput ]]; then
    echo "Missing /dev/uinput. Enable it first:" >&2
    echo "  sudo modprobe uinput" >&2
    exit 1
  fi
  if [[ ! -w /dev/uinput ]]; then
    echo "Configuring /dev/uinput permissions for group input..."
    sudo install -m 0644 "${APP_DIR}/scripts/mapper/99-uinput.rules" /etc/udev/rules.d/99-uinput.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger --name-match=uinput >/dev/null 2>&1 || true
    sudo chgrp input /dev/uinput
    sudo chmod 0660 /dev/uinput
    if command -v setfacl >/dev/null 2>&1; then
      sudo setfacl -m "u:${USER}:rw" /dev/uinput
    fi
  fi
  if [[ ! -w /dev/uinput ]]; then
    echo "Unable to get write access to /dev/uinput for ${USER}." >&2
    echo "Add the user to the input group or grant a persistent ACL, then rerun install." >&2
    echo "  sudo usermod -aG input ${USER}" >&2
    exit 1
  fi

  install -m 0755 "${APP_DIR}/mapper/uconsole_helper_mapper.py" "${mapper_app_dir}/uconsole_helper_mapper.py"
  install -m 0755 "${APP_DIR}/scripts/uconsole-helper-idle.py" "${idle_app_dir}/uconsole_helper_idle.py"
  install -m 0755 "${APP_DIR}/scripts/mapper/generate_desktop_keybinds.py" "${mapper_app_dir}/generate_desktop_keybinds.py"
  install -m 0755 "${APP_DIR}/scripts/mapper/sync_labwc_keybinds.py" "${mapper_app_dir}/sync_labwc_keybinds.py"
  install -m 0755 "${APP_DIR}/scripts/mapper/sync_keyd_default_conf.py" "${mapper_app_dir}/sync_keyd_default_conf.py"
  install -m 0755 "${APP_DIR}/scripts/mapper/toggle-uconsole-helper.sh" "${bin_dir}/toggle-uconsole-helper"
  install -m 0755 "${APP_DIR}/scripts/mapper/toggle-lxterminal.sh" "${bin_dir}/toggle-lxterminal"
  install -m 0755 "${APP_DIR}/scripts/mapper/run-or-raise-chromium.sh" "${bin_dir}/run-or-raise-chromium"
  install -m 0755 "${APP_DIR}/scripts/mapper/run-or-raise-filemanager.sh" "${bin_dir}/run-or-raise-filemanager"
  install -m 0755 "${APP_DIR}/scripts/mapper/run-or-raise-flashai.sh" "${bin_dir}/run-or-raise-flashai"
  install -m 0755 "${APP_DIR}/scripts/mapper/run-or-raise-vscode.sh" "${bin_dir}/run-or-raise-vscode"
  install -m 0755 "${APP_DIR}/scripts/mapper/run-or-raise-wechat.sh" "${bin_dir}/run-or-raise-wechat"
  install -m 0755 "${APP_DIR}/scripts/mapper/run-or-raise-zdesktop.sh" "${bin_dir}/run-or-raise-zdesktop"
  install -m 0755 "${APP_DIR}/scripts/mapper/show-desktop.sh" "${bin_dir}/uconsole-show-desktop"
  install -m 0755 "${APP_DIR}/scripts/mapper/esc-switch-ime-english.sh" "${bin_dir}/esc-switch-ime-english"
  install -m 0755 "${APP_DIR}/scripts/mapper/shift-enter-newline.sh" "${bin_dir}/shift-enter-newline"
  install -m 0755 "${APP_DIR}/scripts/mapper/uconsole-paste.sh" "${bin_dir}/uconsole-paste"
  install -m 0755 "${APP_DIR}/scripts/mapper/uconsole-voice-ptt.sh" "${bin_dir}/uconsole-voice-ptt"
  install -m 0755 "${APP_DIR}/scripts/mapper/uconsole-voice-stream.py" "${bin_dir}/uconsole-voice-stream"
  if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists gtk+-3.0 gtk-layer-shell-0; then
    cc "${APP_DIR}/scripts/mapper/uconsole-asr-popup.c" -o "${bin_dir}/uconsole-asr-popup" $(pkg-config --cflags --libs gtk+-3.0 gtk-layer-shell-0)
    chmod 0755 "${bin_dir}/uconsole-asr-popup"
  else
    echo "gtk+-3.0 and gtk-layer-shell development files are required to build uconsole-asr-popup" >&2
    exit 1
  fi
  install -m 0644 "${APP_DIR}/scripts/mapper/fcitx-uconsole-voice-commit.lua" "${fcitx_lua_dir}/uconsole_voice_commit.lua"
  install -m 0644 "${APP_DIR}/services/user/uconsole-helper-mapper.service" "${systemd_dir}/uconsole-helper-mapper.service"
  install -m 0644 "${APP_DIR}/services/user/uconsole-helper-idle.service" "${systemd_dir}/uconsole-helper-idle.service"
  sudo install -m 0755 "${APP_DIR}/scripts/mapper/uconsole-launch-in-session.sh" /usr/local/bin/uconsole-launch-in-session
  sudo install -m 0755 "${APP_DIR}/scripts/mapper/uconsole-helper-mapper-display-control" /usr/local/bin/uconsole-helper-mapper-display-control

  local sudoers_file="/etc/sudoers.d/uconsole-helper-mapper-display-control"
  local sudoers_line="${USER} ALL=(root) NOPASSWD: /usr/local/bin/uconsole-helper-mapper-display-control *"
  if [[ ! -f "${sudoers_file}" ]] || ! sudo grep -Fxq "${sudoers_line}" "${sudoers_file}"; then
    local tmp_sudoers
    tmp_sudoers="$(mktemp)"
    printf '%s\n' "${sudoers_line}" > "${tmp_sudoers}"
    sudo visudo -cf "${tmp_sudoers}" >/dev/null
    sudo install -m 0440 "${tmp_sudoers}" "${sudoers_file}"
    rm -f "${tmp_sudoers}"
  fi

  if [[ ! -f "${config_dir}/config.toml" ]]; then
    install -m 0644 "${APP_DIR}/config/mapper/config.toml.example" "${config_dir}/config.toml"
  fi
  if [[ ! -f "${config_dir}/desktop-keybinds.toml" ]]; then
    install -m 0644 "${APP_DIR}/config/mapper/desktop-keybinds.toml.example" "${config_dir}/desktop-keybinds.toml"
  else
    merge_default_rightshift_bindings "${config_dir}/desktop-keybinds.toml" "${APP_DIR}/config/mapper/desktop-keybinds.toml.example"
  fi
  if [[ ! -f "${config_dir}/voice.env" ]]; then
    install -m 0644 "${APP_DIR}/config/mapper/voice.env.example" "${config_dir}/voice.env"
  fi
  if grep -Eq '^ASR_TIMEOUT=(30|60)$' "${config_dir}/voice.env"; then
    perl -0pi -e 's/^ASR_TIMEOUT=(30|60)$/ASR_TIMEOUT=15/m' "${config_dir}/voice.env"
  fi
  if ! grep -Eq '^ASR_REQUEST_ATTEMPT_TIMEOUT=' "${config_dir}/voice.env"; then
    printf 'ASR_REQUEST_ATTEMPT_TIMEOUT=8\n' >> "${config_dir}/voice.env"
  fi
  if ! grep -Eq '^ASR_CONNECT_TIMEOUT=' "${config_dir}/voice.env"; then
    printf 'ASR_CONNECT_TIMEOUT=2\n' >> "${config_dir}/voice.env"
  fi
  if ! grep -Eq '^ASR_RETRY_COUNT=' "${config_dir}/voice.env"; then
    printf 'ASR_RETRY_COUNT=3\n' >> "${config_dir}/voice.env"
  elif grep -Eq '^ASR_RETRY_COUNT=1$' "${config_dir}/voice.env"; then
    perl -0pi -e 's/^ASR_RETRY_COUNT=1$/ASR_RETRY_COUNT=2/m' "${config_dir}/voice.env"
  fi
  if ! grep -Eq '^ASR_RETRY_DELAY=' "${config_dir}/voice.env"; then
    printf 'ASR_RETRY_DELAY=0.35\n' >> "${config_dir}/voice.env"
  fi
  if [[ ! -f "${config_dir}/voice-glossary.txt" ]]; then
    install -m 0644 "${APP_DIR}/config/mapper/voice-glossary.txt.example" "${config_dir}/voice-glossary.txt"
  fi

  "${python_bin}" "${mapper_app_dir}/generate_desktop_keybinds.py" --config "${config_dir}/desktop-keybinds.toml"
  "${python_bin}" "${mapper_app_dir}/sync_labwc_keybinds.py"
  if command -v labwc >/dev/null 2>&1; then
    labwc --reconfigure >/dev/null 2>&1 || true
  fi
  if command -v keyd >/dev/null 2>&1 || command -v keyd.rvaiya >/dev/null 2>&1 || [[ -d /etc/keyd ]]; then
    sudo install -d -m 0755 /etc/keyd
    sudo install -m 0644 "${mapper_app_dir}/keyd-uconsole-helper-mapper" /etc/keyd/uconsole-helper-mapper
    sudo "${python_bin}" "${mapper_app_dir}/sync_keyd_default_conf.py"
    if command -v keyd >/dev/null 2>&1; then
      sudo keyd reload >/dev/null 2>&1 || sudo systemctl restart keyd >/dev/null 2>&1 || true
    elif command -v keyd.rvaiya >/dev/null 2>&1; then
      sudo keyd.rvaiya reload >/dev/null 2>&1 || sudo systemctl restart keyd >/dev/null 2>&1 || true
    fi
  fi

  systemctl --user daemon-reload
  if [[ "${START_SERVICE}" -eq 1 ]]; then
    systemctl --user enable --now uconsole-helper-mapper.service
    systemctl --user restart uconsole-helper-mapper.service
    systemctl --user enable --now uconsole-helper-idle.service
    systemctl --user restart uconsole-helper-idle.service
    systemctl --user disable --now uconsole-helper-asr-preview.service >/dev/null 2>&1 || true
  fi

  echo "Installed uConsole input mapper:"
  echo "  ${systemd_dir}/uconsole-helper-mapper.service"
  echo "Config:"
  echo "  ${config_dir}/config.toml"
  echo "ASR:"
  echo "  ${config_dir}/voice.env"
  echo "Idle:"
  echo "  ${systemd_dir}/uconsole-helper-idle.service"
}

if [[ "${INSTALL_DESKTOP}" -eq 1 ]]; then
  install_desktop
fi

if [[ "${INSTALL_SERVICE}" -eq 1 ]]; then
  install_service
fi

if [[ "${INSTALL_MAPPER}" -eq 1 ]]; then
  install_mapper
fi
