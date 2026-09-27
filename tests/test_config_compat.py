"""Backward-compat tests for config schema changes (batch 1).

Covers requirement 1 (delete "启动终端"): old configs that still carry the
``terminal_envs`` key must load without error and the field must be silently
dropped on the next save — no destructive migration, no crash.
"""
import json
import os
import tempfile
import unittest

from mpconf import config
class TestTerminalEnvsDropped(unittest.TestCase):
    def _old_cfg(self):
        return {
            "socks5_port": 1080,
            "http_listen_port": 8888,
            "proxy_server_id": "",
            "servers": [
                {"name": "demo", "ssh": {"user": "u", "host": "h",
                                         "port": 22, "auth_type": "key",
                                         "ssh_key": "",
                                         "compression": True}}
            ],
            "terminal_envs": [
                {"name": "old profile", "env": "FOO=bar\nBAZ=qux"}
            ],
        }

    def test_merge_config_drops_terminal_envs(self):
        merged = config.merge_config(self._old_cfg())
        self.assertNotIn("terminal_envs", merged)
        self.assertNotIn("terminal_envs", config.DEFAULT_CONFIG)

    def test_load_then_save_drops_terminal_envs(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, ".magic-proxy.json")
            with open(cfg_path, "w") as f:
                json.dump(self._old_cfg(), f)

            cfg = config.load_config(cfg_path)
            self.assertIsNotNone(cfg, "load should not crash on old schema")
            merged = config.merge_config(cfg)
            self.assertNotIn("terminal_envs", merged)
            config.save_config(merged, cfg_path)
            with open(cfg_path) as f:
                on_disk = json.load(f)
            self.assertNotIn("terminal_envs", on_disk)


class TestConfigValidation(unittest.TestCase):
    def test_invalid_values_fall_back_safely(self):
        merged = config.merge_config({
            "http_listen_port": 99999,
            "socks5_port": "bad",
            "capture_port": -1,
            "retention_days": "bad",
            "proxy_server_id": 5,
            "capture_dir": "~/captures",
            "servers": [{"ssh": {"host": " example.com ", "port": 70000}}],
        })
        self.assertEqual(merged["http_listen_port"], 8888)
        self.assertEqual(merged["socks5_port"], 1080)
        self.assertEqual(merged["capture_port"], 8080)
        self.assertEqual(merged["retention_days"], 7)
        self.assertEqual(merged["servers"][0]["ssh"]["port"], 22)
        self.assertTrue(os.path.isabs(merged["capture_dir"]))

    def test_non_object_config_is_backed_up_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, ".magic-proxy.json")
            with open(cfg_path, "w") as f:
                json.dump([], f)
            self.assertIsNone(config.load_config(cfg_path))
            self.assertTrue(os.path.exists(cfg_path + ".bak"))


class TestHttpListenPortBackcompat(unittest.TestCase):
    """Old configs stored ``http_listen`` as a "host:port" string. The new
    schema reads ``http_listen_port`` (int). Legacy values must convert."""

    def test_old_http_listen_string_converted_to_port(self):
        merged = config.merge_config({"http_listen": "127.0.0.1:8888"})
        self.assertEqual(merged["http_listen_port"], 8888)
        self.assertNotIn("http_listen", merged)

    def test_old_http_listen_non_loopback_silently_drops(self):
        # 0.0.0.0 host: legacy value parses for the port, but merge_config's
        # int range check (1-65535) is the only gate left. Host is always
        # treated as loopback now.
        merged = config.merge_config({"http_listen": "0.0.0.0:8888"})
        self.assertEqual(merged["http_listen_port"], 8888)

    def test_explicit_port_wins_over_legacy_string(self):
        merged = config.merge_config({
            "http_listen": "127.0.0.1:7777",
            "http_listen_port": 9999,
        })
        self.assertEqual(merged["http_listen_port"], 9999)


