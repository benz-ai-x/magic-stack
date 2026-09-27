"""Tests for menu_builder.MenuBuilder — struct-key driven rebuild logic."""
import unittest
from unittest.mock import MagicMock

import rumps

from shellui.menu_builder import MenuBuilder, MenuState
from shared.server_shape import is_proxy_server
from tunnel.connection_coordinator import ForwardState
from mount.coordinator import MountState


def _state(**overrides):
    base = dict(
        ssh_status="stopped", ssh_cmd_str="", ssh_log="", ssh_error_msg="",
        paused=False,
        stats_snapshot={"active_connections": 0, "rate_down": 0.0, "rate_up": 0.0},
        config={}, sys_proxy_on=False, sys_proxy_error="",
        capture_enabled=False, capture_state="idle", capture_hint=None,
        suanpan_running=False, suanpan_error="", suanpan_listen_address="",
        current_server=None,
    )
    base.update(overrides)
    return MenuState(**base)


class TestStructKey(unittest.TestCase):
    def test_connection_count_does_not_rebuild_menu(self):
        # #40: active_connections fluctuates every tick while traffic flows;
        # it only affects the traffic *title*, never the menu structure.
        key_idle = MenuBuilder(MagicMock(), lambda: _state()).struct_key()
        key_busy = MenuBuilder(
            MagicMock(),
            lambda: _state(stats_snapshot={
                "active_connections": 5, "rate_down": 1.0, "rate_up": 1.0}),
        ).struct_key()
        self.assertEqual(key_idle, key_busy)


