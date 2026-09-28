"""vpn/coordinator 契约测试（R7-C1）。

连接序列、接入切换后置、established 服务层重建、安装知识单一归宿、
读侧投影（is_active/configured_server/menu_vpn/connect_precheck）——
自 app.py 迁出后的单一可测面。依赖全注入 + vpn 域模块就地 patch，
零子进程零网络。
"""
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from vpn import coordinator
from vpn.coordinator import VpnCoordinator, error_key, install_error_key

_SRV = {"id": "t-1", "name": "s1",
        "ssh": {"host": "h1", "port": 22, "auth_type": "key"},
        "services": {"openvpn": {"profile_set": True}}}


def _make(effects, config=None):
    """构造协调器 + 副作用记录器。effects 在 switch_access 时记录。"""
    vpn = VpnCoordinator(
        get_config=lambda: config if config is not None
        else {"servers": [_SRV]},
        switch_access=lambda: effects.append("switch_access"),
        on_established=lambda: effects.append("established"),
        notify=lambda title, body="": effects.append(("notify", title)),
        mark_dirty=lambda: effects.append("dirty"))
    return vpn


def _ready_mocks(assets_current=True, install_ok=True):
    """vpn 域模块整组替身：二进制在/profile 在/sudoers 在/资产新鲜。"""
    priv = MagicMock()
    priv.resolve_openvpn_bin.return_value = "/opt/homebrew/sbin/openvpn"
    priv.check_sudoers.return_value = True
    priv.install.return_value = (install_ok, "")
    priv.runtime_conf.side_effect = lambda text, pull_dns=True: text
    store = MagicMock()
    store.load_profile.return_value = "client\nremote h 1\n"
    dns = MagicMock()
    dns.assets_current.return_value = assets_current
    return priv, store, dns


class TestConnectSequence(unittest.TestCase):
    def _run(self, vpn, server=_SRV):
        with patch.object(coordinator, "privilege") as priv, \
                patch.object(coordinator, "profile_store") as store, \
                patch.object(coordinator, "keychain"), \
                patch.object(coordinator, "dns_scripts") as dns, \
                patch.object(coordinator, "VpnClient") as vc:
            priv.resolve_openvpn_bin.return_value = "/opt/openvpn"
            priv.check_sudoers.return_value = True
            priv.install.return_value = (True, "")
            store.load_profile.return_value = "client\nremote h 1\n"
            dns.assets_current.return_value = True
            vc.return_value = MagicMock()
            vpn.connect(server)
            return priv, store, dns, vc

    def test_happy_path_switches_access_and_starts_client(self):
        effects = []
        vpn = _make(effects)
        priv, store, dns, vc = self._run(vpn)
        self.assertIn("switch_access", effects)
        self.assertIn("dirty", effects)
        vc.assert_called_once()               # 客户端重建
        vc.return_value.start.assert_called_once()

    def test_no_binary_notifies_and_stops(self):
        effects = []
        vpn = _make(effects)
        with patch.object(coordinator, "privilege") as priv, \
                patch.object(coordinator, "VpnClient") as vc:
            priv.resolve_openvpn_bin.return_value = None
            vpn.connect(_SRV)
        self.assertNotIn("switch_access", effects)
        vc.assert_not_called()
        self.assertTrue(any(e[0] == "notify" for e in effects))

    def test_empty_profile_notifies_and_stops(self):
        effects = []
        vpn = _make(effects)
        with patch.object(coordinator, "privilege") as priv, \
                patch.object(coordinator, "profile_store") as store, \
                patch.object(coordinator, "VpnClient") as vc:
            priv.resolve_openvpn_bin.return_value = "/opt/openvpn"
            store.load_profile.return_value = "   "
            vpn.connect(_SRV)
        self.assertNotIn("switch_access", effects)
        vc.assert_not_called()

    def test_stale_assets_trigger_install_failure_notifies(self):
        """重装判据（sudoers 缺失或资产陈旧）命中且安装失败 → 通知后停，
        绝不切换接入层。"""
        effects = []
        vpn = _make(effects)
        with patch.object(coordinator, "privilege") as priv, \
                patch.object(coordinator, "profile_store") as store, \
                patch.object(coordinator, "keychain"), \
                patch.object(coordinator, "dns_scripts") as dns, \
                patch.object(coordinator, "VpnClient") as vc:
            priv.resolve_openvpn_bin.return_value = "/opt/openvpn"
            priv.check_sudoers.return_value = False      # 触发重装
            priv.install.return_value = (False, "cancelled")
            store.load_profile.return_value = "client\n"
            dns.assets_current.return_value = True
            vpn.connect(_SRV)
        priv.install.assert_called_once()
        self.assertNotIn("switch_access", effects)
        vc.assert_not_called()
        self.assertTrue(any(e[0] == "notify" for e in effects))

    def test_reentry_lock_skips_second_connect(self):
        effects = []
        vpn = _make(effects)
        import threading
        with vpn._connect_lock:            # 模拟已在途
            priv, store, dns, vc = self._run(vpn)
        vc.assert_not_called()             # 重入即跳过
        self.assertNotIn("switch_access", effects)

    def test_switch_stops_old_client_before_rebuild(self):
        effects = []
        vpn = _make(effects)
        old = MagicMock()
        vpn._client = old
        priv, store, dns, vc = self._run(vpn)
        old.stop.assert_called_once()      # 重建前优雅停旧客户端


