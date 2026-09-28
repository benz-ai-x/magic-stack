"""Test-session safety net: never touch the user's real config files.

All config I/O resolves paths from config_store.PATHS at call time, so
redirecting this ONE registry makes it impossible for any test to write
the real ~/.magic-proxy.json / ~/.suanpan.yaml / ~/.claude/settings.json.
(This happened in practice: an unmocked TestWriteSp wiped the user's
Suanpan config 3x.)
"""
from unittest.mock import patch

import pytest

from shared import config_store
@pytest.fixture(autouse=True, scope="session")
def _sandbox_real_config_paths(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("real-config-sandbox")
    with patch.dict(config_store.PATHS, {
        "mp": str(sandbox / "magic-proxy.json"),
        "sp": str(sandbox / "suanpan.yaml"),
        "claude_settings": str(sandbox / "claude-settings.json"),
        # ADR-010 M4：Agent 配置目标文件同样永不落真实用户文件
        "codex_config": str(sandbox / "codex-config.toml"),
        "opencode_config": str(sandbox / "opencode.json"),
        "zcode_config": str(sandbox / "zcode-config.json"),
        # vpn profile 目录（spec §4）+ install stamp（R8-C1）——第 7 键
        # 接线（R9：docstring 的「不可能写到真实 ~/.magic-proxy*」承诺
        # 对 VPN 面此前不成立，散装 patch.dict 收敛进会话沙箱）
        "vpn_profiles_dir": str(sandbox / "vpn-profiles"),
    }):
        yield