class TestStateGrammar(unittest.TestCase):
    """状态语法矩阵：A 类运行物（标题=动作，状态点=现状）与 B 类设置
    （中性名词 + 原生 ✓）；组标题异常 rollup（默认安静，异常响亮）。"""

    @staticmethod
    def _cfg():
        return TestMultiActiveTunnels._cfg()

    def _build(self, st):
        app = MagicMock()
        with unittest.mock.patch("shellui.menu_builder.chromium_proxy.installed_apps",
                                 return_value=[]):
            mb = MenuBuilder(app, lambda: st)
            mb.build()
        return mb

    def test_router_toggle_title_follows_running(self):
        mb = self._build(_state(suanpan_running=True,
                                suanpan_listen_address="127.0.0.1:9527"))
        titles = [i.title for i in mb.refs["group_ai"].values()
                  if hasattr(i, "title")]
        self.assertIn("停止路由", titles)
        self.assertNotIn("启动路由", titles)

    def test_capture_toggle_pure_verbs_with_hint(self):
        mb = self._build(_state(
            capture_enabled=False, capture_state="idle",
            capture_hint="  首次抓包需先信任本地根 CA——启动后按引导操作"))
        titles = [i.title for i in mb.refs["group_ai"].values()
                  if hasattr(i, "title")]
        self.assertIn("启动抓包", titles)          # 标题=纯动作
        self.assertNotIn("（需先信任证书）", titles[0])  # 状态/引导不进标题
        self.assertTrue(any("首次抓包" in t for t in titles))  # hint 行
        mb2 = self._build(_state(capture_enabled=True, capture_state="ok"))
        titles2 = [i.title for i in mb2.refs["group_ai"].values()
                   if hasattr(i, "title")]
        self.assertIn("停止抓包", titles2)

    def test_group_rollup_counts_when_healthy(self):
        """组标题 rollup（定稿）：健康且有活跃项挂计数（端口映射 n=已
        连通会话、远程挂载 n=已挂载；connecting 不计数），AI 组无计数。"""
        mb = self._build(_state(
            ssh_status="connected", config=self._cfg(),
            forward_states=(ForwardState("t-2", "AWS-ap", "connected"),
                            ForwardState("t-3", "x", "connecting")),
            mount_states=(MountState("t-1", "a", "data", "mounted", ""),),
            suanpan_running=True))
        self.assertEqual(mb.refs["group_forward"].title, "端口映射 · 1")
        self.assertEqual(mb.refs["group_mount"].title, "远程挂载 · 1")
        self.assertEqual(mb.refs["group_ai"].title, "AI")

    def test_group_rollup_marks_errors(self):
        mb = self._build(_state(
            ssh_status="error", config=self._cfg(),
            forward_states=(ForwardState("t-2", "AWS-ap", "error"),),
            mount_states=(MountState("t-1", "a", "data", "error", "x"),
                          MountState("t-1", "a", "ws", "error", "y")),
            suanpan_error="dep missing", capture_state="err"))
        self.assertEqual(mb.refs["group_forward"].title, "端口映射 ⚠ 1")
        self.assertEqual(mb.refs["group_mount"].title, "远程挂载 ⚠ 2")
        # AI 合并组：路由错误与抓包异常聚合计数
        self.assertEqual(mb.refs["group_ai"].title, "AI ⚠ 2")

    def test_header_traffic_line_connected_only(self):
        """状态段（行即开关定稿）：常态零行——接入行圆点已承载状态；
        仅 SSH 已连接时出现流量行。"""
        mb = self._build(_state(
            ssh_status="connected", config=self._cfg()))
        self.assertIn("traffic", mb.refs)
        mb2 = self._build(_state(
            ssh_status="stopped", config=self._cfg()))
        self.assertNotIn("traffic", mb2.refs)

    def test_access_rows_idle_no_dots_no_reconnect(self):
        """接入段（行即开关）：空闲态两行——SSH 行带服务器名（断开也
        知道当前配的是谁）、VPN 行无装饰；无重连行、无服务器子菜单
        （单服务器渐进披露）。"""
        cfg = {"proxy_server_id": "t-1", "servers": [
            {"id": "t-1", "name": "Aws-eu", "ssh": {"host": "a"}}]}
        mb = self._build(_state(config=cfg, current_server=cfg["servers"][0]))
        self.assertEqual(mb.refs["ssh_access"].title, "SSH · Aws-eu")
        self.assertEqual(mb.refs["vpn_access"].title, "VPN")
        self.assertNotIn("reconnect_row", mb.refs)
        self.assertNotIn("servers_sub", mb.refs)

    def test_access_rows_connected_ssh_no_extra_rows(self):
        """接入段恒两行（+多服务器时的服务器▸）——独立「重新连接」行
        已删（用户裁决 2026-09-27：突兀；失败态再点 SSH 行即重连）。"""
        cfg = self._cfg()
        mb = self._build(_state(
            ssh_status="connected", config=cfg,
            current_server=cfg["servers"][0]))
        self.assertEqual(mb.refs["ssh_access"].title, "SSH · Aws-eu")
        self.assertNotIn("reconnect_row", mb.refs)

    def test_access_rows_vpn_connected_features_stay_enabled(self):
        """VPN 激活：VPN 行带 tun IP；重连行退场；功能段恒可用
        （ADR-011 修订：服务层自治——转发/挂载/系统代理不随接入陪葬，
        各行圆点自证健康）。"""
        mb = self._build(_state(
            vpn_status="connected", vpn_tun_ip="10.8.0.2",
            config=self._cfg()))
        self.assertEqual(mb.refs["vpn_access"].title, "VPN · 10.8.0.2")
        self.assertNotIn("reconnect_row", mb.refs)
        self.assertTrue(
            mb.refs["sys_proxy_check"]._menuitem.isEnabled())
        self.assertTrue(mb.refs["group_forward"]._menuitem.isEnabled())
        self.assertTrue(mb.refs["group_mount"]._menuitem.isEnabled())

    def test_access_servers_submenu_multi_server_only(self):
        """多服务器 → 接入段「服务器 ▸」（当前上游 ✓ 打头）；单服务器
        不出现（SSH 行副标题即其名）。"""
        mb = self._build(_state(config=self._cfg()))   # 两台服务器
        sub_titles = [r.title for r in mb.refs["servers_sub"].values()
                      if hasattr(r, "title")]
        self.assertIn("✓ Aws-eu", sub_titles)
        self.assertIn("AWS-ap", sub_titles)

    def test_ai_submenu_carries_copy_instructions(self):
        """「复制 AI 助手指令」自 footer 并入 AI ▸（同类 AI 助手脚手架
        动作归组）；footer 不再出现。"""
        mb = self._build(_state())
        titles = [i.title for i in mb.refs["group_ai"].values()
                  if hasattr(i, "title")]
        self.assertIn("复制 AI 助手指令", titles)


