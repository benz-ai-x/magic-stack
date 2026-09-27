"""vpn/privilege.py + dns_scripts.py —— argv 钉死/安装命令面/脚本语法。

sudoers 条目零通配是安全模型的第一道闸（spec §5.1）——这里逐字钉死。
DNS 脚本经 /bin/sh -n 做语法校验（不执行）。
"""
import subprocess
import tempfile
import unittest
from unittest import mock

from vpn import dns_scripts, privilege
from vpn.privilege import (
    CONF_PATH,
    MGMT_PW_PATH,
    SUDOERS_PATH,
    build_argv,
    build_full_command,
    resolve_openvpn_bin,
    runtime_conf,
    sudoers_rule,
)

BIN = "/opt/homebrew/opt/openvpn/sbin/openvpn"


class TestArgvPinning(unittest.TestCase):
    def test_argv_shape_and_space_free(self):
        argv = build_argv(BIN, mgmt_port=17511)
        self.assertEqual(argv[0], BIN)
        self.assertIn("--management-hold", argv)
        self.assertIn("--management-query-passwords", argv)
        self.assertIn("interact", argv)          # auth-retry interact 必在
        self.assertIn(dns_scripts.DNS_UP_PATH, argv)
        self.assertIn(dns_scripts.DNS_DOWN_PATH, argv)
        # sudoers 词法安全的前提：参数无空格（pull_dns 走 conf 不走 argv）
        for token in argv:
            self.assertNotIn(" ", token)

    def test_full_command_prefixed_with_sudo_n(self):
        cmd = build_full_command(BIN)
        self.assertEqual(cmd[:2], ["sudo", "-n"])
        self.assertEqual(cmd[2], BIN)

    def test_sudoers_rule_pins_exact_argv_and_reconcile(self):
        rule = sudoers_rule("tester", BIN, 17511)
        self.assertTrue(rule.startswith("tester ALL=(root) NOPASSWD: "))
        pinned = " ".join(build_argv(BIN, 17511))
        self.assertIn(pinned, rule)              # 逐字同源（build_argv 单一归宿）
        self.assertIn("/bin/sh " + dns_scripts.DNS_DOWN_PATH, rule)
        self.assertNotIn("*", rule)              # 零通配
        self.assertIn("127.0.0.1 17511", rule)   # 固定管理口端口进规则

    def test_runtime_conf_pull_dns_toggle(self):
        base = "client\nremote h 1\n"
        self.assertEqual(runtime_conf(base), "client\nremote h 1\n")
        no_dns = runtime_conf(base, pull_dns=False)
        self.assertIn('pull-filter ignore "dhcp-option"', no_dns)

    def test_resolve_openvpn_bin_env_override(self):
        self.assertEqual(
            resolve_openvpn_bin({"MAGIC_STACK_OPENVPN": "/custom/openvpn"}),
            "/custom/openvpn")
        # 空环境 → 三级链探测，结果必是 str（本机是否装 openvpn 不影响）
        self.assertIsInstance(resolve_openvpn_bin({}), str)


