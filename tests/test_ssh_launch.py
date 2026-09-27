"""Tests for tunnel/ssh_launch.py — SSH 调用策略单一归宿。

策略面（argv / 失败分类）直接钉在本模块；两个调用方
（SSHMonitor.start / config_server.test_tunnel）只留各自职责的测试。
"""
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tunnel import host_key, ssh_launch


class TestDescribeFailure(unittest.TestCase):
    """stderr → 中文短语的分类表：顺序敏感，变更须先于未信任。"""

    def test_host_key_changed_maps_before_verification_failed(self):
        stderr = ("@@@@ REMOTE HOST IDENTIFICATION HAS CHANGED! @@@@\n"
                  "Host key verification failed.")
        self.assertIn("主机密钥已变更", ssh_launch.describe_failure(stderr))

    def test_host_key_untrusted_mapping(self):
        self.assertIn("主机密钥未信任",
                      ssh_launch.describe_failure("Host key verification failed."))

    def test_permission_denied_mapping(self):
        self.assertIn("认证失败", ssh_launch.describe_failure(
            "u@example.com: Permission denied (publickey)."))

    def test_connection_refused_mapping(self):
        self.assertIn("连接被服务器拒绝",
                      ssh_launch.describe_failure("Connection refused"))

    def test_resolve_failure_mapping(self):
        self.assertIn("无法解析服务器地址",
                      ssh_launch.describe_failure("Could not resolve hostname x"))

    def test_timeout_mapping(self):
        self.assertIn("连接超时",
                      ssh_launch.describe_failure("Connection timed out"))

    def test_no_route_mapping(self):
        self.assertIn("无法路由到服务器",
                      ssh_launch.describe_failure("No route to host"))

    def test_network_unreachable_mapping(self):
        self.assertIn("网络不可达",
                      ssh_launch.describe_failure("Network is unreachable"))

    def test_unknown_failure_includes_first_stderr_line(self):
        result = ssh_launch.describe_failure("some exotic failure\nsecond line")
        self.assertIn("some exotic failure", result)
        self.assertNotIn("second line", result)

    def test_long_first_line_is_truncated(self):
        result = ssh_launch.describe_failure("x" * 200)
        self.assertLess(len(result), 200)

    def test_none_stderr_degrades_gracefully(self):
        self.assertIn("未知错误", ssh_launch.describe_failure(None))

    def test_empty_stderr_degrades_gracefully(self):
        self.assertIn("未知错误", ssh_launch.describe_failure("  "))


class TestHostKeyChanged(unittest.TestCase):
    """is_host_key_changed 的分类谓词与失败文案共用同一张表。"""

    def test_changed_phrase_matches(self):
        self.assertTrue(ssh_launch.host_key_changed(
            "REMOTE HOST IDENTIFICATION HAS CHANGED"))

    def test_plain_verification_failure_is_not_changed(self):
        self.assertFalse(ssh_launch.host_key_changed(
            "Host key verification failed."))

    def test_empty_is_not_changed(self):
        self.assertFalse(ssh_launch.host_key_changed(""))


