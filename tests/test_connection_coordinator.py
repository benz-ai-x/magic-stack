"""Tests for connection_coordinator.py — ConnectionCoordinator lifecycle."""
import unittest
from unittest.mock import MagicMock, patch

from tunnel import connection_coordinator as cc
from tunnel.connection_coordinator import ConnectionCoordinator


def _make_config():
    return {
        "socks5_port": 1080,
        "http_listen_port": 8888,
        "proxy_server_id": "",
        "servers": [{"ssh": {"host": "test", "user": "user",
                             "port": 22, "auth_type": "key"}}],
    }


def _make_coordinator():
    stats = MagicMock()
    return ConnectionCoordinator(
        stats=stats,
        ssh_log_sink=lambda line: None,
        get_config=lambda: _make_config(),
        get_tunnel_password=lambda t: "",
    )


class TestInitialProperties(unittest.TestCase):
    def test_not_paused_on_init(self):
        conn = _make_coordinator()
        self.assertFalse(conn.paused)

    def test_proxy_not_running_on_init(self):
        conn = _make_coordinator()
        self.assertFalse(conn.proxy_running)

    def test_ssh_accessible(self):
        conn = _make_coordinator()
        self.assertIsNotNone(conn.ssh)

    def test_current_server(self):
        conn = _make_coordinator()
        self.assertIsNotNone(conn.current_server)
        self.assertEqual(conn.current_server["ssh"]["host"], "test")

    def test_socks5_port(self):
        conn = _make_coordinator()
        self.assertEqual(conn.socks5_port, 1080)


class TestTogglePause(unittest.TestCase):
    def test_pause_sets_paused_true(self):
        conn = _make_coordinator()
        conn._proxy_runtime._rt._thread = None  # running → False
        with patch.object(conn._proxy_runtime, "stop", return_value=True) as m:
            paused = conn.toggle_pause()
        self.assertTrue(paused)
        self.assertTrue(conn.paused)
        # #40: pausing must not join the worker on the menu thread.
        m.assert_called_once_with(timeout=0)

    def test_resume_clears_paused(self):
        conn = _make_coordinator()
        conn._paused = True
        conn._proxy_runtime._rt._thread = None
        with patch.object(conn._proxy_runtime, "start", return_value=True), \
             patch.object(conn._host_key, "start_check"):
            paused = conn.toggle_pause()
        self.assertFalse(paused)
        self.assertFalse(conn.paused)


class TestTickSplit(unittest.TestCase):
    def test_check_ssh_noop_when_paused(self):
        conn = _make_coordinator()
        conn._paused = True
        with patch.object(conn._ssh, "check") as mock_check:
            conn.check_ssh()
        mock_check.assert_not_called()

    def test_check_ssh_checks_when_connecting(self):
        conn = _make_coordinator()
        conn._ssh._status = "connecting"
        with patch.object(conn._ssh, "check") as mock_check:
            conn.check_ssh()
        mock_check.assert_called_once_with(1080)

    def test_handle_retry_calls_retry_connect(self):
        conn = _make_coordinator()
        conn._retry._due = True
        with patch.object(conn, "_retry_connect") as mock_connect:
            conn.handle_retry()
        mock_connect.assert_called_once()


class TestStopAll(unittest.TestCase):
    def test_stop_all_stops_everything(self):
        """全停面（退出路径专用）：-D + 全部转发会话 + 本地代理运行时。"""
        conn = _make_coordinator()
        with patch.object(conn._ssh, "stop") as mock_ssh_stop, \
             patch.object(conn._proxy_runtime, "stop") as mock_proxy_stop:
            conn.stop_all()
        mock_ssh_stop.assert_called_once_with(blocking=False)
        mock_proxy_stop.assert_called_once()
        self.assertFalse(conn.proxy_running)

    def test_stop_all_non_blocking(self):
        conn = _make_coordinator()
        with patch.object(conn._ssh, "stop") as mock_ssh_stop, \
             patch.object(conn._proxy_runtime, "stop") as mock_proxy_stop:
            conn.stop_all()
        mock_ssh_stop.assert_called_once_with(blocking=False)
        mock_proxy_stop.assert_called_once()
        self.assertFalse(conn.proxy_running)


