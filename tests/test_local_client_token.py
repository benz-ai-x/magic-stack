"""本地客户端 token 契约（issue #9，决策 A×4）.

- 每安装实例专用随机 token（无业务含义），存 `~/.magic-proxy.json` 字段
- 单活轮换：任意时刻一个有效值，轮换即刻作废旧值
- 网关 token 为空=不校验本地客户端，但出站**无条件**剥除一切入站
  Authorization/x-api-key（含 mage-router/本地 token/用户真实值）
- 永不回显明文于 UI/日志/diff（掩码布尔契约）
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mpconf.local_token import get_local_token
from shared.provider_auth import build_outbound_headers


class TestTokenLifecycle(unittest.TestCase):
    def test_empty_host_rows_do_not_rotate_token(self):
        """R9 交互缺陷回归：存量空 host 行（R8-C2 之前可落盘）不得把
        token 铸造连坐成每次调用轮换——分发的 ANTHROPIC_AUTH_TOKEN
        会静默 401。字段级 upsert 跳过逐服务器行校验。"""
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            cfg_path = str(Path(d) / "magic-proxy.json")
            with open(cfg_path, "w") as f:
                _json.dump({"servers": [{"name": "legacy-no-host"}]}, f)
            tok1 = get_local_token(cfg_path)
            tok2 = get_local_token(cfg_path)
            self.assertEqual(tok1, tok2, "空 host 行不得使 token 轮换")
            with open(cfg_path) as f:
                self.assertEqual(_json.load(f).get("local_client_token"), tok1)

    def test_duplicate_id_migration_error_degrades_not_crashes(self):
        """R9：重复 id 的迁移异常（load_config 上抛 IdentityMigrationError，
        ValueError 子类）——Docker 三入口无兜底即 boot 崩；此处按
        「未持久化」降级（token 仍返回、原文件不动）。"""
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            cfg_path = str(Path(d) / "magic-proxy.json")
            dup = {"schema_version": 2, "proxy_server_id": "t-x",
                   "servers": [
                       {"id": "t-x", "ssh": {"host": "a", "port": 22}},
                       {"id": "t-x", "ssh": {"host": "b", "port": 22}}]}
            with open(cfg_path, "w") as f:
                _json.dump(dup, f)
            tok = get_local_token(cfg_path)     # 不上抛
            self.assertTrue(tok)
            with open(cfg_path) as f:
                self.assertEqual(_json.load(f), dup)   # 原文件不动

    def test_corrupt_config_never_overwritten(self):
        """R8-C3 回归：主文件损坏时 token 仍可取，但文件内容绝不覆写
        （此前裸 read-modify-write 会把整文件重写成单键 = 配置蒸发）。"""
        with tempfile.TemporaryDirectory() as d:
            cfg_path = str(Path(d) / "magic-proxy.json")
            with open(cfg_path, "w") as f:
                f.write("{corrupt json")
            tok = get_local_token(cfg_path)
            self.assertTrue(tok)
            with open(cfg_path) as f:
                self.assertEqual(f.read(), "{corrupt json")

    def test_write_goes_through_transaction_store(self):
        """写径经 update_mp：落盘的是完整事务产物（含 schema 默认面），
        不是只含 token 的单键文件。"""
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            cfg_path = str(Path(d) / "magic-proxy.json")
            tok = get_local_token(cfg_path)
            with open(cfg_path) as f:
                data = _json.load(f)
        self.assertEqual(data.get("local_client_token"), tok)
        self.assertIn("servers", data)      # 事务管线产物，非单键覆写

    def test_generate_once_and_persist_0600(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = str(Path(d) / "magic-proxy.json")
            tok = get_local_token(cfg_path)
            self.assertTrue(tok)
            self.assertEqual(len(tok), 32, "token_hex(16) = 32 hex chars")
            # 落盘 0600 + 幂等
            self.assertEqual(stat.S_IMODE(os.stat(cfg_path).st_mode), 0o600)
            self.assertEqual(get_local_token(cfg_path), tok)


class TestUnconditionalOutboundStripping(unittest.TestCase):
    """验收⑤：keyless Provider 出站绝不透传任何入站凭证。"""

    def test_keyless_provider_strips_all_incoming_auth(self):
        incoming = {"Authorization": "Bearer mage-router",
                    "x-api-key": "real-user-secret"}
        out = build_outbound_headers(incoming, api_key=None)
        self.assertNotIn("Authorization", out)
        self.assertNotIn("x-api-key", out)

    def test_keyless_provider_strips_local_token_too(self):
        incoming = {"Authorization": "Bearer lc-abc123"}
        out = build_outbound_headers(incoming, api_key=None)
        self.assertNotIn("Authorization", out)


if __name__ == "__main__":
    unittest.main()


class TestPreviewMasking(unittest.TestCase):
    """验收⑥：preview diff 永不回显 token 明文（新旧两端都掩码）。"""

    def test_preview_never_echoes_local_token(self):
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            cfg_path = str(Path(d) / "magic-proxy.json")
            settings_path = str(Path(d) / "settings.json")
            from mpconf.local_token import get_local_token
            from services.claude_code_setup import preview
            tok = get_local_token(cfg_path)
            with patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value={}), \
                 patch("services.claude_code_setup.config_store.get_path",
                       side_effect=lambda k: cfg_path if k == "mp" else settings_path), \
                 patch("services.claude_code_setup.config_store.PATHS",
                       {"claude_settings": settings_path}):
                pv = preview()
            self.assertNotIn(tok, _json.dumps(pv),
                             "preview diff 不得回显本地 token 明文")
