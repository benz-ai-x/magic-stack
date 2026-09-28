"""i18n 守卫（ADR-012）——catalog 与取词纪律的防漂移。

四道闸 + 行为冒烟：

1. **键位奇偶**——zh/en 键集合一致、值非空、格式占位符逐键一致
   （zh 有 ``{port}`` 而 en 漏了 → 红）；
2. **en 全译**——en catalog 的值不含汉字（防「复制中文充当翻译」；
   语言自称除外，如 Chinese (Simplified) 用英文名）；
3. **取词守卫**——产品代码里 ``i18n.t("...")`` 的字面键必须在
   catalog 内（键打错/删键即红；**动态键被禁止**——调用点一律字面量，
   状态词表存键名而非拼前缀）；
4. **汉字字面量守卫**——产品 .py 的字符串字面量不得含汉字：新用户
   可见文案必须进 catalog。docstring 与 logging 调用子树豁免
   （D6：日志不翻译）。未迁移文件在 ``_HAN_WHITELIST`` 挂号，逐
   里程碑烧掉（M1 菜单/通知、M2 设置窗、M3 校验器、M4 清零）。

tests/ 不在守卫范围（测试钉死 zh-CN 文案是既定口径——缺省语言
zh-CN，存量测试零改动）。
"""
import ast
import json
import re
import unittest
from pathlib import Path

from shared import i18n

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "shared" / "locales"

_HAN = re.compile(r"[\u4e00-\u9fff]")
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_LOG_METHODS = {"debug", "info", "warning", "error", "exception",
                "critical", "log"}

_PACKAGES = ("shared", "tunnel", "mpconf", "shellui", "capture",
             "sysctl", "services", "suanpan", "docker", "mount", "vpn")


def _product_files():
    yield ROOT / "app.py"
    yield ROOT / "util.py"
    for pkg in _PACKAGES:
        for p in sorted((ROOT / pkg).rglob("*.py")):
            if "__pycache__" not in p.parts:
                yield p


# 未迁移白名单——每条必须带去处；迁移完成即删行（M4 清零）。
# 理由缩写：M2=设置窗批次、M3=校验器结构化错误、M4=长尾表面批次。
_HAN_WHITELIST = {
    "shared/provider_auth.py": "M4：供应商注册表显示名（火山方舟等品牌）",
    "tunnel/async_runtime.py": "M4：运行时错误串",
    "tunnel/connection_coordinator.py": "M4：守卫通知文案",
    "tunnel/host_key.py": "M4：host-key 提示文案",
    "tunnel/host_key_flow.py": "M4：信任流程弹窗文案",
    "tunnel/ssh_launch.py": "M4：stderr 失败分类表",
    "mpconf/config.py": "M4：错误/提示文案",
    "mpconf/config_state.py": "M4：事务错误文案",
    "mpconf/validate.py": "M3：校验错误行",
    "shellui/log_window.py": "M4：日志窗标题",
    "shellui/webview_window.py": "M4：设置窗标题/脚本",
    "capture/ca_trust.py": "M4：CA 信任引导窗",
    "capture/capture_controller.py": "M4：抓包 hint 文案",
    "capture/capture_store.py": "M4：抓包目录提示",
    "capture/resources.py": "M4：资源契约错误",
    "sysctl/login_item.py": "M4：登录项错误文案",
    "services/authenticated_http.py": "M4：出站认证错误",
    "services/balance_usage.py": "M4：余额/用量文案",
    "services/claude_code_setup.py": "M4：Agent 配置文案",
    "services/config_server.py": "M2：登录页 + agent_instructions",
    "services/provider_probe.py": "M4：端点探测文案",
    "services/server_check.py": "M4：服务卡文案",
    "services/suanpan_runtime.py": "M4：网关运行时错误",
    "suanpan/config.py": "M4：schema 默认/错误文案",
    "suanpan/main.py": "M4：网关错误响应",
    "suanpan/proxy.py": "M4：网关 wire 错误（D6 单议）",
    "suanpan/usage_log.py": "M4：用量日志错误",
    "suanpan/validate.py": "M3：sp 校验错误行",
    "docker/entry.py": "M4：Docker 引导文案",
    "mount/coordinator.py": "M4：挂载错误文案",
    "mount/mount_control.py": "M4：挂载控制错误",
    "mount/remote_setup.py": "M4：远程安装提示",
    # 检测探针非用户可见文案：osascript 管理员授权取消的 stderr 匹配串
    # （随系统语言本地化；mount_control 同款串在其条目内）
    "vpn/privilege.py": "常驻：osascript 取消检测探针（非 UI 文案，不进 catalog）",
}


