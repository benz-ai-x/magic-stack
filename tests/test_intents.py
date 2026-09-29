"""UserIntents 直面单测（架构评审 R5 候选 1）。

意图的执行纪律在此钉住：guard 分派真值表 / 默认线程纪律（真 daemon
Thread）/ 通知文案 / 状态推导翻转。app.py 侧的 adapter 翻译（菜单闭包
与 _bridge_action）在 test_menu_actions 经 _make_app 的同步执行器组合
覆盖。
"""
import os
import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services import intents as intents_mod
from services.intents import UserIntents

_SYNC = lambda target, name=None: target()  # noqa: E731


def _intents(**over):
    """最小依赖装配：conn/mounts 为 MagicMock，通知与 dirty 收集进 list。"""
    conn = MagicMock()
    conn.proxy_server_id = "t-proxy"
    conn.proxy_connected = True
    conn.forward_sessions.return_value = []
    mounts = MagicMock()
    mounts.mount_states.return_value = []
    notes = []
    dirties = []
    deps = dict(
        conn=conn,
        mounts=mounts,
        notify=lambda s, m="": notes.append((s, m)),
        mark_dirty=lambda: dirties.append(1),
        update_mp=lambda mut: True,
        reload_config=MagicMock(),
        spawn=_SYNC,
    )
    deps.update(over)
    return UserIntents(**deps), conn, mounts, notes, dirties


class TestStopProxyPostcondition(unittest.TestCase):
    """R8-C6：桥接停接入的后置收敛——与菜单 stop_proxy_tunnel 同一
    不变式，停完即跑（此前零后置、靠每秒 tick 治愈）。"""

    def test_stop_proxy_runs_access_tail_after_stop(self):
        tail = []
        intents, conn, _m, _n, _d = _intents(
            on_access_stopped=lambda: tail.append("ran"))
        intents.stop_proxy()
        conn.stop_access.assert_called_once()
        self.assertEqual(tail, ["ran"])       # 同步执行器下停完即跑

    def test_stop_proxy_tail_failure_does_not_break_stop(self):
        def _boom():
            raise RuntimeError("tail boom")
        intents, conn, _m, _n, _d = _intents(on_access_stopped=_boom)
        intents.stop_proxy()                  # 后置炸了也不外溢
        conn.stop_access.assert_called_once()

    def test_no_tail_injected_is_fine(self):
        intents, conn, _m, _n, _d = _intents()
        intents.stop_proxy()                  # 未注入（旧装配）照常停
        conn.stop_access.assert_called_once()


class TestReconnectDispatch(unittest.TestCase):
    """reconnect_proxy_or_forward 真值表：转发会话按 id / 代理按守卫。"""

    def test_forward_session_id_dispatches_guarded_rebuild(self):
        ui, conn, _, notes, dirties = _intents()
        ui.reconnect_proxy_or_forward("t-fw", guarded=True)
        conn.restart_forward_async.assert_called_once_with(
            "t-fw", ui._reload_config, guarded=True,
            thread_name="BridgeReconnectForward")
        self.assertEqual(dirties, [1])

    def test_proxy_server_id_still_targets_its_forward_session(self):
        ui, conn, _, _, _ = _intents()
        ui.reconnect_proxy_or_forward("t-proxy")
        conn.restart.assert_not_called()
        conn.restart_forward_async.assert_called_once_with(
            "t-proxy", ui._reload_config, guarded=False,
            thread_name="BridgeReconnectForward")

    def test_guarded_proxy_skip_when_not_connected(self):
        """保存流守卫：未连接的代理绝不因保存配置被拉起。"""
        ui, conn, _, _, dirties = _intents()
        conn.proxy_connected = False
        ui.reconnect_proxy_or_forward(guarded=True)
        conn.restart.assert_not_called()
        self.assertEqual(dirties, [])

    def test_unguarded_reconnects_even_when_not_connected(self):
        """显式重连（Spec-A）：未连接也允许重建。"""
        ui, conn, _, _, _ = _intents()
        conn.proxy_connected = False
        ui.reconnect_proxy_or_forward(guarded=False)
        conn.restart.assert_called_once()

    def test_reconnect_marks_dirty_after_restart(self):
        ui, conn, _, _, dirties = _intents()
        ui.reconnect()
        conn.restart.assert_called_once_with(ui._reload_config)
        self.assertEqual(dirties, [1])

    def test_default_spawn_is_daemon_thread(self):
        """默认线程纪律：慢操作走 daemon Thread（不卡菜单/窗口主线程）。"""
        ui, conn, _, _, _ = _intents(spawn=None)  # 缺省执行器
        ui._reload_config = reload_mock = MagicMock()
        with patch.object(intents_mod.threading, "Thread") as thread:
            thread.return_value.start = MagicMock()
            ui.reconnect()
        thread.assert_called_once()
        self.assertIs(thread.call_args[1].get("daemon"), True)
        # start() 被 stub —— 核心未真跑（接线断言，不起真线程）
        reload_mock.assert_not_called()
        thread.return_value.start.assert_called_once()


