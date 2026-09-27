"""root 侧 DNS up/down 脚本——macOS 上 openvpn 不自带 DNS 管线（spec §5.4）。

push 的 dhcp-option 以 ``foreign_option_N`` 环境变量交给 ``--up`` 脚本：
up 脚本收集 DNS/域名搜索项，应用到持有默认路由的网络服务，把原值快照
落盘，并顺带装 IPv6 reject 路由（堵 IPv4-only 隧道的 v6 绕行——
``redirect-gateway ipv6`` 在 macOS 路由装不上，真机实锄；reject 语义
= 立即回 ICMPv6 不可达，happy-eyeballs 瞬间回落 IPv4 进隧道）；down
脚本按快照恢复 + 摘路由。崩溃时 down 不会跑——app 启动/断开后以
``marker 存在`` 为判据，经 sudoers 补跑 down（reconcile）。

竞态免疫（2026-09-28 真机案例：双击重连/孤儿收养场景，旧进程的 down
在新连接的 up 之后迟到执行，把刚应用的 DNS 还原）：down 还原前先
pgrep——还有别的 openvpn 实例在跑就不动（活着的连接拥有 DNS）；
up/down 全程落 ``dns.log``（此前无日志只能考古）。

脚本模板是 raw 三引号串 + ``__PLACEHOLDER__`` 替换（sh 的 ``${}`` 与
Python 花括号转义互不干扰）；内容由本模块唯一持有，经 vpn/privilege.install
以 root 落盘（0755、root-owned——用户不可写是安全模型的一部分）。
``SCRIPTS_VERSION`` 埋进脚本注释：连接时经 ``assets_current`` 比对磁盘
版本（脚本 0755 可读，conf 0600 不可读——conf 重装挂钩脚本版本），
变更即重装全套（修掉「conf 注入逻辑改了、磁盘 conf 用到天荒地老」）。

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
LOG_PATH = f"{SYSTEM_DIR}/dns.log"
# 语义版本：脚本行为或 runtime conf 组成变更时 bump——磁盘旧版触发重装
SCRIPTS_VERSION = "2026-09-28.2"

# IPv6 reject 路由目标：全球单播聚合前缀 2000::/3（黑名单整个公网 v6，
# ULA/链路本地不受影响）
_V6_BLOCK_PREFIX = "2000::/3"

_UP_TEMPLATE = r"""#!/bin/sh
# Magic Stack -- OpenVPN DNS up-script (run as root by openvpn).
# version: __VERSION__
# macOS does not apply pushed dhcp-options: collect foreign_option_N and
# apply them to the service that owns the default route, snapshotting the
# previous values for down/restore. Also installs an IPv6 reject route
# (IPv4-only tunnel -> IPv6 must not leak around it). Idempotent (marker
# short-circuit); every decision logged to __LOG__.
set -u
MARKER="__MARKER__"
BACKUP="__BACKUP__"
LOG="__LOG__"
log() { printf '%s %s\n' "$(/bin/date '+%F %T')" "up: $*" >> "$LOG"; }
log "invoked (PATH='$PATH' dev='$dev' script_type='$script_type')"
[ -f "$MARKER" ] && { log "marker present, skip (idempotent)"; exit 0; }
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
log "collected dns='$dns' search='$search'"
[ -z "$dns" ] && { log "no dhcp-option DNS pushed, nothing to apply"; exit 0; }
# Default-route probe (2026-09-28 real-machine finding: "route get
# default" can come up empty in the instant right after the tunnel comes
# up -- configd re-evaluation window; absolute paths guard against a
# script env whose PATH lacks sbin; raw output logged instead of
# swallowed; 3 retries ride out the transient).
iface=""
try=0
while [ $try -lt 3 ]; do
  raw=$(/sbin/route -n get default 2>&1)
  iface=$(printf '%s\n' "$raw" | /usr/bin/awk '/interface:/ {print $2; exit}')
  [ -n "$iface" ] && break
  try=$((try+1))
  log "default-route probe #$try" "empty, raw: $(printf '%s' "$raw" | /usr/bin/tr '\n' ';')"
  /bin/sleep 1
