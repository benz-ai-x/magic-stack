"""Tests for MagicProxyApp menu callbacks — UI callback logic.

The callbacks live directly on MagicProxyApp.  Since MagicProxyApp extends
rumps.App (heavy ObjC setup), tests create the instance via __new__
(skipping __init__) and hand-set attributes.  This keeps internal method
dispatch (self._dirty, self.reconnect, etc.) working through real bound
methods.

Candidate-1 refactor: MagicProxyApp 直接持有 _suanpan / _capture_ctrl /
_sys_proxy；ServiceCoordinator 不再暴露这些子模块的直通属性。测试中把
_svc 退化成只负责 tick/sync_sleep/stop_all 的 MagicMock，子模块改成 App
的直属属性。
"""
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

import app
from app import MagicProxyApp


def _make_app(config=None):
    """Build a MagicProxyApp without rumps.App.__init__ for callback testing."""
    a = MagicProxyApp.__new__(MagicProxyApp)
    a._conn = MagicMock()
    a._mounts = MagicMock()
    a._lifecycle = MagicMock()
    a._suanpan = MagicMock()
    a._capture_ctrl = MagicMock()
    a._sys_proxy = MagicMock()
    a._config = config if config is not None else {
        "http_listen_port": 8888,
        "capture_port": 8080,
        "capture_dir": "~/captures",
        "proxy_server_id": "",
    }
    a._menu_builder = MagicMock()
    a.VERSION_DISPLAY = "0.4.2"
    a._log_path = "/tmp/log.txt"
    a._log_buffer = MagicMock()
    # #46：菜单开关写径走真事务 store（conftest 会话沙箱重定向 PATHS，
    # 不会触碰真实配置文件）
    from mpconf.config_state import ConfigStateStore
    a._config_store = ConfigStateStore(keychain=None)
    # ADR-009：配置服务持有者（_sync_config_server 读；_lifecycle 已是
    # MagicMock，收敛调用被吸收）
    a._config_window_open = False
    a._copy_api_latch = False
    # 用户意图单一归宿（R5 候选 1）：同步执行器保断言确定性；notify/
    # update_mp 经 lambda 晚绑定（事后 patch.object(a, "_notify") 仍可
    # 拦截）；reload_config 直传绑定方法（ToggleForward 断言调用身份）
    from services.intents import UserIntents
    a._intents = UserIntents(
        conn=a._conn,
        mounts=a._mounts,
        notify=lambda s, m="": a._notify(s, m),
        mark_dirty=lambda: a._dirty(),
        update_mp=lambda mut: a._update_mp_config(mut),
        reload_config=a._reload_config_or_alert,
        spawn=lambda target, name=None: target(),
        capture_ctrl=a._capture_ctrl,
        get_capture_dir=lambda: a._config.get(
            "capture_dir", "~/captures"),
        alert=lambda message: app.rumps.alert(
            title="Magic Stack", message=message),
        hold_copy_latch=lambda: a._set_config_holders(copy_latch=True),
        get_agent_instructions=lambda: a._config_server.agent_instructions(),
    )
    return a