class TestBuildTunnelCommand(unittest.TestCase):
    """长驻隧道 argv 策略：SSHMonitor.start 的完整策略面。"""

    _KEY = {"ssh": {"host": "srv", "user": "u", "port": 22,
                    "auth_type": "key", "ssh_key": "~/.ssh/id_rsa"}}

    def test_key_auth_full_argv(self):
        sc = ssh_launch.build_tunnel_command(self._KEY, 1080)
        try:
            self.assertEqual(sc.cmd, [
                "ssh", "-i", "~/.ssh/id_rsa",
                "-D", "1080", "-N",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
                "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "ServerAliveInterval=20",
                "-o", "ServerAliveCountMax=3",
                "-o", "IPQoS=none",
                "-o", "ConnectionAttempts=3",
                "-C",
                "-p", "22", "u@srv",
            ])
            self.assertEqual(sc.display_cmd, " ".join(sc.cmd))
            self.assertEqual(sc.pass_fds, ())
            self.assertIsNone(sc.password_fd)
        finally:
            sc.close_password_fd()

    def test_compression_off_omits_dash_c(self):
        t = {"ssh": {**self._KEY["ssh"], "compression": False}}
        sc = ssh_launch.build_tunnel_command(t, 1080)
        self.assertNotIn("-C", sc.cmd)

    def test_no_user_destination_is_bare_host(self):
        t = {"ssh": {k: v for k, v in self._KEY["ssh"].items()
                     if k != "user"}}
        sc = ssh_launch.build_tunnel_command(t, 1080)
        self.assertEqual(sc.cmd[-1], "srv")

    def test_default_port_22(self):
        t = {"ssh": {k: v for k, v in self._KEY["ssh"].items()
                     if k != "port"}}
        sc = ssh_launch.build_tunnel_command(t, 1080)
        self.assertEqual(sc.cmd[-2:], ["22", "u@srv"])

    def test_password_auth_uses_sshpass_fd(self):
        sc = ssh_launch.build_tunnel_command(
            {"ssh": {"host": "srv", "user": "u",
                     "auth_type": "password"}},
            1080, "sekrit")
        try:
            self.assertEqual(sc.cmd[0], "sshpass")
            self.assertEqual(sc.cmd[1], "-d")
            # 密码绝不出现在 argv；展示串的 -d 参数打码为 ***。
            self.assertNotIn("sekrit", sc.cmd)
            self.assertEqual(sc.display_cmd.split()[:3],
                             ["sshpass", "-d", "***"])
            self.assertEqual(sc.pass_fds, (sc.password_fd,))
            self.assertIsNotNone(sc.password_fd)
        finally:
            sc.close_password_fd()

    def test_close_password_fd_is_idempotent(self):
        sc = ssh_launch.build_tunnel_command(
            {"ssh": {"host": "srv", "auth_type": "password"}}, 1080, "sekrit")
        fd = sc.password_fd
        sc.close_password_fd()
        with self.assertRaises(OSError):
            os.fstat(fd)  # 已真正关闭
        sc.close_password_fd()  # 第二次不抛

    def test_key_auth_close_is_noop(self):
        sc = ssh_launch.build_tunnel_command(self._KEY, 1080)
        sc.close_password_fd()  # 无 fd 也不抛