class TestPreventSleepLaunchLoginDefaults(unittest.TestCase):
    def test_new_fields_default_false_when_absent(self):
        old = {
            "socks5_port": 1080,
            "http_listen_port": 8888,
            "servers": [],
        }
        merged = config.merge_config(old)
        self.assertFalse(merged["prevent_sleep"])
        self.assertFalse(merged["launch_at_login"])

    def test_non_bool_values_reset_to_false(self):
        merged = config.merge_config({"prevent_sleep": "yes", "launch_at_login": 1})
        self.assertIs(merged["prevent_sleep"], False)
        self.assertIs(merged["launch_at_login"], False)

    def test_bool_true_round_trips(self):
        merged = config.merge_config({"prevent_sleep": True, "launch_at_login": True})
        self.assertIs(merged["prevent_sleep"], True)
        self.assertIs(merged["launch_at_login"], True)


class TestMergeConfigServers(unittest.TestCase):
    def test_server_missing_fields_get_defaults(self):
        merged = config.merge_config({"servers": [{"ssh": {"host": "srv"}}]})
        t = merged["servers"][0]
        self.assertEqual(t["ssh"]["port"], 22)
        self.assertEqual(t["ssh"]["auth_type"], "key")
        self.assertTrue(t["ssh"]["compression"])

    def test_server_port_out_of_range_falls_back(self):
        merged = config.merge_config(
            {"servers": [{"ssh": {"host": "s", "port": 99999}}]})
        self.assertEqual(merged["servers"][0]["ssh"]["port"], 22)

    def test_server_auth_type_validated(self):
        merged = config.merge_config(
            {"servers": [{"ssh": {"host": "s", "auth_type": "bogus"}}]})
        self.assertEqual(merged["servers"][0]["ssh"]["auth_type"], "key")

    def test_server_missing_fields_get_forwards_default(self):
        merged = config.merge_config({"servers": [{"ssh": {"host": "srv"}}]})
        # forwards 缺省得 []（旧配置无感升级，无需迁移脚本）
        self.assertEqual(config.server_forwards(merged["servers"][0]), [])


class TestMergeConfigForwards(unittest.TestCase):
    """端口转发读路径归一：剥未知键、字符串端口兼容、缺省回填、浅拷贝防护。"""

    @staticmethod
    def _cfg(forwards):
        return {"servers": [{"ssh": {"host": "s"},
                             "services": {"ssh": {"forwards": forwards}}}]}

    def test_string_ports_read_compat(self):
        merged = config.merge_config(self._cfg([
            {"local_port": "9000", "remote_host": "db", "remote_port": "5432"},
        ]))
        self.assertEqual(config.server_forwards(merged["servers"][0]), [
            {"local_port": 9000, "remote_host": "db", "remote_port": 5432,
             "enabled": True}])

    def test_unknown_keys_stripped_and_remote_host_defaults(self):
        merged = config.merge_config(self._cfg([
            {"local_port": 9000, "remote_port": 8000, "note": "dropped"},
        ]))
        self.assertEqual(config.server_forwards(merged["servers"][0]), [
            {"local_port": 9000, "remote_host": "127.0.0.1", "remote_port": 8000,
             "enabled": True}])

    def test_invalid_port_falls_to_zero_not_dropped(self):
        """非法端口落 0（下次保存被 prepare 拦），绝不静默丢行。"""
        merged = config.merge_config(self._cfg([
            {"local_port": "abc", "remote_host": "h", "remote_port": 70000},
        ]))
        self.assertEqual(config.server_forwards(merged["servers"][0]), [
            {"local_port": 0, "remote_host": "h", "remote_port": 0,
             "enabled": True}])

    def test_non_dict_rows_and_non_list_dropped(self):
        merged = config.merge_config({"servers": [
            {"ssh": {"host": "s"},
             "services": {"ssh": {"forwards": ["bad", 42,
              {"local_port": 9000, "remote_port": 80}]}}},
            {"ssh": {"host": "s2"},
             "services": {"ssh": {"forwards": "not-a-list"}}},
        ]})
        self.assertEqual(config.server_forwards(merged["servers"][0]), [
            {"local_port": 9000, "remote_host": "127.0.0.1", "remote_port": 80,
             "enabled": True}])
        self.assertEqual(config.server_forwards(merged["servers"][1]), [])

    def test_autostart_defaults_and_normalization(self):
        merged = config.merge_config({"servers": [
            {"ssh": {"host": "a"}},                          # 缺省 False
            {"ssh": {"host": "b"},
             "services": {"ssh": {"autostart": True}}},      # 显式开
            {"ssh": {"host": "c"},
             "services": {"ssh": {"autostart": "yes"}}},     # 非 bool 归 False
        ]})
        autostarts = [s["services"]["ssh"]["autostart"]
                      for s in merged["servers"]]
        self.assertEqual(autostarts, [False, True, False])

    def test_default_list_not_shared_across_servers(self):
        """归一逐层全新构造——forwards 默认 [] 绝不能跨服务器共享同一
        list 对象（后续 append 会串服务器）。"""
        merged = config.merge_config({"servers": [
            {"ssh": {"host": "a"}}, {"ssh": {"host": "b"}},
        ]})
        fa = config.server_forwards(merged["servers"][0])
        fb = config.server_forwards(merged["servers"][1])
        self.assertIsNot(fa, fb)
        fa.append({"local_port": 1, "remote_host": "h", "remote_port": 2})
        self.assertEqual(fb, [])


