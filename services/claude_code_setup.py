"""Configure coding agents to route through the Suanpan gateway.

ADR-010 M4：本模块是 Agent 配置写入的唯一归宿——Claude Code 的角色
映射同步（原有，ADR-003 env 契约不变）+ 多 Agent 注册表引擎
（Codex/OpenCode/ZCode，五件套模式沿用 CC 已验证的
plan/preview 共享单一真源、first_write 备份、幂等 already、owned-keys
边界）。全部经 config_store.atomic_write + PATHS（tests 沙箱同管线）。

Claude Code entry points（不变，config_server 三端点继续消费）:
- setup(roles=None) -> {"ok", "action", "msg"}.
- default_roles(force_rules=False) -> UI role-table seed + synced/drift.

Multi-agent entry points（M4 新增，GET /api/agents 与
POST /api/agent-setup-preview / /api/setup-agent 消费）:
- agents_status() -> [{id,label,installed,synced,path}]
- agent_preview(agent_id, options) / agent_setup(agent_id, options)
  → 与 CC preview()/setup() 同形状载荷（target/changes/backup；ok/action/msg）

其余不变：网关地址恒取 sp_config.suanpan_listen()（ADR-002 正典解析器）；
写入各 Agent 配置的是本地回环凭证 local_client_token（非厂商真 key，
真 key 永不出网关进程）；首次写入备份 .bak，重复同步不覆盖。
"""
import json
import logging
import os
import shutil

from shared import config_store
from services import sp_config
logger = logging.getLogger("magic-proxy.claude_code_setup")

# Suanpan prefix rules that map to Claude Code tier roles.
_PREFIX_TO_TIER = {
    "claude-opus": "opus",
    "claude-sonnet": "sonnet",
    "claude-haiku": "haiku",
    "claude-fable": "fable",
}

# tier → 前缀（_PREFIX_TO_TIER 的逆映射）：保存时把角色表回写成
# 网关 tier 规则——两面对同一个用户意图只有一个编辑入口（方案：
# 规则是持久真相，env 是 Claude Code 专属投影；一次保存原子落两面）。
_TIER_TO_PREFIX = {tier: prefix for prefix, tier in _PREFIX_TO_TIER.items()}

# Role definitions: (key, label, env_var, has_model_name).
# "subagent" is a Claude Code concept (CLAUDE_CODE_SUBAGENT_MODEL), not a
# Suanpan routing prefix — it maps to the subagent role in the UI.
_ROLES = [
    ("opus", "Opus", "ANTHROPIC_DEFAULT_OPUS_MODEL", True),
    ("sonnet", "Sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL", True),
    ("fable", "Fable", "ANTHROPIC_DEFAULT_FABLE_MODEL", True),
    ("haiku", "Haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL", True),
    ("subagent", "Subagent", "CLAUDE_CODE_SUBAGENT_MODEL", False),
    ("default", "默认兜底模型", "ANTHROPIC_MODEL", False),
]


def _add_suffix(model: str, ctx_1m: bool) -> str:
    """Append "[1M]" to a model target if enabled and not already present."""
    if not ctx_1m or not model or model.endswith("[1M]"):
        return model
    return model + "[1M]"


def _default_roles_from_sp(sp: dict) -> dict:
    """Derive default role mappings from Suanpan routing rules.

    Returns {role_key: {"model": str, "ctx_1m": True}}.  Used when the caller
    doesn't provide explicit roles (backward compat) or as initial values
    for the UI.
    """
    roles = {}
    if not isinstance(sp, dict):
        return roles

    rules = sp.get("rules") or []
    if not isinstance(rules, list):
        rules = []

    # 前缀命中语义归 router（first_tier_route，与 decide_route 同源）——
    # 延迟导入保住「网关依赖缺失时宿主降级」路径
    from suanpan.router import first_tier_route

    default_target = (sp.get("router") or {}).get("default") or ""

    for prefix, role_key in _PREFIX_TO_TIER.items():
        target = first_tier_route(rules, prefix) or default_target
        if target:
            roles[role_key] = {"model": target, "ctx_1m": True}

    if default_target:
        roles["default"] = {"model": default_target, "ctx_1m": True}
        # #43: subagents use the cheap haiku tier (CONTEXT.md design
        # intent: 子代理用便宜模型), falling back to the default route
        # when no haiku rule exists.
        roles["subagent"] = {
            "model": (roles.get("haiku") or {}).get("model") or default_target,
            "ctx_1m": True,
        }

    return roles