class TestAccessSwitching(unittest.TestCase):
    def test_vpn_blocks_ssh_reconnect_and_wake_only_rebuilds_services(self):
        ui, conn, mounts, _, _ = _intents(vpn_active=lambda: True)
        ui.reconnect()
        ui.wake()
        conn.restart.assert_not_called()
        conn.handle_reconnect_trigger.assert_not_called()
        conn.reconnect_forwards_now.assert_called_once()
        mounts.reconnect_now.assert_called_once()

    def test_ssh_wake_keeps_access_and_service_recovery(self):
        ui, conn, mounts, _, _ = _intents(vpn_active=lambda: False)
        ui.wake()
        conn.handle_reconnect_trigger.assert_called_once()
        mounts.reconnect_now.assert_called_once()

    def test_failed_vpn_stop_never_starts_ssh(self):
        disconnect = MagicMock()
        ui, conn, _, _, _ = _intents(
            vpn_disconnect=disconnect, vpn_active=lambda: True)
        ui.switch_to_ssh()
        disconnect.assert_called_once()
        conn.start.assert_not_called()

    def test_wake_waits_for_vpn_connect_and_checks_final_access(self):
        connecting, release, waking, woke = (threading.Event() for _ in range(4))
        workers, active = [], []

        def connect(server):
            connecting.set()
            if not release.wait(2):
                raise TimeoutError("test barrier timed out")
            active.append(True)

        def spawn(target, name):
            def run():
                if name == "WakeReconnect":
                    waking.set()
                target()
            worker = threading.Thread(target=run, name=name)
            workers.append(worker)
            worker.start()

        ui, conn, mounts, _, _ = _intents(
            spawn=spawn, vpn_connect=connect, vpn_active=lambda: bool(active))
        mounts.reconnect_now.side_effect = woke.set
        try:
            ui.vpn_connect({})
            self.assertTrue(connecting.wait(1))
            ui.wake()
            self.assertTrue(waking.wait(1))
            self.assertFalse(woke.wait(0.05))
        finally:
            release.set()
            for worker in workers:
                worker.join(2)
        self.assertTrue(woke.is_set())
        self.assertTrue(all(not w.is_alive() for w in workers))
        conn.handle_reconnect_trigger.assert_not_called()
        conn.reconnect_forwards_now.assert_called_once()


class TestForwardSession(unittest.TestCase):

    def test_start_spawns_and_notifies_on_failure(self):
        ui, conn, _, notes, dirties = _intents()
        spawned = []
        ui._spawn = lambda target, name=None: spawned.append(target)
        conn.start_forward.return_value = (False, "端口被占")
        ui.forward_session("t-fw", "start")
        self.assertEqual(len(spawned), 1)
        spawned[0]()  # 执行后台核心
        self.assertEqual(notes, [("无法启动端口转发", "端口被占")])
        self.assertEqual(dirties, [1])

    def test_stop_is_synchronous(self):
        ui, conn, _, _, dirties = _intents()
        ui.forward_session("t-fw", "stop")
        conn.stop_forward.assert_called_once_with("t-fw")
        conn.start_forward.assert_not_called()
        self.assertEqual(dirties, [1])

    def test_toggle_derives_from_running_sessions(self):
        ui, conn, _, _, _ = _intents()
        conn.forward_sessions.return_value = [
            SimpleNamespace(tunnel_id="t-fw")]
        with patch.object(ui, "forward_session") as fs:
            ui.toggle_forward_session("t-fw")
            fs.assert_called_once_with("t-fw", "stop")
        conn.forward_sessions.return_value = []
        with patch.object(ui, "forward_session") as fs:
            ui.toggle_forward_session("t-fw")
            fs.assert_called_once_with("t-fw", "start")


