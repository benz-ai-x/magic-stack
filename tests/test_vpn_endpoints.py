"""M2 接线——VPN 设置窗端点 + 意图面（spec §7.2）。

config server 端点经真 HTTP（_start_server 同款脚手架）；connect/
disconnect 是注入回调 seam 的薄翻译，重点钉：index 解析、profile_set
拒连、互斥拒连（force 翻越）、意图线程纪律。
"""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from services import config_server
from services.intents import UserIntents
from shared import config_store


def _start_server(**attrs):
    import threading
    s = config_server.ConfigServer()
    # attrs 打在包装层（与生产 app.py 同款）——经 _forward_seams 转发
    # 到内层，测试恒走真实接线路径（P0-1 的掩蔽就此结构性消灭）
    for k, v in attrs.items():
        setattr(s, k, v)
    s._server = config_server._ThreadingHTTPServer(
        ("127.0.0.1", 0), config_server._Handler, expected_token=s._token)
    s._forward_seams()
    port = s._server.server_address[1]
    s._thread = threading.Thread(target=s._server.serve_forever, daemon=True)
    s._thread.start()
    return s, port


def _request(port, method, path, body=None, token="x"):
    from http.client import HTTPConnection
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, (json.loads(payload) if payload.strip() else {})


_CFG = {
    "servers": [
        {"id": "t-aaa", "name": "vpnbox", "ssh": {"host": "h1"},
         "services": {"openvpn": {"profile_set": True, "auth": "none",
                                  "pull_dns": True}}},
        {"id": "t-bbb", "name": "plain", "ssh": {"host": "h2"},
         "services": {}},
    ],
    "proxy_server_id": "t-aaa",
}