def _env_to_roles(env) -> dict:
    """Inverse of _roles_to_env: parse Claude Code env vars back to roles.

    Round-trip contract: _roles_to_env(_env_to_roles(env)) == env for every
    key _roles_to_env can emit.  The [1M] suffix maps back to ctx_1m;
    *_MODEL_NAME vars map to the role's display name.  Only _ROLES-owned
    keys are read — model vars of other tools are never picked up.
    """
    roles = {}
    if not isinstance(env, dict):
        return roles
    for key, label, env_var, has_name in _ROLES:
        raw = env.get(env_var)
        if not isinstance(raw, str) or not raw.strip():
            continue
        model = raw.strip()
        ctx_1m = model.endswith("[1M]")
        if ctx_1m:
            model = model[:-len("[1M]")]
        if not model:
            continue
        role = {"model": model, "ctx_1m": ctx_1m}
        if has_name:
            name = env.get(env_var + "_NAME")
            if isinstance(name, str) and name.strip():
                role["name"] = name.strip()
        roles[key] = role
    return roles


def _read_current_roles():
    """Parse the live Claude Code env into (roles, synced).

    synced means settings.json's ANTHROPIC_BASE_URL already points at this
    gateway (the same basis _plan uses for first_write) — then the model env
    is our own last write, not another setup's residue.  Missing files,
    read errors and non-dict shapes (#69 R7) degrade to ({}, False): the
    seed falls back to rule derivation.
    """
    try:
        listen = sp_config.suanpan_listen()
        settings_path = config_store.get_path("claude_settings")
        if not settings_path or not os.path.exists(settings_path):
            return {}, False
        with open(settings_path) as f:
            settings = json.load(f)
        if not isinstance(settings, dict):
            return {}, False
        env = settings.get("env")
        if not isinstance(env, dict):
            env = {}
        synced = env.get("ANTHROPIC_BASE_URL") == f"http://{listen}"
        return _env_to_roles(env), synced
    except (OSError, json.JSONDecodeError, ValueError):
        return {}, False


def _seed_projection(roles: dict) -> dict:
    """Comparable projection of a role mapping: {key: (model, ctx_1m)}.

    Display names are cosmetic and never rule-derived, so they don't count
    as drift — only the actual routing targets do.
    """
    return {k: (r.get("model"), bool(r.get("ctx_1m")))
            for k, r in roles.items()
            if isinstance(r, dict) and r.get("model")}


def default_roles(force_rules: bool = False) -> dict:
    """Derive the UI role-table seed plus sync/drift status.

    Seed policy (read-back): when ~/.claude/settings.json already points at
    THIS gateway its live env values are the user's last confirmed sync and
    seed the table (roles absent from env fall back to rule derivation per
    key); otherwise — no file, unreadable, or pointing elsewhere — the
    Suanpan routing rules derive the seed (first-setup guidance).
    force_rules=True (UI「按路由规则重置」) skips the read-back.

    #44: the payload carries the table metadata (order / labels / readonly)
    derived from _ROLES so config_ui.html needs no parallel role list —
    Python is the single source of truth.  The "default" role is rendered
    by a dedicated UI control (ccRenderDefault), not the table, so it is
    excluded from `order` and `labels`.
    """
    derived = _default_roles_from_sp(sp_config.sp_load_raw())
    current, synced = _read_current_roles()
    roles = derived
    if synced and not force_rules:
        roles = {**derived, **current}
    table = [(key, label, has_name)
             for key, label, _env_var, has_name in _ROLES
             if key != "default"]
    return {
        "roles": roles,
        "order": [key for key, _label, _has in table],
        "labels": {key: label for key, label, _has in table},
        "readonly": [key for key, _label, has_name in table if not has_name],
        "synced": synced,
        "drift": synced and _seed_projection(derived) != _seed_projection(current),
    }


def _roles_to_env(roles: dict) -> dict:
    """Convert a role mapping dict to Claude Code env vars.

    Each role produces its env var (with optional [1M] suffix).  Tier roles
    also produce a *_MODEL_NAME display variant: the role's custom "name"
    when set, otherwise the model without the [1M] suffix.
    """
    env = {}
    if not isinstance(roles, dict):
        return env
    for key, label, env_var, has_name in _ROLES:
        role = roles.get(key)
        if not isinstance(role, dict):
            continue
        model = role.get("model", "").strip()
        if not model:
            continue
        # #44: ctx_1m replaced one_m; the old key is still read so a stale
        # settings-window payload (pre-rename) keeps working.
        ctx_1m = bool(role.get("ctx_1m", role.get("one_m", True)))
        env[env_var] = _add_suffix(model, ctx_1m)
        if has_name:
            env[env_var + "_NAME"] = role.get("name", "").strip() or model
    return env