class TestConnectionActions(unittest.TestCase):
    def test_cancel_connection_delegates(self):
        a = _make_app()
        a.cancel_connection(None)
        a._conn.cancel.assert_called_once()

    def test_reconnect_restarts_and_dirties(self):
        a = _make_app()
        with patch.object(app, "load_config", return_value=None):
            a.reconnect(None)
        a._conn.restart.assert_called_once()
        self.assertIsNone(a._menu_builder.last_struct_key)

    def test_reconnect_merges_loaded_config(self):
        a = _make_app()
        # Make restart actually invoke the reload callback
        a._conn.restart.side_effect = lambda fn: fn()
        with patch.object(app, "load_config", return_value={"tunnels": []}), \
             patch.object(app, "merge_config", return_value={"merged": True}):
            a.reconnect(None)
        self.assertEqual(a._config, {"merged": True})

    def test_toggle_pause_syncs_proxy_and_sleep(self):
        a = _make_app()
        a._conn.ssh.status = "connected"
        a._conn.paused = False
        a.toggle_pause(None)
        a._conn.toggle_pause.assert_called_once()
        a._sys_proxy.sync.assert_called_once()
        a._lifecycle.sync_sleep.assert_called_once()

    def test_stop_proxy_tunnel_cancels_and_syncs(self):
        """菜单「停止代理」（原暂停改造）：取消隧道 + 后置同步面对齐
        toggle_pause（系统代理收敛 / 防睡眠重算）。"""
        a = _make_app()
        a._conn.ssh.status = "connected"
        a._conn.paused = False
        a.stop_proxy_tunnel(None)
        a._conn.cancel.assert_called_once()
        a._sys_proxy.sync.assert_called_once()
        a._lifecycle.sync_sleep.assert_called_once()

    def test_switch_mode_ssh_idle_click_connects(self):
        """接入段语义（2026-09-27 纠偏）：空闲态点「SSH 连接」= 发起连接
        ——接入段就是连接入口，不再无操作。"""
        a = _make_app()
        a._vpn_client = None
        a._conn.ssh.status = "stopped"
        with patch.object(app, "load_config", return_value=None):
            a.switch_mode_ssh(None)
        a._conn.restart.assert_called_once()

    def test_switch_mode_ssh_noop_when_connected(self):
        """单选语义：SSH 已连接时点「SSH 连接」无操作。"""
        a = _make_app()
        a._vpn_client = None
        a._conn.ssh.status = "connected"
        a.switch_mode_ssh(None)
        a._conn.restart.assert_not_called()

    def test_switch_mode_ssh_confirms_when_vpn_active(self):
        """VPN 活跃时点「SSH 连接」：原生确认 → 断 VPN → 恢复 SSH 会话
        与挂载（显式切换 = 主动恢复）。"""
        a = _make_app()
        a._vpn_client = MagicMock()
        a._vpn_client.vpn.status = "connected"
        a._intents = MagicMock()
        with patch("rumps.alert", return_value=True) as alert:
            a.switch_mode_ssh(None)
        args, kwargs = alert.call_args
        self.assertLessEqual(len(args), 2,
                             "rumps.alert 第 3 个位置参数即 ok——按钮文案"
                             "必须走关键字（真机 8da4753 TypeError 实锤）")
        self.assertIn("ok", kwargs)
        a._intents.vpn_disconnect.assert_called_once()
        a._conn.start.assert_called_once()
        a._conn.apply_autostarts.assert_called_once()
        a._mounts.apply_autostarts.assert_called_once()

    def test_toggle_vpn_ssh_active_confirm_alert_shape(self):
        """SSH 活跃时点「VPN 连接」：确认框形状回归——rumps.alert 的
        第 3 个位置参数就是 ok，3 位置 + ok= 关键字在真机必炸
        TypeError（点击无反应，2026-09-27 日志实锤）。"""
        a = _make_app({"servers": [
            {"id": "t-1", "name": "s1",
             "ssh": {"host": "h1", "port": 22, "auth_type": "key"},
             "services": {"openvpn": {"profile_set": True}}}]})
        a._vpn_client = None
        a._conn.any_connected = True
        a._mounts.mount_states.return_value = ()
        with patch("rumps.alert", return_value=False) as alert:
            a.toggle_vpn(None)          # 取消 → 不发起连接
        args, kwargs = alert.call_args
        self.assertLessEqual(len(args), 2)
        self.assertIn("ok", kwargs)
        a._conn.restart.assert_not_called()

    def test_toggle_system_proxy_delegates(self):
        a = _make_app()
        a.toggle_system_proxy(None)
        a._sys_proxy.toggle.assert_called_once()

    def test_switch_server_noop_when_current_and_connected(self):
        a = _make_app({"proxy_server_id": "t-a", "servers": [
            {"id": "t-a", "name": "t1",
             "ssh": {"host": "h1", "port": 22, "auth_type": "key"}}]})
        a._conn.ssh.status = "connected"
        a._conn.current_server = a._config["servers"][0]
        switch = a.make_switch_server("t-a")  # already current
        with patch.object(a, "_update_mp_config") as upd:
            switch(None)
        upd.assert_not_called()

    def test_switch_server_persists_and_reconnects(self):
        # #46：切换代理服务器经事务写径落盘（磁盘可见），再重连。写径
        # 写前读新——种子须先落沙箱磁盘，内存 _config 会被磁盘真相刷新
        servers = [
            {"id": "t-a", "name": "t1",
             "ssh": {"user": "u", "host": "h1", "port": 22,
                     "auth_type": "key"}},
            {"id": "t-b", "name": "t2",
             "ssh": {"user": "u", "host": "h2", "port": 22,
                     "auth_type": "key"}}]
        import json as _json_seed
        from shared import config_store as _cs
        with open(_cs.PATHS["mp"], "w") as f:
            _json_seed.dump({"proxy_server_id": "t-a",
                             "servers": servers}, f)
        a = _make_app({"proxy_server_id": "t-a", "servers": servers})
        a._conn.ssh.status = "stopped"
        switch = a.make_switch_server("t-b")
        switch(None)
        self.assertEqual(a._config["proxy_server_id"], "t-b")
        import json as _json
        from shared import config_store
        disk = _json.loads(open(config_store.PATHS["mp"]).read())
        self.assertEqual(disk.get("proxy_server_id"), "t-b")
        a._conn.restart.assert_called_once()


