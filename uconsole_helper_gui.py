#!/usr/bin/python3
"""GTK desktop GUI for running a local DHCP server on one interface."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

import array
import fcntl
import ipaddress
import json as json_module
import math
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import select
import termios
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html import escape
from pathlib import Path

from gi.repository import Gdk, GLib, Gtk, Pango


APP_DIR = Path(__file__).resolve().parent
HELPER = APP_DIR / "uconsole_helper_dhcp.py"
SYS_NET = Path("/sys/class/net")
LEASE_FILE = Path("/tmp/uconsole-helper/dhcp/dnsmasq.leases")
DHCP_CONFIG_FILE = Path("/tmp/uconsole-helper/dhcp/dnsmasq.conf")
SYSTEM_SERVICE = "uconsole-helper.service"
SERVICE_CONFIG = Path("/etc/uconsole-helper/uconsole-helper.conf")
MAPPER_USER_SERVICE = "uconsole-helper-mapper.service"
MAPPER_CONFIG = Path.home() / ".config/uconsole-helper-mapper/config.toml"
MAPPER_DESKTOP_KEYBINDS_CONFIG = Path.home() / ".config/uconsole-helper-mapper/desktop-keybinds.toml"
MAPPER_ASR_CONFIG = Path.home() / ".config/uconsole-helper-mapper/voice.env"
MAPPER_GLOSSARY_FILE = Path.home() / ".config/uconsole-helper-mapper/voice-glossary.txt"
MCU_SHARED_SAMPLE_FILE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "uconsole-helper-mcu-latest.json"
BATTERY_CALIBRATE_PATH = Path("/sys/class/power_supply/axp20x-battery/calibrate")
DISPLAY_BACKLIGHT_BRIGHTNESS_PATH = Path("/sys/class/backlight/backlight@0/brightness")
DISPLAY_BACKLIGHT_MAX_PATH = Path("/sys/class/backlight/backlight@0/max_brightness")
SCREEN_TIMEOUT_OPTIONS = ("Default", "30s", "1min", "2min", "5min", "10min", "15min")
AUTO_SCREEN_TIMEOUT_OPTIONS = ("5s", "10s", "15s", "30s", "1min", "2min", "5min", "10min", "15min", "30min", "Never")
DHCP_LEASE_TIME_OPTIONS = ("1h", "2h", "4h", "8h", "12h", "24h", "48h")
APP_POWER_MIN_CPU_PERCENT = 1.0
APP_POWER_MIN_IO_BYTES_PER_SEC = 256 * 1024
APP_POWER_MIN_SCORE = 10.0
XIAO_USB_VENDOR = "2886"
XIAO_USB_PRODUCT_IDS = {"0065", "8044", "8065"}
XIAO_BOOTLOADER_PRODUCT_IDS = {"0065"}
XIAO_BOOTLOADER_HINTS = ("uf2", "bootloader", "mass storage")
XIAO_SERIAL_BAUD = 115200
XIAO_STILL_G_FORCE = 0.08
XIAO_PICKUP_G_FORCE = 0.12
XIAO_MOVE_G_FORCE = 0.18
XIAO_LONG_STILL_SECONDS = 30.0
XIAO_STILL_WINDOW_SECONDS = 2.5
XIAO_PUTDOWN_WINDOW_SECONDS = 3.8
XIAO_SAMPLE_MAX_AGE_SECONDS = 45.0
XIAO_STATUS_REQUEST_SECONDS = 5.0
XIAO_STATUS_STALE_SECONDS = 35.0
MCU_EVENT_LABELS = ("拿起", "放下", "支架")
MCU_FIRMWARE_EVENT_LABELS = {
    "ready": "",
    "requested": "",
    "heartbeat": "",
    "motion_started": "拿起",
    "state_changed": "状态变化",
    "pose_changed": "姿态变化",
    "pose_calibrated": "",
    "freefall": "姿态变化",
    "impact": "姿态变化",
}
MCU_POSE_LABELS = {
    "face_up": "正面朝上",
    "face_down": "正面朝下",
    "left_edge": "左侧立起",
    "right_edge": "右侧立起",
    "top_edge": "顶边立起",
    "bottom_edge": "底边立起",
    "stand": "支架模式",
    "tilted": "倾斜",
}
MCU_LIGHT_LEVELS = (
    ("night", "夜间", 2.0, 1),
    ("dim", "昏暗", 5.0, 2),
    ("low_indoor", "低亮室内", 9.0, 3),
    ("indoor", "室内", 30.0, 4),
    ("bright_indoor", "明亮室内", 120.0, 5),
    ("desk_light", "台灯/近光", 220.0, 6),
    ("shade", "户外阴影", 420.0, 7),
    ("bright_shade", "明亮户外", 900.0, 8),
    ("sunlight", "强光", None, 9),
)


DEFAULTS = {
    "server_ip": "192.168.50.1",
    "netmask": "255.255.255.0",
    "pool_start": "192.168.50.100",
    "pool_end": "192.168.50.200",
    "lease_time": "12h",
    "gateway": "",
    "dns": "",
}

DHCP_NETWORK_CANDIDATES = [
    "192.168.50.0/24",
    "192.168.60.0/24",
    "192.168.70.0/24",
    "192.168.80.0/24",
    "10.50.0.0/24",
    "10.60.0.0/24",
    "172.20.50.0/24",
]


@dataclass(frozen=True)
class InterfaceInfo:
    name: str
    supported: bool
    status: str
    reason: str = ""

    @property
    def label(self) -> str:
        if self.supported:
            return f"{self.name} ({self.status})"
        return f"{self.name} (不支持)"


@dataclass(frozen=True)
class McuTelemetrySample:
    timestamp: float
    ax: float
    ay: float
    az: float
    temperature: float | None = None
    light_lux: float | None = None
    light_raw: int | None = None
    light_screen: int | None = None
    light_keyboard: int | None = None
    light_ready: bool = False
    source: str = "serial"
    firmware_state: str = ""
    firmware_event: str = ""
    firmware_motion: str = ""
    firmware_pose: str = ""
    firmware_delta: float | None = None
    mic_peak: int | None = None
    mic_recent_peak: bool = False
    mic_ready: bool = False
    mic_enabled: bool = False
    mic_assist: bool | None = None
    gx: float | None = None
    gy: float | None = None
    gz: float | None = None


@dataclass(frozen=True)
class McuDeviceInfo:
    present: bool
    mode: str
    tty: str
    product: str
    manufacturer: str
    serial: str
    usb_path: str
    notes: str = ""


@dataclass(frozen=True)
class McuStateSnapshot:
    device: McuDeviceInfo
    state: str
    event: str
    motion: str
    still_for: float
    g_force: float
    tilt_deg: float
    sample_rate_hz: float
    pose: str
    light_lux: float | None
    smoothed_light_lux: float | None
    suggested_backlight: int | None
    light_raw: int | None
    light_ready: bool
    last_update: float
    raw_line: str
    last_error: str
    mic_ready: bool = False
    mic_enabled: bool = False
    mic_assist: bool | None = None
    mic_peak: int | None = None
    mic_recent_peak: bool = False
    recent_rows: tuple[tuple[str, str, str, str], ...] = ()


@dataclass
class McuTelemetryState:
    samples: list[McuTelemetrySample]
    prev_sample: McuTelemetrySample | None
    stable_since: float | None
    serial_session: "McuSerialSession | None"
    last_delta_g: float
    last_state: str
    last_event: str
    last_motion: str
    last_error: str
    serial_waiting: bool
    last_status_requested_at: float
    smoothed_light_lux: float | None
    suggested_backlight: int | None

    def __init__(self) -> None:
        self.samples = []
        self.prev_sample = None
        self.stable_since = None
        self.serial_session = None
        self.last_delta_g = 0.0
        self.last_state = "等待数据"
        self.last_event = "等待样本"
        self.last_motion = "-"
        self.last_error = ""
        self.serial_waiting = False
        self.last_status_requested_at = 0.0
        self.smoothed_light_lux = None
        self.suggested_backlight = None


@dataclass
class McuSerialSession:
    tty_name: str
    fd: int
    buffer: str = ""

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1

    def read_line(self, timeout: float = 0.0) -> str | None:
        if self.fd < 0:
            return None
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
            if not ready:
                return None
            chunk = os.read(self.fd, 4096)
        except OSError:
            self.close()
            return None
        if not chunk:
            return None
        self.buffer += chunk.decode("utf-8", errors="ignore")
        for sep in ("\n", "\r"):
            if sep in self.buffer:
                line, self.buffer = self.buffer.split(sep, 1)
                return line.strip()
        return None


class UConsoleHelperWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="uConsole Helper")
        self.set_default_size(920, 640)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.on_key_press)
        self.scan_running = False
        self.scan_cancel = threading.Event()
        self.dashboard_refresh_running = False
        self.dhcp_running = False
        self.dhcp_interface = ""
        self.tailscale_reconnecting = False
        self.mcu_refresh_running = False
        self.mcu_monitor_stop = threading.Event()
        self.mcu_latest_snapshot: McuStateSnapshot | None = None
        self.mcu_last_rendered: McuStateSnapshot | None = None
        self.mcu_event_store = Gtk.ListStore(str, str, str, str)
        self.mcu_event_labels: dict[str, Gtk.Label] = {}
        self.mcu_status_labels: dict[str, Gtk.Label] = {}
        self.mcu_summary_label = Gtk.Label(label="", xalign=0)
        self.mcu_summary_label.get_style_context().add_class("muted")
        self.mcu_action_label = Gtk.Label(label="", xalign=0)
        self.mcu_action_label.get_style_context().add_class("muted")
        self.mcu_hint_label = Gtk.Label(label="", xalign=0)
        self.mcu_hint_label.get_style_context().add_class("muted")
        self.mcu_pose_calibrate_button = Gtk.Button(label="Calibrate Pose")
        self.mcu_pose_calibrate_button.connect("clicked", lambda _button: self.calibrate_mcu_pose())
        self.mcu_mic_assist_switch = Gtk.Switch()
        self.mcu_mic_assist_switch.set_active(True)
        self.mcu_mic_assist_switch.connect("notify::active", self.on_mcu_mic_assist_changed)
        self.mcu_mic_assist_updating = False
        self.mcu_led_controls: dict[str, Gtk.Switch] = {
            "MCU_LED_BATTERY_ENABLED": Gtk.Switch(),
            "MCU_LED_LXTERMINAL_BELL_ENABLED": Gtk.Switch(),
            "MCU_LED_NIGHT_MODE_ENABLED": Gtk.Switch(),
        }
        for switch in self.mcu_led_controls.values():
            switch.connect("notify::active", self.on_mcu_led_config_changed)
        self.mcu_led_config_updating = False

        self.message_label = Gtk.Label(label="", xalign=0)
        self.dhcp_card: Gtk.Widget | None = None
        self.entries = {key: Gtk.Entry() for key in DEFAULTS}
        self.pool_prefix_labels: dict[str, Gtk.Label] = {}
        self.pool_suffix_entries: dict[str, Gtk.Entry] = {}
        self.lease_time_combo = combo_text_from_values(DHCP_LEASE_TIME_OPTIONS)
        self.dhcp_defaults = dhcp_defaults()
        for key, entry in self.entries.items():
            entry.set_text(self.dhcp_defaults[key])
        set_combo_text(self.lease_time_combo, self.dhcp_defaults["lease_time"])
        self.entries["server_ip"].connect("changed", lambda _entry: self.update_pool_address_controls())
        self.entries["netmask"].connect("changed", lambda _entry: self.update_pool_address_controls())

        self.scan_message_label = Gtk.Label(label="在上方 Interface 表格中选择网口后扫描同网段在线设备。", xalign=0)
        self.scan_store = Gtk.ListStore(str, str, str, str)
        self.interface_status_store = Gtk.ListStore(str, str, str, str, str, str, str)
        self.interface_tree: Gtk.TreeView | None = None
        self.tailscale_store = Gtk.ListStore(str, str, str, str, str, str, str, str, str, str, str)
        self.tailscale_summary_label = Gtk.Label(label="", xalign=0)
        self.tailscale_summary_label.get_style_context().add_class("muted")
        self.tailscale_netcheck_label = Gtk.Label(label="", xalign=0)
        self.tailscale_netcheck_label.get_style_context().add_class("muted")
        self.tailscale_netcheck_value_labels: dict[str, Gtk.Label] = {}
        self.tailscale_netcheck_running = False
        self.tailscale_netcheck_details: dict[str, str] = {}
        self.tailscale_latency_running = False
        self.tailscale_latency_cache: dict[str, str] = {}
        self.power_labels: dict[str, Gtk.Label] = {}
        self.power_controls: dict[str, Gtk.Widget] = {}
        self.power_control_rows: dict[str, tuple[Gtk.Widget, Gtk.Widget]] = {}
        self.power_profile_cards: dict[str, Gtk.Widget] = {}
        self.selected_power_mode = "balanced"
        self.app_power_store = Gtk.ListStore(str, str, str, str, str)
        self.app_power_previous: dict[int, dict[str, object]] = {}
        self.app_power_previous_time = 0.0
        self.mapper_desktop_store = Gtk.ListStore(str, str, str)
        self.mapper_binding_store = Gtk.ListStore(str, str, str)
        self.asr_controls: dict[str, Gtk.Widget] = {}
        self.asr_status_label = Gtk.Label(label="", xalign=0)
        self.asr_status_label.get_style_context().add_class("muted")
        self.utils_battery_label = Gtk.Label(label="-", xalign=0)
        self.utils_calibrate_button = Gtk.Button(label="Battery Calibrate")
        self.utils_calibrate_button.connect("clicked", lambda _button: self.calibrate_battery())
        self.utils_reset_xiao_usb_button = Gtk.Button(label="Reset USB2.0 HUB")
        self.utils_reset_xiao_usb_button.connect("clicked", lambda _button: self.reset_xiao_usb_hub())
        self.utils_usb_reset_running = False
        self.utils_usb_store = Gtk.ListStore(str, str, str, str, str)
        self.utils_status_label = Gtk.Label(label="", xalign=0)
        self.utils_status_label.get_style_context().add_class("muted")
        self.inline_panel_box: Gtk.Box | None = None
        self.inline_panel_title: Gtk.Label | None = None
        self.inline_panel_body: Gtk.Box | None = None
        self.inline_panel_close_button: Gtk.Button | None = None
        self.dashboard_labels: dict[str, Gtk.Label] = {}
        self.dashboard_bars: dict[str, Gtk.ProgressBar] = {}
        self.dashboard_secondary_bars: dict[str, Gtk.ProgressBar] = {}
        self.dashboard_cpu_sample: tuple[int, int] | None = None
        self.dashboard_net_sample: tuple[float, int, int] | None = None
        self.dashboard_grid: Gtk.Grid | None = None
        self.dashboard_cards: list[Gtk.Widget] = []
        self.dashboard_columns = 3
        self.mcu_page_ready = False
        self.gui_refresh_active = True
        self.current_page_name = "dashboard"

        self._build_ui()
        self.refresh_dashboard()
        self.refresh_dhcp_defaults()
        self.refresh_status()
        self.refresh_interface_status()
        self.refresh_tailscale_status()
        self.refresh_power_status()
        self.refresh_mcu_status()
        self.refresh_mapper_status()
        self.load_asr_config_controls()
        GLib.timeout_add_seconds(5, self.auto_refresh_visible_status)
        self.mcu_monitor_thread = threading.Thread(target=self._mcu_monitor_worker, daemon=True)
        self.mcu_monitor_thread.start()

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.get_style_context().add_class("app-root")
        self.add(root)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(140)
        self.stack.add_titled(self._build_dashboard_page(), "dashboard", "Dashboard")
        self.stack.add_titled(scrolled_page(self._build_lan_page()), "lan", "LAN")
        self.stack.add_titled(scrolled_page(self._build_tailscale_page()), "tailscale", "Tailscale")
        self.stack.add_titled(scrolled_page(self._build_power_page()), "power", "Power")
        self.stack.add_titled(scrolled_page(self._build_mcu_page()), "mcu", "MCU")
        self.stack.add_titled(scrolled_page(self._build_utils_page()), "utils", "Utils")
        self.stack.add_titled(scrolled_page(self._build_mapper_page()), "mapper", "Mapper")
        self.stack.add_titled(scrolled_page(self._build_asr_page()), "asr", "ASR")
        self.stack.connect("notify::visible-child-name", self.on_visible_page_changed)
        self.connect("window-state-event", self.on_window_state_event)
        self.connect("focus-in-event", self.on_focus_visibility_event)
        self.connect("focus-out-event", self.on_focus_visibility_event)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.get_style_context().add_class("topbar")
        root.pack_start(header, False, False, 0)

        tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tabs.get_style_context().add_class("app-tabs")
        header.pack_start(tabs, False, False, 0)
        self.dashboard_tab = underlined_button("Dash", "h")
        self.dashboard_tab.connect("clicked", lambda _button: self.set_tab("dashboard"))
        self.dashboard_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.dashboard_tab, False, False, 0)

        self.lan_tab_label = Gtk.Label()
        self.lan_tab_label.set_use_markup(True)
        self.lan_tab_label.set_markup(lan_tab_markup(False))
        self.lan_tab = Gtk.Button()
        self.lan_tab.add(self.lan_tab_label)
        self.lan_tab.connect("clicked", lambda _button: self.set_tab("lan"))
        self.lan_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.lan_tab, False, False, 0)

        self.tailscale_tab_label = Gtk.Label()
        self.tailscale_tab_label.set_use_markup(True)
        self.tailscale_tab = Gtk.Button()
        self.tailscale_tab.add(self.tailscale_tab_label)
        self.tailscale_tab.connect("clicked", lambda _button: self.set_tab("tailscale"))
        self.tailscale_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.tailscale_tab, False, False, 0)

        self.power_tab_label = Gtk.Label()
        self.power_tab_label.set_use_markup(True)
        self.power_tab = Gtk.Button()
        self.power_tab.add(self.power_tab_label)
        self.power_tab.connect("clicked", lambda _button: self.set_tab("power"))
        self.power_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.power_tab, False, False, 0)

        self.mcu_tab = underlined_button("MCU", "C")
        self.mcu_tab.connect("clicked", lambda _button: self.set_tab("mcu"))
        self.mcu_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.mcu_tab, False, False, 0)

        self.utils_tab = underlined_button("Utils", "U")
        self.utils_tab.connect("clicked", lambda _button: self.set_tab("utils"))
        self.utils_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.utils_tab, False, False, 0)

        self.mapper_tab_label = Gtk.Label()
        self.mapper_tab_label.set_use_markup(True)
        self.mapper_tab = Gtk.Button()
        self.mapper_tab.add(self.mapper_tab_label)
        self.mapper_tab.connect("clicked", lambda _button: self.set_tab("mapper"))
        self.mapper_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.mapper_tab, False, False, 0)

        self.asr_tab = underlined_button("ASR", "A")
        self.asr_tab.connect("clicked", lambda _button: self.set_tab("asr"))
        self.asr_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.asr_tab, False, False, 0)

        spacer = Gtk.Box()
        header.pack_start(spacer, True, True, 0)
        self.context_action_button = underlined_button("Start", "S")
        self.context_action_button.connect("clicked", lambda _button: self.run_context_action())
        self.context_action_button.get_style_context().add_class("context-action")
        header.pack_start(self.context_action_button, False, False, 0)
        self.tailscale_reconnect_button = underlined_button("Reconnect", "C")
        self.tailscale_reconnect_button.connect("clicked", lambda _button: self.run_secondary_header_action())
        self.tailscale_reconnect_button.get_style_context().add_class("context-action")
        self.tailscale_reconnect_button.get_style_context().add_class("action-ready")
        header.pack_start(self.tailscale_reconnect_button, False, False, 0)
        self.header_refresh_button = underlined_button("Refresh", "R")
        self.header_refresh_button.connect("clicked", lambda _button: self.run_refresh_action())
        self.header_refresh_button.get_style_context().add_class("context-action")
        self.header_refresh_button.get_style_context().add_class("action-ready")
        header.pack_start(self.header_refresh_button, False, False, 0)

        self.inline_panel_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.inline_panel_box.get_style_context().add_class("inline-panel")
        panel_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.inline_panel_title = Gtk.Label(label="", xalign=0)
        self.inline_panel_title.get_style_context().add_class("inline-panel-title")
        panel_header.pack_start(self.inline_panel_title, True, True, 0)
        close_panel_button = Gtk.Button(label="Close")
        close_panel_button.get_style_context().add_class("inline-panel-close")
        close_panel_button.connect("clicked", lambda _button: self.hide_inline_panel())
        panel_header.pack_start(close_panel_button, False, False, 0)
        self.inline_panel_close_button = close_panel_button
        self.inline_panel_box.pack_start(panel_header, False, False, 0)
        self.inline_panel_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.inline_panel_box.pack_start(self.inline_panel_body, False, False, 0)
        self.inline_panel_box.set_no_show_all(True)
        self.inline_panel_box.hide()
        root.pack_start(self.inline_panel_box, False, False, 0)

        root.pack_start(self.stack, True, True, 0)

        self._install_css()
        self.update_header()

    def _build_dashboard_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        page.get_style_context().add_class("page")

        grid = Gtk.Grid(column_spacing=5, row_spacing=4)
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(True)
        grid.set_hexpand(True)
        grid.connect("size-allocate", self.on_dashboard_size_allocate)
        self.dashboard_grid = grid
        page.pack_start(grid, True, True, 0)

        cards = [
            ("system", "System"),
            ("power", "Power"),
            ("cpu", "CPU"),
            ("memory", "Memory"),
            ("storage", "Storage"),
            ("network", "Network"),
            ("cellular", "Cellular"),
        ]
        for key, title in cards:
            card = dashboard_card(title)
            label = Gtk.Label(label="-", xalign=0, yalign=0)
            label.set_line_wrap(True)
            label.set_line_wrap_mode(Pango.WrapMode.CHAR)
            label.set_selectable(True)
            label.set_hexpand(True)
            label.set_max_width_chars(20)
            label.get_style_context().add_class("dashboard-value")
            bar = Gtk.ProgressBar()
            bar.set_show_text(True)
            bar.get_style_context().add_class("dashboard-meter")
            if key == "network":
                meter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                meter_row.pack_start(bar, True, True, 0)
                secondary_bar = Gtk.ProgressBar()
                secondary_bar.set_show_text(True)
                secondary_bar.get_style_context().add_class("dashboard-meter")
                meter_row.pack_start(secondary_bar, True, True, 0)
                card.pack_start(meter_row, False, False, 1)
                self.dashboard_secondary_bars[key] = secondary_bar
            else:
                card.pack_start(bar, False, False, 1)
            card.pack_start(label, True, True, 1)
            self.dashboard_bars[key] = bar
            self.dashboard_labels[key] = label
            card.set_hexpand(True)
            card.set_vexpand(True)
            self.dashboard_cards.append(card)
            index = len(self.dashboard_cards) - 1
            grid.attach(card, index % 3, index // 3, 1, 1)
        self.dashboard_columns = 3

        return page

    def _build_dhcp_config_card(self) -> Gtk.Widget:
        config_card = card_box()
        self.dhcp_card = config_card

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        config_card.pack_start(grid, False, False, 0)

        self._attach_entry(grid, "本机地址", "server_ip", 0, 0)
        self._attach_entry(grid, "子网掩码", "netmask", 2, 0)
        self._attach_pool_address(grid, "地址池起始", "pool_start", 0, 1)
        self._attach_pool_address(grid, "地址池结束", "pool_end", 2, 1)
        self._attach_combo(grid, "租约时间", self.lease_time_combo, 0, 2)
        self._attach_entry(grid, "网关(可选)", "gateway", 2, 2)
        self.update_pool_address_controls(self.dhcp_defaults)

        return config_card

    def _build_lan_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.get_style_context().add_class("page")

        interface_card = card_box()
        page.pack_start(interface_card, False, False, 0)

        self.interface_tree = Gtk.TreeView(model=self.interface_status_store)
        self.interface_tree.set_headers_visible(True)
        self.interface_tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        for index, title in enumerate(["设备", "类型", "状态", "连接", "信号", "地址"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index, foreground=6)
            column.set_resizable(True)
            self.interface_tree.append_column(column)

        self.interface_tree.set_hexpand(True)
        self.interface_tree.set_vexpand(False)
        interface_card.pack_start(table_scroll(self.interface_tree, vexpand=False), False, False, 0)

        dhcp_card = self._build_dhcp_config_card()
        page.pack_start(dhcp_card, False, False, 4)
        self.message_label.get_style_context().add_class("muted")
        self.message_label.set_no_show_all(True)
        self.message_label.hide()
        page.pack_start(self.message_label, False, False, 0)

        results_card = card_box()
        page.pack_start(results_card, True, True, 4)
        tree = Gtk.TreeView(model=self.scan_store)
        tree.set_headers_visible(True)
        for index, title in enumerate(["IP", "MAC", "状态", "主机名"]):
            column = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=index)
            column.set_resizable(True)
            tree.append_column(column)

        scroll = table_scroll(tree, vexpand=True)
        results_card.pack_start(scroll, True, True, 0)

        self.scan_message_label.get_style_context().add_class("muted")
        page.pack_start(self.scan_message_label, False, False, 0)
        return page

    def _build_tailscale_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.get_style_context().add_class("page")

        summary_card = card_box()
        page.pack_start(summary_card, False, False, 0)
        self.tailscale_summary_label.set_hexpand(True)
        self.tailscale_summary_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.tailscale_summary_label.set_single_line_mode(True)
        summary_card.pack_start(self.tailscale_summary_label, False, False, 0)

        netcheck_grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        netcheck_grid.set_margin_top(8)
        summary_card.pack_start(netcheck_grid, False, False, 0)
        netcheck_rows = [
            ("udp", "UDP"),
            ("ipv4", "IPv4"),
            ("ipv6", "IPv6"),
            ("derp", "DERP"),
            ("nat", "NAT"),
            ("portmap", "Portmap"),
            ("global", "Global"),
            ("portal", "Portal"),
        ]
        for index, (key, title) in enumerate(netcheck_rows):
            column = (index % 4) * 2
            row = index // 4
            title_label = Gtk.Label(label=title, xalign=0)
            title_label.get_style_context().add_class("muted")
            value_label = Gtk.Label(label="-", xalign=0)
            value_label.set_ellipsize(Pango.EllipsizeMode.END)
            value_label.set_hexpand(True)
            self.tailscale_netcheck_value_labels[key] = value_label
            netcheck_grid.attach(title_label, column, row, 1, 1)
            netcheck_grid.attach(value_label, column + 1, row, 1, 1)

        devices_card = card_box()
        page.pack_start(devices_card, True, True, 0)
        self.tailscale_tree = Gtk.TreeView(model=self.tailscale_store)
        self.tailscale_tree.set_headers_visible(True)
        self.tailscale_tree.connect("button-press-event", self.on_tailscale_tree_button_press)
        for index, title in enumerate(["设备", "OS", "地址", "延迟", "状态", "最后在线", "出口节点"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index, foreground=7)
            column.set_resizable(True)
            self.tailscale_tree.append_column(column)

        scroll = table_scroll(self.tailscale_tree, vexpand=True)
        devices_card.pack_start(scroll, True, True, 0)
        return page

    def _build_power_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.get_style_context().add_class("page")

        status_card = card_box()
        page.pack_start(status_card, False, False, 0)
        status_grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        status_card.pack_start(status_grid, False, False, 0)

        rows = [
            ("time", "Time"),
            ("watts", "Watts"),
            ("sleep", "Sleep"),
            ("power", "Power"),
            ("cpu_freq", "CPU Freq"),
            ("cpu", "CPU"),
            ("wwan", "WWAN"),
        ]
        status_columns = 3
        for index, (key, title) in enumerate(rows):
            column = (index % status_columns) * 2
            row = index // status_columns
            title_label = Gtk.Label(label=title, xalign=0)
            title_label.get_style_context().add_class("muted")
            value_label = Gtk.Label(label="-", xalign=0)
            value_label.set_selectable(True)
            value_label.set_hexpand(True)
            status_grid.attach(title_label, column, row, 1, 1)
            status_grid.attach(value_label, column + 1, row, 1, 1)
            self.power_labels[key] = value_label

        app_power_card = card_box()
        page.pack_start(app_power_card, True, True, 0)
        app_power_header = Gtk.Label(label="Power Ranking", xalign=0)
        app_power_header.get_style_context().add_class("muted")
        app_power_card.pack_start(app_power_header, False, False, 0)
        app_power_tree = Gtk.TreeView(model=self.app_power_store)
        app_power_tree.set_headers_visible(True)
        for index, title in enumerate(["App", "PID", "CPU", "IO/s", "Score"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            app_power_tree.append_column(column)
        app_power_tree.set_hexpand(True)
        app_power_tree.set_vexpand(False)
        app_power_card.pack_start(table_scroll(app_power_tree, vexpand=False), False, False, 6)

        profiles_grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        page.pack_start(profiles_grid, False, False, 0)
        for column, (profile, title) in enumerate((("ECO", "Eco"), ("BALANCED", "Balanced"), ("PERFORMANCE", "Performance"))):
            mode = profile.lower()
            profile_event = Gtk.EventBox()
            profile_event.set_visible_window(False)
            profile_event.connect("button-press-event", lambda _widget, _event, value=mode: self.set_power_mode(value))
            profile_card = card_box()
            profile_card.set_hexpand(True)
            profile_card.get_style_context().add_class("power-profile-card")
            profile_event.add(profile_card)
            profiles_grid.attach(profile_event, column, 0, 1, 1)
            self.power_profile_cards[mode] = profile_card

            header = Gtk.Label(label=f" {title.upper()} ", xalign=0)
            header.get_style_context().add_class("dashboard-title")
            profile_card.pack_start(header, False, False, 0)

            profile_grid = Gtk.Grid(column_spacing=14, row_spacing=10)
            profile_card.pack_start(profile_grid, False, False, 10)
            battery_freq = combo_text_from_values(("1500,1500", "1800,1800", "1500,2400"))
            self.power_controls[f"POWERSAVER_{profile}_BATTERY_CPU_FREQ"] = battery_freq
            self._attach_power_control(profile_grid, f"POWERSAVER_{profile}_BATTERY_CPU_FREQ", "Battery MHz", battery_freq, 0)
            ac_freq = combo_text_from_values(("1500,1500", "1500,1800", "1500,2000", "1500,2400"))
            self.power_controls[f"POWERSAVER_{profile}_AC_CPU_FREQ"] = ac_freq
            self._attach_power_control(profile_grid, f"POWERSAVER_{profile}_AC_CPU_FREQ", "AC MHz", ac_freq, 1)
            unknown_action = combo_text_from_values(("AC", "Battery", "Keep"))
            self.power_controls[f"POWERSAVER_{profile}_UNKNOWN_POWER_ACTION"] = unknown_action
            self._attach_power_control(profile_grid, f"POWERSAVER_{profile}_UNKNOWN_POWER_ACTION", "Unknown", unknown_action, 2)
            wwan_policy = combo_text_from_values(("ondemand", "keep", "off"))
            self.power_controls[f"POWERSAVER_{profile}_WWAN_POLICY"] = wwan_policy
            self._attach_power_control(profile_grid, f"POWERSAVER_{profile}_WWAN_POLICY", "WWAN", wwan_policy, 3)
            battery_screen_timeout = combo_text_from_values(SCREEN_TIMEOUT_OPTIONS)
            self.power_controls[f"POWERSAVER_{profile}_BATTERY_SCREEN_TIMEOUT_SEC"] = battery_screen_timeout
            self._attach_power_control(profile_grid, f"POWERSAVER_{profile}_BATTERY_SCREEN_TIMEOUT_SEC", "Battery Screen", battery_screen_timeout, 4)
            ac_screen_timeout = combo_text_from_values(SCREEN_TIMEOUT_OPTIONS)
            self.power_controls[f"POWERSAVER_{profile}_AC_SCREEN_TIMEOUT_SEC"] = ac_screen_timeout
            self._attach_power_control(profile_grid, f"POWERSAVER_{profile}_AC_SCREEN_TIMEOUT_SEC", "AC Screen", ac_screen_timeout, 5)
            auto_brightness = Gtk.Switch()
            self.power_controls[f"POWERSAVER_{profile}_AUTO_BRIGHTNESS"] = auto_brightness
            self._attach_power_control(profile_grid, f"POWERSAVER_{profile}_AUTO_BRIGHTNESS", "Auto Bright", auto_brightness, 6)
            screen_mode = Gtk.Switch()
            self.power_controls[f"POWERSAVER_{profile}_SCREEN_MODE"] = screen_mode
            screen_mode.connect("notify::active", lambda _switch, _pspec, value=profile: self.sync_power_screen_mode_visibility(value))
            self._attach_power_control(profile_grid, f"POWERSAVER_{profile}_SCREEN_MODE", "Auto Power Save", screen_mode, 7)
            auto_battery = combo_text_from_values(AUTO_SCREEN_TIMEOUT_OPTIONS)
            self.power_controls[f"POWERSAVER_{profile}_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC"] = auto_battery
            self._attach_power_control(profile_grid, f"POWERSAVER_{profile}_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC", "Put Down (Battery)", auto_battery, 8)
            auto_ac = combo_text_from_values(AUTO_SCREEN_TIMEOUT_OPTIONS)
            self.power_controls[f"POWERSAVER_{profile}_AUTO_AC_PUTDOWN_TIMEOUT_SEC"] = auto_ac
            self._attach_power_control(profile_grid, f"POWERSAVER_{profile}_AUTO_AC_PUTDOWN_TIMEOUT_SEC", "Put Down (AC)", auto_ac, 9)
            stand_mode = Gtk.Switch()
            self.power_controls[f"POWERSAVER_{profile}_STAND_MODE"] = stand_mode
            self._attach_power_control(
                profile_grid,
                f"POWERSAVER_{profile}_STAND_MODE",
                "Bypass Stand",
                stand_mode,
                10,
                tooltip="Bypass put-down lock while the device is in stand pose.",
            )

        self.load_power_policy_controls()

        return page

    def _build_utils_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.get_style_context().add_class("page")

        card = card_box()
        page.pack_start(card, False, False, 0)

        header = Gtk.Label(label="Battery Calibration", xalign=0)
        header.get_style_context().add_class("muted")
        card.pack_start(header, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.set_margin_top(8)
        card.pack_start(row, False, False, 0)

        battery_title = Gtk.Label(label="Battery", xalign=0)
        battery_title.get_style_context().add_class("muted")
        row.pack_start(battery_title, False, False, 0)
        self.utils_battery_label.set_hexpand(True)
        row.pack_start(self.utils_battery_label, True, True, 0)

        self.utils_calibrate_button.get_style_context().add_class("suggested-action")
        row.pack_start(self.utils_calibrate_button, False, False, 0)

        usb_card = card_box()
        page.pack_start(usb_card, False, False, 0)

        usb_header = Gtk.Label(label="USB2.0 HUB", xalign=0)
        usb_header.get_style_context().add_class("muted")
        usb_card.pack_start(usb_header, False, False, 0)

        usb_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        usb_row.set_margin_top(8)
        usb_card.pack_start(usb_row, False, False, 0)

        usb_hint = Gtk.Label(label="Reset USB2.0 HUB when USB devices have descriptor errors.", xalign=0)
        usb_hint.set_line_wrap(True)
        usb_hint.set_hexpand(True)
        usb_hint.get_style_context().add_class("muted")
        usb_row.pack_start(usb_hint, True, True, 0)

        self.utils_reset_xiao_usb_button.set_tooltip_text("会短暂断开 USB2.0 HUB 下的设备；必要时会继续重置上一层 USB HUB。")
        usb_row.pack_start(self.utils_reset_xiao_usb_button, False, False, 0)

        usb_tree = Gtk.TreeView(model=self.utils_usb_store)
        usb_tree.set_headers_visible(True)
        for index, title in enumerate(("Hub", "Path", "Device", "ID", "Driver")):
            renderer = Gtk.CellRendererText()
            if index in {0, 2, 4}:
                renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            if index == 0:
                column.set_min_width(110)
            elif index == 1:
                column.set_min_width(80)
            elif index == 2:
                column.set_min_width(180)
            usb_tree.append_column(column)
        usb_scrolled = Gtk.ScrolledWindow()
        usb_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        usb_scrolled.set_min_content_height(118)
        usb_scrolled.add(usb_tree)
        usb_card.pack_start(usb_scrolled, False, False, 10)

        page.pack_start(self.utils_status_label, False, False, 0)
        self.refresh_utils_status()
        return page

    def _build_mcu_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.get_style_context().add_class("page")

        summary_card = card_box()
        page.pack_start(summary_card, False, False, 0)
        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        grid.set_hexpand(True)
        summary_card.pack_start(grid, False, False, 0)

        rows = [
            ("device", "Device"),
            ("mode", "Mode"),
            ("state", "State"),
            ("event", "Event"),
            ("motion", "Motion"),
            ("pose", "Pose"),
            ("mic", "Mic"),
            ("raw_imu", "6-Axis"),
            ("light", "Light"),
            ("updated", "Updated"),
            ("error", "Error"),
        ]
        for index, (key, title) in enumerate(rows):
            column = (index % 2) * 2
            row = index // 2
            label = Gtk.Label(label=title, xalign=0)
            label.get_style_context().add_class("muted")
            value = Gtk.Label(label="-", xalign=0)
            value.set_selectable(True)
            value.set_line_wrap(True)
            value.set_line_wrap_mode(Pango.WrapMode.CHAR)
            value.set_hexpand(True)
            grid.attach(label, column, row, 1, 1)
            grid.attach(value, column + 1, row, 1, 1)
            self.mcu_status_labels[key] = value

        summary_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        summary_row.set_margin_top(6)
        summary_card.pack_start(summary_row, False, False, 0)
        summary_row.pack_start(self.mcu_summary_label, False, False, 0)
        summary_row.pack_start(self.mcu_action_label, False, False, 0)
        summary_row.pack_start(self.mcu_hint_label, False, False, 0)
        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_row.set_margin_top(8)
        summary_card.pack_start(action_row, False, False, 0)
        self.mcu_pose_calibrate_button.get_style_context().add_class("suggested-action")
        action_row.pack_start(self.mcu_pose_calibrate_button, False, False, 0)
        mic_label = Gtk.Label(label="Mic Assist", xalign=0)
        mic_label.get_style_context().add_class("muted")
        action_row.pack_start(mic_label, False, False, 0)
        action_row.pack_start(self.mcu_mic_assist_switch, False, False, 0)

        led_card = card_box()
        page.pack_start(led_card, False, False, 0)
        led_header = Gtk.Label(label="LED Behavior", xalign=0)
        led_header.get_style_context().add_class("muted")
        led_card.pack_start(led_header, False, False, 0)
        led_grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        led_grid.set_margin_top(8)
        led_card.pack_start(led_grid, False, False, 0)
        led_rows = [
            ("MCU_LED_BATTERY_ENABLED", "Battery", "Show low battery, charging, and full status."),
            ("MCU_LED_LXTERMINAL_BELL_ENABLED", "LXTerminal Bell", "Flash on terminal bell from local shells, SSH, or remote tmux."),
            ("MCU_LED_NIGHT_MODE_ENABLED", "Night Mode", "Turn off LED indicators when ambient light is below 1 lux."),
        ]
        for row, (key, title, tooltip) in enumerate(led_rows):
            label = Gtk.Label(label=title, xalign=0)
            label.get_style_context().add_class("muted")
            label.set_tooltip_text(tooltip)
            switch = self.mcu_led_controls[key]
            switch.set_tooltip_text(tooltip)
            led_grid.attach(label, 0, row, 1, 1)
            led_grid.attach(switch, 1, row, 1, 1)

        self.load_mcu_led_controls()
        self.mcu_page_ready = True
        return page

    def _build_mapper_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.get_style_context().add_class("page")

        desktop_card = card_box()
        page.pack_start(desktop_card, False, False, 0)
        desktop_header = Gtk.Label(label="Desktop Shortcuts", xalign=0)
        desktop_header.get_style_context().add_class("muted")
        desktop_card.pack_start(desktop_header, False, False, 0)
        desktop_tree = self.editable_tree(
            self.mapper_desktop_store,
            [
                ("Scope", 0),
                ("Key", 1),
                ("Command / Action", 2),
            ],
        )
        desktop_scroll = table_scroll(desktop_tree, vexpand=False)
        desktop_card.pack_start(desktop_scroll, False, False, 6)
        desktop_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        desktop_card.pack_start(desktop_buttons, False, False, 0)
        add_button = Gtk.Button(label="Add")
        add_button.connect("clicked", lambda _button: self.add_mapper_desktop_shortcut())
        desktop_buttons.pack_start(add_button, False, False, 0)
        remove_button = Gtk.Button(label="Remove")
        remove_button.connect("clicked", lambda _button: self.remove_selected_tree_row(desktop_tree))
        desktop_buttons.pack_start(remove_button, False, False, 0)

        bindings_card = card_box()
        page.pack_start(bindings_card, False, False, 0)
        bindings_header = Gtk.Label(label="Mapper Bindings", xalign=0)
        bindings_header.get_style_context().add_class("muted")
        bindings_card.pack_start(bindings_header, False, False, 0)
        binding_tree = self.editable_tree(
            self.mapper_binding_store,
            [
                ("Device", 0),
                ("Buttons", 1),
                ("Action", 2),
            ],
        )
        binding_scroll = table_scroll(binding_tree, vexpand=False)
        bindings_card.pack_start(binding_scroll, False, False, 6)
        binding_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bindings_card.pack_start(binding_buttons, False, False, 0)
        add_binding_button = Gtk.Button(label="Add")
        add_binding_button.connect("clicked", lambda _button: self.add_mapper_binding())
        binding_buttons.pack_start(add_binding_button, False, False, 0)
        remove_binding_button = Gtk.Button(label="Remove")
        remove_binding_button.connect("clicked", lambda _button: self.remove_selected_tree_row(binding_tree, self.mapper_binding_store))
        binding_buttons.pack_start(remove_binding_button, False, False, 0)

        self.load_mapper_shortcuts()
        return page

    def _build_asr_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.get_style_context().add_class("page")

        config_card = card_box()
        page.pack_start(config_card, False, False, 0)
        config_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        config_box.set_hexpand(True)
        config_card.pack_start(config_box, False, False, 0)

        self.asr_controls["ASR_URL"] = Gtk.Entry()
        config_box.pack_start(asr_control_row("Endpoint", self.asr_controls["ASR_URL"]), False, False, 0)
        self.asr_controls["ASR_FINALIZE_TEXT_URL"] = Gtk.Entry()
        config_box.pack_start(asr_control_row("Finalize Endpoint", self.asr_controls["ASR_FINALIZE_TEXT_URL"]), False, False, 0)
        self.asr_controls["ASR_PREVIEW_WS_URL"] = Gtk.Entry()
        config_box.pack_start(asr_control_row("Preview WS", self.asr_controls["ASR_PREVIEW_WS_URL"]), False, False, 0)
        self.asr_controls["ASR_AUTH_TOKEN"] = Gtk.Entry()
        if isinstance(self.asr_controls["ASR_AUTH_TOKEN"], Gtk.Entry):
            self.asr_controls["ASR_AUTH_TOKEN"].set_visibility(False)
        config_box.pack_start(asr_control_row("Token", self.asr_controls["ASR_AUTH_TOKEN"]), False, False, 0)

        options_flow = Gtk.FlowBox()
        options_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        options_flow.set_min_children_per_line(1)
        options_flow.set_max_children_per_line(2)
        options_flow.set_column_spacing(14)
        options_flow.set_row_spacing(10)
        options_flow.set_homogeneous(True)
        config_box.pack_start(options_flow, False, False, 0)

        self.asr_controls["ASR_LANGUAGE"] = combo_text_from_values(("zh", "en", "ja", "auto"))
        self._attach_asr_flow_control(options_flow, "Language", self.asr_controls["ASR_LANGUAGE"])
        self.asr_controls["ASR_CORRECTION_MODE"] = combo_text_from_values(("auto", "on", "off"))
        self.asr_controls["ASR_CORRECTION_MODE"].connect("changed", lambda _combo: self.sync_asr_tmux_context_state())
        self._attach_asr_flow_control(options_flow, "Correction", self.asr_controls["ASR_CORRECTION_MODE"])
        stream_preview = Gtk.Switch()
        stream_preview.set_halign(Gtk.Align.START)
        self.asr_controls["VOICE_QWEN_ASR_STREAMING"] = stream_preview
        self._attach_asr_flow_control(options_flow, "Preview", stream_preview)
        self.asr_controls["VOICE_RECORDER"] = combo_text_from_values(("auto", "pw-record", "ffmpeg", "arecord"))
        self._attach_asr_flow_control(options_flow, "Recorder", self.asr_controls["VOICE_RECORDER"])
        self.asr_controls["VOICE_INPUT"] = ellipsized_combo_text_from_values(tuple(audio_input_options()), width_chars=28)
        self._attach_asr_flow_control(options_flow, "Input", self.asr_controls["VOICE_INPUT"])
        self.asr_controls["VOICE_OUTPUT_MODE"] = combo_text_from_values(
            ("paste", "type", "type_enter", "clipboard", "fcitx_commit")
        )
        self._attach_asr_flow_control(options_flow, "Output", self.asr_controls["VOICE_OUTPUT_MODE"])
        self.asr_controls["VOICE_TMUX_OUTPUT_MODE"] = combo_text_from_values(
            ("type", "paste", "type_enter", "clipboard", "fcitx_commit")
        )
        self._attach_asr_flow_control(options_flow, "Tmux Output", self.asr_controls["VOICE_TMUX_OUTPUT_MODE"])
        timeout_adjustment = Gtk.Adjustment(value=90, lower=3, upper=300, step_increment=1, page_increment=10, page_size=0)
        timeout_spin = Gtk.SpinButton(adjustment=timeout_adjustment, climb_rate=1, digits=0)
        timeout_spin.set_numeric(True)
        disable_spin_scroll(timeout_spin)
        self.asr_controls["ASR_TIMEOUT"] = timeout_spin
        self._attach_asr_flow_control(options_flow, "Request Timeout (s)", timeout_spin)
        attempt_timeout_adjustment = Gtk.Adjustment(value=75, lower=1, upper=180, step_increment=1, page_increment=5, page_size=0)
        attempt_timeout_spin = Gtk.SpinButton(adjustment=attempt_timeout_adjustment, climb_rate=1, digits=0)
        attempt_timeout_spin.set_numeric(True)
        disable_spin_scroll(attempt_timeout_spin)
        self.asr_controls["ASR_REQUEST_ATTEMPT_TIMEOUT"] = attempt_timeout_spin
        self._attach_asr_flow_control(options_flow, "Attempt Timeout (s)", attempt_timeout_spin)
        connect_timeout_adjustment = Gtk.Adjustment(value=2.0, lower=0.2, upper=10.0, step_increment=0.1, page_increment=1.0, page_size=0)
        connect_timeout_spin = Gtk.SpinButton(adjustment=connect_timeout_adjustment, climb_rate=0.1, digits=1)
        connect_timeout_spin.set_numeric(True)
        disable_spin_scroll(connect_timeout_spin)
        self.asr_controls["ASR_CONNECT_TIMEOUT"] = connect_timeout_spin
        self._attach_asr_flow_control(options_flow, "Connect Timeout (s)", connect_timeout_spin)
        retry_adjustment = Gtk.Adjustment(value=1, lower=1, upper=8, step_increment=1, page_increment=2, page_size=0)
        retry_spin = Gtk.SpinButton(adjustment=retry_adjustment, climb_rate=1, digits=0)
        retry_spin.set_numeric(True)
        disable_spin_scroll(retry_spin)
        self.asr_controls["ASR_RETRY_COUNT"] = retry_spin
        self._attach_asr_flow_control(options_flow, "Retries", retry_spin)
        final_wait_adjustment = Gtk.Adjustment(value=1.5, lower=0.5, upper=10, step_increment=0.5, page_increment=1, page_size=0)
        final_wait_spin = Gtk.SpinButton(adjustment=final_wait_adjustment, climb_rate=0.5, digits=1)
        final_wait_spin.set_numeric(True)
        disable_spin_scroll(final_wait_spin)
        self.asr_controls["ASR_PREVIEW_FINAL_WAIT_SECONDS"] = final_wait_spin
        self._attach_asr_flow_control(options_flow, "Final Wait (s)", final_wait_spin)
        preview_ws_timeout_adjustment = Gtk.Adjustment(value=2.0, lower=0.2, upper=30.0, step_increment=0.1, page_increment=1.0, page_size=0)
        preview_ws_timeout_spin = Gtk.SpinButton(adjustment=preview_ws_timeout_adjustment, climb_rate=0.1, digits=1)
        preview_ws_timeout_spin.set_numeric(True)
        disable_spin_scroll(preview_ws_timeout_spin)
        self.asr_controls["ASR_PREVIEW_WS_TIMEOUT"] = preview_ws_timeout_spin
        self._attach_asr_flow_control(options_flow, "Preview WS Timeout (s)", preview_ws_timeout_spin)
        self.asr_controls["VOICE_PASTE_BACKEND"] = combo_text_from_values(("uinput", "auto", "wtype"))
        self._attach_asr_flow_control(options_flow, "Paste Backend", self.asr_controls["VOICE_PASTE_BACKEND"])
        tmux_context = Gtk.Switch()
        tmux_context.set_halign(Gtk.Align.START)
        self.asr_controls["VOICE_TMUX_CONTEXT"] = tmux_context
        self._attach_asr_flow_control(options_flow, "Tmux Context", tmux_context)

        glossary_card = card_box()
        page.pack_start(glossary_card, True, True, 0)
        glossary_label = Gtk.Label(label="Glossary", xalign=0)
        glossary_label.get_style_context().add_class("muted")
        glossary_card.pack_start(glossary_label, False, False, 0)
        self.asr_glossary_view = Gtk.TextView()
        self.asr_glossary_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        glossary_scroll = Gtk.ScrolledWindow()
        glossary_scroll.set_hexpand(True)
        glossary_scroll.set_vexpand(True)
        glossary_scroll.add(self.asr_glossary_view)
        glossary_card.pack_start(glossary_scroll, True, True, 6)

        page.pack_start(self.asr_status_label, False, False, 0)

        return page

    def _attach_power_control(
        self,
        grid: Gtk.Grid,
        key: str,
        title: str,
        widget: Gtk.Widget,
        row: int,
        tooltip: str | None = None,
    ) -> None:
        label = Gtk.Label(label=title, xalign=0)
        label.get_style_context().add_class("muted")
        if tooltip:
            label.set_tooltip_text(tooltip)
            widget.set_tooltip_text(tooltip)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        self.power_control_rows[key] = (label, widget)

    def sync_power_screen_mode_visibility(self, profile: str) -> None:
        screen_mode = self.power_controls.get(f"POWERSAVER_{profile}_SCREEN_MODE")
        auto = isinstance(screen_mode, Gtk.Switch) and screen_mode.get_active()
        visibility = {
            f"POWERSAVER_{profile}_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC": auto,
            f"POWERSAVER_{profile}_AUTO_AC_PUTDOWN_TIMEOUT_SEC": auto,
            f"POWERSAVER_{profile}_STAND_MODE": auto,
        }
        for key, visible in visibility.items():
            row = self.power_control_rows.get(key)
            if row is None:
                continue
            label, widget = row
            label.set_visible(visible)
            widget.set_visible(visible)

    def _attach_asr_flow_control(self, flow: Gtk.FlowBox, title: str, widget: Gtk.Widget) -> None:
        row = asr_control_row(title, widget)
        row.set_size_request(260, -1)
        flow.add(row)

    def editable_tree(self, store: Gtk.ListStore, columns: list[tuple[str, int]]) -> Gtk.TreeView:
        tree = Gtk.TreeView(model=store)
        tree.set_headers_visible(True)
        for title, index in columns:
            renderer = Gtk.CellRendererText()
            renderer.set_property("editable", True)
            renderer.connect("edited", lambda _renderer, path, text, column=index: store.set_value(store.get_iter(path), column, text))
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            tree.append_column(column)
        return tree

    def add_mapper_desktop_shortcut(self) -> None:
        self.show_mapper_row_panel(
            "Add Desktop Shortcut",
            [
                ("Scope", "rightshift"),
                ("Key", "x"),
                ("Command / Action", "~/.local/bin/command"),
            ],
            self.mapper_desktop_store,
        )

    def add_mapper_binding(self) -> None:
        self.show_mapper_row_panel(
            "Add Mapper Binding",
            [
                ("Device", "gamepad"),
                ("Buttons", "BTN_THUMB"),
                ("Action", "command ~/.local/bin/command"),
            ],
            self.mapper_binding_store,
        )

    def show_mapper_row_panel(self, title: str, fields: list[tuple[str, str]], store: Gtk.ListStore) -> None:
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        entries: list[Gtk.Entry] = []
        for row, (label_text, default) in enumerate(fields):
            label = Gtk.Label(label=label_text, xalign=0)
            label.get_style_context().add_class("muted")
            entry = Gtk.Entry()
            entry.set_hexpand(True)
            entry.set_text(default)
            grid.attach(label, 0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)
            entries.append(entry)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda _button: self.hide_inline_panel())
        buttons.pack_start(cancel_button, False, False, 0)
        add_button = Gtk.Button(label="Add")
        add_button.get_style_context().add_class("suggested-action")
        add_button.connect("clicked", lambda _button: self.finish_mapper_row_panel(store, entries))
        buttons.pack_start(add_button, False, False, 0)
        self.show_inline_panel(title, [grid, buttons], show_close=False)
        if entries:
            entries[0].grab_focus()

    def finish_mapper_row_panel(self, store: Gtk.ListStore, entries: list[Gtk.Entry]) -> None:
        values = [entry.get_text().strip() for entry in entries]
        if not all(values):
            self.show_error("配置错误", "请填写所有字段。")
            return
        store.append(values)
        self.hide_inline_panel()

    def remove_selected_tree_row(self, tree: Gtk.TreeView, store: Gtk.ListStore | None = None) -> None:
        _model, tree_iter = tree.get_selection().get_selected()
        if tree_iter is not None:
            (store or self.mapper_desktop_store).remove(tree_iter)

    def load_mapper_shortcuts(self) -> None:
        self.mapper_desktop_store.clear()
        for row in desktop_shortcut_rows():
            self.mapper_desktop_store.append([row["scope"], row["key"], row["action"]])
        self.mapper_binding_store.clear()
        for row in mapper_binding_rows():
            self.mapper_binding_store.append([row["device"], row["buttons"], row["action"]])

    def save_mapper_desktop_shortcuts(self) -> bool:
        rows: list[dict[str, str]] = []
        for item in self.mapper_desktop_store:
            rows.append({"scope": item[0].strip(), "key": item[1].strip(), "action": item[2].strip()})
        try:
            MAPPER_DESKTOP_KEYBINDS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            MAPPER_DESKTOP_KEYBINDS_CONFIG.write_text(desktop_keybinds_text(rows), encoding="utf-8")
        except OSError as exc:
            self.show_error("Save shortcuts failed", str(exc))
            return False
        for command in (
            [sys.executable, str(Path.home() / ".local/share/uconsole-helper-mapper/generate_desktop_keybinds.py"), "--config", str(MAPPER_DESKTOP_KEYBINDS_CONFIG)],
            [sys.executable, str(Path.home() / ".local/share/uconsole-helper-mapper/sync_labwc_keybinds.py")],
        ):
            if not Path(command[1]).exists():
                continue
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            if result.returncode != 0:
                self.show_error("Apply shortcuts failed", combine_output(result) or "命令执行失败。")
                return False
        self.refresh_mapper_status()
        return True

    def save_mapper_bindings(self) -> bool:
        rows: list[dict[str, str]] = []
        for item in self.mapper_binding_store:
            rows.append({"device": item[0].strip(), "buttons": item[1].strip(), "action": item[2].strip()})
        try:
            MAPPER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            MAPPER_CONFIG.write_text(mapper_config_text(rows), encoding="utf-8")
        except OSError as exc:
            self.show_error("Save mapper bindings failed", str(exc))
            return False
        self.refresh_mapper_status()
        return True

    def save_mapper_all(self) -> bool:
        return self.save_mapper_desktop_shortcuts() and self.save_mapper_bindings()

    def on_tailscale_tree_button_press(self, tree: Gtk.TreeView, event: Gdk.EventButton) -> bool:
        if event.button != 3:
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        if hit is None:
            return False
        path, _column, _cell_x, _cell_y = hit
        tree.get_selection().select_path(path)
        tree_iter = self.tailscale_store.get_iter(path)
        menu = Gtk.Menu()
        for value in (
            self.tailscale_store[tree_iter][8],
            self.tailscale_store[tree_iter][9],
            self.tailscale_store[tree_iter][10],
        ):
            item = Gtk.MenuItem(label=value)
            item.set_sensitive(value != "-")
            item.connect("activate", lambda _item, text=value: copy_to_clipboard(text))
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _install_css(self) -> None:
        css = Gtk.CssProvider()
        css.load_from_data(
            b"""
            window { background: #121212; color: #f0f0f0; }
            .app-root { padding: 16px; background: #121212; }
            .topbar { padding: 0 0 2px 0; }
            .page { padding: 2px; }
            .section-title { font-size: 17px; font-weight: 700; color: #f2f2f2; }
            .dashboard-title {
                font-family: "SauceCode Pro Mono", monospace;
                font-size: 20px;
                font-weight: 700;
                color: #61d6d6;
                background: #111820;
                border: 1px solid #2f6f6d;
                border-radius: 6px;
                padding: 1px 6px;
            }
            .dashboard-value {
                font-family: "SauceCode Pro Mono", monospace;
                font-size: 19px;
                color: #d9f7ef;
            }
            .dashboard-meter {
                min-height: 6px;
            }
            .dashboard-meter trough {
                background: #080d10;
                border: 1px solid #24444a;
                border-radius: 6px;
            }
            .dashboard-meter progress {
                background: #2fdf84;
                border-radius: 6px;
            }
            .muted { color: #b7b7b7; }
            .card {
                background: #111418;
                border: 1px solid #2a5258;
                border-radius: 8px;
                padding: 5px;
            }
            .card.dhcp-running {
                background: #142a25;
                border-color: #39e58a;
            }
            .dashboard-card {
                padding: 3px;
            }
            .power-profile-card {
                background: #111418;
                border-color: #2a5258;
            }
            .power-profile-card.power-profile-selected {
                background: #153034;
                border-color: #61d6d6;
            }
            .tab-button,
            .context-action {
                min-height: 34px;
                padding: 6px 8px;
                border-radius: 12px;
                border: 1px solid #4a4a4a;
                background: #1e1e1e;
                color: #f0f0f0;
                font-weight: 700;
            }
            .tab-button.tab-active {
                background: #2f6f6d;
                border-color: #2f6f6d;
                color: #ffffff;
            }
            .tab-button:hover,
            .context-action:hover {
                border-color: #707070;
            }
            .context-action {
                background: #4a4b50;
                color: #ffffff;
                min-width: 72px;
            }
            .context-action.action-ready,
            .context-action.action-active {
                background: #2f6f6d;
                border-color: #2f6f6d;
            }
            .context-action.action-busy {
                background: #342a1d;
                border-color: #d68a24;
            }
            .context-action.action-danger {
                background: #8f2d2d;
                border-color: #e85b5b;
                color: #ffffff;
            }
            .context-action.action-success {
                background: #1f8f55;
                border-color: #39e58a;
                color: #ffffff;
            }
            .mcu-event-pill {
                background: #171b20;
                border: 1px solid #33464c;
                border-radius: 6px;
                color: #b7c8cc;
                padding: 4px 8px;
                font-weight: 700;
            }
            .mcu-event-pill.mcu-event-active {
                background: #2f6f6d;
                border-color: #61d6d6;
                color: #ffffff;
            }
            .inline-panel {
                background: #2a1717;
                border: 1px solid #b84a4a;
                border-radius: 8px;
                padding: 8px 10px;
            }
            .inline-panel-title {
                color: #ffd7d7;
                font-weight: 700;
            }
            .inline-panel-close {
                min-height: 28px;
                padding: 3px 8px;
            }
            button.suggested-action {
                background: #2f6f6d;
                border-color: #2f6f6d;
                color: #ffffff;
            }
            button {
                background: #2a2a2a;
                border-color: #4a4a4a;
                color: #f0f0f0;
            }
            switch.power-switch {
                color: transparent;
                text-shadow: none;
            }
            switch.power-switch:checked {
                background: #34e88a;
                border-color: #34e88a;
                color: transparent;
                text-shadow: none;
            }
            switch.power-switch:checked slider {
                background: #f7fff9;
                border-color: #f7fff9;
            }
            entry, combobox, textview {
                background: #171717;
                color: #f0f0f0;
                border-color: #4a4a4a;
            }
            treeview, scrolledwindow {
                background: #111418;
                color: #f0f0f0;
                border-color: #2a5258;
            }
            entry {
                border: 1px solid #4a4a4a;
            }
            treeview.view {
                background: #111418;
                color: #f0f0f0;
                border: 1px solid #2a5258;
            }
            treeview.view:selected {
                background: #2f6f6d;
            }
            treeview header button {
                background: #111820;
                border-color: #2a5258;
                color: #d9f7ef;
                font-weight: 700;
            }
            textview {
                font-family: monospace;
                border: 1px solid #4a4a4a;
            }
            """
        )
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def set_tab(self, name: str) -> None:
        if name == "dhcp":
            name = "lan"
        current = self.stack.get_visible_child_name()
        if current == name:
            return
        entering_power = name == "power"
        if entering_power:
            self.reset_app_power_ranking()
        self.stack.set_visible_child_name(name)
        self.refresh_page(name, reload_config=True)

    def update_header(self) -> None:
        page = self.stack.get_visible_child_name() or "dashboard"
        self.tailscale_tab_label.set_markup(tailscale_tab_markup())
        self.lan_tab_label.set_markup(lan_tab_markup(self.dhcp_running))
        self.power_tab_label.set_markup(power_tab_markup())
        toggle_style_class(self.mcu_tab, "tab-active", page == "mcu")
        self.mapper_tab_label.set_markup(mapper_tab_markup())
        toggle_style_class(self.dashboard_tab, "tab-active", page == "dashboard")
        toggle_style_class(self.lan_tab, "tab-active", page == "lan")
        toggle_style_class(self.tailscale_tab, "tab-active", page == "tailscale")
        toggle_style_class(self.power_tab, "tab-active", page == "power")
        toggle_style_class(self.utils_tab, "tab-active", page == "utils")
        toggle_style_class(self.mapper_tab, "tab-active", page == "mapper")
        toggle_style_class(self.asr_tab, "tab-active", page == "asr")
        self.tailscale_reconnect_button.set_visible(page in {"lan", "tailscale", "power", "mapper"})
        self.tailscale_reconnect_button.set_sensitive(page != "tailscale" or not self.tailscale_reconnecting)
        reconnect_context = self.tailscale_reconnect_button.get_style_context()
        for class_name in ("action-ready", "action-active", "action-busy", "action-danger"):
            reconnect_context.remove_class(class_name)
        if page == "lan":
            if self.dhcp_running:
                set_status_underlined_button_label(self.tailscale_reconnect_button, "Disable", "E", "#34c759")
                reconnect_context.add_class("action-danger")
            else:
                set_status_underlined_button_label(self.tailscale_reconnect_button, "DHCP", "D", "#8a8f98")
                reconnect_context.add_class("action-ready")
        elif page == "power":
            enabled = powersaver_enabled()
            set_underlined_button_label(self.tailscale_reconnect_button, "Disable" if enabled else "Enable", "E")
            reconnect_context.add_class("action-active" if enabled else "action-ready")
        elif page == "mapper":
            active = user_service_active(MAPPER_USER_SERVICE)
            set_underlined_button_label(self.tailscale_reconnect_button, "Disable" if active else "Enable", "E")
            reconnect_context.add_class("action-active" if active else "action-ready")
        elif self.tailscale_reconnecting:
            set_underlined_button_label(self.tailscale_reconnect_button, "Reconnecting", "E")
            reconnect_context.add_class("action-busy")
        else:
            set_underlined_button_label(self.tailscale_reconnect_button, "Reconnect", "E")
            reconnect_context.add_class("action-ready")
        action_context = self.context_action_button.get_style_context()
        for class_name in ("action-ready", "action-active", "action-busy"):
            action_context.remove_class(class_name)

        if page in {"power", "mapper", "asr"}:
            self.context_action_button.show()
            set_underlined_button_label(self.context_action_button, "Save", "V")
            action_context.add_class("action-ready")
            return

        if page == "mcu":
            self.context_action_button.show()
            set_underlined_button_label(self.context_action_button, "Bootloader", "B")
            action_context.add_class("action-ready")
            return

        if page == "lan" and self.scan_running:
            self.context_action_button.show()
            set_underlined_button_label(self.context_action_button, "Stop", "S")
            action_context.add_class("action-busy")
        elif page == "lan":
            self.context_action_button.show()
            set_underlined_button_label(self.context_action_button, "Scan", "S")
            action_context.add_class("action-ready")
        else:
            self.context_action_button.hide()
        self.update_dhcp_card_state()

    def update_dhcp_card_state(self) -> None:
        if self.dhcp_card is None:
            return
        toggle_style_class(self.dhcp_card, "dhcp-running", self.dhcp_running)

    def on_dashboard_size_allocate(self, _widget: Gtk.Widget, allocation: Gdk.Rectangle) -> None:
        if allocation.width >= 860 and allocation.height < 520:
            columns = 4
        elif allocation.width >= 680:
            columns = 3
        elif allocation.width >= 440:
            columns = 2
        else:
            columns = 1
        if columns != self.dashboard_columns:
            self.reflow_dashboard(columns)

    def reflow_dashboard(self, columns: int) -> None:
        if self.dashboard_grid is None:
            return
        for card in self.dashboard_cards:
            self.dashboard_grid.remove(card)
        for index, card in enumerate(self.dashboard_cards):
            self.dashboard_grid.attach(card, index % columns, index // columns, 1, 1)
        self.dashboard_columns = columns
        self.dashboard_grid.show_all()

    def run_secondary_header_action(self) -> None:
        page = self.stack.get_visible_child_name()
        if page == "lan":
            if self.dhcp_running:
                self.stop_server()
            else:
                self.start_server()
        elif page == "tailscale":
            self.reconnect_tailscale()
        elif page == "power":
            self.toggle_powersaver_enabled()
        elif page == "mapper":
            self.toggle_user_service(MAPPER_USER_SERVICE, "Mapper service")

    def run_context_action(self) -> None:
        page = self.stack.get_visible_child_name()
        if page == "tailscale":
            self.refresh_tailscale_status()
            return
        if page == "power":
            self.set_header_button_busy(self.context_action_button, "Saving", "V")
            try:
                saved = self.save_power_policy()
            except Exception as exc:
                self.show_error("Save policy failed", str(exc))
                saved = False
            if saved:
                self.flash_header_button(self.context_action_button, "Saved", "V")
            else:
                self.update_header()
            return
        if page == "mcu":
            self.trigger_mcu_bootloader()
            self.flash_header_button(self.context_action_button, "Sent", "B")
            return
        if page == "mapper":
            self.set_header_button_busy(self.context_action_button, "Saving", "V")
            if self.save_mapper_all():
                self.flash_header_button(self.context_action_button, "Saved", "V")
            else:
                self.update_header()
            return
        if page == "asr":
            self.set_header_button_busy(self.context_action_button, "Saving", "V")
            if self.save_asr_config():
                self.flash_header_button(self.context_action_button, "Saved", "V")
            else:
                self.update_header()
            return
        if page == "dashboard":
            self.refresh_dashboard()
            return

        if self.scan_running:
            self.stop_lan_scan()
        else:
            self.start_lan_scan()

    def run_refresh_action(self) -> None:
        if self.refresh_page(self.stack.get_visible_child_name(), reload_config=True):
            self.flash_header_button(self.header_refresh_button, "Refreshed", "R")

    def refresh_page(self, page: str | None, *, reload_config: bool = False) -> bool:
        if page == "dashboard":
            self.refresh_dashboard()
        elif page == "lan":
            self.refresh_dhcp_defaults()
            self.refresh_status()
        elif page == "tailscale":
            self.refresh_tailscale_status()
        elif page == "power":
            self.refresh_power_status(reload_config=reload_config)
        elif page == "utils":
            self.refresh_utils_status()
        elif page == "mapper":
            if reload_config:
                self.refresh_mapper_status()
            else:
                self.update_header()
        elif page == "asr":
            if reload_config:
                self.load_asr_config_controls()
        elif page == "mcu":
            self.refresh_mcu_status()
        else:
            return False
        return True

    def flash_header_button(self, button: Gtk.Button, text: str, key: str) -> None:
        context = button.get_style_context()
        for class_name in ("action-ready", "action-active", "action-busy"):
            context.remove_class(class_name)
        context.add_class("action-success")
        set_underlined_button_label(button, text, key)
        GLib.timeout_add(900, self.finish_header_button_flash, button)

    def set_header_button_busy(self, button: Gtk.Button, text: str, key: str) -> None:
        context = button.get_style_context()
        for class_name in ("action-ready", "action-active", "action-success"):
            context.remove_class(class_name)
        context.add_class("action-busy")
        set_underlined_button_label(button, text, key)
        while Gtk.events_pending():
            Gtk.main_iteration()

    def finish_header_button_flash(self, button: Gtk.Button) -> bool:
        button.get_style_context().remove_class("action-success")
        self.update_header()
        if button is self.header_refresh_button:
            set_underlined_button_label(self.header_refresh_button, "Refresh", "R")
            self.header_refresh_button.get_style_context().add_class("action-ready")
        return False

    def on_key_press(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        key = Gdk.keyval_name(event.keyval) or ""
        key_lower = key.lower()
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        if ctrl and key == "1":
            self.set_tab("dashboard")
            return True
        if ctrl and key == "2":
            self.set_tab("lan")
            return True
        if ctrl and key == "3":
            self.set_tab("tailscale")
            return True
        if ctrl and key == "4":
            self.set_tab("power")
            return True
        if ctrl and key == "5":
            self.set_tab("mcu")
            return True
        if ctrl and key == "6":
            self.set_tab("utils")
            return True
        if ctrl and key == "7":
            self.set_tab("mapper")
            return True
        if ctrl and key == "8":
            self.set_tab("asr")
            return True
        if alt and key in {"Left", "Right"}:
            self.switch_tab(-1 if key == "Left" else 1)
            return True
        if is_text_input_focus(self):
            return False
        if key_lower == "h":
            self.set_tab("dashboard")
            return True
        if key_lower == "l":
            self.set_tab("lan")
            return True
        if key_lower == "t":
            self.set_tab("tailscale")
            return True
        if key_lower == "p":
            self.set_tab("power")
            return True
        if key_lower == "u":
            self.set_tab("utils")
            return True
        if key_lower == "m":
            self.set_tab("mapper")
            return True
        if key_lower == "a":
            self.set_tab("asr")
            return True
        if key_lower == "r":
            self.run_refresh_action()
            return True
        if key_lower == "s" and self.stack.get_visible_child_name() == "lan":
            self.run_context_action()
            return True
        if key_lower == "v" and self.stack.get_visible_child_name() in {"power", "mapper", "asr"}:
            self.run_context_action()
            return True
        if key_lower == "e" and self.stack.get_visible_child_name() == "tailscale":
            self.run_secondary_header_action()
            return True
        if key_lower == "c":
            self.set_tab("mcu")
            return True
        if key in {"Return", "KP_Enter"}:
            self.run_context_action()
            return True
        return False

    def switch_tab(self, direction: int) -> None:
        pages = ["dashboard", "lan", "tailscale", "power", "mcu", "utils", "mapper", "asr"]
        current = self.stack.get_visible_child_name()
        try:
            index = pages.index(current)
        except ValueError:
            index = 0
        self.set_tab(pages[(index + direction) % len(pages)])

    def _attach_label(self, grid: Gtk.Grid, text: str, col: int, row: int) -> None:
        label = Gtk.Label(label=text, xalign=0)
        grid.attach(label, col, row, 1, 1)

    def _attach_entry(self, grid: Gtk.Grid, label: str, key: str, col: int, row: int) -> None:
        self._attach_label(grid, label, col, row)
        entry = self.entries[key]
        entry.set_hexpand(True)
        grid.attach(entry, col + 1, row, 1, 1)

    def _attach_combo(self, grid: Gtk.Grid, label: str, combo: Gtk.ComboBoxText, col: int, row: int) -> None:
        self._attach_label(grid, label, col, row)
        combo.set_hexpand(True)
        grid.attach(combo, col + 1, row, 1, 1)

    def _attach_pool_address(self, grid: Gtk.Grid, label: str, key: str, col: int, row: int) -> None:
        self._attach_label(grid, label, col, row)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_hexpand(True)
        prefix = Gtk.Label(label="", xalign=0)
        prefix.set_selectable(True)
        entry = Gtk.Entry()
        entry.set_width_chars(8)
        entry.set_max_width_chars(15)
        box.pack_start(prefix, True, True, 0)
        box.pack_start(entry, False, False, 0)
        grid.attach(box, col + 1, row, 1, 1)
        self.pool_prefix_labels[key] = prefix
        self.pool_suffix_entries[key] = entry

    def current_dhcp_form_values(self) -> dict[str, str]:
        values = {key: entry.get_text().strip() for key, entry in self.entries.items()}
        values["lease_time"] = self.lease_time_combo.get_active_text() or DEFAULTS["lease_time"]
        for key in ("pool_start", "pool_end"):
            values[key] = self.pool_address_text_or_default(key)
        return values

    def set_dhcp_config_values(self, values: dict[str, str]) -> None:
        for key, entry in self.entries.items():
            if key in {"pool_start", "pool_end", "lease_time"}:
                continue
            entry.set_text(values.get(key, DEFAULTS[key]))
        set_combo_text(self.lease_time_combo, values.get("lease_time", DEFAULTS["lease_time"]))
        self.update_pool_address_controls(values)

    def update_pool_address_controls(self, values: dict[str, str] | None = None) -> None:
        source = values or {
            "server_ip": self.entries["server_ip"].get_text().strip(),
            "netmask": self.entries["netmask"].get_text().strip(),
            "pool_start": self.pool_address_text_or_default("pool_start"),
            "pool_end": self.pool_address_text_or_default("pool_end"),
        }
        try:
            server_ip = ipaddress.IPv4Address(source["server_ip"])
            network = ipaddress.IPv4Network(f"{server_ip}/{source['netmask']}", strict=False)
            pool_bounds = dhcp_pool_bounds(network)
            if pool_bounds is None:
                raise ValueError
        except (KeyError, ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError):
            for key in ("pool_start", "pool_end"):
                self.pool_prefix_labels[key].set_text("请先设置有效本机地址和子网掩码")
                self.pool_suffix_entries[key].set_text("")
            return

        prefix, first_suffix, last_suffix = pool_edit_parts(network)
        first_pool_ip, last_pool_ip = pool_bounds
        for key in ("pool_start", "pool_end"):
            fallback = pool_bounds[0] if key == "pool_start" else pool_bounds[1]
            address = pool_address_from_value(source.get(key, ""), first_pool_ip, last_pool_ip, fallback)
            self.pool_prefix_labels[key].set_text(prefix)
            self.pool_suffix_entries[key].set_placeholder_text(f"{first_suffix}-{last_suffix}")
            self.pool_suffix_entries[key].set_text(address_suffix(address, prefix))

    def pool_address_text(self, key: str) -> str:
        server_ip = ipaddress.IPv4Address(self.entries["server_ip"].get_text().strip())
        network = ipaddress.IPv4Network(f"{server_ip}/{self.entries['netmask'].get_text().strip()}", strict=False)
        prefix, _first_suffix, _last_suffix = pool_edit_parts(network)
        suffix = self.pool_suffix_entries[key].get_text().strip()
        return prefix + suffix

    def pool_address_text_or_default(self, key: str) -> str:
        try:
            return self.pool_address_text(key)
        except ValueError:
            return self.dhcp_defaults.get(key, DEFAULTS[key])

    def refresh_dhcp_defaults(self) -> None:
        current_defaults = self.dhcp_defaults
        current_values = self.current_dhcp_form_values()
        if any(current_values.get(key, "") != current_defaults.get(key, "") for key in DEFAULTS):
            return
        self.dhcp_defaults = dhcp_defaults()
        self.set_dhcp_config_values(self.dhcp_defaults)

    def refresh_interface_status(self) -> None:
        selected_name = self.selected_scan_interface_name()
        self.interface_status_store.clear()
        wifi_signals = wifi_signal_by_device()
        modem_signals = modem_signal_by_port()
        hidden_modem_ports = hidden_duplicate_modem_ports()
        tailscale = tailscale_status()
        addresses = interface_addresses()

        for device in nmcli_device_status():
            if device["device"] == "lo" or device["device"] in hidden_modem_ports:
                continue
            signal = "-"
            connection = device["connection"] or "-"
            if device["type"] == "wifi":
                signal = wifi_signals.get(device["device"], "-")
            elif device["type"] in {"gsm", "cdma"} or device["device"] in modem_signals:
                modem_signal = modem_signals.get(device["device"], {})
                signal = modem_signal.get("signal", "-")
                if modem_signal.get("connected") and modem_signal.get("connection"):
                    connection = modem_signal["connection"]
            elif device["device"].startswith("tailscale") or device["type"] == "tun":
                signal = tailscale_summary(tailscale)
            signal = signal_with_bars(signal)

            state = display_nm_state(device["state"])
            if self.dhcp_running and device["device"] == self.dhcp_interface:
                state = "DHCP Server"
                connection = "dnsmasq"
            row_color = interface_row_color(state, signal)
            self.interface_status_store.append(
                [
                    device["device"],
                    device["type"],
                    state,
                    connection,
                    signal,
                    addresses.get(device["device"], "-"),
                    row_color,
                ]
            )
        self.select_scan_interface(selected_name or preferred_scan_interface(discover_scan_interfaces()))

    def refresh_tailscale_status(self) -> None:
        status = tailscale_status()
        self.tailscale_store.clear()
        if not status:
            self.tailscale_summary_label.set_text("Tailscale status unavailable")
            self.update_tailscale_netcheck_details({})
            return

        self.tailscale_summary_label.set_text(tailscale_network_summary())
        self.update_tailscale_netcheck_details(self.tailscale_netcheck_details)
        self.refresh_tailscale_netcheck_async()
        devices = tailscale_devices(status)
        for device in devices:
            latency = device["ping_latency"]
            if device["ping_target"] != "-":
                latency = self.tailscale_latency_cache.get(device["ping_target"], "...")
            self.tailscale_store.append(
                [
                    device["name"],
                    device["os"],
                    device["addresses"],
                    latency,
                    device["status"],
                    device["last_seen"],
                    device["exit_node"],
                    tailscale_row_color(device["status"]),
                    device["ipv4"],
                    device["ipv6"],
                    device["dns"],
                ]
            )
        self.refresh_tailscale_latency_async(devices)

    def refresh_tailscale_latency_async(self, devices: list[dict[str, str]]) -> None:
        if self.tailscale_latency_running:
            return
        targets = sorted(
            {
                device["ping_target"]
                for device in devices
                if device["ping_target"] != "-" and device["status"] in {"Online", "Active"}
            }
        )
        if not targets:
            return
        self.tailscale_latency_running = True
        thread = threading.Thread(target=self._tailscale_latency_worker, args=(targets,), daemon=True)
        thread.start()

    def _tailscale_latency_worker(self, targets: list[str]) -> None:
        latencies = tailscale_ping_latencies(targets)
        GLib.idle_add(self.finish_tailscale_latency_refresh, latencies)

    def finish_tailscale_latency_refresh(self, latencies: dict[str, str]) -> bool:
        self.tailscale_latency_running = False
        self.tailscale_latency_cache.update(latencies)
        if self.current_page_name == "tailscale":
            for row in self.tailscale_store:
                target = row[8]
                if target in latencies:
                    row[3] = latencies[target]
        return False

    def refresh_tailscale_netcheck_async(self) -> None:
        if self.tailscale_netcheck_running:
            return
        self.tailscale_netcheck_running = True
        thread = threading.Thread(target=self._tailscale_netcheck_worker, daemon=True)
        thread.start()

    def _tailscale_netcheck_worker(self) -> None:
        netcheck_details = tailscale_netcheck_details(tailscale_netcheck())
        GLib.idle_add(self.finish_tailscale_netcheck, netcheck_details)

    def finish_tailscale_netcheck(self, netcheck_details: dict[str, str]) -> bool:
        self.tailscale_netcheck_running = False
        self.tailscale_netcheck_details = netcheck_details
        if self.current_page_name == "tailscale":
            self.update_tailscale_netcheck_details(netcheck_details)
        return False

    def update_tailscale_netcheck_details(self, details: dict[str, str]) -> None:
        for key, label in self.tailscale_netcheck_value_labels.items():
            label.set_text(details.get(key) or "-")

    def reconnect_tailscale(self) -> None:
        if self.tailscale_reconnecting:
            return
        if shutil.which("tailscale") is None:
            self.show_error("缺少依赖", "未找到 tailscale 命令。")
            return
        self.tailscale_reconnecting = True
        self.tailscale_summary_label.set_text("Reconnecting Tailscale...")
        self.update_header()
        thread = threading.Thread(target=self._tailscale_reconnect_worker, daemon=True)
        thread.start()

    def _tailscale_reconnect_worker(self) -> None:
        commands = (["tailscale", "down"], ["tailscale", "up"])
        for index, command in enumerate(commands):
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            if result.returncode != 0:
                error = f"{' '.join(command)} failed:\n{combine_output(result) or '命令执行失败。'}"
                GLib.idle_add(self.finish_tailscale_reconnect, error)
                return
            if index == 0:
                time.sleep(2)
        GLib.idle_add(self.finish_tailscale_reconnect, None)

    def finish_tailscale_reconnect(self, error: str | None) -> bool:
        self.tailscale_reconnecting = False
        self.refresh_tailscale_status()
        self.update_header()
        if error:
            self.show_error("Tailscale reconnect failed", error)
        return False

    def refresh_dashboard(self) -> None:
        if self.dashboard_refresh_running:
            return
        self.dashboard_refresh_running = True
        if all((label.get_text() or "-") == "-" for label in self.dashboard_labels.values()):
            for label in self.dashboard_labels.values():
                label.set_text("Loading...")
        thread = threading.Thread(target=self._dashboard_refresh_worker, daemon=True)
        thread.start()

    def _dashboard_refresh_worker(self) -> None:
        try:
            cpu_sample = read_cpu_sample()
            cpu_percent = cpu_usage_percent(self.dashboard_cpu_sample, cpu_sample)
            net_sample = read_network_sample()
            net_rates = network_rates(self.dashboard_net_sample, net_sample)
            data = dashboard_status(cpu_percent=cpu_percent, net_rates=net_rates)
        except Exception as exc:
            GLib.idle_add(self.finish_dashboard_refresh_error, str(exc))
            return
        GLib.idle_add(self.finish_dashboard_refresh, data, cpu_sample, net_sample)

    def finish_dashboard_refresh_error(self, error: str) -> bool:
        self.dashboard_refresh_running = False
        self.show_error("Dashboard refresh failed", error)
        return False

    def finish_dashboard_refresh(
        self,
        data: dict[str, dict[str, object]],
        cpu_sample: dict[str, int],
        net_sample: dict[str, tuple[int, int]],
    ) -> bool:
        self.dashboard_cpu_sample = cpu_sample
        self.dashboard_net_sample = net_sample
        self.dashboard_refresh_running = False
        for key, label in self.dashboard_labels.items():
            item = data.get(key, {})
            if isinstance(item, dict):
                label.set_text(str(item.get("text") or "-"))
                bar = self.dashboard_bars.get(key)
                if bar is not None:
                    percent = max(0, min(100, int(item.get("percent", 0))))
                    hidden = bool(item.get("hide_meter"))
                    bar.set_visible(not hidden)
                    if not hidden:
                        bar.set_fraction(percent / 100)
                        bar.set_text(str(item.get("meter") or f"{percent}%"))
                secondary_bar = self.dashboard_secondary_bars.get(key)
                if secondary_bar is not None:
                    second_percent = max(0, min(100, int(item.get("second_percent", 0))))
                    secondary_bar.set_fraction(second_percent / 100)
                    secondary_bar.set_text(str(item.get("second_meter") or f"{second_percent}%"))
            else:
                label.set_text(str(item or "-"))
        return False

    def auto_refresh_visible_status(self) -> bool:
        self.gui_refresh_active = self.should_refresh_ui()
        if not self.should_refresh_ui():
            return True
        self.refresh_page(self.stack.get_visible_child_name())
        return True

    def on_visible_page_changed(self, *_args: object) -> None:
        self.current_page_name = self.stack.get_visible_child_name() or "dashboard"
        self.update_header()

    def on_window_state_event(self, _window: Gtk.Window, _event: Gdk.EventWindowState) -> bool:
        self.gui_refresh_active = self.should_refresh_ui()
        return False

    def on_focus_visibility_event(self, *_args: object) -> bool:
        self.gui_refresh_active = self.should_refresh_ui()
        return False

    def should_refresh_ui(self) -> bool:
        if not self.get_visible():
            return False
        window = self.get_window()
        if window is None:
            return True
        state = window.get_state()
        if state & (Gdk.WindowState.ICONIFIED | Gdk.WindowState.WITHDRAWN):
            return False
        return self.is_active()

    def refresh_power_status(self, *, reload_config: bool = False) -> None:
        status = power_status()
        for key, label in self.power_labels.items():
            label.set_text(status.get(key, "-"))
        self.refresh_app_power_ranking()
        if reload_config:
            self.load_power_policy_controls()
        self.update_header()

    def refresh_mcu_status(self) -> None:
        if not self.mcu_page_ready:
            return
        snapshot = self.mcu_latest_snapshot or mcu_snapshot_placeholder()
        self.mcu_last_rendered = snapshot
        self.mcu_status_labels["device"].set_text(mcu_device_label(snapshot.device))
        self.mcu_status_labels["mode"].set_text(snapshot.device.mode or "-")
        self.mcu_status_labels["state"].set_text(snapshot.state)
        self.mcu_status_labels["event"].set_text(snapshot.event)
        self.mcu_status_labels["motion"].set_text(snapshot.motion)
        self.mcu_status_labels["pose"].set_text(format_pose_label(snapshot.pose))
        self.mcu_status_labels["mic"].set_text(format_mic_label(snapshot))
        self.mcu_status_labels["raw_imu"].set_text(snapshot.raw_line or "-")
        self.mcu_status_labels["light"].set_text(format_light_label(snapshot))
        self.mcu_status_labels["updated"].set_text(mcu_updated_label(snapshot))
        self.mcu_status_labels["error"].set_text(snapshot.last_error or "-")
        for event_name, label in self.mcu_event_labels.items():
            toggle_style_class(label, "mcu-event-active", event_name == snapshot.state)
        self.mcu_summary_label.set_text(mcu_summary_text(snapshot))
        self.mcu_action_label.set_text(mcu_action_text(snapshot))
        self.mcu_hint_label.set_text(mcu_hint_text(snapshot))
        self.update_mcu_mic_assist_switch(snapshot)
        self.mcu_event_store.clear()
        for row in mcu_recent_rows(snapshot):
            self.mcu_event_store.append(row)
        self.update_header()

    def _mcu_monitor_worker(self) -> None:
        state = McuTelemetryState()
        while not self.mcu_monitor_stop.is_set():
            if not self.gui_refresh_active or self.current_page_name != "mcu":
                time.sleep(5.0)
                continue
            try:
                snapshot = read_mcu_snapshot(state)
                state = snapshot_to_telemetry_state(snapshot, state)
                GLib.idle_add(self.finish_mcu_refresh, snapshot)
            except Exception as exc:
                device = find_xiao_device()
                if state.samples:
                    last_sample = state.samples[-1]
                    snapshot = McuStateSnapshot(
                        device=device,
                        state=state.last_state or ("未连接" if not device.present else ("刷机模式" if device.mode == "bootloader" else "已连接")),
                        event=state.last_event or "等待样本",
                        motion=state.last_motion or "-",
                        still_for=max(0.0, time.time() - state.stable_since) if state.stable_since is not None else 0.0,
                        g_force=vector_magnitude(last_sample.ax, last_sample.ay, last_sample.az),
                        tilt_deg=estimate_tilt_deg(last_sample.ax, last_sample.ay, last_sample.az),
                        sample_rate_hz=estimate_sample_rate(state.samples),
                        pose=last_sample.firmware_pose or "-",
                        light_lux=last_sample.light_lux,
                        smoothed_light_lux=state.smoothed_light_lux,
                        suggested_backlight=state.suggested_backlight,
                        light_raw=last_sample.light_raw,
                        light_ready=last_sample.light_ready,
                        last_update=last_sample.timestamp,
                        raw_line=format_sample_line(last_sample),
                        last_error=str(exc),
                        mic_ready=last_sample.mic_ready,
                        mic_enabled=last_sample.mic_enabled,
                        mic_assist=last_sample.mic_assist,
                        mic_peak=last_sample.mic_peak,
                        mic_recent_peak=last_sample.mic_recent_peak,
                        recent_rows=recent_rows_from_samples(state.samples),
                    )
                    GLib.idle_add(self.finish_mcu_refresh, snapshot)
                    time.sleep(1.5)
                    continue
                snapshot = McuStateSnapshot(
                    device=device,
                    state="未连接" if not device.present else ("刷机模式" if device.mode == "bootloader" else "等待数据"),
                    event="等待数据",
                    motion="-",
                    still_for=0.0,
                    g_force=0.0,
                    tilt_deg=0.0,
                    sample_rate_hz=0.0,
                    pose="-",
                    light_lux=None,
                    smoothed_light_lux=state.smoothed_light_lux,
                    suggested_backlight=state.suggested_backlight,
                    light_raw=None,
                    light_ready=False,
                    last_update=time.time(),
                    raw_line="",
                    last_error=str(exc),
                )
                GLib.idle_add(self.finish_mcu_refresh, snapshot)
            time.sleep(1.5)

    def finish_mcu_refresh(self, snapshot: McuStateSnapshot) -> bool:
        self.mcu_latest_snapshot = snapshot
        if self.stack.get_visible_child_name() == "mcu":
            self.refresh_mcu_status()
        return False

    def trigger_mcu_bootloader(self) -> None:
        device = find_xiao_device()
        if not device.present:
            self.show_error("XIAO not found", "没有检测到 XIAO 设备。")
            return
        if device.tty:
            try:
                touch_xiao_bootloader(device.tty)
                self.mcu_action_label.set_text("Bootloader request sent. Watch for UF2 mode.")
            except OSError as exc:
                self.show_error("Bootloader request failed", str(exc))
                return
        else:
            self.show_error("Bootloader request failed", "当前没有可用的串口，无法发送 1200-baud 请求。")

    def calibrate_mcu_pose(self) -> None:
        message = Gtk.Label(label="请先将机器水平放置好，保持静止后再确认校准。", xalign=0)
        message.set_line_wrap(True)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda _button: self.hide_inline_panel())
        confirm_button = Gtk.Button(label="Confirm Calibration")
        confirm_button.get_style_context().add_class("suggested-action")
        confirm_button.connect("clicked", lambda _button: self.confirm_mcu_pose_calibration())
        buttons.pack_start(cancel_button, False, False, 0)
        buttons.pack_start(confirm_button, False, False, 0)
        self.show_inline_panel("放置姿态校准", [message, buttons], show_close=False)

    def confirm_mcu_pose_calibration(self) -> None:
        try:
            send_xiao_command("calibrate pose")
        except OSError as exc:
            self.show_error("Pose calibration failed", str(exc))
            return
        message = Gtk.Label(label="校准已保存。", xalign=0)
        message.set_line_wrap(True)
        self.show_inline_panel("放置姿态校准", [message], show_close=False)
        GLib.timeout_add(1200, self.hide_inline_panel_after_timeout)
        self.mcu_action_label.set_text("放置姿态校准已发送：当前摆放姿态会作为新的放置基准。")

    def on_mcu_mic_assist_changed(self, switch: Gtk.Switch, _pspec: object) -> None:
        if self.mcu_mic_assist_updating:
            return
        command = "mic assist on" if switch.get_active() else "mic assist off"
        try:
            send_xiao_command(command)
        except OSError as exc:
            self.show_error("Mic assist failed", str(exc))
            self.mcu_mic_assist_updating = True
            switch.set_active(not switch.get_active())
            self.mcu_mic_assist_updating = False
            return
        self.mcu_action_label.set_text("麦克风辅助已更新。")

    def update_mcu_mic_assist_switch(self, snapshot: McuStateSnapshot) -> None:
        if not hasattr(self, "mcu_mic_assist_switch"):
            return
        enabled = snapshot.device.present and snapshot.device.mode != "bootloader" and bool(snapshot.device.tty)
        self.mcu_mic_assist_switch.set_sensitive(enabled)
        if snapshot.mic_assist is None:
            return
        if self.mcu_mic_assist_switch.get_active() == snapshot.mic_assist:
            return
        self.mcu_mic_assist_updating = True
        self.mcu_mic_assist_switch.set_active(snapshot.mic_assist)
        self.mcu_mic_assist_updating = False

    def load_mcu_led_controls(self) -> None:
        if not hasattr(self, "mcu_led_controls"):
            return
        config = helper_service_config()
        self.mcu_led_config_updating = True
        for key, switch in self.mcu_led_controls.items():
            switch.set_active(config.get(key, "1").lower() in {"1", "yes", "true", "on", "enabled"})
        self.mcu_led_config_updating = False

    def on_mcu_led_config_changed(self, _switch: Gtk.Switch, _pspec: object) -> None:
        if self.mcu_led_config_updating:
            return
        if self.save_power_policy():
            self.mcu_action_label.set_text("LED behavior updated.")

    def hide_inline_panel_after_timeout(self) -> bool:
        self.hide_inline_panel()
        return False

    def refresh_app_power_ranking(self) -> None:
        now = time.monotonic()
        current = process_power_samples()
        if not self.app_power_previous or self.app_power_previous_time <= 0:
            self.app_power_previous = current
            self.app_power_previous_time = now
            self.app_power_store.clear()
            self.app_power_store.append(["Collecting sample...", "-", "-", "-", "-"])
            return

        elapsed = max(0.1, now - self.app_power_previous_time)
        rows = app_power_rows(self.app_power_previous, current, elapsed)
        self.app_power_previous = current
        self.app_power_previous_time = now
        self.app_power_store.clear()
        if not rows:
            self.app_power_store.append(["No high power apps", "-", "-", "-", "-"])
            return
        for row in rows[:5]:
            self.app_power_store.append(
                [
                    str(row["name"]),
                    str(row["pid"]),
                    f"{row['cpu']:.1f}%",
                    format_bytes_per_second(row["io_rate"]),
                    f"{row['score']:.0f}",
                ]
            )

    def reset_app_power_ranking(self) -> None:
        self.app_power_previous = {}
        self.app_power_previous_time = 0.0
        self.app_power_store.clear()
        self.app_power_store.append(["Collecting sample...", "-", "-", "-", "-"])

    def refresh_utils_status(self) -> None:
        capacity = battery_capacity_percent()
        if capacity >= 0:
            self.utils_battery_label.set_text(f"{capacity}%")
        else:
            self.utils_battery_label.set_text("Unknown")
        self.utils_calibrate_button.set_sensitive(capacity == 100)
        self.utils_reset_xiao_usb_button.set_sensitive(not self.utils_usb_reset_running)
        if capacity == 100:
            self.utils_calibrate_button.set_tooltip_text("电量为 100%，可以执行电量校准。")
        else:
            self.utils_calibrate_button.set_tooltip_text("只有电池电量为 100% 时才能执行电量校准。")
        self.refresh_utils_usb_devices()

    def refresh_utils_usb_devices(self) -> None:
        self.utils_usb_store.clear()
        rows = usb_hub_device_rows(("1-1.4", "1-1.4.2"))
        if not rows:
            self.utils_usb_store.append(["-", "-", "No USB devices", "-", "-"])
            return
        for row in rows:
            self.utils_usb_store.append(list(row))

    def calibrate_battery(self) -> None:
        capacity = battery_capacity_percent()
        self.refresh_utils_status()
        if capacity != 100:
            self.show_error("Battery calibrate blocked", "只有电池电量为 100% 时才能执行电量校准。")
            return
        command = ["sudo", "tee", str(BATTERY_CALIBRATE_PATH)]
        result = subprocess.run(command, input="1\n", text=True, capture_output=True, check=False)
        if result.returncode != 0:
            self.utils_status_label.set_text("Battery calibration failed.")
            self.show_error("Battery calibrate failed", combine_output(result) or "命令执行失败。")
            return
        self.utils_status_label.set_text("Battery calibration command executed.")
        self.refresh_utils_status()

    def reset_xiao_usb_hub(self) -> None:
        if self.utils_usb_reset_running:
            return
        self.utils_usb_reset_running = True
        self.utils_reset_xiao_usb_button.set_sensitive(False)
        self.utils_status_label.set_text("Resetting USB2.0 HUB...")
        thread = threading.Thread(target=self._reset_xiao_usb_hub_worker, daemon=True)
        thread.start()

    def _reset_xiao_usb_hub_worker(self) -> None:
        messages: list[str] = []
        error = ""
        try:
            for index, target in enumerate(xiao_usb_reset_targets()):
                reset_usb_device(target, settle_seconds=2.0 if index == 0 else 3.0)
                messages.append(f"reset {usb_device_product(target)}")
                time.sleep(1.0)
                device = find_xiao_device()
                if device.present and device.tty:
                    GLib.idle_add(
                        self.finish_xiao_usb_hub_reset,
                        f"USB2.0 HUB reset done: /dev/{device.tty} ({', '.join(messages)})",
                        None,
                    )
                    return
        except Exception as exc:
            error = str(exc)
        device = find_xiao_device()
        if device.present:
            if device.tty:
                message = f"USB2.0 HUB reset done: /dev/{device.tty}"
            else:
                message = "USB2.0 HUB reset done, but ttyACM is not ready."
        else:
            message = "XIAO still not detected after USB hub reset."
        if messages:
            message = f"{message} ({', '.join(messages)})"
        GLib.idle_add(self.finish_xiao_usb_hub_reset, message, error or None)

    def finish_xiao_usb_hub_reset(self, message: str, error: str | None) -> bool:
        self.utils_usb_reset_running = False
        self.refresh_utils_status()
        self.utils_status_label.set_text(message)
        if error:
            self.show_error("USB2.0 HUB reset failed", error)
        else:
            self.show_success_banner("USB2.0 HUB reset", message, timeout_ms=3000)
        return False

    def refresh_mapper_status(self) -> None:
        self.load_mapper_shortcuts()

    def load_asr_config_controls(self) -> None:
        values = env_config(MAPPER_ASR_CONFIG, default_asr_config())
        for key, widget in self.asr_controls.items():
            value = values.get(key, "")
            if isinstance(widget, Gtk.Entry):
                widget.set_text(value)
            elif isinstance(widget, Gtk.ComboBoxText):
                set_combo_text(widget, value)
            elif isinstance(widget, Gtk.ComboBox):
                set_combo_model_text(widget, value)
            elif isinstance(widget, Gtk.SpinButton):
                try:
                    widget.set_value(float(value))
                except ValueError:
                    pass
            elif isinstance(widget, Gtk.Switch):
                widget.set_active(value.lower() in {"1", "yes", "true", "on", "enabled"})
        self.sync_asr_tmux_context_state()
        if hasattr(self, "asr_glossary_view"):
            buffer = self.asr_glossary_view.get_buffer()
            try:
                glossary = MAPPER_GLOSSARY_FILE.read_text(encoding="utf-8")
            except OSError:
                glossary = ""
            buffer.set_text(glossary)
        self.asr_status_label.set_text("")

    def save_asr_config(self) -> bool:
        values = env_config(MAPPER_ASR_CONFIG, default_asr_config())
        for key, value in default_asr_config().items():
            values.setdefault(key, value)
        for key, widget in self.asr_controls.items():
            if isinstance(widget, Gtk.Switch):
                values[key] = "1" if widget.get_active() else "0"
            else:
                values[key] = widget_text(widget)
        if values["VOICE_INPUT"] == "Default":
            values["VOICE_INPUT"] = "default"
        if values["ASR_CORRECTION_MODE"] == "off":
            values["VOICE_TMUX_CONTEXT"] = "0"
        if not values["ASR_URL"]:
            self.show_error("ASR config error", "Endpoint is required.")
            return False
        try:
            MAPPER_ASR_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            MAPPER_ASR_CONFIG.write_text(asr_config_text(values), encoding="utf-8")
            if hasattr(self, "asr_glossary_view"):
                buffer = self.asr_glossary_view.get_buffer()
                start, end = buffer.get_bounds()
                MAPPER_GLOSSARY_FILE.write_text(buffer.get_text(start, end, True), encoding="utf-8")
        except OSError as exc:
            self.show_error("Save ASR failed", str(exc))
            return False
        self.asr_status_label.set_text("ASR config saved.")
        return True

    def sync_asr_tmux_context_state(self) -> None:
        correction = widget_text(self.asr_controls.get("ASR_CORRECTION_MODE"))
        tmux_context = self.asr_controls.get("VOICE_TMUX_CONTEXT")
        if isinstance(tmux_context, Gtk.Switch):
            disabled = correction == "off"
            if disabled:
                tmux_context.set_active(False)
            tmux_context.set_sensitive(not disabled)

    def load_power_policy_controls(self) -> None:
        config = helper_service_config()
        self.set_power_mode(config.get("POWERSAVER_MODE", "balanced"), persist=False)
        for key, widget in self.power_controls.items():
            value = config.get(key, "")
            if isinstance(widget, Gtk.Switch):
                if key.endswith("_SCREEN_MODE"):
                    widget.set_active(value.lower() == "auto")
                else:
                    widget.set_active(value.lower() in {"1", "yes", "true", "on", "enabled"})
            elif isinstance(widget, Gtk.ComboBoxText):
                if key.endswith("_UNKNOWN_POWER_ACTION"):
                    value = unknown_action_display_value(value)
                elif key.endswith("_SCREEN_TIMEOUT_SEC"):
                    value = screen_timeout_display_value(value)
                elif "_AUTO_" in key and key.endswith("_TIMEOUT_SEC"):
                    value = auto_screen_timeout_display_value(value)
                set_combo_text(widget, value)
            elif isinstance(widget, Gtk.Entry):
                widget.set_text(value)
        for profile in ("ECO", "BALANCED", "PERFORMANCE"):
            self.sync_power_screen_mode_visibility(profile)

    def save_power_policy(self) -> bool:
        try:
            values = self.power_policy_values()
        except ValueError as exc:
            self.show_error("Power policy error", str(exc))
            return False
        config_text = power_policy_config_text(values)
        if not self.write_power_config(config_text):
            return False
        saved_values = helper_service_config()
        mismatched = [
            key
            for key, value in values.items()
            if key.startswith("POWERSAVER_") and saved_values.get(key) != value
        ]
        if mismatched:
            self.show_error("Save policy failed", f"配置写入后校验失败: {mismatched[0]}")
            return False
        if self.should_enable_wwan_after_policy_save(values):
            if not self.enable_wwan_radio_after_policy_save():
                return False
        idle_restart = self.run_user_systemctl(["restart", "uconsole-helper-idle.service"], "Restart idle service")
        if idle_restart.returncode != 0:
            return False
        self.refresh_power_status()
        return True

    def should_enable_wwan_after_policy_save(self, values: dict[str, str]) -> bool:
        return any(
            values.get(f"POWERSAVER_{profile}_WWAN_POLICY") == "keep"
            for profile in ("ECO", "BALANCED", "PERFORMANCE")
        )

    def enable_wwan_radio_after_policy_save(self) -> bool:
        if shutil.which("nmcli") is None:
            self.show_error("Save policy failed", "WWAN 策略已保存，但找不到 nmcli，无法自动开启 WWAN。")
            return False
        result = subprocess.run(["nmcli", "radio", "wwan", "on"], text=True, capture_output=True, check=False)
        if result.returncode != 0:
            self.show_error("Save policy failed", combine_output(result) or "WWAN 策略已保存，但自动开启 WWAN 失败。")
            return False
        return True

    def set_power_mode(self, mode: str, persist: bool = True) -> bool:
        if mode not in {"eco", "balanced", "performance"}:
            mode = "balanced"
        self.selected_power_mode = mode
        self.refresh_power_mode_cards()
        return False

    def refresh_power_mode_cards(self) -> None:
        for mode, card in self.power_profile_cards.items():
            context = card.get_style_context()
            if mode == self.selected_power_mode:
                context.add_class("power-profile-selected")
            else:
                context.remove_class("power-profile-selected")

    def toggle_powersaver_enabled(self) -> None:
        try:
            values = self.power_policy_values(enabled_override=not powersaver_enabled())
        except ValueError as exc:
            self.show_error("Power policy error", str(exc))
            return
        config_text = power_policy_config_text(values)
        if not self.write_power_config(config_text):
            return
        self.refresh_power_status()

    def write_power_config(self, config_text: str) -> bool:
        try:
            result = subprocess.run(
                ["sudo", "-n", "/usr/local/bin/uconsole-helper-service", "write-config"],
                input=config_text,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            self.show_error("Save policy failed", f"配置写入失败: {exc}")
            return False
        if result.returncode != 0:
            self.show_error("Save policy failed", combine_output(result) or "配置写入失败。")
            return False
        return True

    def power_policy_values(self, enabled_override: bool | None = None) -> dict[str, str]:
        current_enabled = powersaver_enabled()
        enabled = current_enabled if enabled_override is None else enabled_override
        values = {
            "POWERSAVER_ENABLED": "1" if enabled else "0",
            "POWERSAVER_MODE": self.selected_power_mode,
            "POWERSAVER_POLL_INTERVAL_SEC": "5",
        }
        for profile in ("ECO", "BALANCED", "PERFORMANCE"):
            battery_key = f"POWERSAVER_{profile}_BATTERY_CPU_FREQ"
            ac_key = f"POWERSAVER_{profile}_AC_CPU_FREQ"
            battery_screen_key = f"POWERSAVER_{profile}_BATTERY_SCREEN_TIMEOUT_SEC"
            ac_screen_key = f"POWERSAVER_{profile}_AC_SCREEN_TIMEOUT_SEC"
            unknown_key = f"POWERSAVER_{profile}_UNKNOWN_POWER_ACTION"
            wwan_key = f"POWERSAVER_{profile}_WWAN_POLICY"
            screen_mode_key = f"POWERSAVER_{profile}_SCREEN_MODE"
            auto_brightness_key = f"POWERSAVER_{profile}_AUTO_BRIGHTNESS"
            stand_mode_key = f"POWERSAVER_{profile}_STAND_MODE"
            auto_battery_key = f"POWERSAVER_{profile}_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC"
            auto_ac_key = f"POWERSAVER_{profile}_AUTO_AC_PUTDOWN_TIMEOUT_SEC"
            values[battery_key] = widget_text(self.power_controls[battery_key])
            values[ac_key] = widget_text(self.power_controls[ac_key])
            values[battery_screen_key] = screen_timeout_config_value(widget_text(self.power_controls[battery_screen_key]))
            values[ac_screen_key] = screen_timeout_config_value(widget_text(self.power_controls[ac_screen_key]))
            values[unknown_key] = unknown_action_config_value(widget_text(self.power_controls[unknown_key]))
            values[wwan_key] = widget_text(self.power_controls[wwan_key])
            values[screen_mode_key] = "auto" if self.power_controls[screen_mode_key].get_active() else "default"
            values[auto_brightness_key] = "1" if self.power_controls[auto_brightness_key].get_active() else "0"
            values[stand_mode_key] = "1" if self.power_controls[stand_mode_key].get_active() else "0"
            values[auto_battery_key] = auto_screen_timeout_config_value(widget_text(self.power_controls[auto_battery_key]))
            values[auto_ac_key] = auto_screen_timeout_config_value(widget_text(self.power_controls[auto_ac_key]))
            validate_freq_pair(values[battery_key], f"{profile.title()} Battery MHz")
            if values[ac_key] != "restore":
                validate_freq_pair(values[ac_key], f"{profile.title()} AC MHz")
            try:
                if int(values[battery_screen_key]) < 0 or int(values[ac_screen_key]) < 0:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"{profile.title()} screen timeouts must be Default or seconds.") from exc
            if values[unknown_key] not in {"restore", "battery", "keep"}:
                raise ValueError(f"{profile.title()} Unknown must be AC, Battery, or Keep.")
            if values[wwan_key] not in {"keep", "off", "ondemand"}:
                raise ValueError(f"{profile.title()} WWAN must be keep, off, or ondemand.")
            if values[screen_mode_key] not in {"default", "auto"}:
                raise ValueError(f"{profile.title()} Auto Power Save must be on or off.")
            for key in (auto_battery_key, auto_ac_key):
                try:
                    if int(values[key]) < 0:
                        raise ValueError
                except ValueError as exc:
                    raise ValueError(f"{profile.title()} auto screen timeouts must be seconds or Never.") from exc
        values["POWERSAVER_UNKNOWN_POWER_ACTION"] = values["POWERSAVER_BALANCED_UNKNOWN_POWER_ACTION"]
        values["POWERSAVER_WWAN_POLICY"] = values["POWERSAVER_BALANCED_WWAN_POLICY"]
        if values["POWERSAVER_MODE"] not in {"eco", "balanced", "performance"}:
            raise ValueError("Mode must be eco, balanced, or performance.")
        config = helper_service_config()
        values["POWERSAVER_CPU_POLICY_PATH"] = config.get(
            "POWERSAVER_CPU_POLICY_PATH", "/sys/devices/system/cpu/cpufreq/policy0"
        )
        values["POWERSAVER_POWER_SUPPLY_DIR"] = config.get(
            "POWERSAVER_POWER_SUPPLY_DIR", "/sys/class/power_supply"
        )
        led_controls = getattr(self, "mcu_led_controls", {})
        for key, switch in led_controls.items():
            values[key] = "1" if switch.get_active() else "0"
        values.setdefault("MCU_LED_BATTERY_ENABLED", config.get("MCU_LED_BATTERY_ENABLED", "1"))
        values.setdefault("MCU_LED_LXTERMINAL_BELL_ENABLED", config.get("MCU_LED_LXTERMINAL_BELL_ENABLED", "1"))
        values.setdefault("MCU_LED_NIGHT_MODE_ENABLED", config.get("MCU_LED_NIGHT_MODE_ENABLED", "1"))
        return values

    def run_systemctl(self, args: list[str], title: str) -> subprocess.CompletedProcess[str]:
        command = ["sudo", "-n", "systemctl", *args]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            self.show_error(f"{title} failed", combine_output(result) or "命令执行失败。")
        return result

    def run_user_systemctl(self, args: list[str], title: str) -> subprocess.CompletedProcess[str]:
        command = ["systemctl", "--user", *args]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            self.show_error(f"{title} failed", combine_output(result) or "命令执行失败。")
        return result

    def toggle_user_service(self, service: str, title: str) -> None:
        active = user_service_active(service)
        args = ["disable", "--now", service] if active else ["enable", "--now", service]
        result = self.run_user_systemctl(args, title)
        if result.returncode == 0:
            self.refresh_mapper_status()
            self.update_header()

    def refresh_status(self) -> None:
        result = run_helper("status")
        if result.returncode == 0 and "running" in result.stdout:
            self.dhcp_running = True
            self.dhcp_interface = running_dhcp_interface()
        elif dnsmasq_running():
            self.dhcp_running = True
            self.dhcp_interface = running_dhcp_interface()
        elif result.returncode == 0:
            self.dhcp_running = False
            self.dhcp_interface = ""
        else:
            self.dhcp_running = False
            self.dhcp_interface = ""
        self.update_header()
        self.refresh_interface_status()

    def start_server(self) -> None:
        try:
            config = self.validated_config()
        except ValueError as exc:
            self.show_error("配置错误", str(exc))
            return

        if not shutil.which("dnsmasq"):
            self.show_error("缺少依赖", "未找到 dnsmasq，请先安装 dnsmasq。")
            return

        message = Gtk.Label(
            label=f"将刷新 {config['interface']} 的地址并启动 DHCP Server。\n不要选择正在上网或远程连接的网口。",
            xalign=0,
        )
        message.set_line_wrap(True)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.connect("clicked", lambda _button: self.hide_inline_panel())
        buttons.pack_start(cancel_button, False, False, 0)
        start_button = Gtk.Button(label="Start")
        start_button.get_style_context().add_class("suggested-action")
        start_button.connect("clicked", lambda _button, value=config: self.confirm_start_server(value))
        buttons.pack_start(start_button, False, False, 0)
        self.show_inline_panel("确认启动 DHCP Server", [message, buttons], show_close=False)

    def confirm_start_server(self, config: dict[str, str]) -> None:
        self.hide_inline_panel()
        self.message_label.set_text("正在启动 DHCP Server...")
        self.message_label.show()
        while Gtk.events_pending():
            Gtk.main_iteration()
        result = run_helper("start", config)
        output = combine_output(result)
        if result.returncode == 0:
            self.dhcp_running = True
            self.dhcp_interface = config["interface"]
            self.message_label.hide()
            self.refresh_interface_status()
        else:
            self.dhcp_running = False
            self.dhcp_interface = ""
            self.message_label.set_text("启动失败。")
            self.show_error("启动失败", output or "命令执行失败。")
        self.update_header()

    def stop_server(self) -> None:
        self.message_label.set_text("正在停止 DHCP Server...")
        self.message_label.show()
        while Gtk.events_pending():
            Gtk.main_iteration()
        result = run_helper("stop")
        output = combine_output(result)
        if result.returncode == 0:
            self.dhcp_running = False
            self.dhcp_interface = ""
            self.message_label.hide()
            self.refresh_interface_status()
        else:
            self.message_label.set_text("停止失败。")
            self.show_error("停止失败", output or "命令执行失败。")
        self.update_header()

    def validated_config(self) -> dict[str, str]:
        selected = self.selected_dhcp_interface()
        interface = selected.name if selected else ""
        if not interface:
            raise ValueError("请选择网口。")
        if selected and not selected.supported:
            detail = f"原因: {selected.reason}" if selected.reason else "该网口不适合用于 DHCP Server。"
            raise ValueError(f"{interface} 不支持作为 DHCP Server 网口。\n{detail}")
        if not interface_exists(interface):
            raise ValueError("选择的网口不存在。")

        config = self.current_dhcp_form_values()
        for key in ("server_ip", "netmask", "pool_start", "pool_end", "lease_time"):
            if not config[key]:
                raise ValueError(f"{key} 不能为空。")

        server_ip = ipaddress.IPv4Address(config["server_ip"])
        pool_start = ipaddress.IPv4Address(config["pool_start"])
        pool_end = ipaddress.IPv4Address(config["pool_end"])
        network = ipaddress.IPv4Network(f"{server_ip}/{config['netmask']}", strict=False)
        pool_bounds = dhcp_pool_bounds(network)
        if pool_bounds is None:
            raise ValueError("DHCP 地址池需要至少 2 个可用主机地址。")
        first_pool_ip, last_pool_ip = pool_bounds

        if pool_start < first_pool_ip or pool_start > last_pool_ip or pool_end < first_pool_ip or pool_end > last_pool_ip:
            raise ValueError("地址池必须在本机地址所在子网内。")
        if pool_start > pool_end:
            raise ValueError("地址池起始地址不能大于结束地址。")
        if pool_start <= server_ip <= pool_end:
            raise ValueError("本机地址不能落在 DHCP 地址池内。")

        for optional in ("gateway", "dns"):
            if config[optional]:
                for item in [part.strip() for part in config[optional].split(",") if part.strip()]:
                    ipaddress.IPv4Address(item)

        config["interface"] = interface
        return config

    def selected_scan_interface(self) -> InterfaceInfo | None:
        name = self.selected_scan_interface_name()
        if not name:
            return None
        if self.dhcp_running and name == self.dhcp_interface:
            network = self.selected_scan_network(name)
            status = "DHCP Server"
            if network is not None:
                status = f"{status}, {network}"
            return InterfaceInfo(name=name, supported=network is not None, status=status, reason="" if network else "没有可扫描 IPv4 网段")
        return scan_interface_info(name, "")

    def selected_scan_network(self, name: str) -> ipaddress.IPv4Network | None:
        network = interface_ipv4_network(name)
        if network is None and self.dhcp_running and name == self.dhcp_interface:
            network = running_dhcp_network(name)
        return network

    def selected_dhcp_interface(self) -> InterfaceInfo | None:
        name = self.selected_scan_interface_name()
        if not name:
            return None
        return dhcp_interface_info(name)

    def selected_scan_interface_name(self) -> str:
        if self.interface_tree is None:
            return ""
        model, tree_iter = self.interface_tree.get_selection().get_selected()
        if tree_iter is None:
            return ""
        return str(model[tree_iter][0])

    def select_scan_interface(self, name: str) -> None:
        if self.interface_tree is None or not name:
            return
        for row in self.interface_status_store:
            if row[0] == name:
                path = self.interface_status_store.get_path(row.iter)
                self.interface_tree.get_selection().select_iter(row.iter)
                self.interface_tree.scroll_to_cell(path, None, False, 0.0, 0.0)
                return

    def start_lan_scan(self) -> None:
        if self.scan_running:
            return
        selected = self.selected_scan_interface()
        if selected is None:
            self.show_error("配置错误", "请选择要扫描的网口。")
            return
        if not selected.supported:
            detail = f"原因: {selected.reason}" if selected.reason else "该网口当前无法扫描。"
            self.show_error("配置错误", f"{selected.name} 当前无法扫描。\n{detail}")
            return

        network = self.selected_scan_network(selected.name)
        if network is None:
            self.show_error("没有 IPv4 地址", f"{selected.name} 当前没有可扫描的 IPv4 网段。")
            return

        self.scan_running = True
        self.scan_cancel.clear()
        self.scan_message_label.set_text(f"正在扫描 {selected.name} / {network} ...")
        self.scan_store.clear()
        self.update_header()
        thread = threading.Thread(target=self._scan_worker, args=(selected.name, network), daemon=True)
        thread.start()

    def stop_lan_scan(self) -> None:
        if not self.scan_running:
            return
        self.scan_cancel.set()
        self.scan_message_label.set_text("正在停止 LAN Scan...")
        self.update_header()

    def _scan_worker(self, interface: str, network: ipaddress.IPv4Network) -> None:
        try:
            hosts = scan_lan(interface, network, self.scan_cancel)
            GLib.idle_add(self.finish_lan_scan, interface, str(network), hosts, None)
        except Exception as exc:
            GLib.idle_add(self.finish_lan_scan, interface, str(network), [], str(exc))

    def finish_lan_scan(
        self,
        interface: str,
        network: str,
        hosts: list[dict[str, str]],
        error: str | None,
    ) -> bool:
        self.scan_running = False
        cancelled = self.scan_cancel.is_set()
        self.scan_cancel.clear()
        self.update_header()
        if cancelled:
            self.scan_message_label.set_text("LAN Scan 已停止。")
            return False

        self.scan_store.clear()
        if error:
            self.scan_message_label.set_text("扫描失败。")
            self.show_error("扫描失败", error)
            return False

        for host in hosts:
            self.scan_store.append([host["ip"], host["mac"], host["state"], host["hostname"]])
        self.scan_message_label.set_text(f"{interface} / {network} 扫描完成，发现 {len(hosts)} 台设备。")
        return False

    def show_error(self, title: str, message: str) -> None:
        message_label = Gtk.Label(label=message, xalign=0)
        message_label.set_line_wrap(True)
        message_label.set_selectable(True)
        self.show_inline_panel(title, [message_label], show_close=True)

    def show_success_banner(self, title: str, message: str, timeout_ms: int = 3000) -> None:
        message_label = Gtk.Label(label=message, xalign=0)
        message_label.set_line_wrap(True)
        self.show_inline_panel(title, [message_label], show_close=False)
        GLib.timeout_add(timeout_ms, self.hide_inline_panel_after_timeout)

    def show_inline_panel(self, title: str, widgets: list[Gtk.Widget], show_close: bool = True) -> None:
        if self.inline_panel_box is None or self.inline_panel_title is None or self.inline_panel_body is None:
            print(title, file=sys.stderr)
            return
        for child in list(self.inline_panel_body.get_children()):
            self.inline_panel_body.remove(child)
        self.inline_panel_title.set_text(title)
        for widget in widgets:
            self.inline_panel_body.pack_start(widget, False, False, 0)
        self.inline_panel_box.set_no_show_all(False)
        self.inline_panel_box.show_all()
        if self.inline_panel_close_button is not None:
            self.inline_panel_close_button.set_visible(show_close)
        self.inline_panel_box.set_no_show_all(True)

    def hide_error(self) -> None:
        self.hide_inline_panel()

    def hide_inline_panel(self) -> None:
        if self.inline_panel_box is not None:
            self.inline_panel_box.hide()


def card_box() -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    box.get_style_context().add_class("card")
    return box


def dashboard_card(title: str) -> Gtk.Box:
    box = card_box()
    box.get_style_context().add_class("dashboard-card")
    title_label = Gtk.Label(label=f" {title.upper()} ", xalign=0)
    title_label.get_style_context().add_class("dashboard-title")
    box.pack_start(title_label, False, False, 0)
    return box


def asr_control_row(title: str, widget: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
    box.set_hexpand(True)
    label = Gtk.Label(label=title, xalign=0)
    label.get_style_context().add_class("muted")
    label.set_width_chars(13)
    widget.set_hexpand(True)
    box.pack_start(label, False, False, 0)
    box.pack_start(widget, True, True, 0)
    return box


def disable_spin_scroll(spin: Gtk.SpinButton) -> None:
    spin.connect("scroll-event", lambda _widget, _event: True)


def scrolled_page(content: Gtk.Widget) -> Gtk.ScrolledWindow:
    scroll = Gtk.ScrolledWindow()
    scroll.set_hexpand(True)
    scroll.set_vexpand(True)
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.add(content)
    return scroll


def table_scroll(content: Gtk.Widget, vexpand: bool) -> Gtk.ScrolledWindow:
    scroll = Gtk.ScrolledWindow()
    scroll.set_hexpand(True)
    scroll.set_vexpand(vexpand)
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC if vexpand else Gtk.PolicyType.NEVER)
    scroll.add(content)
    return scroll


def underlined_button(text: str, key: str) -> Gtk.Button:
    label = Gtk.Label()
    label.set_use_markup(True)
    label.set_markup(underlined_markup(text, key))
    button = Gtk.Button()
    button.add(label)
    return button


def set_underlined_button_label(button: Gtk.Button, text: str, key: str) -> None:
    child = button.get_child()
    if isinstance(child, Gtk.Label):
        child.set_use_markup(True)
        child.set_markup(underlined_markup(text, key))
    else:
        button.set_label(text)


def set_status_underlined_button_label(button: Gtk.Button, text: str, key: str, color: str) -> None:
    child = button.get_child()
    markup = f'<span foreground="{escape(color)}">●</span> {underlined_markup(text, key)}'
    if isinstance(child, Gtk.Label):
        child.set_use_markup(True)
        child.set_markup(markup)
    else:
        button.set_label(text)


def underlined_markup(text: str, key: str) -> str:
    index = text.lower().find(key.lower())
    if index == -1:
        return escape(text)
    before = escape(text[:index])
    letter = escape(text[index : index + 1])
    after = escape(text[index + 1 :])
    return f"{before}<u>{letter}</u>{after}"


def toggle_style_class(widget: Gtk.Widget, class_name: str, enabled: bool) -> None:
    context = widget.get_style_context()
    if enabled:
        context.add_class(class_name)
    else:
        context.remove_class(class_name)


def combo_text_from_values(values: tuple[str, ...]) -> Gtk.ComboBoxText:
    combo = Gtk.ComboBoxText()
    combo.connect("scroll-event", block_combo_scroll)
    for value in values:
        combo.append_text(value)
    if values:
        combo.set_active(0)
    return combo


def ellipsized_combo_text_from_values(values: tuple[str, ...], width_chars: int) -> Gtk.ComboBox:
    store = Gtk.ListStore(str)
    for value in values:
        store.append([value])
    combo = Gtk.ComboBox.new_with_model(store)
    combo.connect("scroll-event", block_combo_scroll)
    renderer = Gtk.CellRendererText()
    renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
    renderer.set_property("width-chars", width_chars)
    renderer.set_property("max-width-chars", width_chars)
    combo.pack_start(renderer, True)
    combo.add_attribute(renderer, "text", 0)
    combo.set_size_request(width_chars * 8, -1)
    combo.connect("changed", update_combo_model_tooltip)
    if len(store) > 0:
        combo.set_active(0)
        update_combo_model_tooltip(combo)
    return combo


def update_combo_model_tooltip(combo: Gtk.ComboBox) -> None:
    active = combo.get_active_iter()
    model = combo.get_model()
    if active is not None and model is not None:
        combo.set_tooltip_text(str(model[active][0]))
    else:
        combo.set_tooltip_text(None)


def block_combo_scroll(combo: Gtk.ComboBox, event: Gdk.EventScroll) -> bool:
    parent = combo.get_parent()
    while parent is not None and not isinstance(parent, Gtk.ScrolledWindow):
        parent = parent.get_parent()
    if isinstance(parent, Gtk.ScrolledWindow):
        adjustment = parent.get_vadjustment()
        step = adjustment.get_step_increment() or 48
        delta = 0.0
        if event.direction == Gdk.ScrollDirection.DOWN:
            delta = step
        elif event.direction == Gdk.ScrollDirection.UP:
            delta = -step
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            _ok, _dx, dy = event.get_scroll_deltas()
            delta = dy * step
        if delta:
            lower = adjustment.get_lower()
            upper = adjustment.get_upper() - adjustment.get_page_size()
            adjustment.set_value(max(lower, min(upper, adjustment.get_value() + delta)))
    return True


def set_combo_text(combo: Gtk.ComboBoxText, value: str) -> None:
    if value == "default":
        value = "Default"
    model = combo.get_model()
    if model is not None:
        for index, row in enumerate(model):
            if row[0] == value:
                combo.set_active(index)
                return
    if model is not None and len(model) > 0:
        combo.set_active(0)


def set_combo_model_text(combo: Gtk.ComboBox, value: str) -> None:
    if value == "default":
        value = "Default"
    model = combo.get_model()
    if model is not None:
        for index, row in enumerate(model):
            if row[0] == value:
                combo.set_active(index)
                return
    if model is not None and len(model) > 0:
        combo.set_active(0)


def dhcp_pool_bounds(network: ipaddress.IPv4Network) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address] | None:
    first = int(network.network_address) + 1
    last = int(network.broadcast_address) - 1
    if first > last:
        return None
    return ipaddress.IPv4Address(first), ipaddress.IPv4Address(last)


def pool_edit_parts(network: ipaddress.IPv4Network) -> tuple[str, str, str]:
    bounds = dhcp_pool_bounds(network)
    if bounds is None:
        raise ValueError("DHCP 地址池需要至少 2 个可用主机地址。")
    first, last = bounds
    first_text = str(first)
    last_text = str(last)
    prefix_length = common_prefix_length(first_text, last_text)
    prefix = first_text[:prefix_length]
    return prefix, first_text[prefix_length:], last_text[prefix_length:]


def common_prefix_length(left: str, right: str) -> int:
    length = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        length += 1
    return length


def pool_address_from_value(
    value: str,
    first_pool_ip: ipaddress.IPv4Address,
    last_pool_ip: ipaddress.IPv4Address,
    fallback: ipaddress.IPv4Address,
) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return fallback
    if address < first_pool_ip or address > last_pool_ip:
        return fallback
    return address


def address_suffix(address: ipaddress.IPv4Address, prefix: str) -> str:
    text = str(address)
    return text[len(prefix) :] if text.startswith(prefix) else text


def unknown_action_display_value(value: str) -> str:
    return {
        "restore": "AC",
        "battery": "Battery",
        "keep": "Keep",
    }.get(value, value)


def unknown_action_config_value(value: str) -> str:
    return {
        "AC": "restore",
        "Battery": "battery",
        "Keep": "keep",
    }.get(value, value)


def screen_timeout_display_value(value: str) -> str:
    return {
        "0": "Default",
        "30": "30s",
        "60": "1min",
        "120": "2min",
        "300": "5min",
        "600": "10min",
        "900": "15min",
    }.get(value, value)


def screen_timeout_config_value(value: str) -> str:
    return {
        "Default": "0",
        "30s": "30",
        "1min": "60",
        "2min": "120",
        "5min": "300",
        "10min": "600",
        "15min": "900",
    }.get(value, value)


def auto_screen_timeout_display_value(value: str) -> str:
    return {
        "-1": "Never",
        "5": "5s",
        "10": "10s",
        "15": "15s",
        "30": "30s",
        "60": "1min",
        "120": "2min",
        "300": "5min",
        "600": "10min",
        "900": "15min",
        "1800": "30min",
    }.get(value, value)


def auto_screen_timeout_config_value(value: str) -> str:
    return {
        "Never": "-1",
        "5s": "5",
        "10s": "10",
        "15s": "15",
        "30s": "30",
        "1min": "60",
        "2min": "120",
        "5min": "300",
        "10min": "600",
        "15min": "900",
        "30min": "1800",
    }.get(value, value)


def widget_text(widget: Gtk.Widget) -> str:
    if isinstance(widget, Gtk.Entry):
        return widget.get_text().strip()
    if isinstance(widget, Gtk.ComboBoxText):
        text = widget.get_active_text()
        if text:
            return text.strip()
        child = widget.get_child()
        if isinstance(child, Gtk.Entry):
            return child.get_text().strip()
    if isinstance(widget, Gtk.SpinButton):
        return str(int(widget.get_value())) if widget.get_digits() == 0 else str(widget.get_value())
    if isinstance(widget, Gtk.ComboBox):
        active = widget.get_active_iter()
        model = widget.get_model()
        if active is not None and model is not None:
            return str(model[active][0]).strip()
    return ""


def validate_freq_pair(value: str, title: str) -> None:
    parts = value.split(",", 1)
    if len(parts) != 2:
        raise ValueError(f"{title} must be min,max MHz, for example 1500,1500.")
    try:
        min_mhz = int(parts[0])
        max_mhz = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"{title} must contain integer MHz values.") from exc
    if min_mhz < 1500 or max_mhz < 1500 or min_mhz > max_mhz:
        raise ValueError(f"{title} must be at least 1500 MHz and min must be <= max.")


def power_policy_config_text(values: dict[str, str]) -> str:
    return "\n".join(
        [
            "###########################################################################",
            "#                   uConsole Helper background service                     #",
            "###########################################################################",
            "",
            "### POWERSAVER_ENABLED --- [1|0] --- Enable AC/battery CPU policy task",
            f"POWERSAVER_ENABLED={values['POWERSAVER_ENABLED']}",
            "",
            "### POWERSAVER_MODE --- [eco|balanced|performance]",
            f"POWERSAVER_MODE={values['POWERSAVER_MODE']}",
            "",
            "### Mode policy matrix --- CPU MHz while on battery / AC",
            f"POWERSAVER_ECO_BATTERY_CPU_FREQ={values['POWERSAVER_ECO_BATTERY_CPU_FREQ']}",
            f"POWERSAVER_ECO_AC_CPU_FREQ={values['POWERSAVER_ECO_AC_CPU_FREQ']}",
            f"POWERSAVER_ECO_BATTERY_SCREEN_TIMEOUT_SEC={values['POWERSAVER_ECO_BATTERY_SCREEN_TIMEOUT_SEC']}",
            f"POWERSAVER_ECO_AC_SCREEN_TIMEOUT_SEC={values['POWERSAVER_ECO_AC_SCREEN_TIMEOUT_SEC']}",
            f"POWERSAVER_ECO_UNKNOWN_POWER_ACTION={values['POWERSAVER_ECO_UNKNOWN_POWER_ACTION']}",
            f"POWERSAVER_ECO_WWAN_POLICY={values['POWERSAVER_ECO_WWAN_POLICY']}",
            f"POWERSAVER_ECO_SCREEN_MODE={values['POWERSAVER_ECO_SCREEN_MODE']}",
            f"POWERSAVER_ECO_AUTO_BRIGHTNESS={values['POWERSAVER_ECO_AUTO_BRIGHTNESS']}",
            f"POWERSAVER_ECO_STAND_MODE={values['POWERSAVER_ECO_STAND_MODE']}",
            f"POWERSAVER_ECO_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC={values['POWERSAVER_ECO_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC']}",
            f"POWERSAVER_ECO_AUTO_AC_PUTDOWN_TIMEOUT_SEC={values['POWERSAVER_ECO_AUTO_AC_PUTDOWN_TIMEOUT_SEC']}",
            f"POWERSAVER_BALANCED_BATTERY_CPU_FREQ={values['POWERSAVER_BALANCED_BATTERY_CPU_FREQ']}",
            f"POWERSAVER_BALANCED_AC_CPU_FREQ={values['POWERSAVER_BALANCED_AC_CPU_FREQ']}",
            f"POWERSAVER_BALANCED_BATTERY_SCREEN_TIMEOUT_SEC={values['POWERSAVER_BALANCED_BATTERY_SCREEN_TIMEOUT_SEC']}",
            f"POWERSAVER_BALANCED_AC_SCREEN_TIMEOUT_SEC={values['POWERSAVER_BALANCED_AC_SCREEN_TIMEOUT_SEC']}",
            f"POWERSAVER_BALANCED_UNKNOWN_POWER_ACTION={values['POWERSAVER_BALANCED_UNKNOWN_POWER_ACTION']}",
            f"POWERSAVER_BALANCED_WWAN_POLICY={values['POWERSAVER_BALANCED_WWAN_POLICY']}",
            f"POWERSAVER_BALANCED_SCREEN_MODE={values['POWERSAVER_BALANCED_SCREEN_MODE']}",
            f"POWERSAVER_BALANCED_AUTO_BRIGHTNESS={values['POWERSAVER_BALANCED_AUTO_BRIGHTNESS']}",
            f"POWERSAVER_BALANCED_STAND_MODE={values['POWERSAVER_BALANCED_STAND_MODE']}",
            f"POWERSAVER_BALANCED_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC={values['POWERSAVER_BALANCED_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC']}",
            f"POWERSAVER_BALANCED_AUTO_AC_PUTDOWN_TIMEOUT_SEC={values['POWERSAVER_BALANCED_AUTO_AC_PUTDOWN_TIMEOUT_SEC']}",
            f"POWERSAVER_PERFORMANCE_BATTERY_CPU_FREQ={values['POWERSAVER_PERFORMANCE_BATTERY_CPU_FREQ']}",
            f"POWERSAVER_PERFORMANCE_AC_CPU_FREQ={values['POWERSAVER_PERFORMANCE_AC_CPU_FREQ']}",
            f"POWERSAVER_PERFORMANCE_BATTERY_SCREEN_TIMEOUT_SEC={values['POWERSAVER_PERFORMANCE_BATTERY_SCREEN_TIMEOUT_SEC']}",
            f"POWERSAVER_PERFORMANCE_AC_SCREEN_TIMEOUT_SEC={values['POWERSAVER_PERFORMANCE_AC_SCREEN_TIMEOUT_SEC']}",
            f"POWERSAVER_PERFORMANCE_UNKNOWN_POWER_ACTION={values['POWERSAVER_PERFORMANCE_UNKNOWN_POWER_ACTION']}",
            f"POWERSAVER_PERFORMANCE_WWAN_POLICY={values['POWERSAVER_PERFORMANCE_WWAN_POLICY']}",
            f"POWERSAVER_PERFORMANCE_SCREEN_MODE={values['POWERSAVER_PERFORMANCE_SCREEN_MODE']}",
            f"POWERSAVER_PERFORMANCE_AUTO_BRIGHTNESS={values['POWERSAVER_PERFORMANCE_AUTO_BRIGHTNESS']}",
            f"POWERSAVER_PERFORMANCE_STAND_MODE={values['POWERSAVER_PERFORMANCE_STAND_MODE']}",
            f"POWERSAVER_PERFORMANCE_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC={values['POWERSAVER_PERFORMANCE_AUTO_BATTERY_PUTDOWN_TIMEOUT_SEC']}",
            f"POWERSAVER_PERFORMANCE_AUTO_AC_PUTDOWN_TIMEOUT_SEC={values['POWERSAVER_PERFORMANCE_AUTO_AC_PUTDOWN_TIMEOUT_SEC']}",
            "",
            "### POWERSAVER_UNKNOWN_POWER_ACTION --- [restore|battery|keep]",
            f"POWERSAVER_UNKNOWN_POWER_ACTION={values['POWERSAVER_UNKNOWN_POWER_ACTION']}",
            "",
            "### POWERSAVER_WWAN_POLICY --- [keep|off|ondemand]",
            f"POWERSAVER_WWAN_POLICY={values['POWERSAVER_WWAN_POLICY']}",
            "",
            "### POWERSAVER_POLL_INTERVAL_SEC --- [1.0~]",
            f"POWERSAVER_POLL_INTERVAL_SEC={values['POWERSAVER_POLL_INTERVAL_SEC']}",
            "",
            "### POWERSAVER_CPU_POLICY_PATH --- cpufreq policy directory",
            f"POWERSAVER_CPU_POLICY_PATH={values['POWERSAVER_CPU_POLICY_PATH']}",
            "",
            "### POWERSAVER_POWER_SUPPLY_DIR --- power_supply sysfs directory",
            f"POWERSAVER_POWER_SUPPLY_DIR={values['POWERSAVER_POWER_SUPPLY_DIR']}",
            "",
            "### MCU LED behavior --- [1|0]",
            f"MCU_LED_BATTERY_ENABLED={values['MCU_LED_BATTERY_ENABLED']}",
            f"MCU_LED_LXTERMINAL_BELL_ENABLED={values['MCU_LED_LXTERMINAL_BELL_ENABLED']}",
            f"MCU_LED_NIGHT_MODE_ENABLED={values['MCU_LED_NIGHT_MODE_ENABLED']}",
            "",
        ]
    )


def copy_to_clipboard(text: str) -> None:
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_text(text, -1)
    clipboard.store()


def is_text_input_focus(window: Gtk.Window) -> bool:
    focus = window.get_focus()
    return isinstance(focus, (Gtk.Entry, Gtk.TextView))


def section_header(title: str, subtitle: str) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    title_label = Gtk.Label(label=title, xalign=0)
    title_label.get_style_context().add_class("section-title")
    box.pack_start(title_label, False, False, 0)
    subtitle_label = Gtk.Label(label=subtitle, xalign=0)
    subtitle_label.set_line_wrap(True)
    subtitle_label.get_style_context().add_class("muted")
    box.pack_start(subtitle_label, False, False, 0)
    return box




def interface_exists(name: str) -> bool:
    if not name:
        return False
    if SYS_NET.exists():
        return (SYS_NET / name).exists()
    try:
        return any(interface_name == name for _, interface_name in socket.if_nameindex())
    except OSError:
        return False


def preferred_scan_interface(interfaces: list[InterfaceInfo]) -> str:
    route_device = preferred_route_interface()
    if route_device and any(info.name == route_device and info.supported for info in interfaces):
        return route_device
    for info in interfaces:
        if info.supported:
            return info.name
    return ""


def preferred_route_interface() -> str:
    routes = ipv4_routes()
    default_routes = [route for route in routes if route["default"] and route["device"]]
    if not default_routes:
        return ""
    default_routes.sort(key=lambda route: route["metric"])
    return default_routes[0]["device"]

def discover_interfaces() -> list[InterfaceInfo]:
    if not SYS_NET.exists():
        try:
            return [
                InterfaceInfo(name=name, supported=True, status="未知")
                for _, name in socket.if_nameindex()
                if name != "lo"
            ]
        except OSError:
            return []

    nm_devices = network_manager_devices()
    interfaces: list[InterfaceInfo] = []

    for item in sorted(SYS_NET.iterdir()):
        name = item.name
        nm_type, nm_state = nm_devices.get(name, ("", ""))
        reason = interface_filter_reason(name, nm_type)
        interfaces.append(
            InterfaceInfo(
                name=name,
                supported=not reason,
                status=interface_status(name, nm_state),
                reason=reason or "",
            )
        )

    return interfaces


def dhcp_interface_info(name: str) -> InterfaceInfo:
    nm_type, nm_state = network_manager_devices().get(name, ("", ""))
    reason = interface_filter_reason(name, nm_type)
    return InterfaceInfo(
        name=name,
        supported=not reason,
        status=interface_status(name, nm_state),
        reason=reason or "",
    )


def discover_scan_interfaces() -> list[InterfaceInfo]:
    if not SYS_NET.exists():
        try:
            return [
                scan_interface_info(name, "")
                for _, name in socket.if_nameindex()
                if name != "lo"
            ]
        except OSError:
            return []

    nm_devices = network_manager_devices()
    return [
        scan_interface_info(item.name, nm_devices.get(item.name, ("", ""))[1])
        for item in sorted(SYS_NET.iterdir())
    ]


def scan_interface_info(name: str, nm_state: str) -> InterfaceInfo:
    path = SYS_NET / name
    network = interface_ipv4_network(name)
    reason = scan_interface_filter_reason(name, network)
    status = interface_status(name, nm_state)
    if network is not None:
        status = f"{status}, {network}"
    return InterfaceInfo(name=name, supported=not reason, status=status, reason=reason or "")


def scan_interface_filter_reason(name: str, network: ipaddress.IPv4Network | None) -> str | None:
    if name == "lo":
        return "loopback"
    if network is None:
        return "没有可扫描 IPv4 网段"
    if network.prefixlen < 23:
        return f"网段过大 {network}"
    return None


def interface_filter_reason(name: str, nm_type: str | None) -> str | None:
    path = SYS_NET / name
    if name == "lo":
        return "loopback"
    if not path.exists():
        return "不存在"

    if (path / "wireless").exists() or (path / "phy80211").exists():
        return "无线网卡"

    if read_text(path / "type") != "1":
        return "非以太网链路"

    if "virtual" in resolved_path(path):
        return "虚拟网口"

    if nm_type and nm_type != "ethernet":
        return f"NetworkManager 类型为 {nm_type}"

    props = udev_properties(path)
    driver = props.get("ID_NET_DRIVER") or Path(resolved_path(path / "device" / "driver")).name
    usb_interfaces = props.get("ID_USB_INTERFACES", "")
    usb_bus = props.get("ID_BUS") == "usb"

    if usb_bus and is_mobile_broadband_interface(driver, usb_interfaces, props):
        model = props.get("ID_MODEL") or props.get("ID_USB_MODEL") or "USB 蜂窝网络设备"
        return f"移动网络设备 {model}"

    return None


def is_mobile_broadband_interface(driver: str, usb_interfaces: str, props: dict[str, str]) -> bool:
    modem_drivers = {
        "cdc_mbim",
        "cdc_ncm",
        "cdc_ether",
        "huawei_cdc_ncm",
        "qmi_wwan",
        "rndis_host",
        "sierra_net",
    }
    usb_classes = (":020600:", ":0a0000:", ":ff0000:")
    has_modem_usb_shape = any(item in usb_interfaces for item in usb_classes)
    modem_candidate = props.get("ID_MM_CANDIDATE") == "1"
    return modem_candidate and driver in modem_drivers and has_modem_usb_shape


def interface_status(name: str, nm_state: str) -> str:
    state = nm_state.lower()
    if state.startswith("connected"):
        return "已连接"
    if state == "unavailable":
        return "不可用"
    if state in {"disconnected", "disconnecting", "connecting", "deactivating"}:
        return "未连接"

    path = SYS_NET / name
    carrier = read_text(path / "carrier")
    operstate = read_text(path / "operstate")
    if carrier == "1" or operstate == "up":
        return "已连接"
    if carrier == "0" or operstate == "down":
        return "未连接"
    return "不可用"


def network_manager_devices() -> dict[str, tuple[str, str]]:
    if shutil.which("nmcli") is None:
        return {}
    result = subprocess.run(
        ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return {}

    devices: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        device, dev_type, state = parts
        if device:
            devices[device] = (dev_type, state)
    return devices


def nmcli_device_status() -> list[dict[str, str]]:
    if shutil.which("nmcli") is None:
        return fallback_device_status()
    result = subprocess.run(
        ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return fallback_device_status()

    devices: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) != 4:
            continue
        device, dev_type, state, connection = parts
        if device:
            devices.append(
                {
                    "device": device,
                    "type": dev_type,
                    "state": state,
                    "connection": connection,
                }
            )
    return devices


def clean_nm_state(state: str) -> str:
    return re.sub(r"\s*\(externally\)\s*", "", state).strip()


def display_nm_state(state: str) -> str:
    state = clean_nm_state(state)
    normalized = state.lower()
    if normalized == "connected":
        return "已连接"
    if normalized == "disconnected":
        return "未连接"
    if normalized == "unavailable":
        return "不可用"
    if normalized == "connecting":
        return "连接中"
    if normalized == "disconnecting":
        return "断开中"
    return state


def running_dhcp_interface() -> str:
    text = read_text(DHCP_CONFIG_FILE)
    for line in text.splitlines():
        if line.startswith("interface="):
            return line.split("=", 1)[1].strip()
    return ""


def dnsmasq_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", f"dnsmasq --conf-file={DHCP_CONFIG_FILE}"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def running_dhcp_network(interface: str) -> ipaddress.IPv4Network | None:
    text = read_text(DHCP_CONFIG_FILE)
    config_interface = ""
    netmask = ""
    pool_start = ""
    for line in text.splitlines():
        if line.startswith("interface="):
            config_interface = line.split("=", 1)[1].strip()
        elif line.startswith("dhcp-range="):
            parts = line.split("=", 1)[1].split(",")
            if len(parts) >= 3:
                pool_start = parts[0].strip()
                netmask = parts[2].strip()

    if not netmask or config_interface != interface:
        return None

    server_ip = interface_ipv4_address(interface)
    if not server_ip or server_ip == "-":
        server_ip = pool_start
    try:
        network = ipaddress.IPv4Network(f"{server_ip}/{netmask}", strict=False)
    except ValueError:
        return None
    if network.prefixlen <= 30:
        return network
    return None


def interface_row_color(state: str, signal: str) -> str:
    normalized = state.lower()
    if "dhcp server" in normalized:
        return "#61d6d6"
    if "不可用" in state or "unavailable" in normalized or "failed" in normalized:
        return "#ff6b5f"
    if "未连接" in state or "disconnect" in normalized:
        return "#8a8f98"
    if signal.startswith("0%"):
        return "#d68a24"
    if "offline" in signal.lower():
        return "#8a8f98"
    if "online" in signal.lower() or "已连接" in state or "connected" in normalized:
        return "#34c759"
    return "#f0f0f0"


def signal_with_bars(signal: str) -> str:
    bars = signal_bars(signal)
    if bars is None:
        return signal
    return f"{signal_bar_icon(bars)} {signal}"


def signal_bar_icon(bars: int) -> str:
    bars = max(0, min(4, bars))
    return "".join("▮" if index < bars else "▯" for index in range(4))


def signal_bars(signal: str) -> int | None:
    if not signal or signal == "-":
        return None
    rsrp_match = re.search(r"\bRSRP\s+(-?\d+(?:\.\d+)?)", signal, re.IGNORECASE)
    if rsrp_match:
        quality = int((float(rsrp_match.group(1)) + 120) * 100 / 40)
        return bars_from_quality(quality)
    rssi_match = re.search(r"\bRSSI\s+(-?\d+(?:\.\d+)?)", signal, re.IGNORECASE)
    if rssi_match:
        quality = int((float(rssi_match.group(1)) + 113) * 100 / 62)
        return bars_from_quality(quality)
    percent_match = re.search(r"\b(\d{1,3})%", signal)
    if percent_match:
        return bars_from_quality(int(percent_match.group(1)))
    return None


def bars_from_quality(quality: int) -> int:
    quality = max(0, min(100, quality))
    if quality == 0:
        return 0
    if quality >= 75:
        return 4
    if quality >= 50:
        return 3
    if quality >= 25:
        return 2
    return 1


def fallback_device_status() -> list[dict[str, str]]:
    if not SYS_NET.exists():
        return []
    return [
        {
            "device": item.name,
            "type": "net",
            "state": interface_status(item.name, ""),
            "connection": "",
        }
        for item in sorted(SYS_NET.iterdir())
    ]


def wifi_signal_by_device() -> dict[str, str]:
    if shutil.which("nmcli") is None:
        return {}
    result = subprocess.run(
        ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,DEVICE", "device", "wifi", "list"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return {}

    signals: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) != 4:
            continue
        in_use, ssid, signal, device = parts
        if in_use == "*" and device:
            signals[device] = f"{signal}%"
    return signals


def modem_signal_by_port() -> dict[str, dict[str, str | bool]]:
    if shutil.which("mmcli") is None:
        return {}
    try:
        list_result = subprocess.run(["mmcli", "-L"], text=True, capture_output=True, timeout=2, check=False)
    except subprocess.TimeoutExpired:
        return {}
    if list_result.returncode != 0:
        return {}

    signals: dict[str, dict[str, str | bool]] = {}
    for modem_id in re.findall(r"/Modem/(\d+)", list_result.stdout):
        try:
            result = subprocess.run(
                ["mmcli", "-m", modem_id, "--output-keyvalue"],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
        except subprocess.TimeoutExpired:
            continue
        if result.returncode != 0:
            continue
        data = parse_key_value_output(result.stdout)
        port = data.get("modem.generic.primary-port", "")
        quality = modem_quality_label(data)
        detail = modem_signal_detail(modem_id)
        access = first_key_value(data, "modem.generic.access-technologies.value")
        operator_name = data.get("modem.3gpp.operator-name", "")
        signal_parts: list[str] = []
        if detail:
            signal_parts.append(detail)
            if quality:
                signal_parts.append(f"({quality})")
        elif quality:
            signal_parts.append(quality)
        connection_parts = []
        if access and access != "--":
            connection_parts.append(access.upper())
        if operator_name and operator_name != "--":
            connection_parts.append(operator_name)
        label = " ".join(signal_parts) if signal_parts else "-"
        connection = " ".join(connection_parts)
        value = {
            "signal": label,
            "connection": connection,
            "connected": modem_data_connected(data),
        }
        if port:
            signals[port] = value
        for netdev in modem_net_devices(data.get("modem.generic.device", "")):
            signals[netdev] = value
    return signals


def modem_data_connected(data: dict[str, str]) -> bool:
    state = data.get("modem.generic.state", "").lower()
    bearers = data.get("modem.generic.bearers", "").strip()
    return state == "connected" or bool(bearers and bearers != "--")


def modem_quality_label(data: dict[str, str]) -> str:
    quality = data.get("modem.generic.signal-quality.value", "")
    recent = data.get("modem.generic.signal-quality.recent", "")
    if not quality or quality == "--" or recent == "no":
        return ""
    return f"{quality}%"


def modem_signal_detail(modem_id: str) -> str:
    try:
        result = subprocess.run(
            ["mmcli", "-m", modem_id, "--signal-get"],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ""
    if result.returncode != 0:
        return ""
    rsrp = signal_metric(result.stdout, "rsrp")
    rsrq = signal_metric(result.stdout, "rsrq")
    rssi = signal_metric(result.stdout, "rssi")
    if rsrp:
        parts = [f"RSRP {rsrp}"]
        if rsrq:
            parts.append(f"RSRQ {rsrq}")
        return ", ".join(parts)
    if rssi:
        return f"RSSI {rssi}"
    return ""


def signal_metric(text: str, name: str) -> str:
    match = re.search(rf"\b{name}\s*:\s*([-.\d]+\s*(?:dBm|dB))", text, re.IGNORECASE)
    return match.group(1) if match else ""


def modem_net_devices(device_path: str) -> list[str]:
    if not device_path or device_path == "--":
        return []
    root = Path(device_path)
    if not root.exists():
        return []
    devices: list[str] = []
    try:
        for path in root.rglob("net"):
            if not path.is_dir():
                continue
            for item in path.iterdir():
                if item.is_dir():
                    devices.append(item.name)
    except OSError:
        return devices
    return sorted(set(devices))


def hidden_duplicate_modem_ports() -> set[str]:
    hidden = hidden_duplicate_modem_ports_from_sysfs()
    if shutil.which("mmcli") is None:
        return hidden
    try:
        list_result = subprocess.run(["mmcli", "-L"], text=True, capture_output=True, timeout=2, check=False)
    except subprocess.TimeoutExpired:
        return hidden
    if list_result.returncode != 0:
        return hidden

    sys_net_names = {path.name for path in SYS_NET.iterdir()} if SYS_NET.exists() else set()
    for modem_id in re.findall(r"/Modem/(\d+)", list_result.stdout):
        try:
            result = subprocess.run(
                ["mmcli", "-m", modem_id, "--output-keyvalue"],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
        except subprocess.TimeoutExpired:
            continue
        if result.returncode != 0:
            continue
        data = parse_key_value_output(result.stdout)
        port = data.get("modem.generic.primary-port", "")
        device_path = data.get("modem.generic.device", "")
        if not port or not device_path or device_path == "--":
            continue
        sibling_netdevs = modem_sibling_net_devices(port, device_path)
        if any(name in sys_net_names for name in sibling_netdevs):
            hidden.add(port)
    return hidden


def hidden_duplicate_modem_ports_from_sysfs() -> set[str]:
    hidden: set[str] = set()
    if not SYS_NET.exists():
        return hidden
    net_roots: list[Path] = []
    for netdev in SYS_NET.iterdir():
        if netdev.name == "lo" or netdev.name.startswith("tailscale"):
            continue
        root = usb_device_root(netdev)
        if root is not None:
            net_roots.append(root)
    if not net_roots:
        return hidden
    for tty_dir in Path("/sys/class/tty").glob("ttyUSB*"):
        tty_root = usb_device_root(tty_dir)
        if tty_root is not None and any(tty_root == net_root for net_root in net_roots):
            hidden.add(tty_dir.name)
    for tty_dir in Path("/sys/class/tty").glob("ttyACM*"):
        tty_root = usb_device_root(tty_dir)
        if tty_root is not None and any(tty_root == net_root for net_root in net_roots):
            hidden.add(tty_dir.name)
    return hidden


def usb_device_root(path: Path) -> Path | None:
    try:
        real = path.resolve()
    except OSError:
        return None
    for parent in [real, *real.parents]:
        if re.fullmatch(r"\d+(?:-\d+(?:\.\d+)*)+", parent.name):
            return parent
    return None


def modem_sibling_net_devices(port: str, device_path: str) -> list[str]:
    root = Path(device_path)
    tty_path = Path(f"/sys/class/tty/{port}")
    if not root.exists() or not tty_path.exists():
        return []
    try:
        tty_path.resolve().relative_to(root)
    except (OSError, ValueError):
        return []
    return modem_net_devices(str(root))


def parse_key_value_output(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip()
    return data


def first_key_value(data: dict[str, str], prefix: str) -> str:
    for key in sorted(data):
        if key.startswith(prefix):
            return data[key]
    return ""


def tailscale_status() -> dict[str, object]:
    if shutil.which("tailscale") is None:
        return {}
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {}
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def tailscale_netcheck() -> dict[str, object]:
    if shutil.which("tailscale") is None:
        return {}
    try:
        result = subprocess.run(
            ["tailscale", "netcheck", "--format=json"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {}
    if result.returncode != 0:
        return {}
    output = result.stdout.strip()
    json_start = output.find("{")
    json_end = output.rfind("}")
    if json_start < 0 or json_end < json_start:
        return {}
    try:
        parsed = json.loads(output[json_start : json_end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def tailscale_summary(status: dict[str, object]) -> str:
    if not status:
        return "-"
    backend = str(status.get("BackendState") or "-")
    self_info = status.get("Self")
    online = ""
    if isinstance(self_info, dict):
        online = "online" if self_info.get("Online") else "offline"
    parts = [part for part in [backend, online] if part and part != "-"]
    return " / ".join(parts) if parts else "-"


def tailscale_tab_markup() -> str:
    status = tailscale_status()
    color = tailscale_status_color(status)
    return f'<span foreground="{color}">●</span> {underlined_markup("TS", "T")}'


def lan_tab_markup(dhcp_running: bool) -> str:
    label = underlined_markup("LAN", "L")
    if not dhcp_running:
        return label
    return f'<span foreground="#34c759">●</span> {label}'


def tailscale_status_color(status: dict[str, object]) -> str:
    if not status:
        return "#8a8f98"
    backend = str(status.get("BackendState") or "").lower()
    self_info = status.get("Self")
    online = isinstance(self_info, dict) and bool(self_info.get("Online"))
    if backend == "running" and online:
        return "#34c759"
    if backend in {"stopped", "no state"}:
        return "#8a8f98"
    if backend == "needslogin" or "login" in backend:
        return "#d68a24"
    if backend in {"running", "starting"}:
        return "#d68a24"
    return "#ff6b5f"


def power_tab_markup() -> str:
    color = power_service_status_color()
    return f'<span foreground="{color}">●</span> {underlined_markup("Pwr", "P")}'


def powersaver_enabled() -> bool:
    enabled = helper_service_config().get("POWERSAVER_ENABLED", "1")
    return enabled.lower() in {"1", "yes", "true", "on", "enabled"}


def mapper_tab_markup() -> str:
    color = user_service_status_color(MAPPER_USER_SERVICE)
    return f'<span foreground="{color}">●</span> {underlined_markup("Map", "M")}'


def power_service_status_color() -> str:
    if shutil.which("systemctl") is None:
        return "#8a8f98"
    active = subprocess.run(
        ["systemctl", "is-active", SYSTEM_SERVICE],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    enabled = subprocess.run(
        ["systemctl", "is-enabled", SYSTEM_SERVICE],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    if active == "active":
        return "#34c759"
    if active == "failed":
        return "#ff6b5f"
    if enabled == "enabled":
        return "#d68a24"
    return "#8a8f98"


def user_service_status_color(service: str) -> str:
    if shutil.which("systemctl") is None:
        return "#8a8f98"
    active = subprocess.run(
        ["systemctl", "--user", "is-active", service],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    enabled = subprocess.run(
        ["systemctl", "--user", "is-enabled", service],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    if active == "active":
        return "#34c759"
    if active == "failed":
        return "#ff6b5f"
    if enabled == "enabled":
        return "#d68a24"
    return "#8a8f98"


def user_service_active(service: str) -> bool:
    if shutil.which("systemctl") is None:
        return False
    result = subprocess.run(
        ["systemctl", "--user", "is-active", service],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() == "active"


def tailscale_admin_summary(status: dict[str, object]) -> str:
    backend = str(status.get("BackendState") or "Unknown")
    current_tailnet = status.get("CurrentTailnet")
    tailnet = ""
    if isinstance(current_tailnet, dict):
        tailnet = str(current_tailnet.get("Name") or "")
    devices = tailscale_devices(status)
    online_count = sum(1 for device in devices if device["status"] in {"Online", "Active"})
    total = len(devices)
    self_info = status.get("Self")
    hostname = ""
    if isinstance(self_info, dict):
        hostname = str(self_info.get("HostName") or "")
    parts = [f"Backend {backend}", f"{online_count}/{total} online"]
    if tailnet:
        parts.append(tailnet)
    if hostname:
        parts.append(hostname)
    return "  |  ".join(parts)


def tailscale_network_summary() -> str:
    route = preferred_route_interface()
    if not route:
        return "Network -"
    devices = nmcli_device_status()
    wifi_signals = wifi_signal_by_device()
    modem_signals = modem_signal_by_port()
    hidden_modem_ports = hidden_duplicate_modem_ports()
    for device in devices:
        name = device["device"]
        if name != route or name in hidden_modem_ports:
            continue
        if device["type"] == "wifi":
            signal = signal_with_bars(wifi_signals.get(name, "-"))
            connection = device["connection"] or "Wi-Fi"
            return f"Network Wi-Fi {name} {connection} {signal}"
        elif device["type"] in {"ethernet", "gsm", "cdma"} or name in modem_signals:
            modem = modem_signals.get(name, {})
            signal = signal_with_bars(str(modem.get("signal") or "-"))
            connection = str(modem.get("connection") or device["connection"] or device["type"])
            return f"Network {name} {connection} {signal}"
        connection = device["connection"] or device["type"] or "-"
        return f"Network {name} {connection}"
    return f"Network {route}"


def tailscale_netcheck_summary(netcheck: dict[str, object]) -> str:
    if not netcheck:
        return ""
    details = tailscale_netcheck_details(netcheck)
    parts = [
        f"UDP {details['udp']}",
        f"v4 {details['ipv4']}",
        f"v6 {details['ipv6']}",
    ]
    if details["derp"] != "-":
        parts.append(f"DERP {details['derp']}")
    parts.append(f"NAT {details['nat']}")
    parts.append(f"Portmap {details['portmap']}")
    if details["global"] != "-":
        parts.append(f"Global {details['global']}")
    if details["portal"] != "No":
        parts.append("Captive portal")
    return "Netcheck " + " / ".join(parts)


def tailscale_netcheck_details(netcheck: dict[str, object]) -> dict[str, str]:
    if not netcheck:
        return {}
    details: dict[str, str] = {
        "udp": "ok" if netcheck.get("UDP") else "blocked",
        "ipv4": "ok" if netcheck.get("IPv4") else "off",
        "ipv6": "ok" if netcheck.get("IPv6") else "off",
        "derp": "-",
        "nat": "varies" if netcheck.get("MappingVariesByDestIP") else "stable",
        "portmap": "off",
        "global": "-",
        "portal": "Yes" if netcheck.get("CaptivePortal") else "No",
    }

    preferred_derp = netcheck.get("PreferredDERP")
    derp_latency = tailscale_derp_latency_ms(netcheck, preferred_derp)
    if preferred_derp:
        derp = str(preferred_derp)
        if derp_latency is not None:
            derp = f"{derp} {derp_latency:.1f}ms"
        details["derp"] = derp

    portmap = [name for name in ("UPnP", "PMP", "PCP") if netcheck.get(name)]
    if portmap:
        details["portmap"] = "/".join(portmap)

    global_v4 = tailscale_global_address(netcheck.get("GlobalV4"))
    global_v6 = tailscale_global_address(netcheck.get("GlobalV6"))
    if global_v4:
        details["global"] = global_v4
    elif global_v6:
        details["global"] = "v6"
    return details


def tailscale_derp_latency_ms(netcheck: dict[str, object], region: object) -> float | None:
    if not region:
        return None
    latencies = netcheck.get("RegionLatency")
    if not isinstance(latencies, dict):
        return None
    latency = latencies.get(str(region))
    if latency is None:
        latency = latencies.get(region)
    if not isinstance(latency, (int, float)):
        return None
    return float(latency) / 1_000_000


def tailscale_global_address(value: object) -> str:
    if not value:
        return ""
    text = str(value)
    if text.startswith("[") and "]:" in text:
        return text[1 : text.find("]")]
    if text.count(":") == 1:
        return text.rsplit(":", 1)[0]
    return text


def tailscale_devices(status: dict[str, object]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    self_info = status.get("Self")
    if isinstance(self_info, dict):
        rows.append(tailscale_device_row(self_info, is_self=True))
    peers = status.get("Peer")
    if isinstance(peers, dict):
        for peer in peers.values():
            if isinstance(peer, dict):
                rows.append(tailscale_device_row(peer, is_self=False))
    return sorted(rows, key=lambda row: (row["status"] != "Online", row["name"].lower()))


def tailscale_device_row(device: dict[str, object], is_self: bool) -> dict[str, str]:
    name = str(device.get("HostName") or device.get("DNSName") or "-")
    if is_self:
        name = f"{name} ⭐"
    os_name = str(device.get("OS") or "-")
    addresses = device.get("TailscaleIPs")
    ipv4, ipv6 = tailscale_ip_pair(addresses)
    dns = str(device.get("DNSName") or "-").rstrip(".")
    online = bool(device.get("Online"))
    active = bool(device.get("Active"))
    status = "Online" if online else "Offline"
    if active:
        status = "Active"
    last_seen = tailscale_time_label(str(device.get("LastSeen") or ""))
    if is_self or online:
        last_seen = "-"
    exit_node = "yes" if bool(device.get("ExitNode")) or bool(device.get("ExitNodeOption")) else "-"
    ping_target = ipv4 if online and not is_self and ipv4 != "-" else "-"
    return {
        "name": name,
        "os": os_name,
        "addresses": ipv4,
        "ping_latency": "-",
        "ping_target": ping_target,
        "status": status,
        "last_seen": last_seen,
        "exit_node": exit_node,
        "ipv4": ipv4,
        "ipv6": ipv6,
        "dns": dns or "-",
    }


def tailscale_ping_latencies(targets: list[str]) -> dict[str, str]:
    ping = shutil.which("tailscale")
    if ping is None:
        return {target: "-" for target in targets}

    def ping_one(target: str) -> tuple[str, str]:
        try:
            result = subprocess.run(
                [ping, "ping", "--c", "1", "--timeout", "1s", target],
                text=True,
                capture_output=True,
                timeout=2.5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return target, "-"
        label = tailscale_ping_latency_label(result.stdout)
        return target, label

    latencies: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(targets)))) as executor:
        futures = [executor.submit(ping_one, target) for target in targets]
        for future in as_completed(futures):
            target, label = future.result()
            latencies[target] = label
    return latencies


def tailscale_ping_latency_label(output: str) -> str:
    match = re.search(r"\bin\s+([0-9.]+)\s*(µs|us|ms|s)\b", output)
    if match is None:
        return "-"
    try:
        value = float(match.group(1))
    except ValueError:
        return "-"
    unit = match.group(2)
    if unit in {"µs", "us"}:
        ms = value / 1000.0
    elif unit == "s":
        ms = value * 1000.0
    else:
        ms = value
    route = " DERP" if "via DERP" in output else ""
    if ms >= 100:
        return f"{ms:.0f} ms{route}"
    if ms >= 10:
        return f"{ms:.1f} ms{route}"
    return f"{ms:.2f} ms{route}"


def tailscale_ip_pair(addresses: object) -> tuple[str, str]:
    ipv4 = "-"
    ipv6 = "-"
    if not isinstance(addresses, list):
        return ipv4, ipv6
    for item in addresses:
        value = str(item)
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.version == 4 and ipv4 == "-":
            ipv4 = value
        elif address.version == 6 and ipv6 == "-":
            ipv6 = value
    return ipv4, ipv6


def tailscale_time_label(value: str) -> str:
    if not value or value.startswith("0001-01-01"):
        return "-"
    return value.replace("T", " ").replace("Z", "").split(".")[0]


def tailscale_row_color(status: str) -> str:
    if status == "Active":
        return "#2b84c6"
    if status == "Online":
        return "#34c759"
    return "#8a8f98"


def dashboard_status(
    cpu_percent: int | None = None,
    net_rates: dict[str, float] | None = None,
) -> dict[str, dict[str, object]]:
    power = power_status()
    memory = memory_metrics()
    storage = storage_metrics()
    cpu = cpu_metrics()
    power_percent = battery_capacity_percent()
    power_meter = power_meter_label(power.get("power", "-"), power_percent)
    if cpu_percent is not None:
        cpu["percent"] = cpu_percent
        cpu["meter"] = f"CPU {cpu_percent}%"
    net_rates = net_rates or {"rx": 0.0, "tx": 0.0}
    return {
        "system": dashboard_item(dashboard_system_summary(), 100, "LIVE"),
        "power": dashboard_item(
            "\n".join(
                [
                    kv_line("TIME", power_time_estimate()),
                    power_watt_line(power.get("power", "-")),
                    kv_line("FREQ", power.get("cpu_freq", "-")),
                    kv_line("CPU", power.get("cpu", "-")),
                ]
            ),
            power_percent,
            power_meter,
        ),
        "cpu": dashboard_item(dashboard_cpu_summary(cpu), int(cpu.get("percent", 0)), cpu.get("meter", "CPU")),
        "memory": dashboard_item(dashboard_memory_summary(memory), int(memory.get("percent", 0)), memory.get("meter", "RAM")),
        "storage": dashboard_item(dashboard_storage_summary(), int(storage.get("percent", 0)), storage.get("meter", "DISK")),
        "network": dashboard_item(
            dashboard_network_summary(net_rates),
            network_activity_percent(net_rates.get("rx", 0.0)),
            f"DOWN {format_rate(net_rates['rx'])}",
            second_percent=network_activity_percent(net_rates.get("tx", 0.0)),
            second_meter=f"UP {format_rate(net_rates['tx'])}",
        ),
        "cellular": dashboard_item(dashboard_cellular_summary(), cellular_meter_percent(), "WWAN"),
    }


def dashboard_item(
    text: str,
    percent: int,
    meter: object,
    hide_meter: bool = False,
    second_percent: int | None = None,
    second_meter: object | None = None,
) -> dict[str, object]:
    return {
        "text": text,
        "percent": percent,
        "meter": str(meter),
        "hide_meter": hide_meter,
        "second_percent": 0 if second_percent is None else second_percent,
        "second_meter": "" if second_meter is None else str(second_meter),
    }


def kv_line(key: str, value: object, width: int = 5) -> str:
    return f"{key:<{width}}  {value}"


def dashboard_system_summary() -> str:
    hostname = socket.gethostname()
    kernel = platform.release()
    uptime = system_uptime_label()
    load = Path("/proc/loadavg").read_text(encoding="utf-8").split()[:3] if Path("/proc/loadavg").exists() else []
    parts = [kv_line("HOST", hostname), kv_line("KERN", kernel), kv_line("UP", uptime)]
    if load:
        parts.append(kv_line("LOAD", " ".join(load)))
    return "\n".join(parts)


def hardware_model() -> str:
    for path in (Path("/proc/device-tree/model"), Path("/sys/firmware/devicetree/base/model")):
        try:
            value = path.read_text(encoding="utf-8").replace("\x00", "").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def system_uptime_label() -> str:
    try:
        seconds = int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
    except (OSError, ValueError, IndexError):
        return "-"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def dashboard_cpu_summary(metrics: dict[str, object] | None = None) -> str:
    metrics = metrics or cpu_metrics()
    config = helper_service_config()
    policy = Path(config.get("POWERSAVER_CPU_POLICY_PATH", "/sys/devices/system/cpu/cpufreq/policy0"))
    current = str(metrics.get("current") or read_first_existing(policy / "scaling_cur_freq", policy / "cpuinfo_cur_freq"))
    governor = read_first_existing(policy / "scaling_governor")
    temp = cpu_temperature_label()
    lines = []
    if current:
        lines.append(kv_line("FREQ", f"{int(current) // 1000} MHz"))
    if governor:
        lines.append(kv_line("GOV", governor))
    if temp:
        lines.append(kv_line("TEMP", temp))
    return "\n".join(lines)


def cpu_metrics() -> dict[str, object]:
    config = helper_service_config()
    policy = Path(config.get("POWERSAVER_CPU_POLICY_PATH", "/sys/devices/system/cpu/cpufreq/policy0"))
    current = read_first_existing(policy / "scaling_cur_freq", policy / "cpuinfo_cur_freq")
    try:
        cur = int(current)
        min_freq = int((policy / "cpuinfo_min_freq").read_text(encoding="utf-8").strip())
        max_freq = int((policy / "cpuinfo_max_freq").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return {"percent": 0, "current": current, "meter": "CPU"}
    total = max_freq - min_freq
    used = max(0, cur - min_freq)
    percent = int(max(0, min(100, used * 100 / total))) if total > 0 else 0
    return {"percent": percent, "current": current, "meter": f"{cur // 1000} MHz"}


def read_cpu_sample() -> tuple[int, int] | None:
    try:
        parts = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
    except (OSError, IndexError):
        return None
    if not parts or parts[0] != "cpu":
        return None
    try:
        values = [int(value) for value in parts[1:]]
    except ValueError:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total, idle


def cpu_usage_percent(previous: tuple[int, int] | None, current: tuple[int, int] | None) -> int | None:
    if previous is None or current is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None
    busy = max(0, total_delta - idle_delta)
    return int(max(0, min(100, busy * 100 / total_delta)))


def process_power_samples() -> dict[int, dict[str, object]]:
    samples: dict[int, dict[str, object]] = {}
    ticks = os.sysconf("SC_CLK_TCK")
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        pid = int(proc.name)
        parsed = parse_proc_stat(read_first_existing(proc / "stat"))
        if parsed is None:
            continue
        name, utime, stime = parsed
        samples[pid] = {
            "name": process_display_name(proc, name),
            "cpu_seconds": (utime + stime) / ticks,
            "io_bytes": process_io_bytes(proc / "io"),
        }
    return samples


def parse_proc_stat(stat: str) -> tuple[str, int, int] | None:
    if not stat:
        return None
    left = stat.find("(")
    right = stat.rfind(")")
    if left == -1 or right == -1 or right <= left:
        return None
    name = stat[left + 1 : right].strip() or "-"
    parts = stat[right + 2 :].split()
    try:
        return name, int(parts[11]), int(parts[12])
    except (IndexError, ValueError):
        return None


def process_display_name(proc: Path, fallback: str) -> str:
    name = read_first_existing(proc / "comm").strip() or fallback
    return name[:40]


def process_io_bytes(path: Path) -> int:
    total = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        key, _separator, value = line.partition(":")
        if key not in {"read_bytes", "write_bytes"}:
            continue
        try:
            total += int(value.strip())
        except ValueError:
            continue
    return total


def app_power_rows(
    previous: dict[int, dict[str, object]],
    current: dict[int, dict[str, object]],
    elapsed: float,
) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for pid, sample in current.items():
        old = previous.get(pid)
        if old is None:
            continue
        cpu_delta = max(0.0, float(sample["cpu_seconds"]) - float(old["cpu_seconds"]))
        io_delta = max(0, int(sample["io_bytes"]) - int(old["io_bytes"]))
        cpu_percent = cpu_delta / elapsed * 100
        io_rate = io_delta / elapsed
        score = cpu_percent + min(50.0, io_rate / 1_000_000)
        if score < APP_POWER_MIN_SCORE:
            continue
        name = str(sample["name"])
        item = grouped.setdefault(name, {"name": name, "pids": [], "cpu": 0.0, "io_rate": 0.0, "score": 0.0})
        item["pids"].append(pid)
        item["cpu"] = float(item["cpu"]) + cpu_percent
        item["io_rate"] = float(item["io_rate"]) + io_rate
        item["score"] = float(item["score"]) + score

    rows: list[dict[str, object]] = []
    for item in grouped.values():
        pids = sorted(item["pids"])
        pid_label = str(pids[0]) if len(pids) == 1 else f"{pids[0]} +{len(pids) - 1}"
        rows.append(
            {
                "name": item["name"],
                "pid": pid_label,
                "cpu": float(item["cpu"]),
                "io_rate": float(item["io_rate"]),
                "score": float(item["score"]),
            }
        )
    rows.sort(key=lambda row: float(row["score"]), reverse=True)
    return rows


def format_bytes_per_second(value: float) -> str:
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B/s":
                return f"{amount:.0f} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} GB/s"


def read_first_existing(*paths: Path) -> str:
    for path in paths:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


def cpu_temperature_label() -> str:
    for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        try:
            milli_c = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if milli_c > 0:
            return f"{milli_c / 1000:.1f} C"
    return ""


def dashboard_memory_summary(metrics: dict[str, object] | None = None) -> str:
    metrics = metrics or memory_metrics()
    lines = [str(metrics.get("ram", "-"))]
    swap = str(metrics.get("swap", ""))
    if swap:
        lines.append(swap)
    return "\n".join(lines)


def memory_metrics() -> dict[str, object]:
    info: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
    except (OSError, ValueError):
        return "-"
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    used = max(0, total - available)
    ram = metric_line("RAM", used, total, unit="kib")
    percent = int(max(0, min(100, used * 100 / total))) if total else 0
    swap = ""
    if swap_total:
        swap = metric_line("SWAP", swap_total - swap_free, swap_total, unit="kib")
    return {"percent": percent, "ram": ram, "swap": swap, "meter": f"RAM {percent}%"}


def metric_line(label: str, used: int, total: int, unit: str) -> str:
    if total <= 0:
        return f"{label:<6} -"
    percent = int(max(0, min(100, used * 100 / total)))
    if unit == "kib":
        used_text = format_kib(used)
        total_text = format_kib(total)
    else:
        used_text = format_bytes(used)
        total_text = format_bytes(total)
    return f"{compact_label(label):<12} {percent:3d}% {used_text:>9}/{total_text:<9}"


def format_kib(kib: int) -> str:
    if kib >= 1024 * 1024:
        return f"{kib / 1024 / 1024:.1f} GiB"
    return f"{kib / 1024:.0f} MiB"


def dashboard_storage_summary() -> str:
    mounts = storage_mounts()
    rows = []
    for item in mounts[:4]:
        rows.append(metric_line(item["label"], int(item["used"]), int(item["total"]), unit="bytes"))
    return "\n".join(rows) if rows else "-"


def storage_metrics() -> dict[str, object]:
    mounts = storage_mounts()
    if not mounts:
        return {"percent": 0, "meter": "DISK"}
    root = next((item for item in mounts if item["mount_point"] == "/"), mounts[0])
    return {"percent": int(root["percent"]), "meter": f"/ {root['percent']}%"}


def storage_mounts() -> list[dict[str, object]]:
    mounts: dict[str, dict[str, object]] = {}
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        item = parse_mountinfo_line(line)
        if item is None:
            continue
        source = str(item["source"])
        mount_point = str(item["mount_point"])
        candidate = storage_usage_item(source, mount_point)
        if candidate is None:
            continue
        storage_key = mount_point if mount_point == "/" else source
        current = mounts.get(storage_key)
        if current is None or storage_mount_sort_key(candidate) < storage_mount_sort_key(current):
            mounts[storage_key] = candidate
    return sorted(mounts.values(), key=storage_mount_sort_key)


def storage_usage_item(source: str, mount_point: str) -> dict[str, object] | None:
    try:
        usage = shutil.disk_usage(mount_point)
    except OSError:
        return None
    if usage.total <= 0:
        return None
    used = usage.total - usage.free
    percent = int(max(0, min(100, used * 100 / usage.total)))
    return {
        "source": source,
        "mount_point": mount_point,
        "label": storage_label(mount_point),
        "used": used,
        "total": usage.total,
        "percent": percent,
        "rank": storage_mount_rank(mount_point),
    }


def parse_mountinfo_line(line: str) -> dict[str, str] | None:
    parts = line.split()
    if " - " not in line:
        return None
    separator = parts.index("-")
    if separator + 3 > len(parts):
        return None
    mount_point = mountinfo_unescape(parts[4])
    if mount_point == "/boot/firmware":
        return None
    fs_type = parts[separator + 1]
    source = parts[separator + 2]
    if not real_storage_source(source, fs_type, mount_point):
        return None
    return {"mount_point": mount_point, "fs_type": fs_type, "source": source}


def mountinfo_unescape(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def real_storage_source(source: str, fs_type: str, mount_point: str) -> bool:
    if fs_type in {"tmpfs", "devtmpfs", "proc", "sysfs", "overlay", "cgroup", "cgroup2", "autofs", "devpts"}:
        return False
    if fs_type in {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs", "fuse.rclone"}:
        return mount_point.startswith(("/mnt/", "/media/")) or mount_point in {"/mnt", "/media"}
    return source == "/dev/root" or source.startswith(("/dev/mmcblk", "/dev/nvme", "/dev/sd", "/dev/dm-"))


def storage_label(mount_point: str) -> str:
    if mount_point in {"/", "/boot", "/boot/firmware"}:
        return mount_point
    for prefix in ("/media/", "/mnt/"):
        if mount_point.startswith(prefix):
            return mount_point
    return mount_point


def compact_label(label: str) -> str:
    if label.startswith("/mnt/"):
        return label.removeprefix("/mnt/")
    if label.startswith("/media/"):
        return label.removeprefix("/media/")
    return label


def storage_mount_rank(mount_point: str) -> int:
    ranks = {"/": 0, "/home": 1, "/boot/firmware": 2, "/boot": 3}
    if mount_point in ranks:
        return ranks[mount_point]
    if mount_point.startswith("/media/"):
        return 4
    if mount_point.startswith("/mnt/"):
        return 5
    return 10


def storage_mount_sort_key(item: dict[str, object]) -> tuple[int, int, str]:
    mount_point = str(item["mount_point"])
    return int(item["rank"]), len(mount_point), mount_point


def format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    if size >= 100:
        return f"{size:.0f} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def dashboard_network_summary(rates: dict[str, float] | None = None) -> str:
    rates = rates or {"rx": 0.0, "tx": 0.0}
    devices = nmcli_device_status()
    wifi_signals = wifi_signal_by_device()
    connected = [
        item
        for item in devices
        if clean_nm_state(item["state"]).startswith("connected") and dashboard_network_device_visible(item)
    ]
    route = preferred_route_interface() or "-"
    lines = [kv_line("DEF", route), kv_line("CONN", len(connected))]
    for item in connected[:4]:
        connection = item["connection"] or "-"
        detail = f"{item['type']} / {connection}"
        if item["type"] == "wifi":
            signal = wifi_signals.get(item["device"], "")
            bars = signal_bars(signal)
            if bars is not None:
                detail = f"{signal_bar_icon(bars)} {detail}"
        lines.append(kv_line(item["device"][:5], detail))
    return "\n".join(lines)


def read_network_sample() -> tuple[float, int, int]:
    rx_total = 0
    tx_total = 0
    for path in Path("/sys/class/net").iterdir():
        if path.name == "lo" or path.name.startswith("tailscale"):
            continue
        operstate = read_first_existing(path / "operstate")
        if operstate and operstate == "down":
            continue
        try:
            rx_total += int((path / "statistics" / "rx_bytes").read_text(encoding="utf-8").strip())
            tx_total += int((path / "statistics" / "tx_bytes").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
    return time.time(), rx_total, tx_total


def dashboard_network_device_visible(device: dict[str, str]) -> bool:
    name = device.get("device", "")
    dev_type = device.get("type", "")
    if name == "lo" or name.startswith("tailscale"):
        return False
    if dev_type in {"loopback", "tun"}:
        return False
    return True


def network_rates(
    previous: tuple[float, int, int] | None,
    current: tuple[float, int, int],
) -> dict[str, float]:
    if previous is None:
        return {"rx": 0.0, "tx": 0.0}
    elapsed = current[0] - previous[0]
    if elapsed <= 0:
        return {"rx": 0.0, "tx": 0.0}
    return {
        "rx": max(0.0, (current[1] - previous[1]) / elapsed),
        "tx": max(0.0, (current[2] - previous[2]) / elapsed),
    }


def network_activity_percent(bytes_per_sec: float) -> int:
    if bytes_per_sec <= 0:
        return 0
    if bytes_per_sec < 64 * 1024:
        return 10
    if bytes_per_sec < 512 * 1024:
        return 30
    if bytes_per_sec < 2 * 1024 * 1024:
        return 55
    if bytes_per_sec < 8 * 1024 * 1024:
        return 75
    return 100


def format_rate(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / 1024 / 1024:.1f} MiB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.0f} KiB/s"
    return f"{bytes_per_sec:.0f} B/s"


def dashboard_cellular_summary() -> str:
    wwan = current_wwan_summary()
    if wwan.lower() in {"disabled", "off", "已禁用"}:
        return "Off"
    modems = modem_signal_by_port()
    hidden_ports = hidden_duplicate_modem_ports()
    visible_modems = [(port, info) for port, info in sorted(modems.items()) if port not in hidden_ports]
    if not visible_modems:
        return "-"
    return cellular_operator_summary(visible_modems[0][1])


def cellular_operator_summary(info: dict[str, str | bool]) -> str:
    connection = str(info.get("connection") or "-")
    signal = str(info.get("signal") or "-")
    bars = signal_bars(signal)
    if bars is None:
        return connection
    return f"{signal_bar_icon(bars)} {connection}"


def compact_cellular_signal(signal: str) -> str:
    if signal == "-":
        return signal
    percent = re.search(r"(\d{1,3})%", signal)
    rsrp = re.search(r"RSRP\s+(-?\d+(?:\.\d+)?)", signal, re.IGNORECASE)
    rsrq = re.search(r"RSRQ\s+(-?\d+(?:\.\d+)?)", signal, re.IGNORECASE)
    rssi = re.search(r"RSSI\s+(-?\d+(?:\.\d+)?)", signal, re.IGNORECASE)
    parts = []
    if rsrp:
        parts.append(short_number(rsrp.group(1)))
    if rsrq:
        parts.append(short_number(rsrq.group(1)))
    if not parts and rssi:
        parts.append(short_number(rssi.group(1)))
    if percent:
        parts.append(f"{percent.group(1)}%")
    compact = "/".join(parts) if parts else signal
    bars = signal_bars(signal)
    return f"{signal_bar_icon(bars)} {compact}" if bars is not None else compact


def short_number(value: str) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def cellular_meter_percent() -> int:
    wwan = current_wwan_summary()
    if wwan.lower() in {"disabled", "off", "已禁用"}:
        return 0
    modems = modem_signal_by_port()
    hidden_ports = hidden_duplicate_modem_ports()
    best = 0
    for port, info in modems.items():
        if port in hidden_ports:
            continue
        bars = signal_bars(str(info.get("signal") or ""))
        if bars is not None:
            best = max(best, int(bars * 100 / 4))
    return best


def power_status() -> dict[str, str]:
    config = helper_service_config()
    power = current_power_state(config)
    wwan = current_wwan_summary()
    if wwan.lower() == "enabled":
        wwan = power_wwan_signal_summary()
    return {
        "time": power_time_estimate(),
        "watts": realtime_power_label(power),
        "sleep": power_screen_timeout_summary(config, power),
        "power": power,
        "cpu_freq": current_cpu_freq_summary(config),
        "cpu": current_cpu_summary(config),
        "wwan": wwan,
        "powersaver": powersaver_config_summary(config),
    }


def power_wwan_signal_summary() -> str:
    modems = modem_signal_by_port()
    hidden_ports = hidden_duplicate_modem_ports()
    visible_modems = [(port, info) for port, info in sorted(modems.items()) if port not in hidden_ports]
    if not visible_modems:
        return "-"
    return cellular_operator_summary(visible_modems[0][1])


def desktop_shortcut_rows() -> list[dict[str, str]]:
    config = toml_file(MAPPER_DESKTOP_KEYBINDS_CONFIG)
    rows: list[dict[str, str]] = []
    for item in nested_binding_list(config, "rightshift"):
        rows.append(
            {
                "scope": "rightshift",
                "key": str(item.get("key", "")),
                "action": str(item.get("command", "")),
            }
        )
    for item in nested_binding_list(config, "labwc"):
        action = str(item.get("command") or item.get("action") or "")
        rows.append(
            {
                "scope": "labwc",
                "key": str(item.get("key", "")),
                "action": action,
            }
        )
    return rows


def mapper_binding_rows() -> list[dict[str, str]]:
    config = toml_file(MAPPER_CONFIG)
    rows: list[dict[str, str]] = []
    for device in ("gamepad", "keyboard"):
        for item in nested_binding_list(config, device):
            rows.append(
                {
                    "device": device,
                    "buttons": ", ".join(str(value) for value in item.get("buttons", [])),
                    "action": binding_action_text(item),
                }
            )
    mouse = config.get("mouse", {})
    if isinstance(mouse, dict):
        for item in mouse.get("remaps", []):
            if isinstance(item, dict):
                rows.append(
                    {
                        "device": "mouse",
                        "buttons": str(item.get("from", "")),
                        "action": f"emit {item.get('to', '')}",
                    }
                )
    return rows


def toml_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def nested_binding_list(config: dict[str, object], section: str) -> list[dict[str, object]]:
    data = config.get(section, {})
    if not isinstance(data, dict):
        return []
    bindings = data.get("bindings", [])
    return [item for item in bindings if isinstance(item, dict)]


def binding_action_text(item: dict[str, object]) -> str:
    hold_ms = item.get("hold_ms")
    prefix = f"hold {hold_ms} " if hold_ms else ""
    for key in ("command", "text", "press_command", "release_command", "emit_key", "emit_rel"):
        value = item.get(key)
        if value:
            if key == "text":
                suffix = " enter" if item.get("press_enter") else ""
                return f"{prefix}text {value}{suffix}"
            if key == "press_command":
                release = item.get("release_command")
                return prefix + f"press {value}" + (f" / release {release}" if release else "")
            if key == "command":
                return prefix + f"command {value}"
            return prefix + str(value)
    return "-"


def desktop_keybinds_text(rows: list[dict[str, str]]) -> str:
    lines = [
        "# Declarative desktop shortcut config.",
        "# rightshift rows generate keyd bindings; labwc rows generate compositor bindings.",
        "",
    ]
    for row in rows:
        scope = row["scope"].strip().lower()
        key = row["key"].strip()
        action = row["action"].strip()
        if not key or not action:
            continue
        if scope in {"rightshift", "right shift", "keyd"}:
            lines.extend(
                [
                    "[[rightshift.bindings]]",
                    f'key = "{toml_escape(key)}"',
                    f'command = "{toml_escape(action)}"',
                    "",
                ]
            )
        elif scope == "labwc":
            lines.extend(["[[labwc.bindings]]", f'key = "{toml_escape(key)}"'])
            if action.startswith("~") or "/" in action or " " in action:
                lines.append(f'command = "{toml_escape(action)}"')
            else:
                lines.append(f'action = "{toml_escape(action)}"')
            lines.append("")
    return "\n".join(lines)


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def mapper_config_text(rows: list[dict[str, str]]) -> str:
    grouped: dict[str, list[dict[str, str]]] = {"gamepad": [], "keyboard": [], "mouse": []}
    for row in rows:
        device = row["device"].strip().lower()
        if device in grouped:
            grouped[device].append(row)
    lines = [
        "[general]",
        "rescan_seconds = 3.0",
        "",
        "[gamepad]",
        'device_name_patterns = ["ClockworkPI uConsole"]',
        "debounce_ms = 250",
        "",
    ]
    for row in grouped["gamepad"]:
        lines.extend(binding_toml_block("gamepad", row))
    lines.extend(
        [
            "[keyboard]",
            "enabled = true",
            "grab = false",
            'device_name_patterns = ["ClockworkPI uConsole Keyboard", "keyd virtual keyboard"]',
            "debounce_ms = 50",
            "repeat_rate = 20",
            "repeat_delay_ms = 600",
            "",
        ]
    )
    for row in grouped["keyboard"]:
        lines.extend(binding_toml_block("keyboard", row))
    lines.extend(
        [
            "[lock]",
            "enabled = false",
            'key = "KEY_COFFEE"',
            'lock_command = "sudo -n /usr/local/bin/uconsole-helper-mapper-display-control off"',
            'unlock_command = "sudo -n /usr/local/bin/uconsole-helper-mapper-display-control on"',
            'keyboard_backlight_script = "~/WorkSpace/uconsole-keyboard/tools/keyboard_state.sh"',
            "",
            "[power_button]",
            "enabled = false",
            'device_name_patterns = ["axp20x-pek"]',
            "hold_ms = 700",
            "",
            "[mouse]",
            "enabled = true",
            "grab = true",
            "device_name_patterns = []",
            "",
        ]
    )
    for row in grouped["mouse"]:
        buttons = row["buttons"].strip()
        action = row["action"].strip()
        target = action.removeprefix("emit ").strip()
        if buttons and target:
            lines.extend(
                [
                    "[[mouse.remaps]]",
                    f'from = "{toml_escape(buttons)}"',
                    f'to = "{toml_escape(target)}"',
                    "",
                ]
            )
    return "\n".join(lines)


def binding_toml_block(section: str, row: dict[str, str]) -> list[str]:
    buttons = [part.strip() for part in row["buttons"].split(",") if part.strip()]
    action = parse_mapper_action(row["action"])
    if not buttons or not action:
        return []
    lines = [f"[[{section}.bindings]]", f"buttons = [{', '.join(toml_string(button) for button in buttons)}]"]
    hold_ms = action.pop("hold_ms", "")
    if hold_ms:
        lines.append(f"hold_ms = {hold_ms}")
    press_enter = action.pop("press_enter", "")
    for key, value in action.items():
        if key in {"emit_rel_value", "repeat_ms"}:
            lines.append(f"{key} = {value}")
        else:
            lines.append(f"{key} = {toml_string(value)}")
    if press_enter:
        lines.append("press_enter = true")
    lines.append("")
    return lines


def parse_mapper_action(value: str) -> dict[str, str]:
    text = value.strip()
    if not text or text == "-":
        return {}
    result: dict[str, str] = {}
    if text.startswith("hold "):
        parts = text.split(maxsplit=2)
        if len(parts) == 3 and parts[1].isdigit():
            result["hold_ms"] = parts[1]
            text = parts[2].strip()
    if text.startswith("command "):
        result["command"] = text.removeprefix("command ").strip()
        return result
    if text.startswith("text "):
        payload = text.removeprefix("text ").strip()
        if payload.endswith(" enter"):
            result["text"] = payload.removesuffix(" enter").strip()
            result["press_enter"] = "true"
            return result
        result["text"] = payload
        return result
    if text.startswith("press ") and " / release " in text:
        press, release = text.removeprefix("press ").split(" / release ", 1)
        result["press_command"] = press.strip()
        result["release_command"] = release.strip()
        return result
    if text.startswith("emit "):
        result["emit_key"] = text.removeprefix("emit ").strip()
        return result
    if text.startswith("rel "):
        parts = text.split()
        if len(parts) >= 3:
            result["emit_rel"] = parts[1]
            result["emit_rel_value"] = parts[2]
            return result
    result["command"] = text
    return result


def toml_string(value: str) -> str:
    return f'"{toml_escape(value)}"'


def helper_service_config() -> dict[str, str]:
    defaults = {
        "POWERSAVER_ENABLED": "1",
        "POWERSAVER_MODE": "balanced",
        "POWERSAVER_BATTERY_CPU_FREQ": "1500,1500",
        "POWERSAVER_AC_CPU_FREQ": "restore",
        "POWERSAVER_ECO_BATTERY_CPU_FREQ": "1500,1500",
        "POWERSAVER_ECO_AC_CPU_FREQ": "1500,1500",
        "POWERSAVER_ECO_BATTERY_SCREEN_TIMEOUT_SEC": "0",
        "POWERSAVER_ECO_AC_SCREEN_TIMEOUT_SEC": "0",
        "POWERSAVER_ECO_UNKNOWN_POWER_ACTION": "restore",
        "POWERSAVER_ECO_WWAN_POLICY": "ondemand",
        "POWERSAVER_ECO_AUTO_BRIGHTNESS": "0",
        "POWERSAVER_BALANCED_BATTERY_CPU_FREQ": "1500,1500",
        "POWERSAVER_BALANCED_AC_CPU_FREQ": "restore",
        "POWERSAVER_BALANCED_BATTERY_SCREEN_TIMEOUT_SEC": "0",
        "POWERSAVER_BALANCED_AC_SCREEN_TIMEOUT_SEC": "0",
        "POWERSAVER_BALANCED_UNKNOWN_POWER_ACTION": "restore",
        "POWERSAVER_BALANCED_WWAN_POLICY": "ondemand",
        "POWERSAVER_BALANCED_AUTO_BRIGHTNESS": "0",
        "POWERSAVER_PERFORMANCE_BATTERY_CPU_FREQ": "1500,2400",
        "POWERSAVER_PERFORMANCE_AC_CPU_FREQ": "restore",
        "POWERSAVER_PERFORMANCE_BATTERY_SCREEN_TIMEOUT_SEC": "0",
        "POWERSAVER_PERFORMANCE_AC_SCREEN_TIMEOUT_SEC": "0",
        "POWERSAVER_PERFORMANCE_UNKNOWN_POWER_ACTION": "restore",
        "POWERSAVER_PERFORMANCE_WWAN_POLICY": "ondemand",
        "POWERSAVER_PERFORMANCE_AUTO_BRIGHTNESS": "0",
        "POWERSAVER_UNKNOWN_POWER_ACTION": "restore",
        "POWERSAVER_WWAN_POLICY": "ondemand",
        "POWERSAVER_POLL_INTERVAL_SEC": "5",
        "POWERSAVER_CPU_POLICY_PATH": "/sys/devices/system/cpu/cpufreq/policy0",
        "POWERSAVER_POWER_SUPPLY_DIR": "/sys/class/power_supply",
        "MCU_LED_BATTERY_ENABLED": "1",
        "MCU_LED_LXTERMINAL_BELL_ENABLED": "1",
        "MCU_LED_NIGHT_MODE_ENABLED": "1",
    }
    if not SERVICE_CONFIG.exists():
        return defaults
    values = defaults.copy()
    try:
        text = SERVICE_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def system_service_summary(service: str) -> str:
    if shutil.which("systemctl") is None:
        return "systemctl unavailable"
    active = subprocess.run(
        ["systemctl", "is-active", service],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    enabled = subprocess.run(
        ["systemctl", "is-enabled", service],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    return " / ".join(part for part in [active or "unknown", enabled or "unknown"] if part)


def env_config(path: Path, defaults: dict[str, str]) -> dict[str, str]:
    values = defaults.copy()
    if not path.exists():
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def default_asr_config() -> dict[str, str]:
    return {
        "ASR_URL": "http://dgx-spark.tail97583.ts.net:3300/api/asr/transcriptions",
        "ASR_FINALIZE_TEXT_URL": "http://dgx-spark.tail97583.ts.net:3300/api/asr/transcriptions/finalize-text",
        "ASR_PREVIEW_WS_URL": "ws://dgx-spark.tail97583.ts.net:3300/api/asr-preview/ws",
        "ASR_AUTH_TOKEN": "",
        "ASR_LANGUAGE": "zh",
        "ASR_CORRECTION_MODE": "auto",
        "ASR_TIMEOUT": "90",
        "ASR_REQUEST_ATTEMPT_TIMEOUT": "75",
        "ASR_CONNECT_TIMEOUT": "2",
        "ASR_RETRY_COUNT": "2",
        "ASR_RETRY_DELAY": "0.35",
        "ASR_PREVIEW_FINAL_WAIT_SECONDS": "1.5",
        "ASR_PREVIEW_WS_TIMEOUT": "2",
        "VOICE_RECORDER": "arecord",
        "VOICE_INPUT": "default",
        "VOICE_OUTPUT_MODE": "paste",
        "VOICE_TMUX_OUTPUT_MODE": "type",
        "VOICE_PASTE_BACKEND": "uinput",
        "VOICE_TMUX_CONTEXT": "1",
        "VOICE_QWEN_ASR_STREAMING": "1",
    }


def asr_config_text(values: dict[str, str]) -> str:
    current_values = env_config(MAPPER_ASR_CONFIG, {})
    merged_values = current_values.copy()
    merged_values.update(values)
    asr_defaults = {
        "VOICE_STREAM_PREVIEW": "1",
        "VOICE_STREAM_SEND_INTERVAL_MS": "50",
        "VOICE_STREAM_NOTIFY_FROM_READER": "1",
    }
    for key, value in asr_defaults.items():
        merged_values.setdefault(key, value)
    lines = [
        "ASR_URL={ASR_URL}",
        "ASR_PREVIEW_WS_URL={ASR_PREVIEW_WS_URL}",
        "ASR_FINALIZE_TEXT_URL={ASR_FINALIZE_TEXT_URL}",
        "ASR_PREVIEW_FINAL_WAIT_SECONDS={ASR_PREVIEW_FINAL_WAIT_SECONDS}",
        "ASR_PREVIEW_WS_TIMEOUT={ASR_PREVIEW_WS_TIMEOUT}",
        "ASR_LANGUAGE={ASR_LANGUAGE}",
        "ASR_AUTH_TOKEN={ASR_AUTH_TOKEN}",
        "ASR_CORRECTION_MODE={ASR_CORRECTION_MODE}",
        "ASR_NO_PROXY=1",
        "ASR_TIMEOUT={ASR_TIMEOUT}",
        "ASR_REQUEST_ATTEMPT_TIMEOUT={ASR_REQUEST_ATTEMPT_TIMEOUT}",
        "ASR_CONNECT_TIMEOUT={ASR_CONNECT_TIMEOUT}",
        "ASR_RETRY_COUNT={ASR_RETRY_COUNT}",
        "ASR_RETRY_DELAY={ASR_RETRY_DELAY}",
        "VOICE_RECORDER={VOICE_RECORDER}",
        "VOICE_INPUT={VOICE_INPUT}",
        "VOICE_MIN_RECORD_MS=350",
        "VOICE_MAX_RECORD_MS=60000",
        "VOICE_SAMPLE_RATE=16000",
        "VOICE_CHANNELS=1",
        "VOICE_OUTPUT_MODE={VOICE_OUTPUT_MODE}",
        "VOICE_TMUX_OUTPUT_MODE={VOICE_TMUX_OUTPUT_MODE}",
        "VOICE_WECHAT_OUTPUT_MODE=paste",
        "VOICE_PASTE_BACKEND={VOICE_PASTE_BACKEND}",
        "VOICE_PASTE_SHORTCUT=ctrl_v",
        "VOICE_WECHAT_PASTE_SHORTCUT=ctrl_v",
        "VOICE_KEEP_AUDIO=0",
        "VOICE_NOTIFY_USE_MARKUP=0",
        "VOICE_NOTIFY_FONT_SIZE=22",
        "VOICE_NOTIFY_PADDING_LINES=1",
        "VOICE_TMUX_CONTEXT={VOICE_TMUX_CONTEXT}",
        "VOICE_STREAM_PREVIEW={VOICE_STREAM_PREVIEW}",
        "VOICE_QWEN_ASR_STREAMING={VOICE_QWEN_ASR_STREAMING}",
        "VOICE_STREAM_SEND_INTERVAL_MS={VOICE_STREAM_SEND_INTERVAL_MS}",
        "VOICE_STREAM_NOTIFY_FROM_READER={VOICE_STREAM_NOTIFY_FROM_READER}",
        "",
    ]
    emitted_keys = {
        line.split("=", 1)[0]
        for line in lines
        if line and not line.startswith("#") and "=" in line
    }
    extra_lines = [
        f"{key}={value}"
        for key, value in sorted(merged_values.items())
        if key not in emitted_keys
    ]
    return "\n".join(line.format(**merged_values) for line in lines + extra_lines)


def audio_input_options() -> list[str]:
    options = ["Default"]
    options.extend(pactl_source_names())
    options.extend(arecord_device_names())
    result: list[str] = []
    seen: set[str] = set()
    for option in options:
        if option and option not in seen:
            result.append(option)
            seen.add(option)
    return result


def pactl_source_names() -> list[str]:
    if shutil.which("pactl") is None:
        return []
    result = subprocess.run(["pactl", "list", "short", "sources"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return []
    names: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not parts[1].endswith(".monitor"):
            names.append(parts[1])
    return names


def arecord_device_names() -> list[str]:
    if shutil.which("arecord") is None:
        return []
    result = subprocess.run(["arecord", "-L"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return []
    names: list[str] = []
    for line in result.stdout.splitlines():
        name = line.strip()
        if (
            name
            and not line.startswith(" ")
            and name not in {"null", "default"}
            and (name.startswith(("hw:", "plughw:", "sysdefault:", "front:", "usbstream:")) or "CARD=" in name)
        ):
            names.append(name)
    return names


def current_power_state(config: dict[str, str]) -> str:
    power_dir = Path(config.get("POWERSAVER_POWER_SUPPLY_DIR", "/sys/class/power_supply"))
    if not power_dir.is_dir():
        return "unknown"
    has_battery = False
    ac_online = False
    for path in sorted(power_dir.iterdir()):
        type_path = path / "type"
        if not path.is_dir() or not type_path.is_file():
            continue
        try:
            supply_type = type_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if supply_type == "Battery" and power_supply_present(path):
            has_battery = True
            status_path = path / "status"
            if status_path.is_file():
                try:
                    if status_path.read_text(encoding="utf-8").strip().lower() in {"charging", "full"}:
                        ac_online = True
                except OSError:
                    pass
        if supply_type in {"Mains", "USB", "USB_C", "USB_PD", "USB_DCP", "USB_CDP"}:
            if power_supply_online(path) is True:
                ac_online = True
    if ac_online:
        return "AC"
    if has_battery:
        return "Battery"
    return "Unknown"


def power_time_estimate() -> str:
    for battery in battery_supplies():
        status = read_first_existing(battery / "status").lower()
        energy_now = read_number(battery / "energy_now")
        energy_full = read_number(battery / "energy_full", battery / "energy_full_design")
        if energy_now is not None and energy_full is not None and energy_full > 0:
            rate = battery_power_rate(battery)
            if rate is None or rate <= 0:
                return "-"
            if status == "discharging":
                return f"LEFT {format_hours(energy_now / rate)}"
            if status in {"charging", "full"}:
                if status == "full":
                    return "FULL"
                if energy_full > energy_now:
                    return f"FULL {format_hours((energy_full - energy_now) / rate)}"
                return "-"
            return "-"

        charge_now = read_number(battery / "charge_now")
        charge_full = read_number(battery / "charge_full", battery / "charge_full_design")
        current_now = read_number(battery / "current_now")
        if charge_now is not None and charge_full is not None and current_now is not None and current_now != 0:
            current_now = abs(current_now)
            if status == "discharging":
                return f"LEFT {format_hours(charge_now / current_now)}"
            if status in {"charging", "full"}:
                if status == "full":
                    return "FULL"
                if charge_full > charge_now:
                    return f"FULL {format_hours((charge_full - charge_now) / current_now)}"
                return "-"
        return "-"
    return "-"


def battery_supplies() -> list[Path]:
    power_dir = Path("/sys/class/power_supply")
    if not power_dir.is_dir():
        return []
    batteries: list[Path] = []
    for path in sorted(power_dir.iterdir()):
        if path.is_dir() and read_first_existing(path / "type") == "Battery" and power_supply_present(path):
            batteries.append(path)
    return batteries


def read_number(*paths: Path) -> float | None:
    for path in paths:
        value = read_first_existing(path)
        if not value:
            continue
        try:
            return float(value)
        except ValueError:
            continue
    return None


def battery_power_rate(battery: Path) -> float | None:
    power_now = read_number(battery / "power_now")
    if power_now is not None and power_now > 0:
        return power_now
    current_now = read_number(battery / "current_now")
    if current_now is None or current_now == 0:
        return None
    voltage_now = read_number(battery / "voltage_now")
    if voltage_now is not None and voltage_now > 0:
        return abs(current_now) * voltage_now / 1_000_000
    return abs(current_now)


def battery_power_label() -> str:
    for battery in battery_supplies():
        rate = battery_power_rate(battery)
        if rate is not None and rate > 0:
            return f"{rate / 1_000_000:.2f} W"
    return "-"


def ac_power_label() -> str:
    for supply in mains_supplies():
        rate = read_number(supply / "power_now")
        if rate is not None and rate > 0:
            return f"{rate / 1_000_000:.2f} W"
        current = read_number(supply / "current_now", supply / "input_current_now")
        voltage = read_number(supply / "voltage_now", supply / "input_voltage_now")
        if current is not None and voltage is not None and current != 0 and voltage > 0:
            return f"{abs(current) * voltage / 1_000_000_000_000:.2f} W"
    return "-"


def mains_supplies() -> list[Path]:
    power_dir = Path("/sys/class/power_supply")
    if not power_dir.is_dir():
        return []
    supplies: list[Path] = []
    for path in sorted(power_dir.iterdir()):
        supply_type = read_first_existing(path / "type")
        if path.is_dir() and supply_type in {"Mains", "USB", "USB_C", "USB_PD", "USB_DCP", "USB_CDP"}:
            if power_supply_online(path) is True:
                supplies.append(path)
    return supplies


def battery_capacity_label() -> str:
    capacity = battery_capacity_percent()
    if capacity >= 0:
        return f"{capacity}%"
    return "-"


def battery_capacity_percent() -> int:
    for battery in battery_supplies():
        capacity = read_number(battery / "capacity")
        if capacity is not None:
            return int(max(0, min(100, capacity)))
    return -1


def power_state_label(state: object) -> str:
    label = str(state or "-")
    capacity = battery_capacity_label()
    if capacity != "-":
        return f"{label} {capacity}"
    return label


def power_meter_label(state: object, percent: int) -> str:
    label = str(state or "-").upper()
    if percent >= 0:
        return f"{label} {percent}%"
    return label


def power_watt_label(state: object) -> str:
    if str(state).lower() == "ac":
        ac_power = ac_power_label()
        if ac_power != "-":
            return ac_power
    return battery_power_label()


def realtime_power_label(state: object) -> str:
    battery_power = battery_power_label()
    if battery_power != "-":
        return battery_power
    if str(state).lower() == "ac":
        return ac_power_label()
    return "-"


def power_watt_line(state: object) -> str:
    if str(state).lower() == "ac":
        ac_power = ac_power_label()
        if ac_power != "-":
            return kv_line("AC", ac_power)
        if battery_status_label() == "charging":
            return kv_line("CHG", battery_power_label())
        return kv_line("AC", "-")
    return kv_line("BAT", battery_power_label())


def battery_status_label() -> str:
    for battery in battery_supplies():
        status = read_first_existing(battery / "status").lower()
        if status:
            return status
    return ""


def format_hours(hours: float) -> str:
    if hours <= 0:
        return "-"
    minutes = int(hours * 60)
    if minutes < 1:
        return "<1m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def power_supply_present(path: Path) -> bool:
    present_path = path / "present"
    if not present_path.is_file():
        return True
    try:
        return present_path.read_text(encoding="utf-8").strip() not in {"0", "false", "False"}
    except OSError:
        return False


def power_supply_online(path: Path) -> bool | None:
    online_path = path / "online"
    if online_path.is_file():
        try:
            return online_path.read_text(encoding="utf-8").strip() == "1"
        except OSError:
            return None
    status_path = path / "status"
    if status_path.is_file():
        try:
            return status_path.read_text(encoding="utf-8").strip().lower() in {"charging", "full"}
        except OSError:
            return None
    return None


def current_cpu_summary(config: dict[str, str]) -> str:
    policy = Path(config.get("POWERSAVER_CPU_POLICY_PATH", "/sys/devices/system/cpu/cpufreq/policy0"))
    try:
        min_freq = int((policy / "scaling_min_freq").read_text(encoding="utf-8").strip()) // 1000
        max_freq = int((policy / "scaling_max_freq").read_text(encoding="utf-8").strip()) // 1000
    except (OSError, ValueError):
        return "-"
    return f"{min_freq}-{max_freq} MHz"


def current_cpu_freq_summary(config: dict[str, str]) -> str:
    policy = Path(config.get("POWERSAVER_CPU_POLICY_PATH", "/sys/devices/system/cpu/cpufreq/policy0"))
    current = read_first_existing(policy / "scaling_cur_freq", policy / "cpuinfo_cur_freq")
    try:
        return f"{int(current) // 1000} MHz"
    except (TypeError, ValueError):
        return "-"


def current_wwan_summary() -> str:
    if shutil.which("nmcli") is None:
        return "-"
    for command in (["nmcli", "-t", "-f", "WWAN", "radio"], ["nmcli", "radio", "wwan"]):
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            continue
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return "-"


def powersaver_config_summary(config: dict[str, str]) -> str:
    enabled = config.get("POWERSAVER_ENABLED", "1")
    mode = config.get("POWERSAVER_MODE", "balanced")
    profile = mode.upper()
    battery = config.get(
        f"POWERSAVER_{profile}_BATTERY_CPU_FREQ",
        config.get("POWERSAVER_BATTERY_CPU_FREQ", "1500,1500"),
    )
    ac = config.get(f"POWERSAVER_{profile}_AC_CPU_FREQ", config.get("POWERSAVER_AC_CPU_FREQ", "restore"))
    unknown = config.get(
        f"POWERSAVER_{profile}_UNKNOWN_POWER_ACTION",
        config.get("POWERSAVER_UNKNOWN_POWER_ACTION", "restore"),
    )
    wwan = config.get(f"POWERSAVER_{profile}_WWAN_POLICY", config.get("POWERSAVER_WWAN_POLICY", "ondemand"))
    battery_screen = config.get(f"POWERSAVER_{profile}_BATTERY_SCREEN_TIMEOUT_SEC", "0")
    ac_screen = config.get(f"POWERSAVER_{profile}_AC_SCREEN_TIMEOUT_SEC", "0")
    state = "enabled" if enabled.lower() in {"1", "yes", "true", "on", "enabled"} else "disabled"
    screen_label = "off" if battery_screen == "0" and ac_screen == "0" else f"B {battery_screen}s / AC {ac_screen}s"
    return f"{state}; {mode}; battery {battery} MHz; AC {ac}; unknown {unknown}; WWAN {wwan}; screen {screen_label}"


def power_screen_timeout_summary(config: dict[str, str], power_state: str) -> str:
    mode = config.get("POWERSAVER_MODE", "balanced")
    profile = mode.upper()
    battery = config.get(f"POWERSAVER_{profile}_BATTERY_SCREEN_TIMEOUT_SEC", "0")
    ac = config.get(f"POWERSAVER_{profile}_AC_SCREEN_TIMEOUT_SEC", "0")
    power_state = power_state.lower()
    if power_state == "ac":
        return screen_timeout_display_value(ac)
    if power_state == "battery":
        return screen_timeout_display_value(battery)
    unknown = config.get(
        f"POWERSAVER_{profile}_UNKNOWN_POWER_ACTION",
        config.get("POWERSAVER_UNKNOWN_POWER_ACTION", "restore"),
    )
    if unknown == "restore":
        return screen_timeout_display_value(ac)
    if unknown == "battery":
        return screen_timeout_display_value(battery)
    battery_text = screen_timeout_display_value(battery)
    ac_text = screen_timeout_display_value(ac)
    if battery_text == ac_text:
        return battery_text
    return f"Unknown, B {battery_text} / AC {ac_text}"


def interface_addresses() -> dict[str, str]:
    if shutil.which("ip") is None:
        return {}
    result = subprocess.run(
        ["ip", "-o", "addr", "show"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return {}
    addresses: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        device = parts[1]
        family = parts[2]
        address = parts[3]
        if family not in {"inet", "inet6"}:
            continue
        addresses.setdefault(device, []).append(address)
    return {device: ", ".join(values) for device, values in addresses.items()}


def udev_properties(path: Path) -> dict[str, str]:
    if shutil.which("udevadm") is None:
        return {}
    result = subprocess.run(
        ["udevadm", "info", "-q", "property", "-p", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return {}

    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    return props


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def resolved_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return ""


def run_helper(action: str, config: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(HELPER), action]
    if config is not None:
        command.append(json.dumps(config))

    if os.geteuid() != 0 and action in {"start", "stop"}:
        if shutil.which("pkexec"):
            command = ["pkexec", *command]
        else:
            command = ["sudo", *command]

    return subprocess.run(command, text=True, capture_output=True, check=False)


def combine_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)


def dhcp_defaults() -> dict[str, str]:
    network = choose_dhcp_network()
    values = dict(DEFAULTS)
    values["server_ip"] = str(network.network_address + 1)
    values["netmask"] = str(network.netmask)
    values["pool_start"] = str(network.network_address + 100)
    values["pool_end"] = str(network.network_address + 200)
    return values


def choose_dhcp_network() -> ipaddress.IPv4Network:
    used_networks = current_ipv4_networks()
    for candidate in DHCP_NETWORK_CANDIDATES:
        network = ipaddress.IPv4Network(candidate)
        if not any(network.overlaps(used) for used in used_networks):
            return network

    for third_octet in range(50, 255):
        network = ipaddress.IPv4Network(f"192.168.{third_octet}.0/24")
        if not any(network.overlaps(used) for used in used_networks):
            return network

    return ipaddress.IPv4Network(DEFAULTS["server_ip"] + "/" + DEFAULTS["netmask"], strict=False)


def current_ipv4_networks() -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    if shutil.which("ip") is not None:
        result = subprocess.run(["ip", "-4", "-o", "addr", "show"], text=True, capture_output=True, check=False)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)", line)
                if not match:
                    continue
                try:
                    networks.append(ipaddress.IPv4Interface(match.group(1)).network)
                except ValueError:
                    continue

    for route in ipv4_routes():
        line = str(route["line"])
        first = line.split(maxsplit=1)[0]
        if first == "default":
            continue
        try:
            networks.append(ipaddress.IPv4Network(first, strict=False))
        except ValueError:
            continue
    return networks


def interface_ipv4_network(interface: str) -> ipaddress.IPv4Network | None:
    if shutil.which("ip") is None:
        return None
    result = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", interface],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)", line)
        if match:
            try:
                network = ipaddress.IPv4Interface(match.group(1)).network
            except ValueError:
                continue
            if network.prefixlen <= 30:
                return network
    return None


def ipv4_routes() -> list[dict[str, object]]:
    if shutil.which("ip") is None:
        return []
    result = subprocess.run(["ip", "-4", "route", "show"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return []

    routes: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        device = ""
        metric = 0
        if "dev" in parts:
            index = parts.index("dev")
            if index + 1 < len(parts):
                device = parts[index + 1]
        if "metric" in parts:
            index = parts.index("metric")
            if index + 1 < len(parts):
                try:
                    metric = int(parts[index + 1])
                except ValueError:
                    metric = 0
        routes.append({"default": parts[0] == "default", "device": device, "metric": metric, "line": line})
    return routes


def scan_lan(
    interface: str,
    network: ipaddress.IPv4Network,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, str]]:
    if shutil.which("ip") is None:
        raise RuntimeError("缺少 ip 命令，无法读取邻居表。")

    addresses = [str(host) for host in network.hosts()]
    if len(addresses) > 512:
        raise RuntimeError(f"{network} 太大，请选择 /23 或更小的网段后再扫描。")

    ping_hosts(interface, addresses, cancel_event)
    if cancel_event is not None and cancel_event.is_set():
        return []
    neighbors = read_neighbors(interface)
    merge_dhcp_leases(neighbors, network)
    hostnames = resolve_hostnames(interface, neighbors)
    rows: list[dict[str, str]] = []
    for ip in sorted(neighbors, key=lambda item: ipaddress.IPv4Address(item)):
        item = neighbors[ip]
        rows.append(
            {
                "ip": ip,
                "mac": item.get("mac", "-"),
                "state": item.get("state", "-"),
                "hostname": hostnames.get(ip, "-"),
            }
        )
    return rows


def merge_dhcp_leases(neighbors: dict[str, dict[str, str]], network: ipaddress.IPv4Network) -> None:
    for lease in read_dhcp_leases():
        try:
            address = ipaddress.IPv4Address(lease["ip"])
        except ValueError:
            continue
        if address not in network:
            continue
        item = neighbors.setdefault(lease["ip"], {})
        if not item.get("mac") or item.get("mac") == "-":
            item["mac"] = lease["mac"]
        item.setdefault("state", "LEASED")


def ping_hosts(interface: str, addresses: list[str], cancel_event: threading.Event | None = None) -> None:
    ping = shutil.which("ping")
    if ping is None:
        return

    def ping_one(ip: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            return
        subprocess.run(
            [ping, "-I", interface, "-c", "1", "-W", "1", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=48) as executor:
        futures = [executor.submit(ping_one, ip) for ip in addresses]
        for _future in as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                break


def read_neighbors(interface: str) -> dict[str, dict[str, str]]:
    result = subprocess.run(
        ["ip", "-4", "neighbor", "show", "dev", interface],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "读取邻居表失败。")

    neighbors: dict[str, dict[str, str]] = {}
    ignored_states = {"FAILED", "INCOMPLETE"}
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        ip = parts[0]
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        state = parts[-1] if parts else ""
        if state in ignored_states:
            continue
        mac = "-"
        if "lladdr" in parts:
            index = parts.index("lladdr")
            if index + 1 < len(parts):
                mac = parts[index + 1]
        neighbors[ip] = {"mac": mac, "state": state}
    return neighbors


def resolve_hostnames(interface: str, neighbors: dict[str, dict[str, str]]) -> dict[str, str]:
    names: dict[str, str] = {}
    names.update(hostnames_from_hosts_file())
    leases = read_dhcp_leases()
    names.update(hostnames_from_leases(leases))
    names_by_mac = hostnames_by_lease_mac(leases)

    result: dict[str, str] = {}
    for ip, item in neighbors.items():
        name = names.get(ip)
        if not name and item.get("mac"):
            name = names_by_mac.get(item["mac"].lower(), "")
        name = lan_hostname_or_empty(ip, name)
        if not name:
            name = lan_hostname_or_empty(ip, reverse_hostname(ip))
        if not name:
            name = lan_hostname_or_empty(ip, ptr_hostname_from_interface_dns(interface, ip))
        if not name:
            name = lan_hostname_or_empty(ip, mdns_hostname(interface, ip))
        if not name:
            name = lan_hostname_or_empty(ip, netbios_hostname(ip))
        result[ip] = name or "-"
    return result


def lan_hostname_or_empty(ip: str, name: str | None) -> str:
    name = str(name or "").strip().rstrip(".")
    if not name:
        return ""
    short_name = name.split(".", 1)[0].lower()
    if short_name in {"localhost", "localhost4", "localhost6"}:
        return ""
    if name.lower() in {"ip6-localhost", "ip6-loopback"}:
        return ""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return name
    if address.is_loopback:
        return ""
    return name


def hostnames_from_hosts_file() -> dict[str, str]:
    path = Path("/etc/hosts")
    hostnames: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return hostnames

    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            ipaddress.IPv4Address(parts[0])
        except ValueError:
            continue
        if not parts[1].lower().endswith(".local"):
            hostnames[parts[0]] = parts[1]
    return hostnames


def hostnames_from_leases(leases: list[dict[str, str]]) -> dict[str, str]:
    hostnames: dict[str, str] = {}
    for lease in leases:
        if lease["hostname"] != "*":
            hostnames[lease["ip"]] = lease["hostname"]
    return hostnames


def hostnames_by_lease_mac(leases: list[dict[str, str]]) -> dict[str, str]:
    return {
        lease["mac"].lower(): lease["hostname"]
        for lease in leases
        if lease["hostname"] != "*"
    }


def read_dhcp_leases() -> list[dict[str, str]]:
    try:
        lines = LEASE_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    leases: list[dict[str, str]] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        leases.append({"mac": parts[1], "ip": parts[2], "hostname": parts[3]})
    return leases


def reverse_hostname(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return ""


def ptr_hostname_from_interface_dns(interface: str, ip: str) -> str:
    for server in dns_servers_for_interface(interface):
        name = ptr_hostname_from_dns(server, ip)
        if name:
            return name
    return ""


def dns_servers_for_interface(interface: str) -> list[str]:
    servers: list[str] = []
    for route in ipv4_routes():
        if route["device"] == interface:
            line = str(route["line"])
            if line.startswith("default via "):
                parts = line.split()
                if len(parts) >= 3:
                    servers.append(parts[2])

    for path in Path("/run/NetworkManager").glob("devices/*"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if f"dhcp4.ip_address={interface_ipv4_address(interface)}" not in text:
            continue
        for line in text.splitlines():
            if line.startswith("dhcp4.domain_name_servers="):
                servers.extend(line.split("=", 1)[1].split())

    try:
        resolv_text = Path("/run/NetworkManager/no-stub-resolv.conf").read_text(encoding="utf-8")
    except OSError:
        resolv_text = ""
    for line in resolv_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "nameserver":
            servers.append(parts[1])

    return unique_ipv4_addresses(servers)


def interface_ipv4_address(interface: str) -> str:
    if shutil.which("ip") is None:
        return ""
    result = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", interface],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", result.stdout)
    return match.group(1) if match else ""


def unique_ipv4_addresses(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        try:
            ipaddress.IPv4Address(value)
        except ValueError:
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def ptr_hostname_from_dns(server: str, ip: str) -> str:
    try:
        query = build_dns_ptr_query(ip)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.8)
            sock.sendto(query, (server, 53))
            data, _addr = sock.recvfrom(1024)
    except OSError:
        return ""
    return parse_dns_ptr_response(data)


def build_dns_ptr_query(ip: str) -> bytes:
    transaction_id = os.getpid() & 0xFFFF
    header = struct.pack("!HHHHHH", transaction_id, 0x0100, 1, 0, 0, 0)
    reverse_name = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
    question = encode_dns_name(reverse_name) + struct.pack("!HH", 12, 1)
    return header + question


def encode_dns_name(name: str) -> bytes:
    out = bytearray()
    for label in name.strip(".").split("."):
        encoded = label.encode("ascii", errors="ignore")
        out.append(len(encoded))
        out.extend(encoded)
    out.append(0)
    return bytes(out)


def parse_dns_ptr_response(data: bytes) -> str:
    if len(data) < 12:
        return ""
    _tid, _flags, qdcount, ancount, _nscount, _arcount = struct.unpack("!HHHHHH", data[:12])
    offset = 12
    for _ in range(qdcount):
        _name, offset = read_dns_name(data, offset)
        offset += 4
    for _ in range(ancount):
        _name, offset = read_dns_name(data, offset)
        if offset + 10 > len(data):
            return ""
        rtype, _rclass, _ttl, rdlength = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata_offset = offset
        offset += rdlength
        if rtype == 12:
            value, _next = read_dns_name(data, rdata_offset)
            return value.rstrip(".")
    return ""


def read_dns_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    next_offset = offset
    seen = 0
    while offset < len(data):
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                next_offset = offset
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                next_offset = offset + 2
            offset = pointer
            jumped = True
            seen += 1
            if seen > 20:
                break
            continue
        offset += 1
        label = data[offset : offset + length].decode("utf-8", errors="ignore")
        labels.append(label)
        offset += length
        if not jumped:
            next_offset = offset
    return ".".join(labels), next_offset


def mdns_hostname(interface: str, ip: str) -> str:
    for command in (
        ["avahi-resolve-address", ip],
        ["avahi-resolve", "-a", ip],
    ):
        if shutil.which(command[0]) is None:
            continue
        result = subprocess.run(command, text=True, capture_output=True, timeout=2, check=False)
        if result.returncode != 0:
            continue
        parts = result.stdout.split()
        if len(parts) >= 2:
            name = lan_hostname_or_empty(ip, parts[1].removesuffix(".local"))
            if name:
                return name
    return mdns_hostname_from_browse(interface, ip)


def mdns_hostname_from_browse(interface: str, ip: str) -> str:
    if shutil.which("avahi-browse") is None:
        return ""
    result = subprocess.run(
        ["avahi-browse", "-alrt"],
        text=True,
        capture_output=True,
        timeout=4,
        check=False,
    )
    if result.returncode != 0:
        return ""

    current_interface = ""
    current_hostname = ""
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("=", "+")):
            parts = stripped.split()
            current_interface = parts[1] if len(parts) >= 2 else ""
            current_hostname = ""
            continue
        if current_interface != interface:
            continue
        match = re.search(r"hostname\s*=\s*\[(.+)\]", stripped)
        if match:
            current_hostname = match.group(1).strip()
            continue
        match = re.search(r"address\s*=\s*\[(.+)\]", stripped)
        if match and match.group(1).strip() == ip:
            name = lan_hostname_or_empty(ip, current_hostname.removesuffix(".local"))
            if name:
                return name
    return ""


def netbios_hostname(ip: str) -> str:
    query = build_netbios_node_status_query()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.8)
            sock.sendto(query, (ip, 137))
            data, _addr = sock.recvfrom(1024)
    except OSError:
        return ""
    return parse_netbios_node_status(data)


def build_netbios_node_status_query() -> bytes:
    transaction_id = os.getpid() & 0xFFFF
    header = struct.pack("!HHHHHH", transaction_id, 0x0000, 1, 0, 0, 0)
    encoded_name = encode_netbios_name("*")
    question = encoded_name + struct.pack("!HH", 0x0021, 0x0001)
    return header + question


def encode_netbios_name(name: str) -> bytes:
    raw = name.upper().encode("ascii", errors="ignore")[:15].ljust(15, b" ") + b"\x00"
    encoded = bytearray()
    for byte in raw:
        encoded.append(ord("A") + ((byte >> 4) & 0x0F))
        encoded.append(ord("A") + (byte & 0x0F))
    return bytes([32]) + bytes(encoded) + b"\x00"


def parse_netbios_node_status(data: bytes) -> str:
    if len(data) < 57:
        return ""
    offset = 12
    while offset < len(data) and data[offset] != 0:
        offset += data[offset] + 1
    offset += 1
    offset += 4
    if offset + 10 >= len(data):
        return ""
    offset += 10
    if offset >= len(data):
        return ""
    name_count = data[offset]
    offset += 1
    candidates: list[tuple[int, str]] = []
    for _index in range(name_count):
        if offset + 18 > len(data):
            break
        raw_name = data[offset : offset + 15].decode("ascii", errors="ignore").strip()
        suffix = data[offset + 15]
        flags = int.from_bytes(data[offset + 16 : offset + 18], "big")
        offset += 18
        if not raw_name or raw_name == "*":
            continue
        is_group = bool(flags & 0x8000)
        if is_group:
            continue
        priority = 1 if suffix == 0x00 else 2
        candidates.append((priority, raw_name))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def find_xiao_device() -> McuDeviceInfo:
    tty = find_xiao_tty()
    mode = "unknown"
    product = ""
    manufacturer = ""
    serial = ""
    usb_path = ""
    notes = ""
    if tty:
        tty_path = Path("/sys/class/tty") / tty
        usb_root = usb_device_root(tty_path)
        if usb_root is not None:
            usb_path = usb_root.name
            mode = "bootloader" if usb_bootloader_hint(usb_root) else "sensor"
            product = read_text(usb_root / "product")
            manufacturer = read_text(usb_root / "manufacturer")
            serial = read_text(usb_root / "serial")
    else:
        usb_root = find_xiao_usb_root()
        if usb_root is not None:
            usb_path = usb_root.name
            product = read_text(usb_root / "product")
            manufacturer = read_text(usb_root / "manufacturer")
            serial = read_text(usb_root / "serial")
            mode = "bootloader" if usb_bootloader_hint(usb_root) else "usb"
            notes = "USB device present without ttyACM"
    present = bool(tty or usb_path)
    return McuDeviceInfo(
        present=present,
        mode=mode,
        tty=tty,
        product=product or "XIAO nRF52840 Sense Plus",
        manufacturer=manufacturer or "Seeed",
        serial=serial,
        usb_path=usb_path,
        notes=notes,
    )


def find_xiao_tty() -> str:
    candidates = []
    for tty_dir in Path("/sys/class/tty").glob("ttyACM*"):
        usb_root = usb_device_root(tty_dir)
        if usb_root is None:
            continue
        vendor = read_text(usb_root / "idVendor")
        product = read_text(usb_root / "idProduct")
        if vendor != XIAO_USB_VENDOR or product not in XIAO_USB_PRODUCT_IDS:
            continue
        candidates.append((tty_dir.name, usb_root))
    if candidates:
        candidates.sort(key=lambda item: 0 if item[1].name.endswith(".1") else 1)
        return candidates[0][0]
    return ""


def find_xiao_usb_root() -> Path | None:
    if not SYS_NET.exists():
        return None
    for path in Path("/sys/bus/usb/devices").iterdir():
        if not path.is_dir():
            continue
        vendor = read_text(path / "idVendor")
        product = read_text(path / "idProduct")
        if vendor == XIAO_USB_VENDOR and product in XIAO_USB_PRODUCT_IDS:
            return path
        label = " ".join(
            [
                read_text(path / "manufacturer"),
                read_text(path / "product"),
            ]
        ).lower()
        if "seeed" in label and "xiao" in label:
            return path
    return None


def xiao_usb_reset_targets() -> tuple[str, ...]:
    device = find_xiao_usb_root()
    if device is not None:
        direct_parent = usb_reset_parent_name(device.name)
        upper_parent = usb_reset_parent_name(direct_parent)
        targets = [target for target in (direct_parent, upper_parent) if target]
        if targets:
            return tuple(dict.fromkeys(targets))
    return ("1-1.4.2", "1-1.4")


def usb_reset_parent_name(device_name: str) -> str:
    if "." not in device_name:
        return ""
    return device_name.rsplit(".", 1)[0]


def usb_device_product(device_name: str) -> str:
    product = read_text(Path("/sys/bus/usb/devices") / device_name / "product")
    return product or device_name


def usb_hub_device_rows(hub_names: tuple[str, ...]) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    usb_root = Path("/sys/bus/usb/devices")
    for hub_name in hub_names:
        hub_path = usb_root / hub_name
        if not hub_path.exists():
            rows.append((hub_name, hub_name, "Not present", "-", "-"))
            continue
        hub_product = usb_device_product(hub_name)
        children = direct_usb_children(hub_name)
        if not children:
            rows.append((hub_product, hub_name, "No devices", "-", "-"))
            continue
        for child in children:
            child_path = usb_root / child
            product = read_text(child_path / "product")
            manufacturer = read_text(child_path / "manufacturer")
            label = " ".join(part for part in (manufacturer, product) if part) or child
            vendor = read_text(child_path / "idVendor")
            product_id = read_text(child_path / "idProduct")
            usb_id = f"{vendor}:{product_id}" if vendor and product_id else "-"
            driver = usb_device_driver(child_path)
            rows.append((hub_product, child, label, usb_id, driver or "-"))
    return rows


def direct_usb_children(hub_name: str) -> list[str]:
    prefix = f"{hub_name}."
    children: list[str] = []
    for path in Path("/sys/bus/usb/devices").iterdir():
        name = path.name
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :]
        if not suffix or "." in suffix or ":" in suffix:
            continue
        children.append(name)
    return sorted(children, key=usb_device_sort_key)


def usb_device_sort_key(device_name: str) -> tuple[int, ...]:
    parts = re.split(r"[.-]", device_name)
    values: list[int] = []
    for part in parts:
        if part.isdigit():
            values.append(int(part))
    return tuple(values)


def usb_device_driver(device_path: Path) -> str:
    drivers: set[str] = set()
    for child in device_path.iterdir():
        if ":" not in child.name:
            continue
        driver_link = child / "driver"
        try:
            drivers.add(driver_link.resolve().name)
        except OSError:
            continue
    return ", ".join(sorted(drivers))


def reset_usb_device(device_name: str, settle_seconds: float) -> None:
    device_path = Path("/sys/bus/usb/devices") / device_name
    unbind_path = Path("/sys/bus/usb/drivers/usb/unbind")
    bind_path = Path("/sys/bus/usb/drivers/usb/bind")
    if not device_path.exists():
        raise OSError(f"{usb_device_product(device_name)} ({device_name}) 不存在。")
    write_usb_driver_control(unbind_path, device_name)
    time.sleep(settle_seconds)
    write_usb_driver_control(bind_path, device_name)


def write_usb_driver_control(path: Path, device_name: str) -> None:
    try:
        path.write_text(device_name, encoding="ascii")
        return
    except OSError:
        pass
    result = subprocess.run(
        ["sudo", "tee", str(path)],
        input=device_name,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(combine_output(result) or f"无法写入 {path}")


def usb_bootloader_hint(path: Path) -> bool:
    product_id = read_text(path / "idProduct")
    if product_id in XIAO_BOOTLOADER_PRODUCT_IDS:
        return True
    label = " ".join(
        [
            read_text(path / "product"),
            read_text(path / "manufacturer"),
            read_text(path / "modalias"),
        ]
    ).lower()
    if any(hint in label for hint in XIAO_BOOTLOADER_HINTS):
        return True
    try:
        descendants = list(path.rglob("*"))
    except OSError:
        descendants = []
    for item in descendants:
        if item.name in {"bInterfaceClass", "bInterfaceSubClass", "product"}:
            value = read_text(item).lower()
            if value == "08" or any(hint in value for hint in XIAO_BOOTLOADER_HINTS):
                return True
    return False


def touch_xiao_bootloader(tty_name: str) -> None:
    dev = Path("/dev") / tty_name
    if not dev.exists():
        raise OSError(f"{dev} 不存在")
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = attrs[2] & ~termios.CBAUD
        attrs[2] = attrs[2] | termios.B1200
        attrs[3] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        time.sleep(0.25)
    finally:
        os.close(fd)


def send_xiao_command(command: str) -> None:
    device = find_xiao_device()
    if not device.present:
        raise OSError("没有检测到 XIAO 设备。")
    if not device.tty:
        raise OSError("当前没有可用的 ttyACM 串口。")
    session = open_xiao_serial(device.tty)
    if session is None:
        raise OSError(f"无法打开 /dev/{device.tty}。")
    try:
        os.write(session.fd, command.encode("ascii") + b"\n")
    finally:
        session.close()


def read_mcu_snapshot(state: McuTelemetryState) -> McuStateSnapshot:
    device = find_xiao_device()
    now = time.time()
    if not device.present:
        if state.serial_session is not None:
            state.serial_session.close()
            state.serial_session = None
        state.last_state = "未连接"
        state.last_event = "等待设备"
        state.last_motion = "-"
        state.last_error = ""
        return McuStateSnapshot(
            device=device,
            state="未连接",
            event="等待设备",
            motion="-",
            still_for=0.0,
            g_force=0.0,
            tilt_deg=0.0,
            sample_rate_hz=0.0,
            pose="-",
            light_lux=None,
            smoothed_light_lux=state.smoothed_light_lux,
            suggested_backlight=state.suggested_backlight,
            light_raw=None,
            light_ready=False,
            last_update=now,
            raw_line="",
            last_error="",
            recent_rows=(),
        )

    if device.mode == "bootloader":
        if state.serial_session is not None:
            state.serial_session.close()
            state.serial_session = None
        state.last_state = "刷机模式"
        state.last_event = "UF2 bootloader"
        state.last_motion = "-"
        state.last_error = ""
        return McuStateSnapshot(
            device=device,
            state="刷机模式",
            event="UF2 bootloader",
            motion="-",
            still_for=0.0,
            g_force=0.0,
            tilt_deg=0.0,
            sample_rate_hz=0.0,
            pose="-",
            light_lux=None,
            smoothed_light_lux=state.smoothed_light_lux,
            suggested_backlight=state.suggested_backlight,
            light_raw=None,
            light_ready=False,
            last_update=now,
            raw_line="",
            last_error="",
            recent_rows=(),
        )

    sample = read_shared_mcu_sample()
    if sample is not None and state.serial_session is not None:
        state.serial_session.close()
        state.serial_session = None
    if sample is None:
        sample = read_xiao_sample(device, state)
    if sample is None:
        if state.samples:
            last_sample = state.samples[-1]
            return McuStateSnapshot(
                device=device,
                state=state.last_state or "已连接",
                event=state.last_event or "等待样本",
                motion=state.last_motion or "-",
                still_for=max(0.0, now - state.stable_since) if state.stable_since is not None else 0.0,
                g_force=vector_magnitude(last_sample.ax, last_sample.ay, last_sample.az),
                tilt_deg=estimate_tilt_deg(last_sample.ax, last_sample.ay, last_sample.az),
                sample_rate_hz=estimate_sample_rate(state.samples),
                pose=last_sample.firmware_pose or "-",
                light_lux=last_sample.light_lux,
                smoothed_light_lux=state.smoothed_light_lux,
                suggested_backlight=state.suggested_backlight,
                light_raw=last_sample.light_raw,
                light_ready=last_sample.light_ready,
                last_update=last_sample.timestamp,
                raw_line=format_sample_line(last_sample),
                last_error=state.last_error,
                mic_ready=last_sample.mic_ready,
                mic_enabled=last_sample.mic_enabled,
                mic_assist=last_sample.mic_assist,
                mic_peak=last_sample.mic_peak,
                mic_recent_peak=last_sample.mic_recent_peak,
                recent_rows=recent_rows_from_samples(state.samples),
            )
        state.last_state = "已连接"
        state.last_event = "等待串口数据" if state.serial_waiting else "等待传感器数据"
        state.last_motion = "-"
        return McuStateSnapshot(
            device=device,
            state="已连接",
            event=state.last_event,
            motion="-",
            still_for=0.0,
            g_force=0.0,
            tilt_deg=0.0,
            sample_rate_hz=0.0,
            pose="-",
            light_lux=None,
            smoothed_light_lux=state.smoothed_light_lux,
            suggested_backlight=state.suggested_backlight,
            light_raw=None,
            light_ready=False,
            last_update=now,
            raw_line="",
            last_error=state.last_error,
            recent_rows=(),
        )

    state.samples.append(sample)
    state.samples = [item for item in state.samples if now - item.timestamp <= XIAO_SAMPLE_MAX_AGE_SECONDS]
    sample_rate_hz = estimate_sample_rate(state.samples)
    g_force = vector_magnitude(sample.ax, sample.ay, sample.az)
    tilt_deg = estimate_tilt_deg(sample.ax, sample.ay, sample.az)
    still_for = stable_duration(state, sample)
    motion = mcu_motion_label(sample.firmware_motion)
    event = mcu_event_label(sample.firmware_event)
    current_state = mcu_state_label(sample.firmware_state)
    if sample.light_screen is not None:
        state.smoothed_light_lux = sample.light_lux
        state.suggested_backlight = max(1, min(9, sample.light_screen))
    else:
        state.smoothed_light_lux = sample.light_lux
        state.suggested_backlight = None
    state.last_state = current_state
    state.last_event = event
    state.last_motion = motion
    state.last_error = ""
    rows = recent_rows_from_samples(state.samples)
    return McuStateSnapshot(
        device=device,
        state=current_state,
        event=event,
        motion=motion,
        still_for=still_for,
        g_force=g_force,
        tilt_deg=tilt_deg,
        sample_rate_hz=sample_rate_hz,
        pose=sample.firmware_pose or "-",
        light_lux=sample.light_lux,
        smoothed_light_lux=state.smoothed_light_lux,
        suggested_backlight=state.suggested_backlight,
        light_raw=sample.light_raw,
        light_ready=sample.light_ready,
        last_update=now,
        raw_line=format_sample_line(sample),
        last_error="",
        mic_ready=sample.mic_ready,
        mic_enabled=sample.mic_enabled,
        mic_assist=sample.mic_assist,
        mic_peak=sample.mic_peak,
        mic_recent_peak=sample.mic_recent_peak,
        recent_rows=rows,
    )


def read_xiao_sample(device: McuDeviceInfo, state: McuTelemetryState) -> McuTelemetrySample | None:
    if not device.tty:
        state.last_error = "没有 ttyACM 串口。"
        return None
    session = state.serial_session
    if session is None or session.tty_name != device.tty or session.fd < 0:
        if session is not None:
            session.close()
        session = open_xiao_serial(device.tty)
        state.serial_session = session
    if session is None:
        dev = Path("/dev") / device.tty
        if not dev.exists():
            state.last_error = f"{dev} 不存在。"
        else:
            state.last_error = f"无法打开 {dev}；检查 dialout 组或串口占用。"
        return None
    line = session.read_line(timeout=0.0)
    if not line:
        state.serial_waiting = True
        state.last_error = ""
        return None
    sample = parse_mcu_line(line)
    if sample is None:
        state.serial_waiting = False
        state.last_error = f"无法解析串口行: {line[:80]}"
        return None
    write_shared_mcu_line(line)
    state.serial_waiting = False
    state.last_error = ""
    return sample


def read_shared_mcu_sample() -> McuTelemetrySample | None:
    try:
        stat = MCU_SHARED_SAMPLE_FILE.stat()
    except OSError:
        return None
    if time.time() - stat.st_mtime > 5.0:
        return None
    try:
        text = MCU_SHARED_SAMPLE_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    sample = parse_mcu_line(text)
    if sample is None:
        return None
    return dataclasses.replace(sample, timestamp=stat.st_mtime, source="shared")


def write_shared_mcu_line(line: str) -> None:
    text = line.strip()
    if not text.startswith("{"):
        return
    try:
        payload = json_module.loads(text)
    except json_module.JSONDecodeError:
        return
    if not isinstance(payload, dict) or "accel" not in payload:
        return
    try:
        MCU_SHARED_SAMPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = MCU_SHARED_SAMPLE_FILE.with_suffix(f"{MCU_SHARED_SAMPLE_FILE.suffix}.{os.getpid()}.tmp")
        tmp_path.write_text(json_module.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(MCU_SHARED_SAMPLE_FILE)
    except OSError:
        return


def open_xiao_serial(tty_name: str) -> McuSerialSession | None:
    dev = Path("/dev") / tty_name
    if not dev.exists():
        return None
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = attrs[2] & ~termios.CBAUD
        attrs[2] = attrs[2] | termios.B115200
        attrs[3] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        set_serial_modem_lines(fd, termios.TIOCM_DTR | termios.TIOCM_RTS)
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except OSError:
            pass
        time.sleep(0.15)
        return McuSerialSession(tty_name=tty_name, fd=fd)
    except OSError:
        return None


def set_serial_modem_lines(fd: int, mask: int) -> None:
    try:
        flags = array.array("i", [mask])
        fcntl.ioctl(fd, termios.TIOCMBIS, flags, True)
    except OSError:
        pass


def parse_mcu_line(line: str) -> McuTelemetrySample | None:
    text = line.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            payload = json_module.loads(text)
        except json_module.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            values = payload.get("accel")
            if isinstance(values, dict):
                ax = float(values.get("x", 0.0))
                ay = float(values.get("y", 0.0))
                az = float(values.get("z", 0.0))
            else:
                return None
            temperature = None
            delta_value = payload.get("delta")
            gyro = payload.get("gyro")
            gx = None
            gy = None
            gz = None
            if isinstance(gyro, dict):
                gx = float(gyro.get("x", 0.0))
                gy = float(gyro.get("y", 0.0))
                gz = float(gyro.get("z", 0.0))
            mic = payload.get("mic")
            mic_peak = None
            mic_recent_peak = False
            if isinstance(mic, dict):
                peak_value = mic.get("peak")
                mic_peak = int(peak_value) if peak_value is not None else None
                mic_recent_peak = bool(mic.get("recent_peak"))
                mic_ready = bool(mic.get("ready"))
                mic_enabled = bool(mic.get("enabled"))
                mic_assist = bool(mic.get("assist")) if mic.get("assist") is not None else None
            else:
                mic_ready = False
                mic_enabled = False
                mic_assist = None
            light = payload.get("light")
            light_lux = None
            light_raw = None
            light_screen = None
            light_keyboard = None
            light_ready = False
            if isinstance(light, dict):
                lux_value = light.get("lux")
                raw_value = light.get("raw")
                screen_value = light.get("screen")
                keyboard_value = light.get("keyboard")
                valid = bool(light.get("valid", lux_value is not None))
                light_lux = float(lux_value) if lux_value is not None and valid else None
                light_raw = int(raw_value) if raw_value is not None else None
                light_screen = int(screen_value) if screen_value is not None else None
                light_keyboard = int(keyboard_value) if keyboard_value is not None else None
                light_ready = bool(light.get("ready"))
            return McuTelemetrySample(
                time.time(),
                ax,
                ay,
                az,
                temperature,
                light_lux=light_lux,
                light_raw=light_raw,
                light_screen=light_screen,
                light_keyboard=light_keyboard,
                light_ready=light_ready,
                source="json",
                firmware_state=str(payload.get("state") or ""),
                firmware_event=str(payload.get("event") or ""),
                firmware_motion=str(payload.get("motion") or ""),
                firmware_pose=str(payload.get("pose") or ""),
                firmware_delta=float(delta_value) if delta_value is not None else None,
                mic_peak=mic_peak,
                mic_recent_peak=mic_recent_peak,
                mic_ready=mic_ready,
                mic_enabled=mic_enabled,
                mic_assist=mic_assist,
                gx=gx,
                gy=gy,
                gz=gz,
            )

    parts = [part for part in re.split(r"[,\s]+", text) if part]
    if len(parts) < 3:
        return None
    try:
        ax = float(parts[0])
        ay = float(parts[1])
        az = float(parts[2])
    except ValueError:
        return None
    temperature = None
    if len(parts) >= 4:
        try:
            temperature = float(parts[3])
        except ValueError:
            temperature = None
    return McuTelemetrySample(time.time(), ax, ay, az, temperature, source="csv")


def vector_magnitude(ax: float, ay: float, az: float) -> float:
    return math.sqrt(ax * ax + ay * ay + az * az)


def estimate_tilt_deg(ax: float, ay: float, az: float) -> float:
    denominator = math.sqrt(ay * ay + az * az)
    if denominator <= 0:
        return 0.0
    return abs(math.degrees(math.atan2(ax, denominator)))


def stable_duration(state: McuTelemetryState, sample: McuTelemetrySample) -> float:
    if state.prev_sample is None:
        state.stable_since = sample.timestamp
        state.last_delta_g = 0.0
        state.prev_sample = sample
        return 0.0
    delta = abs(sample.ax - state.prev_sample.ax) + abs(sample.ay - state.prev_sample.ay) + abs(sample.az - state.prev_sample.az)
    state.last_delta_g = delta
    if delta <= XIAO_STILL_G_FORCE:
        if state.stable_since is None:
            state.stable_since = sample.timestamp
    else:
        state.stable_since = sample.timestamp
    state.prev_sample = sample
    if state.stable_since is None:
        return 0.0
    return max(0.0, sample.timestamp - state.stable_since)


def mcu_state_label(state: str) -> str:
    return {
        "held": "拿起",
        "put_down": "放下",
        "stand": "支架",
    }.get(state, state or "-")


def mcu_motion_label(motion: str) -> str:
    return {
        "moving": "移动中",
        "still": "静止",
    }.get(motion, motion or "-")


def mcu_event_label(event: str) -> str:
    return MCU_FIRMWARE_EVENT_LABELS.get(event, event or "-")


def estimate_sample_rate(samples: list[McuTelemetrySample]) -> float:
    if len(samples) < 2:
        return 0.0
    span = samples[-1].timestamp - samples[0].timestamp
    if span <= 0:
        return 0.0
    return (len(samples) - 1) / span


def format_sample_line(sample: McuTelemetrySample) -> str:
    parts = [f"accel={sample.ax:.3f},{sample.ay:.3f},{sample.az:.3f}"]
    if sample.gx is not None and sample.gy is not None and sample.gz is not None:
        parts.append(f"gyro={sample.gx:.2f},{sample.gy:.2f},{sample.gz:.2f}")
    if sample.firmware_delta is not None:
        parts.append(f"delta={sample.firmware_delta:.3f}")
    if sample.light_lux is not None:
        parts.append(f"lux={sample.light_lux:.1f}")
    if sample.mic_peak is not None:
        parts.append(f"mic={sample.mic_peak}{'*' if sample.mic_recent_peak else ''}")
    if sample.temperature is not None:
        parts.append(f"t={sample.temperature:.1f}")
    return ", ".join(parts)


def recent_rows_from_samples(samples: list[McuTelemetrySample]) -> tuple[tuple[str, str, str, str], ...]:
    rows: list[tuple[str, str, str, str]] = []
    for sample in samples[-6:][::-1]:
        g_force = vector_magnitude(sample.ax, sample.ay, sample.az)
        rows.append(
            (
                time.strftime("%H:%M:%S", time.localtime(sample.timestamp)),
                f"{sample.ax:.2f},{sample.ay:.2f},{sample.az:.2f} ({g_force:.2f}g)",
                sample.firmware_event or sample.source,
                format_sample_line(sample),
            )
        )
    return tuple(rows)


def mcu_recent_rows(snapshot: McuStateSnapshot) -> tuple[tuple[str, str, str, str], ...]:
    return snapshot.recent_rows or ()


def snapshot_to_telemetry_state(snapshot: McuStateSnapshot, state: McuTelemetryState) -> McuTelemetryState:
    state.last_state = snapshot.state
    state.last_event = snapshot.event
    state.last_motion = snapshot.motion
    state.last_error = snapshot.last_error
    state.smoothed_light_lux = snapshot.smoothed_light_lux
    state.suggested_backlight = snapshot.suggested_backlight
    return state


def mcu_snapshot_placeholder() -> McuStateSnapshot:
    device = find_xiao_device()
    return McuStateSnapshot(
        device=device,
        state="等待设备" if not device.present else "已连接",
        event="等待传感器数据",
        motion="-",
        still_for=0.0,
        g_force=0.0,
        tilt_deg=0.0,
        sample_rate_hz=0.0,
        pose="-",
        light_lux=None,
        smoothed_light_lux=None,
        suggested_backlight=None,
        light_raw=None,
        light_ready=False,
        last_update=time.time(),
        raw_line="",
        last_error="",
        recent_rows=(),
    )


def mcu_device_label(device: McuDeviceInfo) -> str:
    if not device.present:
        return "未检测到 XIAO"
    parts = [device.manufacturer or "Seeed", device.product or "XIAO"]
    if device.tty:
        parts.append(device.tty)
    if device.mode:
        parts.append(device.mode)
    return " / ".join(parts)


def mcu_updated_label(snapshot: McuStateSnapshot) -> str:
    age = max(0.0, time.time() - snapshot.last_update)
    return f"{age:.1f}s ago"


def format_mic_label(snapshot: McuStateSnapshot) -> str:
    if snapshot.mic_assist is None:
        return "-"
    assist = "on" if snapshot.mic_assist else "off"
    active = "active" if snapshot.mic_enabled else "idle"
    parts = [f"assist {assist}", active]
    if snapshot.mic_peak is not None:
        recent = "*" if snapshot.mic_recent_peak else ""
        parts.append(f"peak {snapshot.mic_peak}{recent}")
    return " / ".join(parts)


def format_pose_label(pose: str) -> str:
    if not pose or pose == "-":
        return "-"
    label = MCU_POSE_LABELS.get(pose)
    if label:
        return label
    return pose


def format_light_label(snapshot: McuStateSnapshot) -> str:
    lux = snapshot.light_lux
    smoothed_lux = snapshot.smoothed_light_lux
    raw = snapshot.light_raw
    ready = snapshot.light_ready
    actual_backlight = read_display_backlight_label()
    if lux is not None:
        suggested = snapshot.suggested_backlight
        smooth_text = f"{smoothed_lux:.1f}" if smoothed_lux is not None else "-"
        age_text = f"{max(0.0, time.time() - snapshot.last_update):.1f}s"
        if suggested is None:
            suggest_text = "-"
        else:
            level = light_level_by_backlight(suggested)
            suggest_text = f"{level[1]} {suggested}/9"
        suffix = f" / 滤波 {smooth_text} lx / 建议 {suggest_text} / 实际 {actual_backlight} / 样本 {age_text}"
        if raw is not None:
            return f"原始 {lux:.1f} lx (raw {raw}){suffix}"
        return f"原始 {lux:.1f} lx{suffix}"
    if ready:
        return f"等待光照样本 / 实际 {actual_backlight}"
    return "-"


def light_level_by_backlight(backlight: int) -> tuple[str, str, float | None, int]:
    for level in MCU_LIGHT_LEVELS:
        if level[3] == backlight:
            return level
    return MCU_LIGHT_LEVELS[-1]


def read_display_backlight_label() -> str:
    try:
        brightness = int(DISPLAY_BACKLIGHT_BRIGHTNESS_PATH.read_text(encoding="utf-8").strip())
        max_brightness = int(DISPLAY_BACKLIGHT_MAX_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return "-"
    if max_brightness > 0:
        return f"{brightness}/{max_brightness}"
    return str(brightness)


def mcu_summary_text(snapshot: McuStateSnapshot) -> str:
    if not snapshot.device.present:
        return "XIAO 未连接。"
    if snapshot.device.mode == "bootloader":
        return "当前在 UF2/bootloader 模式。"
    if snapshot.last_error:
        return snapshot.last_error
    if snapshot.state == "等待数据" or snapshot.event == "等待传感器数据":
        return "设备已连接，但还没有收到姿态数据。"
    return f"当前状态: {snapshot.state} / {snapshot.motion}"


def mcu_action_text(snapshot: McuStateSnapshot) -> str:
    if not snapshot.device.present:
        return "插上 XIAO 后这里会显示姿态状态。"
    if snapshot.device.mode == "bootloader":
        return "可以直接刷机。"
    if snapshot.last_error:
        return "先确认 /dev/ttyACM0 权限、会话组和固件串口输出。"
    if snapshot.state == "等待数据" or snapshot.event == "等待传感器数据":
        return "设备已连接，但还没有收到姿态数据。"
    if snapshot.state == "拿起":
        return "拿起状态，可唤醒屏幕。"
    if snapshot.state == "放下":
        return "放下状态，可启动熄屏倒计时。"
    if snapshot.state == "支架":
        return "支架状态，可按配置屏蔽自动熄屏。"
    return "等待更多样本。"


def mcu_hint_text(snapshot: McuStateSnapshot) -> str:
    if not snapshot.device.present:
        return "建议先确认串口输出格式，再决定是否自动锁屏。"
    if snapshot.last_error:
        return "当前用户会话若未包含 dialout，注销重登后再试；固件也需要持续输出 JSON 或 CSV 加速度行。"
    if snapshot.device.tty:
        return ""
    return "当前没有 ttyACM，只有 USB 设备描述符。"


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--list-interfaces":
        for info in discover_interfaces():
            if info.supported:
                print(f"{info.name}: 支持, {info.status}")
            else:
                print(f"{info.name}: 不支持, {info.reason}")
        return 0

    window = UConsoleHelperWindow()
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