class TestStart(unittest.TestCase):
    def test_start_launches_background_and_ssh(self):
        conn = _make_coordinator()
        conn._proxy_runtime._rt._thread = None
        with patch.object(conn, "_start_background") as bg, \
             patch.object(conn, "start_ssh") as ssh:
            conn.start()
        bg.assert_called_once()
        ssh.assert_called_once()

    def test_start_ssh_cancels_retry_and_checks_host_key(self):
        conn = _make_coordinator()
        with patch.object(conn._retry, "cancel") as cancel, \
             patch.object(conn._host_key, "start_check") as check:
            conn.start_ssh()
        cancel.assert_called_once()
        check.assert_called_once()
        self.assertFalse(conn.paused)


class TestCheckSshOutcomes(unittest.TestCase):
    def test_ignores_status_not_in_watchlist(self):
        conn = _make_coordinator()
        conn._ssh._status = "paused-like-unknown"  # 状态全集外 → 忽略
        with patch.object(conn._ssh, "check") as mock_check:
            conn.check_ssh()
        mock_check.assert_not_called()

    def test_error_status_keeps_scheduling_retry(self):
        """#85：error 态每拍继续调度重试（timer 存活时 handle_error 自去重）。"""
        conn = _make_coordinator()
        conn._ssh._status = "error"
        conn._ssh._error_msg = "connection timed out"
        with patch.object(conn._retry, "handle_error") as handle:
            conn.check_ssh()
            conn.check_ssh()  # 第二拍：仍要继续调度，不躺平
        self.assertEqual(handle.call_count, 2)

    def test_connected_resets_retry(self):
        conn = _make_coordinator()
        conn._ssh._status = "connecting"
        with patch.object(conn._ssh, "check"):
            # After check(), status becomes connected
            conn._ssh._status = "connected"
            with patch.object(conn._retry, "reset") as reset:
                conn.check_ssh()
        reset.assert_called_once()

    def test_error_with_host_key_change_begins_replacement(self):
        conn = _make_coordinator()
        conn._ssh._status = "connecting"
        conn._ssh._error_msg = "REMOTE HOST IDENTIFICATION HAS CHANGED"
        conn._host_key.change_prompted = False

        def fake_check(port):
            conn._ssh._status = "error"

        with patch.object(conn._ssh, "check", side_effect=fake_check), \
             patch.object(conn._host_key, "begin_replacement") as begin, \
             patch.object(conn._retry, "handle_error") as handle:
            conn.check_ssh()
        begin.assert_called_once()
        handle.assert_not_called()

    def test_error_without_host_key_change_schedules_retry(self):
        conn = _make_coordinator()
        conn._ssh._status = "connecting"
        conn._ssh._error_msg = "connection timed out"

        def fake_check(port):
            conn._ssh._status = "error"

        with patch.object(conn._ssh, "check", side_effect=fake_check), \
             patch.object(conn._retry, "handle_error") as handle:
            conn.check_ssh()
        handle.assert_called_once()