if __name__ == "__main__":
    unittest.main()


class TestMultiActiveTunnels(unittest.TestCase):
    """多活（v0.9）：forward_states 参与 struct_key + 子菜单构建。"""

    @staticmethod
    def _cfg():
        return {"proxy_server_id": "t-1", "servers": [
            {"id": "t-1", "name": "Aws-eu",
             "ssh": {"host": "a"},
             "services": {"ssh": {"forwards": [
                 {"local_port": 9000, "remote_host": "h",
                  "remote_port": 80}]}}},
            {"id": "t-2", "name": "AWS-ap",
             "ssh": {"host": "b"},
             "services": {"ssh": {"forwards": [
                 {"local_port": 9001, "remote_host": "h",
                  "remote_port": 81}]}}},
        ]}

    def test_status_flip_does_not_rebuild_menu(self):
        """UX 批次：后台状态翻转就地刷新，不整树重建——此前任何会话
        connecting→connected 都会 clear+rebuild，用户展开子菜单时塌掉。"""
        cfg = self._cfg()
        key_idle = MenuBuilder(MagicMock(), lambda: _state(
            config=cfg, forward_states=())).struct_key()
        key_up = MenuBuilder(MagicMock(), lambda: _state(
            config=cfg,
            forward_states=(ForwardState("t-2", "AWS-ap", "connected"),))).struct_key()
        self.assertEqual(key_idle, key_up)

    def test_identity_change_rebuilds_menu(self):
        """行集合变化（配置增删转发规则）仍然重建。"""
        cfg = self._cfg()
        cfg_more = self._cfg()
        cfg_more["servers"][1]["services"]["ssh"]["forwards"].append(
            {"local_port": 9002, "remote_host": "h", "remote_port": 82})
        key_a = MenuBuilder(MagicMock(), lambda: _state(config=cfg)).struct_key()
        key_b = MenuBuilder(
            MagicMock(), lambda: _state(config=cfg_more)).struct_key()
        self.assertNotEqual(key_a, key_b)

    def _submenu(self, title, cfg=None, forward_states=(), ssh_status="connected"):
        """直接构建子菜单（真实 rumps.MenuItem 树——MockApp 的 menu 不会
        真建树）；返回 (parent, [子行 MenuItem])。"""
        app = MagicMock()
        with unittest.mock.patch("shellui.menu_builder.chromium_proxy.installed_apps",
                                 return_value=[{"name": "ChatGPT"}]):
            mb = MenuBuilder(app, lambda: _state(
                ssh_status=ssh_status, config=cfg or self._cfg(),
                forward_states=forward_states))
            builder = {"端口映射": mb._build_forward_submenu,
                       "AI": mb._build_ai_submenu}[title]
            parent = builder()
        self._mb = mb
        rows = list(parent.values())
        self._titles = [r.title for r in rows if hasattr(r, "title")]
        return parent, [r for r in rows if hasattr(r, "values")]

    def test_features_section_structure(self):
        """功能段（定稿）：系统代理（B 类 ✓）+ 端口映射/远程挂载组 +
        经代理启动；会话动作（连接/停止/重连/服务器）已归接入段——
        不再在此出现。"""
        app = MagicMock()
        added = []
        app.menu.add.side_effect = lambda i: added.append(i)
        with unittest.mock.patch(
                "shellui.menu_builder.chromium_proxy.installed_apps",
                return_value=[{"name": "ChatGPT"}]):
            mb = MenuBuilder(app, lambda: _state(
                ssh_status="connected", config=self._cfg()))
            mb._build_features_section()
        titles = [i.title for i in added if hasattr(i, "title")]
        self.assertIn("系统代理", titles)           # B 类中性名词 + ✓
        self.assertIn("端口映射", titles)
        self.assertIn("远程挂载", titles)
        self.assertNotIn("停止代理", titles)        # 会话动作在接入行
        self.assertNotIn("连接代理", titles)
        launches = [r for r in added if hasattr(r, "values")]
        self.assertTrue(any(
            "ChatGPT" in [x.title for x in list(l.values())
                          if hasattr(x, "title")] for l in launches))
        self.assertEqual(mb.refs["sys_proxy_check"]._menuitem.state(), 0)

    def test_forward_submenu_structure(self):
        """ADR-011 修订：全服务器统一行结构——代理服务器（t-1）也是
        普通包装行（会话启停/重连），「随代理运行」上下文行退役。"""
        parent, subs = self._submenu(
            "端口映射", forward_states=(ForwardState("t-2", "AWS-ap", "connected"),))
        titles = self._titles
        self.assertFalse(any("随代理运行" in t for t in titles), titles)
        self.assertFalse(any("启停将重启代理" in t for t in titles), titles)
        running = [s for s in subs if s.title.startswith("AWS-ap")]
        idle = [s for s in subs if s.title.startswith("Aws-eu")]
        self.assertTrue(running, titles)
        self.assertIn("— 转发中", running[0].title)
        self.assertIn("— 未启动", idle[0].title)   # 代理服务器无独立会话时如实显示
        items = [i.title for i in list(running[0].values())
                 if hasattr(i, "title")]
        # 逐条转发子行（点击即启停）在前，会话动作在后（标签随状态刷新）
        self.assertIn("9001 → 81 · 已映射", items)
        self.assertIn("停止端口转发", items)
        self.assertIn("重新连接", items)

    def test_forward_submenu_idle_and_no_rules(self):
        cfg = self._cfg()
        cfg["servers"][1]["services"]["ssh"]["forwards"] = []
        parent, subs = self._submenu("端口映射", cfg=cfg)
        titles = self._titles
        idle = [s for s in subs if s.title.startswith("AWS-ap")]
        self.assertIn("— 未启动", idle[0].title)
        items = [i.title for i in list(idle[0].values())
                 if hasattr(i, "title")]
        # 无规则：深链直达偏好设置（不再是死文本行）
        self.assertIn("添加转发规则…", items)
        # t-1 有规则 → 不出现全局空态提示
        self.assertNotIn("添加转发规则…", titles)

    def test_forward_submenu_empty_state_hint(self):
        cfg = self._cfg()
        for t in cfg["servers"]:
            t["services"]["ssh"]["forwards"] = []
        parent, _ = self._submenu("端口映射", cfg=cfg)
        titles = self._titles
        self.assertIn("添加转发规则…", titles)

    def test_forward_empty_state_deep_links_to_prefs(self):
        cfg = self._cfg()
        for t in cfg["servers"]:
            t["services"]["ssh"]["forwards"] = []
        app = MagicMock()
        mb = MenuBuilder(app, lambda: _state(config=cfg))
        parent = mb._build_forward_submenu()
        row = [r for r in parent.values()
               if hasattr(r, "title") and r.title == "添加转发规则…"][0]
        self.assertIs(row.callback, app.show_prefs_forwards)

    def test_forward_submenu_proxy_row_uniform_when_disconnected(self):
        """代理服务器行与会话状态解耦：-D 断开不再影响它的转发行
        呈现（有无独立会话由 forward_states 说话）。"""
        parent, _ = self._submenu("端口映射", ssh_status="stopped")
        idle = [t for t in self._titles if t.startswith("Aws-eu")]
        self.assertTrue(idle)
        self.assertIn("— 未启动", idle[0])
        self.assertNotIn("随代理运行", idle[0])

    def test_forward_submenu_single_tunnel_flattens(self):
        """单隧道拍平：转发行一级直达（免隧道包装行的嵌套）。"""
        cfg = {"proxy_server_id": "t-1", "servers": [
            {"id": "t-1", "name": "Aws-eu",
             "ssh": {"host": "a"},
             "services": {"ssh": {"forwards": [
                 {"local_port": 7001, "remote_host": "h",
                  "remote_port": 71}]}}}]}
        app = MagicMock()
        with unittest.mock.patch("shellui.menu_builder.chromium_proxy.installed_apps",
                                 return_value=[]):
            mb = MenuBuilder(app, lambda: _state(
                ssh_status="connected", config=cfg))
            parent = mb._build_forward_submenu()
        titles = [r.title for r in parent.values() if hasattr(r, "title")]
        # 拍平依旧：转发行与动作行都在顶层（一级）；代理服务器不再有
        # 特判上下文行。无独立会话（forward_states 空）→ 如实未连接 +
        # 启动动作在（会话口径与 -D 状态解耦）
        self.assertIn("7001 → 71 · 未连接", titles)
        self.assertIn("启动端口转发", titles)
        self.assertFalse(any("随代理运行" in t for t in titles), titles)

    def test_footer_slim(self):
        """应用段（定稿）：偏好/日志/关于/退出四行；防睡眠与登录启动
        退役回设置窗（一次性设置不占一级）；复制 AI 助手指令并入 AI ▸。"""
        app = MagicMock()
        added = []
        app.menu.add.side_effect = lambda i: added.append(i)
        mb = MenuBuilder(app, lambda: _state(config={
            "prevent_sleep": True, "launch_at_login": False,
            "config_api_enabled": True, "servers": []}))
        mb._build_footer()
        titles = [i.title for i in added if hasattr(i, "title")]
        self.assertEqual(titles, ["偏好设置…", "查看日志",
                                  "关于 Magic Stack", "退出"])
        self.assertNotIn("prevent_sleep", mb.refs)
        self.assertNotIn("launch_login", mb.refs)


