"""ConfigStateStore（issue #6）：MP + SP + Keychain 的唯一事务边界.

load() / prepare() / commit() 三段式——候选配置在首次 mutation 前完成
全部校验；提交经 journal 可恢复；invalid 主文件永不覆盖最后已知良好的
.bak；on_sp_saved 只在完整提交后由 commit 触发。
"""
from __future__ import annotations

import json
import logging
import os
from typing import NamedTuple

import yaml

from shared.provider_auth import restore_masked_key
from shared.server_shape import servers as _servers, ssh_node as _ssh_node

logger = logging.getLogger("magic-proxy.config_state")


class LoadResult(NamedTuple):
    mp_state: str          # missing | valid | invalid | io_error
    sp_state: str
    mp_data: dict | None
    sp_data: dict | None
    error: str | None


class CommitPlan(NamedTuple):
    ok: bool
    errors: list
    mp_candidate: dict | None = None
    sp_candidate: dict | None = None
    keychain_sets: list = ()      # [(tunnel_snapshot, password)] 密码只在计划里
    keychain_dels: list = ()


class SaveResult(NamedTuple):
    ok: bool
    stage: str | None   # validate | journal | mp | sp | keychain | callback
    errors: list


def _read_one(path: str, loader):
    """读单个配置文件 → (state, data, error)。"""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return "missing", None, None
    except OSError as exc:
        return "io_error", None, str(exc)
    try:
        data = loader(text)
    except (ValueError, yaml.YAMLError) as exc:
        return "invalid", None, str(exc)
    if not isinstance(data, dict):
        return "invalid", None, "根节点必须是对象"
    return "valid", data, None


# ── prepare：分域校验 orchestrator ──────────────────────────────
# 校验器单一归宿：mpconf.validate（mp 顶层数值 + 隧道级行 + 全局端口/
# 挂载点冲突）与 suanpan.validate（sp schema + 路由引用，经 lazy import
# 保持 ADR-000 无网关依赖宿主的降级）。本文件只持事务边界（journal/
# merge/掩码恢复/Keychain 差量），新域校验在域内生长。


# 服务端注入的只读装饰字段（#52 单点声明）：config_server 读取时注入
# 供 UI 展示，prepare 剥除保证持久化配置永不携带——两侧共用此名单，
# 新增装饰字段不再靠注释对齐。运行态半边派生自 mpconf.config
# （RUNTIME_DECORATED_FIELDS：装饰写入的键即剥除的键，一处声明）。
from mpconf.config import RUNTIME_DECORATED_FIELDS

READONLY_DECORATED_FIELDS = frozenset(
    {"has_password"}) | RUNTIME_DECORATED_FIELDS


def recover_pending_txn() -> bool:
    """启动即重放残留 journal（#48 T6a）——跨文件崩溃后幂等补齐；残留
    journal 只会永久阻塞后续提交。macOS（LifecycleRuntime.start_all）
    与 Docker（entry 的 serve/config-ui 两模式）共用的单一归宿。

    无 handlers 时 logging 的 lastResort 兜底会把 WARNING 落 stderr
    （容器形态可见），两种形态同一呈现。
    """
    if not ConfigStateStore().recover():
        logger.warning("配置事务 journal 恢复失败——保留现场待人工检查")
        return False
    return True


