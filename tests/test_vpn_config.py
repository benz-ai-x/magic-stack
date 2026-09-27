"""mpconf services.openvpn 配置模型——归一/访问器/服务器接线。"""
import unittest

from mpconf.config import (
    _normalize_server,
    normalize_openvpn,
    server_openvpn,
)


class TestNormalizeOpenvpn(unittest.TestCase):
    def test_garbage_input_gets_defaults(self):
        for bad in (None, "x", [], 42):
            defaults = normalize_openvpn(bad)
            self.assertEqual(defaults["enabled"], False)
            self.assertEqual(defaults["profile_set"], False)
            self.assertEqual(defaults["auth"], "none")
            self.assertEqual(defaults["username"], "")
            self.assertEqual(defaults["password_set"], False)
            self.assertEqual(defaults["pull_dns"], True)
            self.assertEqual(defaults["autostart"], False)

    def test_bad_auth_falls_back_to_none(self):
        self.assertEqual(normalize_openvpn({"auth": "poodle"})["auth"], "none")
        self.assertEqual(
            normalize_openvpn({"auth": "userpass"})["auth"], "userpass")

    def test_shape_preserved(self):
        row = {"enabled": True, "profile_set": True, "auth": "userpass",
               "username": " alice ", "password_set": True,
               "pull_dns": False, "autostart": True}
        out = normalize_openvpn(row)
        self.assertTrue(out["enabled"])
        self.assertEqual(out["username"], "alice")
        self.assertFalse(out["pull_dns"])
        self.assertTrue(out["autostart"])


class TestServerWiring(unittest.TestCase):
    def test_normalize_server_fills_openvpn_key(self):
        out = _normalize_server({"id": "t-abc", "ssh": {"host": "h"}})
        self.assertIn("openvpn", out["services"])
        self.assertEqual(out["services"]["openvpn"]["auth"], "none")

    def test_normalize_server_keeps_openvpn_values(self):
        raw = {"id": "t-abc", "ssh": {"host": "h"},
               "services": {"openvpn": {"enabled": True,
                                        "auth": "userpass"}}}
        out = _normalize_server(raw)
        self.assertTrue(out["services"]["openvpn"]["enabled"])
        self.assertEqual(out["services"]["openvpn"]["auth"], "userpass")

    def test_server_openvpn_accessor(self):
        server = _normalize_server({"id": "t-abc"})
        self.assertEqual(server_openvpn(server)["enabled"], False)
        self.assertEqual(server_openvpn(None), {})
        self.assertEqual(server_openvpn({"services": None}), {})

    def test_legacy_servers_without_services_key(self):
        # v1 迁移路径：无 services 的服务器 → openvpn 默认值补齐
        out = _normalize_server({"id": "t-x", "ssh": {"host": "h"}})
        self.assertFalse(out["services"]["openvpn"]["profile_set"])


if __name__ == "__main__":
    unittest.main()
