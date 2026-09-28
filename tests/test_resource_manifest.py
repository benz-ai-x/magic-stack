"""资源清单一致性（issue #14）：manifest 是 dev/build/smoke 的单一真源.

守卫三件套：
- 所有 manifest src 真实存在；
- 所有 resource_path 消费项被 manifest 覆盖（无漏项无幽灵）；
- bundle 校验函数能逐条核验 dest 文件存在。
"""
import unittest
from pathlib import Path

from tools.resource_manifest import RESOURCE_MANIFEST, RESOURCE_NAMES

ROOT = Path(__file__).resolve().parents[1]


class TestManifestCoversResources(unittest.TestCase):
    def test_all_manifest_sources_exist(self):
        missing = [s for s, _ in RESOURCE_MANIFEST
                   if not (ROOT / s).is_file()]
        self.assertEqual(missing, [])

    def test_every_resource_path_consumer_covered(self):
        import re
        consumed = set()
        for py in list(ROOT.glob("*.py")) + list(ROOT.glob("capture/*.py")) \
                + list(ROOT.glob("services/*.py")) + list(ROOT.glob("shellui/*.py")) \
                + list(ROOT.glob("tunnel/*.py")) + list(ROOT.glob("sysctl/*.py")) \
                + list(ROOT.glob("app.py", )):
            text = py.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'resource_path\("([a-zA-Z0-9_/. -]+\.(?:py|html|md|png|yaml))"',
                                 text):
                consumed.add(m.group(1))
        uncovered = consumed - set(RESOURCE_NAMES)
        self.assertEqual(uncovered, set(),
                         f"resource_path 消费了未列入 manifest 的资源: {uncovered}")

    def test_suanpan_example_in_manifest(self):
        self.assertIn("suanpan.example.yaml", RESOURCE_NAMES)

    def test_bundle_verifier(self):
        from tools.resource_manifest import verify_bundle
        import tempfile
        with tempfile.TemporaryDirectory() as bundle:
            for src, dest in RESOURCE_MANIFEST:
                name = src.rsplit("/", 1)[-1]
                target = Path(bundle) / dest / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x")
            ok, missing = verify_bundle(bundle)
        self.assertTrue(ok)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()


class TestBuildScriptCoverage(unittest.TestCase):
    """build.sh 的 --add-data 必须覆盖 manifest（单一真源驱动构建）。"""

    def test_build_script_consumes_manifest_generator(self):
        """R9-C2：build.sh 的 --add-data 自 manifest 单源生成（原 60 行
        手抄镜像 + substring 兜底退役）——镜像变消费。"""
        build = (ROOT / "build.sh").read_text()
        self.assertIn(
            "$(python tools/resource_manifest.py --pyinstaller-args)",
            build, "build.sh 必须经 manifest 生成 add-data（不再手抄）")

    def test_no_flat_name_collisions_in_output(self):
        """R9 复查钉：add-data 平铺到 bundle 根——两源同 basename 即
        静默覆盖（mount/vpn 的 coordinator.py 曾撞）。清单保留双源
        （覆盖面真源），冲突消解在生成器输出层（首见去重）——本钉
        保证**实际下发**的参数永不重名。"""
        from tools.resource_manifest import pyinstaller_add_data_args
        from collections import Counter
        tokens = pyinstaller_add_data_args().splitlines()
        pairs = [tokens[i + 1] for i, t in enumerate(tokens)
                 if t == "--add-data"]
        flats = [p.rsplit("/", 1)[-1].rsplit(":", 1)[0] for p in pairs]
        dupes = {n: c for n, c in Counter(flats).items() if c > 1}
        self.assertEqual(
            dupes, {},
            f"生成器输出的 add-data 平铺名冲突（bundle 根静默覆盖）: "
            f"{dupes}")

    def test_runtime_modules_exist(self):
        from tools.resource_manifest import RUNTIME_MODULES
        missing = [m for m in RUNTIME_MODULES if not (ROOT / m).is_file()]
        self.assertEqual(missing, [])

    def test_runtime_modules_cover_product_packages(self):
        """R9-C2 补全的回归钉：belt-and-suspenders 清单须覆盖全部产品
        域模块（此前 vpn/mount/server_shape 等靠 PyInstaller 追踪兜底、
        清单空转无人知）。"""
        from tools.resource_manifest import RUNTIME_MODULES, RESOURCE_MANIFEST
        import os
        covered = set(RUNTIME_MODULES)
        covered |= {src for src, _ in RESOURCE_MANIFEST}
        missing = []
        for pkg in ("shared", "mpconf", "tunnel", "mount", "vpn", "shellui",
                    "capture", "sysctl", "services"):
            for f in sorted(os.listdir(ROOT / pkg)):
                if not f.endswith(".py") or f == "__init__.py":
                    continue
                rel = f"{pkg}/{f}"
                if rel not in covered:
                    missing.append(rel)
        self.assertEqual(
            missing, [],
            f"RUNTIME_MODULES 漏收（新模块须同步 manifest——belt 空转"
            f"即静默）: {missing}")


class TestBuildScriptReverseCoverage(unittest.TestCase):
    """#49：守卫双向——build.sh 多打的 --add-data 也必须在 manifest
    （或显式白名单）。ssh_launch 曾漂移出 RUNTIME_MODULES 无人报警。"""

    def test_no_handwritten_add_data_beyond_allowlist(self):
        """R9-C2：生成化后 build.sh 只许三处 add-data——build_time/
        manifest 生成调用/dist-mitmdump。手抄回归即红。"""
        import re
        build = (ROOT / "build.sh").read_text()
        literals = re.findall(r'--add-data "([^"]+)"', build)
        self.assertEqual(
            sorted(literals),
            ["build_time.txt:.", "dist-mitmdump/mitmdump:mitmdump"],
            f"build.sh 出现手抄 --add-data（须进 manifest）: {literals}")

