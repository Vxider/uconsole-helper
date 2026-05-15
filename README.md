# Network Helper 图形界面

这是一个本机运行的 GTK 桌面图形界面，用于 DHCP Server 配置和 LAN Scan。典型用途是把本机 `eth0` 通过网线直连到另一台服务器，让对方从本机获取 IP，然后扫描同网段设备，方便后续登录和维护。

## 依赖

- Python 3
- PyGObject / GTK 3
- `dnsmasq`
- `iproute2`
- `iputils-ping`
- `pkexec` 或 `sudo`，用于启动/停止时提权

Debian/Ubuntu 可安装：

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 dnsmasq iproute2 iputils-ping policykit-1
```

## 启动

```bash
./run.sh
```

会直接打开本地 GTK 桌面窗口。启动 DHCP Server 或停止时会通过 `pkexec` 或 `sudo` 请求管理员权限。

也可以把 `network-helper.desktop` 放到 `~/.local/share/applications/`，之后从桌面应用菜单启动。

## 使用方式

顶部标签支持点击切换，也支持快捷键：

- `Ctrl+1`: DHCP
- `Ctrl+2`: LAN Scan
- `Alt+Left` / `Alt+Right`: 前后切换标签

### DHCP

1. 选择要作为 DHCP Server 的网口，例如 `eth0`。支持的网口会显示“已连接 / 未连接 / 不可用”；无线、Tailscale/tun、loopback、虚拟网口、蜂窝/USB modem 网络设备会置灰并标注“不支持”。
2. 设置本机地址，例如 `192.168.50.1`。
3. 设置地址池，例如 `192.168.50.100` 到 `192.168.50.200`。
4. 点击“启动 DHCP Server”。
5. 将该网口通过网线连接到目标服务器，目标服务器设置为 DHCP 获取地址。

启动时程序会把所选网口配置为静态地址，并在该网口上提供 DHCP 服务。运行时文件位于 `/tmp/network-helper/dhcp`。

### LAN Scan

1. 切换到 `LAN Scan` 标签。
2. 选择要扫描的网口。
3. 点击“扫描 LAN”。

扫描会根据该网口当前 IPv4 地址计算网段，对网段内主机做一次 ping 探测，然后读取系统邻居表展示 IP、MAC、状态和主机名。为避免误扫大网段，当前限制为 /23 或更小网段。

## 注意

- 不要选择当前正在上网的网口，否则程序会刷新该网口地址，可能导致网络中断。
- 默认不提供 NAT，只负责给直连设备分配 IP。如需让对方通过本机上网，需要额外配置转发和 NAT。
- 停止 DHCP Server 不会自动恢复网口启动前的地址配置。

## 调试网口识别

不用打开 GUI 也可以查看筛选结果：

```bash
/usr/bin/python3 network_helper_gui.py --list-interfaces
```
