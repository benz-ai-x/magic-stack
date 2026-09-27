"""vpn/openvpn_client.py —— 状态机映射/错误分类/凭证注入/流量折叠。

不打真实子进程：事件面直打（tests/test_intents.py 的同款口径），进程层
崩溃翻译用 poll() 恒非零的假进程对象驱动基类 check() 路径。
"""
import unittest

from vpn.openvpn_client import (
    ERR_AUTH_CHALLENGE,
    ERR_AUTH_FAILED,
    ERR_AUTH_REQUIRED,
    ERR_CIPHER,
    ERR_CRASHED,
    ERR_MGMT_LOST,
    VpnClient,
    classify_log,
)


class StubMgmt:
    def __init__(self):
        self.sent = []
        self.alive = True

    def send(self, line):
        self.sent.append(line)

    def send_and_wait(self, line, timeout=None):
        self.sent.append(line)
        return True, ""

    def close(self):
        self.alive = False


def _client(**kw):
    changes, errors = [], []
    creds = kw.pop("credentials", lambda: ("alice", "s3cret"))
    client = VpnClient(
        full_cmd=["/usr/bin/true"],
        mgmt_port=17511,
        credentials=creds,
        on_state_change=changes.append,
        on_error=lambda kind, text: errors.append(kind),
        **kw)
    return client, changes, errors


class TestClassifyLog(unittest.TestCase):
    def test_cipher_negotiation_beats_generic_auth_failed(self):
        self.assertEqual(classify_log(
            "AUTH_FAILED,Data channel cipher negotiation failed (no shared cipher)"),
            ERR_CIPHER)

    def test_cert_expired(self):
        self.assertEqual(classify_log(
            "VERIFY ERROR: depth=1, error=certificate has expired: /CN=ca"),
            "cert_expired")

    def test_tls_and_unreachable(self):
        self.assertEqual(classify_log("TLS Error: TLS key negotiation failed"),
                         "tls_error")
        self.assertEqual(classify_log("RESOLVE_ERROR pin vpn.example.com"),
                         "unreachable")

    def test_generic_auth_failed(self):
        self.assertEqual(classify_log("AUTH_FAILED,session expired"), ERR_AUTH_FAILED)

    def test_benign(self):
        self.assertEqual(classify_log("Initialization Sequence Completed"), "")
        self.assertEqual(classify_log(""), "")


class TestStateMapping(unittest.TestCase):
    def test_connected_success(self):
        client, changes, _ = _client()
        client._on_state({"name": "CONNECTED", "desc": "SUCCESS",
                          "tun_ip": "10.8.0.2", "ts": "1758936000"})
        self.assertEqual(client.vpn.status, "connected")
        self.assertEqual(client.vpn.tun_ip, "10.8.0.2")
        self.assertEqual(client.vpn.since, "1758936000")
        self.assertTrue(changes)

    def test_connected_route_error_not_green(self):
        client, _, _ = _client()
        client._on_state({"name": "CONNECTED", "desc": "ROUTE_ERROR",
                          "tun_ip": "10.8.0.2"})
        self.assertNotEqual(client.vpn.status, "connected")

    def test_reconnecting_then_exiting(self):
        client, _, _ = _client()
        client._on_state({"name": "CONNECTED", "desc": "SUCCESS", "tun_ip": ""})
        client._on_state({"name": "RECONNECTING", "desc": "ping-restart"})
        self.assertEqual(client.vpn.status, "reconnecting")
        client._on_state({"name": "EXITING", "desc": "sigterm"})
        self.assertEqual(client.vpn.status, "exiting")

    def test_exiting_ignored_on_user_stop(self):
        client, _, _ = _client()
        client._user_stop = True
        client._on_state({"name": "EXITING", "desc": "SIGTERM"})
        self.assertNotEqual(client.vpn.status, "exiting")


class TestCredentials(unittest.TestCase):
    def test_full_auth_pair(self):
        client, _, _ = _client()
        stub = StubMgmt()
        client._mgmt = stub
        client._on_password_need({"kind": "Auth", "need_password": True,
                                  "challenge": ""})
        self.assertEqual(stub.sent, ['username "Auth" alice',
                                     'password "Auth" s3cret'])

    def test_username_only_variant(self):
        client, _, _ = _client()
        stub = StubMgmt()
        client._mgmt = stub
        client._on_password_need({"kind": "Auth", "need_password": False,
                                  "challenge": ""})
        self.assertEqual(stub.sent, ['username "Auth" alice'])

    def test_private_key(self):
        client, _, _ = _client()
        stub = StubMgmt()
        client._mgmt = stub
        client._on_password_need({"kind": "Private Key",
                                  "need_password": True, "challenge": ""})
        self.assertEqual(stub.sent, ['password "Private Key" s3cret'])

    def test_no_credentials_fails_with_code(self):
        client, _, errors = _client(credentials=lambda: None)
        client._mgmt = StubMgmt()
        client._on_password_need({"kind": "Auth", "need_password": True,
                                  "challenge": ""})
        self.assertEqual(client.vpn.error_kind, ERR_AUTH_REQUIRED)
        self.assertIn(ERR_AUTH_REQUIRED, errors)

    def test_static_challenge_unsupported(self):
        client, _, errors = _client()
        client._mgmt = StubMgmt()
        client._on_password_need({"kind": "Auth", "need_password": True,
                                  "challenge": "1,Please enter token PIN"})
        self.assertEqual(client.vpn.error_kind, ERR_AUTH_CHALLENGE)
        self.assertIn(ERR_AUTH_CHALLENGE, errors)

    def test_password_failed_retry_cap(self):
        client, _, errors = _client()
        client._mgmt = StubMgmt()
        for _ in range(3):
            client._on_password_failed("'Auth'")
        self.assertEqual(client.vpn.error_kind, ERR_AUTH_FAILED)
        self.assertEqual(errors.count(ERR_AUTH_FAILED), 1)  # 首错保留不叠加