class TestSuanpanActions(unittest.TestCase):
    """toggle/reload/restart 现在内联在 app.py 里，直接调用
    SuanpanRuntime 的公开方法（running / start / stop / reload /
    listen_address / error）并组装通知文案。
    """

    def test_toggle_suanpan_running_notifies_started(self):
        a = _make_app()
        a._suanpan.running = False
        a._suanpan.start.return_value = True
        a._suanpan.listen_address.return_value = "127.0.0.1:9527"
        with patch.object(app.rumps, "notification") as notif:
            a.toggle_suanpan(None)
        notif.assert_called_once()
        self.assertIn("已启动", notif.call_args[0][1])
        a._menu_builder.build.assert_called_once()

    def test_toggle_suanpan_error_notifies_failure(self):
        a = _make_app()
        a._suanpan.running = False
        a._suanpan.start.return_value = False
        type(a._suanpan).error = PropertyMock(return_value="missing deps")
        with patch.object(app.rumps, "notification") as notif:
            a.toggle_suanpan(None)
        self.assertIn("启动失败", notif.call_args[0][1])

    def test_toggle_suanpan_stopped_notifies(self):
        a = _make_app()
        a._suanpan.running = True
        with patch.object(app.rumps, "notification") as notif:
            a.toggle_suanpan(None)
        self.assertIn("已停止", notif.call_args[0][1])

    def test_reload_suanpan_success(self):
        a = _make_app()
        a._suanpan.reload.return_value = True
        with patch.object(app.rumps, "notification") as notif:
            a.reload_suanpan(None)
        self.assertIn("已重载", notif.call_args[0][1])
        a._menu_builder.build.assert_called_once()

    def test_reload_suanpan_failure(self):
        a = _make_app()
        a._suanpan.reload.return_value = False
        type(a._suanpan).error = PropertyMock(return_value="bad config")
        with patch.object(app.rumps, "notification") as notif:
            a.reload_suanpan(None)
        self.assertIn("重载失败", notif.call_args[0][1])

    def test_restart_suanpan_success_notifies_restarted(self):
        a = _make_app()
        a._suanpan.running = True
        a._suanpan.start.return_value = True
        a._suanpan.listen_address.return_value = "127.0.0.1:9527"
        with patch.object(app.rumps, "notification") as notif:
            a.restart_suanpan(None)
        notif.assert_called_once()
        self.assertIn("已重启", notif.call_args[0][1])
        a._menu_builder.build.assert_called_once()

    def test_restart_suanpan_failure_notifies(self):
        a = _make_app()
        a._suanpan.running = True
        a._suanpan.start.return_value = False
        type(a._suanpan).error = PropertyMock(return_value="missing deps")
        with patch.object(app.rumps, "notification") as notif:
            a.restart_suanpan(None)
        self.assertIn("重启失败", notif.call_args[0][1])

    def test_copy_suanpan_url_uses_pbcopy(self):
        a = _make_app()
        a._suanpan.listen_address.return_value = "127.0.0.1:9527"
        with patch.object(app.subprocess, "Popen") as popen, \
             patch.object(app.rumps, "notification"):
            a.copy_suanpan_url(None)
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0], ["pbcopy"])

    def test_copy_suanpan_example_missing_file(self):
        a = _make_app()
        with patch.object(app, "resource_path", return_value="/nope.yaml"), \
             patch.object(app.os.path, "exists", return_value=False), \
             patch.object(app.rumps, "notification") as notif:
            a.copy_suanpan_example(None)
        self.assertIn("文件未找到", notif.call_args[0][2])

    def test_copy_suanpan_example_reads_file(self):
        a = _make_app()
        m = unittest.mock.mock_open(read_data="listen: x")
        with patch.object(app, "resource_path", return_value="/ex.yaml"), \
             patch.object(app.os.path, "exists", return_value=True), \
             patch.object(app, "open", m), \
             patch.object(app.subprocess, "Popen"), \
             patch.object(app.rumps, "notification") as notif:
            a.copy_suanpan_example(None)
        self.assertIn("字节", notif.call_args[0][2])