class TestEstablishedRebuild(unittest.TestCase):
    def test_connected_rebuilds_service_layer_only(self):
        """VPN connected → 服务层僵尸重建（注入回调）；绝不拉 -D
        （接入互斥——与唤醒触发的根本差异）。"""
        effects = []
        vpn = _make(effects)
        vpn._on_state_change({"status": "connected"})
        self.assertEqual(effects.count("established"), 1)
        self.assertIn("dirty", effects)

    def test_non_connected_only_marks_dirty(self):
        effects = []
        vpn = _make(effects)
        vpn._on_state_change({"status": "reconnecting"})
        self.assertNotIn("established", effects)
        self.assertIn("dirty", effects)


class TestProjections(unittest.TestCase):
    def test_is_active_truth_table(self):
        vpn = _make([])
        self.assertFalse(vpn.is_active())          # 无客户端
        for st, want in (("connecting", True), ("connected", True),
                         ("reconnecting", True), ("exiting", False),
                         ("stopped", False), ("error", False)):
            vpn._client = MagicMock()
            vpn._client.vpn.status = st
            self.assertEqual(vpn.is_active(), want, st)

    def test_configured_server_first_profile_set(self):
        cfg = {"servers": [
            {"id": "a", "services": {"openvpn": {"profile_set": False}}},
            {"id": "b", "services": {"openvpn": {"profile_set": True}}},
            {"id": "c", "services": {"openvpn": {"profile_set": True}}},
        ]}
        vpn = _make([], config=cfg)
        self.assertEqual(vpn.configured_server().get("id"), "b")

    def test_menu_vpn_with_and_without_client(self):
        vpn = _make([])
        mv = vpn.menu_vpn()
        self.assertEqual(
            (mv.status, mv.server_name, mv.error_kind, mv.tun_ip),
            ("idle", "s1", "", ""))
        vpn._client = MagicMock()
        vpn._client.vpn.status = "connected"
        vpn._client.vpn.error_kind = "tls_error"
        vpn._client.vpn.tun_ip = "10.8.0.2"
        mv = vpn.menu_vpn()
        self.assertEqual(
            (mv.status, mv.server_name, mv.error_kind, mv.tun_ip),
            ("connected", "s1", "tls_error", "10.8.0.2"))

    def test_projection_enriches_semantics(self):
        """projection：快照 + active/in_flight 布尔 + error_key（JS 侧
        状态集字面量与手抄映射表的替代物）。"""
        vpn = _make([])
        self.assertIsNone(vpn.projection())     # 无客户端 → None
        vpn._client = MagicMock()
        vpn._client.snapshot.return_value = {
            "status": "exiting", "error_kind": "", "tun_ip": "10.0.0.1"}
        proj = vpn.projection()
        self.assertFalse(proj["active"])        # exiting：行为面不活跃
        self.assertTrue(proj["in_flight"])      # 显示面忙
        self.assertEqual(proj["error_key"], "")
        vpn._client.snapshot.return_value = {
            "status": "error", "error_kind": "tls_error", "tun_ip": ""}
        proj = vpn.projection()
        self.assertTrue(proj["active"] is False)
        self.assertEqual(proj["error_key"], "vpn.err.tls")

    def test_connect_precheck_profile_file_wins(self):
        """落盘文件为准：profile_set 缺但文件在 → 可连（真机案例：
        导入后未保存被 no_profile 弹回）。"""
        vpn = _make([])
        with patch.object(coordinator, "profile_store") as store:
            store.profile_exists.return_value = True
            self.assertIsNone(vpn.connect_precheck(
                {"id": "t-1", "services": {}}))
            store.profile_exists.return_value = False
            srv = {"id": "t-1",
                   "services": {"openvpn": {"profile_set": False}}}
            self.assertEqual(vpn.connect_precheck(srv), "no_profile")


