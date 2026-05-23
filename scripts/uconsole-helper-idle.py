#!/usr/bin/env python3
"""User-session idle policy runner for uConsole Helper."""

from __future__ import annotations

import os
import json
import select
import shlex
import shutil
import signal
import struct
import subprocess
import time
import termios
from pathlib import Path


CONFIG_FILE = Path(os.environ.get("UCONSOLE_HELPER_CONFIG", "/etc/uconsole-helper/uconsole-helper.conf"))
POWER_SUPPLY_DIR = Path("/sys/class/power_supply")
DISPLAY_CONTROL = "/usr/local/bin/uconsole-helper-mapper-display-control"
KEYBOARD_BACKLIGHT_SCRIPT = Path("~/WorkSpace/uconsole-keyboard/tools/keyboard_state.sh").expanduser()
MCU_SHARED_SAMPLE_FILE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "uconsole-helper-mcu-latest.json"
POLL_SECONDS = 1
CONFIG_POLL_SECONDS = 15
DISPLAY_STATUS_POLL_SECONDS = 30
DISPLAY_STATUS_ACTIVE_POLL_SECONDS = 10
AUTO_MCU_STALE_SECONDS = 20
AUTO_PICKUP_DELTA_THRESHOLD = 0.045
AUTO_PICKUP_TILT_DELTA_DEGREES = 5.0
AUTO_PICKUP_MIN_INTERVAL_SECONDS = 0.5
AUTO_PICKUP_MOVE_MIN_SECONDS = 0.6
AUTO_PICKUP_SETTLE_SECONDS = 0.4
AUTO_PICKUP_MOVE_EXPIRE_SECONDS = 3.0
AUTO_PICKUP_WAKE_ENABLED = False
LIGHT_LEVELS = (
    (2.0, 1),
    (5.0, 2),
    (9.0, 3),
    (30.0, 4),
    (120.0, 5),
    (220.0, 6),
    (420.0, 7),
    (900.0, 8),
    (None, 9),
)
LAST_KEYBOARD_BACKLIGHT_LEVEL: int | None = None
INPUT_EVENT_FORMAT = "llHHI"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FORMAT)
INPUT_ACTIVITY_TYPES = {1, 2}


class McuSerialReader:
    def __init__(self) -> None:
        self.fd: int | None = None
        self.path: Path | None = None
        self.buffer = ""
        self.last_shared_sample_mtime = 0.0

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.path = None
        self.buffer = ""

    def read_sample(self, enabled: bool) -> dict[str, object] | None:
        if not enabled:
            self.close()
            return None
        dev = find_xiao_tty()
        if dev is None:
            self.close()
            return None
        if self.fd is None or self.path != dev:
            self.close()
            self.fd = open_mcu_fd(dev)
            self.path = dev if self.fd is not None else None
        if self.fd is None:
            return read_shared_mcu_sample(self)
        sample = read_mcu_sample_from_fd(self.fd, self)
        if sample is not None:
            return sample
        return read_shared_mcu_sample(self)

    def write_command(self, command: str) -> None:
        if self.fd is None:
            return
        try:
            os.write(self.fd, f"{command.strip()}\n".encode("utf-8"))
        except OSError:
            self.close()


class AutoBrightnessState:
    def __init__(self) -> None:
        self.smoothed_lux: float | None = None
        self.current_backlight: int | None = None