def _mask_old(key, old):
    """Display form of a replaced value. The user's real auth token must
    never leave the process (ADR-002 masking spirit) — only ANTHROPIC_AUTH_TOKEN
    carries a secret; URLs and model names are not sensitive."""
    if key == "ANTHROPIC_AUTH_TOKEN" and old not in (None, "", "mage-router"):
        return "（已设置，不回显）"
    return old


def _plan_rule_changes(roles, sp, allow_add=True):
    """角色表 → 网关 tier 规则 upsert 计划（纯函数，不落盘）。

    语义边界：只更新 match_prefix 精确等于 tier 前缀的既有规则、或
    追加新规则——绝不重排、绝不删除（更细前缀的自定义规则保持原
    首序命中语义）；tier 之外的自定义规则一律不碰。allow_add=False
    （roles 由规则推导的向导路径）只对齐既有规则、不新增——避免
    首次运行把 default 兜底悄悄物化成四条 tier 规则。返回
    (changes, new_rules)：changes 为 [{match_prefix, action, old, new}]。
    """
    rules = [dict(r) for r in (sp.get("rules") or [])
             if isinstance(r, dict)]
    changes = []
    for tier, prefix in _TIER_TO_PREFIX.items():
        target = ((roles.get(tier) or {}).get("model") or "").strip()
        if not target:
            continue  # 角色未选模型：不写规则（清空语义归 sp 编辑面）
        idx = next((i for i, r in enumerate(rules)
                    if r.get("match_prefix") == prefix), None)
        if idx is None:
            if not allow_add:
                continue
            rules.append({"match_prefix": prefix, "route_to": target})
            changes.append({"match_prefix": prefix, "action": "add",
                            "old": None, "new": target})
        elif rules[idx].get("route_to") != target:
            changes.append({"match_prefix": prefix, "action": "replace",
                            "old": rules[idx].get("route_to"),
                            "new": target})
            rules[idx]["route_to"] = target
    return changes, rules


def _unlisted_targets(roles, sp):
    """软警告：角色目标模型不在其供应商清单（glm-5.2 真机案例——上游
    仍可能服务未列模型，故只警示不拦截）。返回 [{role, target}]。"""
    providers = sp.get("providers") or {}
    out = []
    for tier in _TIER_TO_PREFIX:
        target = ((roles.get(tier) or {}).get("model") or "").strip()
        name, _, model = target.partition("/")
        if not name or not model:
            continue
        p = providers.get(name)
        if isinstance(p, dict) and p.get("models") \
                and model not in p["models"]:
            out.append({"role": tier, "target": target})
    return out


def _write_sp_rules(sp, new_rules):
    """经 ConfigStateStore 事务管线把 upsert 后的规则写回 ~/.suanpan.yaml
    （与 UI 保存同一校验 + journal + 原子写）。返回 (ok, errors)。"""
    from mpconf.config_state import ConfigStateStore
    candidate = dict(sp)
    candidate["rules"] = new_rules
    store = ConfigStateStore(keychain=None)
    result = store.save(sp=candidate)
    if not result.ok:
        return False, list(result.errors)
    return True, []