class TestSleepLoginActions(unittest.TestCase):
    def test_toggle_prevent_sleep_flips_and_persists(self):
        a = _make_app({"prevent_sleep": False})
        a.toggle_prevent_sleep(None)
        self.assertTrue(a._config["prevent_sleep"])
        import json as _json
        from shared import config_store
        disk = _json.loads(open(config_store.PATHS["mp"]).read())
        self.assertIs(disk.get("prevent_sleep"), True)
        a._lifecycle.sync_sleep.assert_called_once()

    def test_toggle_launch_at_login_success(self):
        a = _make_app({"launch_at_login": False})
        with patch.object(app.login_item, "set_launch_at_login",
                          return_value=(True, "")), \
             patch.object(app.rumps, "notification") as notif:
            a.toggle_launch_at_login(None)
        self.assertTrue(a._config["launch_at_login"])
        import json as _json
        from shared import config_store
        disk = _json.loads(open(config_store.PATHS["mp"]).read())
        self.assertIs(disk.get("launch_at_login"), True)
        self.assertIn("登录启动：开", notif.call_args[0][1])

    def test_toggle_launch_at_login_failure_alerts(self):
        a = _make_app({"launch_at_login": False})
        with patch.object(app.login_item, "set_launch_at_login",
                          return_value=(False, "denied")), \
             patch.object(a, "_update_mp_config") as upd, \
             patch.object(app.rumps, "alert") as alert:
            a.toggle_launch_at_login(None)
        alert.assert_called_once()
        # On failure the flag is left unchanged and config not saved
        self.assertFalse(a._config["launch_at_login"])
        upd.assert_not_called()


class TestCaptureActions(unittest.TestCase):
    def test_toggle_capture_off_disables(self):
        a = _make_app()
        a._capture_ctrl.enabled = True
        a.toggle_capture(None)
        a._capture_ctrl.disable.assert_called_once()

    def test_toggle_capture_on_trusted_enables(self):
        a = _make_app()
        a._capture_ctrl.enabled = False
        a._capture_ctrl.enable.return_value = True
        with patch.object(app.port_check, "who_owns", return_value=None), \
             patch.object(app.ca_trust, "is_trusted", return_value=True):
            a.toggle_capture(None)
        a._capture_ctrl.enable.assert_called_once()

    def test_toggle_capture_port_occupied_declined(self):
        a = _make_app()
        a._capture_ctrl.enabled = False
        owner = MagicMock(name="proc", pid=99, cmd="some cmd")
        with patch.object(app.port_check, "who_owns", return_value=owner), \
             patch.object(app.rumps, "alert", return_value=0), \
             patch.object(app.ca_trust, "is_trusted") as trusted:
            a.toggle_capture(None)
        trusted.assert_not_called()

    def test_enable_capture_alerts_when_mitmdump_missing(self):
        a = _make_app()
        a._capture_ctrl.enable.return_value = False
        with patch.object(app.rumps, "alert") as alert:
            a._intents.set_capture(True)
        alert.assert_called_once()

    def test_open_capture_dir(self):
        a = _make_app()
        with patch("capture.capture_store.prepare", return_value="/tmp/cap"), \
             patch.object(app.subprocess, "Popen") as popen:
            a.open_capture_dir(None)
        popen.assert_called_once()

    def test_open_today_jsonl_existing(self):
        a = _make_app()
        with patch("capture.capture_store.prepare", return_value="/tmp/cap"), \
             patch.object(app.os.path, "exists", return_value=True), \
             patch.object(app.time, "strftime", return_value="2026-08-10"), \
             patch.object(app.subprocess, "Popen") as popen:
            a.open_today_jsonl(None)
        self.assertEqual(popen.call_args[0][0][1], "-t")


class TestMiscActions(unittest.TestCase):
    def test_open_log(self):
        a = _make_app()
        with patch.object(app.subprocess, "Popen") as popen:
            a.open_log(None)
        popen.assert_called_once()

    def test_show_log_window(self):
        a = _make_app()
        with patch.object(app, "show_log_window") as slw:
            a.show_log_window(None)
        slw.assert_called_once()

    def test_about_alerts_version(self):
        a = _make_app()
        a.VERSION_DISPLAY = "0.4.3.08102116"
        with patch.object(app.rumps, "alert") as alert:
            a.about(None)
        self.assertIn("0.4.3.08102116", alert.call_args[1]["message"])


