"""Final coverage batch: chipping away at all remaining < 92% modules."""
import os
import time
import unittest
from unittest.mock import patch, MagicMock

# ── connection_coordinator.py: tick/check_ssh error paths ─────────
from tunnel.connection_coordinator import ConnectionCoordinator


class TestConnCoordTickPaths(unittest.TestCase):
    def _make(self):
        conn = ConnectionCoordinator(
            stats=MagicMock(), ssh_log_sink=lambda _: None,
            get_config=lambda: {"socks5_port": 1080, "http_listen_port": 8888,
                                 "tunnels": [{"ssh_host": "s"}]},
            get_tunnel_password=lambda t: "",
        )
        conn._ssh._status = "error"
        return conn

    def test_stop_access_calls_stop(self):
        """接入层停止入口（ADR-011 修订）：-D 会话 + 本地代理运行时。"""
        conn = self._make()
        conn._ssh._status = "connecting"
        with patch.object(conn._ssh, "stop"), \
             patch.object(conn._proxy_runtime, "stop"):
            conn.stop_access()
        self.assertFalse(conn.proxy_running)




# ── service_coordinator.py: tick/stop_all paths ───────────────────
from services.lifecycle_runtime import LifecycleRuntime


class TestSvcCoordPaths(unittest.TestCase):
    def _make(self):
        with patch("sysctl.sys_proxy_controller.system_proxy") as mock_sp:
            mock_sp.recover_stale_transaction.return_value = (False, "")
            return LifecycleRuntime(
                config_fn=lambda: {"prevent_sleep": False},
                ssh_monitor=MagicMock(),
                paused_fn=lambda: False,
                on_menu_dirty=lambda: None,
            )

    def test_tick_calls_capture_check(self):
        svc = self._make()
        with patch.object(type(svc._capture_ctrl), "enabled", True), \
             patch.object(svc._capture, "check") as mock_check, \
             patch.object(svc._sys_proxy, "sync"):
            svc.tick(8080)
        mock_check.assert_called_once_with(8080)

    def test_stop_all_releases_blocker(self):
        svc = self._make()
        svc._caffeinate_on = True
        with patch.object(svc._capture, "stop"), \
             patch.object(svc._blocker, "release") as mock_release:
            svc.stop_all()
        mock_release.assert_called_once()


# ── config.py: _migrate password + save error ──────────────────────
from mpconf import config
class TestConfigMigratePassword(unittest.TestCase):
    def test_migrate_preserves_server_config(self):
        old = {"tunnels": [{"ssh_host": "s", "auth_type": "password"}]}
        result = config._migrate(old)
        self.assertEqual(result["servers"][0]["ssh"]["host"], "s")
        self.assertEqual(result["servers"][0]["ssh"]["auth_type"], "password")


class TestConfigSaveOSError(unittest.TestCase):
    def test_save_write_failure_returns_false(self):
        with patch("tempfile.mkstemp", side_effect=OSError("no space")):
            self.assertFalse(config.save_config({}))


# ── keychain.py: OSError paths ─────────────────────────────────────
from shared import keychain
class TestKeychainOSError(unittest.TestCase):
    @patch("shared.keychain.Security.SecItemAdd", side_effect=OSError("keychain busy"))
    def test_set_password_oserror(self, _):
        result = keychain.set_password({"ssh_host": "h"}, "pw")
        self.assertFalse(result)

    @patch("shared.keychain.Security.SecItemCopyMatching",
           side_effect=OSError("keychain busy"))
    def test_get_password_oserror(self, _):
        result = keychain.get_password({"ssh_host": "h"})
        self.assertEqual(result, "")

    @patch("shared.keychain.Security.SecItemDelete", side_effect=OSError("keychain busy"))
    def test_delete_password_oserror(self, _):
        keychain.delete_password({"ssh_host": "h"})  # should not raise


# ── login_item.py: error paths ─────────────────────────────────────
from sysctl import login_item
class TestLoginItemErrors(unittest.TestCase):
    @patch("subprocess.run")
    def test_set_launch_at_login_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        ok, err = login_item.set_launch_at_login(True)
        self.assertFalse(ok)


# ── port_check.py: error paths ─────────────────────────────────────
from sysctl import port_check
class TestPortCheckEdges(unittest.TestCase):
    def test_who_owns_free_port(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            self.assertIsNone(port_check.who_owns(59999))

    def test_kill_dead_process_returns_true(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            ok, err = port_check.kill(99999)
            self.assertTrue(ok)

    def test_kill_permission_denied(self):
        with patch("os.kill", side_effect=PermissionError):
            ok, err = port_check.kill(1)
            self.assertFalse(ok)

    def test_alive_dead_process(self):
        with patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(port_check._alive(99999))


# ── balance_usage.py: edges ────────────────────────────────────────
from services import balance_usage
class TestBalanceEdges(unittest.TestCase):
    def test_resolve_key_none_returns_none(self):
        p = {"api_key": None}
        self.assertIsNone(balance_usage.resolve_api_key(p))

    def test_resolve_key_env_var(self):
        with patch.dict(os.environ, {"TEST_KEY": "from-env"}):
            p = {"api_key": None, "api_key_env": "TEST_KEY"}
            self.assertEqual(balance_usage.resolve_api_key(p), "from-env")


# ── async_runtime.py: error paths ──────────────────────────────────
from tunnel.async_runtime import AsyncRuntime


class TestAsyncRuntimeErrors(unittest.TestCase):
    def test_start_factory_exception_captured(self):
        rt = AsyncRuntime("test", stop_timeout=1)

        def bad_factory(loop):
            raise RuntimeError("factory failed")

        rt.start(bad_factory)
        time.sleep(0.3)
        self.assertIn("factory failed", rt.error)


# ── capture.py: states ─────────────────────────────────────────────
from capture.capture import CaptureMonitor


class TestCaptureMonitorStates(unittest.TestCase):
    def test_not_running_on_init(self):
        m = CaptureMonitor()
        self.assertEqual(m.status, "stopped")