class TestDynamicRefreshInPlace(unittest.TestCase):
    """状态翻转的就地刷新（UX 批次）：行对象不变、标题/标签更新。"""

    @staticmethod
    def _cfg():
        return {"proxy_server_id": "t-1", "servers": [
            {"id": "t-1", "name": "Aws-eu",
             "ssh": {"host": "a"},
             "services": {"ssh": {"forwards": [
                 {"local_port": 9000, "remote_host": "h",
                  "remote_port": 80}]}}},
            {"id": "t-2", "name": "AWS-ap",
             "ssh": {"host": "b"},
             "services": {"ssh": {"forwards": [
                 {"local_port": 9001, "remote_host": "h",
                  "remote_port": 81}]}}},
        ]}

    def test_forward_rows_refresh_without_rebuild(self):
        state = {"forward_states": (ForwardState("t-2", "AWS-ap", "connected"),),
                 "ssh_status": "connected"}
        mb = MenuBuilder(MagicMock(), lambda: _state(
            config=self._cfg(), **state))
        parent = mb._build_forward_submenu()
        row = mb.refs[("fw_row", "t-2", 0)]
        action = mb.refs[("fw_action", "t-2")]
        self.assertEqual(row.title, "9001 → 81 · 已映射")
        self.assertEqual(action.title, "停止端口转发")

        state["forward_states"] = ()   # 会话掉线（状态翻转）
        mb.refresh_titles()
        self.assertIs(mb.refs[("fw_row", "t-2", 0)], row)  # 同一行对象
        self.assertEqual(row.title, "9001 → 81 · 未连接")
        self.assertEqual(action.title, "启动端口转发")

    def test_mount_rows_refresh_without_rebuild(self):
        state = {"mount_states": (MountState("t-1", "Aws-eu", "data",
                                             "mounted", ""),)}
        mb = MenuBuilder(MagicMock(), lambda: _state(
            config={"servers": []}, **state))
        mb._build_mount_submenu()
        row = mb.refs[("mount_row", "t-1", "data")]
        action = mb.refs[("mount_action", "t-1", "data")]
        self.assertIn("· 已挂载", row.title)
        self.assertEqual(action.title, "卸载")

        state["mount_states"] = (MountState("t-1", "Aws-eu", "data",
                                            "error", "远程无导出"),)
        mb.refresh_titles()
        self.assertIs(mb.refs[("mount_row", "t-1", "data")], row)
        self.assertIn("异常：远程无导出", row.title)
        self.assertEqual(action.title, "挂载")