class ConfigStateStore:
    def __init__(self, mp_path=None, sp_path=None, keychain=None):
        from shared import config_store as _cs
        self.mp_path = mp_path or _cs.get_path("mp")
        self.sp_path = sp_path or _cs.get_path("sp")
        self._keychain = keychain

    def load(self) -> LoadResult:
        mp_state, mp_data, mp_err = _read_one(self.mp_path, json.loads)
        sp_state, sp_data, sp_err = _read_one(self.sp_path, yaml.safe_load)
        return LoadResult(mp_state, sp_state, mp_data, sp_data,
                          mp_err or sp_err)

    def prepare(self, mp=None, sp=None, *,
                 skip_server_rows=False) -> CommitPlan:
        """分域校验 orchestrator：任何失败都不触碰磁盘。

        校验顺序与文案被测试钉死；merge 默认值必须在校验之后
        （merge_config 会把非法端口/负保留静默重置为默认，前置会让
        数值约束在真实入口永不触发）。skip_server_rows=True 跳过
        逐服务器行规则（字段级 upsert 用——铸造 local token 不该
        被既有空 host 行连坐：R9 交互缺陷修复，token 曾因此每次
        调用轮换、已分发的 ANTHROPIC_AUTH_TOKEN 静默 401）。
        """
        from mpconf import validate as _mp_validate
        errors = []
        mp_c = mp if isinstance(mp, dict) else None
        sp_c = sp if isinstance(sp, dict) else None

        if mp_c is not None:
            errors += _mp_validate.numeric_errors(mp_c)
            if not skip_server_rows:
                errors += _mp_validate.server_rows_errors(mp_c)
        if sp_c is not None:
            from suanpan import validate as _sp_validate
            errors += _sp_validate.sp_errors(sp_c)
        if mp_c is not None or sp_c is not None:
            errors += _mp_validate.port_conflict_errors(mp_c, sp_c)
            errors += _mp_validate.mount_dir_conflict_errors(mp_c)

        if errors:
            return CommitPlan(False, errors)
        # merge 默认值必须在校验之后：merge_config 会把非法端口/负保留
        # 静默重置为默认，前置会让 mp 侧数值约束在真实入口永不触发。
        # 掩码意图须在 merge 前捕获——白名单会剥掉 local_client_token_set
        # 信号（非注册字段）
        _restore_local_token = bool(
            isinstance(mp_c, dict) and mp_c.pop("local_client_token_set", False))
        if mp_c is not None:
            if mp_c.get("_load_error"):
                return CommitPlan(False, [
                    f"配置装载失败，已阻止保存以防覆盖：{mp_c['_load_error']}"])
            from mpconf.config import merge_config
            # 密码是 UI 提交的瞬态字段：merge 的白名单归一会剥掉它——
            # 必须先按下标摘出，merge 后再按同位对齐喂给 Keychain 扫描
            # （merge 逐行归一保位不丢行，下标对齐安全）
            _pre_passwords = [
                (t.pop("password", None) if isinstance(t, dict) else None)
                for t in _servers(mp_c)]
            mp_c = merge_config(mp_c)
        if sp_c is not None:
            if sp_c.get("_load_error"):
                return CommitPlan(False, [
                    f"配置装载失败，已阻止保存以防覆盖：{sp_c['_load_error']}"])
            # 掩码 key 恢复（keep 语义单一归宿 provider_auth
            # .restore_masked_key）：按 id 匹配旧档恢复真实 key；legacy 无
            # id 档按名
            sp_c = self._restore_masked_sp_keys(sp_c)
        kc_sets, kc_dels = [], []
        if mp_c is not None:
            import copy
            mp_c = copy.deepcopy(mp_c)
            # 删除的隧道（id 在旧档、不在候选）：双账户清理 secret
            old_mp = self._read_mp_current() or {}
            # 掩码恢复（#66 复核）：UI 回 local_client_token_set 布尔 +
            # 无明文 → 按旧档恢复真实 token；明文永不从 UI 进磁盘
            if _restore_local_token:
                from mpconf.local_token import FIELD as _lt_field
                old_tok = old_mp.get(_lt_field)
                if old_tok:
                    mp_c[_lt_field] = old_tok
            new_ids = {t.get("id") for t in _servers(mp_c)
                       if isinstance(t, dict)}
            for t in _servers(old_mp):
                if isinstance(t, dict) and t.get("id") and t["id"] not in new_ids:
                    kc_dels.append(("all", t))
            for i, t in enumerate(_servers(mp_c)):
                for deco in READONLY_DECORATED_FIELDS:
                    t.pop(deco, None)
                pw = (_pre_passwords[i]
                      if i < len(_pre_passwords) else None)
                _ssh = _ssh_node(t)
                _auth = _ssh.get("auth_type")
                if pw:
                    kc_sets.append((dict(t), pw))
                elif (_auth == "password" and t.get("id")
                      and self._keychain is not None):
                    # issue #8 re-pin——只在 id==当前身份哈希时读 legacy：
                    # 身份编辑过的服务器 id 与地址已脱钩，legacy 账户可能
                    # 属于别的实体（Y 改址到 X 旧地址会串走 X 的密码），
                    # 绝不猜测归属。收敛：写入 id 账户 + legacy-only 删除。
                    from mpconf.config import stable_server_id
                    if t["id"] == stable_server_id(
                            _ssh.get("user", ""), _ssh.get("host", ""),
                            _ssh.get("port", 22)):
                        legacy = {"ssh": dict(_ssh)}
                        old_pw = self._keychain.get_password(legacy)
                        if old_pw:
                            kc_sets.append((dict(t), old_pw))
                            kc_dels.append(("legacy-only", legacy))
                elif "auth_type" in _ssh and _auth != "password":
                    kc_dels.append(dict(t))
        return CommitPlan(True, [], mp_c, sp_c, kc_sets, kc_dels)

    @property
    def journal_path(self):
        return self.sp_path + ".txn.json"

    def _journal_write(self, payload):
        parent = os.path.dirname(self.journal_path) or "."
        if not os.path.isdir(parent):
            os.makedirs(parent, mode=0o700)
        # mkstemp 唯一临时名（#46 同标准：固定 path+".tmp" 名并发互截断）；
        # 失败抛 OSError 由 commit 的回滚路径接住
        import tempfile as _tempfile
        fd, tmp = _tempfile.mkstemp(
            dir=parent, prefix="." + os.path.basename(self.journal_path) + ".",
            suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.journal_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _atomic_install(self, path, text):
        parent = os.path.dirname(path) or "."
        if not os.path.isdir(parent):
            os.makedirs(parent, mode=0o700)  # 首创建目录权限
        from shared import config_store
        if not config_store.atomic_write(path, text):  # 唯一安全写入口
            raise OSError(f"atomic_write failed: {path}")

    def _restore_masked_sp_keys(self, sp_c: dict) -> dict:
        """api_key_set 掩码契约：UI 回传 api_key=null+api_key_set=true 表示
        保留旧 key——按 id（或 legacy 名）从当前磁盘档恢复真实值。"""
        import copy
        sp_c = copy.deepcopy(sp_c)
        sp_c.pop("_load_error", None)  # 装载错误标记永不落盘
        try:
            with open(self.sp_path) as f:
                old = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            old = {}
        old_by_id = {p.get("id"): p for p in (old.get("providers") or {}).values()
                     if isinstance(p, dict) and p.get("id")}
        old_by_name = old.get("providers") or {}
        for name, p in sp_c.get("providers", {}).items():
            if not isinstance(p, dict):
                continue
            old_p = old_by_id.get(p.get("id"))
            if old_p is None:
                legacy = old_by_name.get(name)
                if isinstance(legacy, dict) and not legacy.get("id"):
                    old_p = legacy
            keep = bool(p.pop("api_key_set", False))
            new_key = p.get("api_key")
            p["api_key"] = restore_masked_key(
                new_key, (old_p or {}).get("api_key"), keep)
        top_keep = bool(sp_c.pop("api_key_set", False))
        top_new = sp_c.get("api_key")
        sp_c["api_key"] = restore_masked_key(
            top_new, old.get("api_key"), top_keep)
        return sp_c

    def _read_mp_current(self) -> dict | None:
        try:
            with open(self.mp_path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _current_text(self, path):
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return None

    def _rollback(self, payload):
        """文件段/Keychain 失败后恢复旧内容（尽力而为，异常只记日志）。"""
        for key, path in (("mp_old", self.mp_path), ("sp_old", self.sp_path)):
            if key in payload:
                try:
                    if payload[key] is None:
                        try:
                            os.unlink(path)
                        except FileNotFoundError:
                            pass  # 本就不存在 = 已是旧状态
                    else:
                        self._atomic_install(path, payload[key])
                except OSError:
                    logger.warning("rollback %s 失败，journal 保留待启动恢复", path)
                    return False
        try:
            os.unlink(self.journal_path)
        except OSError:
            pass
        return True

    def commit(self, plan, on_committed=None) -> SaveResult:
        """journal → MP → SP → Keychain → 清 journal → 回调.

        任何文件段/Keychain 失败：尽力回滚两文件到旧内容——不暴露
        「接口失败但部分新状态已生效」；回不成则 journal 保留，下次
        启动 recover() 收敛。
        """
        if not plan.ok:
            return SaveResult(False, "validate", list(plan.errors))
        payload = {}
        if plan.mp_candidate is not None:
            payload["mp"] = json.dumps(plan.mp_candidate, indent=2,
                                       ensure_ascii=False)
        if plan.sp_candidate is not None:
            payload["sp"] = yaml.safe_dump(plan.sp_candidate,
                                           allow_unicode=True, sort_keys=False)
        # *_old 只在对应侧参与本事务时存在；None 表示「事务前文件不存在」，
        # 键缺失表示「该侧不在事务内，回滚不得触碰」
        if "mp" in payload:
            payload["mp_old"] = self._current_text(self.mp_path)
        if "sp" in payload:
            payload["sp_old"] = self._current_text(self.sp_path)
        stage = "journal"
        try:
            if any(k in payload for k in ("mp", "sp")):
                self._journal_write(payload)
            if "mp" in payload:
                stage = "mp"
                self._atomic_install(self.mp_path, payload["mp"])
            if "sp" in payload:
                stage = "sp"
                self._atomic_install(self.sp_path, payload["sp"])
        except OSError as exc:
            self._rollback(payload)
            return SaveResult(False, stage, [f"提交失败：{exc}"])
        # Keychain 段原子性：sets 先行且首个失败立即中止——dels 不执行
        # （旧密码绝不因新密码写失败而丢失）；del 失败属非破坏残留（可
        # 重试），逐条收集继续。任何失败都回滚两文件，不暴露部分新状态。
        keychain_errors = []
        if self._keychain is not None:
            for tunnel, pw in plan.keychain_sets:
                if not self._keychain.set_password(tunnel, pw):
                    keychain_errors.append(
                        f"服务器 {tunnel.get('name', '?')} 的密码保存到钥匙串失败")
                    break
            if not keychain_errors:
                for entry in plan.keychain_dels:
                    if isinstance(entry, tuple):
                        mode, tunnel = entry
                    else:
                        mode, tunnel = "all", entry
                    if mode == "all":
                        ok = self._keychain.delete_password(tunnel)
                        # ADR-007：NFS sudo 槽随隧道删除一并清理（测试
                        # 替身可能无此方法——缺席不视为失败）
                        sudo_del = getattr(self._keychain,
                                           "delete_sudo_password", None)
                        if callable(sudo_del):
                            sudo_del(tunnel)
                    else:
                        ok = self._keychain.delete_legacy_password(tunnel)
                    if not ok:
                        keychain_errors.append(
                            f"服务器 {tunnel.get('name', (tunnel.get('ssh') or {}).get('host', '?'))} 的旧密码清理失败")
        if keychain_errors:
            self._rollback(payload)  # 文件回到旧内容：不暴露部分新状态
            return SaveResult(False, "keychain", keychain_errors)
        try:
            if os.path.exists(self.journal_path):
                os.unlink(self.journal_path)
        except OSError:
            pass
        if on_committed is not None:
            try:
                on_committed()
            except Exception:
                logger.exception("on_committed callback failed")
        return SaveResult(True, None, [])

    def recover(self) -> bool:
        """journal 重放：跨文件崩溃后补齐到一致状态（幂等）。

        损坏 journal 视为无事务清除——残留只会永久阻塞后续提交。
        """
        try:
            with open(self.journal_path) as f:
                payload = json.load(f)
        except FileNotFoundError:
            return True
        except ValueError:
            try:
                os.unlink(self.journal_path)
            except OSError:
                pass
            return True
        try:
            if "mp" in payload:
                self._atomic_install(self.mp_path, payload["mp"])
            if "sp" in payload:
                self._atomic_install(self.sp_path, payload["sp"])
            os.unlink(self.journal_path)
        except OSError:
            return False
        return True

    def update_mp(self, mutate, *, skip_server_rows=False) -> SaveResult:
        """菜单开关的唯一写径（#46 T1a/d）：写前读新 → mutate → 事务写。

        内存副本永不整文件覆写磁盘——stale 副本丢更新的根因即此。读新
        经 load_config（含迁移；IdentityMigrationError 原样上抛，由调用
        方决定弹窗），缺文件时从 merge 默认起步；主文件损坏/不可读时
        拒绝（load_config 会把损坏折叠成 None → merge 默认整文件覆写，
        静默清空用户配置）。随后走与 UI 保存完全相同的 prepare/commit
        管线（校验 + journal + 0600 原子写）。
        """
        from mpconf.config import load_config, merge_config
        mp_state = self.load().mp_state
        if mp_state in ("invalid", "io_error"):
            return SaveResult(False, "validate", [
                f"主配置文件{('损坏' if mp_state == 'invalid' else '不可读')}"
                "，已阻止菜单写入以防覆盖（原内容见 .bak 隔离档）"])
        cfg = load_config(self.mp_path)
        if cfg is None:
            cfg = merge_config(None)
        mutated = mutate(cfg)
        plan = self.prepare(mp=mutated,
                            skip_server_rows=skip_server_rows)
        if not plan.ok:
            return SaveResult(False, "validate", plan.errors)
        return self.commit(plan)


