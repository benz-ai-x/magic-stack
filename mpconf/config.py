"""Shared config I/O for Magic Stack.

Reads, writes, migrates, and validates ~/.magic-proxy.json.
Imported by app.py and config_server.py — no circular dependency.

Schema v2（服务器中心模型，ADR-011）：``servers[]`` 取代
``tunnels[]``——服务器（SSH 连接参数）与其上的服务（ssh 隧道 / nfs）
与实例（转发行 / 挂载行）分层；代理角色由 ``proxy_server_id`` 单一
持有（v1 的 current_tunnel/current_tunnel_id 双表示退役）。v1→v2
自动迁移保稳定 id——Keychain 槽位（``tunnel:{id}``）随之保值。

本模块是 servers 形状知识的**单一归宿**：归一化（merge 后每台服务器
ssh/services 全键在场，消费方免防御式取链）与访问器（servers /
server_by_id / proxy_server / server_forwards / server_nfs）都在此。
"""
import json
import logging
import os

from shared import keychain
from shared import netloc
from shared.defaults import DEFAULT_CAPTURE_DIR, DEFAULT_CAPTURE_PORT
from shared.identity import IdentityMigrationError, stable_id
from shared.config_store import DEFAULT_PATHS, atomic_write, get_path

logger = logging.getLogger("magic-proxy.config")

# Compatibility alias for the default location — the live value is
# config_store.PATHS["mp"], read at call time via get_path().
CONFIG_PATH = DEFAULT_PATHS["mp"]

SCHEMA_VERSION = 2

# v1（ tunnels[] 时代）的隧道默认形状——仅供迁移器消费，运行时不再出现
_DEFAULT_TUNNEL_V1 = {
    "name": "", "ssh_user": "", "ssh_host": "", "ssh_port": 22,
    "auth_type": "key", "ssh_key": "", "ssh_compression": True,
    "forwards": [], "forward_autostart": False,
    "nfs": {"enabled": False, "local_port": 12049,
            "squash_to_ssh_user": False, "mounts": []},
}

DEFAULT_CONFIG = {
    "schema_version": SCHEMA_VERSION,
    "socks5_port": 1080,
    "http_listen_port": 8888,
    "system_proxy_default": False,
    # 代理角色：proxy_server_id（稳定 id）唯一持有——哪台服务器的 SSH
    # 服务承担 -D SOCKS5 上游（:8888 的上游）
    "proxy_server_id": "",
    "servers": [],
    "capture_port": DEFAULT_CAPTURE_PORT,
    "capture_dir": DEFAULT_CAPTURE_DIR,
    "retention_days": 7,
    "prevent_sleep": False,
    "launch_at_login": False,
    "config_port": 9528,
    # 配置 API 常驻开关（ADR-009）：False=默认不监听 :9528——设置窗
    # 打开期间/复制 AI 助手指令手势按需起停；True=随应用常驻（浏览器
    # 直开与 AI agent 随时可连）。Docker 形态不走此键（恒常驻）。
    "config_api_enabled": False,
    # 界面语言（ADR-012）：auto=跟随系统（AppleLanguages）/zh-CN/en。
    # 值不校验（validate/JS 镜像零新增规则）——i18n.resolve 全兜底，
    # 坏值落缺省 zh-CN
    "language": "zh-CN",
}


def stable_server_id(user: str, host: str, port) -> str:
    """确定性 id：t-<sha1(user@host:port)[:10]>——同身份恒同 id（issue #8）。

    派生串与 v1 逐字节一致（id 是 Keychain 槽位与运行时会话的兼容契约）。
    """
    return stable_id("t", f"{user or ''}@{host or ''}:{port or 22}")


# 兼容别名（v1 名称）——存量测试引用；运行时新代码用 stable_server_id
stable_tunnel_id = stable_server_id


def _coerce_port(value, fallback: int) -> int:
    """端口读时兼容：字符串数字接受，非法/越界回落 fallback（merge 侧）。"""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return fallback
    return port if 1 <= port <= 65535 else fallback