class TestLaunchAppProxied(unittest.TestCase):
    def test_missing_app_path_alerts(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "app_path", return_value=None), \
             patch.object(app.rumps, "alert") as alert:
            a._launch_app_proxied({"name": "Chrome"})
        alert.assert_called_once()

    def test_launch_success_alerts(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "is_running", return_value=False), \
             patch.object(app.chromium_proxy, "launch", return_value=(True, "")), \
             patch.object(app.rumps, "alert") as alert:
            a._launch_app_proxied({"name": "Chrome", "path": "/App/Chrome.app"})
        alert.assert_called_once()

    def test_launch_failure_alerts(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "is_running", return_value=False), \
             patch.object(app.chromium_proxy, "launch", return_value=(False, "err")), \
             patch.object(app.rumps, "alert") as alert:
            a._launch_app_proxied({"name": "Chrome", "path": "/App/Chrome.app"})
        self.assertIn("启动失败", alert.call_args[1]["message"])

    def test_toggle_capture_untrusted_guide_trusted_enables(self):
        a = _make_app()
        a._capture_ctrl.enabled = False
        a._capture_ctrl.enable.return_value = True
        captured = {}

        def fake_guide(on_result=None):
            captured["cb"] = on_result

        with patch.object(app.port_check, "who_owns", return_value=None), \
             patch.object(app.ca_trust, "is_trusted", return_value=False), \
             patch.object(app.ca_trust, "show_ca_trust_guide", side_effect=fake_guide):
            a.toggle_capture(None)
        captured["cb"](True)
        a._capture_ctrl.enable.assert_called_once()

    def test_toggle_capture_untrusted_guide_declined_dirties(self):
        a = _make_app()
        a._capture_ctrl.enabled = False
        captured = {}

        def fake_guide(on_result=None):
            captured["cb"] = on_result

        with patch.object(app.port_check, "who_owns", return_value=None), \
             patch.object(app.ca_trust, "is_trusted", return_value=False), \
             patch.object(app.ca_trust, "show_ca_trust_guide", side_effect=fake_guide):
            a.toggle_capture(None)
        captured["cb"](False)
        self.assertIsNone(a._menu_builder.last_struct_key)


class TestOSErrorPaths(unittest.TestCase):
    def test_open_capture_dir_oserror_swallowed(self):
        a = _make_app()
        with patch("capture.capture_store.prepare",
                   side_effect=OSError("denied")):
            a.open_capture_dir(None)  # should not raise

    def test_open_today_jsonl_missing_file_opens_dir(self):
        a = _make_app()
        with patch("capture.capture_store.prepare", return_value="/tmp/cap"), \
             patch.object(app.os.path, "exists", return_value=False), \
             patch.object(app.time, "strftime", return_value="2026-08-10"), \
             patch.object(app.subprocess, "Popen") as popen:
            a.open_today_jsonl(None)
        # Falls back to opening the directory (no -t flag)
        self.assertNotIn("-t", popen.call_args[0][0])

    def test_open_today_jsonl_oserror_swallowed(self):
        a = _make_app()
        with patch.object(app.os, "makedirs", side_effect=OSError("denied")):
            a.open_today_jsonl(None)

    def test_open_log_oserror_swallowed(self):
        a = _make_app()
        with patch.object(app.subprocess, "Popen", side_effect=OSError("no open")):
            a.open_log(None)

    def test_show_log_window_exception_swallowed(self):
        a = _make_app()
        with patch.object(app, "show_log_window", side_effect=RuntimeError("boom")):
            a.show_log_window(None)


class TestLaunchProxiedRunningApp(unittest.TestCase):
    def test_make_launch_proxied_returns_callback(self):
        a = _make_app()
        with patch.object(a, "_launch_app_proxied") as launch:
            cb = a.make_launch_proxied({"name": "X"})
            cb(None)
        launch.assert_called_once_with({"name": "X"})

    def test_running_app_user_confirms_relaunch(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "is_running", return_value=True), \
             patch.object(app.rumps, "alert", return_value=1), \
             patch.object(app.chromium_proxy, "quit_app") as quit_app, \
             patch.object(app.chromium_proxy, "wait_until_stopped", return_value=True), \
             patch.object(app.chromium_proxy, "launch", return_value=(True, "")):
            a._launch_app_proxied({"name": "Chrome", "path": "/App/Chrome.app"})
        quit_app.assert_called_once()

    def test_running_app_user_cancels(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "is_running", return_value=True), \
             patch.object(app.rumps, "alert", return_value=0), \
             patch.object(app.chromium_proxy, "launch") as launch:
            a._launch_app_proxied({"name": "Chrome", "path": "/App/Chrome.app"})
        launch.assert_not_called()

    def test_running_app_quit_times_out(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "is_running", return_value=True), \
             patch.object(app.rumps, "alert", return_value=1), \
             patch.object(app.chromium_proxy, "quit_app"), \
             patch.object(app.chromium_proxy, "wait_until_stopped", return_value=False), \
             patch.object(app.chromium_proxy, "launch") as launch:
            a._launch_app_proxied({"name": "Chrome", "path": "/App/Chrome.app"})
        launch.assert_not_called()


