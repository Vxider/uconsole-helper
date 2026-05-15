#!/usr/bin/env python3
"""GTK desktop GUI for running a local DHCP server on one interface."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html import escape
from pathlib import Path

from gi.repository import Gdk, GLib, Gtk


APP_DIR = Path(__file__).resolve().parent
HELPER = APP_DIR / "network_helper_dhcp.py"
SYS_NET = Path("/sys/class/net")
LEASE_FILE = Path("/tmp/network-helper/dhcp/dnsmasq.leases")


DEFAULTS = {
    "server_ip": "192.168.50.1",
    "netmask": "255.255.255.0",
    "pool_start": "192.168.50.100",
    "pool_end": "192.168.50.200",
    "lease_time": "12h",
    "gateway": "",
    "dns": "",
}


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


class NetworkHelperWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="Network Helper")
        self.set_default_size(920, 640)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.on_key_press)
        self.scan_running = False
        self.scan_cancel = threading.Event()
        self.dhcp_running = False

        self.interface_store = Gtk.ListStore(str, str, bool, str, bool, str)
        self.interface_combo = Gtk.ComboBox.new_with_model(self.interface_store)
        renderer = Gtk.CellRendererText()
        self.interface_combo.pack_start(renderer, True)
        self.interface_combo.add_attribute(renderer, "text", 0)
        self.interface_combo.add_attribute(renderer, "sensitive", 2)
        self.interface_combo.add_attribute(renderer, "foreground", 3)
        self.interface_combo.add_attribute(renderer, "foreground-set", 4)
        self.interface_combo.set_row_separator_func(interface_row_is_separator)
        self.message_label = Gtk.Label(label="", xalign=0)
        self.entries = {key: Gtk.Entry() for key in DEFAULTS}
        for key, entry in self.entries.items():
            entry.set_text(DEFAULTS[key])

        self.scan_interface_store = Gtk.ListStore(str, str, bool, str, bool, str)
        self.scan_interface_combo = Gtk.ComboBox.new_with_model(self.scan_interface_store)
        scan_renderer = Gtk.CellRendererText()
        self.scan_interface_combo.pack_start(scan_renderer, True)
        self.scan_interface_combo.add_attribute(scan_renderer, "text", 0)
        self.scan_interface_combo.add_attribute(scan_renderer, "sensitive", 2)
        self.scan_interface_combo.add_attribute(scan_renderer, "foreground", 3)
        self.scan_interface_combo.add_attribute(scan_renderer, "foreground-set", 4)
        self.scan_interface_combo.set_row_separator_func(interface_row_is_separator)
        self.scan_message_label = Gtk.Label(label="选择网口后扫描同网段在线设备。", xalign=0)
        self.scan_store = Gtk.ListStore(str, str, str, str)
        self.interface_status_store = Gtk.ListStore(str, str, str, str, str, str, str)
        self.tailscale_store = Gtk.ListStore(str, str, str, str, str, str, str, str, str, str)
        self.tailscale_summary_label = Gtk.Label(label="", xalign=0)
        self.tailscale_summary_label.get_style_context().add_class("muted")

        self._build_ui()
        self.refresh_interfaces()
        self.refresh_interface_status()
        self.refresh_tailscale_status()
        self.refresh_status()

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.get_style_context().add_class("app-root")
        self.add(root)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(140)
        self.stack.add_titled(scrolled_page(self._build_dhcp_page()), "dhcp", "DHCP")
        self.stack.add_titled(scrolled_page(self._build_lanscan_page()), "lanscan", "LAN SCAN")
        self.stack.add_titled(scrolled_page(self._build_interface_page()), "interface", "Interface")
        self.stack.add_titled(scrolled_page(self._build_tailscale_page()), "tailscale", "Tailscale")
        self.stack.connect("notify::visible-child-name", lambda *_args: self.update_header())

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.get_style_context().add_class("topbar")
        root.pack_start(header, False, False, 0)

        tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tabs.get_style_context().add_class("app-tabs")
        header.pack_start(tabs, False, False, 0)
        self.dhcp_tab_label = Gtk.Label()
        self.dhcp_tab_label.set_use_markup(True)
        self.dhcp_tab = Gtk.Button()
        self.dhcp_tab.add(self.dhcp_tab_label)
        self.dhcp_tab.connect("clicked", lambda _button: self.set_tab("dhcp"))
        self.dhcp_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.dhcp_tab, False, False, 0)

        self.lanscan_tab = underlined_button("LAN Scan", "L")
        self.lanscan_tab.connect("clicked", lambda _button: self.set_tab("lanscan"))
        self.lanscan_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.lanscan_tab, False, False, 0)

        self.interface_tab = underlined_button("Interface", "I")
        self.interface_tab.connect("clicked", lambda _button: self.set_tab("interface"))
        self.interface_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.interface_tab, False, False, 0)

        self.tailscale_tab = underlined_button("Tailscale", "T")
        self.tailscale_tab.connect("clicked", lambda _button: self.set_tab("tailscale"))
        self.tailscale_tab.get_style_context().add_class("tab-button")
        tabs.pack_start(self.tailscale_tab, False, False, 0)

        spacer = Gtk.Box()
        header.pack_start(spacer, True, True, 0)
        self.context_action_button = underlined_button("Start", "S")
        self.context_action_button.connect("clicked", lambda _button: self.run_context_action())
        self.context_action_button.get_style_context().add_class("context-action")
        header.pack_start(self.context_action_button, False, False, 0)
        self.header_refresh_button = underlined_button("Refresh", "R")
        self.header_refresh_button.connect("clicked", lambda _button: self.run_refresh_action())
        self.header_refresh_button.get_style_context().add_class("context-action")
        self.header_refresh_button.get_style_context().add_class("action-ready")
        header.pack_start(self.header_refresh_button, False, False, 0)

        root.pack_start(self.stack, True, True, 0)

        self._install_css()
        self.update_header()

    def _build_dhcp_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.get_style_context().add_class("page")

        config_card = card_box()
        page.pack_start(config_card, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        config_card.pack_start(grid, False, False, 0)

        self._attach_label(grid, "网口", 0, 0)
        grid.attach(self.interface_combo, 1, 0, 1, 1)

        self._attach_entry(grid, "本机地址", "server_ip", 0, 1)
        self._attach_entry(grid, "子网掩码", "netmask", 2, 1)
        self._attach_entry(grid, "地址池起始", "pool_start", 0, 2)
        self._attach_entry(grid, "地址池结束", "pool_end", 2, 2)
        self._attach_entry(grid, "租约时间", "lease_time", 0, 3)
        self._attach_entry(grid, "网关(可选)", "gateway", 2, 3)
        self._attach_entry(grid, "DNS(可选)", "dns", 0, 4)

        return page

    def _build_lanscan_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.get_style_context().add_class("page")

        scan_card = card_box()
        page.pack_start(scan_card, False, False, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        scan_card.pack_start(controls, False, False, 0)
        label = Gtk.Label(label="网口", xalign=0)
        controls.pack_start(label, False, False, 0)
        self.scan_interface_combo.set_hexpand(True)
        controls.pack_start(self.scan_interface_combo, True, True, 0)

        results_card = card_box()
        page.pack_start(results_card, True, True, 0)

        tree = Gtk.TreeView(model=self.scan_store)
        tree.set_headers_visible(True)
        for index, title in enumerate(["IP", "MAC", "状态", "主机名"]):
            column = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=index)
            column.set_resizable(True)
            tree.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.add(tree)
        results_card.pack_start(scroll, True, True, 0)

        self.scan_message_label.get_style_context().add_class("muted")
        page.pack_start(self.scan_message_label, False, False, 0)
        return page

    def _build_interface_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.get_style_context().add_class("page")

        card = card_box()
        page.pack_start(card, True, True, 0)

        tree = Gtk.TreeView(model=self.interface_status_store)
        tree.set_headers_visible(True)
        for index, title in enumerate(["设备", "类型", "状态", "连接", "信号", "地址"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index, foreground=6)
            column.set_resizable(True)
            tree.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.set_margin_top(12)
        scroll.add(tree)
        card.pack_start(scroll, True, True, 0)
        return page

    def _build_tailscale_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.get_style_context().add_class("page")

        summary_card = card_box()
        page.pack_start(summary_card, False, False, 0)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        summary_card.pack_start(header, False, False, 0)
        self.tailscale_summary_label.set_hexpand(True)
        header.pack_start(self.tailscale_summary_label, True, True, 0)

        devices_card = card_box()
        page.pack_start(devices_card, True, True, 0)
        self.tailscale_tree = Gtk.TreeView(model=self.tailscale_store)
        self.tailscale_tree.set_headers_visible(True)
        self.tailscale_tree.connect("button-press-event", self.on_tailscale_tree_button_press)
        for index, title in enumerate(["设备", "OS", "地址", "状态", "最后在线", "出口节点"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index, foreground=6)
            column.set_resizable(True)
            self.tailscale_tree.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.add(self.tailscale_tree)
        devices_card.pack_start(scroll, True, True, 0)
        return page

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
            self.tailscale_store[tree_iter][7],
            self.tailscale_store[tree_iter][8],
            self.tailscale_store[tree_iter][9],
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
            .muted { color: #b7b7b7; }
            .card {
                background: #1e1e1e;
                border: 1px solid #4a4a4a;
                border-radius: 18px;
                padding: 16px;
            }
            .tab-button,
            .context-action {
                min-height: 34px;
                padding: 6px 18px;
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
                min-width: 92px;
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
            entry, combobox, treeview, textview, scrolledwindow {
                background: #171717;
                color: #f0f0f0;
                border-color: #4a4a4a;
            }
            entry {
                border: 1px solid #4a4a4a;
            }
            treeview.view {
                background: #171717;
                color: #f0f0f0;
            }
            treeview.view:selected {
                background: #2f6f6d;
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
        self.stack.set_visible_child_name(name)

    def update_header(self) -> None:
        page = self.stack.get_visible_child_name() or "dhcp"
        dot = "●" if self.dhcp_running else "○"
        self.dhcp_tab_label.set_markup(f"{dot} {underlined_markup('DHCP Server', 'D')}")
        toggle_style_class(self.dhcp_tab, "tab-active", page == "dhcp")
        toggle_style_class(self.lanscan_tab, "tab-active", page == "lanscan")
        toggle_style_class(self.interface_tab, "tab-active", page == "interface")
        toggle_style_class(self.tailscale_tab, "tab-active", page == "tailscale")
        action_context = self.context_action_button.get_style_context()
        for class_name in ("action-ready", "action-active", "action-busy"):
            action_context.remove_class(class_name)

        if page == "dhcp":
            self.context_action_button.show()
            if self.dhcp_running:
                set_underlined_button_label(self.context_action_button, "Stop", "S")
                action_context.add_class("action-active")
            else:
                set_underlined_button_label(self.context_action_button, "Start", "S")
                action_context.add_class("action-ready")
            return

        if page == "lanscan" and self.scan_running:
            self.context_action_button.show()
            set_underlined_button_label(self.context_action_button, "Stop", "S")
            action_context.add_class("action-busy")
        elif page == "lanscan":
            self.context_action_button.show()
            set_underlined_button_label(self.context_action_button, "Scan", "S")
            action_context.add_class("action-ready")
        else:
            self.context_action_button.hide()

    def run_context_action(self) -> None:
        page = self.stack.get_visible_child_name()
        if page == "dhcp":
            if self.dhcp_running:
                self.stop_server()
            else:
                self.start_server()
            return

        if page == "interface":
            self.refresh_interface_status()
            return
        if page == "tailscale":
            self.refresh_tailscale_status()
            return

        if self.scan_running:
            self.stop_lan_scan()
        else:
            self.start_lan_scan()

    def run_refresh_action(self) -> None:
        page = self.stack.get_visible_child_name()
        if page in {"dhcp", "lanscan"}:
            self.refresh_interfaces()
        elif page == "interface":
            self.refresh_interface_status()
        elif page == "tailscale":
            self.refresh_tailscale_status()

    def on_key_press(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        key = Gdk.keyval_name(event.keyval) or ""
        key_lower = key.lower()
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        if ctrl and key == "1":
            self.set_tab("dhcp")
            return True
        if ctrl and key == "2":
            self.set_tab("lanscan")
            return True
        if ctrl and key == "3":
            self.set_tab("interface")
            return True
        if ctrl and key == "4":
            self.set_tab("tailscale")
            return True
        if alt and key in {"Left", "Right"}:
            self.switch_tab(-1 if key == "Left" else 1)
            return True
        if is_text_input_focus(self):
            return False
        if key_lower == "d":
            self.set_tab("dhcp")
            return True
        if key_lower == "l":
            self.set_tab("lanscan")
            return True
        if key_lower == "i":
            self.set_tab("interface")
            return True
        if key_lower == "t":
            self.set_tab("tailscale")
            return True
        if key_lower == "r":
            self.run_refresh_action()
            return True
        if key_lower == "s":
            self.run_context_action()
            return True
        return False

    def switch_tab(self, direction: int) -> None:
        pages = ["dhcp", "lanscan", "interface", "tailscale"]
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

    def refresh_interfaces(self) -> None:
        interfaces = discover_interfaces()
        scan_interfaces = discover_scan_interfaces()
        current = self.selected_interface_name()
        self.interface_store.clear()
        scan_current = self.selected_scan_interface_name()
        self.scan_interface_store.clear()
        selected_index = populate_interface_store(
            self.interface_store,
            interfaces,
            current,
            preferred=preferred_dhcp_interface(interfaces),
        )
        scan_selected_index = populate_interface_store(
            self.scan_interface_store,
            scan_interfaces,
            scan_current,
            preferred=preferred_scan_interface(scan_interfaces),
        )
        if selected_index != -1:
            self.interface_combo.set_active(selected_index)
        if scan_selected_index != -1:
            self.scan_interface_combo.set_active(scan_selected_index)

    def refresh_interface_status(self) -> None:
        self.interface_status_store.clear()
        wifi_signals = wifi_signal_by_device()
        modem_signals = modem_signal_by_port()
        tailscale = tailscale_status()
        addresses = interface_addresses()

        for device in nmcli_device_status():
            if device["device"] == "lo":
                continue
            signal = "-"
            connection = device["connection"] or "-"
            if device["type"] == "wifi":
                signal = wifi_signals.get(device["device"], "-")
            elif device["type"] in {"gsm", "cdma"} or device["device"] in modem_signals:
                modem_signal = modem_signals.get(device["device"], {})
                signal = modem_signal.get("signal", "-")
                if modem_signal.get("connection"):
                    connection = modem_signal["connection"]
            elif device["device"].startswith("tailscale") or device["type"] == "tun":
                signal = tailscale_summary(tailscale)
            signal = signal_with_bars(signal)

            state = display_nm_state(device["state"])
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

    def refresh_tailscale_status(self) -> None:
        status = tailscale_status()
        self.tailscale_store.clear()
        if not status:
            self.tailscale_summary_label.set_text("Tailscale status unavailable")
            return

        self.tailscale_summary_label.set_text(tailscale_admin_summary(status))
        for device in tailscale_devices(status):
            self.tailscale_store.append(
                [
                    device["name"],
                    device["os"],
                    device["addresses"],
                    device["status"],
                    device["last_seen"],
                    device["exit_node"],
                    tailscale_row_color(device["status"]),
                    device["ipv4"],
                    device["ipv6"],
                    device["dns"],
                ]
            )

    def refresh_status(self) -> None:
        result = run_helper("status")
        if result.returncode == 0 and "running" in result.stdout:
            self.dhcp_running = True
        elif result.returncode == 0:
            self.dhcp_running = False
        else:
            self.dhcp_running = False
        self.update_header()

    def start_server(self) -> None:
        try:
            config = self.validated_config()
        except ValueError as exc:
            self.show_error("配置错误", str(exc))
            return

        if not shutil.which("dnsmasq"):
            self.show_error("缺少依赖", "未找到 dnsmasq，请先安装 dnsmasq。")
            return

        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="确认启动 DHCP Server",
        )
        dialog.format_secondary_text(
            f"将刷新 {config['interface']} 的地址并启动 DHCP Server。\n不要选择正在上网或远程连接的网口。"
        )
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        self.message_label.set_text("正在启动 DHCP Server...")
        while Gtk.events_pending():
            Gtk.main_iteration()
        result = run_helper("start", config)
        output = combine_output(result)
        if result.returncode == 0:
            self.dhcp_running = True
            self.message_label.set_text(f"DHCP Server 已在 {config['interface']} 上启动。")
        else:
            self.dhcp_running = False
            self.message_label.set_text("启动失败。")
            self.show_error("启动失败", output or "命令执行失败。")
        self.update_header()

    def stop_server(self) -> None:
        self.message_label.set_text("正在停止 DHCP Server...")
        while Gtk.events_pending():
            Gtk.main_iteration()
        result = run_helper("stop")
        output = combine_output(result)
        if result.returncode == 0:
            self.dhcp_running = False
            self.message_label.set_text("DHCP Server 已停止。")
        else:
            self.message_label.set_text("停止失败。")
            self.show_error("停止失败", output or "命令执行失败。")
        self.update_header()

    def validated_config(self) -> dict[str, str]:
        selected = self.selected_interface()
        interface = selected.name if selected else ""
        if not interface:
            raise ValueError("请选择网口。")
        if selected and not selected.supported:
            detail = f"原因: {selected.reason}" if selected.reason else "该网口不适合用于 DHCP Server。"
            raise ValueError(f"{interface} 不支持作为 DHCP Server 网口。\n{detail}")
        if interface not in list_interfaces():
            raise ValueError("选择的网口不存在。")

        config = {key: entry.get_text().strip() for key, entry in self.entries.items()}
        for key in ("server_ip", "netmask", "pool_start", "pool_end", "lease_time"):
            if not config[key]:
                raise ValueError(f"{key} 不能为空。")

        server_ip = ipaddress.IPv4Address(config["server_ip"])
        pool_start = ipaddress.IPv4Address(config["pool_start"])
        pool_end = ipaddress.IPv4Address(config["pool_end"])
        network = ipaddress.IPv4Network(f"{server_ip}/{config['netmask']}", strict=False)

        if pool_start not in network or pool_end not in network:
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

    def selected_interface(self) -> InterfaceInfo | None:
        active = self.interface_combo.get_active_iter()
        if active is None:
            return None
        name = self.interface_store[active][1]
        if name == "__separator__":
            return None
        supported = self.interface_store[active][2]
        label = self.interface_store[active][0]
        reason = self.interface_store[active][5]
        status = label.removeprefix(name).strip(" ()")
        return InterfaceInfo(name=name, supported=supported, status=status, reason=reason)

    def selected_interface_name(self) -> str:
        active = self.interface_combo.get_active_iter()
        if active is None:
            return ""
        name = self.interface_store[active][1]
        return "" if name == "__separator__" else name

    def selected_scan_interface(self) -> InterfaceInfo | None:
        active = self.scan_interface_combo.get_active_iter()
        if active is None:
            return None
        name = self.scan_interface_store[active][1]
        if name == "__separator__":
            return None
        supported = self.scan_interface_store[active][2]
        label = self.scan_interface_store[active][0]
        reason = self.scan_interface_store[active][5]
        status = label.removeprefix(name).strip(" ()")
        return InterfaceInfo(name=name, supported=supported, status=status, reason=reason)

    def selected_scan_interface_name(self) -> str:
        active = self.scan_interface_combo.get_active_iter()
        if active is None:
            return ""
        name = self.scan_interface_store[active][1]
        return "" if name == "__separator__" else name

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

        network = interface_ipv4_network(selected.name)
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
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()


def card_box() -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    box.get_style_context().add_class("card")
    return box


def scrolled_page(content: Gtk.Widget) -> Gtk.ScrolledWindow:
    scroll = Gtk.ScrolledWindow()
    scroll.set_hexpand(True)
    scroll.set_vexpand(True)
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
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


def copy_to_clipboard(text: str) -> None:
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_text(text, -1)
    clipboard.store()


def is_text_input_focus(window: Gtk.Window) -> bool:
    focus = window.get_focus()
    return isinstance(focus, Gtk.Entry)


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




def list_interfaces() -> list[str]:
    return [info.name for info in discover_interfaces() if info.supported]


def populate_interface_store(
    store: Gtk.ListStore,
    interfaces: list[InterfaceInfo],
    current: str,
    preferred: str = "",
) -> int:
    selected_index = -1
    first_supported_index = -1
    supported_interfaces = [info for info in interfaces if info.supported]
    unsupported_interfaces = [info for info in interfaces if not info.supported]
    ordered_interfaces = supported_interfaces[:]
    if supported_interfaces and unsupported_interfaces:
        ordered_interfaces.append(InterfaceInfo(name="__separator__", supported=False, status=""))
    ordered_interfaces.extend(unsupported_interfaces)

    for index, info in enumerate(ordered_interfaces):
        if info.name == "__separator__":
            store.append(["", info.name, False, "#000000", False, ""])
            continue
        is_unavailable = info.supported and info.status == "不可用"
        color = "#8a8f98" if (not info.supported or is_unavailable) else "#f0f0f0"
        row = [info.label, info.name, info.supported, color, (not info.supported or is_unavailable), info.reason]
        store.append(row)
        if info.supported and first_supported_index == -1:
            first_supported_index = index
        if info.name == current:
            selected_index = index
        if selected_index == -1 and preferred and info.name == preferred:
            selected_index = index

    if selected_index == -1:
        return first_supported_index if first_supported_index != -1 else (0 if interfaces else -1)
    return selected_index


def preferred_dhcp_interface(interfaces: list[InterfaceInfo]) -> str:
    for info in interfaces:
        if info.supported and info.status == "已连接":
            return info.name
    for info in interfaces:
        if info.supported:
            return info.name
    return ""


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


def interface_row_is_separator(model: Gtk.TreeModel, tree_iter: Gtk.TreeIter) -> bool:
    return model[tree_iter][1] == "__separator__"


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


def interface_row_color(state: str, signal: str) -> str:
    normalized = state.lower()
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
    percent_match = re.search(r"\b(\d{1,3})%", signal)
    if percent_match:
        return bars_from_quality(int(percent_match.group(1)))
    rsrp_match = re.search(r"\bRSRP\s+(-?\d+(?:\.\d+)?)", signal, re.IGNORECASE)
    if rsrp_match:
        quality = int((float(rsrp_match.group(1)) + 120) * 100 / 40)
        return bars_from_quality(quality)
    rssi_match = re.search(r"\bRSSI\s+(-?\d+(?:\.\d+)?)", signal, re.IGNORECASE)
    if rssi_match:
        quality = int((float(rssi_match.group(1)) + 113) * 100 / 62)
        return bars_from_quality(quality)
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


def modem_signal_by_port() -> dict[str, dict[str, str]]:
    if shutil.which("mmcli") is None:
        return {}
    list_result = subprocess.run(["mmcli", "-L"], text=True, capture_output=True, check=False)
    if list_result.returncode != 0:
        return {}

    signals: dict[str, dict[str, str]] = {}
    for modem_id in re.findall(r"/Modem/(\d+)", list_result.stdout):
        result = subprocess.run(
            ["mmcli", "-m", modem_id, "--output-keyvalue"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            continue
        data = parse_key_value_output(result.stdout)
        port = data.get("modem.generic.primary-port", "")
        quality = modem_quality_label(data)
        detail = modem_signal_detail(modem_id)
        access = first_key_value(data, "modem.generic.access-technologies.value")
        operator_name = data.get("modem.3gpp.operator-name", "")
        signal_parts = []
        if detail:
            signal_parts.append(detail)
        elif quality:
            signal_parts.append(quality)
        connection_parts = []
        if access and access != "--":
            connection_parts.append(access.upper())
        if operator_name and operator_name != "--":
            connection_parts.append(operator_name)
        label = " ".join(signal_parts) if signal_parts else "-"
        connection = " ".join(connection_parts)
        value = {"signal": label, "connection": connection}
        if port:
            signals[port] = value
        for netdev in modem_net_devices(data.get("modem.generic.device", "")):
            signals[netdev] = value
    return signals


def modem_quality_label(data: dict[str, str]) -> str:
    quality = data.get("modem.generic.signal-quality.value", "")
    recent = data.get("modem.generic.signal-quality.recent", "")
    if not quality or quality == "--" or recent == "no":
        return ""
    return f"{quality}%"


def modem_signal_detail(modem_id: str) -> str:
    result = subprocess.run(
        ["mmcli", "-m", modem_id, "--signal-get"],
        text=True,
        capture_output=True,
        check=False,
    )
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
    result = subprocess.run(
        ["tailscale", "status", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


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
        parts.append(f"This device: {hostname}")
    return "  |  ".join(parts)


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
        name = f"{name} (this device)"
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
    return {
        "name": name,
        "os": os_name,
        "addresses": ipv4,
        "status": status,
        "last_seen": last_seen,
        "exit_node": exit_node,
        "ipv4": ipv4,
        "ipv6": ipv6,
        "dns": dns or "-",
    }


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
        if not name:
            name = reverse_hostname(ip)
        if not name:
            name = ptr_hostname_from_interface_dns(interface, ip)
        if not name:
            name = mdns_hostname(ip)
        if not name:
            name = netbios_hostname(ip)
        result[ip] = name or "-"
    return result


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


def mdns_hostname(ip: str) -> str:
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
            return parts[1].removesuffix(".local")
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


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--list-interfaces":
        for info in discover_interfaces():
            if info.supported:
                print(f"{info.name}: 支持, {info.status}")
            else:
                print(f"{info.name}: 不支持, {info.reason}")
        return 0

    window = NetworkHelperWindow()
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
