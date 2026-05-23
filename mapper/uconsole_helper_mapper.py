#!/usr/bin/python3

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import subprocess
import shlex
import shutil
import signal
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evdev import InputDevice, UInput, ecodes, list_devices


LOGGER = logging.getLogger("uconsole-helper-mapper")
DEFAULT_CONFIG_PATH = Path("~/.config/uconsole-helper-mapper/config.toml").expanduser()
DEFAULT_BACKLIGHT_POWER = Path("/sys/class/backlight/backlight@0/bl_power")
DEFAULT_KEYBOARD_STATE_SCRIPT = Path("~/WorkSpace/uconsole-keyboard/tools/keyboard_state.sh").expanduser()
LOCK_POPUP_HELPER = Path("~/.local/bin/uconsole-asr-popup").expanduser()
LOCK_POPUP_TEXT = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "uconsole-helper-lock-popup.txt"
LOCK_UNLOCK_KEY = ecodes.BTN_NORTH
LOCK_UNLOCK_HOLD_SECONDS = 1.0
LOCK_SCREEN_TIMEOUT_SECONDS = 5.0
LOCK_UI_POLL_SECONDS = 0.15
IGNORED_DEVICE_SUBSTRINGS = (
    "uconsole-virtual-mouse",
    "uconsole-virtual-keyboard",
    "input-remapper",
)
LISTENER_RETRY_SECONDS = 10.0
LISTENER_LOG_SECONDS = 60.0