class TestBridgeActions(unittest.TestCase):
    """设置窗 bridge 动作分发（reconnectProxy / openPath captureDir）。"""

    def test_reconnect_action_runs_reconnect_off_thread(self):
        a = _make_app()
        spawned = []
        a._intents._spawn = (lambda target, name=None:
                             spawned.append((target, name)))
        a._bridge_action({"type": "reconnectProxy"})
        self.assertEqual(len(spawned), 1)
        # 后台跑的是 intents 的重连核心（真线程纪律在 test_intents 钉住）
        self.assertEqual(spawned[0][0], a._intents._do_reconnect)

    def test_open_path_capture_dir_reuses_menu_handler(self):
        a = _make_app()
        with patch.object(a, "open_capture_dir") as ocd:
            a._bridge_action({"type": "openPath", "kind": "captureDir"})
        ocd.assert_called_once_with(None)

    def test_open_path_unknown_kind_ignored(self):
        a = _make_app()
        with patch.object(a, "open_capture_dir") as ocd:
            a._bridge_action({"type": "openPath", "kind": "/etc"})
        ocd.assert_not_called()

    def test_unknown_action_ignored(self):
        a = _make_app()
        a._bridge_action({"type": "bogus"})  # must not raise


class TestCheckPortsEdge(unittest.TestCase):
    def test_check_port_free_returns_true(self):
        a = _make_app()
        with patch.object(app.port_check, "who_owns", return_value=None):
            self.assertTrue(a._check_port(8888, "HTTP"))

    def test_check_port_occupied_kill_confirmed(self):
        a = _make_app()
        owner = MagicMock(name="proc", pid=42, cmd="cmd")
        with patch.object(app.port_check, "who_owns", return_value=owner), \
             patch.object(app.rumps, "alert", return_value=1), \
             patch.object(app.port_check, "kill", return_value=(True, "")):
            self.assertTrue(a._check_port(8888, "HTTP"))

    def test_check_port_kill_fails(self):
        a = _make_app()
        owner = MagicMock(name="proc", pid=42, cmd="cmd")
        with patch.object(app.port_check, "who_owns", return_value=owner), \
             patch.object(app.rumps, "alert", return_value=1), \
             patch.object(app.port_check, "kill", return_value=(False, "err")):
            self.assertFalse(a._check_port(8888, "HTTP"))

    def test_check_both_ports(self):
        a = _make_app()
        with patch.object(a, "_check_port", return_value=True) as cp:
            a.check_both_ports()
        self.assertEqual(cp.call_count, 2)

    def test_check_both_ports_bad_http_listen(self):
        a = _make_app({"http_listen": "no-port-here"})
        with patch.object(a, "_check_port", return_value=True) as cp:
            a.check_both_ports()
        # Only SOCKS5 is checked; HTTP port parse fails silently
        self.assertEqual(cp.call_count, 1)


if __name__ == "__main__":
    unittest.main()