def _normalize_forward(row) -> dict:
    """归一一条端口转发实例：剥未知键、端口读时兼容、remote_host 缺省回环。

    逐行构造全新 dict（容器绝不跨服务器共享默认值）。prepare 校验在
    merge 前做严格检查（非 int 即拒）；本函数是读路径的容错半边：手编
    字符串端口接受，非法值落 0（下次保存被 prepare 拦下，绝不静默丢行）。
    enabled（逐条启停，随 v0.11）：缺省 True——旧配置零迁移；False 的
    行不进会话 -L 集合、不占本地端口（冲突检查退出）。
    """
    remote_host = str(row.get("remote_host") or "").strip() or "127.0.0.1"
    return {
        "local_port": _coerce_port(row.get("local_port"), 0),
        "remote_host": remote_host,
        "remote_port": _coerce_port(row.get("remote_port"), 0),
        "enabled": row.get("enabled") is not False,
    }


def normalize_forwards(forwards) -> list:
    """forwards 字段的读路径归一入口：非列表→[]，非 dict 行剔除。"""
    if not isinstance(forwards, list):
        return []
    return [_normalize_forward(f) for f in forwards if isinstance(f, dict)]


NFS_LOCAL_PORT_FALLBACK = 12049


def _normalize_mount_row(row) -> dict:
    """归一一条 NFS 挂载实例：剥未知键，字符串原样 strip（空值合法——
    local_dir 空表示用默认 /Volumes/<name>，见 resolve_mount_dir）。"""
    return {
        "name": str(row.get("name") or "").strip(),
        "remote_path": str(row.get("remote_path") or "").strip(),
        "local_dir": str(row.get("local_dir") or "").strip(),
        "auto_mount": row.get("auto_mount") is True,
    }


def normalize_nfs(nfs) -> dict:
    """nfs 服务的读路径归一：非 dict→默认；端口读时兼容；mounts 逐行全新
    构造（浅拷贝防护：容器绝不跨服务器共享）。"""
    if not isinstance(nfs, dict):
        nfs = {}
    mounts = nfs.get("mounts")
    if not isinstance(mounts, list):
        mounts = []
    return {
        "enabled": nfs.get("enabled") is True,
        "local_port": _coerce_port(nfs.get("local_port"),
                                   NFS_LOCAL_PORT_FALLBACK),
        "squash_to_ssh_user": nfs.get("squash_to_ssh_user") is True,
        "mounts": [_normalize_mount_row(m) for m in mounts
                   if isinstance(m, dict)],
    }


def resolve_mount_dir(mount_row) -> str:
    """挂载点目录的单一解析归宿：显式 local_dir 优先，空则
    /Volumes/<name>（name 内的路径分隔符替换为 -，防嵌套）。"""
    explicit = str((mount_row or {}).get("local_dir") or "").strip()
    if explicit:
        return explicit
    name = str((mount_row or {}).get("name") or "").strip()
    safe = name.replace("/", "-").replace("\\", "-").strip() or "nfs"
    return f"/Volumes/{safe}"


def normalize_openvpn(openvpn) -> dict:
    """openvpn 服务的读路径归一（与 nfs 同形：单实例服务——enabled + 参数，
    非 1:n 实例）。profile 正文不进 config（内嵌用户私钥属机密，独立文件
    0600），此处只持掩码布尔 profile_set（ADR-002 契约）与提炼元数据。"""
    if not isinstance(openvpn, dict):
        openvpn = {}
    auth = openvpn.get("auth")
    return {
        "enabled": openvpn.get("enabled") is True,
        "profile_set": openvpn.get("profile_set") is True,
        "auth": auth if auth in ("none", "userpass") else "none",
        "username": str(openvpn.get("username") or "").strip(),
        "password_set": openvpn.get("password_set") is True,
        # 缺省 True：不显式关闭即应用服务器 push 的 DNS（关闭经 conf 注入
        # pull-filter，见 vpn/privilege.runtime_conf）
        "pull_dns": openvpn.get("pull_dns") is not False,
        "autostart": openvpn.get("autostart") is True,
    }


# ── servers 形状访问器（路径知识单一归宿）──────────────────────


def servers(cfg) -> list:
    """配置里的服务器列表（形状安全：非列表→[]）。"""
    rows = (cfg or {}).get("servers")
    return rows if isinstance(rows, list) else []