class TestIconInfrastructure(unittest.TestCase):
    """SF Symbols 图标基建：正常返回图像，异常静默降级不抛。"""

    def test_unknown_symbol_returns_none(self):
        from shellui import menu_builder
        self.assertIsNone(menu_builder._symbol_image("definitely-not-a-symbol"))

    def test_apply_icon_tolerates_none_item_and_bad_key(self):
        from shellui import menu_builder
        menu_builder._apply_icon(None, "proxy_menu")  # 不抛即过
        menu_builder._apply_icon(rumps.MenuItem("x", callback=None),
                                 "no-such-key")

    def test_tinted_symbol_must_not_be_template(self):
        """SF Symbol 默认 template=True——NSMenuItem 按 menu 字色单色
        渲染 template 图像、tint 被无视（圆点全黑根因，真机实锄）。
        带 tint 必须 setTemplate_(False)；无 tint 保持 template 随 menu
        文字色明暗自适应。"""
        from AppKit import NSColor
        from shellui import menu_builder
        tinted = menu_builder._symbol_image(
            "circle.fill", point_size=8, color=NSColor.systemGreenColor())
        if tinted is not None:  # 无 AppKit 符号环境（CI）静默跳过
            self.assertFalse(tinted.isTemplate())
        plain = menu_builder._symbol_image("circle.fill", point_size=8)
        if plain is not None:
            self.assertTrue(plain.isTemplate())

    def test_status_dot_image_template_semantics(self):
        """手绘状态圆点（着色唯一可靠通道）：idle=template（随菜单文字
        色明暗自适应），彩色档=非模板（固定色渲染）。"""
        from shellui import menu_builder
        idle = menu_builder._status_dot_image("idle", 8)
        if idle is not None:
            self.assertTrue(idle.isTemplate())
        ok = menu_builder._status_dot_image("ok", 8)
        if ok is not None:
            self.assertFalse(ok.isTemplate())

    def test_status_color_kinds(self):
        from shellui import menu_builder
        for kind in ("ok", "warn", "err", "idle"):
            self.assertIsNotNone(menu_builder._status_color(kind))


