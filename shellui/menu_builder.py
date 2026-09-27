"""Menu bar UI builder for Magic Stack.

Constructs rumps menu trees from a frozen state snapshot + a callback
namespace.  Owns menu refs, status icon cache, and struct-key tracking.
Extracted from MagicProxyApp to isolate ~330 lines of view-layer code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import logging

import rumps
from capture import chromium_proxy
from mpconf.config import proxy_server, server_forwards, servers
from shared import i18n
from shared.i18n import DEFAULT_LANGUAGE
from util import resource_path as _resource_path, truncate as _truncate

logger = logging.getLogger("magic-proxy.menu")

STATUS_ICON_RESOURCE = "MenubarIcon.png"
STATUS_ICON_GRAY_RESOURCE = "MenubarIcon-gray.png"
STATUS_ICON_YELLOW_RESOURCE = "MenubarIcon-yellow.png"
STATUS_STATE_STYLE = {
    "green":  ("systemBlueColor", "🔵"),
    "yellow": ("systemYellowColor", "🟡"),
    "gray":   ("systemGrayColor", "⚪"),
}
_ICON_RESOURCE_FOR_KEY = {
    "green":  STATUS_ICON_RESOURCE,
    "yellow": STATUS_ICON_YELLOW_RESOURCE,
    "gray":   STATUS_ICON_GRAY_RESOURCE,
}


def _status_color_for_connection(status, paused=False):
    if paused:
        return "yellow"
    if status == "connected":
        return "green"
    if status == "connecting":
        return "yellow"
    return "gray"


def _human(n, suffix="B"):
    if n < 1024:
        return f"{int(n)} {suffix}"
    for unit in ("K", "M", "G"):
        n /= 1024
        if n < 1024:
            decimals = 2 if unit == "G" else 1
            return f"{n:.{decimals}f} {unit}{suffix}"
    return f"{n:.2f} T{suffix}"


# ── SF Symbols 图标体系（macOS 11+；旧系统/未知符号静默降级纯文本）──
# 符号全部取 SF 1/2（macOS 11 基线）；_apply_icon 任何失败都不抛。
_ICON = {
    # 分区父项
    "proxy_menu": "bolt.fill",       # 代 理（-D 会话）
    "forward_menu": "arrowshape.turn.up.right",  # 端口映射（-L 转发）
    "mount_menu": "externaldrive",   # 远程挂载（NFS over SSH，ADR-007）
    "router": "cpu", "capture": "eye", "system": "gearshape",
    # 代理区
    "connect": "play.fill", "cancel": "stop.fill", "pause": "pause.fill",
    "refresh": "arrow.clockwise",
    "tunnel_row": "server.rack", "launch": "arrow.up.right.square",
    # 端口映射区 / 挂载区
    "fw_start": "play.circle", "fw_stop": "stop.circle",
    "mount_row": "externaldrive",
    # AI 路由 / 抓包
    "cycle": "arrow.triangle.2.circlepath",
    "doc": "doc.on.doc", "clipboard": "doc.on.clipboard",
    "folder": "folder", "jsonl": "doc.text",
    # 系统区 / 页脚 / 状态区
    "sleep": "moon.zzz", "login": "arrow.up.circle",
    "network": "network",   # 配置 API 服务开关（ADR-009）
    "globe": "globe",       # 语言子菜单（ADR-012）
    "prefs": "slider.horizontal.3", "search": "doc.text.magnifyingglass",
    "about": "info.circle", "quit": "power",
    "updown": "arrow.up.arrow.down", "circle": "circle.fill",
}


def _symbol_image(name, point_size=None, color=None, description=None):
    """SF Symbol → NSImage；不可用（旧系统/符号缺失/异常）返回 None。

    description 进 VoiceOver（a11y）——圆点图标的颜色语义只有视觉通道，
    必须补文字描述（「已连接」等），否则屏幕阅读器拿不到状态。"""
    try:
        from AppKit import NSImage, NSImageSymbolConfiguration
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            name, description)
        if img is None:
            return None
        cfgs = []
        if point_size is not None:
            from AppKit import NSFontWeightRegular
            cfgs.append(NSImageSymbolConfiguration
                        .configurationWithPointSize_weight_(
                            point_size, NSFontWeightRegular))
        if color is not None:
            cfgs.append(NSImageSymbolConfiguration
                        .configurationWithTintColor_(color))
        for i in range(1, len(cfgs)):
            cfgs[i] = cfgs[i - 1].configByApplyingConfiguration_(cfgs[i])
        if cfgs:
            img = img.imageWithSymbolConfiguration_(cfgs[-1])
        if color is not None:
            # SF Symbol 默认 template=True——NSMenuItem 对 template 图像
            # 按菜单文字色单色渲染，tint 配置被无视（菜单里所有圆点一直
            # 显示黑色的根因，真机截图实锄）。带 tint 显式关掉 template
            # 让颜色生效（动态系统色自带明暗适配）；无 tint 的动作图标
            # 保持 template，随菜单文字色自动适配明暗。
            img.setTemplate_(False)
        return img
    except Exception:
        return None


def _apply_icon(item, key, point_size=None, color=None, description=None):
    """给 rumps.MenuItem 挂 SF Symbol 图标（菜单重建随建随挂）。

    description 缺省取 item.title（动态占位标题 __xxx__ 除外）——分区/
    动作图标的语义即标题，无需逐处手传。着色状态点请用
    _apply_status_dot（SF Symbol 的 tint 在 NSMenuItem 上两轮真机实测
    不生效）。"""
    if item is None:
        return
    if description is None:
        title = getattr(item, "title", "")
        if title and not title.startswith("__"):
            description = title
    img = _symbol_image(_ICON.get(key, key), point_size=point_size,
                        color=color, description=description)
    if img is not None:
        try:
            item._menuitem.setImage_(img)
        except Exception:
            pass  # 图标是增强，绝不阻断菜单构建


def _status_dot_image(kind, point_size):
    """手绘状态圆点（位图，不走 SF Symbol 渲染通道）。

    SF Symbol 图像在 NSMenuItem 上的 tint 两轮真机实测不生效
    （template 语义顽固，setTemplate_(False) 亦无效）——直接在画布上
    填色最可靠。idle 档画黑点并保持 template：随菜单文字色自动适配
    明暗（浅色黑/深色白）；彩色档（绿/黄/红）固定色 + 非模板——两种
    外观下都可读。状态语义经行标题文字到达 VoiceOver（符号图像的
    accessibilityDescription 通道随符号一并退役）。"""
    try:
        from AppKit import NSBezierPath, NSColor, NSImage, NSMakeRect
        size = float(point_size)
        img = NSImage.alloc().initWithSize_((size, size))
        img.lockFocus()
        fill = (NSColor.blackColor() if kind == "idle"
                else _status_color(kind))
        if fill is not None:
            fill.setFill()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(0, 0, size, size)).fill()
        img.unlockFocus()
        img.setTemplate_(kind == "idle")
        return img
    except Exception:
        logger.exception("status dot draw failed")
        return None


def _apply_status_dot(item, kind, point_size):
    """给行挂手绘状态圆点（着色唯一可靠通道；失败兜底回符号路径）。"""
    if item is None:
        return
    img = _status_dot_image(kind, point_size)
    if img is None:
        _apply_icon(item, "circle", point_size=point_size,
                    color=_status_color(kind))
        return
    try:
        item._menuitem.setImage_(img)
    except Exception:
        pass


def _apply_check(item, on):
    """B 类设置项的原生 ✓ 状态（NSMenuItem.state）——设置用母语，
    不染运行色；标题保持中性名词（「防睡眠」），✓ 即当前值。"""
    if item is None:
        return
    try:
        item._menuitem.setState_(1 if on else 0)
    except Exception:
        pass


def _error_counts(st):
    """转发/挂载的 error 计数——状态行 ⚠ 与组标题 rollup 的同源单一
    推导（两处手写会漂移）。"""
    fw_bad = sum(1 for f in (st.forward_states or ())
                 if f.status == "error")
    mounts_bad = sum(1 for entry in (st.mount_states or ())
                     if entry.status == "error")
    return fw_bad, mounts_bad


def _is_proxy_server(config, server) -> bool:
    """代理角色判定（v2）：id 有效→命中；悬空/缺省→首条。与 merge 的
    proxy_server 同一解析序——菜单只消费不重定义。

    悬空 id（手编未 merge 的裸配置）不猜归属：merge 会在写侧重写为
    有效值，菜单正常路径只见 merged 配置（TestIsProxyServer 钉住）。"""
    if not isinstance(config, dict) or not isinstance(server, dict):
        return False
    rows = servers(config)
    cid = config.get("proxy_server_id") or ""
    if cid:
        return server.get("id") == cid
    return bool(rows) and server is rows[0]


def _status_color(kind):
    """状态点着色（动态系统色，明暗模式自适应）。idle=未启动用
    labelColor（浅色模式黑/深色模式白）——用户拍板的二元语义：运行绿、
    未启动黑；黄只留给进行中（connecting/mounting），红只留给异常。"""
    try:
        from AppKit import NSColor
        return {"ok": NSColor.systemGreenColor(),
                "warn": NSColor.systemYellowColor(),
                "err": NSColor.systemRedColor(),
                "idle": NSColor.labelColor()}[kind]
    except Exception:
        return None


def _line_status_kind(status, paused=False):
    """状态行圆点的着色档：connected=ok / connecting·paused=warn /
    error=err / 其余=idle。"""
    if paused:
        return "warn"
    return {"connected": "ok", "connecting": "warn",
            "error": "err"}.get(status, "idle")


# ── interface types ──────────────────────────────────────────────

@dataclass(frozen=True)
class MenuState:
    """Frozen snapshot of all render data the MenuBuilder reads."""

    ssh_status: str
    ssh_cmd_str: str
    ssh_log: str
    ssh_error_msg: str
    paused: bool
    stats_snapshot: dict
    config: dict
    sys_proxy_on: bool
    sys_proxy_error: str
    suanpan_running: bool
    suanpan_error: str
    suanpan_listen_address: str
    current_server: dict | None
    # 抓包结构化状态（状态语法批次）：enabled 驱动标题动词方向，
    # state 四值（ok/warn/idle/err）驱动状态点，hint 承载引导/异常详情
    capture_enabled: bool = False
    capture_state: str = "idle"
    capture_hint: str | None = None
    # 多活（v0.9）：转发会话快照 [(tunnel_id, name, status)]——隧道子菜单
    # 与状态行的转发计数消费；默认 () 保持既有测试构造兼容
    forward_states: tuple = ()
    # NFS 挂载快照 [(tunnel_id, tunnel_name, mount_name, status, error)]
    # （ADR-007）——挂载子菜单与状态行的挂载计数消费；默认 () 同上
    mount_states: tuple = ()
    # 界面语言（ADR-012，resolved 值非偏好值）：进 struct_key——语言
    # 翻转走既有整树重建机制，文案在 build/refresh 时经 i18n.t 取词
    language: str = DEFAULT_LANGUAGE
    # VPN（M2 接线）：全局单连接的状态面（spec §5.3——非 per-server）
    vpn_status: str = "idle"
    vpn_server: str = ""
    vpn_error: str = ""


# ── builder ──────────────────────────────────────────────────────

# ── A 类运行物状态词表（状态语法矩阵单一真相）──────────────────
# (状态点色档, 文案键)：进行中=warn / 运行=ok / 未启动=idle / 异常=err。
# 标题=动作（动词），状态点=现状（颜色），行尾状态词=文字冗余通道
# （glance + VoiceOver）——三者各司其职，全菜单同一语法。
# 文案经 i18n 取词（ADR-012）；表存键名而非拼前缀——取词守卫禁止动态键。

# 转发会话（隧道包装行；无会话 = 未启动）
_FW_SESSION = {"connected": ("ok", "forward.session.connected"),
               "connecting": ("warn", "forward.session.connecting"),
               "error": ("err", "forward.session.error")}
_FW_SESSION_RETRY = ("warn", "forward.session.retry")
_FW_SESSION_OFF = ("idle", "forward.session.off")

# 挂载行（单一「·」分隔；异常详情折进行标题保结构恒定）
_MOUNT_TAIL = {"mounted": ("ok", "mount.tail.mounted"),
               "mounting": ("warn", "mount.tail.mounting"),
               "unmounting": ("warn", "mount.tail.unmounting"),
               "unmounted": ("idle", "mount.tail.unmounted"),
               "error": ("err", "mount.tail.error")}

# VPN 行（A 类：动词标题 + 状态点 + 行尾状态词；status 在 struct_key
# 内，态变即重建换点换词）
_VPN_TAIL = {"connecting": ("warn", "vpn.tail.connecting"),
             "connected": ("ok", "vpn.tail.connected"),
             "reconnecting": ("warn", "vpn.tail.reconnecting"),
             "exiting": ("warn", "vpn.tail.exiting"),
             "error": ("err", "vpn.tail.error"),
             "stopped": ("idle", "vpn.tail.stopped")}


class MenuBuilder:
    """Builds and refreshes the menu bar UI from a state snapshot.

    The `app` is the MagicProxyApp itself — callback names are its
    method names (cancel_connection, reconnect, toggle_pause, …).
    """

    def __init__(self, app, get_state: Callable[[], MenuState]):
        self._app = app          # MagicProxyApp: menu + callbacks + _nsapp
        self._get_state = get_state
        self.refs = {}
        self.last_struct_key = None
        self._icon_cache = {}
        self._icon_ok = True

    # ── struct key ────────────────────────────────────────

    def struct_key(self):
        st = self._get_state()
        s = st.ssh_status
        tunnels = servers(st.config)
        # Note: active_connections is deliberately NOT here (#40) — it
        # fluctuates every tick while traffic flows, but only affects the
        # traffic *title* (refresh_titles), never the menu structure.
        #
        # 转发/挂载只进**身份**签名（哪些行存在——隧道/端口对/挂载名）：
        # 行内状态（连接态、挂载态、error 文本、enabled 翻转）由
        # _refresh_forward_rows/_refresh_mount_rows 就地刷新。后台状态
        # 翻转不再整树重建——此前任何一条会话 connecting→connected 都会
        # clear+rebuild 全部七组，用户正展开子菜单时整棵塌掉。
        fw_identity = tuple(
            (t.get("id") or f"#{i}",
             tuple((f.get("local_port"), f.get("remote_port"))
                   for f in server_forwards(t)
                   if isinstance(f, dict)))
            for i, t in enumerate(tunnels) if isinstance(t, dict))
        mount_identity = tuple((e.tunnel_id, e.name)
                               for e in (st.mount_states or ()))
        return (
            s, st.paused,
            st.config.get("proxy_server_id", ""),
            len(tunnels),
            s == "error" and bool(st.ssh_error_msg),
            st.ssh_log if s == "connecting" else "",
            st.sys_proxy_on,
            bool(st.sys_proxy_error),
            st.suanpan_running,
            st.suanpan_error[:50] if st.suanpan_error else "",
            fw_identity,   # 行集合变化（配置增删）→ 重建
            mount_identity,
            st.capture_enabled,      # 抓包动词方向（结构恒定，标签刷新）
            st.capture_state,        # 状态点（err↔ok 随重建换点）
            st.capture_hint,
            st.language,             # ADR-012：语言翻转 → 整树重建换文案
            st.vpn_status,           # VPN 态变 → 重建（换动词/点/状态词）
            st.vpn_server,
        )

    # ── full build ────────────────────────────────────────

    def build(self):
        app = self._app
        app.menu.clear()
        self.refs = {}

        self._build_header()
        app.menu.add(None)
        app.menu.add(self._build_proxy_submenu())
        app.menu.add(self._build_forward_submenu())
        app.menu.add(self._build_mount_submenu())
        app.menu.add(self._build_vpn_submenu())
        app.menu.add(self._build_suanpan_submenu())
        app.menu.add(self._build_capture_submenu())
        app.menu.add(self._build_system_submenu())
        app.menu.add(None)
        self._build_footer()
        self.refresh_titles()

    def _build_header(self):
        app = self._app
        st = self._get_state()
        s = st.ssh_status
        refs = self.refs

        # Proxy status line —— 着色圆点承载状态色（状态字段在 struct_key
        # 内，变化即重建换色；emoji 已退役）
        refs["proxy_status"] = rumps.MenuItem("__proxy_status__", callback=None)
        _apply_status_dot(refs["proxy_status"],
                          _line_status_kind(s, st.paused), point_size=10)
        app.menu.add(refs["proxy_status"])

        # Router status line
        refs["router_status"] = rumps.MenuItem("__router_status__", callback=None)
        _apply_status_dot(
            refs["router_status"],
            "ok" if st.suanpan_running
            else ("err" if st.suanpan_error else "idle"),
            point_size=10)
        app.menu.add(refs["router_status"])

        # Connecting log lines
        if s == "connecting" and st.ssh_cmd_str:
            app.menu.add(rumps.MenuItem(f"  {_truncate(st.ssh_cmd_str, 60)}", callback=None))
            if st.ssh_log:
                app.menu.add(None)
                for line in st.ssh_log.split("\n")[-3:]:
                    app.menu.add(rumps.MenuItem(f"  {_truncate(line, 60)}", callback=None))

        if s == "error" and st.ssh_error_msg:
            app.menu.add(rumps.MenuItem(f"  {_truncate(st.ssh_error_msg, 80)}", callback=None))

        # Traffic line (connected only)
        if s == "connected" and not st.paused:
            refs["traffic"] = rumps.MenuItem("__traffic__", callback=None)
            _apply_icon(refs["traffic"], "updown", point_size=10,
                        color=_status_color("idle"))
            app.menu.add(refs["traffic"])

    def _build_proxy_submenu(self):
        """代 理 ▸ —— 只管那条唯一的 -D 会话（SOCKS5 上游）：启停/暂停/
        重连、系统代理、代理角色单选、经代理启动。"""
        st = self._get_state()
        a = self._app
        parent = rumps.MenuItem(i18n.t("menu.group.proxy"), callback=None)
        _apply_icon(parent, "proxy_menu")

        # Connect control
        s = st.ssh_status
        if s == "connecting":
            item = rumps.MenuItem(i18n.t("proxy.cancel_connect"),
                                  callback=a.cancel_connection, key="r")
            _apply_icon(item, "cancel")
            parent.add(item)
        else:
            if st.paused:
                item = rumps.MenuItem(i18n.t("proxy.resume"),
                                      callback=a.toggle_pause, key="p")
                _apply_icon(item, "connect")
                parent.add(item)
            elif s == "connected":
                item = rumps.MenuItem(i18n.t("proxy.pause"),
                                      callback=a.toggle_pause, key="p")
                _apply_icon(item, "pause")
                parent.add(item)
            item = rumps.MenuItem(
                i18n.t("common.reconnect")
                if s in ("connected", "error") else i18n.t("proxy.connect"),
                callback=a.reconnect, key="r")
            _apply_icon(item, "refresh")
            parent.add(item)

        # System proxy toggle（A 类运行物状态语法：标题=动作，状态点=现状）
        parent.add(None)
        sysp_title = i18n.t("proxy.sysproxy_off" if st.sys_proxy_on
                            else "proxy.sysproxy_on")
        item = rumps.MenuItem(sysp_title, callback=a.toggle_system_proxy, key="g")
        _apply_status_dot(item, "err" if st.sys_proxy_error
                          else ("ok" if st.sys_proxy_on else "idle"),
                          point_size=9)
        parent.add(item)

        # 代理角色单选（哪条隧道当 SOCKS5 上游）
        rows = st.config.get("servers", [])
        if rows:
            parent.add(None)
            parent.add(rumps.MenuItem(i18n.t("proxy.upstream_header"),
                                      callback=None))
            for t in rows:
                if not isinstance(t, dict):
                    continue
                _ssh = t.get("ssh") or {}
                marker = "✓ " if _is_proxy_server(st.config, t) else ""
                name = t.get("name") or f"{_ssh.get('user', '')}@{_ssh.get('host', '')}"
                item = rumps.MenuItem(f"{marker}{name}",
                                      callback=a.make_switch_server(t.get("id") or ""))
                _apply_icon(item, "tunnel_row")
                parent.add(item)

        # Proxied app launches
        apps_list = chromium_proxy.installed_apps()
        if apps_list:
            parent.add(None)
            sub = rumps.MenuItem(i18n.t("proxy.launch_apps"), callback=None)
            _apply_icon(sub, "launch")
            for entry in apps_list:
                item = rumps.MenuItem(
                    entry["name"], callback=a.make_launch_proxied(entry))
                _apply_icon(item, "launch")
                sub.add(item)
            parent.add(sub)

        self.refs["group_proxy"] = parent
        return parent

    def _build_forward_submenu(self):
        """端口映射 ▸ —— 只管纯 -L 转发会话（多活）：代理隧道自身显示
        「随代理运行」信息行；其余隧道各自启停/单会话重连。

        逐条启停（v0.11）：每条转发独立成行（点击即启停），圆点随会话
        状态着色、停用行灰点。UX 批次：①单隧道拍平（包装行只在多隧道
        时有意义——转发行一级直达）；②行结构**恒定**（会话启停动作恒
        在，标签由刷新段定「启动/停止」），状态/文案就地刷新——后台
        状态翻转不重建整树；③代理隧道的行点击会重启整个代理会话（全
        代理流量中断），连接中在隧道行标题明示。
        """
        st = self._get_state()
        a = self._app
        parent = rumps.MenuItem(i18n.t("menu.group.forward"), callback=None)
        _apply_icon(parent, "forward_menu")

        tunnels = servers(st.config)
        single = len(tunnels) == 1
        any_rules = False
        for i, t in enumerate(tunnels):
            tid = t.get("id") or f"#{i}"
            forwards = server_forwards(t)
            any_rules = any_rules or bool(forwards)
            is_proxy = _is_proxy_server(st.config, t)
            if single:
                host = parent          # 拍平：行直接挂顶层（免一层嵌套）
            else:
                # 多隧道：包装行承载隧道名/尾标；代理隧道的包装行即
                # 「随代理运行」上下文行（转发行挂其下）。占位标题按
                # 隧道 id 唯一——rumps Menu 以标题为键，重名行互相覆盖
                host = rumps.MenuItem(f"__fw_tunnel_{tid}__", callback=None)
                if is_proxy:
                    _apply_icon(host, "circle", point_size=9)
                    self.refs[("fw_ctx", tid)] = host
                else:
                    _apply_icon(host, "tunnel_row")
                    self.refs[("fw_tunnel", tid)] = host
            if is_proxy and single:
                ctx = rumps.MenuItem(f"__fw_ctx_{tid}__", callback=None)
                _apply_icon(ctx, "circle", point_size=9)
                self.refs[("fw_ctx", tid)] = ctx
                host.add(ctx)
            self._add_forward_rows(host, a, tid, forwards)
            if not is_proxy:
                # 逐条转发行在前、会话动作在后（v0.12 既定行序）
                host.add(None)
                if forwards:
                    action = rumps.MenuItem(
                        f"__fw_action_{tid}__",
                        callback=a.toggle_forward_session(tid))
                    _apply_icon(action, "fw_start")
                    self.refs[("fw_action", tid)] = action
                    host.add(action)
                    item = rumps.MenuItem(
                        i18n.t("common.reconnect"),
                        callback=a.make_reconnect_tunnel(tid))
                    _apply_icon(item, "refresh")
                    host.add(item)
                else:
                    item = rumps.MenuItem(
                        i18n.t("forward.add_rule"), callback=a.show_prefs_forwards)
                    _apply_icon(item, "forward_menu")
                    host.add(item)
            if not single:
                parent.add(host)
                parent.add(None)

        if tunnels and not any_rules:
            parent.add(rumps.MenuItem(i18n.t("forward.add_rule"),
                                      callback=a.show_prefs_forwards))
        self._refresh_forward_rows(st)
        self.refs["group_forward"] = parent
        return parent

    def _add_forward_rows(self, parent, app, tunnel_id, forwards):
        """隧道行下挂逐条转发子行：点击即启停（唯一动作，免四级嵌套）。
        标题与圆点由 _refresh_forward_rows 就地刷新（结构恒定）。"""
        for fi, f in enumerate(forwards):
            if not isinstance(f, dict):
                continue
            row = rumps.MenuItem(
                f"__fw_row_{tunnel_id}_{fi}__",
                callback=app.make_toggle_forward(tunnel_id, fi))
            _apply_icon(row, "circle", point_size=8)
            self.refs[("fw_row", tunnel_id, fi)] = row
            parent.add(row)

    def _refresh_forward_rows(self, st):
        """端口映射区动态段：隧道行尾标、逐条转发行尾标与圆点、启停
        动作标签。每秒 tick 调用——只在标题变化时重挂图标（SF Symbol
        查找不便宜，不能每 tick 全量重设）。"""
        tunnels = servers(st.config)
        fw_running = {f.tunnel_id: f.status
                      for f in (st.forward_states or ())}
        for i, t in enumerate(tunnels):
            if not isinstance(t, dict):
                continue
            tid = t.get("id") or f"#{i}"
            name = t.get("name") or \
                f"{(t.get('ssh') or {}).get('user', '')}@{(t.get('ssh') or {}).get('host', '')}"
            is_proxy = _is_proxy_server(st.config, t)
            if is_proxy:
                row = self.refs.get(("fw_ctx", tid))
                if row is not None:
                    on = st.ssh_status == "connected"
                    # 点击代理隧道的转发行 = 重启代理会话（全流量中断），
                    # 副作用在行标题明示——先于点击可见
                    title = i18n.t(
                        "forward.ctx.running" if on
                        else "forward.ctx.not_running", name=name)
                    if row.title != title:
                        row.title = title
                        _apply_status_dot(
                            row, "ok" if on else "idle", point_size=9)
            else:
                row = self.refs.get(("fw_tunnel", tid))
                if row is not None:
                    status = fw_running.get(tid)
                    if status is None:
                        kind, key = _FW_SESSION_OFF
                    else:
                        kind, key = _FW_SESSION.get(
                            status, _FW_SESSION_RETRY)
                    new_title = f"{name}{i18n.t(key)}"
                    if row.title != new_title:
                        row.title = new_title
                        _apply_status_dot(row, kind, point_size=9)
                action = self.refs.get(("fw_action", tid))
                if action is not None:
                    running = tid in fw_running
                    new_title = i18n.t(
                        "forward.action.stop" if running
                        else "forward.action.start")
                    if action.title != new_title:  # 图标随标题变化才重挂
                        action.title = new_title
                        _apply_icon(action,
                                    "fw_stop" if running else "fw_start")
            session_up = (st.ssh_status == "connected" if is_proxy
                          else fw_running.get(tid) == "connected")
            for fi, f in enumerate(server_forwards(t)):
                if not isinstance(f, dict):
                    continue
                row = self.refs.get(("fw_row", tid, fi))
                if row is None:
                    continue
                lp, rp = f.get("local_port"), f.get("remote_port")
                enabled = f.get("enabled") is not False
                # 二元着色（用户拍板）：已映射=绿；未连接/已停用都是
                # 「没启动」=黑（idle/labelColor）
                if enabled and session_up:
                    title, kind = f"{lp} → {rp} · {i18n.t('forward.row.mapped')}", "ok"
                elif enabled:
                    title, kind = f"{lp} → {rp} · {i18n.t('forward.row.disconnected')}", "idle"
                else:
                    title, kind = f"{lp} → {rp} · {i18n.t('forward.row.disabled')}", "idle"
                if row.title != title:
                    row.title = title
                    _apply_status_dot(row, kind, point_size=8)

    def _build_mount_submenu(self):
        """远程挂载 ▸ —— NFS over SSH 挂载项（ADR-007）：跨隧道列出所有
        配置了 NFS 挂载的项，每项启停 + 打开挂载目录。NFS 走独立专用
        会话，无端口映射里「随代理运行」的特殊行。

        UX 批次：行结构恒定（挂载/卸载动作恒在，标签刷新定字），状态
        尾标与异常详情就地刷新——后台挂载态翻转不重建整树；空态行可
        点击深链偏好设置。"""
        st = self._get_state()
        a = self._app
        parent = rumps.MenuItem(i18n.t("menu.group.mount"), callback=None)
        _apply_icon(parent, "mount_menu")

        any_mounts = False
        for entry in (st.mount_states or ()):
            # MountState（NamedTuple 投影）：字段即契约，不再防御式猜形状。
            # 占位标题按 (tid, 挂载名) 唯一——rumps Menu 以标题为键去重
            tid, mname = entry.tunnel_id, entry.name
            any_mounts = True
            row = rumps.MenuItem(f"__mount_row_{tid}_{mname}__",
                                 callback=None)
            _apply_icon(row, "circle", point_size=9)
            self.refs[("mount_row", tid, mname)] = row
            action = rumps.MenuItem(
                f"__mount_action_{tid}_{mname}__",
                callback=a.make_toggle_mount(tid, mname))
            _apply_icon(action, "fw_start")
            self.refs[("mount_action", tid, mname)] = action
            row.add(action)
            item = rumps.MenuItem(
                i18n.t("mount.open_dir"),
                callback=a.make_open_mount_dir(tid, mname))
            _apply_icon(item, "folder")
            row.add(item)
            parent.add(row)

        if not any_mounts:
            item = rumps.MenuItem(i18n.t("mount.configure"),
                                  callback=a.show_prefs_mounts)
            _apply_icon(item, "folder")
            parent.add(item)
        self._refresh_mount_rows(st)
        self.refs["group_mount"] = parent
        return parent

    def _refresh_mount_rows(self, st):
        """挂载区动态段：行尾标/圆点/异常详情/挂载-卸载动作标签。"""
        for entry in (st.mount_states or ()):
            row = self.refs.get(("mount_row", entry.tunnel_id, entry.name))
            if row is None:
                continue
            status, error = entry.status, entry.error
            base = f"{entry.tunnel_name} · {entry.name}"
            kind, key = _MOUNT_TAIL.get(status, ("idle", None))
            if status == "error" and error:
                title = f"{base} · {i18n.t('mount.tail.error_detail', detail=_truncate(error, 40))}"
            else:
                title = f"{base} · {i18n.t(key)}" if key else base
            if row.title != title:
                row.title = title
                _apply_status_dot(row, kind, point_size=9)
            action = self.refs.get(
                ("mount_action", entry.tunnel_id, entry.name))
            if action is not None:
                active = status in ("mounted", "mounting", "unmounting")
                new_title = i18n.t(
                    "mount.action.unmount" if active else "mount.action.mount")
                if action.title != new_title:  # 图标随标题变化才重挂
                    action.title = new_title
                    _apply_icon(action, "fw_stop" if active else "fw_start")

    def _build_vpn_submenu(self):
        """VPN 网络 ▸ —— 全局单连接（spec §5.3）：一行动词 + 状态点 +
        行尾状态词（A 类语法）；未配置时点击打开设置窗。"""
        st = self._get_state()
        a = self._app
        parent = rumps.MenuItem(i18n.t("menu.group.vpn"), callback=None)
        _apply_icon(parent, "shield")
        self.refs["group_vpn"] = parent

        active = st.vpn_status in ("connecting", "connected", "reconnecting")
        verb = i18n.t("vpn.disconnect" if active else "vpn.connect")
        target = _truncate(st.vpn_server, 24) if st.vpn_server else ""
        kind, key = _VPN_TAIL.get(st.vpn_status, ("idle", "vpn.tail.idle"))
        item = rumps.MenuItem(f"{verb} {target} · {i18n.t(key)}".strip(),
                              callback=a.toggle_vpn)
        _apply_status_dot(item, kind, point_size=10)
        parent.add(item)

        if st.vpn_status == "error" and st.vpn_error:
            parent.add(rumps.MenuItem(
                f"  {_truncate(st.vpn_error, 60)}", callback=None))
        return parent

    def _build_capture_submenu(self):
        a = self._app
        st = self._get_state()
        parent = rumps.MenuItem(i18n.t("menu.group.capture"), callback=None)
        _apply_icon(parent, "capture")
        # A 类状态语法：标题=纯动作（启动/停止抓包），状态进点
        # （绿=运行/黄=启动中/黑=已停止/红=异常），引导与详情进 hint 行
        item = rumps.MenuItem(
            i18n.t("capture.stop" if st.capture_enabled else "capture.start"),
            callback=a.toggle_capture, key="m")
        _apply_status_dot(item, st.capture_state, point_size=9)
        parent.add(item)
        if st.capture_hint:
            parent.add(rumps.MenuItem(st.capture_hint, callback=None))
        parent.add(None)
        item = rumps.MenuItem(i18n.t("capture.open_dir"),
                              callback=a.open_capture_dir)
        _apply_icon(item, "folder")
        parent.add(item)
        item = rumps.MenuItem(i18n.t("capture.today_jsonl"),
                              callback=a.open_today_jsonl)
        _apply_icon(item, "jsonl")
        parent.add(item)
        self.refs["group_capture"] = parent
        return parent

    def _build_suanpan_submenu(self):
        a = self._app
        st = self._get_state()
        parent = rumps.MenuItem(i18n.t("menu.group.router"), callback=None)
        _apply_icon(parent, "router")
        # A 类状态语法：标题=动作，状态点=现状（绿=运行/黑=已停止/
        # 红=启动失败——结构性动作行随运行态出现，状态不再藏在结构里）
        item = rumps.MenuItem(
            i18n.t("router.stop" if st.suanpan_running else "router.start"),
            callback=a.toggle_suanpan)
        _apply_status_dot(item, "ok" if st.suanpan_running
                          else ("err" if st.suanpan_error else "idle"),
                          point_size=9)
        parent.add(item)
        if st.suanpan_running:
            item = rumps.MenuItem(i18n.t("router.restart"),
                                  callback=a.restart_suanpan)
            _apply_icon(item, "cycle")
            parent.add(item)
            item = rumps.MenuItem(i18n.t("router.reload"),
                                  callback=a.reload_suanpan)
            _apply_icon(item, "refresh")
            parent.add(item)
        parent.add(None)
        item = rumps.MenuItem(i18n.t("router.setup_agent"),
                              callback=a.show_agent_setup)
        _apply_icon(item, "wand.and.stars")
        parent.add(item)
        item = rumps.MenuItem(i18n.t("router.copy_url"),
                              callback=a.copy_suanpan_url)
        _apply_icon(item, "doc")
        parent.add(item)
        item = rumps.MenuItem(i18n.t("router.copy_example"),
                              callback=a.copy_suanpan_example)
        _apply_icon(item, "clipboard")
        parent.add(item)
        self.refs["group_router"] = parent
        return parent

    def _build_system_submenu(self):
        """选 项 ▸ —— B 类设置（防睡眠 / 登录启动 / 配置 API / 语言）。

        状态语法：设置用 macOS 母语 ✓（NSMenuItem.state），标题保持
        中性名词——「防睡眠 ✓」即当前值，点按即切换；不染运行色
        （「防睡眠开着」不是一种「运行」）。一次声明成表。"""
        a = self._app
        st = self._get_state()
        parent = rumps.MenuItem(i18n.t("menu.group.system"), callback=None)
        _apply_icon(parent, "system")
        for ref_key, label_key, cfg_key, callback, shortcut, icon in (
                ("prevent_sleep", "system.prevent_sleep", "prevent_sleep",
                 a.toggle_prevent_sleep, "n", "sleep"),
                ("launch_login", "system.launch_login", "launch_at_login",
                 a.toggle_launch_at_login, "k", "login"),
                ("config_api", "system.config_api", "config_api_enabled",
                 a.toggle_config_api, None, "network")):
            item = rumps.MenuItem(i18n.t(label_key), callback=callback,
                                  key=shortcut)
            _apply_icon(item, icon)
            _apply_check(item, bool(st.config.get(cfg_key)))
            self.refs[ref_key] = item
            parent.add(item)
        # 语言子菜单（ADR-012）：✓ 跟随磁盘偏好值（auto 也是一等选项）；
        # 写径 make_set_language → update_mp → _apply_language + struct_key
        # 既有机制整树重建换文案
        parent.add(None)
        lang_sub = rumps.MenuItem(i18n.t("system.language"), callback=None)
        _apply_icon(lang_sub, "globe")
        pref = st.config.get("language") or i18n.DEFAULT_LANGUAGE
        for value, key in ((i18n.AUTO, "system.language.auto"),
                           ("zh-CN", "system.language.zh"),
                           ("en", "system.language.en")):
            item = rumps.MenuItem(i18n.t(key),
                                  callback=a.make_set_language(value))
            _apply_check(item, pref == value)
            lang_sub.add(item)
        parent.add(lang_sub)
        return parent

    def _build_footer(self):
        app = self._app
        a = self._app
        item = rumps.MenuItem(i18n.t("footer.preferences"),
                              callback=a.show_preferences, key=",")
        _apply_icon(item, "prefs")
        app.menu.add(item)
        item = rumps.MenuItem(i18n.t("footer.view_log"),
                              callback=a.show_log_window, key="l")
        _apply_icon(item, "search")
        app.menu.add(item)
        item = rumps.MenuItem(i18n.t("footer.copy_instructions"),
                              callback=a.copy_agent_instructions)
        _apply_icon(item, "clipboard")
        app.menu.add(item)
        app.menu.add(None)
        item = rumps.MenuItem(i18n.t("footer.about"), callback=a.about)
        _apply_icon(item, "about")
        app.menu.add(item)
        item = rumps.MenuItem(i18n.t("common.quit"), callback=a.quit_app,
                              key="q")
        _apply_icon(item, "quit")
        app.menu.add(item)

    # ── dynamic title refresh ─────────────────────────────

    def refresh_titles(self):
        st = self._get_state()
        s = st.ssh_status
        tunnel = st.current_server
        tunnel_name = tunnel.get("name") if tunnel else None
        tunnel_name = tunnel_name or (
            f"{(tunnel.get('ssh') or {}).get('user', '')}@{(tunnel.get('ssh') or {}).get('host', '')}" if tunnel else i18n.t("status.name.unconfigured"))

        # Proxy status line —— 颜色由行首圆点图标承载（emoji 已退役）
        if st.paused:
            proxy_text = i18n.t("status.proxy.paused", name=tunnel_name)
        elif s == "connected":
            proxy_text = f"AI Proxy · {tunnel_name}"
        elif s == "connecting":
            proxy_text = i18n.t("status.proxy.connecting", name=tunnel_name)
        elif s == "error":
            proxy_text = i18n.t("status.proxy.failed", name=tunnel_name)
        elif tunnel:
            # 断开也保留隧道名——此刻恰恰更需要知道当前配的是谁
            proxy_text = i18n.t("status.proxy.disconnected", name=tunnel_name)
        else:
            proxy_text = "AI Proxy"
        # 多活：转发会话在跑时状态行附转发计数（主图标语义不变——只反映
        # 代理会话，:8888 上游只依赖它）；分隔符全菜单统一「·」
        fw_up = sum(1 for f in (st.forward_states or ())
                    if f.status == "connected")
        if fw_up:
            proxy_text += f" · {i18n.t('status.proxy.fw_count', n=fw_up)}"
        # 挂载计数同款模式（ADR-007）：只数已挂载
        mounts_up = sum(1 for entry in (st.mount_states or ())
                        if entry.status == "mounted")
        if mounts_up:
            proxy_text += f" · {i18n.t('status.proxy.mount_count', n=mounts_up)}"
        # 故障可见性（UX 批次）：异常不数成功、顶部无感知的时代结束——
        # 转发/挂载的 error 态在状态行立即可见，不必逐层展开子菜单
        fw_bad, mounts_bad = _error_counts(st)
        if fw_bad:
            proxy_text += f" · {i18n.t('status.proxy.fw_bad', n=fw_bad)}"
        if mounts_bad:
            proxy_text += f" · {i18n.t('status.proxy.mount_bad', n=mounts_bad)}"
        self._set_title("proxy_status", proxy_text)

        # Router status line —— 原始错误串不进菜单（截断读不完也无法
        # 复制）；短状态 + 详情走日志窗
        if st.suanpan_running:
            router_text = f"AI Router · {st.suanpan_listen_address}"
        elif st.suanpan_error:
            router_text = i18n.t("status.router.failed")
        else:
            router_text = "AI Router"
        self._set_title("router_status", router_text)

        # Traffic line —— 方向箭头由行首图标承载
        if "traffic" in self.refs:
            snap = st.stats_snapshot
            traffic_text = (
                f"{_human(snap['rate_down'], 'B/s')}"
                f"  ·  {_human(snap['rate_up'], 'B/s')}"
                f"  ·  {i18n.t('status.traffic.connections', n=snap['active_connections'])}"
            )
            self._set_title("traffic", traffic_text)

        # B 类设置：原生 ✓ 随磁盘真相收敛（标题中性名词不变）
        for ref_key, cfg_key in (("prevent_sleep", "prevent_sleep"),
                                 ("launch_login", "launch_at_login"),
                                 ("config_api", "config_api_enabled")):
            _apply_check(self.refs.get(ref_key),
                         bool(st.config.get(cfg_key)))

        # 端口映射 / 挂载动态段（UX 批次）：状态翻转就地刷新，不重建
        self._refresh_forward_rows(st)
        self._refresh_mount_rows(st)
        # 组标题异常 rollup：默认安静，异常响亮
        self._refresh_group_rollups(st)

    def _refresh_group_rollups(self, st):
        """组标题异常 rollup（状态语法）：闭合菜单一眼判健康——组内有
        error 态才挂「⚠ n」，正常无任何标记（组级绿点是噪音）。异常
        计数与状态行 ⚠ 计数经 _error_counts 同源。"""
        fw_bad, mounts_bad = _error_counts(st)
        rollups = (
            ("group_proxy", "menu.group.proxy",
             1 if st.ssh_status == "error" else 0),
            ("group_forward", "menu.group.forward", fw_bad),
            ("group_mount", "menu.group.mount", mounts_bad),
            ("group_vpn", "menu.group.vpn",
             1 if st.vpn_status == "error" else 0),
            ("group_router", "menu.group.router",
             1 if st.suanpan_error else 0),
            ("group_capture", "menu.group.capture",
             1 if st.capture_state == "err" else 0),
        )
        for ref_key, base_key, bad in rollups:
            base = i18n.t(base_key)
            self._set_title(ref_key, f"{base} ⚠ {bad}" if bad else base)

    def _set_title(self, key, text):
        item = self.refs.get(key)
        if item is not None and item.title != text:
            item.title = text

    # ── status bar icon ───────────────────────────────────

    def set_status_icon(self, color_key):
        _, emoji = STATUS_STATE_STYLE[color_key]
        item = getattr(getattr(self._app, "_nsapp", None), "nsstatusitem", None)
        if item is None or not self._icon_ok:
            self._app.title = emoji
            return
        try:
            img = self._status_image(color_key)
        except Exception:
            logger.exception("Custom status icon failed; using emoji")
            self._icon_ok = False
            self._app.title = emoji
            return
        item.setImage_(img)
        item.setTitle_("")

    def _status_image(self, color_key):
        cached = self._icon_cache.get(color_key)
        if cached is not None:
            return cached
        from AppKit import NSImage
        from Foundation import NSMakeSize
        resource = _ICON_RESOURCE_FOR_KEY.get(color_key, STATUS_ICON_GRAY_RESOURCE)
        base = NSImage.alloc().initWithContentsOfFile_(
            _resource_path(resource))
        if base is None:
            raise RuntimeError("Status icon unavailable: " + resource)
        img = base.copy()
        img.setSize_(NSMakeSize(22, 22))
        img.setTemplate_(False)
        self._icon_cache[color_key] = img
        return img