class TestMpSavedConverge(unittest.TestCase):
    """UI 保存 MP 段后的内存副本收敛（_on_mp_saved）：重读磁盘刷新内存 +
    登录启动与菜单路径对齐（配置外副作用补注册/注销）。"""

    def _make_saved_app(self, current):
        a = _make_app(dict(current))
        return a

    def test_refreshes_in_memory_config_and_marks_menu_dirty(self):
        a = self._make_saved_app({"launch_at_login": False})
        disk = {"launch_at_login": False, "prevent_sleep": True}
        with patch.object(app, "load_config", return_value=disk), \
             patch.object(app, "merge_config", side_effect=lambda c: c), \
             patch.object(app.login_item, "set_launch_at_login") as reg:
            a._on_mp_saved()
        self.assertIs(a._config["prevent_sleep"], True)
        self.assertIsNone(a._menu_builder.last_struct_key)
        reg.assert_not_called()

    def test_launch_at_login_change_syncs_launch_agent(self):
        a = self._make_saved_app({"launch_at_login": False})
        with patch.object(app, "load_config",
                          return_value={"launch_at_login": True}), \
             patch.object(app, "merge_config", side_effect=lambda c: c), \
             patch.object(app.login_item, "set_launch_at_login",
                          return_value=(True, "")) as reg:
            a._on_mp_saved()
        reg.assert_called_once_with(True)

    def test_unchanged_launch_at_login_does_not_re_register(self):
        a = self._make_saved_app({"launch_at_login": True})
        with patch.object(app, "load_config",
                          return_value={"launch_at_login": True}), \
             patch.object(app, "merge_config", side_effect=lambda c: c), \
             patch.object(app.login_item, "set_launch_at_login") as reg:
            a._on_mp_saved()
        reg.assert_not_called()

    def test_identity_migration_error_keeps_old_config(self):
        from shared.identity import IdentityMigrationError
        a = self._make_saved_app({"prevent_sleep": False})
        with patch.object(app, "load_config",
                          side_effect=IdentityMigrationError("dup")), \
             patch.object(app.login_item, "set_launch_at_login") as reg:
            a._on_mp_saved()
        self.assertIs(a._config.get("prevent_sleep"), False)
        reg.assert_not_called()

    def test_missing_config_file_is_noop(self):
        a = self._make_saved_app({"prevent_sleep": False})
        with patch.object(app, "load_config", return_value=None):
            a._on_mp_saved()  # 不得抛
        self.assertIs(a._config.get("prevent_sleep"), False)


class TestSwitchServerStableRole(unittest.TestCase):
    """v2：菜单切换代理角色只写 proxy_server_id 单一真相——删除/调序
    不再让角色漂移（v1 下标投影随换轴退役）。"""

    def test_switch_server_persists_stable_id_role(self):
        servers = [
            {"name": "t1", "ssh": {"user": "u", "host": "h1", "port": 22,
                                   "auth_type": "key"}, "id": "t-a"},
            {"name": "t2", "ssh": {"user": "u", "host": "h2", "port": 22,
                                   "auth_type": "key"}, "id": "t-b"}]
        import json as _json_seed
        from shared import config_store as _cs
        with open(_cs.PATHS["mp"], "w") as f:
            _json_seed.dump({"proxy_server_id": "t-a",
                             "servers": servers}, f)
        a = _make_app({"proxy_server_id": "t-a", "servers": servers})
        a._conn.ssh.status = "stopped"
        a.make_switch_server("t-b")(None)
        self.assertEqual(a._config["proxy_server_id"], "t-b")
        import json as _json
        from shared import config_store
        disk = _json.loads(open(config_store.PATHS["mp"]).read())
        self.assertEqual(disk.get("proxy_server_id"), "t-b")
        self.assertNotIn("current_tunnel", disk)
        self.assertNotIn("current_tunnel_id", disk)
        a._conn.restart.assert_called_once()