class TestHandleReconnectTrigger(unittest.TestCase):
    """#86：唤醒事件 → 立即重连；只做提前触发，不改状态机语义。"""

    def test_noop_when_paused(self):
        conn = _make_coordinator()
        conn._proxy_running = True
        conn._paused = True
        with patch.object(conn, "start_ssh") as start:
            conn.handle_reconnect_trigger()
        start.assert_not_called()

    def test_stopped_access_stays_stopped_but_forwards_rebuild(self):
        conn = _make_coordinator()
        conn._proxy_running = True
        session = MagicMock()
        conn._forward_sessions["t-forward"] = session
        with patch.object(conn._ssh, "stop"), \
             patch.object(conn._proxy_runtime, "stop"), \
             patch.object(conn, "start_ssh") as start:
            conn.stop_access()
            conn.handle_reconnect_trigger()
        start.assert_not_called()
        session.reconnect_now.assert_called_once()

    def test_connected_rebuilds_tunnel(self):
        """唤醒后 TCP 多为僵尸链路——connected 也要主动重建，不等判死。"""
        conn = _make_coordinator()
        conn._proxy_running = True
        conn._ssh._status = "connected"
        with patch.object(conn._ssh, "stop") as stop, \
             patch.object(conn, "start_ssh") as start:
            conn.handle_reconnect_trigger()
        stop.assert_called_once()
        start.assert_called_once()

    def test_stopped_starts_without_stop(self):
        conn = _make_coordinator()
        conn._proxy_running = True  # 接入仍运行，仅 SSH 子进程退出
        conn._ssh._status = "stopped"
        with patch.object(conn._ssh, "stop") as stop, \
             patch.object(conn, "start_ssh") as start:
            conn.handle_reconnect_trigger()
        stop.assert_not_called()
        start.assert_called_once()

    def test_error_starts_without_stop(self):
        conn = _make_coordinator()
        conn._proxy_running = True
        conn._ssh._status = "error"
        with patch.object(conn._ssh, "stop") as stop, \
             patch.object(conn, "start_ssh") as start:
            conn.handle_reconnect_trigger()
        stop.assert_not_called()
        start.assert_called_once()

    def test_connecting_leaves_existing_flow(self):
        """已有连接在途——让现有流程收敛，不杀重连。"""
        conn = _make_coordinator()
        conn._proxy_running = True
        conn._ssh._status = "connecting"
        with patch.object(conn._ssh, "stop") as stop, \
             patch.object(conn, "start_ssh") as start:
            conn.handle_reconnect_trigger()
        stop.assert_not_called()
        start.assert_not_called()


class TestRestart(unittest.TestCase):
    def test_restart_stops_reloads_and_restarts(self):
        conn = _make_coordinator()
        conn._proxy_runtime._rt._thread = None
        reload_fn = MagicMock()
        with patch.object(conn._retry, "cancel") as retry_cancel, \
             patch.object(conn._host_key, "cancel") as hk_cancel, \
             patch.object(conn._ssh, "stop") as ssh_stop, \
             patch.object(conn._proxy_runtime, "stop") as proxy_stop, \
             patch.object(conn, "_start_background") as bg, \
             patch.object(conn, "start_ssh") as start_ssh:
            conn.restart(reload_fn)
        retry_cancel.assert_called_once()
        hk_cancel.assert_called_once()
        ssh_stop.assert_called_once()
        proxy_stop.assert_called_once()
        reload_fn.assert_called_once()
        bg.assert_called_once()
        start_ssh.assert_called_once()


class TestRetryConnect(unittest.TestCase):
    def test_noop_when_already_connected(self):
        conn = _make_coordinator()
        conn._ssh._status = "connected"
        with patch.object(conn._ssh, "start") as start:
            conn._retry_connect()
        start.assert_not_called()

    def test_starts_ssh_when_stopped(self):
        conn = _make_coordinator()
        conn._ssh._status = "stopped"
        with patch.object(conn._ssh, "start") as start:
            conn._retry_connect()
        start.assert_called_once()

    def test_noop_when_paused(self):
        """#85：暂停即停止重连——due 触发也不得拉起 SSH。"""
        conn = _make_coordinator()
        conn._paused = True
        conn._ssh._status = "stopped"
        with patch.object(conn._ssh, "start") as start:
            conn._retry_connect()
        start.assert_not_called()


class TestStartBackground(unittest.TestCase):
    def test_skips_when_proxy_already_running(self):
        conn = _make_coordinator()
        with patch.object(type(conn._proxy_runtime), "running", True), \
             patch.object(conn._proxy_runtime, "start") as start:
            conn._start_background()
        start.assert_not_called()

    def test_starts_proxy_when_not_running(self):
        conn = _make_coordinator()
        with patch.object(type(conn._proxy_runtime), "running", False), \
             patch.object(conn._proxy_runtime, "start", return_value=True) as start:
            conn._start_background()
        start.assert_called_once()
        self.assertTrue(conn.proxy_running)