class TestProbe(unittest.TestCase):
    """一次性探针：与隧道同策略，连一次即退（remote command `true`）。"""

    _KEY = {"ssh": {"host": "example.com", "user": "u", "port": 2222,
                    "auth_type": "key", "ssh_key": "~/.ssh/id_ed25519"}}
    _PW = {"ssh": {"host": "example.com", "user": "u", "port": 22,
                   "auth_type": "password"}}

    @staticmethod
    def _proc(returncode=0, stderr=b""):
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")
        return SimpleNamespace(returncode=returncode, stderr=stderr)

    def test_key_auth_success_full_argv(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = ssh_launch.probe(self._KEY)
        self.assertEqual(result, {"ok": True})
        cmd = run.call_args[0][0]
        self.assertEqual(cmd, [
            "ssh", "-o", "BatchMode=yes", "-i", "~/.ssh/id_ed25519",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-p", "2222", "u@example.com", "true",
        ])
        kwargs = run.call_args[1]
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], ssh_launch.PROBE_TIMEOUT)
        self.assertEqual(kwargs["pass_fds"], ())

    def test_password_auth_success_uses_sshpass_fd(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = ssh_launch.probe(self._PW, password="sekrit")
        self.assertEqual(result, {"ok": True})
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "sshpass")
        self.assertEqual(cmd[1], "-d")
        self.assertNotIn("sekrit", cmd)
        self.assertIn("NumberOfPasswordPrompts=1", cmd)
        self.assertNotIn("BatchMode=yes", cmd)
        self.assertTrue(run.call_args[1]["pass_fds"])

    def test_timeout_returns_chinese_phrase(self):
        with patch.object(ssh_launch.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("ssh", 15)):
            result = ssh_launch.probe(self._KEY)
        self.assertEqual(result, {"ok": False, "error": "连接超时"})

    def test_missing_binary_returns_oserror_phrase(self):
        with patch.object(ssh_launch.subprocess, "run",
                          side_effect=FileNotFoundError("ssh")):
            result = ssh_launch.probe(self._KEY)
        self.assertFalse(result["ok"])
        self.assertIn("无法启动 ssh", result["error"])
        self.assertNotIn("sshpass", result["error"])

    def test_missing_sshpass_mentions_password_hint(self):
        with patch.object(ssh_launch.subprocess, "run",
                          side_effect=FileNotFoundError("sshpass")):
            result = ssh_launch.probe(self._PW, password="sekrit")
        self.assertFalse(result["ok"])
        self.assertIn("sshpass", result["error"])

    def test_failure_stderr_is_classified(self):
        stderr = ("@@@@ REMOTE HOST IDENTIFICATION HAS CHANGED! @@@@\n"
                  "Host key verification failed.")
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(255, stderr)):
            result = ssh_launch.probe(self._KEY)
        self.assertFalse(result["ok"])
        self.assertIn("主机密钥已变更", result["error"])

    def test_none_stderr_degrades_gracefully(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=SimpleNamespace(returncode=255,
                                                       stderr=None)):
            result = ssh_launch.probe(self._KEY)
        self.assertFalse(result["ok"])
        self.assertIn("未知错误", result["error"])

    def test_non_utf8_stderr_does_not_crash(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(255, b"\xff\xfe broken")):
            result = ssh_launch.probe(self._KEY)
        self.assertFalse(result["ok"])

    def test_null_ssh_key_coerced_to_empty_string(self):
        """显式 ssh_key: null 不得让 None 进入 argv（probe 绝不抛异常）。"""
        t = {"ssh": {**self._KEY["ssh"], "ssh_key": None}}
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = ssh_launch.probe(t)
        self.assertEqual(result, {"ok": True})
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-i") + 1], "")

    def test_null_ssh_key_coerced_in_tunnel_command(self):
        t = {"ssh": {**self._KEY["ssh"], "ssh_key": None}}
        sc = ssh_launch.build_tunnel_command(t, 1080)
        self.assertEqual(sc.cmd[sc.cmd.index("-i") + 1], "")

    def test_pipe_exhaustion_returns_oserror_phrase(self):
        """fd 耗尽（os.pipe OSError）不破「绝不抛异常」契约。"""
        with patch.object(ssh_launch.os, "pipe",
                          side_effect=OSError("too many open files")):
            result = ssh_launch.probe(self._PW, password="sekrit")
        self.assertFalse(result["ok"])
        self.assertIn("无法启动 ssh", result["error"])

    def test_write_failure_closes_read_fd_before_raise(self):
        """os.write 失败时 r_fd 不泄漏（异常仍按 OSError 分类）。"""
        real_pipe = os.pipe
        created = []

        def tracking_pipe():
            fds = real_pipe()
            created.extend(fds)
            return fds

        with patch.object(ssh_launch.os, "pipe", side_effect=tracking_pipe), \
             patch.object(ssh_launch.os, "write",
                          side_effect=OSError("disk full")):
            result = ssh_launch.probe(self._PW, password="sekrit")
        self.assertFalse(result["ok"])
        self.assertIn("无法启动 ssh", result["error"])
        r_fd, w_fd = created
        for fd in (r_fd, w_fd):
            with self.assertRaises(OSError):
                os.fstat(fd)  # 两端都已关闭