def server_by_id(cfg, sid) -> dict | None:
    """稳定 id → 服务器（None = 不存在）。"""
    for s in servers(cfg):
        if isinstance(s, dict) and s.get("id") == sid:
            return s
    return None


def proxy_server_id(cfg) -> str:
    """当前代理服务器的稳定 id（''=未配置）。"""
    sid = (cfg or {}).get("proxy_server_id")
    return sid if isinstance(sid, str) else ""


def proxy_server(cfg) -> dict | None:
    """代理角色服务器：id 有效→id 对应服务器→首条（与 merge 同一解析序，
    单一归宿——消费方不再各写一份判定）。"""
    rows = servers(cfg)
    sid = proxy_server_id(cfg)
    if sid:
        for s in rows:
            if isinstance(s, dict) and s.get("id") == sid:
                return s
    return rows[0] if rows else None


def server_forwards(server) -> list:
    """服务器的 SSH 隧道服务转发实例（merge 后形状恒定，免防御链）。"""
    svc = (server or {}).get("services") or {}
    return ((svc.get("ssh") or {}).get("forwards")) or []


def server_nfs(server) -> dict:
    """服务器的 NFS 服务节点（merge 后形状恒定）。"""
    svc = (server or {}).get("services") or {}
    return (svc.get("nfs") or {})


def server_openvpn(server) -> dict:
    """服务器的 OpenVPN 服务节点（merge 后形状恒定）。"""
    svc = (server or {}).get("services") or {}
    return (svc.get("openvpn") or {})


def assign_stable_ids(server_rows) -> int:
    """为无 id 的服务器赋确定性 id；重复身份/重复 id 抛可行动错误。

    返回迁移数量。已有 id 一律不动（重命名/改地址不影响）。
    """
    seen_ids, seen_identity = {}, {}
    migrated = 0
    for s in server_rows or []:
        ssh = s.get("ssh") if isinstance(s.get("ssh"), dict) else {}
        ident = f"{ssh.get('user', '')}@{ssh.get('host', '')}:{ssh.get('port', 22)}"
        if s.get("id"):
            if s["id"] in seen_ids:
                raise IdentityMigrationError(
                    f"服务器配置存在重复 id：{s['id']}（请修正配置文件后重试）")
            seen_ids[s["id"]] = ident
            seen_identity[ident] = True
            continue
        ordinal = 2 if ident in seen_identity else 1
        if ordinal > 1:
            # legacy 同身份双服务器（如 key+password 并存）本合法——确定性
            # 序数后缀区分 id；两服务器仍共享同一 legacy 凭证槽（与迁移前
            # 行为一致），不猜归属。显式手写重复 id 才致命。
            logger.warning("服务器重复身份 %s：以序数后缀区分 id", ident)
        seen_identity[ident] = True
        suffix = f"#{ordinal}" if ordinal > 1 else ""
        s["id"] = stable_server_id(
            ssh.get("user", ""), ssh.get("host", ""),
            ssh.get("port", 22)) + suffix
        if s["id"] in seen_ids:
            raise IdentityMigrationError(
                f"服务器配置存在重复 id：{s['id']}（请修正配置文件后重试）")
        seen_ids[s["id"]] = ident
        migrated += 1
    return migrated


def load_config(path=None):
    """Load and migrate config; returns merged dict or None."""
    p = path or get_path("mp")
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            cfg = json.load(f)
        before = json.dumps(cfg, sort_keys=True)
        migrated = _migrate(cfg)
        # issue #8：迁移错误（重复身份/id）是可行动错误——绝不与损坏
        # 混同进 .bak 隔离；原样上抛让编排层给出可行动提示
        assign_stable_ids(migrated.get("servers") or [])
        if json.dumps(migrated, sort_keys=True) != before and not save_config(migrated, p):
            # The migrated dict is already clean in memory, but the file on
            # disk is still the pre-migration version — it may hold plaintext
            # ssh_password. Isolate it so cleartext never survives a retry.
            try:
                os.replace(p, p + ".bak")
                logger.error(
                    "Migrated config could not be written; pre-migration file "
                    "(possibly containing plaintext passwords) moved to %s.bak",
                    p)
            except OSError:
                logger.exception(
                    "Migrated config could not be written AND the old file "
                    "could not be isolated")
        return migrated
    except IdentityMigrationError as e:
        # 迁移可行动错误：不隔离、不改写——上抛（JSONDecodeError 等仍走
        # 既有损坏隔离路径）
        logger.error("配置迁移失败（需人工处理，原文件未动）：%s", e)
        raise
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
        backup = p + ".bak"
        try:
            os.replace(p, backup)
            logger.warning("Config corrupted, backed up to %s: %s", backup, e)
        except OSError:
            logger.exception("Config corrupted and backup failed")
        return None