class TestIsProxyServer(unittest.TestCase):
    """代理角色判定（v2）：proxy_server_id 唯一真相，缺省/悬空回退首条
    （与 merge 同语义，菜单只消费不重定义）。"""

    def test_id_truth_marks_server(self):
        cfg = {"proxy_server_id": "t-b",
               "servers": [{"id": "t-a"}, {"id": "t-b"}]}
        self.assertFalse(is_proxy_server(cfg, cfg["servers"][0]))
        self.assertTrue(is_proxy_server(cfg, cfg["servers"][1]))

    def test_dangling_id_marks_nothing(self):
        # 悬空 id 只可能出现在未经 merge 的手编配置里——merge 会把
        # proxy_server_id 重写为解析后的有效值，菜单只消费 merge 后配置；
        # 此处钉住原始判定不猜归属（不静默回退标错行）。
        cfg = {"proxy_server_id": "gone",
               "servers": [{"id": "t-a"}, {"id": "t-b"}]}
        self.assertFalse(is_proxy_server(cfg, cfg["servers"][0]))
        self.assertFalse(is_proxy_server(cfg, cfg["servers"][1]))

    def test_absent_id_first_row_is_proxy(self):
        cfg = {"servers": [{"id": "t-a"}, {"id": "t-b"}]}
        self.assertTrue(is_proxy_server(cfg, cfg["servers"][0]))

    def test_malformed_config_is_safe(self):
        self.assertFalse(is_proxy_server(None, {"id": "t-a"}))
        self.assertFalse(is_proxy_server({}, {"id": "t-a"}))
        self.assertFalse(is_proxy_server({"servers": []}, {}))


