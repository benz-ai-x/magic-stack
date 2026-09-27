"""shared/server_shape 契约测试。

钉住三件结构承诺：
1. 代理角色悬空 id 的**两种语义**（回退版 proxy_server vs 严格版
   is_proxy_server）——分歧是测试钉住的故意设计（菜单读侧不猜归属，
   merge 写侧重写），下沉后必须继续显式共存；
2. enabled 口径（enabled≠False 才进 -L 集合/才占会话存在性）；
3. mpconf 转发导出奇偶——既有 import 面不破（tunnel/mount 够不着的
   病根由叶子层解决，mpconf 消费方零改动）。
"""
import unittest

from shared import server_shape
from shared.server_shape import (
    enabled_forwards, first_forward_port, is_proxy_server, nfs_node,
    openvpn_node, proxy_server, proxy_server_id, server_by_id,
    server_forwards, server_nfs, server_openvpn, servers, servers_by_id,
    ssh_node, ssh_service,
)


def _srv(sid, forwards=None, nfs=None, openvpn=None, ssh=None):
    svc = {}
    if forwards is not None:
        svc["ssh"] = {"forwards": forwards}
    if nfs is not None:
        svc["nfs"] = nfs
    if openvpn is not None:
        svc["openvpn"] = openvpn
    row = {"id": sid, "services": svc}
    if ssh is not None:
        row["ssh"] = ssh
    return row


class TestServers(unittest.TestCase):
    def test_shape_safety(self):
        self.assertEqual(servers(None), [])
        self.assertEqual(servers({}), [])
        self.assertEqual(servers({"servers": "x"}), [])
        self.assertEqual(servers({"servers": ["a"]}), ["a"])


class TestById(unittest.TestCase):
    def test_server_by_id_first_match(self):
        a, a2 = _srv("t-a"), _srv("t-a")
        cfg = {"servers": [a, {"id": "x"}, a2, {"noid": 1}]}
        self.assertIs(server_by_id(cfg, "t-a"), a)
        self.assertIsNone(server_by_id(cfg, "missing"))

    def test_servers_by_id_first_win_and_skip_idless(self):
        a, a2 = _srv("t-a"), _srv("t-a")
        cfg = {"servers": [a, {"noid": 1}, a2, _srv("t-b")]}
        self.assertIs(servers_by_id(cfg)["t-a"], a)
        self.assertEqual(set(servers_by_id(cfg)), {"t-a", "t-b"})


class TestNodes(unittest.TestCase):
    def test_ssh_node(self):
        self.assertEqual(ssh_node({"ssh": {"host": "h"}}), {"host": "h"})
        self.assertEqual(ssh_node({}), {})
        self.assertEqual(ssh_node(None), {})
        self.assertEqual(ssh_node({"ssh": "x"}), {})

    def test_service_node_none_vs_empty(self):
        s = _srv("t")
        # None 语义：校验侧「未配置不校验」
        self.assertIsNone(nfs_node(s))
        self.assertIsNone(openvpn_node(s))
        # {} 语义：运行侧缺省节点
        self.assertEqual(server_nfs(s), {})
        self.assertEqual(server_openvpn(s), {})
        self.assertEqual(ssh_service(s), {})
        # 配置在场时同指一物
        s2 = _srv("t", nfs={"enabled": True}, openvpn={"enabled": False},
                  forwards=[])
        self.assertEqual(nfs_node(s2), {"enabled": True})
        self.assertIs(server_nfs(s2), nfs_node(s2))
        self.assertEqual(openvpn_node(s2), {"enabled": False})
        self.assertIs(server_openvpn(s2), openvpn_node(s2))

    def test_forwards_non_list_safe(self):
        self.assertEqual(server_forwards({"services": {"ssh": {"forwards": "x"}}}), [])
        self.assertEqual(server_forwards(_srv("t", forwards=[{"local_port": 1}])),
                         [{"local_port": 1}])