class TestMergeConfigPorts(unittest.TestCase):
    def test_port_out_of_range_falls_back(self):
        merged = config.merge_config({"socks5_port": 0, "capture_port": 99999, "config_port": 70000})
        self.assertEqual(merged["socks5_port"], 1080)
        self.assertEqual(merged["capture_port"], config.DEFAULT_CAPTURE_PORT)
        self.assertEqual(merged["config_port"], 9528)

    def test_http_listen_port_out_of_range_falls_back(self):
        merged = config.merge_config({"http_listen_port": 70000})
        self.assertEqual(merged["http_listen_port"], config.DEFAULT_CONFIG["http_listen_port"])

    def test_dangling_proxy_id_resets_to_first(self):
        merged = config.merge_config({
            "proxy_server_id": "gone",
            "servers": [{"id": "t-a", "ssh": {"host": "s"}}]})
        self.assertEqual(merged["proxy_server_id"], "t-a")

    def test_capture_dir_expands_home(self):
        merged = config.merge_config({"capture_dir": "~/captures"})
        self.assertTrue(merged["capture_dir"].startswith(os.path.expanduser("~")))
        self.assertNotIn("~", merged["capture_dir"])


if __name__ == "__main__":
    unittest.main()


class TestProxyRoleResolution(unittest.TestCase):
    """v2 代理角色：proxy_server_id（稳定 id）单一真相——merge 按解析序
    重写悬空值（id 命中 → 首条兜底）。"""

    def _two(self):
        return [
            {"id": "t-a", "ssh": {"host": "a", "port": 22,
                                  "auth_type": "key"}},
            {"id": "t-b", "ssh": {"host": "b", "port": 22,
                                  "auth_type": "key"}},
        ]

    def test_id_truth_preserved_by_merge(self):
        merged = config.merge_config({
            "proxy_server_id": "t-b", "servers": self._two()})
        self.assertEqual(merged["proxy_server_id"], "t-b")

    def test_dangling_id_falls_back_to_first(self):
        merged = config.merge_config({
            "proxy_server_id": "t-gone", "servers": self._two()})
        self.assertEqual(merged["proxy_server_id"], "t-a")

    def test_role_survives_reorder(self):
        reordered = config.merge_config({
            "proxy_server_id": "t-b",
            "servers": [self._two()[1], self._two()[0]]})
        self.assertEqual(reordered["proxy_server_id"], "t-b")

    def test_empty_servers_resets_role(self):
        merged = config.merge_config({"proxy_server_id": "t-x", "servers": []})
        self.assertEqual(merged["proxy_server_id"], "")

    def test_idless_server_role_stays_empty(self):
        # 新服务器保存时尚未赋 id（下次 load 才赋）——角色保持空，
        # 消费方按首条解析不丢
        merged = config.merge_config({
            "proxy_server_id": "", "servers": [
                {"ssh": {"host": "x", "port": 22}}]})
        self.assertEqual(merged["proxy_server_id"], "")

    def test_v1_role_keys_never_survive_merge(self):
        merged = config.merge_config({
            "current_tunnel": 1, "current_tunnel_id": "t-b",
            "tunnels": self._two()})
        self.assertNotIn("current_tunnel", merged)
        self.assertNotIn("current_tunnel_id", merged)
        self.assertNotIn("tunnels", merged)
        self.assertEqual(merged["servers"], [])