def _migrate(cfg):
    """迁移编排：老扁平→tunnels（v1 内部）→ servers（v2）+ 密码扫 Keychain。"""
    if not isinstance(cfg, dict):
        raise ValueError("config root must be a JSON object")
    if "tunnels" not in cfg and "servers" not in cfg and "ssh_host" in cfg:
        # 最老格式：扁平单隧道 → tunnels[]（v1 内部中间态）
        tunnel = {}
        for k in _DEFAULT_TUNNEL_V1:
            tunnel[k] = cfg.pop(k, _DEFAULT_TUNNEL_V1[k])
        if cfg.get("ssh_password"):
            tunnel["auth_type"] = "password"
            tunnel["ssh_password"] = cfg.pop("ssh_password")
        tunnel["name"] = tunnel.get("ssh_host", "Default")
        cfg["tunnels"] = [tunnel]
        for k in ("ssh_user", "ssh_key", "ssh_compression", "ssh_password"):
            cfg.pop(k, None)
        cfg.setdefault("current_tunnel", 0)

    if cfg.get("schema_version") != SCHEMA_VERSION and "tunnels" in cfg:
        _migrate_v1_to_v2(cfg)
    elif "servers" not in cfg:
        cfg["servers"] = []
    cfg["schema_version"] = SCHEMA_VERSION
    return cfg


def _migrate_v1_to_v2(cfg):
    """tunnels[]（v1）→ servers[]（v2）：换轴单一归宿，保稳定 id。

    - 连接参数（ssh_user/host/port/auth/key/compression）→ ``ssh`` 节
    - forwards + forward_autostart → ``services.ssh``
    - nfs 节 → ``services.nfs``
    - current_tunnel_id/current_tunnel 双表示 → ``proxy_server_id`` 单一
      （解析序与 v1 resolve 相同：id→下标→首条；id 已由 assign 保证）
    - 明文 ssh_password 在转换中直接扫入 Keychain（server 形状槽位经
      稳定 id 与 v1 一致——``tunnel:{id}`` 保值）；扫不进也绝不留在
      配置里
    """
    tunnels = cfg.get("tunnels")
    if not isinstance(tunnels, list) or any(
            not isinstance(t, dict) for t in tunnels):
        raise ValueError("config tunnels must be an array of objects")
    servers = []
    for t in tunnels:
        ssh = {
            "user": t.get("ssh_user", ""),
            "host": t.get("ssh_host", ""),
            "port": t.get("ssh_port", 22),
            "auth_type": t.get("auth_type", "key"),
            "ssh_key": t.get("ssh_key", ""),
            "compression": t.get("ssh_compression", True),
        }
        srv = {
            "id": t.get("id") or "",
            "name": t.get("name", ""),
            "ssh": ssh,
            "services": {
                "ssh": {"forwards": t.get("forwards") or [],
                        "autostart": t.get("forward_autostart") is True},
                "nfs": t.get("nfs") if isinstance(t.get("nfs"), dict) else {},
            },
        }
        if t.get("ssh_password"):
            plaintext = t.pop("ssh_password")
            if keychain.set_password(srv, plaintext):
                logger.info("明文 SSH 密码已迁入 Keychain（槽位经稳定 id 保持）")
            else:
                logger.error(
                    "SSH 密码无法写入 Keychain，已从配置移除；请在 服务器 "
                    "页重新输入（%s）", srv.get("name") or srv["ssh"]["host"])
        servers.append(srv)
    # 代理角色塌缩：id → 旧下标 → 首条
    rid = cfg.get("current_tunnel_id") or ""
    if not any(s.get("id") == rid for s in servers):
        try:
            idx = int(cfg.get("current_tunnel") or 0)
        except (TypeError, ValueError):
            idx = 0
        rid = (servers[idx].get("id") or "") if 0 <= idx < len(servers) \
            else ((servers[0].get("id") or "") if servers else "")
    cfg["servers"] = servers
    cfg["proxy_server_id"] = rid
    for k in ("tunnels", "current_tunnel", "current_tunnel_id"):
        cfg.pop(k, None)