if __name__ == "__main__":
    unittest.main()


class TestThreadContract(unittest.TestCase):
    """#68：注释声称「ConnectionCoordinator owns its locking」——让它成真。
    daemon 线程 stop() 与主线程 tick check() 的无保护竞态曾以
    AttributeError 落在 rumps 定时器回调内。"""

    def test_concurrent_stop_and_check_serialized(self):
        """#68 竞态点直接钉在 SubprocessMonitor.process：无锁时 daemon
        stop() 置 None 与 check() 读 .process.poll() 形成 AttributeError
        窗口。经 coordinator 锁后 stop/check 串行化——process 在 check
        内永不半路变 None。用 MagicMock process 强制竞态字段非 None
        （pytest 无 NSRunLoop 时 _ssh.start 不跑、process 恒 None，纯
        压力测试打不中——复核证实 vacuous）。"""
        import threading
        conn = _make_coordinator()
        errors = []
        # 竞态字段非 None + 强制交错：poll 返回 None（仍 running）让
        # check 走到第二轮读；stop 置 None 的窗口被锁消除
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        conn._ssh.process = mock_proc
        conn._ssh._status = "connecting"  # check 的进入条件

        stop = threading.Event()

        def stopper():
            while not stop.is_set():
                try:
                    conn.stop_access()   # ADR-011 修订：-D 停止入口
                except Exception as exc:
                    errors.append(("stopper", exc))
                    return
                conn._ssh.process = mock_proc  # 复位供下一轮
                conn._ssh._status = "connecting"

        def checker():
            while not stop.is_set():
                try:
                    conn.check_ssh()
                except AttributeError as exc:
                    errors.append(("checker", exc))
                    return
                except Exception:
                    return  # 非竞态异常（poll 在 None 上等）直接见

        t1 = threading.Thread(target=stopper, daemon=True)
        t2 = threading.Thread(target=checker, daemon=True)
        t1.start()
        t2.start()
        threading.Timer(0.5, stop.set).start()
        t1.join(3)
        t2.join(3)
        self.assertEqual(errors, [],
                         f"并发 stop/check 抛出 AttributeError（#68）: {errors}")



# ── 多活转发会话（v0.9） ─────────────────────────────────
def _fwd(lp=9000, rp=8000):
    return {"local_port": lp, "remote_host": "127.0.0.1", "remote_port": rp}


def _multi_config():
    """两台服务器：t1 代理角色（有 forwards），t2 转发候选。"""
    return {
        "socks5_port": 1080,
        "http_listen_port": 8888,
        "proxy_server_id": "t-1",
        "servers": [
            {"id": "t-1", "name": "proxy",
             "ssh": {"host": "a", "user": "u", "port": 22,
                     "auth_type": "key"},
             "services": {"ssh": {"forwards": [_fwd()]}}},
            {"id": "t-2", "name": "fwd",
             "ssh": {"host": "b", "user": "u", "port": 22,
                     "auth_type": "key"},
             "services": {"ssh": {"forwards": [_fwd(lp=9001, rp=8001)]}}},
        ],
    }


def _mutable_coordinator(cfg):
    holder = {"cfg": cfg}

    def get_config():
        return holder["cfg"]
    conn = ConnectionCoordinator(
        stats=MagicMock(), ssh_log_sink=lambda line: None,
        get_config=get_config, get_tunnel_password=lambda t: "")
    return conn, holder