class TestEnabledForwards(unittest.TestCase):
    def test_enabled_criteria(self):
        rows = [{"local_port": 1},                      # enabled 缺省=启用
                {"local_port": 2, "enabled": True},
                {"local_port": 3, "enabled": False},    # 停用不进 -L 集合
                "junk"]                                 # 非对象行跳过
        self.assertEqual([f["local_port"] for f in enabled_forwards(
            _srv("t", forwards=rows))], [1, 2])

    def test_first_forward_port(self):
        rows = [{"local_port": 3, "enabled": False},   # 停用不算
                {"local_port": True},                   # bool 非 int
                {"local_port": 0}, {"local_port": 70000},
                {"local_port": 8080}]
        self.assertEqual(first_forward_port(_srv("t", forwards=rows)), 8080)
        self.assertIsNone(first_forward_port(_srv("t", forwards=[])))
        self.assertIsNone(first_forward_port(None))


class TestProxyRole(unittest.TestCase):
    def _cfg(self):
        a, b = _srv("t-a"), _srv("t-b")
        return {"servers": [a, b], "proxy_server_id": "t-a"}, a, b

    def test_proxy_server_id(self):
        self.assertEqual(proxy_server_id({"proxy_server_id": "t-a"}), "t-a")
        self.assertEqual(proxy_server_id({}), "")
        self.assertEqual(proxy_server_id(None), "")
        self.assertEqual(proxy_server_id({"proxy_server_id": 5}), "")

    def test_proxy_server_fallback_semantics(self):
        """回退版：id 有效→命中；悬空/缺省→首条（与 merge 写侧同序）。"""
        cfg, a, b = self._cfg()
        self.assertIs(proxy_server(cfg), a)
        cfg2 = {"servers": [a, b], "proxy_server_id": "t-b"}
        self.assertIs(proxy_server(cfg2), b)
        dangling = {"servers": [a, b], "proxy_server_id": "t-gone"}
        self.assertIs(proxy_server(dangling), a)          # 悬空→首条
        self.assertIs(proxy_server({"servers": [a, b]}), a)  # 缺省→首条
        self.assertIsNone(proxy_server({"servers": []}))
        self.assertIsNone(proxy_server(None))

    def test_is_proxy_server_strict_semantics(self):
        """严格版：悬空 id 谁都不标（菜单读侧不猜归属）；缺省→首条。"""
        cfg, a, b = self._cfg()
        self.assertTrue(is_proxy_server(cfg, a))
        self.assertFalse(is_proxy_server(cfg, b))
        dangling = {"servers": [a, b], "proxy_server_id": "t-gone"}
        self.assertFalse(is_proxy_server(dangling, a))    # 悬空→谁都不标
        self.assertFalse(is_proxy_server(dangling, b))
        no_id = {"servers": [a, b]}
        self.assertTrue(is_proxy_server(no_id, a))        # 缺省→首条（同一性）
        self.assertFalse(is_proxy_server(no_id, b))
        self.assertFalse(is_proxy_server(None, a))
        self.assertFalse(is_proxy_server({}, {"id": "t-a"}))
        self.assertFalse(is_proxy_server({"servers": []}, {}))

    def test_dangling_divergence_is_deliberate(self):
        """两语义对悬空 id 的分歧是故意设计——本测试是分歧的显式契约。"""
        a, b = _srv("t-a"), _srv("t-b")
        cfg = {"servers": [a, b], "proxy_server_id": "t-gone"}
        self.assertIs(proxy_server(cfg), a)               # 写侧/运行侧：回退
        self.assertFalse(is_proxy_server(cfg, a))         # 菜单：不标


class TestMpconfReexport(unittest.TestCase):
    def test_mpconf_still_exports_accessors(self):
        """mpconf.config 转发导出奇偶——既有 import 面不破。"""
        import mpconf.config as mc
        for name in ("servers", "server_by_id", "proxy_server",
                     "proxy_server_id", "server_forwards", "server_nfs",
                     "server_openvpn"):
            self.assertIs(getattr(mc, name), getattr(server_shape, name),
                          f"mpconf.config.{name} 必须转发自 server_shape")


if __name__ == "__main__":
    unittest.main()
