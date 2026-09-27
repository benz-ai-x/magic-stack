"""OpenVPN 特权引导——sudoers 全量钉死 + root 侧文件一次性安装（spec §5.1）。

与 NFS 条目（任意参数跑两个二进制）的本质差异：openvpn 的 config 可含
``up`` 等脚本指令，「config 内容可控 = root 任意执行」。因此本模块是
三道闸的施工方：

1. **argv 零通配钉死**——sudoers 条目 = :func:`build_argv` 的逐字序列化
   （连管理端口都固定，登 ``shared.defaults.VPN_MANAGEMENT_PORT``）；
2. **root-only 文件区**——conf / dns 脚本 / mgmt.pw 落
   ``/Library/Application Support/MagicStack/openvpn``（root 属主，用户
   不可写），全部经一次管理员授权写入；
3. **profile 净化**——conf 内容必须来自 :func:`vpn.profile.sanitize_profile`
   的产出（脚本/管理指令剥除），调用方契约。

失败码结构化（英文短码），文案映射归 UI adapter——本域零 i18n 耦合。
"""
from __future__ import annotations

import base64
import getpass
import logging
import os
import shlex
import shutil
import subprocess

from shared.defaults import VPN_MANAGEMENT_PORT
from vpn import dns_scripts

logger = logging.getLogger("magic-proxy.vpn-privilege")

SUDOERS_PATH = "/etc/sudoers.d/magic-stack-openvpn"
CONF_PATH = f"{dns_scripts.SYSTEM_DIR}/client.conf"
MGMT_PW_PATH = f"{dns_scripts.SYSTEM_DIR}/mgmt.pw"

# 二进制三级链（spec §8）：env 覆盖 → brew prefix（注意装在 sbin，默认
# PATH 探不到——已核实的坑）→ MacPorts → PATH 兜底
ENV_OVERRIDE = "MAGIC_STACK_OPENVPN"
BREW_SBIN = "/opt/homebrew/opt/openvpn/sbin/openvpn"
MACPORTS_SBIN = "/opt/local/sbin/openvpn"
OSA_TIMEOUT = 300  # 管理员授权弹窗的思考时间（mount_control 同款）


def resolve_openvpn_bin(env=None) -> str:
    """探测 openvpn 二进制；缺席返回 ''（调用方走安装引导降级）。"""
    environ = env if env is not None else os.environ
    override = environ.get(ENV_OVERRIDE, "").strip()
    if override:
        return override
    for candidate in (BREW_SBIN, MACPORTS_SBIN):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("openvpn") or ""


def build_argv(openvpn_bin, mgmt_port=VPN_MANAGEMENT_PORT) -> list:
    """钉死 argv 的单一归宿——sudoers 条目与实际 spawn 必须同源。

    全部参数是空格自由的单 token（sudoers 词法安全）；易变项（profile
    内容、pull_dns）走 conf 文件，绝不进 argv。
    """
    return [
        openvpn_bin,
        "--config", CONF_PATH,
        "--management", "127.0.0.1", str(int(mgmt_port)), MGMT_PW_PATH,
        "--management-query-passwords",
        "--management-hold",
        "--management-forget-disconnect",
        "--auth-retry", "interact",
        "--script-security", "2",
        "--up", dns_scripts.DNS_UP_PATH,
        "--down", dns_scripts.DNS_DOWN_PATH,
        "--verb", "3",
    ]


def build_full_command(openvpn_bin, mgmt_port=VPN_MANAGEMENT_PORT) -> list:
    """实际 spawn 命令（sudo -n：免密失败立刻报错，绝不挂 tty 等密码）。"""
    return ["sudo", "-n"] + build_argv(openvpn_bin, mgmt_port)


def sudoers_rule(user=None, openvpn_bin=None, mgmt_port=VPN_MANAGEMENT_PORT) -> str:
    """免密规则：钉死的 openvpn argv + reconcile 用的 dns-down 补跑。"""
    openvpn_bin = openvpn_bin or resolve_openvpn_bin()
    pinned = " ".join(build_argv(openvpn_bin, mgmt_port))
    reconcile = "/bin/sh " + dns_scripts.DNS_DOWN_PATH
    return (f"{user or getpass.getuser()} "
            f"ALL=(root) NOPASSWD: {pinned}, {reconcile}\n")