class TestForwardSessions(unittest.TestCase):
    def test_start_forward_guards(self):
        """拒绝只剩两条：服务器不存在 / 无启用中的转发规则（ADR-011
        修订：代理服务器不再被拒——它同样走独立会话）。"""
        conn, _ = _mutable_coordinator(_multi_config())
        ok, reason = conn.start_forward("t-nope")
        self.assertFalse(ok)
        self.assertTrue(reason)
        self.assertEqual(conn.forward_sessions(), [])
        # 摘掉 forwards 后拒绝（含代理服务器——没有规则就没有会话）
        cfg = _multi_config()
        cfg["servers"][0]["services"]["ssh"]["forwards"] = []
        cfg["servers"][1]["services"]["ssh"]["forwards"] = []
        conn2, _ = _mutable_coordinator(cfg)
        for tid in ("t-1", "t-2"):
            ok, reason = conn2.start_forward(tid)
            self.assertFalse(ok)
            self.assertIn("转发", reason)

    def test_start_forward_accepts_proxy_server(self):
        """ADR-011 修订：代理服务器自己的 forwards 走独立纯 -L 会话
        （-D 便车退役——停接入不再杀它的转发）。"""
        conn, _ = _mutable_coordinator(_multi_config())
        with patch("tunnel.ssh_session.SshSession.connect") as c:
            ok, reason = conn.start_forward("t-1")
        self.assertTrue(ok, reason)
        c.assert_called_once()
        self.assertIn("t-1", [tid for tid, _, _ in conn.forward_sessions()])

    def test_start_forward_creates_session_and_connects(self):
        conn, _ = _mutable_coordinator(_multi_config())
        with patch("tunnel.ssh_session.SshSession.connect") as c:
            ok, reason = conn.start_forward("t-2")
        self.assertTrue(ok, reason)
        c.assert_called_once()
        ids = [tid for tid, _, _ in conn.forward_sessions()]
        self.assertEqual(ids, ["t-2"])

    def test_stop_forward_idempotent(self):
        conn, _ = _mutable_coordinator(_multi_config())
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        session = conn._forward_sessions["t-2"]
        with patch.object(session, "stop") as s:
            conn.stop_forward("t-2")
            conn.stop_forward("t-2")  # 幂等
        s.assert_called_once()
        self.assertEqual(conn.forward_sessions(), [])

    def test_check_forwards_reconciles_deleted_tunnel(self):
        conn, holder = _mutable_coordinator(_multi_config())
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        session = conn._forward_sessions["t-2"]
        holder["cfg"] = {**_multi_config(),
                         "servers": _multi_config()["servers"][:1]}
        with patch.object(session, "stop") as s:
            conn.check_forwards()
        s.assert_called_once()
        self.assertEqual(conn.forward_sessions(), [])

    def test_check_forwards_reconciles_emptied_forwards(self):
        conn, holder = _mutable_coordinator(_multi_config())
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        session = conn._forward_sessions["t-2"]
        cfg = _multi_config()
        cfg["servers"][1]["services"]["ssh"]["forwards"] = []
        holder["cfg"] = cfg
        with patch.object(session, "stop") as s:
            conn.check_forwards()
        s.assert_called_once()

    def test_check_forwards_connected_resets_retry(self):
        conn, _ = _mutable_coordinator(_multi_config())
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        session = conn._forward_sessions["t-2"]
        session.monitor._status = "connected"
        with patch.object(session.monitor, "check") as chk, \
             patch.object(session.retry, "reset") as rst:
            conn.check_forwards()
        chk.assert_called_once_with(9001)  # 探测口 = 第一条 -L 本地端口
        rst.assert_called_once()

    def test_any_connected_aggregates_sessions(self):
        conn, _ = _mutable_coordinator(_multi_config())
        self.assertFalse(conn.any_connected)
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        session = conn._forward_sessions["t-2"]
        self.assertFalse(conn.any_connected)
        session.monitor._status = "connected"
        self.assertTrue(conn.any_connected)
        self.assertTrue(conn.any_forward_session_connected)


