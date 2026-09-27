"""macOS Keychain helpers for SSH passwords (Security framework).

Uses SecItemAdd/SecItemCopyMatching/SecItemDelete directly so the password
never appears in a subprocess argv — the old `security -w <pw>` CLI path was
briefly visible in `ps` (#40).

Security 缺失（Linux 容器）时模块仍可导入：Security=None，公开函数的
全吞异常兜底（keychain must never raise to UI）把 None 解引用转成
False/""——Docker 路径本就不调用这些函数。

槽位模型：一切条目都是 (SERVICE, account) 二元组。_slot_add/_slot_get/
_slot_delete 是仅有的三条 SecItem 原语（评审去重：此前 set/get/delete
在 password/sudo/vpn 三族里是逐份拷贝）；各槽位函数只负责账户名推导与
守卫语义（host 守卫、legacy 回退读、NotFound 容忍）。
"""
from __future__ import annotations  # PEP 604 注解惰性求值——3.9 下界兼容（#71 S15）

import logging

try:
    import Security
except ImportError:
    Security = None

logger = logging.getLogger("magic-proxy.keychain")

SERVICE = "com.magic-proxy"


# v2 服务器形状（ADR-011）：连接参数在 ``ssh`` 节。账户名字符串是
# Keychain 里已落盘的兼容契约——**逐字节不变**（tunnel:{id} 与
# user@host:port），只换字段读取路径。

def _ssh_host(node: dict) -> str:
    ssh = node.get("ssh") if isinstance(node.get("ssh"), dict) else {}
    return ssh.get("host", "")


def _account(server: dict) -> str:
    """凭证账户名：优先稳定 id（issue #8）；无 id 时为 legacy 推导。"""
    stable_id = server.get("id")
    if stable_id:
        return f"tunnel:{stable_id}"
    return _legacy_account(server)


def _legacy_account(server: dict) -> str:
    ssh = server.get("ssh") if isinstance(server.get("ssh"), dict) else {}
    user = ssh.get("user", "")
    host = ssh.get("host", "")
    port = ssh.get("port", 22)
    return f"{user}@{host}:{port}"


def _slot_query(account: str) -> dict:
    return {
        Security.kSecClass: Security.kSecClassGenericPassword,
        Security.kSecAttrService: SERVICE,
        Security.kSecAttrAccount: account,
    }


# ── 槽位三原语（唯一的 SecItem 触点）─────────────────────────────

def _slot_add(account: str, password: str, label: str) -> bool:
    """写入槽位（先删后加保 -U 语义）。失败只记状态码，密码绝不进日志。"""
    try:
        Security.SecItemDelete(_slot_query(account))
        attrs = _slot_query(account)
        attrs[Security.kSecValueData] = password.encode("utf-8")
        status = Security.SecItemAdd(attrs, None)
        ok = status[0] == Security.errSecSuccess if isinstance(status, tuple) \
            else status == Security.errSecSuccess
        if not ok:
            logger.warning("Keychain %s set failed: status %s", label, status)
        return ok
    except Exception as e:  # noqa: BLE001 — keychain must never raise to UI
        logger.warning("Keychain %s set failed: %s", label, type(e).__name__)
        return False


def _slot_get(account: str) -> str:
    """读槽位；缺席/失败一律 ''。"""
    try:
        query = _slot_query(account)
        query[Security.kSecReturnData] = True
        query[Security.kSecMatchLimit] = Security.kSecMatchLimitOne
        status, data = Security.SecItemCopyMatching(query, None)
        if status == Security.errSecSuccess and data is not None:
            return bytes(data).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("Keychain get failed: %s", type(e).__name__)
    return ""


def _slot_delete(account: str, label: str) -> bool:
    """删槽位；NotFound（条目本就不存在）视为成功，其他非零如实上报。"""
    try:
        status = Security.SecItemDelete(_slot_query(account))
        if status not in (0, getattr(Security, "errSecItemNotFound", -25300)):
            logger.warning("Keychain %s delete status %s", label, status)
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Keychain %s delete failed: %s", label, type(e).__name__)
        return False


# ── SSH 登录密码槽（tunnel:{id}）──────────────────────────────────

def set_password(tunnel: dict, password: str) -> bool:
    if not _ssh_host(tunnel):
        return False
    return _slot_add(_account(tunnel), password, "set")


def get_password(server: dict) -> str:
    if not _ssh_host(server):
        return ""
    accounts = [_account(server)]
    legacy = _legacy_account(server)
    if legacy not in accounts:
        accounts.append(legacy)  # 迁移期回退读（issue #8）
    for account in accounts:
        value = _slot_get(account)
        if value:
            return value
    return ""


def delete_legacy_password(server: dict) -> bool:
    """仅清 legacy 账户（user@host:port）——re-pin 收敛用，不动 id 账户。"""
    return _slot_delete(_legacy_account(server), "legacy")


def delete_password(server: dict) -> bool:
    """删除服务器 SSH 密码。返回是否成功（条目本就不存在视为成功）。"""
    if not _ssh_host(server):
        return True
    ok = True
    for account in {_account(server), _legacy_account(server)}:
        ok = _slot_delete(account, "delete") and ok
    return ok


# ── 远程 sudo 密码槽（ADR-007：NFS 一键安装的远程提权凭据）────────
# 密钥登录的隧道没有 ssh 密码可复用——UI 显式输入一次后存独立账户槽，
# 与隧道登录密码互不混淆。

def _sudo_account(server: dict) -> str:
    return f"nfs-sudo:{_account(server)}"


def set_sudo_password(server: dict, password: str) -> bool:
    if not _ssh_host(server):
        return False
    return _slot_add(_sudo_account(server), password, "sudo")


def get_sudo_password(server: dict) -> str:
    if not _ssh_host(server):
        return ""
    return _slot_get(_sudo_account(server))


def delete_sudo_password(server: dict) -> bool:
    """删除远程 sudo 密码槽（服务器删除时随 all 清理）。"""
    return _slot_delete(_sudo_account(server), "sudo")


# ── OpenVPN 凭证槽（docs/openvpn-client-spec.md §5.5）─────────────
# 两条独立槽：per-server 的用户密码（经管理口注入、永不落盘）与全局的
# 管理口密码（稳定值——崩溃后收养残留 openvpn 的锚点，§5.4）。

def _vpn_account(server: dict) -> str:
    return f"vpn:{_account(server)}"


def set_vpn_password(server: dict, password: str) -> bool:
    return _slot_add(_vpn_account(server), password, "vpn")


def get_vpn_password(server: dict) -> str:
    return _slot_get(_vpn_account(server))


def delete_vpn_password(server: dict) -> bool:
    """删除服务器 VPN 密码槽（服务器删除时清理）。"""
    return _slot_delete(_vpn_account(server), "vpn")


# 管理口密码槽（全局——不绑服务器）
VPN_MGMT_ACCOUNT = "vpn-mgmt"


def set_vpn_mgmt_password(password: str) -> bool:
    return _slot_add(VPN_MGMT_ACCOUNT, password, "vpn-mgmt")


def get_vpn_mgmt_password() -> str:
    return _slot_get(VPN_MGMT_ACCOUNT)


def ensure_vpn_mgmt_password() -> str:
    """取管理口密码；首次调用生成并落盘（幂等）。失败返回 ''。"""
    existing = get_vpn_mgmt_password()
    if existing:
        return existing
    import secrets
    candidate = secrets.token_urlsafe(24)
    if not set_vpn_mgmt_password(candidate):
        return ""
    # 回读校验（写失败/沙盒环境静默降级为 ''——调用方走无密码形态）
    return get_vpn_mgmt_password() or candidate