class TestMount(unittest.TestCase):

    def test_explicit_mount_and_unmount(self):
        ui, conn, mounts, _, dirties = _intents()
        ui.mount("t", "data", "mount")
        mounts.start_mount.assert_called_once_with("t", "data")
        ui.mount("t", "data", "unmount")
        mounts.stop_mount.assert_called_once_with("t", "data")
        self.assertEqual(dirties, [1, 1])

    def test_toggle_derives_from_mount_states(self):
        ui, _, mounts, _, _ = _intents()
        mounts.mount_states.return_value = [SimpleNamespace(
            tunnel_id="t", name="data", status="mounted")]
        with patch.object(ui, "mount") as m:
            ui.toggle_mount("t", "data")
            m.assert_called_once_with("t", "data", "unmount")
        mounts.mount_states.return_value = [SimpleNamespace(
            tunnel_id="t", name="data", status="error")]
        with patch.object(ui, "mount") as m:
            ui.toggle_mount("t", "data")
            m.assert_called_once_with("t", "data", "mount")


class TestSetCapture(unittest.TestCase):

    def test_disable_is_immediate(self):
        ctrl = MagicMock()
        ctrl.enabled = True
        ui, _, _, _, dirties = _intents(capture_ctrl=ctrl)
        ui.set_capture(False)
        ctrl.disable.assert_called_once()
        self.assertEqual(dirties, [1])

    def test_enable_failure_alerts(self):
        ctrl = MagicMock()
        ctrl.enable.return_value = False
        alerts = []
        ui, _, _, _, dirties = _intents(
            capture_ctrl=ctrl, alert=alerts.append)
        ui.set_capture(True)
        ctrl.enable.assert_called_once()
        self.assertEqual(len(alerts), 1)
        self.assertIn("mitmdump", alerts[0])
        self.assertEqual(dirties, [1])

    def test_enable_success_no_alert(self):
        ctrl = MagicMock()
        ctrl.enable.return_value = True
        alerts = []
        ui, _, _, _, _ = _intents(capture_ctrl=ctrl, alert=alerts.append)
        ui.set_capture(True)
        self.assertEqual(alerts, [])


class TestCopyAgentInstructions(unittest.TestCase):

    def test_latch_then_clipboard_then_notify(self):
        calls = []
        ui, _, _, notes, _ = _intents(
            hold_copy_latch=lambda: calls.append("latch"),
            get_agent_instructions=lambda: (calls.append("text"), "curl ...")[1],
        )
        with patch.object(intents_mod.subprocess, "run") as run:
            result = ui.copy_agent_instructions()
        # 先闩锁再取文本——次序钉死（curl 要立即可用，文本依赖服务在听）
        self.assertEqual(calls, ["latch", "text"])
        run.assert_called_once()
        self.assertEqual(result, {"ok": True})
        self.assertEqual(notes, [("已复制 AI 助手指令",
                                  "含 token 的 curl 已就绪；配置 API 已开启供助手访问")])

    def test_chinese_instructions_force_utf8_even_with_inherited_c_locale(self):
        text = "我在用 Magic Stack，请帮我配置。"
        ui, _, _, _, _ = _intents(
            hold_copy_latch=lambda: True, get_agent_instructions=lambda: text)
        with patch.dict(os.environ, {"LC_ALL": "C"}), \
                patch.object(intents_mod.subprocess, "run") as run:
            result = ui.copy_agent_instructions()
        self.assertTrue(result["ok"])
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/pbcopy"])
        self.assertEqual(kwargs["input"], text.encode("utf-8"))
        self.assertEqual(kwargs["env"]["LC_ALL"], "en_US.UTF-8")
        self.assertTrue(kwargs["check"])
        self.assertGreater(kwargs["timeout"], 0)

    def test_copy_failure_returns_error_without_success_notification(self):
        for error in (OSError("unavailable"),
                      subprocess.CalledProcessError(1, ["/usr/bin/pbcopy"]),
                      subprocess.TimeoutExpired(["/usr/bin/pbcopy"], 5)):
            with self.subTest(error=type(error).__name__):
                ui, _, _, notes, _ = _intents(
                    hold_copy_latch=lambda: True,
                    get_agent_instructions=lambda: "private instructions")
                with patch.object(intents_mod.subprocess, "run", side_effect=error):
                    result = ui.copy_agent_instructions()
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
                self.assertNotIn("private instructions", result["error"])
                self.assertNotEqual(notes[0][0], "已复制 AI 助手指令")

    def test_unavailable_config_api_does_not_copy_unusable_instructions(self):
        ui, _, _, _, _ = _intents(hold_copy_latch=lambda: False)
        with patch.object(intents_mod.subprocess, "run") as run:
            result = ui.copy_agent_instructions()
        run.assert_not_called()
        self.assertFalse(result["ok"])

    def test_notification_failure_does_not_hide_successful_copy(self):
        ui, _, _, _, _ = _intents(
            hold_copy_latch=lambda: True, get_agent_instructions=lambda: "text",
            notify=MagicMock(side_effect=RuntimeError("notifications unavailable")))
        with patch.object(intents_mod.subprocess, "run"):
            self.assertEqual(ui.copy_agent_instructions(), {"ok": True})


