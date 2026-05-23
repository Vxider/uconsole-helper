#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
auto_bootloader=0
bootloader_timeout=12

export PATH="${HOME}/.local/bin:${PATH}"
export PLATFORMIO_SETTING_ENABLE_TELEMETRY="${PLATFORMIO_SETTING_ENABLE_TELEMETRY:-no}"

args=()
while (($#)); do
  case "$1" in
    --bootloader)
      auto_bootloader=1
      shift
      ;;
    --no-bootloader)
      auto_bootloader=0
      shift
      ;;
    --bootloader-timeout)
      bootloader_timeout="${2:-}"
      if [[ -z "${bootloader_timeout}" ]]; then
        echo "--bootloader-timeout requires seconds" >&2
        exit 2
      fi
      shift 2
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done

if ! command -v pio >/dev/null 2>&1; then
  echo "PlatformIO not found. Install it first or add ~/.local/bin to PATH." >&2
  exit 1
fi

upload_port=""
if ((auto_bootloader)); then
  upload_port="$(python3 - "${bootloader_timeout}" <<'PY'
import os
import sys
import termios
import time
from pathlib import Path

VENDOR = "2886"
SENSOR_PRODUCTS = {"8044", "8065"}
BOOTLOADER_PRODUCTS = {"0065", "0044"}
ALL_PRODUCTS = SENSOR_PRODUCTS | BOOTLOADER_PRODUCTS
BOOTLOADER_HINTS = ("uf2", "bootloader", "mass storage")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def usb_root_for_tty(tty_dir: Path) -> Path | None:
    try:
        path = tty_dir.resolve()
    except OSError:
        return None
    for parent in [path, *path.parents]:
        if (parent / "idVendor").exists() and (parent / "idProduct").exists():
            return parent
    return None


def bootloader_hint(path: Path) -> bool:
    product_id = read_text(path / "idProduct")
    if product_id in BOOTLOADER_PRODUCTS:
        return True
    label = " ".join(
        [
            read_text(path / "product"),
            read_text(path / "manufacturer"),
            read_text(path / "modalias"),
        ]
    ).lower()
    return any(hint in label for hint in BOOTLOADER_HINTS)


def find_xiao_tty() -> tuple[str, Path] | None:
    candidates: list[tuple[str, Path]] = []
    for tty_dir in Path("/sys/class/tty").glob("ttyACM*"):
        root = usb_root_for_tty(tty_dir)
        if root is None:
            continue
        vendor = read_text(root / "idVendor")
        product = read_text(root / "idProduct")
        label = " ".join([read_text(root / "manufacturer"), read_text(root / "product")]).lower()
        if vendor == VENDOR and (product in ALL_PRODUCTS or ("seeed" in label and "xiao" in label)):
            candidates.append((tty_dir.name, root))
    candidates.sort(key=lambda item: 0 if bootloader_hint(item[1]) else 1)
    return candidates[0] if candidates else None


def find_xiao_usb_root() -> Path | None:
    for path in Path("/sys/bus/usb/devices").glob("*"):
        if not path.is_dir():
            continue
        vendor = read_text(path / "idVendor")
        product = read_text(path / "idProduct")
        label = " ".join([read_text(path / "manufacturer"), read_text(path / "product")]).lower()
        if vendor == VENDOR and (product in ALL_PRODUCTS or ("seeed" in label and "xiao" in label)):
            return path
    return None


def touch_bootloader(tty: str) -> None:
    dev = Path("/dev") / tty
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = (attrs[2] & ~termios.CBAUD) | termios.B1200
        attrs[3] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        time.sleep(0.25)
    finally:
        os.close(fd)


def main() -> int:
    timeout = float(sys.argv[1])
    found = find_xiao_tty()
    if found is None:
        root = find_xiao_usb_root()
        if root is not None and bootloader_hint(root):
            print("XIAO already appears to be in bootloader mode.", file=sys.stderr)
            return 0
        print("No XIAO ttyACM device found. Connect the board or double-click reset, then retry.", file=sys.stderr)
        return 0

    tty, root = found
    if bootloader_hint(root):
        print(f"XIAO already appears to be in bootloader mode on /dev/{tty}.", file=sys.stderr)
        print(f"/dev/{tty}")
        return 0

    print(f"Requesting XIAO bootloader via /dev/{tty} at 1200 baud...", file=sys.stderr)
    touch_bootloader(tty)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.25)
        current = find_xiao_tty()
        if current is not None and bootloader_hint(current[1]):
            print(f"XIAO bootloader detected on /dev/{current[0]}.", file=sys.stderr)
            print(f"/dev/{current[0]}")
            return 0
        root = find_xiao_usb_root()
        if root is not None and bootloader_hint(root):
            print("XIAO bootloader detected.", file=sys.stderr)
            return 0

    print("Bootloader was requested, but no bootloader device was detected before timeout.", file=sys.stderr)
    print("Reconnect the board or double-click reset, then retry.", file=sys.stderr)
    return 0


raise SystemExit(main())
PY
)"
fi

if [[ -n "${upload_port}" ]]; then
  args=(--upload-port "${upload_port}" "${args[@]}")
fi

cd "${project_dir}"
exec pio run -t upload "${args[@]}"
