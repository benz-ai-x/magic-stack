"""root 侧 DNS up/down 脚本——macOS 上 openvpn 不自带 DNS 管线（spec §5.4）。

push 的 dhcp-option 以 ``foreign_option_N`` 环境变量交给 ``--up`` 脚本：
up 脚本收集 DNS/域名搜索项，应用到持有默认路由的网络服务，并把原值快照
落盘；down 脚本按快照恢复。崩溃时 down 不会跑——app 启动/断开后以
``marker 存在`` 为判据，经 sudoers 补跑 down（reconcile）。

脚本模板是 raw 三引号串 + ``__PLACEHOLDER__`` 替换（sh 的 ``${}`` 与
Python 花括号转义互不干扰）；内容由本模块唯一持有，经 vpn/privilege.install
以 root 落盘（0755、root-owned——用户不可写是安全模型的一部分）。

路径刻意无空格（``/Library/MagicStack/openvpn``）：sudoers 命令匹配按
空白分词，含空格的参数要么加引号要么整类脆断——无空格根上消除
（Tunnelblick 用 "Application Support" 但它不走 sudoers 钉死路线）。
"""
from __future__ import annotations

import os
import subprocess

SYSTEM_DIR = "/Library/MagicStack/openvpn"
DNS_UP_PATH = f"{SYSTEM_DIR}/dns-up.sh"
DNS_DOWN_PATH = f"{SYSTEM_DIR}/dns-down.sh"
MARKER_PATH = f"{SYSTEM_DIR}/dns-active"
BACKUP_PATH = f"{SYSTEM_DIR}/dns-backup.txt"

_UP_TEMPLATE = r"""#!/bin/sh
# Magic Stack -- OpenVPN DNS up-script (run as root by openvpn).
# macOS does not apply pushed dhcp-options: collect foreign_option_N and
# apply them to the service that owns the default route, snapshotting the
# previous values for down/restore. Idempotent (marker short-circuit).
set -u
MARKER="__MARKER__"
BACKUP="__BACKUP__"
[ -f "$MARKER" ] && exit 0
dns=""
search=""
n=1
while :; do
  eval "v=\${foreign_option_$n:-}"
  [ -n "$v" ] || break
  case "$v" in
    "dhcp-option DNS "*) dns="$dns ${v#dhcp-option DNS }" ;;
    "dhcp-option DOMAIN "*|"dhcp-option DOMAIN-SEARCH "*|"dhcp-option SEARCH-DOMAIN "*|"dhcp-option ADAPTER_DOMAIN_SUFFIX "*) search="$search ${v#dhcp-option * }" ;;
  esac
  n=$((n+1))
done
[ -z "$dns" ] && exit 0
iface=$(route -n get default 2>/dev/null | awk '/interface:/ {print $2; exit}')
[ -z "$iface" ] && exit 0
svc=$(networksetup -listallhardwareports 2>/dev/null | awk -v i="$iface" '
  /^Hardware Port: / {hp=substr($0, 16)}
  /^Device: / {if ($2 == i) {print hp; exit}}')
[ -z "$svc" ] && exit 0
cur_dns=$(networksetup -getdnsservers "$svc" 2>/dev/null)
cur_search=$(networksetup -getsearchdomains "$svc" 2>/dev/null)
printf '%s\n%s\n%s\n' "$svc" "$cur_dns" "$cur_search" > "$BACKUP.tmp" && mv "$BACKUP.tmp" "$BACKUP"
networksetup -setdnsservers "$svc" $dns
if [ -n "$search" ]; then networksetup -setsearchdomains "$svc" $search; fi
touch "$MARKER"
exit 0
"""

_DOWN_TEMPLATE = r"""#!/bin/sh
# Magic Stack -- OpenVPN DNS down-script (run as root by openvpn): restore
# the snapshotted DNS settings. Doubles as the crash-reconcile entry -- the
# app runs it via sudo when the marker survived an unclean exit.
set -u
MARKER="__MARKER__"
BACKUP="__BACKUP__"
[ -f "$MARKER" ] || exit 0
[ -f "$BACKUP" ] || { rm -f "$MARKER"; exit 0; }
svc=$(sed -n 1p "$BACKUP")
cur_dns=$(sed -n 2p "$BACKUP")
cur_search=$(sed -n 3p "$BACKUP")
restore() {
  _svc="$1"; _cur="$2"; _setter="$3"
  case "$_cur" in
    ""|*"There aren't any"*|*"There are no"*) networksetup "$_setter" "$_svc" "Empty" ;;
    *) networksetup "$_setter" "$_svc" $_cur ;;
  esac
}
restore "$svc" "$cur_dns" -setdnsservers
restore "$svc" "$cur_search" -setsearchdomains
rm -f "$MARKER" "$BACKUP"
exit 0
"""

UP_SCRIPT = _UP_TEMPLATE.replace("__MARKER__", MARKER_PATH).replace(
    "__BACKUP__", BACKUP_PATH)
DOWN_SCRIPT = _DOWN_TEMPLATE.replace("__MARKER__", MARKER_PATH).replace(
    "__BACKUP__", BACKUP_PATH)


def marker_exists() -> bool:
    """DNS 应用标志还在 = down 脚本没跑过（崩溃/强杀）——reconcile 判据。"""
    return os.path.exists(MARKER_PATH)


def reconcile_command() -> list:
    """补跑 down 脚本的 sudo 命令（sudoers 钉死的第二条命令）。"""
    return ["sudo", "-n", "/bin/sh", DNS_DOWN_PATH]


def run_reconcile(timeout=10) -> bool:
    """崩溃清理：标志在才跑（幂等）。绝不抛异常。"""
    if not marker_exists():
        return True
    try:
        proc = subprocess.run(reconcile_command(), capture_output=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and not marker_exists()