class TestVpnEndpoints(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # 双重定向：mp 配置 + profile 目录（config_store 纪律）
        self._mp_path = os.path.join(self._tmp.name, "mp.json")
        with open(self._mp_path, "w") as fh:
            fh.write(json.dumps(_CFG))
        p1 = patch.dict(config_store.PATHS, {"mp": self._mp_path})
        p2 = patch.dict(config_store.PATHS,
                        {"vpn_profiles_dir": self._tmp.name})
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        self.connect_calls = []
        self.disconnect_calls = []
        self.install_impl = lambda tunnel: (True, "")
        self.server, self.port = _start_server(
            vpn_connect_fn=self._connect,
            vpn_disconnect_fn=self._disconnect,
            vpn_install_fn=lambda t: self.install_impl(t))
        self.token = self.server._token

    def _connect(self, tunnel, force=False):
        self.connect_calls.append((tunnel.get("id"), force))
        return {"ok": True}

    def _disconnect(self):
        self.disconnect_calls.append(True)
        return {"ok": True}

    def _post(self, path, body):
        return _request(self.port, "POST", path, body, self.token)

    def test_profile_import_sanitizes_and_persists(self):
        content = ("client\nremote vpn.example.com 1194\n"
                   "auth-user-pass /tmp/creds.txt\nup /tmp/evil.sh\n")
        code, d = self._post("/api/vpn-profile",
                             {"index": 0, "content": content})
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assertIn("up", d["removed"])           # 脚本指令剥除回执
        self.assertTrue(d["needs_credentials"])     # auth-user-pass → 查询式
        from vpn import profile_store
        saved = profile_store.load_profile("t-aaa")
        self.assertIn("remote vpn.example.com 1194", saved)
        self.assertNotIn("/tmp/evil.sh", saved)
        # profile_set 即时持久化（事务写径）——app 重启不再清零导入状态
        self.assertTrue(d.get("persisted"))
        disk = json.loads(open(self._mp_path).read())
        self.assertIs(disk["servers"][0]["services"]["openvpn"]
                      ["profile_set"], True)

    def test_profile_import_rejects_no_remote(self):
        code, d = self._post("/api/vpn-profile",
                             {"index": 0, "content": "client\ndev tun\n"})
        self.assertEqual(code, 400)
        self.assertFalse(d.get("ok", False))

    def test_profile_import_rejects_bad_body(self):
        code, _ = self._post("/api/vpn-profile", {"index": 0, "content": ""})
        self.assertEqual(code, 400)
        code, _ = self._post("/api/vpn-profile", {"index": True, "content": "x"})
        self.assertEqual(code, 400)

    def test_credentials_go_to_keychain(self):
        with patch("services.config_server.keychain") as kc:
            kc.set_vpn_password.return_value = True
            code, d = self._post("/api/vpn-credentials",
                                 {"index": 0, "password": "s3cret"})
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(kc.set_vpn_password.call_count, 1)

    def test_credentials_reject_empty(self):
        code, _ = self._post("/api/vpn-credentials", {"index": 0, "password": ""})
        self.assertEqual(code, 400)

    def test_install_requires_imported_profile(self):
        # no_profile 由协调器判定，端点只映射 400（安装序列归宿
        # vpn/coordinator——见 test_vpn_coordinator）
        self.install_impl = lambda t: (False, "no_profile")
        code, d = self._post("/api/vpn-install", {"index": 1})
        self.assertEqual(code, 400)

    def test_install_delegates_and_passes_code(self):
        calls = []
        self.install_impl = lambda t: calls.append(t) or (False, "cancelled")
        code, d = self._post("/api/vpn-install", {"index": 0})
        self.assertEqual(code, 200)
        self.assertFalse(d["ok"])
        self.assertEqual(d["error_code"], "cancelled")
        self.assertEqual(calls[0].get("id"), "t-aaa")  # 按磁盘真相解行

    def test_connect_seam_passthrough(self):
        # server_id 稳定寻址（R7-C2）；index 兼容回退仍在（旧载荷）
        code, d = self._post("/api/vpn-connect",
                             {"server_id": "t-aaa", "force": True})
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(self.connect_calls, [("t-aaa", True)])
        code, d = self._post("/api/vpn-connect", {"index": 1, "force": True})
        self.assertEqual(self.connect_calls[-1], ("t-bbb", True))
        code, d = self._post("/api/vpn-connect",
                             {"server_id": "t-gone", "force": True})
        self.assertEqual(code, 400)      # 未知 id 显式拒，不再静默错位

    def test_connect_without_seam_503(self):
        server, port = _start_server()
        token = server._token
        code, _ = _request(port, "POST", "/api/vpn-connect", {"index": 0}, token)
        self.assertEqual(code, 503)

    def test_disconnect_seam(self):
        code, d = self._post("/api/vpn-disconnect", {})
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(self.disconnect_calls, [True])

    def test_state_carries_vpn_decoration(self):
        code, d = _request(self.port, "GET", "/api/state", token=self.token)
        self.assertEqual(code, 200)
        self.assertIn("vpn_state", d["mp"])
        self.assertEqual(d["mp"]["vpn_state"], {})  # 无连接 → 空块


class TestVpnIntents(unittest.TestCase):
    def _intents(self, spawn):
        return UserIntents(
            conn=MagicMock(), mounts=MagicMock(),
            notify=lambda *a: None, mark_dirty=lambda: None,
            update_mp=lambda mut: True, reload_config=lambda: True,
            spawn=spawn,
            vpn_connect=lambda server: self.connected.append(server),
            vpn_disconnect=lambda: self.disconnected.append(True))

    def setUp(self):
        self.connected, self.disconnected = [], []

    def test_connect_spawns_background(self):
        ran = []
        intents = self._intents(lambda target, name: ran.append((target, name)))
        intents.vpn_connect({"id": "t-x"})
        self.assertEqual(self.connected, [])      # spawn 注入 → 不直跑
        target, name = ran[0]
        self.assertIn("Vpn", name)
        target()                                   # 零参闭包（server 已捕获）
        self.assertEqual(self.connected, [{"id": "t-x"}])

    def test_disconnect_spawns_background(self):
        ran = []
        intents = self._intents(lambda target, name: ran.append((target, name)))
        intents.vpn_disconnect()
        self.assertEqual(self.disconnected, [])
        ran[0][0]()
        self.assertEqual(self.disconnected, [True])


if __name__ == "__main__":
    unittest.main()