class TestInstallCommandFace(unittest.TestCase):
    def test_admin_script_shape(self):
        script = privilege._admin_script(["echo x", "true"])
        self.assertIn('do shell script "echo x && true"', script)
        self.assertIn("with administrator privileges", script)

    def test_write_file_part(self):
        part = privilege._write_file_part("/p/conf", "QkFH", mode="0600")
        self.assertIn("base64 -D", part)
        self.assertIn("chown root:wheel", part)
        self.assertIn("chmod 0600", part)

    def test_check_sudoers_reads_sudo_l(self):
        out = f"NOPASSWD: {BIN} --config {CONF_PATH} ...\n" \
              f"NOPASSWD: /bin/sh {dns_scripts.DNS_DOWN_PATH}\n"
        fake = subprocess.CompletedProcess(["sudo", "-n", "-l"], 0,
                                           stdout=out.encode())
        with mock.patch.object(privilege.subprocess, "run",
                               return_value=fake) as run:
            self.assertTrue(privilege.check_sudoers(BIN))
            self.assertEqual(run.call_args[0][0], ["sudo", "-n", "-l"])

    def test_check_sudoers_negative(self):
        fake = subprocess.CompletedProcess(["sudo", "-n", "-l"], 1)
        with mock.patch.object(privilege.subprocess, "run", return_value=fake):
            self.assertFalse(privilege.check_sudoers(BIN))

    def test_install_requires_binary(self):
        with mock.patch.object(privilege, "resolve_openvpn_bin",
                               return_value=""):
            ok, code = privilege.install(conf_text="x", mgmt_password="pw")
        self.assertFalse(ok)
        self.assertEqual(code, "openvpn_missing")

    def test_install_success_path(self):
        out = f"NOPASSWD: {BIN} --config {CONF_PATH} ...\n" \
              f"NOPASSWD: /bin/sh {dns_scripts.DNS_DOWN_PATH}\n"
        fake = subprocess.CompletedProcess(
            ["osascript"], 0,
            stderr=b"")
        fake_l = subprocess.CompletedProcess(["sudo", "-n", "-l"], 0,
                                             stdout=out.encode())

        def runner(argv, **kw):
            return fake if argv[0] == "osascript" else fake_l

        with mock.patch.object(privilege, "resolve_openvpn_bin",
                               return_value=BIN), \
             mock.patch.object(privilege.subprocess, "run",
                               side_effect=runner) as run:
            ok, code = privilege.install(conf_text="client\nremote h 1\n",
                                         mgmt_password="pw",
                                         openvpn_bin=BIN)
        self.assertTrue(ok)
        self.assertEqual(code, "")
        script = run.call_args_list[0][0][0][2]
        self.assertIn("visudo -cf", script)          # sudoers 先校验后转正
        self.assertIn("chmod 0440", script)
        self.assertIn(MGMT_PW_PATH, script)
        self.assertIn(SUDOERS_PATH, script)

    def test_install_cancel_detected_including_localized(self):
        cancelled = subprocess.CompletedProcess(
            ["osascript"], 1,
            stderr="".join(chr(c) for c in (0x7528, 0x6237, 0x53D6, 0x6D88)
                           ).encode())
        with mock.patch.object(privilege, "resolve_openvpn_bin",
                               return_value=BIN), \
             mock.patch.object(privilege.subprocess, "run",
                               return_value=cancelled):
            ok, code = privilege.install(conf_text="x", mgmt_password="pw",
                                         openvpn_bin=BIN)
        self.assertFalse(ok)
        self.assertEqual(code, "cancelled")


class TestDnsScripts(unittest.TestCase):
    def test_paths_registered(self):
        # 路径刻意无空格：sudoers 命令匹配按空白分词（dns_scripts 文档）
        self.assertEqual(dns_scripts.SYSTEM_DIR, "/Library/MagicStack/openvpn")
        self.assertNotIn(" ", dns_scripts.SYSTEM_DIR)
        self.assertIn("dns-up.sh", dns_scripts.DNS_UP_PATH)
        self.assertIn("dns-down.sh", dns_scripts.DNS_DOWN_PATH)

    def test_scripts_syntax_valid_sh(self):
        # /bin/sh -n 只做语法解析不执行——root 脚本进盘前的最低门槛
        for name, text in (("dns-up.sh", dns_scripts.UP_SCRIPT),
                           ("dns-down.sh", dns_scripts.DOWN_SCRIPT)):
            with tempfile.NamedTemporaryFile("w", suffix=".sh",
                                             delete=False) as fh:
                fh.write(text)
                path = fh.name
            proc = subprocess.run(["/bin/sh", "-n", path],
                                  capture_output=True)
            self.assertEqual(proc.returncode, 0,
                             f"{name} 语法错误: {proc.stderr.decode()[:200]}")

    def test_scripts_contain_marker_and_backup_paths(self):
        for text in (dns_scripts.UP_SCRIPT, dns_scripts.DOWN_SCRIPT):
            self.assertIn(dns_scripts.MARKER_PATH, text)
            self.assertIn(dns_scripts.BACKUP_PATH, text)

    def test_up_script_parses_foreign_options(self):
        self.assertIn("foreign_option_", dns_scripts.UP_SCRIPT)
        self.assertIn("networksetup -setdnsservers", dns_scripts.UP_SCRIPT)
        # down 必须有恢复语义（Empty 清空快照过的未配置态）
        self.assertIn('"Empty"', dns_scripts.DOWN_SCRIPT)

    def test_reconcile_command_shape(self):
        self.assertEqual(dns_scripts.reconcile_command(),
                         ["sudo", "-n", "/bin/sh",
                          dns_scripts.DNS_DOWN_PATH])

    def test_marker_predicate(self):
        with mock.patch.object(dns_scripts.os.path, "exists",
                               return_value=True):
            self.assertTrue(dns_scripts.marker_exists())
        with mock.patch.object(dns_scripts.os.path, "exists",
                               return_value=False):
            self.assertFalse(dns_scripts.marker_exists())


if __name__ == "__main__":
    unittest.main()
