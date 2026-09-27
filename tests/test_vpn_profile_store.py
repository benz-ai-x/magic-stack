"""vpn/profile_store.py —— profile 落盘存取（profile_set 布尔的写者）。"""
import os
import tempfile
import unittest
from unittest import mock

from shared import config_store
from vpn import profile_store


class TestProfileStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # PATHS 单点重定向——测试绝不落真实家目录（config_store 纪律）
        patcher = mock.patch.dict(
            config_store.PATHS, {"vpn_profiles_dir": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_roundtrip_and_permissions(self):
        self.assertTrue(profile_store.save_profile("t-abc123", "client\nremote h 1\n"))
        self.assertTrue(profile_store.profile_exists("t-abc123"))
        self.assertEqual(profile_store.load_profile("t-abc123"),
                         "client\nremote h 1\n")
        # 内嵌私钥的文件必须 0600（spec §4 机密性契约）
        mode = os.stat(profile_store.profile_path("t-abc123")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_load_missing_returns_empty(self):
        self.assertEqual(profile_store.load_profile("t-none"), "")
        self.assertFalse(profile_store.profile_exists("t-none"))

    def test_delete_idempotent(self):
        profile_store.save_profile("t-x", "data")
        self.assertTrue(profile_store.delete_profile("t-x"))
        self.assertFalse(profile_store.profile_exists("t-x"))
        self.assertTrue(profile_store.delete_profile("t-x"))  # 再删=成功

    def test_path_stays_inside_registered_dir(self):
        # server_id 含路径分隔符也不越出注册目录（防御性替换）
        path = profile_store.profile_path("../evil")
        self.assertEqual(os.path.dirname(path), self._tmp.name)
        self.assertTrue(
            os.path.abspath(path).startswith(self._tmp.name + os.sep))

    def test_servers_do_not_share_files(self):
        profile_store.save_profile("t-a", "A")
        profile_store.save_profile("t-b", "B")
        self.assertEqual(profile_store.load_profile("t-a"), "A")
        self.assertEqual(profile_store.load_profile("t-b"), "B")


if __name__ == "__main__":
    unittest.main()