class TestRestartServiceLayerAutonomy(unittest.TestCase):
    """ADR-011 修订：restart 只重启 -D 接入；转发会话（服务层）自治——
    既有会话存活，autostart 的（含代理服务器）由 apply_autostarts 补启。"""

    def _restart_with(self, cfg, mutate=None):
        conn, holder = _mutable_coordinator(cfg)
        if mutate:
            mutate(holder)
        with patch.object(conn, "_start_background"), \
             patch.object(conn, "start_ssh"), \
             patch.object(conn._ssh, "stop"), \
             patch.object(conn._proxy_runtime, "stop"), \
             patch.object(conn._retry, "cancel"), \
             patch.object(conn._host_key, "cancel"):
            conn.restart(lambda: None)  # reload 由调用方测试体控制 holder
        return conn

    def test_forward_sessions_survive_access_restart(self):
        """接入重启不碰既有转发会话——服务层与接入层解耦的核心断言。"""
        conn, _ = _mutable_coordinator(_multi_config())
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        session = conn._forward_sessions["t-2"]
        with patch.object(session, "stop") as sstop, \
             patch.object(conn, "_start_background"), \
             patch.object(conn, "start_ssh"), \
             patch.object(conn._ssh, "stop"), \
             patch.object(conn._proxy_runtime, "stop"), \
             patch.object(conn._retry, "cancel"), \
             patch.object(conn._host_key, "cancel"):
            conn.restart(lambda: None)
        sstop.assert_not_called()
        self.assertIn("t-2",
                      [tid for tid, _, _ in conn.forward_sessions()])

    def test_old_proxy_autostarts_own_forward_session(self):
        """切走代理角色后，旧代理按 autostart 获得独立会话（原「降级」
        逻辑的自治化身影——由 apply_autostarts 统一收敛）。"""
        cfg = _multi_config()
        cfg["servers"][0]["services"]["ssh"]["autostart"] = True

        def switch_current(holder):
            holder["cfg"]["proxy_server_id"] = "t-2"
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn = self._restart_with(cfg, switch_current)
        self.assertIn("t-1",
                      [tid for tid, _, _ in conn.forward_sessions()])

    def test_same_proxy_no_autostart_no_session(self):
        conn = self._restart_with(_multi_config())
        self.assertEqual(conn.forward_sessions(), [])


class TestWakeTriggerForwards(unittest.TestCase):
    def test_wake_rebuilds_forward_sessions(self):
        conn, _ = _mutable_coordinator(_multi_config())
        conn._paused = True  # 暂停只豁免代理会话；转发会话仍要重建
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        session = conn._forward_sessions["t-2"]
        session.monitor._status = "connected"
        with patch.object(session.monitor, "stop") as mstop, \
             patch.object(session, "connect") as mconn:
            conn.handle_reconnect_trigger()
        mstop.assert_called_once()
        mconn.assert_called_once()

    def test_apply_autostarts_covers_proxy_and_running(self):
        """ADR-011 修订：代理服务器不再跳过 autostart（自己的转发也
        是服务层）；已在跑的不重复启动。"""
        cfg = _multi_config()
        cfg["servers"][0]["services"]["ssh"]["autostart"] = True   # 代理：同样补启
        cfg["servers"][1]["services"]["ssh"]["autostart"] = True
        conn, _ = _mutable_coordinator(cfg)
        with patch.object(conn, "start_forward") as sf:
            conn.apply_autostarts()
        sf.assert_any_call("t-1")
        sf.assert_any_call("t-2")
        # 已在跑的不再重复启动
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        with patch.object(conn, "start_forward") as sf2:
            conn.apply_autostarts()
        sf2.assert_called_once_with("t-1")

    def test_stop_access_keeps_forward_sessions(self):
        """接入层切换（ADR-011 修订）：stop_access 只停 -D 会话与本地
        代理运行时——转发会话不碰。"""
        conn, _ = _mutable_coordinator(_multi_config())
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        session = conn._forward_sessions["t-2"]
        with patch.object(conn._ssh, "stop") as ssh_stop, \
             patch.object(conn._proxy_runtime, "stop") as rt_stop, \
             patch.object(conn._retry, "cancel"), \
             patch.object(conn._host_key, "cancel"), \
             patch.object(session, "stop") as sstop:
            conn.stop_access()
        ssh_stop.assert_called_once_with(blocking=True)
        rt_stop.assert_called_once()
        sstop.assert_not_called()
        self.assertIn("t-2",
                      [tid for tid, _, _ in conn.forward_sessions()])

    def test_vpn_reconnect_rebuilds_forwards_only(self):
        """VPN 建立的服务层重建：转发会话僵尸重建，**绝不拉起 -D**
        （接入互斥——与 handle_reconnect_trigger 的根本差异）。"""
        conn, _ = _mutable_coordinator(_multi_config())
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        session = conn._forward_sessions["t-2"]
        session.monitor._status = "connected"
        with patch.object(session.monitor, "stop") as mstop, \
             patch.object(session, "connect") as mconn, \
             patch.object(conn, "start_ssh") as start_ssh:
            conn.reconnect_forwards_now()
        mstop.assert_called_once()
        mconn.assert_called_once()
        start_ssh.assert_not_called()