def _plan(roles=None):
    """Compute the complete sync plan WITHOUT writing anything.

    Single source of truth shared by setup() (applies the plan) and
    preview() (reports it), so the diff shown before the write can never
    drift from what the write actually does (#3 验收 9). The loaded settings
    document is never mutated — setup() swaps in plan["new_env"] wholesale.
    """
    listen = sp_config.suanpan_listen()
    gateway_url = f"http://{listen}"
    settings_path = config_store.get_path("claude_settings")
    exists = bool(settings_path) and os.path.exists(settings_path)
    settings = {}
    if exists:
        with open(settings_path) as f:
            settings = json.load(f)
        # 形状守卫（#69 R7）：settings.json 为 JSON 数组/标量时后续
        # .get("env") 裸抛 AttributeError——规整为空 dict 走「新建」分支
        if not isinstance(settings, dict):
            settings = {}
    env = dict(settings.get("env") or {})

    roles_explicit = roles is not None
    if roles is None:
        roles = _default_roles_from_sp(sp_config.sp_load_raw())
    model_env = _roles_to_env(roles)

    # 规则面（方案「规则=持久真相」）：角色表 upsert 网关 tier 规则。
    # already 必须两面都一致才成立——env 已对齐但规则漂移（glm-5.2 真机
    # 案例）时仍要执行写入，漂移才会收敛。推导路径不新增规则。
    sp = sp_config.sp_load_raw()
    rule_changes, new_rules = _plan_rule_changes(
        roles, sp, allow_add=roles_explicit)
    unlisted = _unlisted_targets(roles, sp)

    already = (
        env.get("ANTHROPIC_BASE_URL") == gateway_url
        and env.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS") == "1"
        and all(env.get(k) == v for k, v in model_env.items())
        and not rule_changes
    )
    old_base_url = env.get("ANTHROPIC_BASE_URL") or ""
    # "mage-router" is our own placeholder — only a different, user-set
    # token counts as replaced.
    had_auth_token = env.get("ANTHROPIC_AUTH_TOKEN") not in (None, "", "mage-router")
    # First write = settings did not yet point at this gateway. Only then
    # is the .bak the user's pre-gateway state; re-runs must not clobber
    # it with an already-configured state.
    first_write = old_base_url != gateway_url

    # Wipe-set derived from _ROLES so it only ever removes keys this module
    # owns (never user-set ANTHROPIC_DEFAULT_*_MODEL vars of other tools).
    model_keys = {env_var for _, _, env_var, _ in _ROLES}
    model_keys |= {env_var + "_NAME" for _, _, env_var, has_name in _ROLES
                   if has_name}

    new_env = dict(env)
    new_env["ANTHROPIC_BASE_URL"] = gateway_url
    # issue #9：写入当前有效本地 token（替换 mage-router 占位）
    from mpconf.local_token import get_local_token
    new_env["ANTHROPIC_AUTH_TOKEN"] = get_local_token(
        str(config_store.get_path("mp")))
    new_env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    for key in list(new_env.keys()):
        if key in model_keys:
            del new_env[key]
    new_env.update(model_env)

    # Diff rows: fixed trio first, then owned model keys sorted; no-op rows
    # are dropped so the preview shows exactly what will change.
    changes = []
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"):
        old, new = env.get(key), new_env.get(key)
        if old == new:
            continue
        changes.append({"key": key,
                        "action": "add" if old is None else "replace",
                        "old": _mask_old(key, old), "new": new})
    for key in sorted(model_keys):
        old, new = env.get(key), new_env.get(key)
        if old == new:
            continue
        if new is None:
            changes.append({"key": key, "action": "remove",
                            "old": old, "new": None})
        else:
            changes.append({"key": key,
                            "action": "add" if old is None else "replace",
                            "old": old, "new": new})

    return {
        "settings_path": settings_path, "exists": exists,
        "settings": settings, "new_env": new_env, "model_env": model_env,
        "already": already, "gateway_url": gateway_url,
        "old_base_url": old_base_url, "had_auth_token": had_auth_token,
        "first_write": first_write, "changes": changes,
        "rule_changes": rule_changes, "new_rules": new_rules,
        "sp": sp, "unlisted": unlisted,
    }


def _backup_decision(path: str, exists: bool, first_write: bool) -> dict:
    """备份三态（CC 与多 Agent 预览共用单一归宿）：目标不存在 = 新建
    无需备份；首次接入 = 写前备份 .bak；此后 = 保留首次备份不覆盖。"""
    if not exists:
        return {"will": False, "path": None,
                "note": "目标文件不存在，将新建（无需备份）"}
    if first_write:
        return {"will": True, "path": path + ".bak",
                "note": "首次接入网关：写入前当前文件先备份为 .bak（之后的重复同步不再覆盖该备份）"}
    return {"will": False, "path": path + ".bak",
            "note": "已指向本网关；保留首次同步前创建的 .bak 备份不变"}


def preview(roles=None):
    """Read-only dry run of setup(): target path, per-key env diff and the
    backup decision — without touching disk. Backs the settings-window
    confirmation dialog (#3 验收 9); old token values are masked, never
    echoed."""
    try:
        plan = _plan(roles)
        if plan["already"]:
            return {"ok": True, "already": True, "changes": [],
                    "rule_changes": [], "unlisted": [],
                    "target": plan["settings_path"], "exists": plan["exists"],
                    "gateway_url": plan["gateway_url"],
                    "backup": {"will": False, "path": None,
                               "note": "已指向本网关且映射与路由规则一致，重复同步不会写入，也不覆盖既有备份"}}
        backup = _backup_decision(plan["settings_path"], plan["exists"],
                                  plan["first_write"])
        # preview() 对外掩码 token（_plan 保留实值供防漂移守卫；UI/diff
        # 永不回显明文——决策 A×4 的掩码契约）
        masked = [({**c, "new": _mask_old(c["key"], c["new"])}
                   if c["key"] == "ANTHROPIC_AUTH_TOKEN" else c)
                  for c in plan["changes"]]
        return {"ok": True, "already": False,
                "target": plan["settings_path"], "exists": plan["exists"],
                "gateway_url": plan["gateway_url"],
                "changes": masked, "backup": backup,
                "rule_changes": plan["rule_changes"],
                "unlisted": plan["unlisted"]}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "msg": str(e)}