class TestMountSubmenu(unittest.TestCase):
    """远程挂载（ADR-007）：mount_states 参与 struct_key + 子菜单构建。"""

    @staticmethod
    def _mounts():
        return (MountState("t-1", "Aws-eu", "data", "mounted", ""),
                MountState("t-1", "Aws-eu", "ws", "unmounted", ""))

    def _submenu(self, mount_states=(), cfg=None):
        app = MagicMock()
        with unittest.mock.patch("shellui.menu_builder.chromium_proxy.installed_apps",
                                 return_value=[]):
            mb = MenuBuilder(app, lambda: _state(
                config=cfg or {"servers": []}, mount_states=mount_states))
            parent = mb._build_mount_submenu()
        rows = [r for r in parent.values() if hasattr(r, "values")]
        titles = [r.title for r in parent.values() if hasattr(r, "title")]
        return parent, rows, titles

    def test_status_flip_does_not_rebuild_menu(self):
        """挂载态翻转就地刷新（unmounted→mounting 不再重建整树）。"""
        key_mounted = MenuBuilder(MagicMock(), lambda: _state(
            mount_states=self._mounts())).struct_key()
        key_shift = MenuBuilder(MagicMock(), lambda: _state(
            mount_states=(MountState("t-1", "Aws-eu", "data", "mounting", ""),
                          MountState("t-1", "Aws-eu", "ws", "unmounted", "")))).struct_key()
        self.assertEqual(key_mounted, key_shift)

    def test_identity_change_rebuilds_menu(self):
        key_idle = MenuBuilder(MagicMock(), lambda: _state(
            mount_states=())).struct_key()
        key_mounted = MenuBuilder(MagicMock(), lambda: _state(
            mount_states=self._mounts())).struct_key()
        self.assertNotEqual(key_idle, key_mounted)

    def test_mount_rows_and_actions(self):
        _, rows, titles = self._submenu(self._mounts())
        mounted = [r for r in rows if "Aws-eu · data" in r.title]
        unmounted = [r for r in rows if "Aws-eu · ws" in r.title]
        self.assertTrue(mounted and unmounted, titles)
        self.assertIn("· 已挂载", mounted[0].title)
        self.assertIn("· 未挂载", unmounted[0].title)
        items = [i.title for i in list(mounted[0].values())]
        self.assertIn("卸载", items)
        self.assertIn("打开挂载目录", items)
        items = [i.title for i in list(unmounted[0].values())]
        self.assertIn("挂载", items)

    def test_transient_mount_tails_keep_ellipsis(self):
        """进行态尾标保留省略号（挂载中…/卸载中…——用户能看出未完）。"""
        _, rows, _ = self._submenu(
            (MountState("t-1", "a", "data", "mounting", ""),))
        self.assertIn("挂载中…", rows[0].title)

    def test_all_empty_multi_tunnel_no_duplicate_rows(self):
        """rumps 以标题为键去重——多隧道全空态时各包装行内的
        「添加转发规则…」分属不同子菜单，顶层仅全局一行（不塌行）。"""
        cfg = {"proxy_server_id": "", "servers": [
            {"id": "t-1", "name": "a", "ssh": {"host": "h", "user": "u"},
             "services": {"ssh": {"forwards": []}}},
            {"id": "t-2", "name": "b", "ssh": {"host": "h", "user": "u"},
             "services": {"ssh": {"forwards": []}}},
        ]}
        app = MagicMock()
        mb = MenuBuilder(app, lambda: _state(config=cfg))
        parent = mb._build_forward_submenu()
        top = [r.title for r in parent.values() if hasattr(r, "title")]
        self.assertEqual(top.count("添加转发规则…"), 1)
        for wrapper in [r for r in parent.values() if hasattr(r, "values")]:
            inner = [i.title for i in wrapper.values()
                     if hasattr(i, "title")]
            self.assertLessEqual(inner.count("添加转发规则…"), 1)

    def test_error_detail_folded_into_row_title(self):
        states = (MountState("t-1", "srv", "data", "error", "挂载失败：权限不足"),)
        _, rows, titles = self._submenu(states)
        err = [r for r in rows if "srv · data" in r.title]
        self.assertIn("异常：挂载失败：权限不足", err[0].title)

    def test_empty_state_hint_deep_links(self):
        app = MagicMock()
        mb = MenuBuilder(app, lambda: _state(config={"servers": []}))
        parent = mb._build_mount_submenu()
        titles = [r.title for r in parent.values() if hasattr(r, "title")]
        self.assertIn("配置挂载…", titles)
        row = [r for r in parent.values()
               if hasattr(r, "title") and r.title == "配置挂载…"][0]
        self.assertIs(row.callback, app.show_prefs_mounts)