class TestInstallFor(unittest.TestCase):
    """安装知识单一归宿（设置窗按钮与连接序列同款调用）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from shared import config_store
        p = patch.dict(config_store.PATHS,
                       {"vpn_profiles_dir": self._tmp.name})
        p.start()
        self.addCleanup(p.stop)
        from vpn import profile_store
        profile_store.save_profile("t-1", "client\nremote h 1\n")

    def test_no_profile_code(self):
        vpn = _make([])
        ok, code = vpn.install_for({"id": "t-gone"})
        self.assertFalse(ok)
        self.assertEqual(code, "no_profile")

    def test_pull_dns_default_and_false(self):
        from vpn import privilege
        vpn = _make([])
        for pull_dns, marker in ((True, None),
                                 (False, 'pull-filter ignore "dhcp-option"')):
            with patch.object(privilege, "install",
                              return_value=(True, "")) as install, \
                    patch.object(coordinator, "keychain"):
                srv = {"id": "t-1", "services": {
                    "openvpn": {"pull_dns": pull_dns}}}
                ok, _ = vpn.install_for(srv)
            self.assertTrue(ok)
            conf = install.call_args.kwargs["conf_text"]
            if marker:
                self.assertIn(marker, conf)
            else:
                self.assertNotIn("dhcp-option", conf)


class TestShutdownAndKeys(unittest.TestCase):
    def test_shutdown_non_blocking(self):
        """quit 路径：blocking=False（主线程不裸等）+ 客户端清空。"""
        vpn = _make([])
        client = MagicMock()
        vpn._client = client
        vpn.shutdown()
        client.stop.assert_called_once_with(blocking=False)
        self.assertIsNone(vpn._client)

    def test_error_key_mapping(self):
        self.assertEqual(error_key("tls_error"), "vpn.err.tls")
        self.assertEqual(error_key("unknown_x"), "vpn.err.generic")
        self.assertEqual(install_error_key("cancelled"),
                         "vpn.err.install_cancelled")
        self.assertEqual(install_error_key("nope"),
                         "vpn.err.install_generic")

    def test_startup_reconcile_silent_without_password(self):
        vpn = _make([])
        with patch.object(coordinator, "keychain") as kc, \
                patch.object(coordinator, "adopt_stale_openvpn") as adopt:
            kc.get_vpn_mgmt_password.return_value = None
            vpn.startup_reconcile()          # 缺密码：静默跳过
        adopt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