class TestBuildTunnelCommandForwards(unittest.TestCase):
    """-L argv 段与形态分离（ADR-011 修订）：代理模式（socks5_port 非
    None）恒纯 -D（结构上忽略 forwards——代理服务器自己的转发走独立
    会话）；纯转发模式（None）只携带 -L。"""

    _KEY = {"ssh": {"host": "srv", "user": "u", "port": 22,
                    "auth_type": "key", "ssh_key": "~/.ssh/id_rsa"}}

    def test_proxy_mode_is_pure_d(self):
        """-D 会话不搭载 -L 便车：有 forwards 也只出 -D（结构保证，
        不依赖调用方剥隧道副本）。"""
        t = {**self._KEY, "services": {"ssh": {"forwards": [
            {"local_port": 9000, "remote_host": "127.0.0.1", "remote_port": 8000},
            {"local_port": 9001, "remote_host": "10.0.0.5", "remote_port": 5432},
        ]}}}
        sc = ssh_launch.build_tunnel_command(t, 1080)
        try:
            self.assertEqual(sc.cmd, [
                "ssh", "-i", "~/.ssh/id_rsa",
                "-D", "1080", "-N",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
                "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "ServerAliveInterval=20",
                "-o", "ServerAliveCountMax=3",
                "-o", "IPQoS=none",
                "-o", "ConnectionAttempts=3",
                "-C",
                "-p", "22", "u@srv",
            ])
        finally:
            sc.close_password_fd()

    def test_two_forwards_full_argv(self):
        """纯转发模式（socks5_port=None）：只携带 -L，无 -D。"""
        t = {**self._KEY, "services": {"ssh": {"forwards": [
            {"local_port": 9000, "remote_host": "127.0.0.1", "remote_port": 8000},
            {"local_port": 9001, "remote_host": "10.0.0.5", "remote_port": 5432},
        ]}}}
        sc = ssh_launch.build_tunnel_command(t, None)
        self.assertNotIn("-D", sc.cmd)
        self.assertEqual(sc.cmd, [
            "ssh", "-i", "~/.ssh/id_rsa",
            "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-L", "127.0.0.1:9000:127.0.0.1:8000",
            "-L", "127.0.0.1:9001:10.0.0.5:5432",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "ServerAliveInterval=20",
            "-o", "ServerAliveCountMax=3",
            "-o", "IPQoS=none",
            "-o", "ConnectionAttempts=3",
            "-C",
            "-p", "22", "u@srv",
        ])

    def test_no_forwards_argv_unchanged(self):
        """无 forwards 的隧道 argv 与历史完全一致（缺省字段零影响）。"""
        sc_plain = ssh_launch.build_tunnel_command(self._KEY, 1080)
        sc_empty = ssh_launch.build_tunnel_command(
            {**self._KEY, "services": {"ssh": {"forwards": []}}}, 1080)
        self.assertEqual(sc_plain.cmd, sc_empty.cmd)

    def test_remote_host_blank_defaults_to_loopback(self):
        t = {**self._KEY, "services": {"ssh": {"forwards": [
            {"local_port": 9000, "remote_host": "", "remote_port": 8000}]}}}
        sc = ssh_launch.build_tunnel_command(t, None)
        self.assertIn("-L", sc.cmd)
        self.assertEqual(sc.cmd[sc.cmd.index("-L") + 1],
                         "127.0.0.1:9000:127.0.0.1:8000")

    def test_invalid_rows_skipped_defensively(self):
        """越界/缺字段/非 dict 行防御性跳过——prepare+merge 双保险下不可达，
        但 argv 构建绝不能因坏行产出畸形 -L 参数。"""
        t = {**self._KEY, "services": {"ssh": {"forwards": [
            {"local_port": 70000, "remote_host": "h", "remote_port": 80},
            {"local_port": 9000, "remote_host": "h", "remote_port": 0},
            "not-a-dict",
            {"local_port": True, "remote_host": "h", "remote_port": 80},
            {"local_port": 9100, "remote_host": "db", "remote_port": 5432},
        ]}}}
        sc = ssh_launch.build_tunnel_command(t, None)
        fw = [sc.cmd[i + 1] for i, a in enumerate(sc.cmd) if a == "-L"]
        self.assertEqual(fw, ["127.0.0.1:9100:db:5432"])


