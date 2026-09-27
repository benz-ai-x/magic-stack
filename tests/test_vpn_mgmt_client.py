"""vpn/mgmt_client.py —— 管理口行协议：纯函数 + socketpair 假服务器全链路。"""
import socket
import threading
import time
import unittest
from unittest import mock

from vpn import mgmt_client
from vpn.mgmt_client import (
    ManagementClient,
    ManagementError,
    parse_bytecount,
    parse_log_line,
    parse_password_need,
    parse_state_line,
    quote_arg,
)

WELCOME = (b"SUCCESS: password is correct\n"
           b">INFO:OpenVPN Management Interface Version 6 -- "
           b"type 'help' for more info\n")


class FakeMgmtServer(threading.Thread):
    """脚本化假 openvpn 管理口：密码握手 + 按命令回放响应/事件。"""

    def __init__(self, sock, password="s3cret"):
        super().__init__(daemon=True)
        self.sock = sock
        self.password = password
        self.received = []
        self.events_to_push = []      # 连接建立后主动推送的事件行
        self._setup_done = threading.Event()

    def run(self):
        f = self.sock.makefile("rwb")
        try:
            self._serve(f)
        except OSError:
            pass  # 客户端先关连接属正常收尾——线程绝不带警告退出
        finally:
            try:
                f.close()
            except OSError:
                pass

    def _serve(self, f):
        # openvpn 实测：提示符无换行（行导向解析等不到——曾致真机握手超时）
        f.write(b"ENTER PASSWORD:")
        f.flush()
        f.readline()  # 密码行
        f.write(WELCOME)
        f.flush()
        for line in self.events_to_push:
            f.write(line.encode() + b"\n")
        f.flush()
        self._setup_done.set()
        while True:
            raw = f.readline()
            if not raw:
                break
            cmd = raw.decode("utf-8", "replace").strip()
            if not cmd:
                continue
            self.received.append(cmd)
            if cmd.startswith("version"):
                f.write(b"SUCCESS: ...version acknowledged...\n")
            elif cmd == "hold off":
                f.write(b"SUCCESS: hold flag set to OFF\n")
            elif cmd == "hold release":
                f.write(b"SUCCESS: hold release succeeded\n")
            elif cmd.startswith("state on all"):
                f.write(b">STATE:1758936000,CONNECTING,,,\n"
                        b"SUCCESS: real-time state notification set to ON\n")
            elif cmd.startswith("log on all"):
                f.write(b">LOG:1758936000,I,OpenVPN 2.7.7 ...\n"
                        b"SUCCESS: real-time log notification set to ON\n")
            elif cmd.startswith("bytecount"):
                f.write(b"SUCCESS: bytecount interval changed\n")
            elif cmd == "signal SIGTERM":
                f.write(b"SUCCESS: signal SIGTERM thrown\n"
                        b">STATE:1758936001,EXITING,SIGTERM,connection exited\n")
            elif cmd.startswith("username") or cmd.startswith("password"):
                f.write(b"SUCCESS: 'Auth' credentials entered\n")
            else:
                f.write(b"ERROR: unknown command\n")
            f.flush()
        f.close()


