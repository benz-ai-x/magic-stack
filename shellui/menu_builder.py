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
from shared.server_shape import (
    is_proxy_server, proxy_server, server_forwards, servers, ssh_node,
)
from shared import i18n
from shared.i18n import DEFAULT_LANGUAGE
from util import resource_path as _resource_path, truncate as _truncate

logger = logging.getLogger("magic-proxy.menu")

STATUS_ICON_RESOURCE = "MenubarIcon.png"          # 图标本体即蓝底（蓝=SSH）
STATUS_ICON_GREEN_RESOURCE = "MenubarIcon-green.png"
STATUS_ICON_GRAY_RESOURCE = "MenubarIcon-gray.png"
STATUS_ICON_YELLOW_RESOURCE = "MenubarIcon-yellow.png"
STATUS_STATE_STYLE = {
    "blue":   ("systemBlueColor", "🔵"),
    "green":  ("systemGreenColor", "🟢"),
    "yellow": ("systemYellowColor", "🟡"),
    "gray":   ("systemGrayColor", "⚪"),
}
_ICON_RESOURCE_FOR_KEY = {
    "blue":   STATUS_ICON_RESOURCE,
    "green":  STATUS_ICON_GREEN_RESOURCE,
    "yellow": STATUS_ICON_YELLOW_RESOURCE,
    "gray":   STATUS_ICON_GRAY_RESOURCE,
}


def _menubar_color(ssh_status, paused, vpn_status):
    """菜单栏主图标色（用户拍板语义 2026-09-27）：灰 = SSH/VPN 均未
    连接，蓝 = SSH 已连接，绿 = VPN 已连接，黄 = 连接中/暂停。

    互斥模型下 SSH 与 VPN 不同时活跃；VPN 收尾期（exiting）与
    connecting 同档黄。SSH error = 未连接 → 灰（详情进菜单）。"""
    if vpn_status == "connected":
        return "green"
    if vpn_status in ("connecting", "reconnecting", "exiting"):
        return "yellow"
    if paused or ssh_status == "connecting":
        return "yellow"
    if ssh_status == "connected":
        return "blue"
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


def _status_color(kind):
    """状态点着色（动态系统色，明暗模式自适应）。idle=未启动用
    labelColor（浅色模式黑/深色模式白）——二元语义：运行绿、未启动黑；
    黄只留给进行中（connecting/mounting），红只留给异常；info=蓝，
    接入行「SSH 已连接」专用（与菜单栏图标四色语义同源）。"""
    try:
        from AppKit import NSColor
        return {"ok": NSColor.systemGreenColor(),
                "info": NSColor.systemBlueColor(),
                "warn": NSColor.systemYellowColor(),
                "err": NSColor.systemRedColor(),
                "idle": NSColor.labelColor()}[kind]
    except Exception:
        return None


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
    vpn_tun_ip: str = ""


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



