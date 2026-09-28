"""Coverage push: close the remaining gaps in the config stack.

Covers previously-uncovered lines in:
- config_server.py (lines 37, 55, 63, 122-127, 135-136, 165, 173-174, 192, 194-197)
- config.py (lines 134-135 — ValueError swallow in http_listen back-compat)
- claude_code_setup.py (lines 31, 34-37, 41, 60, 97)
- bridge_protocol.py (lines 39-40, 67-69)
- capture_controller.py (lines 67, 71)

Lives in its own file so it doesn't collide with sibling coverage PRs.
"""
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import MagicMock, patch

from shellui import bridge_protocol
from services import claude_code_setup
from mpconf import config
from services import config_server
from capture.capture_controller import CaptureController


# ── config_server helpers ──────────────────────────────────────────────

def _start_server():
    """Start a config server on a random port, return (server, port)."""
    s = config_server.ConfigServer()
    s._server = config_server._ThreadingHTTPServer(
        ("127.0.0.1", 0), config_server._Handler, expected_token=s._token)
    port = s._server.server_address[1]
    s._thread = threading.Thread(target=s._server.serve_forever, daemon=True)
    s._thread.start()
    return s, port


def _request(port, method, path, body=None, token=None, host_header=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host_header is not None:
        headers["Host"] = host_header
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, data


# ── config_server: _read_mp line 37 ───────────────────────────────────

class TestReadMpServers(unittest.TestCase):
    def test_server_gets_has_password_flag(self):
        """Line 37: servers get has_password set based on keychain lookup."""
        servers = [{"name": "t1",
                    "ssh": {"host": "h", "auth_type": "password"}}]
        with patch.object(config_server, "load_config", return_value={}), \
             patch.object(config_server, "merge_config",
                          return_value={"servers": servers}), \
             patch.object(config_server.keychain, "get_password",
                          return_value="secret"):
            cfg = config_server._read_mp()
        self.assertTrue(cfg["servers"][0]["has_password"])

    def test_server_has_password_false_when_no_keychain_entry(self):
        servers = [{"name": "t1",
                    "ssh": {"host": "h", "auth_type": "password"}}]
        with patch.object(config_server, "load_config", return_value={}), \
             patch.object(config_server, "merge_config",
                          return_value={"servers": servers}), \
             patch.object(config_server.keychain, "get_password",
                          return_value=""):
            cfg = config_server._read_mp()
        self.assertFalse(cfg["servers"][0]["has_password"])

    def test_server_has_password_false_when_auth_is_key(self):
        """auth_type != password → has_password stays False; keychain is
        never even consulted (the `and` short-circuits)."""
        servers = [{"name": "t1", "ssh": {"host": "h", "auth_type": "key"}}]
        with patch.object(config_server, "load_config", return_value={}), \
             patch.object(config_server, "merge_config",
                          return_value={"servers": servers}), \
             patch.object(config_server.keychain, "get_password",
                          return_value="never-called") as gp:
            cfg = config_server._read_mp()
        self.assertFalse(cfg["servers"][0]["has_password"])
        gp.assert_not_called()


# ── keychain 编排语义（原 _write_mp）——由 ConfigStateStore.prepare 承接 ──
# 详见 tests/test_config_state.py::TestKeychainTransaction（迁移后唯一权威）


class TestAgentMdRoute(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_agent_md_served_without_token(self):
        """Lines 122-124: agent.md is public (loopback-only, no token)."""
        status, data = _request(self.port, "GET", "/agent.md")
        self.assertEqual(status, 200)
        self.assertTrue(len(data) > 0)

    def test_agent_md_404_when_resource_missing(self):
        """Lines 125-126: OSError on reading agent.md returns JSON 404."""
        with patch.object(config_server, "_resource_path",
                          return_value="/nonexistent/agent.md"):
            status, data = _request(self.port, "GET", "/agent.md")
        self.assertEqual(status, 404)
        self.assertIn("agent.md not found", json.loads(data)["error"])


class TestBearerHeaderAuth(unittest.TestCase):
    """Guard for #39: authenticated endpoints accept header-only tokens.

    The settings UI sends the bearer token exclusively via the
    Authorization header (never the URL query string).  This pins the
    server-side half of that contract — _valid_token must accept a token
    that arrives without any ?token= query parameter.
    """

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_get_state_authenticates_via_header_only(self):
        status, data = _request(self.port, "GET", "/api/state", token=self.token)
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertIn("mp", body)
        self.assertIn("sp", body)


# ── config_server: html OSError path (lines 135-136) ──────────────────

class TestHtmlOSError(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_root_html_404_when_missing(self):
        """Lines 135-136: config_ui.html not found → JSON 404."""
        with patch.object(config_server, "_resource_path",
                          return_value="/nonexistent/config_ui.html"):
            status, data = _request(
                self.port, "GET", "/", token=self.token)
        self.assertEqual(status, 404)
        self.assertIn("config_ui.html not found", json.loads(data)["error"])


# ── config_server: setup-claude-code POST (line 165) ──────────────────

class TestSetupClaudeCodeEndpoint(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_setup_claude_code_endpoint(self):
        """POST /api/setup-claude-code delegates to claude_code_setup.setup()
        (#44: the middle-man adapter is inlined away)."""
        mock_result = {"ok": True, "action": "added", "msg": "done"}
        with patch.object(config_server.claude_code_setup, "setup",
                          return_value=mock_result):
            status, data = _request(
                self.port, "POST",
                "/api/setup-claude-code", token=self.token,
                body=json.dumps({}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), mock_result)

    def test_cc_sync_preview_endpoint(self):
        """POST /api/cc-sync-preview delegates to claude_code_setup.preview()
        with the request's roles payload (#3 验收 9 read-only dry run)."""
        mock_result = {"ok": True, "already": False, "changes": []}
        roles = {"default": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}}
        with patch.object(config_server.claude_code_setup, "preview",
                          return_value=mock_result) as mock_preview:
            status, data = _request(
                self.port, "POST",
                "/api/cc-sync-preview", token=self.token,
                body=json.dumps({"roles": roles}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), mock_result)
        mock_preview.assert_called_once_with(roles=roles)


class TestCcDefaultRolesEndpoint(unittest.TestCase):
    """GET /api/cc-default-roles returns the UI's role-table seed."""

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_cc_default_roles_endpoint(self):
        roles = {"opus": {"model": "KIMI/k3", "one_m": True}}
        with patch.object(config_server.claude_code_setup, "default_roles",
                          return_value=roles):
            status, data = _request(
                self.port, "GET",
                "/api/cc-default-roles", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), roles)

    def test_cc_default_roles_seed_rules_param(self):
        """?seed=rules forces the rule-derived seed (按路由规则重置) — the
        live-env read-back is skipped via force_rules=True."""
        payload = {"roles": {}, "synced": True, "drift": True}
        with patch.object(config_server.claude_code_setup, "default_roles",
                          return_value=payload) as mock_roles:
            status, data = _request(
                self.port, "GET",
                "/api/cc-default-roles?seed=rules", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), payload)
        mock_roles.assert_called_once_with(force_rules=True)

    def test_cc_default_roles_rejects_unknown_seed(self):
        status, data = _request(
            self.port, "GET",
            "/api/cc-default-roles?seed=live", token=self.token)
        self.assertEqual(status, 400)
        self.assertIn("seed", json.loads(data)["error"])


# ── config_server: PUT 403 path (lines 173-174) ───────────────────────

class TestPutForbidden(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_put_without_token_returns_403(self):
        """Lines 173-174: PUT without a valid token → 403 before any work."""
        with patch.object(config_server, "ConfigStateStore") as store_cls:
            status, data = _request(
                self.port, "PUT", "/api/state",
                body=json.dumps({"mp": {}}))
        self.assertEqual(status, 401)
        store_cls.assert_not_called()

    def test_put_with_invalid_host_returns_403(self):
        status, data = _request(
            self.port, "PUT", "/api/state", token=self.token,
            body=json.dumps({}), host_header="evil.com")
        self.assertEqual(status, 401)


# ── config_server: PUT 校验失败 + on_sp_saved 回调次序 ─

class TestPutSpSaveAndCallback(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_prepare_failure_appends_error(self):
        """issue #6：prepare 校验失败 → 422。"""
        from mpconf.config_state import SaveResult
        with patch.object(config_server, "ConfigStateStore") as store_cls:
            store_cls.return_value.save.return_value = SaveResult(
                False, "validate", ["invalid config"])
            status, data = _request(
                self.port, "PUT", "/api/state", token=self.token,
                body=json.dumps({"sp": {"providers": {}}}))
        self.assertEqual(status, 422)
        self.assertIn("invalid config", json.loads(data)["errors"])

    def test_on_sp_saved_callback_invoked_on_success(self):
        """issue #6：on_sp_saved 只在完整提交后触发（作为 on_committed）。"""
        from mpconf.config_state import SaveResult
        callback = MagicMock()
        with patch.object(config_server, "ConfigStateStore") as store_cls:
            def save(*, mp=None, sp=None, on_committed=None):
                if on_committed is not None:
                    on_committed()
                return SaveResult(True, None, [])
            store_cls.return_value.save.side_effect = save
            self.server._server.on_sp_saved = callback
            try:
                _request(
                    self.port, "PUT", "/api/state", token=self.token,
                    body=json.dumps({"sp": {"providers": {}}}))
            finally:
                self.server._server.on_sp_saved = None
        callback.assert_called_once()

    def test_on_sp_saved_exception_swallowed(self):
        """Lines 196-197: a raising on_sp_saved callback must not bubble up."""
        callback = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(config_server, "ConfigStateStore"):
            self.server._server.on_sp_saved = callback
            try:
                status, data = _request(
                    self.port, "PUT", "/api/state", token=self.token,
                    body=json.dumps({"sp": {"providers": {}}}))
            finally:
                self.server._server.on_sp_saved = None
        # The exception was caught; the response is still 200 OK.
        self.assertEqual(status, 200)


# ── config.py: lines 134-135 (http_listen ValueError swallow) ─────────

class TestMergeConfigHttpListenInvalid(unittest.TestCase):
    def test_invalid_http_listen_falls_through_to_default(self):
        """Lines 134-135: when netloc.parse_listen raises ValueError on the
        legacy http_listen value, the exception is swallowed and the range
        check below resets http_listen_port to the default (8888)."""
        cfg = {"http_listen": "bad-no-colon"}
        merged = config.merge_config(cfg)
        self.assertEqual(merged["http_listen_port"], 8888)


# ── claude_code_setup.py: setup listen resolution ───────────

class TestSetupListenResolution(unittest.TestCase):
    def test_setup_uses_suanpan_listen(self):
        """setup() resolves listen via sp_config.suanpan_listen()."""
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:8888"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value={}), \
                 patch.dict("shared.config_store.PATHS",
                            {"claude_settings": settings_path}):
                result = claude_code_setup.setup()
            with open(settings_path) as f:
                written = json.load(f)
        self.assertTrue(result["ok"])
        self.assertEqual(
            written["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8888")


class TestSetupFallbackDefaultListen(unittest.TestCase):
    def test_setup_uses_schema_default_when_sp_unreadable(self):
        """suanpan_listen() falls back to the schema default (sandboxed
        PATHS["sp"] doesn't exist), giving 127.0.0.1:9527."""
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value={}), \
                 patch.dict("shared.config_store.PATHS",
                            {"claude_settings": settings_path}):
                result = claude_code_setup.setup()
            self.assertTrue(result["ok"])
            with open(settings_path) as f:
                written = json.load(f)
            self.assertEqual(
                written["env"]["ANTHROPIC_BASE_URL"],
                "http://127.0.0.1:9527")


class TestSetupAtomicWriteFailure(unittest.TestCase):
    def test_atomic_write_returning_false_yields_failed(self):
        """Line 97: atomic_write returns False → action 'failed'."""
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value={}), \
                 patch.dict("shared.config_store.PATHS",
                            {"claude_settings": settings_path}), \
                 patch("services.claude_code_setup.config_store.atomic_write",
                       return_value=False):
                result = claude_code_setup.setup()
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "failed")