class TestRestartForwardExplicitSemantics(unittest.TestCase):
    """评审 Spec-A：显式重连对 error/stopped 态会话同样重建（死按钮修复）。"""

    def test_error_state_session_reconnects(self):
        conn, _ = _mutable_coordinator(_multi_config())
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        session = conn._forward_sessions["t-2"]
        session.monitor._status = "error"
        with patch.object(session, "stop"), \
             patch.object(session, "connect") as mconn:
            self.assertTrue(conn.restart_forward("t-2", lambda: None))
        mconn.assert_called_once()

    def test_tunnel_deleted_during_restart_removes_session(self):
        conn, holder = _mutable_coordinator(_multi_config())
        with patch("tunnel.ssh_session.SshSession.connect"):
            conn.start_forward("t-2")
        session = conn._forward_sessions["t-2"]

        def drop_t2():
            holder["cfg"] = {**_multi_config(),
                             "servers": _multi_config()["servers"][:1]}
        with patch.object(session, "stop"):
            self.assertTrue(conn.restart_forward("t-2", drop_t2))
        self.assertEqual(conn.forward_sessions(), [])

    def test_unknown_session_returns_false(self):
        conn, _ = _mutable_coordinator(_multi_config())
        self.assertFalse(conn.restart_forward("t-nope", lambda: None))


class TestCurrentServerResolution(unittest.TestCase):
    """代理角色解析（v2）：proxy_server_id 单一真相；悬空/缺省回退首条
    ——永不因删除/调序漂移到另一台服务器。"""

    def _conn(self, cfg):
        return ConnectionCoordinator(
            stats=MagicMock(),
            ssh_log_sink=lambda line: None,
            get_config=lambda: cfg,
            get_tunnel_password=lambda t: "",
        )

    def _servers(self):
        return [
            {"id": "t-a", "ssh": {"host": "a", "user": "u", "port": 22,
                                  "auth_type": "key"}},
            {"id": "t-b", "ssh": {"host": "b", "user": "u", "port": 22,
                                  "auth_type": "key"}},
        ]

    def test_id_truth_resolves(self):
        cfg = {"proxy_server_id": "t-b", "servers": self._servers()}
        self.assertEqual(self._conn(cfg).current_server["id"], "t-b")

    def test_dangling_id_falls_back_to_first(self):
        cfg = {"proxy_server_id": "t-gone", "servers": self._servers()}
        self.assertEqual(self._conn(cfg).current_server["id"], "t-a")

    def test_absent_id_resolves_to_first(self):
        cfg = {"proxy_server_id": "", "servers": self._servers()}
        self.assertEqual(self._conn(cfg).current_server["id"], "t-a")

    def test_empty_servers_yields_none(self):
        self.assertIsNone(self._conn({"servers": []}).current_server)