def setup(roles=None):
    """Configure ~/.claude/settings.json to route Claude Code through the gateway.

    Writes ANTHROPIC_BASE_URL to the gateway address, writes the local
    client token (issue #9——stored in ~/.magic-proxy.json, never echoed in
    plaintext), writes model mappings (from explicit roles or derived from
    Suanpan routing rules), and sets CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1.
    Backs up the original settings on the first write only (see module
    docstring); replaced user-set values are reported in the returned msg.

    Returns {"ok": bool, "action": "added"|"already"|"failed", "msg": str}.
    """
    try:
        plan = _plan(roles)
        if plan["already"]:
            return {"ok": True, "action": "already",
                    "msg": f"Claude Code 已指向本网关 ({plan['gateway_url']})，映射与路由规则均一致，无需重复配置"}

        # 规则面先行：校验+事务写 ~/.suanpan.yaml（失败时 settings.json
        # 未动，整体报失败可安全重试）。tier 规则 upsert 与 env 写入同源
        # 于一次保存——两面漂移自此结构上不可再发生。
        rules_written = False
        if plan["rule_changes"]:
            ok, errors = _write_sp_rules(plan["sp"], plan["new_rules"])
            if not ok:
                return {"ok": False, "action": "failed", "rules_written": False,
                        "msg": "网关路由规则写入被拒：" + "；".join(errors)[:200]}
            rules_written = True

        settings_path = plan["settings_path"]
        settings = plan["settings"]
        settings["env"] = plan["new_env"]

        text = json.dumps(settings, indent=2, ensure_ascii=False)
        ok = config_store.atomic_write(settings_path, text, backup=plan["first_write"])
        if not ok:
            return {"ok": False, "action": "failed", "rules_written": rules_written,
                    "msg": f"写入 {settings_path} 失败"}

        n_models = len([k for k in plan["model_env"] if not k.endswith("_NAME")])
        msg = f"已配置 → {plan['gateway_url']}，写入 {n_models} 个模型映射，重启 Claude Code 生效"
        if rules_written:
            msg += f"，同步 {len(plan['rule_changes'])} 条 tier 路由规则"
            if plan["unlisted"]:
                msg += f"（{len(plan['unlisted'])} 项目标不在供应商清单，请确认可用性）"
        msg += "，已启用供应商兼容模式"
        if plan["first_write"]:
            msg += "，原配置备份: settings.json.bak"
        if plan["old_base_url"] and plan["old_base_url"] != plan["gateway_url"]:
            msg += f"；已替换原 ANTHROPIC_BASE_URL={plan['old_base_url']}"
        if plan["had_auth_token"]:
            msg += "；原 ANTHROPIC_AUTH_TOKEN 已被替换（明文不回显，原值见备份）"
        return {"ok": True, "action": "added", "msg": msg,
                "rules_written": rules_written}

    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "action": "failed", "msg": str(e)}


# ══ ADR-010 M4：多 Agent 配置注册表引擎 ════════════════════════════
# 三条新车道（codex/opencode/zcode）各一个 plan 函数，输出与 CC _plan 同族
# 的计划 dict；agent_preview/agent_setup 是统一外壳（防漂移：preview 与
# setup 消费同一 plan）。Claude Code 保持原有三端点，不走该外壳（零回归）。

PROVIDER_ID = "magic-router"  # 各 Agent 配置里我们拥有的独立 id
  # Codex 内置 id（openai 等）不可覆盖——独立 id 是官方要求


def _gateway_url() -> str:
    return f"http://{sp_config.suanpan_listen()}"


def _local_token() -> str:
    from mpconf.local_token import get_local_token
    return get_local_token(str(config_store.get_path("mp")))


def _default_model_from_sp() -> str:
    """默认路由目标的模型段（"provider/model" → model；provider,model 文法
    归 router.parse_route_target 所有者）。"""
    from suanpan.router import parse_route_target
    sp = sp_config.sp_load_raw()
    target = ((sp or {}).get("router") or {}).get("default") or ""
    if not target:
        return ""
    _provider, model = parse_route_target(target)
    return model


def _sp_default_models() -> list:
    """默认路由供应商的 models 清单（OpenCode/ZCode 的 models 块种子）。"""
    from suanpan.router import parse_route_target
    sp = sp_config.sp_load_raw() or {}
    target = (sp.get("router") or {}).get("default") or ""
    if not target:
        return []
    provider, _model = parse_route_target(target)
    p = (sp.get("providers") or {}).get(provider) or {}
    models = p.get("models") or []
    return [m for m in models if isinstance(m, str) and m]