class HostInputMonitor:
    def __init__(self) -> None:
        self.fds: dict[int, Path] = {}
        self.last_scan_at = 0.0
        self.last_active_at = time.time()
        self.permission_failed = False

    def close(self) -> None:
        for fd in list(self.fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()

    def scan(self) -> None:
        now = time.time()
        if now - self.last_scan_at < 10.0:
            return
        self.last_scan_at = now
        seen = set(Path("/dev/input").glob("event*"))
        for fd, path in list(self.fds.items()):
            if path not in seen:
                try:
                    os.close(fd)
                except OSError:
                    pass
                self.fds.pop(fd, None)
        for path in sorted(seen):
            if path in self.fds.values():
                continue
            try:
                fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except PermissionError:
                self.permission_failed = True
                continue
            except OSError:
                continue
            self.fds[fd] = path

    def poll(self) -> float:
        self.scan()
        if not self.fds:
            return self.last_active_at
        try:
            ready, _, _ = select.select(list(self.fds), [], [], 0)
        except OSError:
            return self.last_active_at
        now = time.time()
        for fd in ready:
            try:
                data = os.read(fd, INPUT_EVENT_SIZE * 32)
            except BlockingIOError:
                continue
            except OSError:
                path = self.fds.pop(fd, None)
                try:
                    os.close(fd)
                except OSError:
                    pass
                if path is not None:
                    print(f"host input monitor: closed {path}", flush=True)
                continue
            for offset in range(0, len(data) - INPUT_EVENT_SIZE + 1, INPUT_EVENT_SIZE):
                try:
                    _sec, _usec, event_type, _code, value = struct.unpack(
                        INPUT_EVENT_FORMAT,
                        data[offset : offset + INPUT_EVENT_SIZE],
                    )
                except struct.error:
                    continue
                if event_type in INPUT_ACTIVITY_TYPES and value != 0:
                    self.last_active_at = now
                    break
        return self.last_active_at


class DisplayStateCache:
    def __init__(self) -> None:
        self.off = False
        self.last_checked_at = 0.0
        self.known = False

    def refresh(self) -> bool:
        result = run_display_control("status")
        self.off = display_off_from_status_result(result)
        self.last_checked_at = time.time()
        self.known = result is not None and result.returncode == 0
        return self.off

    def get(self, *, force: bool = False, interval: float = DISPLAY_STATUS_POLL_SECONDS) -> bool:
        now = time.time()
        if force or not self.known or now - self.last_checked_at >= interval:
            return self.refresh()
        return self.off

    def mark_on(self) -> None:
        self.off = False
        self.known = True
        self.last_checked_at = time.time()

    def mark_off(self) -> None:
        self.off = True
        self.known = True
        self.last_checked_at = time.time()


class AutoMotionState:
    def __init__(self) -> None:
        self.last_vector: tuple[float, float, float] | None = None
        self.last_tilt_deg: float | None = None
        self.last_pickup_at = 0.0
        self.last_device_state = ""
        self.move_started_at: float | None = None
        self.last_move_at: float | None = None
        self.waiting_for_settle = False

    def pickup_detected(self, sample: dict[str, object], now: float, motion: str, device_state: str) -> bool:
        vector = accel_vector(sample)
        if vector is None:
            return False
        tilt = tilt_degrees(vector)
        previous = self.last_vector
        previous_tilt = self.last_tilt_deg
        self.last_vector = vector
        self.last_tilt_deg = tilt
        if previous is None or previous_tilt is None:
            return False
        delta = sum(abs(vector[index] - previous[index]) for index in range(3))
        tilt_delta = abs(tilt - previous_tilt)
        moving_now = (
            device_state == "held"
            or motion == "moving"
            or delta >= AUTO_PICKUP_DELTA_THRESHOLD
            or tilt_delta >= AUTO_PICKUP_TILT_DELTA_DEGREES
        )
        if moving_now:
            if self.move_started_at is None or (
                self.last_move_at is not None and now - self.last_move_at > AUTO_PICKUP_MOVE_EXPIRE_SECONDS
            ):
                self.move_started_at = now
                self.waiting_for_settle = False
            self.last_move_at = now
            if now - self.move_started_at >= AUTO_PICKUP_MOVE_MIN_SECONDS:
                self.waiting_for_settle = True
            return False

        if self.waiting_for_settle and self.last_move_at is not None and now - self.last_move_at >= AUTO_PICKUP_SETTLE_SECONDS:
            self.move_started_at = None
            self.last_move_at = None
            self.waiting_for_settle = False
            if now - self.last_pickup_at < AUTO_PICKUP_MIN_INTERVAL_SECONDS:
                return False
            self.last_pickup_at = now
            return True

        if self.last_move_at is not None and now - self.last_move_at > AUTO_PICKUP_MOVE_EXPIRE_SECONDS:
            self.move_started_at = None
            self.last_move_at = None
            self.waiting_for_settle = False
            return False
        if now - self.last_pickup_at < AUTO_PICKUP_MIN_INTERVAL_SECONDS:
            return False
        return False


def run_display_control(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["sudo", "-n", DISPLAY_CONTROL, *args],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"warning: display-control {' '.join(args)} failed: {exc}", flush=True)
        return None


def display_is_off() -> bool:
    result = run_display_control("status")
    if result is None or result.returncode != 0:
        return False
    return result.stdout.strip() == "off"


def display_off_from_status_result(result: subprocess.CompletedProcess[str] | None) -> bool:
    if result is None or result.returncode != 0:
        return False
    return result.stdout.strip() == "off"


def display_on() -> bool:
    result = run_display_control("on")
    return result is not None and result.returncode == 0


def display_off() -> bool:
    result = run_display_control("off")
    return result is not None and result.returncode == 0


def display_brightness(level: int) -> None:
    run_display_control("brightness", str(level))


def keyboard_backlight_level(screen_brightness: int) -> int:
    if screen_brightness <= 1:
        return 1
    if screen_brightness <= 4:
        return 2
    return 0


def set_keyboard_backlight(level: int) -> None:
    global LAST_KEYBOARD_BACKLIGHT_LEVEL
    if not KEYBOARD_BACKLIGHT_SCRIPT.exists():
        return
    if LAST_KEYBOARD_BACKLIGHT_LEVEL == level:
        return
    try:
        result = subprocess.run(
            ["sudo", "-n", "bash", str(KEYBOARD_BACKLIGHT_SCRIPT), "set", "--backlight", str(level)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"warning: keyboard backlight set failed: {exc}", flush=True)
        return
    if result.returncode != 0:
        print(f"warning: keyboard backlight set failed: {result.stderr.strip()}", flush=True)
        return
    LAST_KEYBOARD_BACKLIGHT_LEVEL = level


def read_keyboard_backlight() -> int | None:
    if not KEYBOARD_BACKLIGHT_SCRIPT.exists():
        return None
    try:
        result = subprocess.run(
            ["sudo", "-n", "bash", str(KEYBOARD_BACKLIGHT_SCRIPT), "get"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for token in result.stdout.split():
        if token.startswith("backlight="):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return None
    return None


def display_control_command(action: str) -> str:
    return f"sudo -n {shlex.quote(DISPLAY_CONTROL)} {shlex.quote(action)}"


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


def current_profile(values: dict[str, str]) -> str:
    mode = values.get("POWERSAVER_MODE", "balanced").lower()
    if mode not in {"eco", "balanced", "performance"}:
        return "BALANCED"
    return mode.upper()


def screen_mode_for_profile(values: dict[str, str]) -> str:
    return values.get(f"POWERSAVER_{current_profile(values)}_SCREEN_MODE", "default").lower()


def auto_brightness_enabled(values: dict[str, str]) -> bool:
    value = values.get(f"POWERSAVER_{current_profile(values)}_AUTO_BRIGHTNESS", "0").lower()
    return value in {"1", "yes", "true", "on", "enabled"}


def stand_mode_enabled(values: dict[str, str]) -> bool:
    value = values.get(f"POWERSAVER_{current_profile(values)}_STAND_MODE", "0").lower()
    return value in {"1", "yes", "true", "on", "enabled"}


def int_config(values: dict[str, str], key: str, default: int) -> int:
    try:
        return int(values.get(key, str(default)))
    except ValueError:
        return default


def auto_timeout_for_state(values: dict[str, str], state: str, pose: str) -> int:
    profile = current_profile(values)
    if state == "ac":
        return int_config(values, f"POWERSAVER_{profile}_AUTO_AC_PUTDOWN_TIMEOUT_SEC", 120)
    return int_config(values, f"POWERSAVER_{profile}_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC", 60)


def find_xiao_tty() -> Path | None:
    ports = sorted(Path("/dev").glob("ttyACM*"))
    return ports[0] if ports else None


def open_mcu_fd(dev: Path) -> int | None:
    try:
        fd = os.open(str(dev), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = attrs[2] & ~termios.CBAUD
        attrs[2] = attrs[2] | termios.B115200
        attrs[3] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except OSError:
        os.close(fd)
        return None
    return fd


def read_mcu_sample_from_fd(fd: int, reader: McuSerialReader) -> dict[str, object] | None:
    try:
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            return None
        chunk = os.read(fd, 4096)
        if not chunk:
            return None
        reader.buffer += chunk.decode("utf-8", errors="ignore")
        while "\n" in reader.buffer or "\r" in reader.buffer:
            split_at = min((index for index in (reader.buffer.find("\n"), reader.buffer.find("\r")) if index >= 0), default=-1)
            if split_at < 0:
                break
            line = reader.buffer[:split_at].strip()
            reader.buffer = reader.buffer[split_at + 1 :]
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "accel" in payload:
                payload["_received_at"] = time.time()
                write_shared_mcu_sample(payload)
                return payload
    except OSError:
        reader.close()
    return None


def read_shared_mcu_sample(reader: McuSerialReader) -> dict[str, object] | None:
    try:
        stat = MCU_SHARED_SAMPLE_FILE.stat()
    except OSError:
        return None
    now = time.time()
    if now - stat.st_mtime > AUTO_MCU_STALE_SECONDS:
        return None
    if stat.st_mtime <= reader.last_shared_sample_mtime:
        return None
    try:
        payload = json.loads(MCU_SHARED_SAMPLE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or "accel" not in payload:
        return None
    reader.last_shared_sample_mtime = stat.st_mtime
    payload["_received_at"] = now
    return payload


def write_shared_mcu_sample(payload: dict[str, object]) -> None:
    try:
        MCU_SHARED_SAMPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = MCU_SHARED_SAMPLE_FILE.with_suffix(f"{MCU_SHARED_SAMPLE_FILE.suffix}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(MCU_SHARED_SAMPLE_FILE)
    except OSError:
        return


def lux_from_sample(sample: dict[str, object]) -> float | None:
    light = sample.get("light")
    if isinstance(light, dict):
        lux = light.get("smoothed_lux", light.get("lux"))
        if not bool(light.get("valid", lux is not None)):
            return None
    else:
        lux = sample.get("lux", sample.get("light_lux"))
    try:
        return float(lux) if lux is not None else None
    except (TypeError, ValueError):
        return None


def accel_vector(sample: dict[str, object]) -> tuple[float, float, float] | None:
    accel = sample.get("accel")
    try:
        if isinstance(accel, dict):
            return (
                float(accel.get("x", accel.get("ax"))),
                float(accel.get("y", accel.get("ay"))),
                float(accel.get("z", accel.get("az"))),
            )
        if isinstance(accel, (list, tuple)) and len(accel) >= 3:
            return (float(accel[0]), float(accel[1]), float(accel[2]))
    except (TypeError, ValueError):
        return None
    return None


def tilt_degrees(vector: tuple[float, float, float]) -> float:
    ax, ay, az = vector
    horizontal = (ay * ay + az * az) ** 0.5
    if horizontal <= 0:
        return 0.0
    import math

    return abs(math.degrees(math.atan2(ax, horizontal)))


def classify_light_level(lux: float) -> int:
    for limit, backlight in LIGHT_LEVELS:
        if limit is None or lux < limit:
            return backlight
    return 9


def update_light_smoothing(smoothed_lux: float | None, current_backlight: int | None, lux: float | None) -> tuple[float | None, int | None]:
    if lux is None:
        return smoothed_lux, current_backlight
    target = classify_light_level(lux)
    if smoothed_lux is None:
        smoothed_lux = lux
    else:
        smoothed_lux = (smoothed_lux * 0.35) + (lux * 0.65)
    return smoothed_lux, classify_light_level(smoothed_lux)


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
        display_control_command("off"),
        "resume",
        display_control_command("on"),
    ]
    print(f"idle timeout={timeout_sec}s", flush=True)
    return subprocess.Popen(command, text=True)


def stop_process(process: subprocess.Popen[str] | None, *, suppress_resume: bool = False) -> None:
    if process is None or process.poll() is not None:
        return
    if suppress_resume:
        process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def auto_screen_tick(
    values: dict[str, str],
    state: str,
    sample: dict[str, object] | None,
    last_active_at: float,
    last_sample_at: float | None,
    putdown_since: float | None,
    host_last_active_at: float,
    display_off_now: bool,
    motion_state: AutoMotionState,
) -> tuple[float, float | None, float | None, bool | None]:
    now = time.time()
    if sample is None:
        if host_last_active_at > last_active_at:
            last_active_at = host_last_active_at
            putdown_since = None
        if last_sample_at is None or now - last_sample_at > AUTO_MCU_STALE_SECONDS:
            last_active_at = now
            putdown_since = None
        return last_active_at, last_sample_at, putdown_since, None

    last_sample_at = now
    event = str(sample.get("event") or "")
    motion = str(sample.get("motion") or "")
    pose = str(sample.get("pose") or "")
    device_state = str(sample.get("state") or "")

    if device_state != "put_down" and host_last_active_at > last_active_at:
        last_active_at = host_last_active_at
        putdown_since = None

    pickup_detected = motion_state.pickup_detected(sample, now, motion, device_state)
    moving = (
        device_state == "held"
        or (device_state != "put_down" and pickup_detected)
    )
    if moving:
        last_active_at = now
        putdown_since = None
        if display_off_now:
            if not AUTO_PICKUP_WAKE_ENABLED:
                print(
                    f"auto pickup detected: wake disabled state={device_state} event={event} "
                    f"motion={motion} pickup={int(pickup_detected)}",
                    flush=True,
                )
                return last_active_at, last_sample_at, putdown_since, None
            if display_on():
                print(
                    f"auto screen on: state={device_state} event={event} "
                    f"motion={motion} pickup={int(pickup_detected)}",
                    flush=True,
                )
                return last_active_at, last_sample_at, putdown_since, False
        return last_active_at, last_sample_at, putdown_since, None

    if device_state == "stand" and stand_mode_enabled(values):
        putdown_since = None
        return last_active_at, last_sample_at, putdown_since, None

    motion_state.last_device_state = device_state
    if device_state == "put_down" and putdown_since is None:
        putdown_since = now
        print(
            f"auto putdown timer start: state={device_state} pose={pose} motion={motion} event={event}",
            flush=True,
        )

    if putdown_since is None:
        return last_active_at, last_sample_at, putdown_since, None

    timeout_sec = auto_timeout_for_state(values, state, pose)
    if timeout_sec < 0:
        return last_active_at, last_sample_at, putdown_since, None
    if timeout_sec == 0:
        return last_active_at, last_sample_at, putdown_since, None
    if now - putdown_since >= timeout_sec and not display_off_now:
        print(
            f"auto screen off: state={device_state} pose={pose} motion={motion} event={event} "
            f"timeout={timeout_sec}s putdown={int(now - putdown_since)}s",
            flush=True,
        )
        if display_off():
            putdown_since = None
            return last_active_at, last_sample_at, putdown_since, True
    return last_active_at, last_sample_at, putdown_since, None


def maybe_apply_auto_brightness(
    values: dict[str, str],
    sample: dict[str, object] | None,
    state: AutoBrightnessState,
    display_off_now: bool,
) -> None:
    if sample is None or not auto_brightness_enabled(values) or display_off_now:
        return
    state.smoothed_lux, suggested = update_light_smoothing(
        state.smoothed_lux,
        state.current_backlight,
        lux_from_sample(sample),
    )
    if suggested is None or suggested == state.current_backlight:
        return
    if state.current_backlight is not None:
        delta = abs(suggested - state.current_backlight)
        is_extreme = suggested in {1, 9}
        if delta > 1 and not is_extreme:
            step = 2 if delta >= 3 else 1
            suggested = state.current_backlight + (step if suggested > state.current_backlight else -step)
    display_brightness(suggested)
    keyboard_level = keyboard_backlight_level(suggested)
    current_keyboard_level = read_keyboard_backlight()
    if current_keyboard_level != keyboard_level:
        set_keyboard_backlight(keyboard_level)
    print(f"auto brightness: screen={suggested} keyboard={keyboard_level}", flush=True)
    state.current_backlight = suggested


def main() -> int:
    process: subprocess.Popen[str] | None = None
    active_timeout: int | None = None
    active_power_state: str | None = None
    active_screen_mode: str | None = None
    last_display_off: bool | None = None
    auto_last_active_at = time.time()
    auto_last_sample_at: float | None = None
    auto_putdown_since: float | None = None
    auto_motion_state = AutoMotionState()
    auto_brightness_state = AutoBrightnessState()
    mcu_reader = McuSerialReader()
    host_input = HostInputMonitor()
    display_cache = DisplayStateCache()
    reported_display_off: bool | None = None
    values = load_config()
    state = power_state(Path(values.get("POWERSAVER_POWER_SUPPLY_DIR", str(POWER_SUPPLY_DIR))))
    last_config_check = 0.0
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    while not stopping:
        now = time.time()
        if now - last_config_check >= CONFIG_POLL_SECONDS:
            values = load_config()
            state = power_state(Path(values.get("POWERSAVER_POWER_SUPPLY_DIR", str(POWER_SUPPLY_DIR))))
            last_config_check = now
        screen_mode = screen_mode_for_profile(values)
        needs_mcu_sample = screen_mode == "auto" or auto_brightness_enabled(values)
        host_last_active_at = host_input.poll()
        sample = mcu_reader.read_sample(needs_mcu_sample)
        timeout_sec = timeout_for_state(values, state)
        status_interval = DISPLAY_STATUS_POLL_SECONDS
        was_display_off = display_cache.get(interval=status_interval)
        if needs_mcu_sample and reported_display_off != was_display_off:
            mcu_reader.write_command("screen off" if was_display_off else "screen on")
            reported_display_off = was_display_off
        power_state_changed = active_power_state is not None and state != active_power_state
        screen_mode_changed = active_screen_mode is not None and screen_mode != active_screen_mode
        restore_display_off = was_display_off or (power_state_changed and last_display_off is True)
        if screen_mode_changed:
            auto_last_active_at = time.time()
            auto_putdown_since = None
        if timeout_sec != active_timeout or screen_mode_changed or (process is not None and process.poll() is not None):
            stop_process(process, suppress_resume=restore_display_off)
            process = start_swayidle(timeout_sec)
            if restore_display_off:
                if display_off():
                    display_cache.mark_off()
            active_timeout = timeout_sec
        elif restore_display_off and not was_display_off:
            if display_off():
                display_cache.mark_off()
                was_display_off = True
        maybe_apply_auto_brightness(
            values,
            sample,
            auto_brightness_state,
            was_display_off,
        )
        if screen_mode == "auto":
            auto_last_active_at, auto_last_sample_at, auto_putdown_since, new_display_off = auto_screen_tick(
                values,
                state,
                sample,
                auto_last_active_at,
                auto_last_sample_at,
                auto_putdown_since,
                host_last_active_at,
                was_display_off,
                auto_motion_state,
            )
            if new_display_off is True:
                display_cache.mark_off()
                was_display_off = True
            elif new_display_off is False:
                display_cache.mark_on()
                was_display_off = False
        active_power_state = state
        active_screen_mode = screen_mode
        last_display_off = was_display_off
        time.sleep(POLL_SECONDS)

    mcu_reader.close()
    host_input.close()
    stop_process(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