def save_config(config, path=None):
    """Atomic write with 0600 perms — survives mid-write crashes."""
    return atomic_write(path or get_path("mp"), json.dumps(config, indent=2))


# mp 文件的「注册的额外字段」——merge 白名单外但属于合法持久化 schema
# 的键（#66 S2：白名单只拷 DEFAULT_CONFIG 曾把 local_client_token 抹掉，
# Claude Code 侧 ANTHROPIC_AUTH_TOKEN 与 Docker 卷契约随之静默失效）。
EXTRA_CONFIG_FIELDS: set = set()


def forward_row(cfg, server_id, index):
    """按稳定 id + 行下标取转发实例（磁盘真相读侧）——翻转意图的目标行
    推导单一归宿（None = 服务器/行不存在或形状不符）。"""
    rows = forward_rows(cfg, server_id)
    if 0 <= index < len(rows) and isinstance(rows[index], dict):
        return rows[index]
    return None


def forward_rows(cfg, server_id):
    """按稳定 id 取该服务器全部转发实例（形状安全；无服务器/无行 = []）——
    翻转后的「还有启用行吗」等读侧推导共用。"""
    return server_forwards(server_by_id(cfg, server_id) or {})


def toggle_forward_row(cfg, server_id, index, enabled):
    """update_mp 的 mutate 构造（架构评审 C1）：翻转指定转发实例的
    enabled，浅拷贝构造不动原 cfg；行不存在时原样返回（读侧已校验）。"""
    out = []
    for s in servers(cfg):
        if isinstance(s, dict) and s.get("id") == server_id:
            svc = dict(s.get("services") or {})
            ssh_svc = dict(svc.get("ssh") or {})
            fws = [dict(f) for f in (ssh_svc.get("forwards") or [])]
            if 0 <= index < len(fws) and isinstance(fws[index], dict):
                fws[index]["enabled"] = bool(enabled)
            ssh_svc["forwards"] = fws
            svc["ssh"] = ssh_svc
            out.append({**s, "services": svc})
        else:
            out.append(s)
    return {**cfg, "servers": out}


def _normalize_server(raw) -> dict:
    """单台服务器读路径归一：ssh/services 全键在场（消费方免防御链），
    容器逐层全新构造（默认值绝不跨服务器共享）。"""
    if not isinstance(raw, dict):
        raw = {}
    ssh = raw.get("ssh") if isinstance(raw.get("ssh"), dict) else {}
    services = raw.get("services") if isinstance(raw.get("services"), dict) else {}
    ssh_svc = services.get("ssh") if isinstance(services.get("ssh"), dict) else {}
    auth = ssh.get("auth_type")
    return {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or "").strip(),
        "ssh": {
            "user": str(ssh.get("user") or "").strip(),
            "host": str(ssh.get("host") or "").strip(),
            "port": _coerce_port(ssh.get("port"), 22),
            "auth_type": auth if auth in ("key", "password") else "key",
            "ssh_key": str(ssh.get("ssh_key") or "").strip(),
            # 缺省 True（v1 DEFAULT_TUNNEL 同语义）：消费方（ssh_launch /
            # 设置窗 JS）一律按「!==False」解读，merge 落 False 会让
            # 未显式关压缩的服务器静默丢 -C
            "compression": ssh.get("compression") is not False,
        },
        "services": {
            "ssh": {
                "forwards": normalize_forwards(ssh_svc.get("forwards")),
                "autostart": ssh_svc.get("autostart") is True,
            },
            "nfs": normalize_nfs(services.get("nfs")),
            "openvpn": normalize_openvpn(services.get("openvpn")),
        },
    }


