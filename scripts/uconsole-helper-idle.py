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


def run_display_control(action: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["sudo", "-n", DISPLAY_CONTROL, action],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"warning: display-control {action} failed: {exc}", flush=True)
        return None


def display_is_off() -> bool:
    result = run_display_control("status")
    if result is None or result.returncode != 0:
        return False
    return result.stdout.strip() == "off"


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
    active_power_state: str | None = None
    last_display_off: bool | None = None
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
        was_display_off = display_is_off()
        power_state_changed = active_power_state is not None and state != active_power_state
        restore_display_off = was_display_off or (power_state_changed and last_display_off is True)
        if timeout_sec != active_timeout or (process is not None and process.poll() is not None):
            stop_process(process)
            process = start_swayidle(timeout_sec)
            if restore_display_off:
                run_display_control("off")
            active_timeout = timeout_sec
        elif restore_display_off and not was_display_off:
            run_display_control("off")
        active_power_state = state
        last_display_off = display_is_off()
        time.sleep(POLL_SECONDS)

    stop_process(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
