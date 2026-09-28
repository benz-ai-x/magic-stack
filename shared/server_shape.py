"""servers[] 形状与口径的叶子层单一归宿（纯函数，零 I/O）。

Server → Service → Instance（ADR-011 v2 schema）的形状访问器 + 代理角色
两种悬空语义 + enabled 口径。曾住 mpconf/config.py——tunnel/mount 与
mpconf 同层（分层 DAG 禁横向 import），够不着正主只能手抄（13 个解析点
的病根，#115 NFS v2 事故即手抄漂移实弹）；下沉叶子层后全域同消费，
mpconf 转发导出保持既有 import 不破。

代理角色悬空 id 的两种语义在此显式命名（分歧是测试钉住的故意设计）：
- proxy_server（回退版，写侧/运行侧）：悬空 → 首条——与 merge 的写侧
  重写同一解析序；
- is_proxy_server（严格版，菜单标记）：悬空 → 谁都不标——读侧不猜
  归属，正常路径只见 merged 配置。

防御深度统一取「对未经 merge 的裸配置（手编/测试）也稳」的超集——
merge 后形状恒定时与历史行为逐点一致。
"""


def servers(cfg) -> list:
    """配置里的服务器列表（形状安全：非列表→[]）。"""
    rows = (cfg or {}).get("servers")
    return rows if isinstance(rows, list) else []


def server_by_id(cfg, sid) -> dict | None:
    """稳定 id → 服务器（None = 不存在；首中语义）。"""
    for s in servers(cfg):
        if isinstance(s, dict) and s.get("id") == sid:
            return s
    return None


def servers_by_id(cfg) -> dict:
    """稳定 id → 服务器映射（与 server_by_id 同为首中语义；无 id 行跳过）。"""
    out = {}
    for s in servers(cfg):
        sid = s.get("id") if isinstance(s, dict) else None
        if sid and sid not in out:
            out[sid] = s
    return out


def ssh_node(server) -> dict:
    """连接参数节（server["ssh"]；非 dict → {}）。"""
    node = (server or {}).get("ssh")
    return node if isinstance(node, dict) else {}


def _service_node(server, key):
    """服务节点原值（services.<key>；非 dict → None——「未配置」语义）。"""
    svc = (server or {}).get("services")
    if not isinstance(svc, dict):
        return None
    node = svc.get(key)
    return node if isinstance(node, dict) else None


def ssh_service(server) -> dict:
    """SSH 隧道服务节点（services.ssh；缺省 {}）。"""
    return _service_node(server, "ssh") or {}


def nfs_node(server) -> dict | None:
    """NFS 服务节点原值（services.nfs；None = 未配置——校验侧
    「未配置不校验」语义）。"""
    return _service_node(server, "nfs")


def server_nfs(server) -> dict:
    """NFS 服务节点（services.nfs；缺省 {}）。"""
    return _service_node(server, "nfs") or {}


def openvpn_node(server) -> dict | None:
    """OpenVPN 服务节点原值（services.openvpn；None = 未配置）。"""
    return _service_node(server, "openvpn")


def server_openvpn(server) -> dict:
    """OpenVPN 服务节点（services.openvpn；缺省 {}）。"""
    return _service_node(server, "openvpn") or {}


def server_forwards(server) -> list:
    """服务器的 SSH 隧道服务转发实例（services.ssh.forwards；非列表→[]）。"""
    fw = ssh_service(server).get("forwards")
    return fw if isinstance(fw, list) else []


def forward_enabled(row) -> bool:
    """单条转发实例的启用判定（enabled≠False）——与 enabled_forwards
    同口径的行级谓词（菜单行渲染 / 意图翻转判定共用）。"""
    return isinstance(row, dict) and row.get("enabled") is not False


def enabled_forwards(server) -> list:
    """启用中的转发实例——「不进 -L 集合 / 不占端口 / 会话可存在性」
    口径的单一归宿（原三处注释互称『同口径』的知识上收于此）。"""
    return [f for f in server_forwards(server) if forward_enabled(f)]


def first_forward_port(server):
    """第一条合法且启用 -L 的本地端口（就绪探测口推导的单一归宿）。

    非法/缺字段行防御性跳过——prepare 校验与 merge 归一双保险下，正常
    流转的配置永不触达跳过分支。
    """
    for f in enabled_forwards(server):
        lp = f.get("local_port")
        if isinstance(lp, int) and not isinstance(lp, bool) \
                and 1 <= lp <= 65535:
            return lp
    return None


def proxy_server_id(cfg) -> str:
    """当前代理服务器的稳定 id（''=未配置）。"""
    sid = (cfg or {}).get("proxy_server_id")
    return sid if isinstance(sid, str) else ""


def proxy_server(cfg) -> dict | None:
    """代理角色服务器（回退版，写侧/运行侧）：id 有效→id 对应服务器→
    首条——与 merge 同一解析序，单一归宿，消费方不再各写一份判定。"""
    rows = [s for s in servers(cfg) if isinstance(s, dict)]
    sid = proxy_server_id(cfg)
    if sid:
        for s in rows:
            if s.get("id") == sid:
                return s
    return rows[0] if rows else None


def is_proxy_server(cfg, server) -> bool:
    """代理角色判定（严格版，菜单标记用）：悬空 id 不猜归属（谁都不
    标）——merge 会在写侧重写为有效值，菜单正常路径只见 merged 配置
    （TestIsProxyServer 钉住）。缺省（无 id）→ 首条（对象同一性）。"""
    if not isinstance(cfg, dict) or not isinstance(server, dict):
        return False
    sid = proxy_server_id(cfg)
    if sid:
        return server.get("id") == sid
    rows = servers(cfg)
    return bool(rows) and server is rows[0]


def with_service_patch(server, key, patch):
    """服务节点字段的 copy-on-write 写侧原语（R7-C4）：逐层全新构造
    （不突变入参），缺层补层——「改 servers 子树一个字段」的写侧惯
    用法单一归宿（toggle_forward_row / vpn profile_set / NFS 会话派生
    共用，防每个新持久化点重抄九行逐层重建）。"""
    svc = dict((server or {}).get("services") or {})
    node = svc.get(key) if isinstance(svc.get(key), dict) else {}
    svc[key] = {**node, **patch}
    return {**server, "services": svc}