def _detect_agent(binary: str, dir_expanded: str) -> bool:
    """安装检测：PATH 二进制或配置目录双检（装了没用过也检测得到）。"""
    if shutil.which(binary):
        return True
    return os.path.isdir(os.path.expanduser(dir_expanded))


def _masked(token: str) -> str:
    return token[:4] + "…" if token else "（空）"


# ── Codex（~/.codex/config.toml，Responses 协议，tomlkit 增量编辑）──

def _codex_plan(options: dict | None) -> dict:
    try:
        import tomlkit
    except ImportError as e:
        raise ValueError("缺少 tomlkit 依赖（pip3 install tomlkit）") from e
    options = options or {}
    path = config_store.get_path("codex_config")
    exists = bool(path) and os.path.exists(path)
    doc = None
    if exists:
        with open(path) as f:
            text = f.read()
        try:
            doc = tomlkit.parse(text)
        except Exception as e:
            raise ValueError(
                f"config.toml 解析失败（{e}）——请手动修正后重试") from e
    if doc is None:
        doc = tomlkit.document()

    model = options.get("model") or _default_model_from_sp()
    if not model:
        raise ValueError("未指定默认模型（Suanpan 默认路由为空，请在向导里选择模型）")

    base_url = f"{_gateway_url()}/v1"  # Codex 自拼 /responses
    token = _local_token()
    old_tbl = ((doc.get("model_providers") or {}).get(PROVIDER_ID)
               if isinstance(doc.get("model_providers"), dict) else None)
    tbl_matches = (isinstance(old_tbl, dict)
                   and old_tbl.get("base_url") == base_url
                   and old_tbl.get("experimental_bearer_token") == token
                   and old_tbl.get("wire_api", "responses") == "responses")
    already = (doc.get("model") == model
               and doc.get("model_provider") == PROVIDER_ID
               and tbl_matches)

    changes = []
    tbl_summary = f"base_url={base_url}，wire_api=responses，token={_masked(token)}"
    changes.append({
        "key": f"model_providers.{PROVIDER_ID}",
        "action": "replace" if old_tbl else "add",
        "old": "（已配置，将更新）" if old_tbl else None,
        "new": tbl_summary})
    for key, old, new in (("model", doc.get("model"), model),
                          ("model_provider", doc.get("model_provider"),
                           PROVIDER_ID)):
        if old != new:
            changes.append({"key": key,
                            "action": "replace" if old is not None else "add",
                            "old": old, "new": new})

    return {
        "agent": "codex", "path": path, "exists": exists, "doc": doc,
        "model": model, "base_url": base_url, "token": token,
        "already": already, "changes": changes,
        # first_write：此前未指向我们 → 本次写入前备份 .bak
        "first_write": doc.get("model_provider") != PROVIDER_ID,
    }


def _codex_apply(plan: dict) -> None:
    import tomlkit
    doc = plan["doc"]
    if doc.get("model_providers") is None:
        doc["model_providers"] = tomlkit.table()
    tbl = tomlkit.table()
    tbl["name"] = "Magic Stack"
    tbl["base_url"] = plan["base_url"]
    tbl["wire_api"] = "responses"
    tbl["experimental_bearer_token"] = plan["token"]
    doc["model_providers"][PROVIDER_ID] = tbl  # merge-not-overwrite：
    # 只替换我们自己的表，用户其余内容（含注释/顺序）由 tomlkit 保留——
    # Codex 自身的 /model 持久化也走增量编辑，整文件重写会毁注释
    doc["model"] = plan["model"]
    doc["model_provider"] = PROVIDER_ID
    config_store.atomic_write(plan["path"], tomlkit.dumps(doc),
                              backup=plan["first_write"])


def _codex_synced() -> bool:
    try:
        import tomlkit
    except ImportError:
        return False
    path = config_store.get_path("codex_config")
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            doc = tomlkit.parse(f.read())
        return doc.get("model_provider") == PROVIDER_ID
    except Exception:  # noqa: BLE001 — 任何解析问题都视为未同步
        return False


# ── JSON 家族共享件（OpenCode/ZCode 同一 owned 槽位语义）──────────

def _old_provider_table(cfg) -> dict | None:
    """cfg.provider[magic-router] 旧表查询（JSON 家族同一槽位）。"""
    provider = cfg.get("provider")
    if not isinstance(provider, dict):
        return None
    return provider.get(PROVIDER_ID)