# ── bridge_protocol.py: _plain dict exception (lines 39-40) ───────────

class _HostileDict(dict):
    """A dict subclass whose items() raises — must be neutralized by _plain."""

    def items(self):
        raise RuntimeError("boom")


class TestPlainDictException(unittest.TestCase):
    def test_dict_subclass_with_raising_items_returns_empty_dict(self):
        """Lines 39-40: when .items() raises inside _plain's dict branch,
        it returns {} instead of propagating."""
        hostile = _HostileDict({"type": "dirtyState", "payload": {"dirty": True}})
        result = bridge_protocol._plain(hostile)
        self.assertEqual(result, {})


# ── bridge_protocol.py: handle_message catch-all (lines 67-69) ────────

class TestHandleMessageCatchAll(unittest.TestCase):
    def test_dispatch_exception_caught_and_logged(self):
        """Lines 67-69: if _dispatch raises, handle_message catches it,
        logs, and returns []."""
        core = bridge_protocol.BridgeCore()
        with patch.object(core, "_dispatch",
                          side_effect=RuntimeError("dispatch boom")), \
             self.assertLogs("magic-proxy.bridge", level="ERROR"):
            result = core.handle_message({"type": "anything"})
        self.assertEqual(result, [])


# ── capture_controller.py: status + error_msg properties (lines 67, 71)

