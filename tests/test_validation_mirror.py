"""跨语言校验漂移报警（架构评审 R2-3）。

设置窗 JS `validateConfig` 手抄镜像 Python 校验器（第一道闸 vs 422
兜底的双层拦是既定约定）——镜像没有单一真相，历史已两次实际漂移
（v0.10 NFS 端口漏计、R1-C5 冲突命名空间漏 NFS）。本测试是报警器：
同一语料两侧同跑（Python 分域校验器 + 经 extract.mjs 取真实发布的
LAYER 1），按族断言——

- **mirrored 族**：「同错同净」。一侧改了规则另一侧没跟 → 红。
- **py_only / js_only 族**：单侧规则显式登记（新增单侧规则必须在此
  挂号，迫使「补镜像 or 声明只此一侧」成为显式决策，而非静默漂移）。

不做的事：逐字文案比对（两侧措辞本就有差，比文案只会制造噪音）。
node 缺席时整文件跳过（CI 环境保证 node 在场——node --test 是标准
测试口径）。
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from mpconf import validate as mp_validate
from suanpan.validate import sp_errors

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def _t(host="h", port=22, forwards=None, nfs=None):
    """v2 服务器行：连接参数在 ssh 节，转发/NFS 在 services。"""
    row = {"ssh": {"host": host, "port": port, "auth_type": "key"},
           "services": {}}
    if forwards is not None:
        row["services"]["ssh"] = {"forwards": forwards}
    if nfs is not None:
        row["services"]["nfs"] = nfs
    return row


def _sp(**extra):
    sp = {"providers": {"p": {"base_url": "https://api.example.com"}},
          "rules": [], "router": {"default": ""}}
    sp.update(extra)
    return sp


def _fw(lp, rp=80, host="127.0.0.1", enabled=True):
    return {"local_port": lp, "remote_host": host, "remote_port": rp,
            "enabled": enabled}


# (name, mp, sp, family) —— family: mirrored | py_only | js_only
CORPUS = [
    # ── mirrored：同错同净 ──────────────────────────────
    ("clean_minimal",
     {"servers": [_t()]}, _sp(), "mirrored"),
    # R8-C2：JS-only 漏挂止血——两条挪进 mirrored（Python 半边已补）
    ("ssh_addr_empty",
     {"servers": [_t(host="")]}, _sp(), "mirrored"),
    ("ssh_port_range",
     {"servers": [_t(port=70000)]}, _sp(), "mirrored"),
    ("socks5_port_range",
     {"socks5_port": 70000, "servers": [_t()]}, _sp(), "mirrored"),
    ("listen_port_range",
     {"servers": [_t()]}, _sp(listen_port=0), "mirrored"),
    ("zero_port_capture",
     {"capture_port": 0, "servers": [_t()]}, _sp(), "mirrored"),
    ("forward_port_range",
     {"servers": [_t(forwards=[_fw(0)])]}, _sp(), "mirrored"),
    ("forward_remote_host_ipv6",
     {"servers": [_t(forwards=[_fw(9000, host="a::b")])]}, _sp(), "mirrored"),
    ("forward_dup_in_server",
     {"servers": [_t(forwards=[_fw(9000), _fw(9000, rp=81)])]}, _sp(),
     "mirrored"),
    ("port_conflict_global",
     {"socks5_port": 8080, "capture_port": 8080, "servers": [_t()]}, _sp(),
     "mirrored"),
    ("port_conflict_forward_vs_global",
     {"capture_port": 9000,
      "servers": [_t(forwards=[_fw(9000)])]}, _sp(), "mirrored"),
    ("port_conflict_nfs_vs_forward",
     {"servers": [
         _t(forwards=[_fw(9000)]),
         _t(host="h2", nfs={"enabled": True, "local_port": 9000,
                            "mounts": [{"name": "d", "remote_path": "/d"}]}),
     ]}, _sp(), "mirrored"),
    ("nfs_port_range",
     {"servers": [_t(nfs={"enabled": True, "local_port": 70000,
                          "mounts": [{"name": "d", "remote_path": "/d"}]})]},
     _sp(), "mirrored"),
    ("nfs_default_node_exempt",
     {"servers": [
         _t(nfs={"enabled": False, "local_port": 12049, "mounts": []}),
         _t(host="h2", nfs={"enabled": False, "local_port": 12049,
                            "mounts": []}),
     ]}, _sp(), "mirrored"),
    ("disabled_forward_exempt",
     {"servers": [_t(forwards=[_fw(9000, enabled=False), _fw(9000)])]},
     _sp(), "mirrored"),
    ("dangling_route_ref",
     {"servers": [_t()]},
     _sp(rules=[{"match_prefix": "m", "route_to": "ghost/model-x"}]),
     "mirrored"),
    ("dangling_default_ref",
     {"servers": [_t()]},
     _sp(router={"default": "ghost/model-x"}),
     "mirrored"),
    # ── py_only：Python 有、JS 无——显式登记（第一道闸放行 → 422 现形）──
    ("mount_dir_conflict",
     {"servers": [
         _t(nfs={"enabled": True, "local_port": 12049, "mounts": [
             {"name": "a", "remote_path": "/a", "local_dir": "/Volumes/x"}]}),
         _t(host="h2", nfs={"enabled": True, "local_port": 12050, "mounts": [
             {"name": "b", "remote_path": "/b", "local_dir": "/Volumes/x"}]}),
     ]}, _sp(), "py_only"),
    ("nfs_mount_row_shape",
     {"servers": [_t(nfs={"enabled": True, "local_port": 12049,
                          "mounts": [{"name": "  ", "remote_path": "/d"}]})]},
     _sp(), "py_only"),
    ("retention_days_range",
     {"retention_days": 99999, "servers": [_t()]}, _sp(), "py_only"),
    ("request_timeout_range",
     {"servers": [_t()]}, _sp(request_timeout_s=0), "py_only"),
    ("body_limit_range",
     {"servers": [_t()]}, _sp(body_limit_mb=99999), "py_only"),
    ("provider_base_url_origin",
     {"servers": [_t()]},
     _sp(providers={"p": {"base_url": "notaurl"}}), "py_only"),
    # ── js_only：JS 有、Python 无——显式登记 ──
    ("provider_blank_name",
     {"servers": [_t()]},
     _sp(providers={" ": {"base_url": "https://api.example.com"}}),
     "js_only"),
]


def _py_errors(mp, sp):
    """Python 第一道闸等价物：分域校验器直调（prepare 的校验半边）。"""
    errors = []
    errors += mp_validate.numeric_errors(mp)
    errors += mp_validate.server_rows_errors(mp)
    errors += mp_validate.port_conflict_errors(mp, sp)
    errors += mp_validate.mount_dir_conflict_errors(mp)
    errors += sp_errors(sp)
    return errors


def _js_errors(mp, sp):
    """JS 第一道闸：经 node 跑设置窗 LAYER 1 的真实 validateConfig。"""
    proc = subprocess.run(
        [NODE, str(ROOT / "tests" / "js" / "validate_mirror.mjs")],
        input=json.dumps({"mp": mp, "sp": sp}),
        capture_output=True, text=True, check=True, timeout=60)
    return json.loads(proc.stdout)


@unittest.skipIf(NODE is None, "node 不在场——跨语言报警需要 node")
class TestValidationMirror(unittest.TestCase):

    maxDiff = None

    def _assert_clean(self, name, errors, side):
        self.assertEqual(errors, [], f"{name}: {side} 侧意外报错")

    def test_mirrored_families_agree(self):
        """镜像族「同错同净」——单侧改规则不跟另一侧即红。"""
        for name, mp, sp, family in CORPUS:
            if family != "mirrored":
                continue
            with self.subTest(name=name):
                py = _py_errors(mp, sp)
                js = _js_errors(mp, sp)
                self.assertEqual(bool(py), bool(js),
                                 f"{name}: Python={py} JS={js}——镜像漂移")

    def test_mirrored_clean_families_are_clean_both_sides(self):
        """两侧都应净的语料逐侧断空（错得多干净得也干净）。"""
        clean = ["clean_minimal", "nfs_default_node_exempt",
                 "disabled_forward_exempt"]
        for name, mp, sp, family in CORPUS:
            if name not in clean:
                continue
            with self.subTest(name=name):
                self._assert_clean(name, _py_errors(mp, sp), "Python")
                self._assert_clean(name, _js_errors(mp, sp), "JS")

    def test_python_only_families_registered(self):
        """py_only 族：Python 报、JS 静默（第一道闸放行 → 422 才现形）。
        新增 Python 单侧规则必须在此挂号——补镜像或显式登记，二选一。"""
        for name, mp, sp, family in CORPUS:
            if family != "py_only":
                continue
            with self.subTest(name=name):
                self.assertTrue(_py_errors(mp, sp), f"{name}: Python 未报错？")
                self.assertEqual(_js_errors(mp, sp), [],
                                 f"{name}: JS 已补镜像？请把语料挪回 mirrored")

    def test_js_only_families_registered(self):
        """js_only 族：JS 报、Python 静默（agent 直连 PUT 路径放行）。"""
        for name, mp, sp, family in CORPUS:
            if family != "js_only":
                continue
            with self.subTest(name=name):
                self.assertTrue(_js_errors(mp, sp), f"{name}: JS 未报错？")
                self.assertEqual(_py_errors(mp, sp), [],
                                 f"{name}: Python 已补齐？请把语料挪回 mirrored")

    def test_every_fixture_has_a_family(self):
        families = {"mirrored", "py_only", "js_only"}
        for name, _mp, _sp, family in CORPUS:
            self.assertIn(family, families, f"{name}: 未归类的单侧行为")


if __name__ == "__main__":
    unittest.main()