def _json_provider_plan(agent: str, path, exists: bool, cfg: dict,
                        target: dict, summary: str) -> dict:
    """JSON 家族（OpenCode/ZCode）共享的 plan 半成品：owned 槽位 =
    provider.<PROVIDER_ID> 整表；already = 旧表与新表逐字节相等；
    first_write = 旧表不是 dict（首次接入或异形残留都先备份）。"""
    old_tbl = _old_provider_table(cfg)
    return {
        "agent": agent, "path": path, "exists": exists, "cfg": cfg,
        "target": target, "already": old_tbl == target,
        "changes": [{
            "key": f"provider.{PROVIDER_ID}",
            "action": "replace" if old_tbl else "add",
            "old": "（已配置，将更新）" if old_tbl else None,
            "new": summary}],
        "first_write": not isinstance(old_tbl, dict),
    }


def _json_provider_apply(plan: dict) -> None:
    """JSON 家族共享 apply：只替换 provider.<PROVIDER_ID> 整表，用户其余
    内容原样保留。"""
    cfg = plan["cfg"]
    provider = cfg.get("provider")
    if not isinstance(provider, dict):
        provider = {}
        cfg["provider"] = provider
    provider[PROVIDER_ID] = plan["target"]
    config_store.atomic_write(
        plan["path"], json.dumps(cfg, indent=2, ensure_ascii=False),
        backup=plan["first_write"])


# ── OpenCode（~/.config/opencode/opencode.json，协议可选）──────────

def _opencode_models_block(models_opt) -> dict:
    """models 块（必写——provider 无 models 时 npm 字段被静默忽略的坑）。
    条目：str 或 {id,name,context,output}；限额缺省 200K/8K。"""
    block = {}
    for m in models_opt or []:
        if isinstance(m, str):
            block[m] = {"name": m,
                        "limit": {"context": 200000, "output": 8192}}
        elif isinstance(m, dict) and m.get("id"):
            mid = m["id"]
            block[mid] = {"name": m.get("name") or mid,
                          "limit": {"context": m.get("context", 200000),
                                    "output": m.get("output", 8192)}}
    return block


def _opencode_plan(options: dict | None) -> dict:
    options = options or {}
    path = config_store.get_path("opencode_config")
    exists = bool(path) and os.path.exists(path)
    cfg = {}
    if exists:
        with open(path) as f:
            text = f.read()
        try:
            cfg = json.loads(text)
        except ValueError as e:
            # OpenCode 允许 JSONC（注释）——我们不做有损剥离，安全降级
            raise ValueError(
                "opencode.json 含注释或非标准 JSON，本工具不解析以防损坏——"
                "请手动加入 magic-router provider 块") from e
        if not isinstance(cfg, dict):
            cfg = {}

    protocol = options.get("protocol") or "anthropic"
    if protocol == "anthropic":
        npm, base_url = "@ai-sdk/anthropic", _gateway_url()
    else:
        npm, base_url = "@ai-sdk/openai-compatible", f"{_gateway_url()}/v1"
    token = _local_token()
    models = _opencode_models_block(
        options.get("models") or _sp_default_models())
    if not models:
        raise ValueError("models 清单为空（供应商未配置模型清单）")
    target = {"npm": npm, "name": "Magic Stack",
              "options": {"baseURL": base_url, "apiKey": token},
              "models": models}

    return _json_provider_plan(
        "opencode", path, exists, cfg, target,
        f"npm={npm}，baseURL={base_url}，"
        f"{len(models)} 个模型，apiKey={_masked(token)}")


def _opencode_synced() -> bool:
    path = config_store.get_path("opencode_config")
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            cfg = json.load(f)
        tbl = ((cfg or {}).get("provider") or {}).get(PROVIDER_ID)
        base = (tbl or {}).get("options", {}).get("baseURL", "")
        return base in (_gateway_url(), f"{_gateway_url()}/v1")
    except Exception:  # noqa: BLE001
        return False


# ── ZCode（~/.zcode/v2/config.json，kind=anthropic）────────────────

def _zcode_plan(options: dict | None) -> dict:
    options = options or {}
    path = config_store.get_path("zcode_config")
    exists = bool(path) and os.path.exists(path)
    cfg = {}
    if exists:
        try:
            with open(path) as f:
                cfg = json.load(f)
        except ValueError as e:
            raise ValueError(
                f"config.json 解析失败（{e}）——请手动修正后重试") from e
        if not isinstance(cfg, dict):
            cfg = {}

    token = _local_token()
    models = options.get("models") or _sp_default_models()
    if not models:
        raise ValueError("models 清单为空（供应商未配置模型清单）")
    target = {
        "name": "Magic Stack", "kind": "anthropic",
        "options": {"baseURL": _gateway_url(), "apiKey": token},
        "enabled": True, "source": "custom",
        "models": {m: {"name": m} for m in models if isinstance(m, str)},
    }

    return _json_provider_plan(
        "zcode", path, exists, cfg, target,
        f"kind=anthropic，baseURL={_gateway_url()}，"
        f"{len(target['models'])} 个模型，apiKey={_masked(token)}")