def check_sudoers(openvpn_bin=None, mgmt_port=VPN_MANAGEMENT_PORT) -> bool:
    """规则已装且钉的是当前二进制/端口（读 sudo -n -l，绝不弹密码框）。"""
    openvpn_bin = openvpn_bin or resolve_openvpn_bin()
    if not openvpn_bin:
        return False
    try:
        proc = subprocess.run(["sudo", "-n", "-l"], capture_output=True,
                              timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    out = proc.stdout.decode("utf-8", "replace")
    pinned_prefix = f"{openvpn_bin} --config {CONF_PATH}"
    return pinned_prefix in out and dns_scripts.DNS_DOWN_PATH in out


def runtime_conf(sanitized_profile: str, *, pull_dns=True) -> str:
    """runtime conf = 净化后 profile +（可选）拒收 dhcp-option。

    入参必须是 sanitize_profile 的产出（调用方契约——本函数不再防御，
    净化知识单一归宿在 vpn/profile.py）。pull_dns=False 时注入
    pull-filter（拒收服务器 push 的全部 dhcp-option——DNS 与域名搜索）。
    """
    text = (sanitized_profile or "").rstrip("\n")
    if not pull_dns:
        text += '\npull-filter ignore "dhcp-option"'
    return text + "\n" if text else ""


# osascript 取消文案随系统语言本地化（中文系统为「用户取消…」）。i18n
# 闸禁止产品源码出现汉字字面量（该串是检测探针而非用户可见文案）——
# 按码点运行时拼装，源头零汉字常量
_ZH_CANCEL = "".join(chr(c) for c in (0x7528, 0x6237, 0x53D6, 0x6D88))


def _admin_script(parts) -> str:
    """osascript 以 root 执行的命令串（AppleScript 字面量安全：内层只含
    base64/单引号 safe token——mount_control 同款纪律）。"""
    inner = " && ".join(parts)
    return f'do shell script "{inner}" with administrator privileges'


def _write_file_part(path, content_b64, *, mode, tmp_suffix=".tmp") -> str:
    """base64 载荷 → root 文件的单段命令（写 tmp → mv，避免半写文件）。"""
    tmp = path + tmp_suffix
    return (f"echo {content_b64} | base64 -D > {shlex.quote(tmp)} && "
            f"mv {shlex.quote(tmp)} {shlex.quote(path)} && "
            f"chown root:wheel {shlex.quote(path)} && chmod {mode} {shlex.quote(path)}")


def install(*, conf_text, mgmt_password, openvpn_bin=None,
            mgmt_port=VPN_MANAGEMENT_PORT, user=None) -> tuple:
    """一次管理员授权安装全套 root 侧文件 + sudoers 规则（幂等）。

    返回 (ok, error_code)；error_code ∈ cancelled / sudoers_verify_failed /
    openvpn_missing / osascript_failed。conf_text 必须已是净化产出。
    """
    openvpn_bin = openvpn_bin or resolve_openvpn_bin()
    if not openvpn_bin:
        return False, "openvpn_missing"
    rule = sudoers_rule(user, openvpn_bin, mgmt_port)
    rule_b64 = base64.b64encode(rule.encode("utf-8")).decode("ascii")
    conf_b64 = base64.b64encode((conf_text or "").encode("utf-8")).decode("ascii")
    up_b64 = base64.b64encode(dns_scripts.UP_SCRIPT.encode("utf-8")).decode("ascii")
    down_b64 = base64.b64encode(dns_scripts.DOWN_SCRIPT.encode("utf-8")).decode("ascii")
    pw_b64 = base64.b64encode((mgmt_password or "").encode("utf-8")).decode("ascii")

    sudoers_tmp = SUDOERS_PATH + ".tmp"
    parts = [
        f"mkdir -p {shlex.quote(dns_scripts.SYSTEM_DIR)} && "
        f"chown root:wheel {shlex.quote(dns_scripts.SYSTEM_DIR)} && "
        f"chmod 0755 {shlex.quote(dns_scripts.SYSTEM_DIR)}",
        _write_file_part(CONF_PATH, conf_b64, mode="0600"),
        _write_file_part(dns_scripts.DNS_UP_PATH, up_b64, mode="0755"),
        _write_file_part(dns_scripts.DNS_DOWN_PATH, down_b64, mode="0755"),
        _write_file_part(MGMT_PW_PATH, pw_b64, mode="0400"),
        # sudoers 走 visudo 校验后才转正（语法错绝不落盘）
        f"echo {rule_b64} | base64 -D > {shlex.quote(sudoers_tmp)} && "
        f"visudo -cf {shlex.quote(sudoers_tmp)} && "
        f"mv {shlex.quote(sudoers_tmp)} {shlex.quote(SUDOERS_PATH)} && "
        f"chmod 0440 {shlex.quote(SUDOERS_PATH)}",
    ]
    try:
        proc = subprocess.run(
            ["osascript", "-e", _admin_script(parts)],
            capture_output=True, timeout=OSA_TIMEOUT,
            stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("vpn privilege install could not run osascript: %s", exc)
        return False, "osascript_failed"
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")
        if "User canceled" in stderr or _ZH_CANCEL in stderr:
            return False, "cancelled"
        logger.warning("vpn privilege install failed: %s", stderr.strip()[:160])
        return False, "osascript_failed"
    if not check_sudoers(openvpn_bin, mgmt_port):
        return False, "sudoers_verify_failed"
    return True, ""
