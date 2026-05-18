#!/usr/bin/env python3
"""User-session idle policy runner for uConsole Helper."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path


CONFIG_FILE = Path(os.environ.get("UCONSOLE_HELPER_CONFIG", "/etc/uconsole-helper/uconsole-helper.conf"))
POWER_SUPPLY_DIR = Path("/sys/class/power_supply")
DISPLAY_CONTROL = "/usr/local/bin/uconsole-helper-mapper-display-control"
POLL_SECONDS = 2


def load_config(path: Path = CONFIG_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"warning: failed to read {path}: {exc}", flush=True)
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def power_supply_present(path: Path) -> bool:
    present = path / "present"
    return not present.exists() or read_text(present) not in {"0", "false", "False"}


def power_supply_online(path: Path) -> bool | None:
    online = path / "online"
    if online.exists():
        return read_text(online) == "1"
    status = path / "status"
    if status.exists():
        return read_text(status).lower() in {"charging", "full"}
    return None


def power_state(power_supply_dir: Path = POWER_SUPPLY_DIR) -> str:
    if not power_supply_dir.is_dir():
        return "unknown"
    has_battery = False
    ac_online = False
    for path in sorted(power_supply_dir.iterdir()):
        supply_type = read_text(path / "type")
        if supply_type == "Battery" and power_supply_present(path):
            has_battery = True
            if read_text(path / "status").lower() in {"charging", "full"}:
                ac_online = True
        if supply_type in {"Mains", "USB", "USB_C", "USB_PD", "USB_DCP", "USB_CDP"}:
            if power_supply_online(path) is True:
                ac_online = True
    if ac_online:
        return "ac"
    if has_battery:
        return "battery"
    return "unknown"


def timeout_for_state(values: dict[str, str], state: str) -> int:
    fallback = values.get("POWERSAVER_SCREEN_TIMEOUT_SEC", "0")
    mode = values.get("POWERSAVER_MODE", "balanced").upper()
    if state == "battery":
        raw = values.get(
            f"POWERSAVER_{mode}_BATTERY_SCREEN_TIMEOUT_SEC",
            values.get("POWERSAVER_BATTERY_SCREEN_TIMEOUT_SEC", fallback),
        )
    elif state == "ac":
        raw = values.get(
            f"POWERSAVER_{mode}_AC_SCREEN_TIMEOUT_SEC",
            values.get("POWERSAVER_AC_SCREEN_TIMEOUT_SEC", fallback),
        )
    else:
        raw = values.get(
            f"POWERSAVER_{mode}_BATTERY_SCREEN_TIMEOUT_SEC",
            values.get("POWERSAVER_BATTERY_SCREEN_TIMEOUT_SEC", fallback),
        )
    try:
        return max(0, int(raw or "0"))
    except ValueError:
        print(f"warning: invalid screen timeout {raw!r}; disabling idle timeout", flush=True)
        return 0


def start_swayidle(timeout_sec: int) -> subprocess.Popen[str] | None:
    if timeout_sec <= 0:
        return None
    if shutil.which("swayidle") is None:
        print("warning: swayidle not found; install swayidle to enable screen timeout", flush=True)
        return None
    command = [
        "swayidle",
        "-w",
        "timeout",
        str(timeout_sec),
        f"sudo -n {DISPLAY_CONTROL} off",
        "resume",
        f"sudo -n {DISPLAY_CONTROL} on",
    ]
    print(f"idle timeout={timeout_sec}s", flush=True)
    return subprocess.Popen(command, text=True)


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def main() -> int:
    process: subprocess.Popen[str] | None = None
    active_timeout: int | None = None
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while not stopping:
        values = load_config()
        state = power_state(Path(values.get("POWERSAVER_POWER_SUPPLY_DIR", str(POWER_SUPPLY_DIR))))
        timeout_sec = timeout_for_state(values, state)
        if timeout_sec != active_timeout or (process is not None and process.poll() is not None):
            stop_process(process)
            process = start_swayidle(timeout_sec)
            active_timeout = timeout_sec
        time.sleep(POLL_SECONDS)

    stop_process(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