def _zcode_synced() -> bool:
    path = config_store.get_path("zcode_config")
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            cfg = json.load(f)
        tbl = ((cfg or {}).get("provider") or {}).get(PROVIDER_ID)
        return bool(tbl) and isinstance(tbl, dict)
    except Exception:  # noqa: BLE001
        return False


# ── 注册表 + 统一外壳（plan/preview/setup 同源，owned-keys 即整表）──

_AGENT_REGISTRY = {
    "codex": {
        "label": "Codex", "binary": "codex", "dir": "~/.codex",
        "paths_key": "codex_config",
        "plan": _codex_plan, "apply": _codex_apply, "synced": _codex_synced,
        "protocol_hint": "responses",
    },
    "opencode": {
        "label": "OpenCode", "binary": "opencode",
        "dir": "~/.config/opencode", "paths_key": "opencode_config",
        "plan": _opencode_plan, "apply": _json_provider_apply,
        "synced": _opencode_synced, "protocol_hint": "anthropic|openai",
    },
    "zcode": {
        "label": "ZCode", "binary": "zcode", "dir": "~/.zcode",
        "paths_key": "zcode_config",
        "plan": _zcode_plan, "apply": _json_provider_apply, "synced": _zcode_synced,
        "protocol_hint": "anthropic",
    },
}


def agents_status() -> list:
    """GET /api/agents 载荷：检测已装 Agent + 同步态（含 CC——沿用
    default_roles 的 synced 判定，零重复实现）。"""
    status = [{
        "id": "claude-code", "label": "Claude Code",
        "installed": _detect_agent("claude", "~/.claude"),
        "synced": False,
        "protocol_hint": "anthropic",
        "path": config_store.get_path("claude_settings"),
    }]
    # CC synced 复用既有读回判定（读失败/未指向 = False）
    try:
        status[0]["synced"] = bool(default_roles()["synced"])
    except Exception:  # noqa: BLE001 — 状态探测永不抛
        pass
    for agent_id, meta in _AGENT_REGISTRY.items():
        status.append({
            "id": agent_id, "label": meta["label"],
            "installed": _detect_agent(meta["binary"], meta["dir"]),
            "synced": bool(meta["synced"]()),
            "protocol_hint": meta["protocol_hint"],
            "path": config_store.get_path(meta["paths_key"]),
        })
    return status


def agent_preview(agent: str, options: dict | None = None) -> dict:
    """与 CC preview() 同形状：target/exists/gateway_url/changes/backup；
    already=True 时 changes 为空。业务失败是数据（{"ok": False, "msg"}）。"""
    meta = _AGENT_REGISTRY.get(agent)
    if meta is None:
        return {"ok": False, "msg": f"未知 Agent {agent!r}"}
    try:
        plan = meta["plan"](options)
    except (OSError, ValueError) as e:
        return {"ok": False, "msg": str(e)}
    if plan["already"]:
        return {"ok": True, "already": True, "changes": [],
                "target": plan["path"], "exists": plan["exists"],
                "gateway_url": _gateway_url(),
                "backup": {"will": False, "path": None,
                           "note": "已指向本网关且配置一致，无需重复配置"}}
    backup = _backup_decision(plan["path"], plan["exists"],
                              plan["first_write"])
    return {"ok": True, "already": False, "target": plan["path"],
            "exists": plan["exists"], "gateway_url": _gateway_url(),
            "changes": plan["changes"], "backup": backup}


def agent_setup(agent: str, options: dict | None = None) -> dict:
    """与 CC setup() 同形状：{"ok", "action": added|already|failed, "msg"}。"""
    meta = _AGENT_REGISTRY.get(agent)
    if meta is None:
        return {"ok": False, "action": "failed",
                "msg": f"未知 Agent {agent!r}"}
    try:
        plan = meta["plan"](options)
        if plan["already"]:
            return {"ok": True, "action": "already",
                    "msg": f"{meta['label']} 已指向本网关，无需重复配置"}
        meta["apply"](plan)
    except (OSError, ValueError) as e:
        return {"ok": False, "action": "failed", "msg": str(e)}
    msg = f"已配置 {meta['label']} → {_gateway_url()}，重启该 Agent 生效"
    if plan["first_write"]:
        msg += f"，原配置备份: {os.path.basename(plan['path'])}.bak"
    return {"ok": True, "action": "added", "msg": msg}
