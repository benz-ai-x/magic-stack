"""日志卫生与异常可见性（真机九轮排查的产品化收口，2026-09-27）。

- 测试进程绝不写用户真实日志（root handler 共享曾把测试输出写进
  MagicProxy.log，两轮诊断被 MagicMock/假服务器行带偏）
- windowed 应用的异常黑洞：线程/解释器钩子 + stderr 重定向进日志
- :9528 的 401 静默拒绝必须留痕（「用户在点死页面」服务端可查证）
"""
import json
import logging
import logging.handlers
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

import app  # noqa: F401 — 触发 _setup_logging 的 pytest 守卫路径


class TestNoTestLogPollution(unittest.TestCase):
    def test_pytest_never_attaches_file_handler(self):
        handlers = [h for h in logging.getLogger().handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        self.assertEqual(handlers, [],
                         "pytest 进程不得挂真实日志文件 handler")


class TestExcepthooks(unittest.TestCase):
    def test_thread_hook_logs_without_raising(self):
        with self.assertLogs("magic-proxy.app", level="ERROR") as cm:
            try:
                raise RuntimeError("boom")
            except RuntimeError as exc:
                import sys as _sys
                app._thread_excepthook(type("Args", (), {
                    "thread": threading.current_thread(),
                    "exc_type": RuntimeError,
                    "exc_value": exc,
                    "exc_traceback": exc.__traceback__,
                })())
        self.assertIn("boom", "\n".join(cm.output))


class TestAuthRejectLogging(unittest.TestCase):
    def test_unauthorized_api_request_is_logged(self):
        from services import config_server
        import tempfile as tf
        from shared import config_store
        tmp = tf.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mp = os.path.join(tmp.name, "mp.json")
        with open(mp, "w") as fh:
            fh.write(json.dumps({"servers": []}))
        with patch.dict(config_store.PATHS, {"mp": mp}):
            s = config_server.ConfigServer()
            s._server = config_server._ThreadingHTTPServer(
                ("127.0.0.1", 0), config_server._Handler,
                expected_token=s._token)
            port = s._server.server_address[1]
            t = threading.Thread(target=s._server.serve_forever, daemon=True)
            t.start()
            try:
                with self.assertLogs("magic-proxy.config_server",
                                     level="INFO") as cm:
                    conn = HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("POST", "/api/vpn-connect",
                                 body=json.dumps({}),
                                 headers={"Authorization": "Bearer wrong"})
                    resp = conn.getresponse()
                    resp.read()
                    conn.close()
                self.assertEqual(resp.status, 401)
                joined = "\n".join(cm.output)
                self.assertIn("auth rejected", joined)
                self.assertIn("/api/vpn-connect", joined)
            finally:
                s._server.shutdown()


if __name__ == "__main__":
    unittest.main()