class TestDecorateRuntimeState(unittest.TestCase):
    """C2：/api/state 运行态装饰单一归宿（decorate_runtime_state）——
    is_proxy 与 merge 同一解析序；装饰只写声明的键。"""

    @staticmethod
    def _mp(cid="t-b"):
        return {"proxy_server_id": cid,
                "servers": [{"id": "t-a", "ssh": {"host": "a"}},
                            {"id": "t-b", "ssh": {"host": "b"}}]}

    @staticmethod
    def _proj(capture=False, forwards=(), mounts=()):
        from shared.runtime_state import RuntimeProjection
        return RuntimeProjection(capture_active=capture,
                                 forwards=tuple(forwards),
                                 mounts=tuple(mounts))

    def test_decorates_all_four_declared_fields(self):
        from types import SimpleNamespace as NS
        proj = self._proj(
            capture=True,
            forwards=[NS(tunnel_id="t-a", status="connected")],
            mounts=[NS(tunnel_id="t-b", name="data", status="mounted",
                       error="", fixable="")])
        mp = config.decorate_runtime_state(self._mp(), proj)
        self.assertTrue(mp["capture_active"])
        self.assertFalse(mp["servers"][0]["is_proxy"])
        self.assertTrue(mp["servers"][1]["is_proxy"])
        self.assertTrue(mp["servers"][0]["forward_running"])
        self.assertFalse(mp["servers"][1]["forward_running"])
        self.assertEqual(mp["servers"][1]["nfs_states"],
                         {"data": {"status": "mounted", "error": "",
                                   "fixable": ""}})
        self.assertEqual(mp["servers"][0]["nfs_states"], {})

    def test_absent_projection_decorates_empty(self):
        mp = config.decorate_runtime_state(self._mp(), None)
        self.assertFalse(mp["capture_active"])
        for t in mp["servers"]:
            self.assertFalse(t["forward_running"])
            self.assertEqual(t["nfs_states"], {})
        # 角色解析与 merge 同源：id 命中不缺席
        self.assertTrue(mp["servers"][1]["is_proxy"])

    def test_is_proxy_fallback_matches_merge(self):
        # id 真相缺席/失配 → 首条兜底：与 merge_config 写回的解析一致
        raw = {"proxy_server_id": "", "servers": [{"id": "t-a"},
                                                  {"id": "t-b"}]}
        mp = config.decorate_runtime_state(dict(raw), None)
        self.assertTrue(mp["servers"][0]["is_proxy"])
        raw2 = {"proxy_server_id": "ghost",
                "servers": [{"id": "t-a"}, {"id": "t-b"}]}
        mp2 = config.decorate_runtime_state(dict(raw2), None)
        self.assertTrue(mp2["servers"][0]["is_proxy"])  # 首条兜底
        self.assertEqual(
            config.merge_config(dict(raw2))["proxy_server_id"], "t-a")

    def test_writes_exactly_the_declared_set(self):
        # 装饰只写 RUNTIME_DECORATED_FIELDS；strip 名单运行态半边同源派生
        mp = config.decorate_runtime_state(self._mp(), self._proj())
        written = ({"capture_active", "vpn_state"}
                   | {k for t in mp["servers"]
                      for k in t
                      if k not in ("id", "name", "ssh", "services")})
        self.assertEqual(written, set(config.RUNTIME_DECORATED_FIELDS))
        from mpconf.config_state import READONLY_DECORATED_FIELDS
        self.assertEqual(READONLY_DECORATED_FIELDS,
                         {"has_password"} | config.RUNTIME_DECORATED_FIELDS)

    def test_degraded_error_state_still_decorates(self):
        mp = config.decorate_runtime_state({"_load_error": "装载失败"}, None)
        self.assertFalse(mp["capture_active"])
