"""Tests for 分域校验器（架构评审候选 2）——mpconf.validate 与
suanpan.validate 的直接 interface 面。

行为与文案的完整覆盖仍经 ConfigStateStore.prepare（test_config_state /
test_config_nfs 钉死）；这里钉各校验器的独立可测性与 mp/sp 单侧缺席
的形状。
"""
import unittest

from mpconf import validate as mpv
from suanpan import validate as spv


class TestMpValidate(unittest.TestCase):
    def test_numeric_errors(self):
        self.assertEqual(
            mpv.numeric_errors({"socks5_port": 70000, "retention_days": -1}),
            ["socks5_port 端口无效（须 1..65535）",
             "retention_days 无效（须 0..3650）"])

    def test_numeric_errors_empty_is_skip(self):
        self.assertEqual(mpv.numeric_errors({"socks5_port": ""}), [])

    def test_server_rows_forwards_shape(self):
        errs = mpv.server_rows_errors({"servers": [
            {"name": "s", "services": {"ssh": {"forwards": [
                {"local_port": 0, "remote_host": "h", "remote_port": 80},
                "not-a-dict"]}}}]})
        self.assertTrue(any("第 1 条转发的 local_port 无效" in e for e in errs))
        self.assertTrue(any("第 2 条端口转发必须是对象" in e for e in errs))

    def test_server_rows_nfs_messages(self):
        errs = mpv.server_rows_errors({"servers": [
            {"name": "s", "services": {"nfs": {"local_port": 12049, "mounts": [
                {"name": "", "remote_path": "rel", "local_dir": "x"}]}}}]})
        self.assertTrue(any("挂载名不能为空" in e for e in errs))
        self.assertTrue(any("远程路径必须是绝对路径" in e for e in errs))
        self.assertTrue(any("本地目录必须是绝对路径" in e for e in errs))

    def test_server_rows_nfs_not_dict_treated_as_absent(self):
        # v2 语义（与 JS 第一道闸同口径）：非 dict nfs = 未配置不校验，
        # merge 归一回默认节点——v1 的「nfs 必须是对象」报错随换轴退役
        errs = mpv.server_rows_errors({"servers": [{"name": "s",
                                                    "ssh": {"host": "h", "port": 22},
                                                    "services": {"nfs": ["x"]}}]})
        self.assertEqual(errs, [])
        errs2 = mpv.server_rows_errors({"servers": [{"name": "s",
                                                     "ssh": ["x"]}]})
        # R8-C2：ssh 节非 dict 后地址规则同拍命中（ssh_node 归一为空）
        self.assertEqual(errs2, ["服务器 s 的 ssh 必须是对象",
                                 "服务器 s 的地址不能为空"])

    def test_port_conflict_across_sides(self):
        errs = mpv.port_conflict_errors(
            {"servers": [{"name": "s", "services": {"ssh": {"forwards": [
                {"local_port": 9527, "remote_host": "h",
                 "remote_port": 80}]}}}]},
            {"listen_port": 9527})
        self.assertEqual(len(errs), 1)
        self.assertIn("端口冲突", errs[0])
        self.assertIn("9527", errs[0])

    def test_port_conflict_default_nfs_nodes_do_not_collide(self):
        # merge 填的纯默认 nfs 节（enabled=False 无挂载）不参与——
        # 两条隧道不得互报假冲突
        _srv = lambda n: {"name": n,
                          "services": {"nfs": {"enabled": False,
                                               "local_port": 12049,
                                               "mounts": []}}}
        self.assertEqual(
            mpv.port_conflict_errors(
                {"servers": [_srv("a"), _srv("b")]},
                None), [])

    def test_mount_dir_conflict_uses_default_resolution(self):
        errs = mpv.mount_dir_conflict_errors({"servers": [
            {"name": "a", "services": {"nfs": {"mounts": [{"name": "data"}]}}},
            {"name": "b", "services": {"nfs": {"mounts": [{"name": "data"}]}}}]})
        self.assertEqual(len(errs), 1)
        self.assertIn("/Volumes/data", errs[0])

    def test_mount_dir_conflict_none_side(self):
        self.assertEqual(mpv.mount_dir_conflict_errors(None), [])


class TestSpValidate(unittest.TestCase):
    def test_numeric_fields(self):
        # 顶层数值校验先于 schema（同一非法值两条路径都会报——数值行
        # 文案是本测试钉的对象）
        errs = spv.sp_errors({"listen_port": 0, "request_timeout_s": 0,
                              "body_limit_mb": 99999})
        self.assertIn("listen_port 端口无效（须 1..65535）", errs)
        self.assertTrue(any("request_timeout_s 无效" in e for e in errs))
        self.assertTrue(any("body_limit_mb 无效" in e for e in errs))

    def test_provider_origin(self):
        errs = spv.sp_errors({"providers": {
            "p": {"base_url": "ftp://x"}}})
        self.assertEqual(
            errs, ["供应商 p 的 base_url 必须是合法 http(s) origin"])

    def test_route_reference_integrity(self):
        errs = spv.sp_errors({"providers": {"a": {"base_url": "https://a"}},
                              "rules": [{"match_prefix": "claude-",
                                         "route_to": "ghost/model"}]})
        # schema 自身也校验路由引用（双保险）——本校验器的引用行必在其中
        self.assertIn("route_to/default 引用了不存在的供应商：ghost", errs)

    def test_rules_not_list(self):
        self.assertIn("rules 必须是列表", spv.sp_errors({"rules": "x"}))


if __name__ == "__main__":
    unittest.main()