def expand_path(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def code_from_name(name: str) -> int:
    try:
        return int(getattr(ecodes, name))
    except AttributeError as exc:
        raise ValueError(f"unknown input code: {name}") from exc


def match_any(name: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    lowered = name.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def is_ignored_device(name: str) -> bool:
    lowered = name.lower()
    return any(pattern in lowered for pattern in IGNORED_DEVICE_SUBSTRINGS)


@dataclass(slots=True)
class Binding:
    buttons: frozenset[int]
    command: str | None = None
    press_command: str | None = None
    release_command: str | None = None
    emit_key: int | None = None
    emit_rel: int | None = None
    emit_rel_value: int = 0
    text: str | None = None
    press_enter: bool = False
    hold_ms: int = 0
    repeat_ms: int = 0


@dataclass(slots=True)
class MouseRemap:
    from_code: int
    to_code: int


@dataclass(slots=True)
class Config:
    rescan_seconds: float
    session_watch_processes: list[str]
    session_watch_settle_ms: int
    gamepad_patterns: list[str]
    gamepad_debounce_ms: int
    gamepad_bindings: list[Binding]
    keyboard_enabled: bool
    keyboard_grab: bool
    keyboard_patterns: list[str]
    keyboard_debounce_ms: int
    keyboard_repeat_rate: int
    keyboard_repeat_delay_ms: int
    keyboard_bindings: list[Binding]
    lock_enabled: bool
    lock_key: int
    lock_command: str | None
    unlock_command: str | None
    keyboard_backlight_script: str | None
    power_button_enabled: bool
    power_button_patterns: list[str]
    power_button_hold_ms: int
    mouse_enabled: bool
    mouse_grab: bool
    mouse_patterns: list[str]
    mouse_remaps: list[MouseRemap]


@dataclass(slots=True)
class DeviceTask:
    role: str
    task: asyncio.Task[None]


class DeviceWriteError(RuntimeError):
    pass


class KeyboardBacklightController:
    BACKLIGHT_RE = re.compile(r"\bbacklight=(\d+)\b")

    def __init__(self, script: str | None) -> None:
        self.script = script
        self.saved_level: int | None = None
        self.current_level: int | None = None
        self.task: asyncio.Task[None] | None = None

    def lock(self) -> None:
        self._replace_task(self._lock())

    def unlock(self) -> None:
        self._replace_task(self._unlock())

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def _replace_task(self, coro: Any) -> None:
        self.cancel()
        self.task = asyncio.create_task(coro)

    async def _lock(self) -> None:
        await asyncio.to_thread(self._lock_sync)

    async def _unlock(self) -> None:
        await asyncio.to_thread(self._unlock_sync)

    def _lock_sync(self) -> None:
        level = self._read_level()
        if level is None:
            return
        self.saved_level = level
        if level != 0:
            self._set_level(0)

    def _unlock_sync(self) -> None:
        if self.saved_level is not None:
            self._set_level(self.saved_level)
        self.saved_level = None

    def _read_level(self) -> int | None:
        if not self.script:
            return None
        try:
            result = subprocess.run(
                ["sudo", "-n", "bash", self.script, "get"],
                check=False,
                capture_output=True,
                text=True,
                timeout=4,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOGGER.warning("keyboard backlight read failed: %s", exc)
            return None
        if result.returncode != 0:
            LOGGER.warning("keyboard backlight read failed: %s", result.stderr.strip())
            return None
        match = self.BACKLIGHT_RE.search(result.stdout)
        if not match:
            LOGGER.warning("keyboard backlight read returned unexpected output: %s", result.stdout.strip())
            return None
        self.current_level = int(match.group(1))
        return self.current_level

    def _set_level(self, level: int) -> None:
        if not self.script:
            return
        if self.current_level == level:
            return
        try:
            result = subprocess.run(
                ["sudo", "-n", "bash", self.script, "set", "--backlight", str(level)],
                check=False,
                capture_output=True,
                text=True,
                timeout=4,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOGGER.warning("keyboard backlight set %s failed: %s", level, exc)
            return
        if result.returncode != 0:
            LOGGER.warning("keyboard backlight set %s failed: %s", level, result.stderr.strip())
            return
        self.current_level = level


class LockController:
    def __init__(
        self,
        config: Config,
        runner: ActionRunner,
        keyboard_backlight: KeyboardBacklightController,
        virtual_keys: VirtualKeyClicker | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.keyboard_backlight = keyboard_backlight
        self.virtual_keys = virtual_keys
        self.locked = False
        self.lock_reason = ""
        self.lock_activity_at = time.monotonic()
        self.manual_unlock_pending_until = 0.0
        self.listeners: list[Any] = []
        self.listener_retry_after: dict[int, float] = {}
        self.listener_log_after: dict[int, float] = {}

    def add_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def sync_listeners(self) -> None:
        self._notify_listeners()

    def is_lock_key(self, code: int) -> bool:
        return self.config.lock_enabled and code == self.config.lock_key

    def handle_lock_key(self, value: int) -> bool:
        if value != 1:
            return True
        if self.locked:
            self.force_unlock(reason="lock key wake")
        else:
            self.run_lock_command()
        return True

    def toggle_locked(self, *, reason: str) -> bool:
        return self.set_locked(not self.locked, run_command=True, reason=reason)

    def force_unlock(self, *, reason: str) -> bool:
        if "wake" in reason or "lock key" in reason or "power button" in reason:
            self.manual_unlock_pending_until = time.monotonic() + 3.0
        if not self.locked:
            LOGGER.info("keyboard lock already disabled; force unlock actions (%s)", reason)
            self.run_unlock_command()
            self._notify_listeners()
            return False
        return self.set_locked(False, run_command=True, reason=reason)

    def note_lock_activity(self) -> None:
        self.lock_activity_at = time.monotonic()

    def set_locked(self, locked: bool, *, run_command: bool, reason: str) -> bool:
        if self.locked == locked:
            return False

        self.locked = locked
        self.lock_reason = reason if locked else ""
        self.note_lock_activity()
        LOGGER.info(
            "keyboard lock %s%s",
            "enabled" if self.locked else "disabled",
            f" ({reason})" if reason else "",
        )
        self._notify_listeners()

        if self.locked:
            if run_command:
                self.run_lock_command()
        else:
            if run_command:
                self.run_unlock_command()
        return True

    def _notify_listeners(self) -> None:
        now = time.monotonic()
        for listener in list(self.listeners):
            key = id(listener)
            if now < self.listener_retry_after.get(key, 0.0):
                continue
            try:
                listener()
                self.listener_retry_after.pop(key, None)
                self.listener_log_after.pop(key, None)
            except OSError as exc:
                self.listener_retry_after[key] = now + LISTENER_RETRY_SECONDS
                if now >= self.listener_log_after.get(key, 0.0):
                    LOGGER.warning("lock listener failed; retrying in %.0fs: %s", LISTENER_RETRY_SECONDS, exc)
                    self.listener_log_after[key] = now + LISTENER_LOG_SECONDS

    def run_lock_command(self) -> None:
        self._run_lock_state_command(self.config.lock_command)

    def request_lock_screen(self, *, reason: str) -> None:
        self.lock_reason = reason
        self.note_lock_activity()
        self.run_lock_command()

    def run_unlock_command(self) -> None:
        self._run_lock_state_command(self.config.unlock_command)
        self.emit_wakeup()

    def wake_screen(self) -> None:
        self.note_lock_activity()
        self.run_unlock_command()

    def update_lock_progress(self, fraction: float) -> None:
        for listener in list(self.listeners):
            callback = getattr(listener, "update_lock_progress", None)
            if callback is None:
                callback = getattr(getattr(listener, "__self__", None), "update_lock_progress", None)
            if callback is None:
                continue
            try:
                callback(fraction)
            except OSError:
                pass

    def emit_wakeup(self) -> None:
        if self.virtual_keys is None:
            return
        asyncio.create_task(self._emit_wakeup_delayed())

    async def _emit_wakeup_delayed(self) -> None:
        await asyncio.sleep(0.15)
        self.virtual_keys.click(ecodes.KEY_WAKEUP)

    def _run_lock_state_command(self, command: str | None) -> None:
        if not command:
            return
        self.runner.run(
            Binding(buttons=frozenset({self.config.lock_key}), command=command),
            debounce_ms=0,
        )


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))

    general = raw.get("general", {})
    gamepad = raw.get("gamepad", {})
    keyboard = raw.get("keyboard", {})
    lock = raw.get("lock", {})
    power_button = raw.get("power_button", {})
    mouse = raw.get("mouse", {})
    bindings_raw = gamepad.get("bindings", [])
    keyboard_bindings_raw = keyboard.get("bindings", [])
    remaps_raw = mouse.get("remaps", [])

    bindings: list[Binding] = []
    for item in bindings_raw:
        buttons = frozenset(code_from_name(button) for button in item["buttons"])
        bindings.append(
            Binding(
                buttons=buttons,
                command=expand_path(item["command"]) if item.get("command") else None,
                press_command=expand_path(item["press_command"]) if item.get("press_command") else None,
                release_command=expand_path(item["release_command"]) if item.get("release_command") else None,
                emit_key=code_from_name(item["emit_key"]) if item.get("emit_key") else None,
                emit_rel=code_from_name(item["emit_rel"]) if item.get("emit_rel") else None,
                emit_rel_value=int(item.get("emit_rel_value", 0)),
                text=item.get("text"),
                press_enter=bool(item.get("press_enter", False)),
                hold_ms=int(item.get("hold_ms", 0)),
                repeat_ms=int(item.get("repeat_ms", 0)),
            )
        )

    keyboard_bindings: list[Binding] = []
    for item in keyboard_bindings_raw:
        buttons = frozenset(code_from_name(button) for button in item["buttons"])
        keyboard_bindings.append(
            Binding(
                buttons=buttons,
                command=expand_path(item["command"]) if item.get("command") else None,
                press_command=expand_path(item["press_command"]) if item.get("press_command") else None,
                release_command=expand_path(item["release_command"]) if item.get("release_command") else None,
                emit_key=code_from_name(item["emit_key"]) if item.get("emit_key") else None,
                emit_rel=code_from_name(item["emit_rel"]) if item.get("emit_rel") else None,
                emit_rel_value=int(item.get("emit_rel_value", 0)),
                text=item.get("text"),
                press_enter=bool(item.get("press_enter", False)),
                hold_ms=int(item.get("hold_ms", 0)),
                repeat_ms=int(item.get("repeat_ms", 0)),
            )
        )

    remaps: list[MouseRemap] = []
    for item in remaps_raw:
        remaps.append(
            MouseRemap(
                from_code=code_from_name(item["from"]),
                to_code=code_from_name(item["to"]),
            )
        )

    return Config(
        rescan_seconds=float(general.get("rescan_seconds", 3.0)),
        session_watch_processes=list(general.get("session_watch_processes", ["wf-panel-pi", "labwc"])),
        session_watch_settle_ms=int(general.get("session_watch_settle_ms", 1500)),
        gamepad_patterns=list(gamepad.get("device_name_patterns", ["ClockworkPI uConsole"])),
        gamepad_debounce_ms=int(gamepad.get("debounce_ms", 250)),
        gamepad_bindings=bindings,
        keyboard_enabled=bool(keyboard.get("enabled", False)),
        keyboard_grab=bool(keyboard.get("grab", True)),
        keyboard_patterns=list(keyboard.get("device_name_patterns", ["ClockworkPI uConsole Keyboard"])),
        keyboard_debounce_ms=int(keyboard.get("debounce_ms", 50)),
        keyboard_repeat_rate=int(keyboard.get("repeat_rate", 20)),
        keyboard_repeat_delay_ms=int(keyboard.get("repeat_delay_ms", 600)),
        keyboard_bindings=keyboard_bindings,
        lock_enabled=bool(lock.get("enabled", False)),
        lock_key=code_from_name(lock.get("key", "KEY_COFFEE")),
        lock_command=expand_path(lock["lock_command"]) if lock.get("lock_command") else None,
        unlock_command=expand_path(lock["unlock_command"]) if lock.get("unlock_command") else None,
        keyboard_backlight_script=expand_path(
            lock.get("keyboard_backlight_script", str(DEFAULT_KEYBOARD_STATE_SCRIPT))
        )
        if lock.get("keyboard_backlight_script", str(DEFAULT_KEYBOARD_STATE_SCRIPT))
        else None,
        power_button_enabled=bool(power_button.get("enabled", False)),
        power_button_patterns=list(power_button.get("device_name_patterns", ["axp20x-pek"])),
        power_button_hold_ms=int(power_button.get("hold_ms", 700)),
        mouse_enabled=bool(mouse.get("enabled", True)),
        mouse_grab=bool(mouse.get("grab", True)),
        mouse_patterns=list(mouse.get("device_name_patterns", [])),
        mouse_remaps=remaps,
    )


class ActionRunner:
    def __init__(self) -> None:
        self._last_run: dict[str, float] = {}
        self._env = os.environ.copy()
        self._hydrate_session_env()
        self._wtype_path = shutil.which("wtype")

    def _hydrate_session_env(self) -> None:
        uid = os.getuid()
        self._env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        session_env = self._read_systemd_user_environment()
        for key in ("WAYLAND_DISPLAY", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
            value = session_env.get(key)
            if value:
                self._env[key] = value
        self._env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        self._env.setdefault("DISPLAY", ":0")
        self._env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")

    def _read_systemd_user_environment(self) -> dict[str, str]:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            LOGGER.debug("unable to read systemd user environment: %s", exc)
            return {}

        if result.returncode != 0:
            LOGGER.debug(
                "systemctl --user show-environment failed: rc=%s stderr=%s",
                result.returncode,
                result.stderr.strip(),
            )
            return {}

        session_env: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep and key:
                session_env[key] = value
        return session_env

    def run(self, binding: Binding, debounce_ms: int) -> None:
        key = self._binding_key(binding)
        now = time.monotonic()
        last = self._last_run.get(key, 0.0)
        if (now - last) * 1000 < debounce_ms:
            return
        self._last_run[key] = now
        if binding.command:
            self._run_command(binding.command)
            return
        if binding.text is not None:
            self._run_text(binding.text, binding.press_enter)
            return

    def run_phase(self, binding: Binding, phase: str, debounce_ms: int) -> None:
        command: str | None
        if phase == "press":
            command = binding.press_command
        elif phase == "release":
            command = binding.release_command
        else:
            raise ValueError(f"unknown phase: {phase}")

        if not command:
            return

        key = f"{phase}:{','.join(str(code) for code in sorted(binding.buttons))}:{command}"
        now = time.monotonic()
        last = self._last_run.get(key, 0.0)
        if (now - last) * 1000 < debounce_ms:
            return
        self._last_run[key] = now
        self._run_command(command)

    def _binding_key(self, binding: Binding) -> str:
        if binding.command:
            return f"command:{binding.command}"
        if binding.press_command or binding.release_command:
            return f"phase:{binding.press_command}:{binding.release_command}"
        if binding.text is not None:
            return f"text:{binding.text}:{int(binding.press_enter)}"
        return f"emit:{binding.emit_key}"

    def _run_command(self, command: str) -> None:
        LOGGER.info("run command: %s", command)
        subprocess.Popen(
            ["sh", "-lc", command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=self._env,
        )

    def _run_text(self, text: str, press_enter: bool) -> None:
        if not self._wtype_path:
            LOGGER.error("text binding requires wtype in PATH")
            return
        quoted_text = shlex.quote(text)
        command = f"{shlex.quote(self._wtype_path)} {quoted_text}"
        if press_enter:
            command += f" && {shlex.quote(self._wtype_path)} -k Return"
        LOGGER.info("type text via wtype: %s", text)
        subprocess.Popen(
            ["sh", "-lc", command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=self._env,
        )


def validate_binding(binding: Binding) -> None:
    action_count = sum(
        (
            binding.command is not None,
            binding.emit_key is not None,
            binding.emit_rel is not None,
            binding.text is not None,
        )
    )
    phase_action = binding.press_command is not None or binding.release_command is not None
    if phase_action:
        if action_count != 0:
            raise ValueError("phase bindings cannot be combined with command, emit_key, emit_rel, or text")
    elif action_count != 1:
        raise ValueError("binding must define exactly one of command, emit_key, emit_rel, or text")
    if binding.press_enter and binding.text is None:
        raise ValueError("press_enter requires text")
    if binding.emit_rel is not None and binding.emit_rel_value == 0:
        raise ValueError("emit_rel_value must be non-zero when emit_rel is set")
    if binding.emit_rel is None and binding.emit_rel_value != 0:
        raise ValueError("emit_rel_value requires emit_rel")
    if binding.hold_ms < 0:
        raise ValueError("hold_ms must be >= 0")
    if binding.repeat_ms < 0:
        raise ValueError("repeat_ms must be >= 0")
    if phase_action and binding.repeat_ms != 0:
        raise ValueError("phase bindings do not support repeat_ms")


class GamepadWatcher:
    def __init__(
        self,
        device: InputDevice,
        config: Config,
        runner: ActionRunner,
        lock_controller: LockController,
    ) -> None:
        self.device = device
        self.config = config
        self.runner = runner
        self.lock_controller = lock_controller
        for binding in self.config.gamepad_bindings:
            validate_binding(binding)
        self.pressed: set[int] = set()
        self.active_bindings: set[int] = set()
        self.hold_tasks: dict[int, asyncio.Task[None]] = {}
        self.hold_fired_buttons: set[frozenset[int]] = set()
        self.release_trigger_bindings: set[int] = set()
        self.repeat_tasks: dict[int, asyncio.Task[None]] = {}
        self.grabbed_for_lock = False
        self.unlock_hold_task: asyncio.Task[None] | None = None
        self.suppress_unlock_key_until_release = False
        self.lock_controller.add_listener(self._sync_lock_grab)

    async def run(self) -> None:
        LOGGER.info("watch gamepad: %s (%s)", self.device.name, self.device.path)
        try:
            while True:
                self._sync_lock_grab()
                try:
                    event = await asyncio.wait_for(self.device.async_read_one(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                if event.type != ecodes.EV_KEY:
                    continue
                if self._suppress_unlock_release(event.code, event.value):
                    continue
                if self.lock_controller.locked:
                    self._handle_locked_key(event.code, event.value)
                    continue
                self._handle_key(event.code, event.value)
        except OSError as exc:
            LOGGER.warning("gamepad watcher stopped for %s: %s", self.device.path, exc)
        finally:
            if self.grabbed_for_lock:
                try:
                    self.device.ungrab()
                except OSError:
                    pass
            for task in self.hold_tasks.values():
                task.cancel()
            for task in self.repeat_tasks.values():
                task.cancel()
            if self.unlock_hold_task is not None:
                self.unlock_hold_task.cancel()
            self.device.close()

    def _sync_lock_grab(self) -> None:
        if not self.config.lock_enabled:
            return
        if self.lock_controller.locked and not self.grabbed_for_lock:
            self.device.grab()
            self.grabbed_for_lock = True
            self._clear_state()
            return
        if not self.lock_controller.locked and self.grabbed_for_lock:
            self.device.ungrab()
            self.grabbed_for_lock = False
            self._clear_state()

    def _clear_state(self) -> None:
        self.pressed.clear()
        self.active_bindings.clear()
        self.hold_fired_buttons.clear()
        self.release_trigger_bindings.clear()
        for task in self.hold_tasks.values():
            task.cancel()
        self.hold_tasks.clear()
        for task in self.repeat_tasks.values():
            task.cancel()
        self.repeat_tasks.clear()
        if self.unlock_hold_task is not None:
            self.unlock_hold_task.cancel()
            self.unlock_hold_task = None
        self.suppress_unlock_key_until_release = False

    def _suppress_unlock_release(self, code: int, value: int) -> bool:
        if not self.suppress_unlock_key_until_release or code != LOCK_UNLOCK_KEY:
            return False
        if value == 0:
            self.suppress_unlock_key_until_release = False
        return True

    def _handle_locked_key(self, code: int, value: int) -> None:
        if value == 1:
            self.lock_controller.wake_screen()
        if code != LOCK_UNLOCK_KEY:
            return
        if value == 1:
            self.lock_controller.note_lock_activity()
            self.lock_controller.update_lock_progress(0.0)
            if self.unlock_hold_task is not None:
                self.unlock_hold_task.cancel()
            self.unlock_hold_task = asyncio.create_task(self._unlock_after_hold())
            return
        if value == 2:
            self.lock_controller.note_lock_activity()
            return
        if value == 0:
            self.lock_controller.note_lock_activity()
            if self.unlock_hold_task is not None:
                self.unlock_hold_task.cancel()
                self.unlock_hold_task = None
            self.lock_controller.update_lock_progress(0.0)

    async def _unlock_after_hold(self) -> None:
        started = time.monotonic()
        try:
            while True:
                elapsed = time.monotonic() - started
                progress = min(1.0, elapsed / LOCK_UNLOCK_HOLD_SECONDS)
                self.lock_controller.update_lock_progress(progress)
                if progress >= 1.0:
                    break
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return
        self.unlock_hold_task = None
        self.suppress_unlock_key_until_release = True
        self.lock_controller.force_unlock(reason="gamebutton Y hold")

    async def _fire_hold(self, index: int, binding: Binding) -> None:
        try:
            await asyncio.sleep(binding.hold_ms / 1000)
        except asyncio.CancelledError:
            return
        if index not in self.active_bindings:
            return
        if not binding.buttons.issubset(self.pressed):
            return
        self.hold_fired_buttons.add(binding.buttons)
        if binding.press_command is not None or binding.release_command is not None:
            self.runner.run_phase(binding, "press", self.config.gamepad_debounce_ms)
        else:
            self._trigger_binding(binding)

    def _trigger_binding(self, binding: Binding) -> None:
        if binding.command or binding.text is not None:
            self.runner.run(binding, self.config.gamepad_debounce_ms)

    def _has_hold_variant(self, index: int, binding: Binding) -> bool:
        return any(
            other_index != index and other_binding.hold_ms > 0 and other_binding.buttons == binding.buttons
            for other_index, other_binding in enumerate(self.config.gamepad_bindings)
        )

    async def _repeat_binding(self, index: int, binding: Binding) -> None:
        try:
            while True:
                await asyncio.sleep(binding.repeat_ms / 1000)
                if index not in self.active_bindings:
                    return
                if not binding.buttons.issubset(self.pressed):
                    return
                self._trigger_binding(binding)
        except asyncio.CancelledError:
            return

    def _handle_key(self, code: int, value: int) -> None:
        is_pressed = value != 0
        if is_pressed:
            self.pressed.add(code)
        else:
            self.pressed.discard(code)

        for index, binding in enumerate(self.config.gamepad_bindings):
            matched = binding.buttons.issubset(self.pressed)
            was_active = index in self.active_bindings
            if matched and was_active:
                continue
            if matched and not was_active:
                self.active_bindings.add(index)
                if binding.press_command is not None or binding.release_command is not None:
                    if binding.hold_ms > 0:
                        self.hold_tasks[index] = asyncio.create_task(self._fire_hold(index, binding))
                    else:
                        self.runner.run_phase(binding, "press", self.config.gamepad_debounce_ms)
                elif binding.hold_ms > 0:
                    self.hold_tasks[index] = asyncio.create_task(self._fire_hold(index, binding))
                elif self._has_hold_variant(index, binding):
                    self.release_trigger_bindings.add(index)
                else:
                    self._trigger_binding(binding)
                    if binding.repeat_ms > 0:
                        self.repeat_tasks[index] = asyncio.create_task(self._repeat_binding(index, binding))
            elif not matched and was_active:
                self.active_bindings.remove(index)
                task = self.hold_tasks.pop(index, None)
                if task is not None:
                    task.cancel()
                task = self.repeat_tasks.pop(index, None)
                if task is not None:
                    task.cancel()
                if (
                    (binding.press_command is not None or binding.release_command is not None)
                    and (binding.hold_ms <= 0 or binding.buttons in self.hold_fired_buttons)
                ):
                    self.runner.run_phase(binding, "release", self.config.gamepad_debounce_ms)
                elif index in self.release_trigger_bindings:
                    self.release_trigger_bindings.remove(index)
                    if binding.buttons not in self.hold_fired_buttons:
                        self._trigger_binding(binding)
                if not any(
                    active_binding.buttons == binding.buttons
                    for active_index, active_binding in enumerate(self.config.gamepad_bindings)
                    if active_index in self.active_bindings
                ):
                    self.hold_fired_buttons.discard(binding.buttons)


class VirtualKeyboard:
    def __init__(
        self,
        device: InputDevice,
        repeat_rate: int,
        repeat_delay_ms: int,
        extra_keys: set[int] | None = None,
    ) -> None:
        capabilities = {
            event_type: list(codes)
            for event_type, codes in device.capabilities().items()
            if event_type != ecodes.EV_SYN
        }
        if extra_keys:
            key_codes = set(capabilities.get(ecodes.EV_KEY, []))
            key_codes.update(extra_keys)
            capabilities[ecodes.EV_KEY] = sorted(key_codes)

        self.ui = UInput(
            capabilities,
            name="uconsole-virtual-keyboard",
            vendor=device.info.vendor,
            product=device.info.product,
            version=device.info.version,
            bustype=device.info.bustype,
            phys=f"{device.phys or 'uconsole'}/virtual",
        )
        # Keep repeat settings explicit so held keys repeat even when the
        # physical keyboard is grabbed and re-emitted through uinput.
        if self.ui.device is None:
            LOGGER.debug("virtual keyboard device handle is unavailable; skip repeat configuration")
        else:
            try:
                self.ui.device.repeat = (repeat_delay_ms, repeat_rate)
            except (AttributeError, OSError) as exc:
                LOGGER.warning("failed to configure keyboard repeat on virtual device: %s", exc)

    def close(self) -> None:
        self.ui.close()

    def write_key(self, code: int, value: int) -> None:
        try:
            self.ui.write(ecodes.EV_KEY, code, value)
            self.ui.syn()
        except OSError as exc:
            raise DeviceWriteError(f"virtual keyboard write failed for key {code} value {value}: {exc}") from exc


class VirtualKeyClicker:
    def __init__(self) -> None:
        self.ui = UInput(
            {ecodes.EV_KEY: [ecodes.KEY_POWER, ecodes.KEY_WAKEUP]},
            name="uconsole-virtual-system-keys",
        )

    def close(self) -> None:
        self.ui.close()

    def click(self, code: int) -> None:
        self.ui.write(ecodes.EV_KEY, code, 1)
        self.ui.syn()
        self.ui.write(ecodes.EV_KEY, code, 0)
        self.ui.syn()


class PowerButtonWatcher:
    def __init__(
        self,
        device: InputDevice,
        config: Config,
        lock_controller: LockController,
        virtual_power_button: VirtualKeyClicker,
        screen_off_reader: Any | None = None,
    ) -> None:
        self.device = device
        self.config = config
        self.lock_controller = lock_controller
        self.virtual_power_button = virtual_power_button
        self.screen_off_reader = screen_off_reader
        self.down_at: float | None = None
        self.hold_task: asyncio.Task[None] | None = None
        self.hold_fired = False
        self.unlock_on_release = False

    async def run(self) -> None:
        grabbed = False
        LOGGER.info("watch power button: %s (%s)", self.device.name, self.device.path)
        try:
            self.device.grab()
            grabbed = True
            async for event in self.device.async_read_loop():
                if event.type != ecodes.EV_KEY or event.code != ecodes.KEY_POWER:
                    continue
                self._handle_power(event.value)
        except OSError as exc:
            LOGGER.warning("power button watcher stopped for %s: %s", self.device.path, exc)
        finally:
            if grabbed:
                try:
                    self.device.ungrab()
                except OSError:
                    pass
            if self.hold_task is not None:
                self.hold_task.cancel()
            self.device.close()

    def _handle_power(self, value: int) -> None:
        if value == 1:
            self.down_at = time.monotonic()
            self.hold_fired = False
            self.unlock_on_release = self.screen_off_reader is not None and self.screen_off_reader() is True
            if self.hold_task is not None:
                self.hold_task.cancel()
            self.hold_task = asyncio.create_task(self._fire_hold())
            return

        if value != 0:
            return

        if self.hold_task is not None:
            self.hold_task.cancel()
            self.hold_task = None

        if not self.hold_fired:
            if self.unlock_on_release or (self.screen_off_reader is not None and self.screen_off_reader() is True):
                LOGGER.info("power button short press: screen is off; wake display and unlock keyboard")
                self.lock_controller.force_unlock(reason="power button wake")
            else:
                LOGGER.info("power button short press: display off")
                self.lock_controller.request_lock_screen(reason="power button short press")
        self.down_at = None
        self.unlock_on_release = False

    async def _fire_hold(self) -> None:
        try:
            await asyncio.sleep(self.config.power_button_hold_ms / 1000)
        except asyncio.CancelledError:
            return
        self.hold_fired = True
        LOGGER.info("power button long press: emit KEY_POWER")
        self.virtual_power_button.click(ecodes.KEY_POWER)


class KeyboardWatcher:
    def __init__(
        self,
        device: InputDevice,
        config: Config,
        runner: ActionRunner,
        lock_controller: LockController,
        virtual_mouse: VirtualMouse | None = None,
    ) -> None:
        self.device = device
        self.config = config
        self.runner = runner
        self.lock_controller = lock_controller
        self.virtual_mouse = virtual_mouse
        for binding in self.config.keyboard_bindings:
            validate_binding(binding)

        self.virtual_keyboard: VirtualKeyboard | None = None
        if self.config.keyboard_grab:
            extra_keys = {
                binding.emit_key for binding in self.config.keyboard_bindings if binding.emit_key is not None
            }
            self.virtual_keyboard = VirtualKeyboard(
                device,
                repeat_rate=self.config.keyboard_repeat_rate,
                repeat_delay_ms=self.config.keyboard_repeat_delay_ms,
                extra_keys=extra_keys,
            )
        self.binding_codes = self._binding_codes()
        self.pressed: set[int] = set()
        self.pending_order: list[int] = []
        self.pending_set: set[int] = set()
        self.active_bindings: set[int] = set()
        self.consumed_keys: set[int] = set()
        self.repeat_tasks: dict[int, asyncio.Task[None]] = {}
        self.grab_active = self.virtual_keyboard is not None
        self.grab_degraded = False
        self.grabbed = False
        self.lock_controller.add_listener(self._sync_lock_grab)

    async def run(self) -> None:
        LOGGER.info("watch keyboard: %s (%s)", self.device.name, self.device.path)
        try:
            if self.grab_active:
                self.device.grab()
                self.grabbed = True
            async for event in self.device.async_read_loop():
                if event.type != ecodes.EV_KEY:
                    continue
                self._sync_lock_grab()
                try:
                    self._handle_key(event.code, event.value)
                except DeviceWriteError as exc:
                    if not self._degrade_to_passthrough(exc, self.grabbed):
                        raise
                    self.grabbed = False
                self._sync_lock_grab()
        except OSError as exc:
            LOGGER.warning("keyboard watcher stopped for %s: %s", self.device.path, exc)
        finally:
            if self.grabbed:
                try:
                    self.device.ungrab()
                except OSError:
                    pass
            if self.virtual_keyboard is not None:
                self.virtual_keyboard.close()
            for task in self.repeat_tasks.values():
                task.cancel()
            self.device.close()

    def _sync_lock_grab(self) -> None:
        if not self.config.lock_enabled or self.grab_active:
            return
        if self.lock_controller.locked and not self.grabbed:
            self.device.grab()
            self.grabbed = True
            return
        if not self.lock_controller.locked and self.grabbed:
            self.device.ungrab()
            self.grabbed = False

    def _clear_keyboard_state(self) -> None:
        self.pressed.clear()
        self.pending_order.clear()
        self.pending_set.clear()
        self.active_bindings.clear()
        self.consumed_keys.clear()
        for task in self.repeat_tasks.values():
            task.cancel()
        self.repeat_tasks.clear()

    def _degrade_to_passthrough(self, exc: DeviceWriteError, grabbed: bool) -> bool:
        if not self.grab_active or self.grab_degraded:
            return False

        LOGGER.error(
            "keyboard watchdog: virtual keyboard path failed for %s; disabling grab and falling back to passthrough: %s",
            self.device.path,
            exc,
        )
        self.grab_degraded = True
        self.grab_active = False
        self.pending_order.clear()
        self.pending_set.clear()
        self.active_bindings.clear()
        self.consumed_keys.clear()
        for task in self.repeat_tasks.values():
            task.cancel()
        self.repeat_tasks.clear()
        if grabbed:
            try:
                self.device.ungrab()
            except OSError:
                pass
        if self.virtual_keyboard is not None:
            try:
                self.virtual_keyboard.close()
            except OSError:
                pass
            self.virtual_keyboard = None
        return True

    def _binding_codes(self) -> set[int]:
        codes: set[int] = set()
        for binding in self.config.keyboard_bindings:
            codes.update(binding.buttons)
        return codes

    def _possible_binding(self) -> bool:
        relevant_pressed = self.pressed & self.binding_codes
        if not relevant_pressed:
            return False
        return any(relevant_pressed.issubset(binding.buttons) for binding in self.config.keyboard_bindings)

    def _queue_pending(self, code: int) -> None:
        if code in self.pending_set:
            return
        self.pending_order.append(code)
        self.pending_set.add(code)

    def _discard_pending(self, code: int) -> None:
        if code not in self.pending_set:
            return
        self.pending_set.remove(code)
        self.pending_order = [item for item in self.pending_order if item != code]

    def _flush_pending(self) -> None:
        for code in list(self.pending_order):
            if code in self.pending_set and code in self.pressed and code not in self.consumed_keys:
                self.virtual_keyboard.write_key(code, 1)
        self.pending_order.clear()
        self.pending_set.clear()

    def _refresh_active_bindings(self) -> None:
        for index, binding in enumerate(self.config.keyboard_bindings):
            matched = binding.buttons.issubset(self.pressed)
            was_active = index in self.active_bindings
            if matched and not was_active:
                self._activate_binding(binding)
                self.active_bindings.add(index)
                if binding.repeat_ms > 0 and binding.emit_key is None:
                    self.repeat_tasks[index] = asyncio.create_task(self._repeat_binding(index, binding))
                self.consumed_keys.update(binding.buttons)
                for code in binding.buttons:
                    self._discard_pending(code)
            elif not matched and was_active:
                self.active_bindings.remove(index)
                task = self.repeat_tasks.pop(index, None)
                if task is not None:
                    task.cancel()
                self._deactivate_binding(binding)

        active_codes: set[int] = set()
        for index in self.active_bindings:
            active_codes.update(self.config.keyboard_bindings[index].buttons)
        self.consumed_keys = active_codes

    def _activate_binding(self, binding: Binding) -> None:
        if binding.emit_key is not None and self.virtual_keyboard is not None:
            self.virtual_keyboard.write_key(binding.emit_key, 1)
            return
        if binding.emit_rel is not None and self.virtual_mouse is not None:
            self.virtual_mouse.write_synthetic_rel(binding.emit_rel, binding.emit_rel_value)
            self.virtual_mouse.syn()
            return
        if binding.command or binding.text is not None:
            self.runner.run(binding, self.config.keyboard_debounce_ms)

    def _deactivate_binding(self, binding: Binding) -> None:
        if binding.emit_key is not None and self.virtual_keyboard is not None:
            self.virtual_keyboard.write_key(binding.emit_key, 0)

    async def _repeat_binding(self, index: int, binding: Binding) -> None:
        try:
            while True:
                await asyncio.sleep(binding.repeat_ms / 1000)
                if index not in self.active_bindings:
                    return
                if not binding.buttons.issubset(self.pressed):
                    return
                self._activate_binding(binding)
        except asyncio.CancelledError:
            return

    def _handle_key(self, code: int, value: int) -> None:
        if self.lock_controller.is_lock_key(code):
            was_locked = self.lock_controller.locked
            self.lock_controller.handle_lock_key(value)
            if was_locked and not self.lock_controller.locked:
                self._clear_keyboard_state()
            return

        if self.lock_controller.locked:
            if value == 1:
                self.lock_controller.wake_screen()
            return

        if not self.grab_active:
            self._handle_key_passthrough(code, value)
            return

        if value == 2:
            if code in self.consumed_keys:
                return
            if code in self.pending_set:
                # Let the virtual keyboard own autorepeat once a pending key
                # is disambiguated, instead of replaying hardware repeats.
                self._flush_pending()
            return

        is_pressed = value != 0

        if is_pressed:
            self.pressed.add(code)
            if code in self.binding_codes:
                self._refresh_active_bindings()
                if code in self.consumed_keys:
                    return
                if self._possible_binding():
                    self._queue_pending(code)
                    return
                self._flush_pending()
                self.virtual_keyboard.write_key(code, 1)
                return

            if self.pending_set:
                self._flush_pending()
            self.virtual_keyboard.write_key(code, 1)
            return

        was_consumed = code in self.consumed_keys
        self.pressed.discard(code)
        self._refresh_active_bindings()

        if was_consumed:
            return

        if code in self.pending_set:
            self._discard_pending(code)
            self.virtual_keyboard.write_key(code, 1)
            self.virtual_keyboard.write_key(code, 0)
            return

        self.virtual_keyboard.write_key(code, 0)

    def _handle_key_passthrough(self, code: int, value: int) -> None:
        if self.lock_controller.is_lock_key(code):
            was_locked = self.lock_controller.locked
            self.lock_controller.handle_lock_key(value)
            if was_locked and not self.lock_controller.locked:
                self._clear_keyboard_state()
            return

        if self.lock_controller.locked:
            if value == 1:
                self.lock_controller.wake_screen()
            return

        is_pressed = value != 0
        if is_pressed:
            self.pressed.add(code)
        else:
            self.pressed.discard(code)

        for index, binding in enumerate(self.config.keyboard_bindings):
            matched = binding.buttons.issubset(self.pressed)
            was_active = index in self.active_bindings
            if matched and not was_active:
                self._activate_binding(binding)
                self.active_bindings.add(index)
                if binding.repeat_ms > 0 and binding.emit_key is None:
                    self.repeat_tasks[index] = asyncio.create_task(self._repeat_binding(index, binding))
            elif not matched and was_active:
                self.active_bindings.remove(index)
                task = self.repeat_tasks.pop(index, None)
                if task is not None:
                    task.cancel()
                self._deactivate_binding(binding)


class VirtualMouse:
    def __init__(self) -> None:
        capabilities = {
            ecodes.EV_KEY: [
                ecodes.BTN_LEFT,
                ecodes.BTN_RIGHT,
                ecodes.BTN_MIDDLE,
                ecodes.BTN_SIDE,
                ecodes.BTN_EXTRA,
                ecodes.BTN_FORWARD,
                ecodes.BTN_BACK,
                ecodes.BTN_TASK,
            ],
            ecodes.EV_REL: [
                ecodes.REL_X,
                ecodes.REL_Y,
                ecodes.REL_WHEEL,
                ecodes.REL_HWHEEL,
                ecodes.REL_WHEEL_HI_RES,
                ecodes.REL_HWHEEL_HI_RES,
            ],
        }
        self.ui = UInput(capabilities, name="uconsole-virtual-mouse")

    def close(self) -> None:
        self.ui.close()

    def write_event(self, event_type: int, code: int, value: int) -> None:
        self.ui.write(event_type, code, value)

    def write_synthetic_rel(self, code: int, value: int) -> None:
        self.ui.write(ecodes.EV_REL, code, value)

        hi_res_code: int | None = None
        if code == ecodes.REL_WHEEL:
            hi_res_code = ecodes.REL_WHEEL_HI_RES
        elif code == ecodes.REL_HWHEEL:
            hi_res_code = ecodes.REL_HWHEEL_HI_RES

        # Mirror a physical mouse wheel "click" so stacks that prefer
        # high-resolution wheel events still accept keyboard-driven scrolling.
        if hi_res_code is not None:
            self.ui.write(ecodes.EV_REL, hi_res_code, value * 120)

    def syn(self) -> None:
        self.ui.syn()


class MouseWatcher:
    def __init__(
        self,
        device: InputDevice,
        config: Config,
        virtual_mouse: VirtualMouse,
        lock_controller: LockController,
    ) -> None:
        self.device = device
        self.config = config
        self.virtual_mouse = virtual_mouse
        self.lock_controller = lock_controller
        self.remap_to_target = {item.from_code: item.to_code for item in config.mouse_remaps}
        self.target_sources: dict[int, set[int]] = {}
        for source, target in self.remap_to_target.items():
            self.target_sources.setdefault(target, {target}).add(source)
        self.source_state: dict[int, bool] = {}
        self.target_state: dict[int, int] = {}
        self.grabbed = False
        self.lock_controller.add_listener(self._sync_lock_grab)

    async def run(self) -> None:
        LOGGER.info("watch mouse: %s (%s)", self.device.name, self.device.path)
        try:
            if self.config.mouse_grab:
                self.device.grab()
                self.grabbed = True
            async for event in self.device.async_read_loop():
                self._sync_lock_grab()
                if self.lock_controller.locked:
                    continue
                if not self.config.mouse_grab:
                    continue
                self._handle_event(event)
        except OSError as exc:
            LOGGER.warning("mouse watcher stopped for %s: %s", self.device.path, exc)
        finally:
            if self.grabbed:
                try:
                    self.device.ungrab()
                except OSError:
                    pass
            self.device.close()

    def _sync_lock_grab(self) -> None:
        if not self.config.lock_enabled or self.config.mouse_grab:
            return
        if self.lock_controller.locked and not self.grabbed:
            self.device.grab()
            self.grabbed = True
            self._clear_state()
            return
        if not self.lock_controller.locked and self.grabbed:
            self.device.ungrab()
            self.grabbed = False
            self._clear_state()

    def _clear_state(self) -> None:
        self.source_state.clear()
        self.target_state.clear()

    def _handle_event(self, event: Any) -> None:
        if event.type == ecodes.EV_REL:
            self.virtual_mouse.write_event(event.type, event.code, event.value)
            return

        if event.type == ecodes.EV_SYN:
            self.virtual_mouse.syn()
            return

        if event.type != ecodes.EV_KEY:
            return

        if event.code in self.remap_to_target:
            target = self.remap_to_target[event.code]
            self.source_state[event.code] = event.value != 0
            self._emit_target_state(target)
            return

        if event.code in self.target_sources:
            self.source_state[event.code] = event.value != 0
            self._emit_target_state(event.code)
            return

        self.virtual_mouse.write_event(event.type, event.code, event.value)

    def _emit_target_state(self, target_code: int) -> None:
        active = any(self.source_state.get(source, False) for source in self.target_sources[target_code])
        value = 1 if active else 0
        if self.target_state.get(target_code) == value:
            return
        self.target_state[target_code] = value
        self.virtual_mouse.write_event(ecodes.EV_KEY, target_code, value)


class MapperDaemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.runner = ActionRunner()
        self.keyboard_backlight = KeyboardBacklightController(config.keyboard_backlight_script)
        self.virtual_mouse = VirtualMouse() if config.mouse_enabled else None
        self.virtual_power_button = VirtualKeyClicker() if config.power_button_enabled else None
        self.lock_controller = LockController(
            config,
            self.runner,
            self.keyboard_backlight,
            self.virtual_power_button,
        )
        self.tasks: dict[str, DeviceTask] = {}
        self.session_watch_baselines: dict[str, frozenset[int]] = {}
        self.session_watch_pending: dict[str, tuple[frozenset[int], float]] = {}
        self.backlight_power_path = DEFAULT_BACKLIGHT_POWER
        self.last_backlight_off: bool | None = None
        self.lock_popup_process: subprocess.Popen[Any] | None = None
        self.lock_screen_off_sent = False
        self.lock_progress = 0.0
        self.lock_progress_visible = False
        self.lock_controller.add_listener(self._sync_lock_popup)

    async def shutdown(self) -> None:
        for entry in list(self.tasks.values()):
            entry.task.cancel()
        if self.tasks:
            await asyncio.gather(*(entry.task for entry in self.tasks.values()), return_exceptions=True)
            self.tasks.clear()
        if self.virtual_mouse is not None:
            self.virtual_mouse.close()
        if self.virtual_power_button is not None:
            self.virtual_power_button.close()
        self._close_lock_popup()
        self.keyboard_backlight.cancel()

    async def run(self) -> None:
        backlight_task = asyncio.create_task(self._monitor_backlight_lock_state())
        lock_ui_task = asyncio.create_task(self._monitor_lock_ui())
        try:
            while True:
                self._prune_tasks()
                self._scan_devices()
                self.lock_controller.sync_listeners()
                self._check_session_watch()
                await asyncio.sleep(self.config.rescan_seconds)
        finally:
            backlight_task.cancel()
            lock_ui_task.cancel()
            await asyncio.gather(backlight_task, lock_ui_task, return_exceptions=True)

    async def _monitor_lock_ui(self) -> None:
        while True:
            self._sync_lock_popup()
            self._check_lock_screen_timeout()
            await asyncio.sleep(0.5)

    def _sync_lock_popup(self) -> None:
        if self.lock_controller.locked and self.last_backlight_off is not True and not self.lock_screen_off_sent:
            self._show_lock_popup()
            return
        self._close_lock_popup()

    def _show_lock_popup(self) -> None:
        if not LOCK_POPUP_HELPER.exists():
            return
        if self.lock_popup_process is not None and self.lock_popup_process.poll() is None:
            return
        try:
            self.lock_progress = 0.0
            self.lock_progress_visible = False
            self._write_lock_popup_text()
            self.lock_popup_process = subprocess.Popen(
                [str(LOCK_POPUP_HELPER), str(LOCK_POPUP_TEXT)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            LOGGER.debug("lock popup failed: %s", exc)

    def _write_lock_popup_text(self) -> None:
        text = "# Locked\nhold Y to unlock\n"
        if self.lock_progress_visible:
            text += f"@progress={self.lock_progress:.3f}\n"
        tmp_path = LOCK_POPUP_TEXT.with_suffix(f"{LOCK_POPUP_TEXT.suffix}.{os.getpid()}.tmp")
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(LOCK_POPUP_TEXT)

    def update_lock_progress(self, fraction: float) -> None:
        self.lock_progress = max(0.0, min(1.0, fraction))
        self.lock_progress_visible = self.lock_progress > 0.0
        if self.lock_controller.locked and not self.lock_screen_off_sent:
            try:
                self._write_lock_popup_text()
            except OSError as exc:
                LOGGER.debug("lock popup progress update failed: %s", exc)

    def _close_lock_popup(self) -> None:
        process = self.lock_popup_process
        self.lock_popup_process = None
        if process is not None and process.poll() is None:
            process.terminate()
        try:
            LOCK_POPUP_TEXT.unlink()
        except OSError:
            pass

    def _check_lock_screen_timeout(self) -> None:
        if not self.lock_controller.locked:
            self.lock_screen_off_sent = False
            self.lock_progress = 0.0
            self.lock_progress_visible = False
            return
        if self.last_backlight_off is not False:
            return
        if self.lock_screen_off_sent:
            return
        if time.monotonic() - self.lock_controller.lock_activity_at < LOCK_SCREEN_TIMEOUT_SECONDS:
            return
        LOGGER.info("locked screen idle timeout: display off")
        self.lock_controller.run_lock_command()
        self.lock_screen_off_sent = True

    async def _monitor_backlight_lock_state(self) -> None:
        while True:
            self._check_backlight_lock_state()
            await asyncio.sleep(LOCK_UI_POLL_SECONDS)

    def _check_backlight_lock_state(self) -> None:
        if not self.config.lock_enabled:
            return

        screen_off = self._read_backlight_off()
        if screen_off is None:
            return

        previous = self.last_backlight_off
        self.last_backlight_off = screen_off

        if screen_off:
            self.lock_screen_off_sent = True
            if previous is not True:
                self.keyboard_backlight.lock()
            self.lock_controller.set_locked(
                True,
                run_command=False,
                reason="backlight off",
            )
            return

        if previous is True and screen_off is False:
            self.keyboard_backlight.unlock()

        if previous is not None and self.lock_controller.locked:
            self.lock_screen_off_sent = False
            if previous is True and screen_off is False:
                if time.monotonic() <= self.lock_controller.manual_unlock_pending_until:
                    self.lock_controller.manual_unlock_pending_until = 0.0
                    self.lock_controller.force_unlock(reason="manual wake")
                    return
                self.lock_controller.note_lock_activity()
                self.lock_progress = 0.0
                self.lock_progress_visible = False
                self._write_lock_popup_text()
                self._sync_lock_popup()
            LOGGER.debug("screen is on while mapper lock remains active")

    def _read_backlight_off(self) -> bool | None:
        try:
            return self.backlight_power_path.read_text(encoding="utf-8").strip() == "4"
        except OSError as exc:
            LOGGER.debug("unable to read backlight power state %s: %s", self.backlight_power_path, exc)
            return None

    def _prune_tasks(self) -> None:
        finished = [path for path, entry in self.tasks.items() if entry.task.done()]
        for path in finished:
            entry = self.tasks.pop(path)
            try:
                entry.task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                LOGGER.exception("device task failed for %s: %s", path, exc)
                if entry.role == "keyboard":
                    raise RuntimeError(f"keyboard watcher crashed for {path}") from exc
            else:
                if entry.role == "keyboard":
                    raise RuntimeError(f"keyboard watcher exited unexpectedly for {path}")

    def _scan_devices(self) -> None:
        active_paths = set(self.tasks)
        for path in list_devices():
            if path in active_paths:
                continue
            try:
                device = InputDevice(path)
            except OSError:
                continue

            role = self._detect_role(device)
            if role == "gamepad":
                task = asyncio.create_task(
                    GamepadWatcher(device, self.config, self.runner, self.lock_controller).run()
                )
                self.tasks[path] = DeviceTask(role=role, task=task)
            elif role == "power_button":
                if self.virtual_power_button is None:
                    device.close()
                    continue
                task = asyncio.create_task(
                    PowerButtonWatcher(
                        device,
                        self.config,
                        self.lock_controller,
                        self.virtual_power_button,
                        self._read_backlight_off,
                    ).run()
                )
                self.tasks[path] = DeviceTask(role=role, task=task)
            elif role == "keyboard":
                task = asyncio.create_task(
                    KeyboardWatcher(device, self.config, self.runner, self.lock_controller, self.virtual_mouse).run()
                )
                self.tasks[path] = DeviceTask(role=role, task=task)
            elif role == "mouse":
                if self.virtual_mouse is None:
                    device.close()
                    continue
                task = asyncio.create_task(
                    MouseWatcher(device, self.config, self.virtual_mouse, self.lock_controller).run()
                )
                self.tasks[path] = DeviceTask(role=role, task=task)
            else:
                device.close()

    def _check_session_watch(self) -> None:
        if not self.config.keyboard_grab:
            return
        if not self.config.session_watch_processes:
            return

        now = time.monotonic()
        for raw_pattern in self.config.session_watch_processes:
            pattern = raw_pattern.strip().lower()
            if not pattern:
                continue
            current = self._matching_process_pids(pattern)
            baseline = self.session_watch_baselines.get(pattern)
            if baseline is None:
                if current:
                    self.session_watch_baselines[pattern] = current
                continue

            if current == baseline:
                self.session_watch_pending.pop(pattern, None)
                continue

            pending = self.session_watch_pending.get(pattern)
            if pending is None or pending[0] != current:
                LOGGER.warning(
                    "session watchdog: observed %s process change %s -> %s; waiting %sms before recovery",
                    pattern,
                    sorted(baseline),
                    sorted(current),
                    self.config.session_watch_settle_ms,
                )
                self.session_watch_pending[pattern] = (current, now)
                continue

            if (now - pending[1]) * 1000 < self.config.session_watch_settle_ms:
                continue

            raise RuntimeError(
                f"session process changed for {pattern}: {sorted(baseline)} -> {sorted(current)}"
            )

    def _matching_process_pids(self, pattern: str) -> frozenset[int]:
        matches: set[int] = set()
        proc_root = Path("/proc")
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                comm = (entry / "comm").read_text(encoding="utf-8", errors="ignore").strip().lower()
                if pattern in comm:
                    matches.add(pid)
                    continue
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="ignore").lower()
                if pattern in cmdline:
                    matches.add(pid)
            except OSError:
                continue
        return frozenset(matches)

    def _detect_role(self, device: InputDevice) -> str | None:
        if is_ignored_device(device.name):
            return None

        try:
            caps = device.capabilities()
        except OSError:
            return None

        key_caps = set(caps.get(ecodes.EV_KEY, []))
        rel_caps = set(caps.get(ecodes.EV_REL, []))

        if (
            match_any(device.name, self.config.gamepad_patterns)
            and any(code in key_caps for code in self._binding_codes())
        ):
            return "gamepad"

        if (
            self.config.keyboard_enabled
            and match_any(device.name, self.config.keyboard_patterns)
            and any(code in key_caps for code in self._keyboard_binding_codes())
        ):
            return "keyboard"

        if self.config.mouse_enabled and self._is_mouse(device.name, key_caps, rel_caps):
            return "mouse"

        if (
            self.config.power_button_enabled
            and match_any(device.name, self.config.power_button_patterns)
            and ecodes.KEY_POWER in key_caps
        ):
            return "power_button"

        return None

    def _binding_codes(self) -> set[int]:
        codes: set[int] = set()
        for binding in self.config.gamepad_bindings:
            codes.update(binding.buttons)
        return codes

    def _keyboard_binding_codes(self) -> set[int]:
        codes: set[int] = set()
        for binding in self.config.keyboard_bindings:
            codes.update(binding.buttons)
        if self.config.lock_enabled:
            codes.add(self.config.lock_key)
        return codes

    def _is_mouse(self, name: str, key_caps: set[int], rel_caps: set[int]) -> bool:
        if ecodes.REL_X not in rel_caps or ecodes.REL_Y not in rel_caps:
            return False
        if ecodes.BTN_MIDDLE not in key_caps:
            return False
        return match_any(name, self.config.mouse_patterns)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


async def main_async() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = args.config.expanduser()
    if not config_path.exists():
        LOGGER.error("config not found: %s", config_path)
        return 1

    config = load_config(config_path)
    if config.keyboard_enabled and config.keyboard_grab:
        LOGGER.warning(
            "legacy keyboard grab mode is enabled; prefer keyd plus labwc so normal typing stays off the mapper path"
        )
    daemon = MapperDaemon(config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    runner_task = asyncio.create_task(daemon.run())
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait({runner_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()
    await daemon.shutdown()
    await asyncio.gather(*pending, return_exceptions=True)

    if runner_task in done:
        return runner_task.result() or 0
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
