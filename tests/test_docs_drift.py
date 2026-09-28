"""Docs drift checks (#41) — the permanent anti-drift mechanism.

Architecture docs are artifacts; these tests fail whenever a doc starts
describing code as something it no longer is. Facts that live in code
(versions, deleted features, module inventory) must never be restated in
docs in a way that can go stale silently — they either point at the code
or are checked here.

The ADR-000 更新注记 legitimately names deleted features in the past
tense as a tombstone; these checks therefore target the specific phrases
that PRESENT them as current.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
ADR_000 = ROOT / "docs" / "adr" / "000-system-architecture.md"
CLAUDE_MD = ROOT / "CLAUDE.md"
MODULE_MAP_MD = ROOT / "docs" / "agents" / "architecture.md"
UI_MD = ROOT / "docs" / "agents" / "ui.md"
ADR_024 = ROOT / "docs" / "adr" / "003-claude-code-env-contract.md"
ADR_025 = ROOT / "docs" / "adr" / "004-prompt-caching-and-prefix-stability.md"

# `__init__.py` is a package marker, not a module — never listed in docs.
_SKIP = frozenset({"__init__.py"})


class TestAdr000NoStaleClaims:
    """Deleted features / stale versions must not appear as present-tense."""

    _stale_phrases = (
        "tiktoken + pydantic",            # deleted dependency
        "app factory + `APIKeyMiddleware`",  # middleware moved to middleware.py
        "按场景（默认/后台/长上下文/推理）",      # deleted routing scenarios
        "按请求场景（默认/后台/长上下文/推理）",   # deleted routing scenarios
        "当前版本 v0.4.0",                 # version moved to build.sh/CLAUDE.md
        "25 个",                          # test-file count (now 60+)
        "路由 long_context 场景",          # deleted scenario named as current use
    )

    def test_no_stale_phrases(self):
        text = ADR_000.read_text(encoding="utf-8")
        for phrase in self._stale_phrases:
            assert phrase not in text, f"stale phrase in ADR-000: {phrase!r}"

    def test_no_proxy_runtime_module_entry(self):
        """proxy_runtime.py must not be listed as a module (it doesn't exist;
        the tombstone note mentions it only as `proxy_runtime.py` in backticks)."""
        text = ADR_000.read_text(encoding="utf-8")
        for line in text.splitlines():
            assert not re.match(r"^proxy_runtime\.py\s+#", line), \
                "proxy_runtime.py still listed as a module in ADR-000"

    def test_no_tiktoken_dependency_row(self):
        """#45: tiktoken is deleted from requirements; the versions table
        must not still list it as an Active dependency."""
        text = ADR_000.read_text(encoding="utf-8")
        for line in text.splitlines():
            assert not re.match(r"^\|\s*tiktoken\s*\|", line), \
                "tiktoken still listed in ADR-000 dependency table"


class TestVersionSingleSource:
    """build.sh is the only place a release version is set; app.py reads it."""

    def _version_of(self, rel):
        text = (ROOT / rel).read_text(encoding="utf-8")
        m = re.search(r'VERSION\s*=\s*"([^"]+)"', text)
        assert m, f"no VERSION literal found in {rel}"
        return m.group(1)

    def test_app_py_matches_build_sh(self):
        assert self._version_of("app.py") == self._version_of("build.sh")

    def test_claude_md_does_not_hardcode_version(self):
        """CLAUDE.md must point at build.sh, never restate a version that
        would go stale on release (#41)."""
        text = CLAUDE_MD.read_text(encoding="utf-8")
        assert not re.search(r"\bv\d+\.\d+\.\d+\b", text), \
            "CLAUDE.md hardcodes a version — point to build.sh instead"


class TestConfigStoreDocstringContract:
    """shared/config_store.py must use the current ADR-002 vocabulary."""

    def test_no_masking_wording(self):
        text = (ROOT / "shared/config_store.py").read_text(encoding="utf-8")
        assert "key masking" not in text, \
            "shared/config_store.py still uses the pre-ADR-002 'key masking' wording"
        assert "api_key_set" in text, \
            "shared/config_store.py docstring should name the api_key_set contract"


class TestClaudeCodeEnvContractDocumented:
    """#45: the env-var scheme claude_code_setup writes must be documented
    in ADR-003 — cross-checked against the code so the doc can't drift."""

    def test_adr_024_exists(self):
        assert ADR_024.exists(), "docs/adr/003-claude-code-env-contract.md missing"

    def test_every_env_var_documented(self):
        from services.claude_code_setup import _ROLES
        text = ADR_024.read_text(encoding="utf-8")
        env_vars = [env_var for _, _, env_var, _ in _ROLES]
        for var in env_vars:
            assert var in text, f"{var} not documented in ADR-003"

    def test_contract_terms_documented(self):
        text = ADR_024.read_text(encoding="utf-8")
        for term in ("ctx_1m", "[1M]", "ANTHROPIC_BASE_URL",
                     "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"):
            assert term in text, f"{term} not documented in ADR-003"


class TestPromptCachingAdrDocumented:
    """ADR-004: the caching architecture terms the code relies on
    (anthropic_native, json_mode, the usage buckets) must stay documented."""

    def test_adr_025_exists(self):
        assert ADR_025.exists(), "docs/adr/004-prompt-caching-and-prefix-stability.md missing"

    def test_caching_contract_terms_documented(self):
        text = ADR_025.read_text(encoding="utf-8")
        for term in ("anthropic_native", "json_mode",
                     "cache_read_input_tokens", "cache_creation_input_tokens"):
            assert term in text, f"{term} not documented in ADR-004"

    def test_prefix_stability_rule_documented(self):
        text = ADR_025.read_text(encoding="utf-8")
        assert "确定性" in text, "deterministic-normalization rule not documented in ADR-004"
        assert "缓存" in text


class TestAgentEntryPoint:
    """CLAUDE.md declares code as truth and links the guarded references."""

    def test_disclaimer_present(self):
        text = CLAUDE_MD.read_text(encoding="utf-8")
        assert "以代码为准" in text, \
            "CLAUDE.md lacks the '以代码为准' disclaimer for volatile info"

    def test_guarded_references_are_reachable(self):
        text = CLAUDE_MD.read_text(encoding="utf-8")
        for path in (MODULE_MAP_MD, UI_MD):
            relative = path.relative_to(ROOT).as_posix()
            assert path.is_file(), f"missing reference: {relative}"
            assert f"]({relative})" in text, \
                f"CLAUDE.md must link to {relative}"


class TestModuleMapCoversAllModules:
    """Every repo module appears in the disclosed per-domain module map.

    每域必须有 `<pkg>/ ──` 段，段内 `<name>.py ──` 行覆盖该域全部
    模块；按节匹配避免跨域同名文件（config.py/proxy.py/coordinator.py）
    掩盖缺失条目。迁出 CLAUDE.md 不改变守卫范围。
    """

    # 与 test_arch_imports._PACKAGES 同集（docker 入装配层段）
    _PACKAGES = ("shared", "mpconf", "tunnel", "mount", "vpn", "shellui",
                 "capture", "sysctl", "services", "suanpan", "docker")

    @classmethod
    def _section(cls, lines, package):
        marker = f"{package}/ ──"
        start = next((i for i, line in enumerate(lines)
                      if line.startswith(marker)), None)
        if start is None:
            return None
        section = [lines[start]]
        for line in lines[start + 1:]:
            if not line.strip():
                break
            if line.startswith((" ", "\t")):
                # 缩进行：模块行收录；条目续行（多行描述）跳过但不断节
                if re.match(r"^\s+[\w.]+\.py\s+──", line):
                    section.append(line)
            else:
                break   # 下一节标记/栅栏结束——列 0 起始
        return "\n".join(section)

    def test_every_module_mentioned(self):
        module_map = MODULE_MAP_MD.read_text(encoding="utf-8")
        lines = module_map.splitlines()
        covered_pkgs = set(self._PACKAGES)
        root_section = "\n".join(lines)
        for pkg in self._PACKAGES:
            section = self._section(lines, pkg)
            assert section is not None, (
                f"模块地图缺「{pkg}/ ──」模块清单段（防漂移守卫按域钉"
                f"全量——新增域须同步文档）")
            modules = {p.name for p in (ROOT / pkg).glob("*.py")} - _SKIP
            for name in sorted(modules):
                assert name in section, (
                    f"{pkg}/{name} not mentioned in its module-map section")
            covered_pkgs.discard(pkg)
        # 根目录模块（app.py/util.py）在任何段之外提及即可
        root_modules = {p.name for p in ROOT.glob("*.py")} - _SKIP
        for name in sorted(root_modules):
            assert name in root_section, (
                f"{name} not mentioned in module-map root module list")


class TestSettingsNavigationDocumented:
    """The volatile AI routing sidebar list must come from the UI registry."""

    def test_ui_doc_ai_router_views_match_registry(self):
        html = (ROOT / "shellui" / "config_ui.html").read_text(encoding="utf-8")
        titles = re.findall(
            r"\w+:\{group:'AI 路由',title:'([^']+)'", html)
        assert titles, "no AI routing views found in config_ui VIEWS registry"
        navigation_line = next(
            line for line in UI_MD.read_text(encoding="utf-8").splitlines()
            if line.startswith("**偏好设置：**")
        )
        expected = "AI 路由（" + " / ".join(titles) + "）"
        assert expected in navigation_line, \
            f"UI documentation settings navigation drifted; expected {expected!r}"


if __name__ == "__main__":
    import unittest
    unittest.main()