def merge_config(cfg):
    """Merge raw config with defaults, coerce types, validate ranges."""
    if not isinstance(cfg, dict):
        cfg = None
    merged = DEFAULT_CONFIG.copy()
    if cfg:
        # Backward compat: old configs stored "http_listen" as a "host:port"
        # string. Convert to the new ``http_listen_port`` int field on read.
        if "http_listen" in cfg and "http_listen_port" not in cfg:
            try:
                _host, port = netloc.parse_listen(str(cfg["http_listen"]))
                cfg = {**cfg, "http_listen_port": port}
            except ValueError:
                pass  # fall through; the range check below resets to default
        for k in DEFAULT_CONFIG:
            if k in cfg:
                merged[k] = cfg[k]
        # 注册的额外字段随白名单外保留（schema 单主化）。懒注册：
        # local_token 的 import 顺序不可控，merge 内确定性注册一次（幂等）
        if not EXTRA_CONFIG_FIELDS:
            from mpconf.local_token import FIELD as _lt_field
            EXTRA_CONFIG_FIELDS.add(_lt_field)
        for k in EXTRA_CONFIG_FIELDS:
            if k in cfg:
                merged[k] = cfg[k]
        merged["servers"] = [_normalize_server(s) for s in servers(cfg)]
    for key, default in (("socks5_port", 1080), ("capture_port", DEFAULT_CAPTURE_PORT),
                         ("config_port", 9528), ("http_listen_port", 8888)):
        try:
            merged[key] = int(merged[key])
        except (TypeError, ValueError):
            merged[key] = default
        if not 1 <= merged[key] <= 65535:
            merged[key] = default
    try:
        merged["retention_days"] = max(0, int(merged["retention_days"]))
    except (TypeError, ValueError):
        merged["retention_days"] = 7
    # 代理角色（单一语义，读/存路径共用）：有效 id → 首条。proxy_server()
    # 与全部消费方共享同一判定
    role = proxy_server(merged)
    merged["proxy_server_id"] = (role.get("id") or "") if role else ""
    for _key in ("prevent_sleep", "launch_at_login"):
        if not isinstance(merged.get(_key), bool):
            merged[_key] = False
    capture_dir = merged.get("capture_dir")
    if not isinstance(capture_dir, str) or not capture_dir.strip():
        capture_dir = DEFAULT_CAPTURE_DIR
    merged["capture_dir"] = os.path.abspath(os.path.expanduser(capture_dir))
    return merged


# /api/state 运行态装饰字段的单一归宿（架构评审 C2）：装饰只写这四个
# 键；config_state.READONLY_DECORATED_FIELDS 的运行态半边由此派生——
# 「新增一条运行态事实 = 此处加一个键」替代两份手维护名单
RUNTIME_DECORATED_FIELDS = frozenset(
    {"capture_active", "is_proxy", "forward_running", "nfs_states"})


def decorate_runtime_state(mp, proj):
    """给 merge 后的 mp 注入运行态装饰（纯函数，/api/state 唯一装饰点）。

    读 RuntimeProjection（叶子层容器——按字段形状消费，不 import 生产
    者域）：capture_active 布尔 + forwards/mounts 命名投影（ForwardState
    /MountState）。is_proxy 经 proxy_server 与 merge 同一解析序。
    proj=None（缺席/异常）= 空投影：capture_active=False、装饰全空。
    只写 RUNTIME_DECORATED_FIELDS 声明的键——prepare 剥除同一集合，
    装饰永不落盘。
    """
    if not isinstance(mp, dict):
        return mp
    forward_status = {}
    for s in tuple(getattr(proj, "forwards", ()) or ()):
        tid = getattr(s, "tunnel_id", None)
        if tid is not None:
            forward_status[tid] = getattr(s, "status", "")
    mount_states = {}
    for entry in tuple(getattr(proj, "mounts", ()) or ()):
        mount_states.setdefault(getattr(entry, "tunnel_id", None), {})[
            getattr(entry, "name", "")] = {
                "status": getattr(entry, "status", ""),
                "error": getattr(entry, "error", ""),
                "fixable": getattr(entry, "fixable", ""),
            }
    mp["capture_active"] = bool(getattr(proj, "capture_active", False))
    role = proxy_server(mp)
    for s in servers(mp):
        if not isinstance(s, dict):
            continue
        s["is_proxy"] = s is role
        s["forward_running"] = s.get("id") in forward_status
        s["nfs_states"] = mount_states.get(s.get("id")) or {}
    return mp
