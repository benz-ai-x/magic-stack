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
        argv = build_argv(BIN)
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
        rule = sudoers_rule("tester", BIN)
        self.assertTrue(rule.startswith("tester ALL=(root) NOPASSWD: "))
        pinned = " ".join(build_argv(BIN))
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
        # 空环境 → 探测链，结果必是 str（本机是否装 openvpn 不影响）
        self.assertIsInstance(resolve_openvpn_bin({}), str)

    def test_candidates_cover_both_brew_prefixes(self):
        # Intel（/usr/local）与 ARM（/opt/homebrew）都必须在探测链里——
        # 评审发现的兼容性坑：单硬编码 /opt/homebrew 会让一半 Mac 探不到
        self.assertIn("/opt/homebrew/opt/openvpn/sbin/openvpn",
                      privilege.OPENVPN_BIN_CANDIDATES)
        self.assertIn("/usr/local/opt/openvpn/sbin/openvpn",
                      privilege.OPENVPN_BIN_CANDIDATES)


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
            ["osascript"], 1, stderr="用户取消了授权".encode())
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

    def test_up_script_installs_ipv6_reject_route(self):
        """IPv4-only 隧道的 v6 绕行堵口：up 装 reject 路由（立即回不可达
        → happy-eyeballs 瞬间回落 v4；redirect-gateway ipv6 在 macOS 装
        不上路由，真机实锄 2026-09-28）。"""
        self.assertIn("route -n add -inet6 -reject", dns_scripts.UP_SCRIPT)
        self.assertIn("2000::/3", dns_scripts.UP_SCRIPT)

    def test_down_script_withdraws_route_and_guards_generation(self):
        """down 摘 reject 路由 + 代际守卫：还有别的 openvpn 实例在跑就
        绝不还原（迟到/孤儿 down 把新连接刚应用的 DNS 还原掉的竞态，
        2026-09-28 双击重连真机案例）。pgrep 必须 -x（精确进程名）——
        sudo 包装进程的命令行同样含 openvpn，-f 会把父 sudo 误判为
        存活实例导致永不还原。"""
        self.assertIn("route -n delete -inet6 -reject",
                      dns_scripts.DOWN_SCRIPT)
        self.assertIn("pgrep -x openvpn", dns_scripts.DOWN_SCRIPT)
        self.assertIn('grep -vw "$PPID"', dns_scripts.DOWN_SCRIPT)
        self.assertNotIn("pgrep -f", dns_scripts.DOWN_SCRIPT)

    def test_scripts_log_decisions(self):
        """诊断日志（此前无日志只能考古）：每个判定分支都落 dns.log。"""
        for text in (dns_scripts.UP_SCRIPT, dns_scripts.DOWN_SCRIPT):
            self.assertIn(dns_scripts.LOG_PATH, text)
            self.assertIn(">> \"$LOG\"", text)

    def test_scripts_embed_version_marker(self):
        for text in (dns_scripts.UP_SCRIPT, dns_scripts.DOWN_SCRIPT):
            self.assertIn(f"# version: {dns_scripts.SCRIPTS_VERSION}", text)

    def test_assets_current_compares_version_marker(self):
        """磁盘脚本 0755 可读——版本注记缺失/旧版即触发重装（conf 0600
        不可读，其重装挂脚本版本）。"""
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as td:
            up = _os.path.join(td, "dns-up.sh")
            down = _os.path.join(td, "dns-down.sh")
            cur = f"# version: {dns_scripts.SCRIPTS_VERSION}"
            with mock.patch.object(dns_scripts, "DNS_UP_PATH", up), \
                 mock.patch.object(dns_scripts, "DNS_DOWN_PATH", down):
                self.assertFalse(dns_scripts.assets_current())  # 不存在
                with open(up, "w") as f:
                    f.write("#!/bin/sh\n" + cur + "\n")
                self.assertFalse(dns_scripts.assets_current())  # 只有 up
                with open(down, "w") as f:
                    f.write("old\n")                             # 旧版 down
                self.assertFalse(dns_scripts.assets_current())
                with open(down, "w") as f:
                    f.write("#!/bin/sh\n" + cur + "\n")
                self.assertTrue(dns_scripts.assets_current())
        with mock.patch.object(dns_scripts.os.path, "exists",
                               return_value=False):
            self.assertFalse(dns_scripts.marker_exists())


class TestInstallStampFreshness(unittest.TestCase):
    """R8-C1：conf/mgmt.pw 不可读资产的新鲜度经用户侧 stamp 比对——
    换 profile/pull_dns 翻转/密码重生成都必须触发重装。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.dict(privilege.config_store.PATHS
                            if hasattr(privilege, "config_store")
                            else __import__("shared.config_store",
                                            fromlist=["PATHS"]).PATHS,
                            {"vpn_profiles_dir": self._tmp.name})
        p.start()
        self.addCleanup(p.stop)
        # dns 脚本恒新鲜（stamp 半边独立验证）
        self._dns = mock.patch.object(privilege.dns_scripts, "assets_current",
                                      return_value=True)
        self._dns.start()
        self.addCleanup(self._dns.stop)

    def test_roundtrip_fresh_then_stale_on_any_change(self):
        privilege._write_stamp("conf-A", "pw-A")
        self.assertTrue(privilege.assets_fresh("conf-A", "pw-A"))
        self.assertFalse(privilege.assets_fresh("conf-B", "pw-A"))   # 换 profile
        self.assertFalse(privilege.assets_fresh("conf-A", "pw-B"))   # 换密码
        self.assertFalse(privilege.assets_fresh("conf-A", ""))       # 密码缺席

    def test_missing_or_corrupt_stamp_requires_install(self):
        self.assertFalse(privilege.assets_fresh("conf-A", "pw-A"))
        with open(privilege._stamp_path(), "w") as f:
            f.write("{not json")
        self.assertFalse(privilege.assets_fresh("conf-A", "pw-A"))

    def test_scripts_version_bump_invalidates(self):
        privilege._write_stamp("conf-A", "pw-A")
        with mock.patch.object(privilege.dns_scripts, "SCRIPTS_VERSION",
                               "99999"):
            self.assertFalse(privilege.assets_fresh("conf-A", "pw-A"))

    def test_stale_dns_scripts_short_circuits(self):
        privilege._write_stamp("conf-A", "pw-A")
        self._dns.stop()
        stale = mock.patch.object(privilege.dns_scripts, "assets_current",
                                  return_value=False)
        stale.start()
        self.addCleanup(stale.stop)
        self.assertFalse(privilege.assets_fresh("conf-A", "pw-A"))

    def test_install_success_writes_stamp(self):
        fake = subprocess.CompletedProcess(["osascript"], 0, stderr=b"")
        out = f"NOPASSWD: {BIN} --config {CONF_PATH} ...\n" \
              f"NOPASSWD: /bin/sh {dns_scripts.DNS_DOWN_PATH}\n"

        def runner(argv, **kw):
            return fake if argv[0] == "osascript" else \
                subprocess.CompletedProcess(["sudo"], 0,
                                            stdout=out.encode())

        with mock.patch.object(privilege, "resolve_openvpn_bin",
                               return_value=BIN), \
             mock.patch.object(privilege.subprocess, "run",
                               side_effect=runner):
            ok, _ = privilege.install(conf_text="conf-A",
                                      mgmt_password="pw-A", openvpn_bin=BIN)
        self.assertTrue(ok)
        self.assertTrue(privilege.assets_fresh("conf-A", "pw-A"))


if __name__ == "__main__":
    unittest.main()