class TestForwardRowPerItemToggle(unittest.TestCase):
    """逐条启停：转发行独立成行 + 状态尾标 + 回调接线。"""

    @staticmethod
    def _cfg():
        return {"proxy_server_id": "t-1", "servers": [
            {"id": "t-1", "name": "Aws-eu",
             "ssh": {"host": "a"},
             "services": {"ssh": {"forwards": [
                 {"local_port": 9000, "remote_host": "h",
                  "remote_port": 80}]}}},
            {"id": "t-2", "name": "AWS-ap",
             "ssh": {"host": "b"},
             "services": {"ssh": {"forwards": [
                 {"local_port": 9001, "remote_host": "h",
                  "remote_port": 81}]}}},
        ]}

    @staticmethod
    def _flat_titles(parent):
        """顶层 + 隧道行下一层的全部标题（转发行在隧道行子级）。"""
        out = []
        for i in parent.values():
            if not hasattr(i, "title"):
                continue
            out.append(i.title)
            out += [j.title for j in i.values() if hasattr(j, "title")]
        return out

    def _fw_menu(self, forwards=None, cfg=None, ssh_status="connected",
                 forward_states=None):
        cfg = cfg or self._cfg()
        if forwards is not None:
            cfg["servers"][1]["services"]["ssh"]["forwards"] = forwards
        if forward_states is None and ssh_status == "connected":
            forward_states = (ForwardState("t-2", "AWS-ap", "connected"),)
        mb = MenuBuilder(MagicMock(), lambda: _state(
            ssh_status=ssh_status, config=cfg,
            forward_states=forward_states or ()))
        return mb._build_forward_submenu()

    def test_disabled_row_shows_tail(self):
        parent = self._fw_menu([
            {"local_port": 9001, "remote_host": "127.0.0.1",
             "remote_port": 81},
            {"local_port": 9002, "remote_host": "127.0.0.1",
             "remote_port": 82, "enabled": False}])
        titles = self._flat_titles(parent)
        self.assertIn("9001 → 81 · 已映射", titles)
        self.assertIn("9002 → 82 · 已停用", titles)

    def test_proxy_tunnel_forwards_get_rows(self):
        """ADR-011 修订：代理服务器的转发行与会话统一口径——有独立
        会话（forward_states 含它）时行亮绿、动作行随会话。"""
        cfg = self._cfg()
        cfg["servers"][0]["services"]["ssh"]["forwards"] = [
            {"local_port": 7001, "remote_port": 71}]
        parent = self._fw_menu(
            cfg=cfg,
            forward_states=(ForwardState("t-1", "Aws-eu", "connected"),
                            ForwardState("t-2", "AWS-ap", "connected")))
        proxy_rows = [i for i in parent.values()
                      if hasattr(i, "title") and i.title.startswith("Aws-eu")]
        sub_titles = [i.title for i in list(proxy_rows[0].values())
                      if hasattr(i, "title")]
        self.assertIn("7001 → 71 · 已映射", sub_titles)
        self.assertIn("停止端口转发", sub_titles)   # 与非代理行同结构

    def test_forward_row_pending_when_session_down(self):
        parent = self._fw_menu(ssh_status="stopped", forward_states=())
        titles = self._flat_titles(parent)
        # 会话未跑：启用行"未连接"（守卫语义——未运行绝不拉起；UX 批次
        # 弃内部术语"待会话"）
        self.assertIn("9001 → 81 · 未连接", titles)
