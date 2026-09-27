"""Tests for tunnel/ssh_session — SSH 会话 deep module（ADR-007 收敛）。

钉住的契约：三件套接线（identity/spawn 两个 fns 的分工）、连接序列、
僵尸重建、stop、每秒健康泵 tick（到期重连 + check_and_recover）。
HostKeyFlow 与 SSHMonitor 打桩——真实组装由两个 coordinator 的既有
测试覆盖（test_connection_coordinator / test_mount_coordinator）。
"""
import unittest
from unittest.mock import MagicMock, patch

from tunnel import ssh_session
from tunnel.ssh_session import SshSession, check_and_recover


def _session(identity_fn=None, spawn_fn=None, probe_port_fn=lambda: None,
             password_fn=None):
    holder = {"identity": identity_fn if identity_fn is not None
              else lambda: {"id": "t-1", "name": "srv", "forwards": [
                  {"local_port": 9000, "remote_host": "127.0.0.1",
                   "remote_port": 80}]},
              "spawn": spawn_fn}
    with patch.object(ssh_session, "SSHMonitor") as monitor_cls, \
            patch.object(ssh_session, "HostKeyFlow"):
        monitor_cls.return_value = MagicMock(status="stopped")
        s = SshSession(
            log_sink=lambda line: None,
            identity_fn=lambda: holder["identity"](),
            password_fn=password_fn or (lambda t: "pw"),
            spawn_fn=spawn_fn,
            probe_port_fn=probe_port_fn)
    return s


class TestWiring(unittest.TestCase):
    def test_trio_assembled_once_per_session(self):
        s = _session()
        self.assertIsNotNone(s.monitor)
        self.assertIsNotNone(s.retry)
        self.assertIsNotNone(s.host_key)

    def test_identity_fn_feeds_hostkey_spawn_defaults_to_identity(self):
        # 未传 spawn_fn：spawn 即 identity（转发会话形态）
        s = _session()
        self.assertIs(s._spawn_fn, s._identity_fn)

    def test_spawn_fn_distinct_from_identity(self):
        # NFS 形态：spawn 是注入副本，identity 是原始隧道
        injected = {"id": "t-1", "forwards": [{"local_port": 12049}]}
        s = _session(spawn_fn=lambda: injected)
        self.assertIsNot(s._spawn_fn, s._identity_fn)
        self.assertEqual(s._spawn_fn()["forwards"][0]["local_port"], 12049)


class TestLifecycle(unittest.TestCase):
    def test_connect_resets_retry_then_hostkey_check(self):
        s = _session()
        with patch.object(s.retry, "cancel") as cancel, \
             patch.object(s.host_key, "start_check") as check:
            s.connect()
        cancel.assert_called_once()
        check.assert_called_once()

    def test_start_now_uses_spawn_for_monitor_identity_for_password(self):
        injected = {"id": "t-1", "forwards": [{"local_port": 12049}]}
        original = {"id": "t-1", "name": "srv", "forwards": []}
        passwords = []
        s = _session(identity_fn=lambda: original, spawn_fn=lambda: injected,
                     password_fn=lambda t: passwords.append(t) or "sekrit")
        with patch.object(s.monitor, "start") as start:
            s._start_now()
        start.assert_called_once_with(injected, None, "sekrit")
        self.assertEqual(passwords, [original])  # 凭据按原始隧道解析

    def test_start_now_no_tunnel_is_noop(self):
        s = _session(identity_fn=lambda: None, spawn_fn=lambda: None)
        with patch.object(s.monitor, "start") as start:
            s._start_now()
        start.assert_not_called()

    def test_reconnect_now_zombie_rebuild(self):
        s = _session()
        s.monitor.status = "connected"
        with patch.object(s.monitor, "stop") as stop, \
             patch.object(s.host_key, "start_check") as check:
            s.reconnect_now()
        stop.assert_called_once()
        check.assert_called_once()

    def test_reconnect_now_connecting_is_noop(self):
        s = _session()
        s.monitor.status = "connecting"
        with patch.object(s.monitor, "stop") as stop, \
             patch.object(s.host_key, "start_check") as check:
            s.reconnect_now()
        stop.assert_not_called()
        check.assert_not_called()

    def test_stop_tears_down_trio(self):
        s = _session()
        with patch.object(s.retry, "cancel") as rc, \
             patch.object(s.host_key, "cancel") as hc, \
             patch.object(s.monitor, "stop") as ms:
            s.stop(blocking=False)
        rc.assert_called_once()
        hc.assert_called_once()
        ms.assert_called_once_with(blocking=False)


class TestProbePort(unittest.TestCase):
    """probe_port 恒经显式注入的 fn（隐式默认推导已删——曾是生产死路）。"""

    def test_explicit_fn_wins(self):
        s = _session(probe_port_fn=lambda: 13000)
        self.assertEqual(s.probe_port, 13000)

    def test_first_forward_port_derivation_single_home(self):
        from shared.server_shape import first_forward_port
        self.assertEqual(first_forward_port(
            {"services": {"ssh": {"forwards": [
                {"local_port": 9000, "remote_host": "h", "remote_port": 80},
                {"local_port": True}]}}}), 9000)
        self.assertIsNone(first_forward_port(
            {"services": {"ssh": {"forwards": []}}}))
        self.assertIsNone(first_forward_port(None))
        # 防御分支：非法端口行跳过
        self.assertIsNone(first_forward_port(
            {"services": {"ssh": {"forwards": [
                {"local_port": 70000}]}}}))


class TestTick(unittest.TestCase):
    def test_due_retry_reconnects_stopped_session(self):
        s = _session()
        s.monitor.status = "stopped"
        s.retry._due = True
        with patch.object(s.host_key, "start_check") as check:
            s.tick()
        check.assert_called_once()

    def test_due_retry_skipped_when_connected(self):
        # connected 时到期标志被吃掉但不重连（不拆活连接）
        s = _session()
        s.monitor.status = "connected"
        s.retry._due = True
        with patch.object(s.host_key, "start_check") as check:
            s.tick()
        check.assert_not_called()

    def test_tick_runs_health_check(self):
        s = _session()
        s.monitor.status = "connected"
        with patch.object(ssh_session, "check_and_recover") as health:
            s.tick()
        health.assert_called_once()
        args = health.call_args[0]
        self.assertIs(args[0], s.monitor)
        self.assertIs(args[1], s.retry)
        self.assertIs(args[2], s.host_key)


class TestCheckAndRecover(unittest.TestCase):
    def _monitor(self, status, changed=False):
        m = MagicMock(status=status)
        m.is_host_key_changed = changed
        return m

    def test_connected_resets_retry(self):
        m, r, h = self._monitor("connecting"), MagicMock(), MagicMock()
        m.check = lambda p: setattr(m, "status", "connected")
        check_and_recover(m, r, h, 1080)
        r.reset.assert_called_once()

    def test_error_schedules_retry(self):
        m = self._monitor("error")
        check_and_recover(m, MagicMock(), MagicMock(), 1080)
        # error ⇒ retry.handle_error（非 host-key 变更路径）

    def test_error_host_key_changed_routes_to_replacement(self):
        m = self._monitor("error", changed=True)
        r, h = MagicMock(), MagicMock(change_prompted=False)
        with patch.object(ssh_session.SSHMonitor, "check"):
            check_and_recover(m, r, h, 1080)
        h.begin_replacement.assert_called_once()
        r.handle_error.assert_not_called()

    def test_unknown_status_is_skipped(self):
        m = self._monitor("starting-unknown")
        check_and_recover(m, MagicMock(), MagicMock(), 1080)
        m.check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