def _set_enabled(item, on):
    """行置灰（NSMenuItem.setEnabled——rumps 未封装，经 _menuitem 通道，
    与 _apply_icon 的 setImage 同款）。置灰不隐藏：结构恒定（就地刷新
    机制的前提），且明示「有这些、当前模式不可用」。"""
    if item is None:
        return
    try:
        item._menuitem.setEnabled_(bool(on))
    except Exception:
        pass

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
            st.suanpan_running,
            st.suanpan_error[:50] if st.suanpan_error else "",
            fw_identity,   # 行集合变化（配置增删）→ 重建
            mount_identity,
            st.capture_enabled,      # 抓包动词方向（结构恒定，标签刷新）
            st.capture_state,        # 状态点（err↔ok 随重建换点）
            st.capture_hint,
            st.language,             # ADR-012：语言翻转 → 整树重建换文案
            st.vpn_status,           # VPN 态变 → 重建（接入行换点/重连行增减）
            st.vpn_server,
        )

    # ── full build ────────────────────────────────────────

    def build(self):
        """五段结构（2026-09-27 定稿，行即开关；ADR-011 修订）：状态
        （流量/异常——仅活跃时有行）→ 接入（SSH/VPN 行即开关）→ 功能
        （系统代理/端口映射/远程挂载——服务层自治，恒可用；经代理启动
        随 -D 接入态单独置灰）→ AI ▸ → 应用。连接状态不再三处重复：
        接入行圆点即状态，菜单栏图标同语义（灰=无连接/蓝=SSH/绿=VPN/
        黄=进行中）。"""
        app = self._app
        app.menu.clear()
        self.refs = {}

        if self._build_header():
            app.menu.add(None)
        self._build_access_section()
        app.menu.add(None)
        self._build_features_section()
        app.menu.add(None)
        app.menu.add(self._build_ai_submenu())
        app.menu.add(None)
        self._build_footer()
        self.refresh_titles()

    def _build_header(self):
        """状态段（行即开关定稿）：常态零行——接入行圆点已回答「连没
        连」，菜单栏图标同语义；只有活跃期才有内容：流量行（SSH 已
        连接）、连接日志尾行、异常详情行。返回是否加了行。"""
        app = self._app
        st = self._get_state()
        refs = self.refs
        s = st.ssh_status
        added = False

        # Traffic line (SSH connected only) —— 接入行之外的唯一增量信息
        if s == "connected" and not st.paused:
            refs["traffic"] = rumps.MenuItem("__traffic__", callback=None)
            _apply_icon(refs["traffic"], "updown", point_size=10,
                        color=_status_color("idle"))
            app.menu.add(refs["traffic"])
            added = True

        # Connecting log lines（进行中在做什么，瞬态反馈）
        if s == "connecting" and st.ssh_cmd_str:
            app.menu.add(rumps.MenuItem(
                f"  {_truncate(st.ssh_cmd_str, 60)}", callback=None))
            if st.ssh_log:
                app.menu.add(None)
                for line in st.ssh_log.split("\n")[-3:]:
                    app.menu.add(rumps.MenuItem(
                        f"  {_truncate(line, 60)}", callback=None))
            added = True

        # 异常详情（短行就地、详情走日志窗——原始错误串不整段进菜单）
        if s == "error" and st.ssh_error_msg:
            app.menu.add(rumps.MenuItem(
                f"  {_truncate(st.ssh_error_msg, 80)}", callback=None))
            added = True
        if st.vpn_status == "error" and st.vpn_error:
            app.menu.add(rumps.MenuItem(
                f"  {_truncate(st.vpn_error, 80)}", callback=None))
            added = True

        return added

    # ── 接入段（行即开关）：SSH/VPN 各一行，圆点即状态、点击即动作 ──

    def _build_access_section(self):
        """接入段（定稿）：连接方式各一行——行即开关（Clash 模式）。
        圆点 = 状态（蓝=SSH 已连 / 绿=VPN 已连 / 黄=进行中 / 红=异常 /
        无点=空闲），点击 = 动作（空闲连 / 活跃断 / 对端活跃确认切换；
        失败态再点即重连——手动 kick 由行自身覆盖，不占独立行）。
        多服务器时附「服务器 ▸」。标题与圆点由 refresh_titles 就地刷新
        （占位标题防重名）。"""
        st = self._get_state()
        a = self._app

        row = rumps.MenuItem("__ssh_access__", callback=a.toggle_ssh, key="p")
        self.refs["ssh_access"] = row
        self._app.menu.add(row)

        row = rumps.MenuItem("__vpn_access__", callback=a.toggle_vpn)
        self.refs["vpn_access"] = row
        self._app.menu.add(row)

        # 服务器 ▸（多服务器渐进披露；单服务器时 SSH 行副标题即其名）
        rows_tunnels = [t for t in servers(st.config)
                        if isinstance(t, dict)]
        if len(rows_tunnels) > 1:
            sub = rumps.MenuItem(i18n.t("mode.servers"), callback=None)
            _apply_icon(sub, "tunnel_row")
            for t in rows_tunnels:
                _ssh = ssh_node(t)
                marker = "✓ " if is_proxy_server(st.config, t) else ""
                name = t.get("name") or                     f"{_ssh.get('user', '')}@{_ssh.get('host', '')}"
                item = rumps.MenuItem(
                    f"{marker}{name}",
                    callback=a.make_switch_server(t.get("id") or ""))
                _apply_icon(item, "tunnel_row")
                sub.add(item)
            self.refs["servers_sub"] = sub
            self._app.menu.add(sub)

    # ── 功能段：连上之后用什么（服务层自治，恒可用）──────────────

    def _build_features_section(self):
        """功能段：系统代理（B 类 ✓）+ 端口映射/远程挂载组 + 经代理
        启动。端口映射与挂载是服务层（ADR-011 修订）——永不随 VPN
        置灰，各行圆点自证健康；经代理启动依赖 -D 接入（:8888 上游），
        接入未连接时单独置灰（接入态在 struct_key 内，态变重建换灰）。"""
        st = self._get_state()
        a = self._app
        rows = []

        item = rumps.MenuItem(i18n.t("proxy.sysproxy"),
                              callback=a.toggle_system_proxy, key="g")
        _apply_check(item, st.sys_proxy_on)
        self.refs["sys_proxy_check"] = item
        rows.append(item)

        rows.append(self._build_forward_submenu())
        rows.append(self._build_mount_submenu())

        # 经代理启动（装了 Chromium 系应用才出现；依赖 -D 接入）
        apps_list = chromium_proxy.installed_apps()
        if apps_list:
            sub = rumps.MenuItem(i18n.t("proxy.launch_apps"), callback=None)
            _apply_icon(sub, "launch")
            for entry in apps_list:
                item = rumps.MenuItem(
                    entry["name"], callback=a.make_launch_proxied(entry))
                _apply_icon(item, "launch")
                sub.add(item)
            rows.append(sub)
            self.refs["launch_apps_menu"] = sub

        for item in rows:
            self._app.menu.add(item)
        if "launch_apps_menu" in self.refs:
            _set_enabled(self.refs["launch_apps_menu"],
                         st.ssh_status == "connected")

    # ── AI 合并组（重设计 ④）：路由 + 抓包 ──────────────────

    def _build_ai_submenu(self):
        """AI ▸ —— AI 工具栈运行物合并组：路由（启停/重启/重载/配置/
        复制）+ 抓包（启停/目录/JSONL）。高频启停一级，低频动作二级；
        rollup 聚合路由错误与抓包异常。"""
        a = self._app
        st = self._get_state()
        parent = rumps.MenuItem(i18n.t("menu.group.ai"), callback=None)
        _apply_icon(parent, "router")

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
        item = rumps.MenuItem(i18n.t("router.setup_agent"),
                              callback=a.show_agent_setup)
        _apply_icon(item, "wand.and.stars")
        parent.add(item)
        item = rumps.MenuItem(i18n.t("router.copy_url"),
                              callback=a.copy_suanpan_url)
        _apply_icon(item, "doc")
        parent.add(item)
        # 复制 AI 助手指令（自 footer 并入——同为 AI 助手脚手架动作）
        item = rumps.MenuItem(i18n.t("footer.copy_instructions"),
                              callback=a.copy_agent_instructions)
        _apply_icon(item, "clipboard")
        parent.add(item)

        parent.add(None)
        item = rumps.MenuItem(
            i18n.t("capture.stop" if st.capture_enabled else "capture.start"),
            callback=a.toggle_capture, key="m")
        _apply_status_dot(item, st.capture_state, point_size=9)
        parent.add(item)
        if st.capture_hint:
            parent.add(rumps.MenuItem(st.capture_hint, callback=None))
        item = rumps.MenuItem(i18n.t("capture.open_dir"),
                              callback=a.open_capture_dir)
        _apply_icon(item, "folder")
        parent.add(item)
        item = rumps.MenuItem(i18n.t("capture.today_jsonl"),
                              callback=a.open_today_jsonl)
        _apply_icon(item, "jsonl")
        parent.add(item)

        self.refs["group_ai"] = parent
        return parent

    def _build_forward_submenu(self):
        """端口映射 ▸ —— 每台服务器一条独立纯 -L 会话（ADR-011 修订：
        全服务器统一行结构——代理服务器不再特判，「随代理运行」上下文
        行与「启停将重启代理」警示随 -L 便车退役一并消失）。

        逐条启停（v0.11）：每条转发独立成行（点击即启停），圆点随会话
        状态着色、停用行灰点。UX 批次：①单隧道拍平（包装行只在多隧道
        时有意义——转发行一级直达）；②行结构**恒定**（会话启停动作恒
        在，标签由刷新段定「启动/停止」），状态/文案就地刷新——后台
        状态翻转不重建整树。
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
            if single:
                host = parent          # 拍平：行直接挂顶层（免一层嵌套）
            else:
                # 多隧道：包装行承载隧道名/尾标。占位标题按隧道 id
                # 唯一——rumps Menu 以标题为键，重名行互相覆盖
                host = rumps.MenuItem(f"__fw_tunnel_{tid}__", callback=None)
                _apply_icon(host, "tunnel_row")
                self.refs[("fw_tunnel", tid)] = host
            self._add_forward_rows(host, a, tid, forwards)
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
        查找不便宜，不能每 tick 全量重设）。全服务器统一口径（ADR-011
        修订：代理服务器也是 forward_states 里的一条）。"""
        tunnels = servers(st.config)
        fw_running = {f.tunnel_id: f.status
                      for f in (st.forward_states or ())}
        for i, t in enumerate(tunnels):
            if not isinstance(t, dict):
                continue
            tid = t.get("id") or f"#{i}"
            name = t.get("name") or \
                f"{(t.get('ssh') or {}).get('user', '')}@{(t.get('ssh') or {}).get('host', '')}"
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
            session_up = fw_running.get(tid) == "connected"
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




    def _build_footer(self):
        """应用段（定稿）：偏好/日志/关于/退出。防睡眠与登录启动退役
        回设置窗（一次性设置不占一级）；复制 AI 助手指令并入 AI ▸。"""
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

        # 接入行（行即开关定稿）：标题 = 对象（方式 + 服务器），圆点 =
        # 状态（蓝=SSH 已连 / 绿=VPN 已连 / 黄=进行中 / 红=异常 / 无点=
        # 空闲）——状态与动作同在一行，不再三处重复
        if st.paused:
            ssh_title = i18n.t("status.ssh.paused", name=tunnel_name)
        elif s == "connecting":
            ssh_title = i18n.t("status.ssh.connecting", name=tunnel_name)
        elif s == "error":
            ssh_title = i18n.t("status.ssh.failed", name=tunnel_name)
        elif s == "connected":
            ssh_title = i18n.t("status.ssh.connected", name=tunnel_name)
        else:
            ssh_title = i18n.t("access.ssh", name=tunnel_name)
        self._set_title("ssh_access", ssh_title)
        ssh_row = self.refs.get("ssh_access")
        if ssh_row is not None:
            if s == "connected" and not st.paused:
                kind = "info"          # 蓝 —— 与菜单栏图标同语义
            elif s == "connecting" or st.paused:
                kind = "warn"
            elif s == "error":
                kind = "err"
            else:
                kind = None            # 空闲：无点（点击即连接）
            if kind is not None:
                _apply_status_dot(ssh_row, kind, point_size=10)

        if st.vpn_status == "connected":
            vpn_title = i18n.t("access.vpn.connected",
                               ip=st.vpn_tun_ip or st.vpn_server or "")
            vpn_kind = "ok"            # 绿 —— 与菜单栏图标同语义
        elif st.vpn_status in ("connecting", "reconnecting", "exiting"):
            vpn_title = i18n.t(
                "access.vpn.reconnecting"
                if st.vpn_status == "reconnecting" else "access.vpn.connecting")
            vpn_kind = "warn"
        elif st.vpn_status == "error":
            vpn_title = i18n.t("access.vpn.error")
            vpn_kind = "err"
        elif st.vpn_server:
            vpn_title = i18n.t("access.vpn.named", name=st.vpn_server)
            vpn_kind = None
        else:
            vpn_title = i18n.t("access.vpn")
            vpn_kind = None
        self._set_title("vpn_access", vpn_title)
        vpn_row = self.refs.get("vpn_access")
        if vpn_row is not None and vpn_kind is not None:
            _apply_status_dot(vpn_row, vpn_kind, point_size=10)

        # Traffic line —— 方向箭头由行首图标承载（SSH 已连接才有此行）
        if "traffic" in self.refs:
            snap = st.stats_snapshot
            traffic_text = (
                f"{_human(snap['rate_down'], 'B/s')}"
                f"  ·  {_human(snap['rate_up'], 'B/s')}"
                f"  ·  {i18n.t('status.traffic.connections', n=snap['active_connections'])}"
            )
            self._set_title("traffic", traffic_text)

        # B 类设置：原生 ✓ 随磁盘真相收敛（标题中性名词不变）
        _apply_check(self.refs.get("sys_proxy_check"), st.sys_proxy_on)

        # 端口映射 / 挂载动态段（UX 批次）：状态翻转就地刷新，不重建
        self._refresh_forward_rows(st)
        self._refresh_mount_rows(st)
        # 组标题 rollup：健康挂活跃计数，异常挂 ⚠ 计数
        self._refresh_group_rollups(st)

    def _refresh_group_rollups(self, st):
        """组标题 rollup：默认安静、异常响亮；健康且有活跃项时挂计数
        （端口映射 n=已连通会话，远程挂载 n=已挂载）。异常计数与状态
        行计数经 _error_counts 同源。"""
        fw_bad, mounts_bad = _error_counts(st)
        fw_up = sum(1 for f in (st.forward_states or ())
                    if f.status == "connected")
        mounts_up = sum(1 for entry in (st.mount_states or ())
                        if entry.status == "mounted")
        rollups = (
            ("group_forward", "menu.group.forward", fw_up, fw_bad),
            ("group_mount", "menu.group.mount", mounts_up, mounts_bad),
            ("group_ai", "menu.group.ai", 0,
             (1 if st.suanpan_error else 0)
             + (1 if st.capture_state == "err" else 0)),
        )
        for ref_key, base_key, up, bad in rollups:
            base = i18n.t(base_key)
            if bad:
                self._set_title(ref_key, f"{base} ⚠ {bad}")
            elif up:
                self._set_title(ref_key, f"{base} · {up}")
            else:
                self._set_title(ref_key, base)

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
