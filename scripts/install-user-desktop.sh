#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Network Helper"
APP_ID="network-helper.desktop"
ICON_NAME="network-helper"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DESKTOP_DIR="${XDG_DATA_HOME:-"${HOME}/.local/share"}/applications"
DESKTOP_FILE="${DESKTOP_DIR}/${APP_ID}"
ICON_DIR="${XDG_DATA_HOME:-"${HOME}/.local/share"}/icons/hicolor/scalable/apps"
ICON_FILE="${ICON_DIR}/${ICON_NAME}.svg"

mkdir -p "${DESKTOP_DIR}"
mkdir -p "${ICON_DIR}"
chmod +x "${APP_DIR}/run.sh" "${APP_DIR}/network_helper_dhcp.py"
cp "${APP_DIR}/assets/network-helper.svg" "${ICON_FILE}"

cat > "${DESKTOP_FILE}" <<EOF
[Desktop Entry]
Type=Application
Name=${APP_NAME}
Comment=Run DHCP and scan LAN devices on selected network interfaces
Exec=${APP_DIR}/run.sh
Path=${APP_DIR}
Icon=${ICON_NAME}
Terminal=false
Categories=Network;Settings;
EOF

chmod 0644 "${DESKTOP_FILE}"
chmod 0644 "${ICON_FILE}"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${DESKTOP_DIR}" >/dev/null 2>&1 || true
fi

echo "Installed ${APP_NAME} launcher:"
echo "  ${DESKTOP_FILE}"
echo "Installed icon:"
echo "  ${ICON_FILE}"