class TestEnabledForwardsGuards(unittest.TestCase):
    """逐条启停：全部停用的隧道等价于"无转发规则"。"""

    def _conn_cfg(self, servers):
        return ConnectionCoordinator(
            stats=MagicMock(),
            ssh_log_sink=lambda line: None,
            get_config=lambda: {"proxy_server_id": "t-px",
                                "servers": servers},
            get_tunnel_password=lambda t: "",
        )

    def test_start_forward_rejects_all_disabled(self):
        # 双服务器：t-px 为代理角色，t-1 才是纯转发服务器
        conn = self._conn_cfg([
            {"name": "px", "id": "t-px", "ssh": {"host": "p"}},
            {"name": "fw", "id": "t-1", "ssh": {"host": "h"},
             "services": {"ssh": {"forwards": [
                 {"local_port": 9000, "remote_port": 80,
                  "enabled": False}]}}}])
        ok, reason = conn.start_forward("t-1")
        self.assertFalse(ok)
        self.assertIn("启用", reason)

    def test_check_forwards_stops_session_when_all_disabled(self):
        cfg = {"proxy_server_id": "t-px", "servers": [
            {"name": "px", "id": "t-px", "ssh": {"host": "p"}},
            {"name": "fw", "id": "t-1", "ssh": {"host": "h"},
             "services": {"ssh": {"forwards": [
                 {"local_port": 9000, "remote_port": 80}]}}}]}
        conn = ConnectionCoordinator(
            stats=MagicMock(),
            ssh_log_sink=lambda line: None,
            get_config=lambda: cfg,
            get_tunnel_password=lambda t: "",
        )
        conn.start_forward("t-1")
        # 磁盘态翻成全停用后，check 应收敛停会话（同"forwards 清空"）
        cfg["servers"][1]["services"]["ssh"]["forwards"][0]["enabled"] = False
        conn.check_forwards()
        self.assertNotIn("t-1", conn._forward_sessions)


class TestForwardGuards(unittest.TestCase):
    """「未连接绝不拉起」守卫单一归宿（架构评审 C1）：forward_connected /
    proxy_connected / restart_forward_async(guarded) 的真值表。"""

    @staticmethod
    def _session(status):
        s = MagicMock()
        s.monitor.status = status
        s.monitor.current_name = "fw"
        return s

    def _conn_with(self, tid, status):
        conn, _ = _mutable_coordinator(_multi_config())
        conn._forward_sessions[tid] = self._session(status)
        return conn

    def test_forward_connected_truth_table(self):
        self.assertTrue(
            self._conn_with("t-2", "connected").forward_connected("t-2"))
        for status in ("error", "connecting", "stopped"):
            self.assertFalse(
                self._conn_with("t-2", status).forward_connected("t-2"),
                status)
        # 只认目标会话：别的会话在连不算
        self.assertFalse(
            self._conn_with("t-2", "connected").forward_connected("t-other"))

    def test_proxy_connected_only_when_connected(self):
        conn, _ = _mutable_coordinator(_multi_config())
        conn._ssh._status = "connecting"
        self.assertFalse(conn.proxy_connected)
        conn._ssh._status = "connected"
        self.assertTrue(conn.proxy_connected)

    def test_guarded_async_skips_disconnected(self):
        conn = self._conn_with("t-2", "error")
        with patch.object(cc, "threading") as thr:
            self.assertFalse(
                conn.restart_forward_async("t-2", lambda: None, guarded=True))
            thr.Thread.assert_not_called()

    def test_guarded_async_dispatches_connected(self):
        conn = self._conn_with("t-2", "connected")
        with patch.object(cc, "threading") as thr:
            self.assertTrue(conn.restart_forward_async(
                "t-2", lambda: None, thread_name="X"))
            thr.Thread.assert_called_once()
            kwargs = thr.Thread.call_args.kwargs
            self.assertEqual(kwargs["target"], conn.restart_forward)
            self.assertEqual(kwargs["args"][0], "t-2")
            self.assertTrue(callable(kwargs["args"][1]))  # reload_config_fn
            self.assertEqual(kwargs["name"], "X")
            self.assertTrue(kwargs["daemon"])

    def test_unguarded_dispatches_even_disconnected(self):
        # 显式重连语义（Spec-A）：会话存在即重建——error 态也是死按钮修复
        conn = self._conn_with("t-2", "error")
        with patch.object(cc, "threading") as thr:
            self.assertTrue(
                conn.restart_forward_async("t-2", lambda: None, guarded=False))
            thr.Thread.assert_called_once()
