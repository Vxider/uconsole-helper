#!/usr/bin/env python3
"""Background task runner for uConsole Helper."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG_FILE = Path("/etc/uconsole-helper/uconsole-helper.conf")
CONFIG_FILE = Path(os.environ.get("UCONSOLE_HELPER_CONFIG", str(DEFAULT_CONFIG_FILE)))


@dataclass(frozen=True)
class PowerSaverConfig:
    enabled: bool
    mode: str
    cpu_policy_path: Path
    power_supply_dir: Path
    profiles: dict[str, dict[str, str]]
    unknown_power_action: str
    wwan_policy: str
    poll_interval_sec: float


def load_config(path: Path = CONFIG_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_config_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if len(text.encode("utf-8")) > 65536:
        raise ValueError("config is too large")
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"line {line_no}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not all(ch.isupper() or ch.isdigit() or ch == "_" for ch in key):
            raise ValueError(f"line {line_no}: invalid key")
        values[key] = value.strip().strip('"').strip("'")
    return values


def write_config_from_stdin(path: Path = DEFAULT_CONFIG_FILE) -> None:
    if os.geteuid() != 0:
        raise PermissionError("write-config must run as root")
    text = os.sys.stdin.read()
    if not text.endswith("\n"):
        text += "\n"
    values = parse_config_text(text)
    powersaver_config(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        tmp_path = Path(handle.name)
    try:
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    subprocess.run(["systemctl", "restart", "uconsole-helper.service"], check=True)


def bool_config(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    return value.lower() in {"1", "yes", "true", "on", "enabled"}


def powersaver_config(values: dict[str, str]) -> PowerSaverConfig:
    mode = values.get("POWERSAVER_MODE", "balanced")
    if mode not in {"eco", "balanced", "performance"}:
        raise ValueError("POWERSAVER_MODE must be eco, balanced, or performance")
    unknown_action = values.get("POWERSAVER_UNKNOWN_POWER_ACTION", "restore")
    if unknown_action not in {"restore", "battery", "keep"}:
        raise ValueError("POWERSAVER_UNKNOWN_POWER_ACTION must be restore, battery, or keep")
    wwan_policy = values.get("POWERSAVER_WWAN_POLICY", "ondemand")
    if wwan_policy not in {"keep", "off", "ondemand"}:
        raise ValueError("POWERSAVER_WWAN_POLICY must be keep, off, or ondemand")

    profiles = {}
    for profile in ("eco", "balanced", "performance"):
        unknown_profile = values.get(
            f"POWERSAVER_{profile.upper()}_UNKNOWN_POWER_ACTION",
            values.get("POWERSAVER_UNKNOWN_POWER_ACTION", "restore"),
        )
        if unknown_profile not in {"restore", "battery", "keep"}:
            raise ValueError(f"POWERSAVER_{profile.upper()}_UNKNOWN_POWER_ACTION must be restore, battery, or keep")
        wwan_profile = values.get(
            f"POWERSAVER_{profile.upper()}_WWAN_POLICY",
            values.get("POWERSAVER_WWAN_POLICY", "ondemand"),
        )
        if wwan_profile not in {"keep", "off", "ondemand"}:
            raise ValueError(f"POWERSAVER_{profile.upper()}_WWAN_POLICY must be keep, off, or ondemand")
        profiles[profile] = {
            "battery_cpu_freq": values.get(f"POWERSAVER_{profile.upper()}_BATTERY_CPU_FREQ", values.get("POWERSAVER_BATTERY_CPU_FREQ", "1500,1500")),
            "ac_cpu_freq": values.get(f"POWERSAVER_{profile.upper()}_AC_CPU_FREQ", values.get("POWERSAVER_AC_CPU_FREQ", "restore")),
            "unknown_power_action": unknown_profile,
            "wwan_policy": wwan_profile,
        }

    return PowerSaverConfig(
        enabled=bool_config(values.get("POWERSAVER_ENABLED", "1"), default=True),
        mode=mode,
        cpu_policy_path=Path(
            values.get("POWERSAVER_CPU_POLICY_PATH", "/sys/devices/system/cpu/cpufreq/policy0")
        ),
        power_supply_dir=Path(values.get("POWERSAVER_POWER_SUPPLY_DIR", "/sys/class/power_supply")),
        profiles=profiles,
        unknown_power_action=unknown_action,
        wwan_policy=wwan_policy,
        poll_interval_sec=float(values.get("POWERSAVER_POLL_INTERVAL_SEC", "5")),
    )


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def write_file(path: Path, value: str) -> None:
    path.write_text(str(value), encoding="utf-8")


def parse_freq_pair(value: str) -> tuple[str, str]:
    min_mhz, max_mhz = value.split(",", 1)
    return f"{int(min_mhz) * 1000}", f"{int(max_mhz) * 1000}"


def clamp_freq(freq: str, cpuinfo_min: str, cpuinfo_max: str) -> str:
    freq_int = int(freq)
    return str(max(int(cpuinfo_min), min(freq_int, int(cpuinfo_max))))


class PowerSaverTask:
    def __init__(self, config: PowerSaverConfig, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.default_freqs: tuple[str, str] | None = None
        self.last_profile: str | None = None
        self.last_wwan_target: str | None = None

    def setup(self) -> None:
        if not self.config.enabled:
            print("powersaver disabled", flush=True)
            return
        if not self.config.cpu_policy_path.is_dir():
            raise FileNotFoundError(f"{self.config.cpu_policy_path} not found")
        self.default_freqs = self.read_current_freqs()
        print(
            f"powersaver mode={self.config.mode}; default cpu freq min={self.default_freqs[0]} max={self.default_freqs[1]}",
            flush=True,
        )
        print(
            f"powersaver battery cpu freq={self.active_profile()['battery_cpu_freq']} MHz; "
            f"ac cpu freq={self.active_profile()['ac_cpu_freq']}",
            flush=True,
        )
        print(
            f"powersaver unknown action={self.active_profile()['unknown_power_action']}; "
            f"wwan policy={self.active_profile()['wwan_policy']}; current wwan={self.read_wwan_state()}",
            flush=True,
        )

    def tick(self) -> None:
        if not self.config.enabled:
            return
        if self.default_freqs is None:
            self.setup()
        if self.default_freqs is None:
            return
        state = self.power_state()
        self.last_profile = self.apply_profile(state, self.default_freqs, self.last_profile)
        profile, _ = self.resolve_profile(state, self.default_freqs)
        self.last_wwan_target = self.apply_wwan_policy(profile, self.last_wwan_target)

    def find_power_supplies(self) -> list[tuple[Path, str]]:
        supplies: list[tuple[Path, str]] = []
        if not self.config.power_supply_dir.is_dir():
            return supplies
        for path in sorted(self.config.power_supply_dir.iterdir()):
            type_path = path / "type"
            if path.is_dir() and type_path.is_file():
                supplies.append((path, read_file(type_path)))
        return supplies

    def present(self, path: Path) -> bool:
        present_path = path / "present"
        if not present_path.is_file():
            return True
        return read_file(present_path) not in {"0", "false", "False"}

    def online(self, path: Path) -> bool | None:
        online_path = path / "online"
        if online_path.is_file():
            return read_file(online_path) == "1"
        status_path = path / "status"
        if status_path.is_file():
            return read_file(status_path).lower() in {"charging", "full"}
        return None

    def power_state(self) -> str:
        supplies = self.find_power_supplies()
        if not supplies:
            return "unknown"
        has_battery = False
        ac_online = False
        for path, supply_type in supplies:
            if supply_type == "Battery" and self.present(path):
                has_battery = True
                status_path = path / "status"
                if status_path.is_file() and read_file(status_path).lower() in {"charging", "full"}:
                    ac_online = True
            if supply_type in {"Mains", "USB", "USB_C", "USB_PD", "USB_DCP", "USB_CDP"}:
                if self.online(path) is True:
                    ac_online = True
        if ac_online:
            return "ac"
        if has_battery:
            return "battery"
        return "unknown"

    def read_current_freqs(self) -> tuple[str, str]:
        return (
            read_file(self.config.cpu_policy_path / "scaling_min_freq"),
            read_file(self.config.cpu_policy_path / "scaling_max_freq"),
        )

    def write_freqs(self, min_freq: str, max_freq: str) -> None:
        cpuinfo_min = read_file(self.config.cpu_policy_path / "cpuinfo_min_freq")
        cpuinfo_max = read_file(self.config.cpu_policy_path / "cpuinfo_max_freq")
        min_freq = clamp_freq(min_freq, cpuinfo_min, cpuinfo_max)
        max_freq = clamp_freq(max_freq, cpuinfo_min, cpuinfo_max)
        if int(min_freq) > int(max_freq):
            min_freq = max_freq
        if self.dry_run:
            print(f"dry-run: powersaver cpu freq min={min_freq} max={max_freq}", flush=True)
            return
        write_file(self.config.cpu_policy_path / "scaling_max_freq", max_freq)
        write_file(self.config.cpu_policy_path / "scaling_min_freq", min_freq)
        print(f"powersaver cpu freq min={min_freq} max={max_freq}", flush=True)

    def read_wwan_state(self) -> str | None:
        if shutil.which("nmcli") is None:
            return None
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "WWAN", "radio"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"warning: failed to read WWAN radio state: {exc}", flush=True)
            return None
        states = result.stdout.strip().splitlines()
        return states[-1] if states else None

    def wifi_connected(self) -> bool:
        if shutil.which("nmcli") is None:
            return False
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "TYPE,STATE", "device"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"warning: failed to read Wi-Fi device state: {exc}", flush=True)
            return False
        for line in result.stdout.splitlines():
            fields = line.split(":")
            if len(fields) >= 2 and fields[0] == "wifi" and fields[1] == "connected":
                return True
        return False

    def write_wwan_state(self, state: str) -> None:
        if state not in {"enabled", "disabled"}:
            print(f"warning: invalid WWAN target state: {state}", flush=True)
            return
        action = "on" if state == "enabled" else "off"
        if self.dry_run:
            print(f"dry-run: nmcli radio wwan {action}", flush=True)
            return
        try:
            subprocess.run(["nmcli", "radio", "wwan", action], check=True)
            print(f"powersaver wwan radio={state}", flush=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"warning: failed to set WWAN radio {action}: {exc}", flush=True)

    def apply_wwan_policy(self, profile: str, last_wwan_target: str | None) -> str | None:
        wwan_policy = self.active_profile()["wwan_policy"]
        if wwan_policy == "keep":
            return last_wwan_target
        if wwan_policy == "off":
            target = "disabled"
        elif profile == "ac":
            target = "enabled"
        elif profile == "battery":
            target = "disabled" if self.wifi_connected() else "enabled"
        else:
            return last_wwan_target
        if target == last_wwan_target:
            return last_wwan_target
        print(
            f"powersaver wwan policy={wwan_policy}; profile={profile}; target={target}",
            flush=True,
        )
        self.write_wwan_state(target)
        return target

    def active_profile(self) -> dict[str, str]:
        return self.config.profiles[self.config.mode]

    def resolve_profile(self, state: str, default_freqs: tuple[str, str]) -> tuple[str, tuple[str, str] | None]:
        active = self.active_profile()
        if state == "battery":
            return "battery", parse_freq_pair(active["battery_cpu_freq"])
        if state == "ac":
            freqs = default_freqs if active["ac_cpu_freq"] == "restore" else parse_freq_pair(active["ac_cpu_freq"])
            return "ac", freqs
        unknown_action = active["unknown_power_action"]
        if unknown_action == "battery":
            return "battery", parse_freq_pair(active["battery_cpu_freq"])
        if unknown_action == "restore":
            freqs = default_freqs if active["ac_cpu_freq"] == "restore" else parse_freq_pair(active["ac_cpu_freq"])
            return "ac", freqs
        return "keep", None

    def apply_profile(
        self,
        state: str,
        default_freqs: tuple[str, str],
        last_profile: str | None,
    ) -> str | None:
        profile, freqs = self.resolve_profile(state, default_freqs)
        if profile == last_profile:
            return last_profile
        if freqs:
            print(f"powersaver power state={state}; applying {profile} profile", flush=True)
            self.write_freqs(*freqs)
        else:
            print(f"powersaver power state={state}; keeping current CPU frequency", flush=True)
        return profile


def run_service(once: bool = False, dry_run: bool = False) -> None:
    values = load_config()
    tasks = [PowerSaverTask(powersaver_config(values), dry_run=dry_run)]
    for task in tasks:
        task.setup()

    interval = min(task.config.poll_interval_sec for task in tasks)
    while True:
        for task in tasks:
            try:
                task.tick()
            except Exception as exc:
                print(f"error: {exc}", flush=True)
        if once:
            break
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "write-config"),
        default="run",
        help="run service or write validated config from stdin",
    )
    parser.add_argument("--once", action="store_true", help="run enabled background tasks once and exit")
    parser.add_argument("--dry-run", action="store_true", help="print actions without writing system state")
    args = parser.parse_args()
    if args.command == "write-config":
        write_config_from_stdin()
        return 0
    run_service(once=args.once, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
