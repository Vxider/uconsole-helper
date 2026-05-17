#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/install.sh [options]

Options:
  --desktop-only     install only the current-user desktop launcher and icon
  --service-only     install only the system background service
  --no-start         install the system service but do not enable/start it
  -h, --help         show this help
EOF
}

INSTALL_DESKTOP=1
INSTALL_SERVICE=1
START_SERVICE=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --desktop-only)
      INSTALL_DESKTOP=1
      INSTALL_SERVICE=0
      shift
      ;;
    --service-only)
      INSTALL_DESKTOP=0
      INSTALL_SERVICE=1
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
  fi
  sudo install -m 0644 "${APP_DIR}/services/uconsole-helper.service" "${service_file}"
  sudo systemctl daemon-reload
  if [[ "${START_SERVICE}" -eq 1 ]]; then
    sudo systemctl enable --now uconsole-helper.service
  fi

  echo "Installed uConsole Helper background service:"
  echo "  ${service_file}"
  echo "Installed service runner:"
  echo "  ${bin_file}"
  echo "Config:"
  echo "  ${config_file}"
}

if [[ "${INSTALL_DESKTOP}" -eq 1 ]]; then
  install_desktop
fi

if [[ "${INSTALL_SERVICE}" -eq 1 ]]; then
  install_service
fi
