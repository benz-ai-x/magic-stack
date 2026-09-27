"""OpenVPN profile（.ovpn）解析、校验与净化——安全模型的核心闸（spec §5.1）。

openvpn 以 root 跑（sudoers 钉死 argv），而 ``up``/``plugin`` 等指令让
「config 内容可控 = root 任意执行」。本模块是剥除这类指令的单一归宿：
用户 profile 经 :func:`sanitize_profile` 净化后才允许进 runtime conf；
管理/生命周期类指令（``--management`` 族、daemon、log 重定向）由 app
注入的固定 argv 独占，用户侧一律剥除，防劫持。

纯文本行导向解析：inline 块（``<ca>…</ca>`` 等）整段跳过——块内出现
指令形状的行（base64 裸眼相同时）绝不误伤。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── 净化指令表 ────────────────────────────────────────────────────

# 脚本/执行类——config 即代码，必须剥（sudoers argv 钉死之外的唯一执行面）
SCRIPT_DIRECTIVES = frozenset({
    "up", "down", "down-pre", "iproute", "route-up", "route-pre-down",
    "script-security", "tls-verify", "learn-address",
    "client-connect", "client-disconnect", "plugin", "chroot",
})

# 管理/生命周期类——app 注入的固定 argv 独占；用户 profile 出现即剥
MANAGEMENT_DIRECTIVES = frozenset({
    "management", "management-client", "management-query-passwords",
    "management-hold", "management-signal", "management-forget-disconnect",
    "management-client-user", "management-client-group",
    "management-external-key", "management-external-cert",
    "management-log-cache", "management-up-down",
    "daemon", "inetd", "writepid", "log", "log-append",
    "status", "status-version", "auth-retry",
})

# inline 块标记（块内行不参与解析/净化；官方全集见 man inline-files）
_INLINE_TAGS = frozenset({
    "ca", "cert", "key", "dh", "extra-certs", "pkcs12", "crl-verify",
    "http-proxy-user-pass", "tls-auth", "auth-gen-token-secret",
    "peer-fingerprint", "tls-crypt", "tls-crypt-v2", "verify-hash",
    "connection", "auth-user-pass",
})


@dataclass
class ProfileInfo:
    """解析结果：元数据供 UI/校验消费，remotes/auth 是运行时消费面。"""
    remotes: list = field(default_factory=list)      # [(host, port)]
    proto: str = ""                                   # 全局 proto（缺省 udp）
    has_connection_block: bool = False                # <connection> 备选连接块
    auth_user_pass: bool = False                      # auth-user-pass（查询式或 inline）
    auth_inline: bool = False                         # <auth-user-pass> 内嵌凭证
    missing_client_role: bool = False                 # 无 client/pull——push 不生效
    removed: list = field(default_factory=list)       # [(directive, 原始行)]
    error: str = ""                                   # 非空 = 不可用（英文码，UI 层映射）

    @property
    def needs_credentials(self) -> bool:
        """连接时是否需要经管理口注入用户名密码（查询式 auth-user-pass）。"""
        return self.auth_user_pass and not self.auth_inline

    @property
    def has_endpoint(self) -> bool:
        return bool(self.remotes) or self.has_connection_block


def _directive_of(line: str) -> str:
    """行首 token（小写）；注释/空行/inline 标记行返回 ''。"""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return ""
    if stripped.startswith("<"):
        return ""
    return stripped.split()[0].lower()


def _is_inline_open(line: str) -> str | None:
    """``<tag>`` 行 → tag（小写）；非块开始返回 None。"""
    stripped = line.strip()
    if stripped.startswith("<") and not stripped.startswith("</") and stripped.endswith(">"):
        return stripped[1:-1].strip().lower()
    return None


def _is_inline_close(line: str) -> bool:
    return line.strip().startswith("</")


def parse_profile(text: str, *, sanitize: bool = False) -> ProfileInfo:
    """解析（sanitize=True 时同时产出净化文本，见 sanitize_profile）。

    行导向状态机：inline 块深度 >0 时行只透传不解析。
    """
    info = ProfileInfo()
    out_lines = []
    in_inline = 0
    has_role = False
    for raw in (text or "").splitlines():
        if in_inline:
            out_lines.append(raw)
            if _is_inline_close(raw):
                in_inline -= 1
            continue

        tag = _is_inline_open(raw)
        if tag:
            out_lines.append(raw)
            in_inline += 1
            if tag == "connection":
                info.has_connection_block = True
            elif tag == "auth-user-pass":
                info.auth_user_pass = True
                info.auth_inline = True
            continue

        directive = _directive_of(raw)
        if not directive:
            out_lines.append(raw)
            continue

        if directive == "remote":
            parts = raw.strip().split()
            host = parts[1] if len(parts) > 1 else ""
            port = parts[2] if len(parts) > 2 else ""
            if host:
                info.remotes.append((host, port))
            out_lines.append(raw)
            continue
        if directive == "proto":
            parts = raw.strip().split()
            info.proto = parts[1].lower() if len(parts) > 1 else ""
            out_lines.append(raw)
            continue
        if directive in ("client", "pull"):
            has_role = True
            out_lines.append(raw)
            continue
        if directive == "auth-user-pass":
            # 带 file 参数 → 改写为查询式（经管理口注入，凭证不落盘）
            info.auth_user_pass = True
            if len(raw.strip().split()) == 1:
                out_lines.append(raw)
            else:
                info.removed.append(("auth-user-pass", raw.strip()))
                out_lines.append("auth-user-pass")
            continue

        if sanitize and (directive in SCRIPT_DIRECTIVES
                         or directive in MANAGEMENT_DIRECTIVES):
            info.removed.append((directive, raw.strip()))
            continue
        out_lines.append(raw)

    if not info.has_endpoint:
        info.error = "no_remote"
    # client/pull 缺失只告警不拒绝（静态 key 点对点 profile 合法）
    info.missing_client_role = not has_role
    info._sanitized = "\n".join(out_lines) + ("\n" if out_lines else "")
    return info


def sanitize_profile(text: str) -> tuple:
    """净化入口：返回 (净化后文本, ProfileInfo)。剥除脚本/管理类指令、
    auth-user-pass 改查询式；removed 逐条记录供导入 UI 告警确认。"""
    info = parse_profile(text, sanitize=True)
    return info._sanitized, info
