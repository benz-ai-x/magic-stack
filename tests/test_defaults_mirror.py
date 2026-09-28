"""默认值字面量 mirror（R8-C2 止血半刀）。

Python DEFAULT_CONFIG / NFS_LOCAL_PORT_FALLBACK / shared.defaults 的
数值默认被 config_ui.html 以 JS 字面量复抄三处（投影缺省/渲染占位/
collect 回填）——此前零守卫，Python 改默认只会造成假 dirty/漏 dirty
的静默分叉。本测试是最小 tripwire：任一默认值变更而 JS 未跟 → 红，
提醒两侧同步（终态是 C3 的 manifest 单源下发，届时本测试退役）。

只钉 JS 真实镜像的数值面：retention_days 与 VPN 管理口不经 JS 字面量
（前者表单初值走 /api/state 真值，后者是 Python 侧域参数）。
"""
import unittest
from pathlib import Path

from mpconf.config import DEFAULT_CONFIG, NFS_LOCAL_PORT_FALLBACK
from shared import defaults

HTML = (Path(__file__).resolve().parent.parent
        / "shellui" / "config_ui.html").read_text(encoding="utf-8")

_PY_TRUTH = {
    "socks5_port": DEFAULT_CONFIG["socks5_port"],            # 1080
    "http_listen_port": DEFAULT_CONFIG["http_listen_port"],  # 8888
    "capture_port": DEFAULT_CONFIG["capture_port"],          # 8080
    "config_port": DEFAULT_CONFIG["config_port"],            # 9528
    "gateway_port": defaults.DEFAULT_GATEWAY_PORT,           # 9527
    "nfs_local_port_fallback": NFS_LOCAL_PORT_FALLBACK,      # 12049
}


class TestDefaultsMirroredInJS(unittest.TestCase):
    def test_numeric_defaults_appear_in_settings_ui(self):
        for name, value in _PY_TRUTH.items():
            self.assertIn(
                str(value), HTML,
                f"默认值 {name}={value} 未出现在 config_ui.html——"
                f"JS 侧字面量与本侧真相漂移（改默认必须两侧同步；"
                f"终态由 constraints manifest 单源化，届时删本测试）")


if __name__ == "__main__":
    unittest.main()