class TestCaptureControllerProperties(unittest.TestCase):
    def test_status_property_proxies_monitor(self):
        """Line 67: .status returns monitor.status."""
        monitor = MagicMock()
        monitor.status = "running"
        ctrl = CaptureController(monitor, config_fn=lambda: {})
        self.assertEqual(ctrl.status, "running")

    def test_error_msg_property_proxies_monitor(self):
        """Line 71: .error_msg returns monitor.error_msg."""
        monitor = MagicMock()
        monitor.error_msg = "something broke"
        ctrl = CaptureController(monitor, config_fn=lambda: {})
        self.assertEqual(ctrl.error_msg, "something broke")


if __name__ == "__main__":
    unittest.main()


class TestProviderTemplatesEndpoint(unittest.TestCase):
    """#51：UI 供应商模板单一真源 = PROVIDER_REGISTRY——JS 侧不再平行
    编码 base_url/anthropic_native。"""

    def test_templates_served_from_registry(self):
        self.server, self.port = _start_server()
        try:
            status, data = _request(
                self.port, "GET", "/api/provider-templates",
                token=self.server._token)
            self.assertEqual(status, 200)
            import json as _json
            tpl = _json.loads(data)
            ids = [t["id"] for t in tpl]
            self.assertIn("deepseek", ids)
            self.assertIn("glm", ids)
            self.assertIn("kimi", ids)
            self.assertIn("custom", ids)
            glm = next(t for t in tpl if t["id"] == "glm")
            self.assertEqual(glm["base_url"],
                             "https://open.bigmodel.cn/api/anthropic")
            self.assertTrue(glm["anthropic_native"])
        finally:
            self.server.stop()