def _iter_han_constants(tree):
    """字符串 Constant 迭代：docstring 与 logging 调用子树豁免。"""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    stack = list(ast.iter_child_nodes(tree))
    while stack:
        node = stack.pop()
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOG_METHODS):
            continue  # D6：日志不翻译
        if isinstance(node, ast.Constant):
            if (isinstance(node.value, str) and id(node) not in docstrings
                    and _HAN.search(node.value)):
                yield node
            continue
        stack.extend(ast.iter_child_nodes(node))


class TestCatalogParity(unittest.TestCase):
    """闸 1+2：zh/en 是同一键空间的两个完整体。"""

    @classmethod
    def setUpClass(cls):
        cls.zh = json.loads((LOCALES / "zh-CN.json").read_text("utf-8"))
        cls.en = json.loads((LOCALES / "en.json").read_text("utf-8"))

    def test_key_sets_identical(self):
        self.assertEqual(set(self.zh), set(self.en),
                         "zh/en 键集合漂移：只有一侧新增的键")

    def test_values_non_empty(self):
        for lang, cat in (("zh-CN", self.zh), ("en", self.en)):
            empty = [k for k, v in cat.items() if not str(v).strip()]
            self.assertFalse(empty, f"{lang} catalog 空值键：{empty}")

    def test_placeholders_match_per_key(self):
        drift = []
        for k in set(self.zh) & set(self.en):
            if _PLACEHOLDER.findall(self.zh[k]) != _PLACEHOLDER.findall(self.en[k]):
                drift.append(k)
        self.assertFalse(drift, f"占位符漂移（顺序与多重集须一致）：{drift}")

    def test_english_catalog_has_no_han(self):
        leftover = [k for k, v in self.en.items() if _HAN.search(v)]
        self.assertFalse(leftover, f"en catalog 未翻译的值：{leftover}")


class TestCallSiteKeys(unittest.TestCase):
    """闸 3：i18n.t("...") 的字面键必须存在于 catalog（禁止动态键）。"""

    def test_every_literal_key_exists(self):
        missing = []
        for path in _product_files():
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "t"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "i18n"):
                    continue
                arg = node.args[0] if node.args else None
                if (isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and arg.value not in self.zh_keys
                        and arg.value not in self.en_keys):
                    missing.append(f"{path.name}:{node.lineno} {arg.value!r}")
        self.assertFalse(missing, f"取词键不在 catalog：{missing}")

    zh_keys = en_keys = frozenset()  # setUpClass 填充

    @classmethod
    def setUpClass(cls):
        cls.zh_keys = frozenset(json.loads(
            (LOCALES / "zh-CN.json").read_text("utf-8")))
        cls.en_keys = frozenset(json.loads(
            (LOCALES / "en.json").read_text("utf-8")))


class TestNoRawHanLiterals(unittest.TestCase):
    """闸 4：产品源码的字符串字面量不得含汉字（白名单挂号逐批次烧）。"""

    def test_no_unregistered_han_literals(self):
        violations = []
        for path in _product_files():
            rel = path.relative_to(ROOT).as_posix()
            if rel in _HAN_WHITELIST:
                continue
            tree = ast.parse(path.read_text("utf-8"))
            for node in _iter_han_constants(tree):
                violations.append(
                    f"{rel}:{node.lineno} {node.value[:30]!r}")
        self.assertFalse(violations,
                         "未挂号的汉字字面量（新文案必须进 catalog，见"
                         " ADR-012）：\n  " + "\n  ".join(violations))

    def test_whitelist_has_no_stale_entries(self):
        """白名单条目必须仍有效：文件已无汉字字面量（或已删）即摘牌。"""
        stale = []
        for rel in _HAN_WHITELIST:
            path = ROOT / rel
            if not path.exists():
                stale.append(f"{rel}（文件已删）")
                continue
            tree = ast.parse(path.read_text("utf-8"))
            if not any(True for _ in _iter_han_constants(tree)):
                stale.append(f"{rel}（已无汉字字面量，请摘牌）")
        self.assertFalse(stale, f"白名单过期条目：{stale}")


