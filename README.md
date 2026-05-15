# Network Helper

Network Helper is a local GTK desktop utility for small network maintenance tasks:
running a DHCP server on a selected interface, scanning a LAN, checking interface
status, and viewing Tailscale devices.

It is designed for workflows such as connecting a machine directly to a server
over Ethernet, assigning an address with DHCP, and then finding the target device
on the local network.

## Features

- DHCP Server tab for serving addresses on a selected wired interface.
- LAN Scan tab for scanning hosts in the selected interface's IPv4 subnet.
- Interface tab with NetworkManager-style device status, addresses, Wi-Fi signal,
  cellular signal, and Tailscale interface state.
- Tailscale tab with a device list, online state, Tailscale IPv4 address, and a
  right-click copy menu for IPv4, IPv6, and DNS name.
- Keyboard shortcuts for tab switching and common actions.

## Requirements

- Python 3
- PyGObject / GTK 3
- `dnsmasq`
- `iproute2`
- `iputils-ping`
- `pkexec` or `sudo` for DHCP start/stop privilege elevation

Optional integrations:

- `nmcli` for NetworkManager interface state
- `mmcli` for cellular modem signal
- `tailscale` for Tailscale device status
- `avahi-resolve` for mDNS hostname lookup

On Debian/Ubuntu:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 dnsmasq iproute2 iputils-ping policykit-1
```

## Run

```bash
./run.sh
```

Starting or stopping the DHCP server may prompt for administrator privileges via
`pkexec` or `sudo`.

## Install Desktop Launcher

Install the app launcher and icon for the current user:

```bash
./scripts/install-user-desktop.sh
```

This installs:

- `~/.local/share/applications/network-helper.desktop`
- `~/.local/share/icons/hicolor/scalable/apps/network-helper.svg`

## Shortcuts

Tabs:

- `D`: DHCP Server
- `L`: LAN Scan
- `I`: Interface
- `T`: Tailscale
- `Ctrl+1` through `Ctrl+4`: switch tabs directly
- `Alt+Left` / `Alt+Right`: switch to the previous or next tab

Actions:

- `R`: refresh the current page
- `S`: run the current primary action, such as Start, Stop, or Scan

Text fields do not trigger direct letter shortcuts while focused.

## DHCP Server

1. Select the wired interface to serve DHCP on.
2. Set the local server address, netmask, address pool, lease time, and optional
   gateway/DNS values.
3. Click `Start`.
4. Connect the selected interface to the target machine.
5. Stop the server with `Stop` when finished.

Runtime files are stored under:

```text
/tmp/network-helper/dhcp
```

Notes:

- Do not select an interface currently used for remote access or internet access.
  Starting DHCP flushes and reconfigures addresses on the selected interface.
- NAT is not configured automatically. The DHCP server only assigns addresses.
- Stopping the DHCP server does not restore the interface's previous address
  configuration.

## LAN Scan

1. Select the interface to scan.
2. Click `Scan`.

The default scan interface is selected from the highest-priority IPv4 default
route. Scanning is limited to `/23` or smaller subnets to avoid accidental large
network scans.

Hostnames are resolved from local hosts files, DHCP leases, reverse DNS, mDNS,
and NetBIOS when available. Devices that do not expose a hostname will show `-`.

## Interface

The Interface tab shows device status similar to `nmcli dev status`, plus
addresses and signal details:

- Wi-Fi signal is shown as a percentage with a four-bar indicator.
- Cellular signal is read from ModemManager when available.
- Tailscale interface state is read from `tailscale status --json`.

## Tailscale

The Tailscale tab shows devices from `tailscale status --json`.

The address column displays only the Tailscale IPv4 address. Right-click a device
row to copy its IPv4 address, IPv6 address, or DNS name.

## Debug Interface Detection

List DHCP Server interface filtering without opening the GUI:

```bash
/usr/bin/python3 network_helper_gui.py --list-interfaces
```
