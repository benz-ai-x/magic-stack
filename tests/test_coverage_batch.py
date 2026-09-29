"""Batch coverage: system_proxy, suanpan/router edges, conn_coordinator, suanpan_runtime."""
import unittest
from unittest.mock import patch, MagicMock

# ── system_proxy.py ──────────────────────────────────────
from sysctl import system_proxy
class TestSystemProxy(unittest.TestCase):
    @patch("sysctl.system_proxy.subprocess.run")
    def test_active_services(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0,
            stdout="Ethernet\nWi-Fi\n")
        services = system_proxy._active_services()
        self.assertIn("Wi-Fi", services)


# ── suanpan/router.py edge cases ─────────────────────────
from suanpan.router import decide_route, NoRouteMatched
from suanpan.config import AppConfig, ProviderConfig, RouterConfig, Rule


def _router_config(**kw):
    defaults = dict(
        providers={
            "p1": ProviderConfig(base_url="https://x.com", api_key="k",
                                  auth_header="x-api-key", enabled=True, models=["m1"]),
        },
        router=RouterConfig(default="p1/m1"),
        rules=[],
    )
    defaults.update(kw)
    return AppConfig(**defaults)


class TestRouterEdges(unittest.TestCase):
    def test_disabled_provider_in_inline_override_falls_through(self):
        cfg = _router_config(providers={
            "p1": ProviderConfig(base_url="https://x.com", api_key="k",
                                  auth_header="x-api-key", enabled=False, models=["m1"]),
        })
        # p1 disabled → inline override falls through → default also p1/m1 → also disabled
        with self.assertRaises(NoRouteMatched):
            decide_route({"model": "test"}, config=cfg)

    def test_subagent_model_tag(self):
        cfg = _router_config()
        d = decide_route(
            {"model": "test", "system": "text <SUBAGENT-MODEL>p1/m1</SUBAGENT-MODEL>"},
            config=cfg)
        self.assertEqual(d.provider, "p1")
        self.assertTrue(d.strip_marker)

    def test_rule_match_skips_disabled_provider(self):
        cfg = _router_config(rules=[
            Rule(match_prefix="test", route_to="p1/m1")],
            providers={
                "p1": ProviderConfig(base_url="https://x.com", api_key="k",
                                      auth_header="x-api-key", enabled=False, models=["m1"]),
            })
        with self.assertRaises(NoRouteMatched):
            decide_route({"model": "test"}, config=cfg)


# ── connection_coordinator.py ────────────────────────────
from tunnel.connection_coordinator import ConnectionCoordinator


class TestConnectionCoordinatorLifecycle(unittest.TestCase):
    def test_start_calls_start_background_and_ssh(self):
        conn = ConnectionCoordinator(
            stats=MagicMock(), ssh_log_sink=lambda _: None,
            get_config=lambda: {"socks5_port": 1080, "http_listen_port": 8888,
                                 "tunnels": [{"ssh_host": "s"}]},
            get_tunnel_password=lambda t: "",
        )
        with patch.object(conn, "_start_background") as mock_bg, \
             patch.object(conn, "start_ssh") as mock_ssh:
            conn.start()
        mock_bg.assert_called_once()
        mock_ssh.assert_called_once()

    def test_restart_stops_and_restarts(self):
        conn = ConnectionCoordinator(
            stats=MagicMock(), ssh_log_sink=lambda _: None,
            get_config=lambda: {"socks5_port": 1080, "http_listen_port": 8888,
                                 "tunnels": [{"ssh_host": "s"}]},
            get_tunnel_password=lambda t: "",
        )
        called = []
        conn._start_background = lambda: called.append("bg")
        conn.start_ssh = lambda: called.append("ssh")
        conn._retry = MagicMock()
        conn._host_key = MagicMock()
        conn._ssh = MagicMock()
        conn._proxy_runtime = MagicMock()
        conn.restart(lambda: called.append("reload"))
        self.assertEqual(called, ["reload", "bg", "ssh"])


# ── suanpan_runtime.py ───────────────────────────────────
from services.suanpan_runtime import SuanpanRuntime


class TestSuanpanRuntimeLifecycle(unittest.TestCase):
    def test_listen_address_returns_default_on_missing(self):
        rt = SuanpanRuntime()
        rt._config_path = "/nonexistent/path.yaml"
        self.assertEqual(rt.listen_address(), "127.0.0.1:9527")

    def test_listen_address_cached(self):
        rt = SuanpanRuntime()
        rt._config_path = "/nonexistent/path.yaml"
        first = rt.listen_address()
        second = rt.listen_address()
        self.assertEqual(first, second)

    def test_stop_when_not_running_returns_true(self):
        rt = SuanpanRuntime()
        self.assertTrue(rt.stop())