# ── 闸 5（M2）：设置窗 HTML 残留守卫 ──────────────────────────
# LAYER 2/3（渲染层）零容忍；豁免三类：静态骨架（applyStaticI18n 运行时
# 取词——服务端渲染 zh 骨架 + JS 首拍换装）、LAYER 1 数据面（node 测试
# 钉死的 zh 标签/复合文案，渲染侧已键化；M3/M4 收编）、注释。
_HTML_STATIC_ALLOW = (
    "<title>", "brand-subtitle", "priority-note", "copyAgentInstructions()",
    "id=\"page-title\"", "id=\"save-btn\"", "id=\"pending-title\"",
    "id=\"pending-items\"", "onclick=\"discardAll()\"", "onclick=\"saveAll()\"",
    "loading-text", "id=\"status-left\"", "id=\"shortcut-hint\"",
    "id=\"toast-msg\"",
)


class TestConfigUiHtmlHanResidue(unittest.TestCase):
    def test_render_layers_have_no_raw_han(self):
        html = (ROOT / "shellui" / "config_ui.html").read_text("utf-8")
        layer2 = html.index("// LAYER 2 ")
        violations = []
        for i, line in enumerate(html.splitlines(), 1):
            if i < layer2 or not _HAN.search(line):
                continue
            stripped = line.strip()
            if stripped.startswith(("//", "/*", "*")):
                continue  # 注释
            if any(marker in line for marker in _HTML_STATIC_ALLOW):
                continue  # 静态骨架（applyStaticI18n 运行时取词）
            if self._data_face(stripped):
                continue
            violations.append(f"{i}: {stripped[:70]}")
        self.assertFalse(violations,
                         "渲染层裸汉字（新文案必须 tt() 取词，见 ADR-012）：\n  "
                         + "\n  ".join(violations))

    @staticmethod
    def _data_face(s):
        """zh 数据面（node 测试钉死、渲染侧已键化）：LAYER 1 的标签/
        复合文案在 LAYER 2 标记之前整段跳过；此处只放行 LAYER 2 内的
        VIEWS/NAV/错误路由注册表。"""
        return (s.startswith(("const VIEWS=", "const NAV_ORDER",
                              "const NAV_GROUP_KEY", "const ERROR_VIEW_MAP"))
                or ":{group:" in s)


class TestI18nBehavior(unittest.TestCase):
    """行为冒烟：取词/兜底/解析（全局语言状态必须复原，防测试间泄漏）。"""

    def setUp(self):
        self._saved = i18n.language()
        self.addCleanup(i18n.set_language, self._saved)

    def test_default_is_zh_and_t_translates(self):
        i18n.set_language("zh-CN")
        self.assertEqual(i18n.t("menu.group.forward"), "端口映射")
        self.assertEqual(i18n.t("status.traffic.connections", n=3), "3 连接")

    def test_english_lookup_and_fallback(self):
        i18n.set_language("en")
        self.assertEqual(i18n.t("menu.group.forward"), "Port Forwards")
        self.assertEqual(i18n.t("status.traffic.connections", n=3), "3 connections")

    def test_missing_key_returns_key_and_survives(self):
        i18n.set_language("en")
        self.assertEqual(i18n.t("no.such.key"), "no.such.key")

    def test_set_language_ignores_unknown(self):
        i18n.set_language("zh-CN")
        i18n.set_language("klingon")
        self.assertEqual(i18n.language(), "zh-CN")

    def test_resolve_passthrough_and_auto(self):
        self.assertEqual(i18n.resolve("en"), "en")
        self.assertIn(i18n.resolve(None), i18n.SUPPORTED)
        self.assertIn(i18n.resolve(i18n.AUTO), i18n.SUPPORTED)


if __name__ == "__main__":
    unittest.main()