def _wait_for(predicate, timeout=2.0, interval=0.02):
    """eventually-consistent 等待：socketpair 收账依赖服务器线程被调度。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _connected_client(handlers, password="s3cret", push=()):
    """socketpair + 假服务器 + 完成握手的管理客户端（测试脚手架）。"""
    a, b = socket.socketpair()
    server = FakeMgmtServer(b, password=password)
    server.events_to_push = list(push)
    server.start()
    with mock.patch.object(mgmt_client.socket, "create_connection",
                           return_value=a):
        client = ManagementClient(password=password or None,
                                  handlers=handlers, handshake_timeout=2.0)
        client.connect("127.0.0.1", 17511)
    server._setup_done.wait(timeout=2.0)
    return client, server


class TestPureParsers(unittest.TestCase):
    def test_quote_arg_plain(self):
        self.assertEqual(quote_arg("alice"), "alice")

    def test_quote_arg_specials(self):
        self.assertEqual(quote_arg("a b"), '"a b"')
        self.assertEqual(quote_arg('a"b'), '"a\\"b"')
        self.assertEqual(quote_arg("a\\b"), '"a\\\\b"')
        self.assertEqual(quote_arg(""), '""')

    def test_quote_arg_rejects_newline(self):
        with self.assertRaises(ValueError):
            quote_arg("a\nb")

    def test_parse_state_line_nine_fields_with_empties(self):
        parsed = parse_state_line(
            "1758936000,CONNECTED,SUCCESS,10.8.0.2,203.0.113.7,443,192.168.1.5,52311,")
        self.assertEqual(parsed["name"], "CONNECTED")
        self.assertEqual(parsed["tun_ip"], "10.8.0.2")
        self.assertEqual(parsed["remote_ip"], "203.0.113.7")
        self.assertEqual(parsed["tun_ipv6"], "")

    def test_parse_state_line_short(self):
        parsed = parse_state_line("1758936000,CONNECTING,,")
        self.assertEqual(parsed["name"], "CONNECTING")
        self.assertEqual(parsed["tun_ip"], "")

    def test_parse_bytecount(self):
        self.assertEqual(parse_bytecount("123,456"), (123, 456))
        self.assertIsNone(parse_bytecount("abc,456"))
        self.assertIsNone(parse_bytecount("1,2,3"))

    def test_parse_log_line(self):
        parsed = parse_log_line("1758936000,F,_fatal text, with commas")
        self.assertEqual(parsed["flags"], "F")
        self.assertEqual(parsed["text"], "_fatal text, with commas")

    def test_parse_password_need_variants(self):
        both = parse_password_need("Need 'Auth' username/password")
        self.assertEqual(both["kind"], "Auth")
        self.assertTrue(both["need_password"])
        self.assertEqual(both["challenge"], "")
        user_only = parse_password_need("Need 'Auth' username")
        self.assertFalse(user_only["need_password"])
        pk = parse_password_need("Need 'Private Key' password")
        self.assertEqual(pk["kind"], "Private Key")
        self.assertTrue(pk["need_password"])
        sc = parse_password_need(
            "Need 'Auth' username/password SC:1,Please enter token PIN")
        self.assertEqual(sc["challenge"], "1,Please enter token PIN")
        self.assertTrue(sc["echo"])


class TestWireProtocol(unittest.TestCase):
    def test_handshake_and_events(self):
        events = {"state": [], "log": []}
        push = [">STATE:1758936000,WAIT,,",
                ">BYTECOUNT:1000,2000",
                ">HOLD:Waiting for hold release:10"]
        client, server = _connected_client(
            {"state": events["state"].append,
             "log": events["log"].append,
             "bytecount": lambda p: events.setdefault("bc", p),
             "hold": lambda p: events.setdefault("hold", p)},
            push=push)
        try:
            self.assertTrue(_wait_for(lambda: "version 4" in server.received))
            ok, _ = client.send_and_wait("hold release")
            self.assertTrue(ok)
            self.assertTrue(_wait_for(lambda: "hold release" in server.received))
            ok, _ = client.send_and_wait("state on all")
            self.assertTrue(ok)
            self.assertTrue(
                _wait_for(lambda: "state on all" in server.received))
            # 回放（假服务器推送）+ 命令响应（假服务器再推一条 STATE）
            names = [e["name"] for e in events["state"]]
            self.assertIn("WAIT", names)
            self.assertIn("CONNECTING", names)
            self.assertEqual(events["bc"], (1000, 2000))
            self.assertEqual(events["hold"], "Waiting for hold release:10")
        finally:
            client.close()

    def test_command_error(self):
        client, server = _connected_client({})
        try:
            ok, _ = client.send_and_wait("bogus command")
            self.assertFalse(ok)
        finally:
            client.close()

    def test_credentials_command_shape(self):
        client, server = _connected_client({})
        try:
            client.send('username "Auth" ' + quote_arg("a b"))
            client.send('password "Auth" ' + quote_arg('p"w'))
            client.send('password "Private Key" ' + quote_arg("pk\\1"))
            self.assertTrue(_wait_for(lambda: len(server.received) >= 3))
        finally:
            client.close()
        self.assertIn('username "Auth" "a b"', server.received)
        self.assertIn('password "Auth" "p\\"w"', server.received)
        self.assertIn('password "Private Key" "pk\\\\1"', server.received)

    def test_disconnect_event_on_close(self):
        flag = {"gone": False}
        a, b = socket.socketpair()

        def serve():
            # makefile 持有 socket 的 _io_refs——测试侧直接 b.close() 会被
            # 推迟成空操作（不产生 EOF）；由服务器侧先关 f 再关 b 才是真关
            f = b.makefile("rwb")
            f.write(b"ENTER PASSWORD:")
            f.flush()
            f.readline()
            f.write(WELCOME)
            f.flush()
            f.readline()   # version 4
            f.close()
            b.close()

        threading.Thread(target=serve, daemon=True).start()
        with mock.patch.object(mgmt_client.socket, "create_connection",
                               return_value=a):
            client = ManagementClient(
                password="s3cret",
                handlers={"disconnected": lambda: flag.__setitem__("gone", True)},
                handshake_timeout=2.0)
            client.connect("127.0.0.1", 17511)
        self.assertTrue(_wait_for(lambda: flag["gone"]))
        client.close()

    def test_handshake_without_password_ok(self):
        # 无密码形态：服务器直接欢迎
        a, b = socket.socketpair()

        def serve():
            f = b.makefile("rwb")
            f.write(b">INFO:OpenVPN Management Interface Version 6\n")
            f.flush()
            f.readline()
            f.close()

        threading.Thread(target=serve, daemon=True).start()
        with mock.patch.object(mgmt_client.socket, "create_connection",
                               return_value=a):
            client = ManagementClient(handlers={}, handshake_timeout=2.0)
            client.connect("127.0.0.1", 17511)  # 不应抛
        client.close()

    def test_wrong_password_rejected(self):
        a, b = socket.socketpair()

        def serve():
            f = b.makefile("rwb")
            f.write(b"ENTER PASSWORD:")
            f.flush()
            f.readline()
            f.write(b"ERROR: password is incorrect\n")
            f.flush()
            b.close()

        threading.Thread(target=serve, daemon=True).start()
        with mock.patch.object(mgmt_client.socket, "create_connection",
                               return_value=a):
            client = ManagementClient(password="wrong", handshake_timeout=2.0)
            with self.assertRaises(ManagementError):
                client.connect("127.0.0.1", 17511)


if __name__ == "__main__":
    unittest.main()