class TestBytecount(unittest.TestCase):
    def test_counter_reset_folds_into_base(self):
        client, _, _ = _client()
        client._on_bytecount((100, 200))
        client._on_bytecount((150, 260))
        snap = client.traffic_snapshot()
        self.assertEqual((snap["bytes_in"], snap["bytes_out"]), (150, 260))
        # 重连归零：新值小于旧值 → 旧值折叠进基数，总量单调不减
        client._on_bytecount((10, 20))
        snap = client.traffic_snapshot()
        self.assertEqual((snap["bytes_in"], snap["bytes_out"]), (160, 280))


class TestLogIngestion(unittest.TestCase):
    def test_log_lines_land_in_window_and_classify(self):
        client, _, _ = _client()
        client._on_log({"ts": "1", "flags": "F",
                        "text": "AUTH_FAILED,Data channel cipher negotiation failed"})
        self.assertIn("cipher", client.snapshot_log_lines()[-1])
        self.assertEqual(client._log_error_kind, ERR_CIPHER)

    def test_first_classification_wins(self):
        client, _, _ = _client()
        client._on_log({"ts": "1", "flags": "F",
                        "text": "TLS Error: TLS key negotiation failed"})
        client._on_log({"ts": "2", "flags": "F", "text": "AUTH_FAILED,x"})
        self.assertEqual(client._log_error_kind, "tls_error")


class TestCrashTranslation(unittest.TestCase):
    class _DeadProc:
        """poll() 恒返回非零——驱动基类 check() 的退出翻译路径。"""
        returncode = 1

        def poll(self):
            return 1

    def _dead_process(self):
        return self._DeadProc()

    def test_unexpected_exit_becomes_tunnel_error(self):
        client, _, errors = _client()
        client.process = self._dead_process()
        client.check()
        self.assertEqual(client.vpn.status, "error")
        self.assertEqual(client.vpn.error_kind, ERR_CRASHED)
        self.assertIn(ERR_CRASHED, errors)

    def test_classified_kind_preferred_over_crashed(self):
        client, _, _ = _client()
        client._log_error_kind = ERR_CIPHER
        client.process = self._dead_process()
        client.check()
        self.assertEqual(client.vpn.error_kind, ERR_CIPHER)

    def test_user_stop_never_error(self):
        client, _, errors = _client()
        client._user_stop = True
        client.process = self._dead_process()
        client.check()
        self.assertNotEqual(client.vpn.status, "error")
        self.assertEqual(errors, [])

    def test_mgmt_lost_marks_error(self):
        client, _, errors = _client()
        client._on_mgmt_disconnected()
        self.assertEqual(client.vpn.error_kind, ERR_MGMT_LOST)
        self.assertIn(ERR_MGMT_LOST, errors)

    def test_mgmt_disconnected_ignored_when_exiting(self):
        client, _, errors = _client()
        client.vpn.status = "exiting"
        client._on_mgmt_disconnected()
        self.assertEqual(errors, [])

    def test_start_failure_structured(self):
        client, _, _ = _client()
        client._full_cmd = ["/nonexistent/openvpn"]
        self.assertFalse(client.start())
        self.assertEqual(client.vpn.status, "error")
        self.assertEqual(client.vpn.error_kind, "start_failed")


class TestSnapshot(unittest.TestCase):
    def test_snapshot_shape(self):
        client, _, _ = _client()
        client._on_state({"name": "CONNECTED", "desc": "SUCCESS",
                          "tun_ip": "10.8.0.7", "ts": "123"})
        client._on_bytecount((5, 6))
        snap = client.snapshot()
        self.assertEqual(snap["status"], "connected")
        self.assertEqual(snap["tun_ip"], "10.8.0.7")
        self.assertIn("bytes_in", snap)
        self.assertIn("rate_in", snap)


if __name__ == "__main__":
    unittest.main()