done
log "default-route iface='$iface'"
[ -z "$iface" ] && { log "no default route after retries, abort"; exit 0; }
svc=$(/usr/sbin/networksetup -listallhardwareports 2>/dev/null | /usr/bin/awk -v i="$iface" '
  /^Hardware Port: / {hp=substr($0, 16)}
  /^Device: / {if ($2 == i) {print hp; exit}}')
log "service='$svc'"
[ -z "$svc" ] && { log "iface maps to no hardware service, abort"; exit 0; }
cur_dns=$(/usr/sbin/networksetup -getdnsservers "$svc" 2>/dev/null)
cur_search=$(/usr/sbin/networksetup -getsearchdomains "$svc" 2>/dev/null)
printf '%s\n%s\n%s\n' "$svc" "$cur_dns" "$cur_search" > "$BACKUP.tmp" && mv "$BACKUP.tmp" "$BACKUP"
/usr/sbin/networksetup -setdnsservers "$svc" $dns && log "applied dns '$dns' to '$svc'" \
  || log "setdnsservers FAILED"
if [ -n "$search" ]; then /usr/sbin/networksetup -setsearchdomains "$svc" $search; fi
# IPv6 reject route: v6 unreachable immediately -> dual-stack apps fall
# back to IPv4 (through the tunnel) instantly
/sbin/route -n add -inet6 -reject __V6BLOCK__ >/dev/null 2>&1 \
  && log "ipv6 reject route added (__V6BLOCK__)" \
  || log "ipv6 reject route add failed (exists?)"
touch "$MARKER"
log "marker set, done"
exit 0
"""

_DOWN_TEMPLATE = r"""#!/bin/sh
# Magic Stack -- OpenVPN DNS down-script (run as root by openvpn): restore
# the snapshotted DNS settings and withdraw the IPv6 reject route. Doubles
# as the crash-reconcile entry -- the app runs it via sudo when the marker
# survived an unclean exit.
# version: __VERSION__
# Generation guard (2026-09-28): if another openvpn instance is still
# running, this late/orphaned down must NOT restore -- the living session
# owns the DNS state. pgrep -x matches the exact process name only: the
# sudo wrapper's command line also contains "openvpn", so -f matching
# would misread the parent sudo as a living instance.
set -u
MARKER="__MARKER__"
BACKUP="__BACKUP__"
LOG="__LOG__"
log() { printf '%s %s\n' "$(/bin/date '+%F %T')" "down: $*" >> "$LOG"; }
log "invoked (PATH='$PATH' ppid='$PPID')"
[ -f "$MARKER" ] && log "marker present" || exit 0
others=$(/usr/bin/pgrep -x openvpn 2>/dev/null | /usr/bin/grep -vw "$PPID" | /usr/bin/head -5)
if [ -n "$others" ]; then
  log "another openvpn alive (pids: $others), skip restore"
  exit 0
fi
/sbin/route -n delete -inet6 -reject __V6BLOCK__ >/dev/null 2>&1 \
  && log "ipv6 reject route withdrawn" || true
[ -f "$BACKUP" ] || { rm -f "$MARKER"; log "no backup, marker removed"; exit 0; }
svc=$(sed -n 1p "$BACKUP")
cur_dns=$(sed -n 2p "$BACKUP")
cur_search=$(sed -n 3p "$BACKUP")
restore() {
  _svc="$1"; _cur="$2"; _setter="$3"
  case "$_cur" in
    ""|*"There aren't any"*|*"There are no"*) /usr/sbin/networksetup "$_setter" "$_svc" "Empty" ;;
    *) /usr/sbin/networksetup "$_setter" "$_svc" $_cur ;;
  esac
}
restore "$svc" "$cur_dns" -setdnsservers
restore "$svc" "$cur_search" -setsearchdomains
rm -f "$MARKER" "$BACKUP"
log "restored '$svc' dns='$cur_dns'"
exit 0
"""

UP_SCRIPT = (_UP_TEMPLATE
             .replace("__VERSION__", SCRIPTS_VERSION)
             .replace("__MARKER__", MARKER_PATH)
             .replace("__BACKUP__", BACKUP_PATH)
             .replace("__LOG__", LOG_PATH)
             .replace("__V6BLOCK__", _V6_BLOCK_PREFIX))
DOWN_SCRIPT = (_DOWN_TEMPLATE
               .replace("__VERSION__", SCRIPTS_VERSION)
               .replace("__MARKER__", MARKER_PATH)
               .replace("__BACKUP__", BACKUP_PATH)
               .replace("__LOG__", LOG_PATH)
               .replace("__V6BLOCK__", _V6_BLOCK_PREFIX))


def assets_current() -> bool:
    """磁盘上的 dns 脚本是否已是当前版本（脚本 0755 可读；conf 0600
    不可读——conf 的重装挂在本版本比对上：版本 bump 意味着重装全套）。"""
    marker = f"# version: {SCRIPTS_VERSION}"
    for path in (DNS_UP_PATH, DNS_DOWN_PATH):
        try:
            with open(path, encoding="utf-8") as f:
                if marker not in f.read():
                    return False
        except OSError:
            return False
    return True


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