class TestProbeForward(unittest.TestCase):
    """一次性端口转发探针（ssh -W）：测表单意图，不依赖隧道状态。"""

    _KEY = {"ssh": {"host": "example.com", "user": "u", "port": 2222,
                    "auth_type": "key", "ssh_key": "~/.ssh/id_ed25519"}}

    @staticmethod
    def _proc(returncode=0, stderr=b""):
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")
        return SimpleNamespace(returncode=returncode, stderr=stderr)

    def test_success_full_argv_and_latency(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = ssh_launch.probe_forward(self._KEY, "10.1.2.3", 8000)
        self.assertTrue(result["ok"])
        self.assertIsInstance(result["latency_ms"], int)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd, [
            "ssh", "-o", "BatchMode=yes", "-i", "~/.ssh/id_ed25519",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-p", "2222", "-W", "10.1.2.3:8000", "u@example.com",
        ])
        kwargs = run.call_args[1]
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], ssh_launch.PROBE_TIMEOUT)
        self.assertEqual(kwargs["pass_fds"], ())
        # stdin=DEVNULL：-W 桥接的 stdio 立即 EOF，探针必然自终
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_remote_refused_classified_before_generic_table(self):
        """-W 的 channel open failed 内含 "Connection refused"——必须分类为
        远程端拒绝，而非通用表的「连接被服务器拒绝」（那是 SSH 层语义）。"""
        stderr = ("channel 0: open failed: connect failed: "
                  "Connection refused\r\n")
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(255, stderr)):
            result = ssh_launch.probe_forward(self._KEY, "127.0.0.1", 8000)
        self.assertFalse(result["ok"])
        self.assertIn("拒绝连接", result["error"])
        self.assertIn("127.0.0.1:8000", result["error"])

    def test_remote_timeout_classified(self):
        stderr = "channel 0: open failed: connect failed: Operation timed out"
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(255, stderr)):
            result = ssh_launch.probe_forward(self._KEY, "10.0.0.9", 8000)
        self.assertFalse(result["ok"])
        self.assertIn("连接超时", result["error"])

    def test_other_open_failed_generic_remote_phrase(self):
        stderr = "channel 0: open failed: connect failed: Network is unreachable"
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(255, stderr)):
            result = ssh_launch.probe_forward(self._KEY, "10.0.0.9", 8000)
        self.assertFalse(result["ok"])
        self.assertIn("无法连到远程", result["error"])

    def test_ssh_layer_failure_uses_failure_table(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(
                              255, "Permission denied (publickey).")):
            result = ssh_launch.probe_forward(self._KEY, "127.0.0.1", 8000)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "认证失败：密钥或密码被拒绝")

    def test_timeout_returns_chinese_phrase(self):
        with patch.object(ssh_launch.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("ssh", 15)):
            result = ssh_launch.probe_forward(self._KEY, "127.0.0.1", 8000)
        self.assertEqual(result, {"ok": False, "error": "连接超时"})

    def test_input_guards(self):
        for rh, rp in ((None, "8000"), ("127.0.0.1", "0"),
                       ("127.0.0.1", "abc"), ("bad host", "80"),
                       ("::1", "80"), ("", -1)):
            result = ssh_launch.probe_forward(self._KEY, rh, rp)
            self.assertFalse(result["ok"], f"{rh}:{rp} 不应通过")
            self.assertNotIn("Traceback", result["error"])

    def test_password_auth_uses_sshpass_fd(self):
        t = {"ssh": {"host": "example.com", "user": "u", "port": 22,
                     "auth_type": "password"}}
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = ssh_launch.probe_forward(t, "127.0.0.1", 8000, "sekrit")
        self.assertTrue(result["ok"])
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "sshpass")
        self.assertNotIn("sekrit", cmd)
        self.assertIn("NumberOfPasswordPrompts=1", cmd)
        self.assertNotIn("BatchMode=yes", cmd)
        self.assertTrue(run.call_args[1]["pass_fds"])


if __name__ == "__main__":
    unittest.main()


class TestBuildTunnelCommandForwardMode(unittest.TestCase):
    """纯转发模式（socks5_port=None，多活的转发会话）：无 -D。"""

    _T = {"ssh": {"host": "srv", "user": "u", "port": 22,
                  "auth_type": "key", "ssh_key": "~/.ssh/id_rsa"},
          "services": {"ssh": {"forwards": [
              {"local_port": 9000, "remote_host": "127.0.0.1",
               "remote_port": 8000}]}}}

    def test_none_omits_dash_d(self):
        sc = ssh_launch.build_tunnel_command(self._T, None)
        try:
            self.assertNotIn("-D", sc.cmd)
            self.assertIn("-L", sc.cmd)
            self.assertIn("ExitOnForwardFailure=yes", sc.cmd)
            # 其余策略（host-key 三件套/keepalive/认证）与代理模式恒等
            self.assertIn("-i", sc.cmd)
        finally:
            sc.close_password_fd()

    def test_proxy_mode_still_carries_dash_d(self):
        sc = ssh_launch.build_tunnel_command(self._T, 1080)
        try:
            self.assertIn("-D", sc.cmd)
            self.assertIn("1080", sc.cmd)
        finally:
            sc.close_password_fd()


class TestForwardArgsEnabledFilter(unittest.TestCase):
    """逐条启停：停用行不进 -L 集合。"""

    def test_disabled_row_skipped(self):
        from tunnel.ssh_launch import _forward_args
        args = _forward_args({"services": {"ssh": {"forwards": [
            {"local_port": 9000, "remote_host": "127.0.0.1",
             "remote_port": 80, "enabled": False},
            {"local_port": 9001, "remote_host": "127.0.0.1",
             "remote_port": 81},
        ]}}})
        self.assertEqual(args, ["-L", "127.0.0.1:9001:127.0.0.1:81"])

    def test_absent_enabled_means_on(self):
        from tunnel.ssh_launch import _forward_args
        args = _forward_args({"services": {"ssh": {"forwards": [
            {"local_port": 9000, "remote_port": 80}]}}})
        self.assertEqual(args, ["-L", "127.0.0.1:9000:127.0.0.1:80"])