class TestToggleForwardRow(unittest.TestCase):
    """端口映射行启停写径：写径失败中止、守卫分派、如实文案。"""

    def _seed(self, tmpdir, enabled=True, tid="t-fw"):
        import json
        from shared import config_store as cs
        cs.PATHS["mp"] = str(tmpdir / "mp.json")
        servers = [{"name": "fw", "id": tid,
                    "ssh": {"user": "u", "host": "h", "port": 22,
                            "auth_type": "key"},
                    "services": {"ssh": {"forwards": [
                        {"local_port": 9000, "remote_port": 80,
                         "enabled": enabled}]}}}]
        with open(cs.PATHS["mp"], "w") as f:
            json.dump({"proxy_server_id": "", "servers": servers}, f)
        return tid

    def test_write_failure_aborts_without_notify(self):
        import tempfile, pathlib
        from unittest.mock import patch
        from shared import config_store as cs
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(cs.PATHS):
                tid = self._seed(pathlib.Path(d))
                ui, conn, _, notes, dirties = _intents(
                    update_mp=lambda mut: False)
                ui.toggle_forward_row(tid, 0)
                conn.restart_forward_async.assert_not_called()
                self.assertEqual(notes, [])
                self.assertEqual(dirties, [])

    def test_rebuild_note_says_stopped_when_none_enabled(self):
        import json as _json
        import tempfile, pathlib
        from unittest.mock import patch
        from shared import config_store as cs
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(cs.PATHS):
                tid = self._seed(pathlib.Path(d), enabled=True)
                # 真写盘桩：mutate 落盘——any_enabled 读的是写后磁盘真相
                def _real_update(mut):
                    cfg = _json.loads(open(cs.PATHS["mp"]).read())
                    with open(cs.PATHS["mp"], "w") as f:
                        _json.dump(mut(cfg), f)
                    return True
                ui, conn, _, notes, _ = _intents(update_mp=_real_update)
                conn.proxy_server_id = "t-proxy"
                conn.restart_forward_async.return_value = True
                ui.toggle_forward_row(tid, 0)
                conn.restart_forward_async.assert_called_once_with(
                    tid, ui._reload_config,
                    thread_name="ToggleForwardRebuild")
                self.assertIn("已停用", notes[0][0])
                self.assertIn("已停止（无启用中的转发）", notes[0][1])


if __name__ == "__main__":
    unittest.main()


class TestStopProxy(unittest.TestCase):
    def test_stop_proxy_stops_access_only(self):
        """关闭接入（ADR-011 修订）：只停 -D 会话（stop_access）——
        转发会话与 NFS 挂载是服务层，不陪葬、不卸载。"""
        intents, conn, mounts, _notes, dirties = _intents()
        intents.stop_proxy()
        self.assertTrue(dirties)                      # dirty 即时标记
        self.assertEqual(conn.stop_access.call_count, 1)
        self.assertEqual(conn.stop_all.call_count, 0)
        self.assertEqual(mounts.unmount_all.call_count, 0)

    def test_stop_proxy_spawns_background_when_async(self):
        ran = []
        intents, conn, mounts, _n, _d = _intents(spawn=lambda t, name: ran.append((t, name)))
        intents.stop_proxy()
        self.assertEqual(conn.stop_access.call_count, 0)    # 未直跑
        self.assertIn("StopProxy", ran[0][1])
        ran[0][0]()
        self.assertEqual(conn.stop_access.call_count, 1)
