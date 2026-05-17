#!/usr/bin/env python3
"""Privileged helper for the uConsole Helper DHCP."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path


RUN_DIR = Path("/tmp/uconsole-helper/dhcp")
PID_FILE = RUN_DIR / "dnsmasq.pid"
CONFIG_FILE = RUN_DIR / "dnsmasq.conf"
LEASE_FILE = RUN_DIR / "dnsmasq.leases"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: uconsole_helper_dhcp.py start|stop|status [json-config]", file=sys.stderr)
        return 2

    action = sys.argv[1]
    if action == "start":
        if len(sys.argv) != 3:
            print("start requires a JSON config", file=sys.stderr)
            return 2
        return start(json.loads(sys.argv[2]))
    if action == "stop":
        return stop()
    if action == "status":
        return status()

    print(f"unknown action: {action}", file=sys.stderr)
    return 2


def start(config: dict[str, str]) -> int:
    require_root()
    require_command("ip")
    require_command("dnsmasq")
    stop()
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    interface = config["interface"]
    server_ip = config["server_ip"]
    netmask = config["netmask"]
    prefix = mask_to_prefix(netmask)

    run(["ip", "addr", "flush", "dev", interface])
    run(["ip", "addr", "add", f"{server_ip}/{prefix}", "dev", interface])
    run(["ip", "link", "set", interface, "up"])

    write_dnsmasq_config(config)
    run(
        [
            "dnsmasq",
            "--conf-file=" + str(CONFIG_FILE),
            "--pid-file=" + str(PID_FILE),
        ]
    )
    print(f"running on {interface}, serving {config['pool_start']} - {config['pool_end']}")
    return 0


def stop() -> int:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as exc:
            print(f"failed to stop dnsmasq: {exc}", file=sys.stderr)
            return 1
        finally:
            PID_FILE.unlink(missing_ok=True)
    print("stopped")
    return 0


def status() -> int:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
        except Exception:
            print("stopped")
            return 0
        print(f"running pid={pid}")
        return 0
    print("stopped")
    return 0


def write_dnsmasq_config(config: dict[str, str]) -> None:
    lines = [
        "bind-interfaces",
        f"interface={config['interface']}",
        "except-interface=lo",
        "port=0",
        "log-dhcp",
        f"dhcp-range={config['pool_start']},{config['pool_end']},{config['netmask']},{config['lease_time']}",
        f"dhcp-leasefile={LEASE_FILE}",
    ]
    if config.get("gateway"):
        lines.append(f"dhcp-option=option:router,{config['gateway']}")
    if config.get("dns"):
        lines.append(f"dhcp-option=option:dns-server,{config['dns']}")

    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mask_to_prefix(mask: str) -> int:
    return sum(bin(int(part)).count("1") for part in mask.split("."))


def require_root() -> None:
    if os.geteuid() != 0:
        print("this helper must run as root", file=sys.stderr)
        raise SystemExit(1)


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        print(f"missing required command: {name}", file=sys.stderr)
        raise SystemExit(1)


def run(command: list[str]) -> None:
    subprocess.run(command, text=True, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
