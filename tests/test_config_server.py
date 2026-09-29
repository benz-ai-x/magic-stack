"""Tests for config_server.py — HTTP config API, auth, masking, balance/usage."""
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from http.client import HTTPConnection
from unittest.mock import MagicMock, patch

from services import config_server
def _start_server(on_sp_saved=None, on_mp_saved=None):
    """Start a config server on a random port, return (server, port)."""
    import threading
    s = config_server.ConfigServer(on_sp_saved=on_sp_saved,
                                   on_mp_saved=on_mp_saved)
    s._server = config_server._ThreadingHTTPServer(
        ("127.0.0.1", 0), config_server._Handler, expected_token=s._token,
        on_sp_saved=on_sp_saved, on_mp_saved=on_mp_saved,
        instructions_fn=s.agent_instructions)
    port = s._server.server_address[1]
    s._thread = threading.Thread(target=s._server.serve_forever, daemon=True)
    s._thread.start()
    return s, port


def _request(port, method, path, body=None, token=None, host="127.0.0.1",
             headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = dict(headers or {})
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, data


class TestHostHeaderGuard(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_valid_host_allowed(self):
        status, _ = _request(self.port, "GET", "/api/state", token=self.token)
        self.assertNotEqual(status, 403)

    def test_invalid_host_rejected(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/state", headers={"Host": "evil.com", "Authorization": f"Bearer {self.token}"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 403)


class TestTokenAuth(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_no_token_rejected(self):
        status, _ = _request(self.port, "GET", "/api/state")
        self.assertEqual(status, 401)

    def test_wrong_token_rejected(self):
        status, _ = _request(self.port, "GET", "/api/state", token="wrong")
        self.assertEqual(status, 401)

    def test_correct_token_allowed(self):
        status, _ = _request(self.port, "GET", "/api/state", token=self.token)
        self.assertEqual(status, 200)

    def test_bearer_header_accepted(self):
        status, _ = _request(self.port, "GET", "/api/state", token=self.token)
        self.assertEqual(status, 200)

    def test_token_comparison_is_constant_time(self):
        """_valid_token must delegate to secrets.compare_digest (not ==)."""
        from types import SimpleNamespace
        import secrets as _secrets
        h = SimpleNamespace(
            path="/api/state",
            headers={"Authorization": f"Bearer {self.token}"},
            server=SimpleNamespace(expected_token=self.token),
        )
        with patch("services.config_server.secrets.compare_digest",
                   wraps=_secrets.compare_digest) as spy:
            result = config_server._Handler._valid_token(h)
            self.assertTrue(result)
            spy.assert_called_once_with(self.token, self.token)


class TestLoginPage(unittest.TestCase):
    """GET / 无 token 时返回登录页 HTML（浏览器直接打开可用）；API 401 仍 JSON。

    macOS 桥接首导航带 Bearer → 200（不经过此路径），零变化；Docker 版
    浏览器直接打开获得登录页。
    """

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_get_root_no_token_returns_login_html(self):
        status, body = _request(self.port, "GET", "/")
        self.assertEqual(status, 401)
        # 登录页是自包含 HTML（非 JSON 错误）
        self.assertIn("<!doctype html>", body.lower())
        self.assertIn("token", body.lower())
        # 含表单与 JS：fetch 带 Authorization 头调 / 种 cookie 后跳转
        self.assertIn("Authorization", body)
        self.assertIn("Bearer", body)
        self.assertIn("fetch", body)

    def test_get_root_wrong_token_also_returns_login_html(self):
        status, body = _request(self.port, "GET", "/", token="wrong")
        self.assertEqual(status, 401)
        self.assertIn("<!doctype html>", body.lower())

    def test_api_401_remains_json(self):
        """API 路径的 401 保持纯 JSON——curl/脚本客户端不期待 HTML。"""
        status, body = _request(self.port, "GET", "/api/state")
        self.assertEqual(status, 401)
        data = json.loads(body)
        self.assertEqual(data, {"error": "unauthorized"})

    def test_post_401_remains_json(self):
        status, body = _request(self.port, "POST", "/api/test-provider",
                                body="{}")
        self.assertEqual(status, 401)
        data = json.loads(body)
        self.assertEqual(data, {"error": "unauthorized"})

    def test_bridge_bearer_still_200(self):
        """macOS 桥接路径不回归：带 Bearer 首导航仍直接进页面。"""
        status, body = _request(self.port, "GET", "/", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("<!doctype html>", body.lower())
        self.assertNotIn("登录", body[:500])  # 不是登录页


class TestFavicon(unittest.TestCase):
    """Browsers auto-request /favicon.ico without a token — 204, not 403."""

    def setUp(self):
        self.server, self.port = _start_server()

    def tearDown(self):
        self.server.stop()

    def test_favicon_returns_204_without_token(self):
        status, body = _request(self.port, "GET", "/favicon.ico")
        self.assertEqual(status, 204)
        self.assertEqual(body, "")

    def test_favicon_still_guards_host(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/favicon.ico", headers={"Host": "evil.com"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 403)


class TestBodySizeLimit(unittest.TestCase):
    """POST/PUT bodies over MAX_BODY_BYTES must be rejected with 413."""

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_post_oversized_body_returns_413(self):
        with patch("services.config_server.MAX_BODY_BYTES", 2):
            status, _ = _request(
                self.port, "POST",
                "/api/fetch-models", token=self.token,
                body='{"a": 1}',  # 7 bytes > 2
            )
        self.assertEqual(status, 413)

    def test_put_oversized_body_returns_413(self):
        with patch("services.config_server.MAX_BODY_BYTES", 2):
            status, _ = _request(
                self.port, "PUT",
                "/api/state", token=self.token,
                body='{"a": 1}',
            )
        self.assertEqual(status, 413)

    def test_post_normal_body_still_works(self):
        with patch("services.config_server.MAX_BODY_BYTES", 10_000_000):
            with patch("services.sp_config.sp_load_raw", return_value={"providers": {}}):
                status, _ = _request(
                    self.port, "POST",
                    "/api/fetch-models", token=self.token,
                    body='{"provider": "x"}',
                )
        self.assertEqual(status, 200)

    def test_post_invalid_content_length_returns_400(self):
        """Non-numeric Content-Length must return 400, not crash the handler."""
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(
            f"POST /api/fetch-models HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Content-Length: not-a-number\r\n"
            f"\r\n".encode()
        )
        resp_line = sock.recv(4096).decode(errors="replace").split("\r\n")[0]
        sock.close()
        self.assertIn("400", resp_line)

    def test_negative_content_length_rejected(self):
        """Negative Content-Length must be rejected — rfile.read(-1) reads
        until EOF, defeating the body-size cap.  Python's BaseHTTPRequestHandler
        treats -1 as an oversized value (413) at the protocol level; our own
        guard returns 400. Either way, the request must not reach the handler."""
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(
            f"POST /api/fetch-models HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Content-Length: -1\r\n"
            f"\r\n".encode()
        )
        resp_line = sock.recv(4096).decode(errors="replace").split("\r\n")[0]
        sock.close()
        self.assertTrue("400" in resp_line or "413" in resp_line,
                        f"expected rejection, got: {resp_line}")


class TestApiState(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token
        self._tmpdirs = []

    def tearDown(self):
        self.server.stop()
        for d in self._tmpdirs:
            d.cleanup()

    def test_get_state_returns_json(self):
        status, data = _request(self.port, "GET", "/api/state", token=self.token)
        self.assertEqual(status, 200)
        parsed = json.loads(data)
        self.assertIn("mp", parsed)
        self.assertIn("sp", parsed)

    def test_read_sp_masks_provider_keys(self):
        from suanpan.config import load_config_masked
        with patch("suanpan.config.load_config_raw") as mock_raw:
            mock_raw.return_value = {
                "providers": {"deepseek": {"api_key": "sk-abcd1234efgh5678"}},
                "api_key": "sk-secretkey123",
            }
            result = load_config_masked("/dev/null")
        prov = result["providers"]["deepseek"]
        self.assertIsNone(prov["api_key"])
        self.assertTrue(prov["api_key_set"])
        self.assertIsNone(result["api_key"])
        self.assertTrue(result["api_key_set"])

    def test_unknown_route_returns_404(self):
        status, _ = _request(self.port, "GET", "/api/nonexistent", token=self.token)
        self.assertEqual(status, 404)

    def test_html_served_at_root(self):
        status, data = _request(self.port, "GET", "/", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("<html", data.lower())


class TestPutState(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_put_invalid_json_returns_400(self):
        status, _ = _request(self.port, "PUT", "/api/state", token=self.token,
                             body="not json")
        self.assertEqual(status, 400)

    def test_put_empty_body_returns_400(self):
        status, _ = _request(self.port, "PUT", "/api/state", token=self.token)
        self.assertEqual(status, 400)

    def test_put_goes_through_config_state_store(self):
        """PUT 经 save 把 prepare→commit 纳入同一事务边界。"""
        from mpconf.config_state import SaveResult
        with patch("services.config_server.ConfigStateStore") as store_cls:
            store_cls.return_value.save.return_value = SaveResult(True, None, [])
            body = json.dumps({"mp": {"servers": []}, "sp": {"providers": {}}})
            status, data = _request(self.port, "PUT",
                                    "/api/state", token=self.token,
                                    body=body)
        self.assertEqual(status, 200)
        store_cls.return_value.save.assert_called_once_with(
            mp={"servers": []}, sp={"providers": {}}, on_committed=None)

    def test_put_with_validation_errors_returns_422(self):
        from mpconf.config_state import SaveResult
        with patch("services.config_server.ConfigStateStore") as store_cls:
            store_cls.return_value.save.return_value = SaveResult(
                False, "validate", ["端口无效"])
            body = json.dumps({"mp": {}})
            status, data = _request(self.port, "PUT",
                                    "/api/state", token=self.token,
                                    body=body)
        self.assertEqual(status, 422)
        parsed = json.loads(data)
        self.assertFalse(parsed["ok"])
        self.assertIn("端口无效", parsed["errors"])


class TestFetchModelsEndpoint(unittest.TestCase):
    SP = {
        "providers": {
            "deepseek": {
                "base_url": "https://api.deepseek.com/anthropic",
                "api_key": "sk-test",
            },
        },
    }

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self, body, token=True):
        return _request(self.port, "POST", "/api/fetch-models", body=body,
                        token=self.token if token else None)

    def test_post_requires_token(self):
        status, _ = self._post('{"provider": "deepseek"}', token=False)
        self.assertEqual(status, 401)

    def test_post_unknown_route_404(self):
        status, _ = _request(self.port, "POST", "/api/nope", token=self.token,
                             body="{}")
        self.assertEqual(status, 404)

    def test_post_invalid_json_400(self):
        status, _ = self._post("not json")
        self.assertEqual(status, 400)

    def test_fetch_models_success_through_handler(self):
        body = json.dumps({"data": [{"id": "deepseek-v4-flash"}]}).encode()
        with patch("services.sp_config.sp_load_raw", return_value=self.SP), \
             patch("services.authenticated_http.AuthenticatedHttpClient.open",
                   return_value=body):
            status, data = self._post('{"provider": "deepseek"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"models": ["deepseek-v4-flash"]})

    def test_fetch_models_unknown_provider_returns_error(self):
        with patch("services.sp_config.sp_load_raw", return_value=self.SP):
            status, data = self._post('{"provider": "ghost"}')
        self.assertEqual(status, 200)
        self.assertIn("error", json.loads(data))


class TestRestoreKey(unittest.TestCase):
    def test_untouched_key_restored(self):
        # api_key_set=True + no new value → keep the existing key.
        from shared.provider_auth import restore_masked_key as _restore_key
        result = _restore_key(None, "original-key", keep=True)
        self.assertEqual(result, "original-key")

    def test_new_key_kept(self):
        from shared.provider_auth import restore_masked_key as _restore_key
        result = _restore_key("sk-newkey123", "old-key", keep=True)
        self.assertEqual(result, "sk-newkey123")

    def test_cleared_when_not_flagged(self):
        # api_key_set=False + no new value → the key is cleared.
        from shared.provider_auth import restore_masked_key as _restore_key
        result = _restore_key(None, "old-key", keep=False)
        self.assertIsNone(result)


class TestReadMpEmpty(unittest.TestCase):
    def test_empty_config_returns_empty_dict(self):
        with patch.object(config_server, "load_config", return_value=None), \
             patch.object(config_server, "merge_config", return_value=None):
            self.assertEqual(config_server._read_mp(), {})


class TestBalanceUsageEndpoints(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_balance_endpoint_returns_json(self):
        # Patch fetch_balance so the handler path runs without real network calls.
        with patch.object(config_server, "fetch_balance", return_value=[{"provider": "p"}]):
            status, data = _request(self.port, "GET",
                                    "/api/balance", token=self.token)
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(data), list)

    def test_usage_endpoint_returns_json(self):
        cfg = {"usage_log": {"path": "/nonexistent/usage.jsonl"}}
        with patch.object(config_server.sp_config, "sp_load_raw", return_value=cfg):
            status, data = _request(self.port, "GET",
                                    "/api/usage", token=self.token)
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertIsInstance(payload, dict)
        self.assertIn("daily", payload)
        self.assertIn("scenarios", payload)

    def test_usage_endpoint_accepts_supported_ranges(self):
        cfg = {"usage_log": {"path": "/nonexistent/usage.jsonl"}}
        with patch.object(config_server.sp_config, "sp_load_raw", return_value=cfg):
            for usage_range in ("today", "7d", "all"):
                with self.subTest(usage_range=usage_range):
                    status, _ = _request(
                        self.port, "GET",
                        f"/api/usage?range={usage_range}", token=self.token,
                    )
                    self.assertEqual(status, 200)

    def test_usage_endpoint_defaults_to_all(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({
                "ts": "2020-01-01T00:00:00+08:00", "provider": "p",
                "scenario": "default", "input_tokens": 1, "output_tokens": 1,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "status": 200, "latency_ms": 1,
            }) + "\n")
            path = f.name
        try:
            with patch.object(
                    config_server.sp_config, "sp_load_raw",
                    return_value={"usage_log": {"path": path}}):
                default_status, default_data = _request(
                    self.port, "GET", "/api/usage", token=self.token)
                all_status, all_data = _request(
                    self.port, "GET",
                    "/api/usage?range=all", token=self.token)
        finally:
            os.unlink(path)
        self.assertEqual(default_status, 200)
        self.assertEqual(all_status, 200)
        self.assertEqual(json.loads(default_data), json.loads(all_data))
        self.assertEqual(json.loads(default_data)["total"]["calls"], 1)

    def test_usage_endpoint_rejects_invalid_range(self):
        status, data = _request(
            self.port, "GET",
            "/api/usage?range=bogus", token=self.token,
        )
        self.assertEqual(status, 400)
        self.assertIn("range", json.loads(data)["error"])

    def test_usage_endpoint_accepts_month_range(self):
        status, data = _request(
            self.port, "GET",
            "/api/usage?range=month", token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertIn("total", json.loads(data))


class TestPutStateWrongPath(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_put_wrong_path_returns_404(self):
        status, data = _request(self.port, "PUT",
                                "/api/wrong", token=self.token,
                                body=json.dumps({}))
        self.assertEqual(status, 404)


class TestConfigServerStart(unittest.TestCase):
    def test_start_on_unavailable_port_returns_false(self):
        # Bind a port first, then try to start the server on it.
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            cs = config_server.ConfigServer(port=port)
            self.assertFalse(cs.start())
        finally:
            s.close()

    def test_start_when_already_running_returns_true(self):
        cs = config_server.ConfigServer(port=0)
        # Simulate an already-alive worker thread so running == True
        cs._thread = MagicMock()
        cs._thread.is_alive.return_value = True
        self.assertTrue(cs.start())


class TestCaptureStateField(unittest.TestCase):
    def test_capture_active_never_round_trips_into_plan(self):
        """服务端注入的只读字段在 prepare 阶段剥离，不可能回写文件。"""
        from mpconf.config_state import CommitPlan  # noqa: F401
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            store = config_server.ConfigStateStore(
                mp_path=str(Path(d) / "m.json"),
                sp_path=str(Path(d) / "s.yaml"))
            plan = store.prepare(mp={"servers": [], "capture_active": True})
            self.assertTrue(plan.ok)
            self.assertNotIn("capture_active", plan.mp_candidate)


class TestTestTunnelEndpoint(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self, body, token=True):
        return _request(self.port, "POST", "/api/test-tunnel", body=body,
                        token=self.token if token else None)

    def test_requires_token(self):
        status, _ = self._post('{"index": 0}', token=False)
        self.assertEqual(status, 401)

    def test_invalid_index_type_returns_400(self):
        for bad in ('{"index": "x"}', '{"index": true}', '{"index": null}', '{}', '"str"'):
            with self.subTest(body=bad):
                status, data = self._post(bad)
                self.assertEqual(status, 400)
                body = json.loads(data)
                # #69 S11d：非 dict body（如 "str"）被 _read_json_body 统一
                # 400 拒绝（error 键）；dict 但 index 非法走端点逻辑（ok 键）
                self.assertTrue(
                    ("ok" in body and not body["ok"]) or "error" in body,
                    body)

    def test_no_servers_returns_400(self):
        with patch.object(config_server, "_read_mp", return_value={"servers": []}):
            status, data = self._post('{"index": 0}')
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(data)["ok"])

    def test_index_out_of_range_returns_400(self):
        cfg = {"servers": [{"ssh": {"host": "h"}}]}
        with patch.object(config_server, "_read_mp", return_value=cfg):
            status, data = self._post('{"index": 5}')
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(data)["ok"])

    def test_negative_index_returns_400(self):
        cfg = {"servers": [{"ssh": {"host": "h"}}]}
        with patch.object(config_server, "_read_mp", return_value=cfg):
            status, _ = self._post('{"index": -1}')
        self.assertEqual(status, 400)

    def test_valid_index_delegates_to_test_tunnel(self):
        tunnel = {"ssh": {"host": "example.com", "user": "u",
                          "port": 2222}}
        with patch.object(config_server, "_read_mp",
                          return_value={"servers": [tunnel]}), \
             patch.object(config_server, "test_tunnel",
                          return_value={"ok": True}) as probe:
            status, data = self._post('{"index": 0}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"ok": True})
        probe.assert_called_once_with(tunnel)


class TestTunnelProbeLogic(unittest.TestCase):
    """config_server.test_tunnel 的端点职责：输入校验 + Keychain 取用 +
    委托 ssh_launch.probe（探针 argv / 失败分类的测试在 test_ssh_launch.py）。"""

    _KEY_TUNNEL = {
        "ssh": {"host": "example.com", "user": "u", "port": 2222,
                "auth_type": "key", "ssh_key": "~/.ssh/id_ed25519"},
    }
    _PW_TUNNEL = {
        "ssh": {"host": "example.com", "user": "u", "port": 22,
                "auth_type": "password"},
    }

    def test_missing_host_is_rejected_without_probe(self):
        with patch.object(config_server.ssh_launch, "probe") as probe:
            result = config_server.test_tunnel(
                {"ssh": {"host": "  ", "port": 22}})
        probe.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("地址", result["error"])

    def test_invalid_port_is_rejected(self):
        result = config_server.test_tunnel(
            {"ssh": {"host": "h", "port": 99999}})
        self.assertFalse(result["ok"])
        result = config_server.test_tunnel(
            {"ssh": {"host": "h", "port": "x"}})
        self.assertFalse(result["ok"])

    def test_option_like_destination_is_rejected(self):
        result = config_server.test_tunnel(
            {"ssh": {"host": "-oProxyCommand=evil"}})
        self.assertFalse(result["ok"])
        self.assertIn("无效", result["error"])

    def test_password_auth_without_saved_password(self):
        with patch.object(config_server.keychain, "get_password",
                          return_value=""), \
             patch.object(config_server.ssh_launch, "probe") as probe:
            result = config_server.test_tunnel(self._PW_TUNNEL)
        probe.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("密码", result["error"])

    def test_key_auth_delegates_to_ssh_launch(self):
        with patch.object(config_server.ssh_launch, "probe",
                          return_value={"ok": True}) as probe:
            result = config_server.test_tunnel(self._KEY_TUNNEL)
        self.assertEqual(result, {"ok": True})
        probe.assert_called_once_with(self._KEY_TUNNEL, password="")

    def test_password_auth_passes_saved_password_through(self):
        with patch.object(config_server.keychain, "get_password",
                          return_value="sekrit"), \
             patch.object(config_server.ssh_launch, "probe",
                          return_value={"ok": False, "error": "连接超时"}) as probe:
            result = config_server.test_tunnel(self._PW_TUNNEL)
        self.assertEqual(result, {"ok": False, "error": "连接超时"})
        probe.assert_called_once_with(self._PW_TUNNEL, password="sekrit")

    def test_probe_receives_normalized_tunnel_fields(self):
        """手改配置的空白/非规范端口经校验归一后才进探针。"""
        raw = {"ssh": {"host": "  example.com ", "user": " u ",
                       "port": "2222", "auth_type": "key", "ssh_key": "k"}}
        with patch.object(config_server.ssh_launch, "probe",
                          return_value={"ok": True}) as probe:
            result = config_server.test_tunnel(raw)
        self.assertEqual(result, {"ok": True})
        target = probe.call_args[0][0]["ssh"]
        self.assertEqual(target["host"], "example.com")
        self.assertEqual(target["user"], "u")
        self.assertEqual(target["port"], 2222)


class TestTestForwardEndpoint(unittest.TestCase):
    """POST /api/test-forward {index, forward}：行内测试按钮的后端面。"""

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self, body, token=True):
        return _request(self.port, "POST", "/api/test-forward", body=body,
                        token=self.token if token else None)

    def test_requires_token(self):
        status, _ = self._post('{"index": 0, "forward": {}}', token=False)
        self.assertEqual(status, 401)

    def test_bad_shape_returns_400(self):
        for bad in ('{"index": "x", "forward": {}}',
                    '{"index": true, "forward": {}}',
                    '{}',
                    '{"index": 0}',
                    '{"forward": {}}',
                    '{"index": 0, "forward": "x"}',
                    '{"tunnel": "x", "forward": {}}',
                    '{"tunnel": 5, "forward": {}}'):
            with self.subTest(body=bad):
                status, data = self._post(bad)
                self.assertEqual(status, 400)
                body = json.loads(data)
                self.assertTrue(
                    ("ok" in body and not body["ok"]) or "error" in body,
                    body)

    def test_no_servers_returns_400(self):
        with patch.object(config_server, "_read_mp",
                          return_value={"servers": []}):
            status, data = self._post('{"index": 0, "forward": {}}')
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(data)["ok"])

    def test_index_out_of_range_returns_400(self):
        cfg = {"servers": [{"ssh": {"host": "h"}}]}
        with patch.object(config_server, "_read_mp", return_value=cfg):
            status, _ = self._post('{"index": 5, "forward": {}}')
        self.assertEqual(status, 400)

    def test_form_tunnel_body_delegates_without_index(self):
        """设置窗新载荷 {tunnel, forward}：隧道与转发都取表单当前值——
        未保存的新服务器同样可测，不落 _read_mp（无 index 可解析）。"""
        tunnel = {"ssh": {"host": "new.example.com", "user": "u",
                          "port": 2222, "auth_type": "key"}}
        forward = {"local_port": 9000, "remote_host": "10.0.0.5",
                   "remote_port": 8000}
        with patch.object(config_server, "_read_mp") as rm, \
             patch.object(config_server, "test_forward",
                          return_value={"ok": True, "latency_ms": 42}) as tf:
            status, data = self._post(json.dumps({"tunnel": tunnel,
                                                  "forward": forward}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"ok": True, "latency_ms": 42})
        rm.assert_not_called()
        tf.assert_called_once_with(tunnel, forward)

    def test_legacy_index_body_still_resolves_saved_tunnel(self):
        """旧载荷 {index, forward} 兼容：按已保存服务器解析（agent.md 契约）。"""
        tunnel = {"ssh": {"host": "example.com", "user": "u", "port": 22}}
        forward = {"local_port": 9000, "remote_host": "10.0.0.5",
                   "remote_port": 8000}
        with patch.object(config_server, "_read_mp",
                          return_value={"servers": [tunnel]}), \
             patch.object(config_server, "test_forward",
                          return_value={"ok": True, "latency_ms": 42}) as tf:
            status, data = self._post(json.dumps({"index": 0,
                                                  "forward": forward}))
        self.assertEqual(status, 200)
        tf.assert_called_once_with(tunnel, forward)


class TestServerCheckEndpoint(unittest.TestCase):
    """POST /api/server-check {tunnel|index, only?}：服务卡一键检测的路由面
    （卡探测归一/编排的测试在 test_server_check.py）。"""

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self, body, token=True):
        return _request(self.port, "POST", "/api/server-check", body=body,
                        token=self.token if token else None)

    def test_requires_token(self):
        status, _ = self._post('{"index": 0}', token=False)
        self.assertEqual(status, 401)

    def test_bad_index_returns_400(self):
        cfg = {"servers": [{"ssh": {"host": "h"}}]}
        for bad in ('{"index": 5}', '{"index": "x"}', '{}'):
            with self.subTest(body=bad):
                with patch.object(config_server, "_read_mp",
                                  return_value=cfg):
                    status, data = self._post(bad)
                self.assertEqual(status, 400)
                self.assertFalse(json.loads(data)["ok"])

    def test_no_servers_returns_400(self):
        with patch.object(config_server, "_read_mp",
                          return_value={"servers": []}):
            status, data = self._post('{"index": 0}')
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(data)["ok"])

    def test_invalid_only_returns_400(self):
        tunnel = {"ssh": {"host": "h"}}
        with patch.object(config_server, "_read_mp",
                          return_value={"servers": [tunnel]}), \
             patch.object(config_server.server_check, "check_server") as cs:
            status, data = self._post('{"index": 0, "only": "telnet"}')
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(data)["ok"])
        cs.assert_not_called()

    def test_non_string_only_returns_400(self):
        """only 为 dict/list 等恶形不得打崩 handler 线程（评审实锄）。"""
        tunnel = {"ssh": {"host": "h"}}
        for bad in ('{"index": 0, "only": {"a": 1}}',
                    '{"index": 0, "only": ["ssh"]}',
                    '{"index": 0, "only": 42}'):
            with patch.object(config_server, "_read_mp",
                              return_value={"servers": [tunnel]}), \
                 patch.object(config_server.server_check, "check_server") as cs:
                status, data = self._post(bad)
            self.assertEqual(status, 400, bad)
            self.assertFalse(json.loads(data)["ok"])
        cs.assert_not_called()

    def test_index_body_delegates_with_only(self):
        tunnel = {"ssh": {"host": "example.com", "user": "u", "port": 22}}
        results = {"ssh": {"ok": True, "error": "", "latency_ms": 12}}
        with patch.object(config_server, "_read_mp",
                          return_value={"servers": [tunnel]}), \
             patch.object(config_server.server_check, "check_server",
                          return_value=results) as cs:
            status, data = self._post('{"index": 0, "only": "ssh"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data),
                         {"ok": True, "results": results})
        cs.assert_called_once_with(tunnel, config_server.keychain, only="ssh")

    def test_tunnel_body_runs_all_cards_without_only(self):
        tunnel = {"ssh": {"host": "new.example.com", "port": 22}}
        results = {"ssh": {"ok": True, "error": "", "latency_ms": 5},
                   "nfs": {"ok": True, "error": "", "family": "apt-get",
                           "installed": True, "listening_2049": True,
                           "exports_configured": True},
                   "openvpn": {"ok": True, "error": "", "installed": False,
                               "version": ""}}
        with patch.object(config_server, "_read_mp") as rm, \
             patch.object(config_server.server_check, "check_server",
                          return_value=results) as cs:
            status, data = self._post(json.dumps({"tunnel": tunnel}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data),
                         {"ok": True, "results": results})
        rm.assert_not_called()
        cs.assert_called_once_with(tunnel, config_server.keychain, only=None)


class TestForwardProbeLogic(unittest.TestCase):
    """config_server.test_forward 的端点职责：输入守卫 + Keychain 取用 +
    委托 ssh_launch.probe_forward（-W argv / 分类的测试在 test_ssh_launch.py）。"""

    _KEY_TUNNEL = {
        "ssh": {"host": "example.com", "user": "u", "port": 2222,
                "auth_type": "key", "ssh_key": "~/.ssh/id_ed25519"},
    }
    _PW_TUNNEL = {"ssh": {"host": "example.com", "user": "u",
                          "port": 22, "auth_type": "password"}}

    def test_invalid_tunnel_guarded_without_probe(self):
        with patch.object(config_server.ssh_launch, "probe_forward") as pf:
            result = config_server.test_forward(
                {"ssh": {"host": "  ", "port": 22}}, {"remote_port": 8000})
        pf.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("地址", result["error"])

    def test_option_like_destination_is_rejected(self):
        result = config_server.test_forward(
            {"ssh": {"host": "-oProxyCommand=evil"}}, {"remote_port": 8000})
        self.assertFalse(result["ok"])
        self.assertIn("无效", result["error"])

    def test_password_auth_without_saved_password(self):
        with patch.object(config_server.keychain, "get_password",
                          return_value=""), \
             patch.object(config_server.ssh_launch, "probe_forward") as pf:
            result = config_server.test_forward(
                self._PW_TUNNEL, {"remote_port": 8000})
        pf.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("密码", result["error"])

    def test_delegates_with_form_values_and_password(self):
        with patch.object(config_server.keychain, "get_password",
                          return_value="sekrit"), \
             patch.object(config_server.ssh_launch, "probe_forward",
                          return_value={"ok": True,
                                        "latency_ms": 120}) as pf:
            result = config_server.test_forward(self._PW_TUNNEL, {
                "local_port": 9000, "remote_host": "10.0.0.5",
                "remote_port": 8000})
        self.assertEqual(result, {"ok": True, "latency_ms": 120})
        args, kwargs = pf.call_args
        self.assertEqual(kwargs.get("password"), "sekrit")
        # 隧道侧归一后传入；forward 的三个表单值原样透传（归一在 probe_forward）
        self.assertEqual(args[0]["ssh"]["host"], "example.com")
        self.assertEqual(args[1], "10.0.0.5")
        self.assertEqual(args[2], 8000)


class TestCaptureCleanEndpoint(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self):
        return _request(self.port, "POST",
                        "/api/capture-clean", token=self.token, body="{}")

    def test_requires_token(self):
        status, _ = _request(self.port, "POST", "/api/capture-clean",
                             body="{}")
        self.assertEqual(status, 401)

    def test_empty_body_returns_400(self):
        # Client contract (mirrors the other POST endpoints): a JSON body is
        # mandatory — config_ui's cleanCapture() sends '{}'.
        status, _ = _request(self.port, "POST",
                             "/api/capture-clean", token=self.token)
        self.assertEqual(status, 400)

    def test_clean_delegates_to_capture_store(self):
        with patch.object(config_server, "_read_mp",
                          return_value={"capture_dir": "~/captures"}), \
             patch.object(config_server.capture_store, "clean",
                          return_value=3) as clean:
            status, data = self._post()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"ok": True, "removed": 3})
        clean.assert_called_once_with("~/captures")

    def test_clean_defaults_to_none_when_dir_missing(self):
        with patch.object(config_server, "_read_mp", return_value={}), \
             patch.object(config_server.capture_store, "clean",
                          return_value=0) as clean:
            status, data = self._post()
        self.assertEqual(status, 200)
        clean.assert_called_once_with(None)

    def test_clean_oserror_surfaces_message(self):
        with patch.object(config_server, "_read_mp", return_value={}), \
             patch.object(config_server.capture_store, "clean",
                          side_effect=OSError("拒绝修改非 Magic Stack 创建的现有目录")):
            status, data = self._post()
        self.assertEqual(status, 200)
        parsed = json.loads(data)
        self.assertFalse(parsed["ok"])
        self.assertIn("Magic Stack", parsed["error"])


if __name__ == "__main__":
    unittest.main()


class TestHeaderOnlyAuth(unittest.TestCase):
    """issue #10：token 只进 Authorization header 与 HttpOnly 会话 cookie。

    URL 永不带凭证；query-string 认证路径删除；无凭证的 / 与 /api/* 一律
    401。常量时间比较保留。
    """

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _get(self, path, headers=None):
        return _request(self.port, "GET", path, headers=headers or {})

    def test_root_without_credentials_is_401(self):
        status, _ = self._get("/")
        self.assertEqual(status, 401)

    def test_query_string_token_no_longer_accepted(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/api/state?token={self.token}")  # 真 query 形态
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 401, "query 认证路径必须删除")

    def test_root_with_bearer_header_sets_httponly_session_cookie(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/", headers={
            "Authorization": f"Bearer {self.token}"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        set_cookie = resp.getheader("Set-Cookie", "")
        self.assertIn("cfgsess=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)

    def test_api_with_session_cookie_only_is_authorized(self):
        _, resp_headers = self._get("/", headers={
            "Authorization": f"Bearer {self.token}"})
        # 从原始响应提取 cookie（_request 返回文本，改用 http.client 直取）
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/state", headers={
            "Cookie": f"cfgsess={self.token}"})
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn('"mp"', body)

    def test_bogus_cookie_rejected_constant_time_paths_alive(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/state", headers={
            "Cookie": "cfgsess=wrong"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 401)


class TestConfigServerParameterization(unittest.TestCase):
    """ConfigServer 的 bind_host / token 构造参数（Docker 适配的 seam）。

    默认值保持 macOS 行为：绑 127.0.0.1 + 自造随机 token。
    """

    def tearDown(self):
        srv = getattr(self, "srv", None)
        if srv is not None:
            srv.stop()

    def _start(self, **kwargs):
        self.srv = config_server.ConfigServer(port=0, **kwargs)
        self.assertTrue(self.srv.start())
        return self.srv._server.server_address[1]

    def test_defaults_bind_loopback_and_generate_token(self):
        port = self._start()
        self.assertEqual(self.srv._server.server_address[0], "127.0.0.1")
        self.assertRegex(self.srv.token, r"^[0-9a-f]{32}$")
        status, _ = _request(port, "GET", "/api/state", token=self.srv.token)
        self.assertEqual(status, 200)

    def test_custom_bind_host_honored(self):
        # 0.0.0.0 即 Docker 实取值：绑定后 loopback 仍可达。
        port = self._start(bind_host="0.0.0.0")
        self.assertEqual(self.srv._server.server_address[0], "0.0.0.0")
        status, _ = _request(port, "GET", "/api/state", token=self.srv.token)
        self.assertEqual(status, 200)

    def test_custom_token_used_for_auth(self):
        port = self._start(token="fixed-token")
        self.assertEqual(self.srv.token, "fixed-token")
        status, _ = _request(port, "GET", "/api/state", token="fixed-token")
        self.assertEqual(status, 200)
        status, _ = _request(port, "GET", "/api/state")
        self.assertEqual(status, 401)


class TestMultiActiveDecorations(unittest.TestCase):
    """多活（v0.9）：/api/state 的 is_proxy / forward_running 装饰注入。"""

    def _state(self, runtime_state_fn):
        import threading
        s = config_server.ConfigServer(runtime_state_fn=runtime_state_fn)
        s._server = config_server._ThreadingHTTPServer(
            ("127.0.0.1", 0), config_server._Handler,
            expected_token=s._token, runtime_state_fn=runtime_state_fn)
        port = s._server.server_address[1]
        threading.Thread(target=s._server.serve_forever, daemon=True).start()
        self.addCleanup(s.stop)
        status, body = _request(port, "GET", "/api/state", token=s._token)
        return json.loads(body)["mp"]

    def test_is_proxy_and_forward_running_injected(self):
        cfg = {"servers": [
            {"id": "t-1", "ssh": {"host": "a"},
             "services": {"ssh": {"forwards": []}}},
            {"id": "t-2", "ssh": {"host": "b"},
             "services": {"ssh": {"forwards": []}}}],
            "proxy_server_id": "t-1"}
        with patch.object(config_server, "_read_mp", return_value=cfg):
            from tunnel.connection_coordinator import ForwardState
            from shared.runtime_state import RuntimeProjection
            mp = self._state(
                lambda: RuntimeProjection(
                    forwards=(ForwardState("t-2", "b", "connected"),)))
        self.assertTrue(mp["servers"][0]["is_proxy"])
        self.assertFalse(mp["servers"][0]["forward_running"])
        self.assertFalse(mp["servers"][1]["is_proxy"])
        self.assertTrue(mp["servers"][1]["forward_running"])

    def test_states_fn_absent_decorates_all_false(self):
        cfg = {"servers": [{"id": "t-1", "ssh": {"host": "a"},
                            "services": {"ssh": {"forwards": []}}}],
               "proxy_server_id": "t-1"}
        with patch.object(config_server, "_read_mp", return_value=cfg):
            mp = self._state(None)
        self.assertTrue(mp["servers"][0]["is_proxy"])
        self.assertFalse(mp["servers"][0]["forward_running"])

    def test_decorations_never_round_trip_into_plan(self):
        """READONLY_DECORATED_FIELDS 扩名单——is_proxy/forward_running 永不落盘。"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            store = config_server.ConfigStateStore(
                mp_path=str(Path(d) / "m.json"),
                sp_path=str(Path(d) / "s.yaml"))
            plan = store.prepare(mp={"servers": [
                {"id": "t-1", "ssh": {"host": "a"},
                 "services": {"ssh": {"forwards": []}},
                 "is_proxy": True, "forward_running": True}]})
            self.assertTrue(plan.ok)
            t = plan.mp_candidate["servers"][0]
            self.assertNotIn("is_proxy", t)
            self.assertNotIn("forward_running", t)


class TestPutSectionCallbacks(unittest.TestCase):
    """PUT 提交成功后按「事务涉及的段」触发回调：MP 段 → on_mp_saved
    （app 刷新内存副本），SP 段 → on_sp_saved（网关 reload）。失败不触发。"""

    def setUp(self):
        self.mp_saved = MagicMock()
        self.sp_saved = MagicMock()
        self.server, self.port = _start_server(self.sp_saved, self.mp_saved)
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _put(self, body_obj, commit_ok=True):
        from mpconf.config_state import SaveResult
        result = (SaveResult(True, None, []) if commit_ok
                  else SaveResult(False, "mp", ["bad"]))

        def _save(*, mp=None, sp=None, on_committed=None):
            # 真实 save 的语义替身：成功才触发 on_committed
            if commit_ok and on_committed is not None:
                on_committed()
            return result

        with patch("services.config_server.ConfigStateStore") as store_cls:
            store_cls.return_value.save.side_effect = _save
            return _request(self.port, "PUT", "/api/state",
                            token=self.token, body=json.dumps(body_obj))

    def test_mp_only_put_fires_on_mp_saved_only(self):
        status, _ = self._put({"mp": {"servers": []}})
        self.assertEqual(status, 200)
        self.mp_saved.assert_called_once()
        self.sp_saved.assert_not_called()

    def test_sp_only_put_fires_on_sp_saved_only(self):
        status, _ = self._put({"sp": {"providers": {}}})
        self.assertEqual(status, 200)
        self.sp_saved.assert_called_once()
        self.mp_saved.assert_not_called()

    def test_both_sections_fire_both_callbacks(self):
        status, _ = self._put({"mp": {"servers": []}, "sp": {"providers": {}}})
        self.assertEqual(status, 200)
        self.mp_saved.assert_called_once()
        self.sp_saved.assert_called_once()

    def test_failed_commit_fires_nothing(self):
        status, _ = self._put({"mp": {"servers": []}}, commit_ok=False)
        self.assertEqual(status, 422)
        self.mp_saved.assert_not_called()
        self.sp_saved.assert_not_called()


class TestProxyRoleDecorationById(unittest.TestCase):
    """is_proxy 装饰按角色 id 真相解析（v0.9.2）：下标漂移不再误导 UI。"""

    def test_is_proxy_resolves_by_id(self):
        cfg = {"servers": [
            {"id": "t-1", "ssh": {"host": "a"},
             "services": {"ssh": {"forwards": []}}},
            {"id": "t-2", "ssh": {"host": "b"},
             "services": {"ssh": {"forwards": []}}}],
            "proxy_server_id": "t-2"}
        server = config_server.ConfigServer()
        server._server = config_server._ThreadingHTTPServer(
            ("127.0.0.1", 0), config_server._Handler,
            expected_token=server._token)
        port = server._server.server_address[1]
        import threading
        threading.Thread(target=server._server.serve_forever, daemon=True).start()
        self.addCleanup(server.stop)
        with patch.object(config_server, "_read_mp", return_value=cfg):
            status, body = _request(port, "GET", "/api/state", token=server._token)
        mp = json.loads(body)["mp"]
        self.assertFalse(mp["servers"][0]["is_proxy"])
        self.assertTrue(mp["servers"][1]["is_proxy"])


class TestNfsDecorationShape(unittest.TestCase):
    """ADR-007 可见性链路：nfs_states 装饰携带 {status,error,fixable}——
    error 不再丢弃（2026-09-18 真机案例：用户看到"没反应"正因错误
    断在装饰层）。"""

    def test_mount_decoration_carries_error_and_fixable(self):
        import threading
        from tunnel.connection_coordinator import ForwardState  # noqa: F401
        from mount.coordinator import MountState
        from shared.runtime_state import RuntimeProjection
        cfg = {"servers": [{"id": "t-1", "ssh": {"host": "a"},
                            "services": {"ssh": {"forwards": []}}}],
               "proxy_server_id": "t-1"}
        proj = RuntimeProjection(mounts=(MountState(
            "t-1", "srv", "data", "error", "远程路径不存在或未导出", "exports"),))
        s = config_server.ConfigServer(runtime_state_fn=lambda: proj)
        s._server = config_server._ThreadingHTTPServer(
            ("127.0.0.1", 0), config_server._Handler,
            expected_token=s._token, runtime_state_fn=lambda: proj)
        port = s._server.server_address[1]
        threading.Thread(target=s._server.serve_forever, daemon=True).start()
        self.addCleanup(s.stop)
        with patch.object(config_server, "_read_mp", return_value=cfg):
            status, body = _request(port, "GET", "/api/state", token=s._token)
        servers = json.loads(body)["mp"]["servers"]
        self.assertEqual(servers[0]["nfs_states"]["data"], {
            "status": "error", "error": "远程路径不存在或未导出",
            "fixable": "exports"})


class TestAgentInstructionsApi(unittest.TestCase):
    """GET /api/agent-instructions——浏览器直开设置页的「复制 AI 助手指令」
    回退通道。文案含 Bearer token，必须过认证；文本取自
    agent_instructions() 单一归宿（与原生 bridge 路径逐字节一致）。"""

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_requires_token(self):
        status, body = _request(self.port, "GET", "/api/agent-instructions")
        self.assertEqual(status, 401)
        self.assertIn("error", json.loads(body))

    def test_text_matches_single_source(self):
        status, body = _request(self.port, "GET", "/api/agent-instructions",
                                token=self.token)
        self.assertEqual(status, 200)
        text = json.loads(body)["text"]
        self.assertEqual(text, self.server.agent_instructions())
        self.assertIn(f"Bearer {self.token}", text)
        self.assertIn("agent.md", text)

    def test_guide_link_serves_the_bundled_document_without_credentials(self):
        status, body = _request(self.port, "GET", "/agent.md")
        self.assertEqual(status, 200)
        guide = Path(__file__).resolve().parents[1] / "docs" / "agent.md"
        self.assertEqual(body, guide.read_text(encoding="utf-8"))

    def test_missing_fn_degrades_to_explicit_error(self):
        # 直接构造 server 漏传 instructions_fn：明确 500 JSON，
        # 不在 handler 线程裸抛
        self.server._server.instructions_fn = None
        status, body = _request(self.port, "GET", "/api/agent-instructions",
                                token=self.token)
        self.assertEqual(status, 500)
        self.assertIn("error", json.loads(body))


class TestAgentInstructionsPortLifecycleHint(unittest.TestCase):
    """ADR-009：agent_instructions 尾注按形态区分——macOS 提示按需开启，
    Docker（bind_host 非 loopback）不带菜单提示。"""

    def test_macos_form_carries_hint(self):
        s = config_server.ConfigServer()
        self.assertIn("配置 API 服务", s.agent_instructions())

    def test_docker_form_has_no_menu_hint(self):
        s = config_server.ConfigServer(bind_host="0.0.0.0", token="t")
        self.assertNotIn("配置 API 服务", s.agent_instructions())

    def test_bootstrap_commands_use_actual_port_and_quote_token_for_both_languages(self):
        token = "fixture'$(literal-not-a-command)"
        server = config_server.ConfigServer(port=19528, token=token)
        for language in ("zh-CN", "en"):
            with self.subTest(language=language), \
                    patch.object(config_server.i18n, "_current", language):
                text = server.agent_instructions()
            commands = [shlex.split(line.strip()) for line in text.splitlines()
                        if line.strip().startswith("curl ")]
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][-1], "http://127.0.0.1:19528/agent.md")
            self.assertEqual(commands[1][-1], "http://127.0.0.1:19528/api/state")
            self.assertEqual(commands[1][commands[1].index("-H") + 1],
                             "Authorization: Bearer " + token)
            self.assertNotIn("{lifecycle}", text)
            if language == "en":
                self.assertIn("complete mp or sp section", text)
                self.assertIn("Preferences", text)


class TestProbeProviderEndpoint(unittest.TestCase):
    """ADR-010：POST /api/probe-provider——认证门 + base_url 校验 + 委托。"""

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self, body, token=True):
        return _request(self.port, "POST", "/api/probe-provider", body=body,
                        token=self.token if token else None)

    def test_requires_token(self):
        status, _ = self._post('{"base_url": "https://x"}', token=False)
        self.assertEqual(status, 401)

    def test_missing_base_url_400(self):
        status, data = self._post('{}')
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(data))

    def test_delegates_to_probe_provider(self):
        sentinel = {"anthropic": {"reachable": True}, "openai": {},
                    "responses": {}}
        with patch.object(config_server, "probe_provider",
                          return_value=sentinel) as probe:
            status, data = self._post(
                '{"base_url": "https://api.test.com", "api_key": "sk"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), sentinel)
        probe.assert_called_once_with({"base_url": "https://api.test.com",
                                       "api_key": "sk"})


class TestAgentSetupEndpoints(unittest.TestCase):
    """ADR-010 M4：GET /api/agents + agent-setup-preview/setup-agent。"""

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_agents_list_shape(self):
        status, data = _request(self.port, "GET", "/api/agents",
                                token=self.token)
        self.assertEqual(status, 200)
        agents = json.loads(data)
        ids = [a["id"] for a in agents]
        self.assertEqual(ids, ["claude-code", "codex", "opencode", "zcode"])

    def test_agents_requires_token(self):
        status, _ = _request(self.port, "GET", "/api/agents", token=None)
        self.assertEqual(status, 401)

    def test_agent_setup_preview_and_setup(self):
        with patch.object(config_server.claude_code_setup,
                          "agent_preview",
                          return_value={"ok": True, "already": False,
                                        "changes": []}) as pv, \
             patch.object(config_server.claude_code_setup,
                          "agent_setup",
                          return_value={"ok": True, "action": "added",
                                        "msg": "done"}) as su:
            status, data = _request(
                self.port, "POST", "/api/agent-setup-preview",
                body='{"agent": "codex", "options": {"model": "gpt-5.2"}}',
                token=self.token)
            self.assertEqual(status, 200)
            pv.assert_called_once_with("codex", {"model": "gpt-5.2"})
            status, data = _request(
                self.port, "POST", "/api/setup-agent",
                body='{"agent": "codex"}', token=self.token)
            self.assertEqual(status, 200)
            su.assert_called_once_with("codex", None)

    def test_setup_agent_non_dict_options_coerced_to_none(self):
        with patch.object(config_server.claude_code_setup,
                          "agent_setup",
                          return_value={"ok": True}) as su:
            _request(self.port, "POST", "/api/setup-agent",
                     body='{"agent": "zcode", "options": "junk"}',
                     token=self.token)
        su.assert_called_once_with("zcode", None)


class TestNfsSetupRemoteEndpoint(unittest.TestCase):
    """POST /api/nfs-setup-remote 的 mounts 入参校验。"""

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self, body, token=None):
        return _request(self.port, "POST", "/api/nfs-setup-remote",
                        body=body,
                        token=self.token if token is None else token)

    def test_requires_token(self):
        status, _ = self._post('{"mounts": []}', token=False)
        self.assertEqual(status, 401)

    def test_null_mount_entries_rejected(self):
        # [null] 曾穿透 all() 生成器短路（空序列恒 True）→
        # shlex.quote(None) 在 handler 线程抛 TypeError
        status, data = self._post(
            '{"tunnel": {"ssh_host": "srv"}, "mounts": [null]}')
        self.assertEqual(status, 400)
        body = json.loads(data)
        self.assertFalse(body["ok"])
        self.assertIn("mounts", body["error"])

    def test_mixed_null_entry_rejected(self):
        status, data = self._post(
            '{"tunnel": {"ssh_host": "srv"}, "mounts": ["/data", null]}')
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(data)["ok"])

    def test_valid_mounts_forwarded(self):
        with patch.object(config_server, "nfs_setup_remote",
                          return_value={"ok": True}) as setup:
            status, data = self._post(
                '{"tunnel": {"ssh_host": "srv"}, "mounts": ["/data"], '
                '"squash": true}')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["ok"])
        setup.assert_called_once()


class TestDispatchTables(unittest.TestCase):
    """路由表自洽（架构评审 R2-2）：一个端点一行声明——表项必可调、
    GET/PUT 不越 method、覆盖数与端点清单一致。"""

    def test_tables_map_to_callable_handlers(self):
        from services import config_server as cs
        for table in (cs._API_GET, cs._API_POST, cs._API_PUT):
            for path, handler in table.items():
                self.assertTrue(path.startswith("/api/"), path)
                self.assertTrue(callable(handler), path)

    def test_post_and_put_paths_disjoint(self):
        # GET+PUT 同路径（/api/state 读写对）是正常 REST；带 body 的
        # POST 与 PUT 不得共享路径——共享即语义混淆
        from services import config_server as cs
        self.assertFalse(set(cs._API_POST) & set(cs._API_PUT))

    def test_endpoint_count(self):
        # 端点增减须显式改这里的数字——防表项被误删
        from services import config_server as cs
        self.assertEqual(len(cs._API_GET), 7)
        self.assertEqual(len(cs._API_POST), 18)
        self.assertEqual(len(cs._API_PUT), 1)