class TestToggleForward(unittest.TestCase):
    """菜单「端口映射逐条启停」：翻转磁盘 enabled + 守卫重建。"""

    class _FakeThread:
        """同步执行 target——保断言确定性（daemon 真线程会竞态）。"""
        def __init__(self, target=None, args=(), name=None, daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    def _seed_and_app(self, forwards, tid="t-abc"):
        import json as _json
        from shared import config_store as _cs
        servers = [{"name": "fw", "id": tid,
                    "ssh": {"user": "u", "host": "h", "port": 22,
                            "auth_type": "key"},
                    "services": {"ssh": {"forwards": forwards}}}]
        with open(_cs.PATHS["mp"], "w") as f:
            _json.dump({"proxy_server_id": "", "servers": servers}, f)
        a = _make_app({"proxy_server_id": "", "servers": servers})
        return a, tid

    def test_flips_disk_enabled_and_rebuilds_running_session(self):
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_host": "127.0.0.1",
              "remote_port": 80, "enabled": True}])
        a._conn.proxy_server_id = "t-other"
        a.make_toggle_forward(tid, 0)(None)
        import json as _json
        from shared import config_store
        disk = _json.loads(open(config_store.PATHS["mp"]).read())
        # 磁盘翻转走事务写径；重建分发恒走守卫入口（guarded 默认 True，
        # 未连接绝不拉起的真值表见 coordinator 的 TestForwardGuards）
        self.assertIs(
            disk["servers"][0]["services"]["ssh"]["forwards"][0]["enabled"],
            False)
        a._conn.restart_forward_async.assert_called_once_with(
            tid, a._reload_config_or_alert,
            thread_name="ToggleForwardRebuild")
        a._conn.restart_forward.assert_not_called()

    def test_running_session_gets_rebuild(self):
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_port": 80}])
        a._conn.forward_connected.return_value = True
        a._conn.restart_forward_async.return_value = True
        a._conn.proxy_server_id = "t-other"
        a.make_toggle_forward(tid, 0)(None)
        a._conn.restart_forward_async.assert_called_once_with(
            tid, a._reload_config_or_alert,
            thread_name="ToggleForwardRebuild")

    def test_error_state_session_not_pulled_up(self):
        """review c-1：SSH 失败滞留的 error 态会话——翻转绝不拉起。
        app 只经守卫入口分发（guard 在 coordinator 内判定）；底层
        restart_forward 不被触达。"""
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_port": 80}])
        a._conn.proxy_server_id = "t-other"
        a.make_toggle_forward(tid, 0)(None)
        a._conn.restart_forward_async.assert_called_once_with(
            tid, a._reload_config_or_alert,
            thread_name="ToggleForwardRebuild")
        a._conn.restart_forward.assert_not_called()

    def test_proxy_connecting_not_pulled_up(self):
        """review c-1：connecting 不算在跑——守卫谓词 proxy_connected
        （仅 connected）归 coordinator，意图只消费。"""
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_port": 80}])
        a._conn.proxy_server_id = tid
        a._conn.proxy_connected = False
        a.make_toggle_forward(tid, 0)(None)
        a._conn.restart.assert_not_called()  # 代理未跑：只写配置

    def test_disabling_last_enabled_forward_says_stopped(self):
        """review c-3：全停用后 restart 实为收敛停止——通知如实。"""
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_port": 80}])
        a._conn.forward_connected.return_value = True
        a._conn.restart_forward_async.return_value = True
        a._conn.proxy_server_id = "t-other"
        with patch.object(a, "_notify") as notify:
            a.make_toggle_forward(tid, 0)(None)
        a._conn.restart_forward_async.assert_called_once()
        msg = notify.call_args[0][1]
        self.assertIn("已停止", msg)
        self.assertNotIn("重建中", msg)

    def test_proxy_tunnel_rebuild_only_when_connected(self):
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_port": 80}])
        a._conn.proxy_server_id = tid   # 该隧道就是代理隧道
        a._conn.proxy_connected = False
        a.make_toggle_forward(tid, 0)(None)
        a._conn.restart.assert_not_called()   # 代理未跑：只写配置
        a._conn.proxy_connected = True
        a.make_toggle_forward(tid, 0)(None)
        a._conn.restart.assert_called_once()  # 连接中：翻转即重建代理会话


class TestConfigHoldersAtomicity(unittest.TestCase):
    """ADR-009 持有者唯一写口（架构评审 R2-4）：变更即收敛——
    except 路径曾重置持有者不收敛，零持有者时 :9528 常驻。"""

    def test_open_window_failure_leaves_holder_reset_without_converge(self):
        a = _make_app()
        a._lifecycle.sync_config_server.return_value = False
        with patch.object(app.rumps, "alert"):
            a._open_config_window("")
        self.assertFalse(a._config_window_open)
        self.assertEqual(a._lifecycle.sync_config_server.call_count, 1)

    def test_open_window_exception_converges_down(self):
        """R2-4 修复点：show_config_window 抛异常（服务可能已在听）→
        清位必须收敛——孤儿监听结构性不可能。"""
        a = _make_app()
        a._lifecycle.sync_config_server.return_value = True
        with patch.object(app, "show_config_window",
                          side_effect=RuntimeError("boom")), \
             patch.object(app.rumps, "alert"):
            a._open_config_window("#quickstart")
        self.assertFalse(a._config_window_open)
        # 置位收敛 + 异常清位收敛 = 2 次
        self.assertEqual(a._lifecycle.sync_config_server.call_count, 2)

    def test_window_close_releases_via_write_port(self):
        a = _make_app()
        a._on_config_window_closed()
        self.assertFalse(a._config_window_open)
        a._lifecycle.sync_config_server.assert_called_once_with(False)

    def test_copy_instructions_latches_via_write_port(self):
        a = _make_app()
        a._config_server = MagicMock()
        a._config_server.agent_instructions.return_value = "curl ..."
        with patch.object(app.subprocess, "Popen"), \
             patch.object(a, "_notify"):
            a.copy_agent_instructions(None)
        self.assertTrue(a._copy_api_latch)
        a._lifecycle.sync_config_server.assert_called_once_with(True)
