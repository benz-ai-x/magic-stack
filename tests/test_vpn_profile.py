"""vpn/profile.py —— .ovpn 解析/净化真值表（安全模型第二道闸的钉子）。"""
import unittest

from vpn.profile import (
    MANAGEMENT_DIRECTIVES,
    SCRIPT_DIRECTIVES,
    parse_profile,
    sanitize_profile,
)

PROFILE = """\
client
dev tun
proto udp
remote vpn.example.com 1194
remote 10.0.0.1 1195 tcp
resolv-retry infinite
auth-user-pass
<ca>
-----BEGIN CERTIFICATE-----
up /this/looks/like/a/directive
management 127.0.0.1 6666
-----END CERTIFICATE-----
</ca>
<key>
-----BEGIN ENCRYPTED PRIVATE KEY-----
down /tmp/evil.sh
-----END ENCRYPTED PRIVATE KEY-----
</key>
up /tmp/evil-up.sh
down /tmp/evil-down.sh
script-security 2
tls-verify /tmp/verify.sh
plugin /tmp/p.so
management 127.0.0.1 9999
management-hold
verb 3
"""


class TestParse(unittest.TestCase):
    def test_remotes_and_proto(self):
        info = parse_profile(PROFILE)
        self.assertEqual(info.remotes,
                         [("vpn.example.com", "1194"), ("10.0.0.1", "1195")])
        self.assertEqual(info.proto, "udp")
        self.assertFalse(info.error)

    def test_inline_credentials_detection(self):
        info = parse_profile(PROFILE)
        # auth-user-pass 裸指令（查询式）——<key> 内嵌不算 auth inline
        self.assertTrue(info.auth_user_pass)
        self.assertFalse(info.auth_inline)
        self.assertTrue(info.needs_credentials)

    def test_inline_auth_block(self):
        text = "client\nremote h 1\n<auth-user-pass>\nuser\npw\n</auth-user-pass>\n"
        info = parse_profile(text)
        self.assertTrue(info.auth_inline)
        self.assertFalse(info.needs_credentials)

    def test_no_remote_is_invalid(self):
        info = parse_profile("client\ndev tun\n")
        self.assertEqual(info.error, "no_remote")

    def test_connection_block_counts_as_endpoint(self):
        info = parse_profile("client\n<connection>\nremote h 1\n</connection>\n")
        self.assertTrue(info.has_connection_block)
        self.assertFalse(info.error)

    def test_missing_client_role_warning(self):
        info = parse_profile("remote h 1\n")
        self.assertTrue(info.missing_client_role)
        info = parse_profile("client\nremote h 1\n")
        self.assertFalse(info.missing_client_role)


class TestSanitize(unittest.TestCase):
    def setUp(self):
        self.clean, self.info = sanitize_profile(PROFILE)

    def test_script_directives_stripped(self):
        # 顶层脚本指令剥除——用脚本路径本身作判据（inline 块内允许出现
        # 指令形状的行，不能拿指令名全局断言）
        for evil in ("/tmp/evil-up.sh", "/tmp/evil-down.sh",
                     "/tmp/verify.sh", "/tmp/p.so", "script-security"):
            self.assertNotIn(evil, self.clean)
        removed = {d for d, _ in self.info.removed}
        self.assertTrue({"up", "down", "script-security",
                         "tls-verify", "plugin"} <= removed)

    def test_management_directives_stripped(self):
        self.assertNotIn("127.0.0.1 9999", self.clean)  # 顶层 management 行
        removed = {d for d, _ in self.info.removed}
        self.assertIn("management", removed)
        self.assertIn("management-hold", removed)

    def test_inline_blocks_survive_untouched(self):
        # <ca>/<key> 块内指令形状的行是证书/私钥内容——绝不误伤
        self.assertIn("up /this/looks/like/a/directive", self.clean)
        self.assertIn("down /tmp/evil.sh", self.clean)
        self.assertIn("-----BEGIN CERTIFICATE-----", self.clean)

    def test_benign_directives_survive(self):
        for keep in ("client", "dev tun", "proto udp",
                     "remote vpn.example.com 1194", "verb 3"):
            self.assertIn(keep, self.clean)

    def test_auth_user_pass_file_arg_normalized_to_query(self):
        text = "client\nremote h 1\nauth-user-pass /tmp/creds.txt\n"
        clean, info = sanitize_profile(text)
        self.assertIn("\nauth-user-pass\n", clean)
        self.assertNotIn("/tmp/creds.txt", clean)
        self.assertEqual(info.removed[0][0], "auth-user-pass")
        self.assertTrue(info.needs_credentials)

    def test_directive_tables_are_disjoint(self):
        self.assertFalse(SCRIPT_DIRECTIVES & MANAGEMENT_DIRECTIVES)


if __name__ == "__main__":
    unittest.main()
