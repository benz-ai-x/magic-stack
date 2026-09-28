// Tests for config_ui.html LAYER 1 (pure data/logic, no DOM).
// Source of truth is the HTML file itself — extracted via extract.mjs so the
// tests can never drift from what actually ships in the settings window.
import test from "node:test";
import assert from "node:assert/strict";
import { loadLayer } from "./extract.mjs";

const L = loadLayer(1);

// ── esc ───────────────────────────────────────────────
test("esc escapes HTML metacharacters", () => {
  assert.equal(L.esc(`<a href="x">&'`).slice(0, -1), "&lt;a href=&quot;x&quot;&gt;&amp;");
  assert.equal(L.esc(null), "");
  assert.equal(L.esc(42), "42");
});

// ── normalizeState ────────────────────────────────────
test("normalizeState fills missing containers", () => {
  const S = L.normalizeState({});
  assert.deepEqual(S.mp.servers, []);
  assert.deepEqual(S.sp.providers, {});
  assert.deepEqual(S.sp.router, {});
  assert.deepEqual(S.sp.rules, []);
});

test("normalizeState preserves existing values", () => {
  const S = L.normalizeState({ mp: { servers: [{ ssh: { host: "h" } }] }, sp: { rules: [{}] } });
  assert.equal(S.mp.servers[0].ssh.host, "h");
  assert.equal(S.sp.rules.length, 1);
});

test("normalizeState tolerates null/undefined input", () => {
  assert.deepEqual(L.normalizeState(null).mp.servers, []);
  assert.deepEqual(L.normalizeState(undefined).sp.providers, {});
});

// ── validateConfig ────────────────────────────────────
test("validateConfig accepts a minimal valid config", () => {
  const S = L.normalizeState({ mp: { servers: [{ ssh: { host: "h", port: 22 } }] } });
  assert.deepEqual(L.validateConfig(S), []);
});

test("validateConfig flags missing server host with index", () => {
  const S = L.normalizeState({ mp: { servers: [{ ssh: { host: "" } }] } });
  assert.deepEqual(L.validateConfig(S), ["服务器 1: 未填写地址"]);
});

test("validateConfig flags out-of-range ports", () => {
  const S = L.normalizeState({
    mp: { servers: [{ ssh: { host: "h", port: 70000 } }], socks5_port: 65536, capture_port: 99999, config_port: -1 },
  });
  const errs = L.validateConfig(S);
  assert.ok(errs.some((e) => e.includes("SSH 端口无效")));
  assert.ok(errs.includes("SOCKS5 端口无效"));
  assert.ok(errs.includes("抓包端口无效"));
  assert.ok(errs.includes("配置服务端口无效"));
});

test("validateConfig flags blank provider name", () => {
  const S = L.normalizeState({ sp: { providers: { "  ": {} } } });
  assert.deepEqual(L.validateConfig(S), ["供应商名称不能为空"]);
});

test("validateConfig flags duplicate ports across services", () => {
  const S = L.normalizeState({
    mp: { socks5_port: 8080, capture_port: 8080, config_port: 9528 },
    sp: { listen_port: 9528 },
  });
  const errs = L.validateConfig(S);
  assert.ok(errs.includes("端口冲突：SOCKS5 与 抓包 同为 8080"));
  assert.ok(errs.includes("端口冲突：配置服务 与 路由网关 同为 9528"));
});

test("validateConfig skips port conflict check when ports absent", () => {
  const S = L.normalizeState({});
  assert.deepEqual(L.validateConfig(S), []);
});

// ── NFS 端口进冲突命名空间（镜像 prepare 的「实际在用才占端口」）──
test("validateConfig flags NFS port conflicting with a forward port", () => {
  // 曾漏：NFS×转发撞端口过第一道闸、只在 422 现形
  const S = L.normalizeState({ mp: { servers: [
    { ssh: { host: "a" }, services: { nfs: { enabled: true, local_port: 9000, mounts: [{ name: "d", remote_path: "/d" }] } } },
    { ssh: { host: "b" }, services: { ssh: { forwards: [{ local_port: 9000, remote_host: "127.0.0.1", remote_port: 80 }] } } },
  ] } });
  const errs = L.validateConfig(S);
  assert.ok(errs.some((e) => e.includes("端口冲突") && e.includes("NFS 本地端口") && e.includes("9000")));
});

test("validateConfig flags NFS port conflicting with a global service port", () => {
  const S = L.normalizeState({ mp: {
    socks5_port: 12049,
    servers: [{ ssh: { host: "a" }, services: { nfs: { enabled: false, local_port: 12049, mounts: [{ name: "d", remote_path: "/d" }] } } }],
  } });
  const errs = L.validateConfig(S);
  assert.ok(errs.some((e) => e === "端口冲突：SOCKS5 与 a NFS 本地端口 同为 12049"));
});

test("validateConfig NFS occupies only when enabled or has mounts", () => {
  // merge 会给每条隧道填纯默认 nfs 节（enabled=False、无挂载、12049）——
  // 两条隧道的默认节点不得互报假冲突
  const S = L.normalizeState({ mp: { servers: [
    { ssh: { host: "a" }, services: { nfs: { enabled: false, local_port: 12049, mounts: [] } } },
    { ssh: { host: "b" }, services: { nfs: { enabled: false, local_port: 12049, mounts: [] } } },
  ] } });
  assert.deepEqual(L.validateConfig(S), []);
});

test("validateConfig flags out-of-range NFS local port at row level", () => {
  const S = L.normalizeState({ mp: { servers: [
    { ssh: { host: "a" }, services: { nfs: { enabled: true, local_port: 70000, mounts: [{ name: "d", remote_path: "/d" }] } } },
  ] } });
  assert.ok(L.validateConfig(S).some((e) => e === "a: NFS 本地端口无效（须 1..65535）"));
});

// ── parseAddr ─────────────────────────────────────────
test("parseAddr handles user@host", () => {
  assert.deepEqual(L.parseAddr("ubuntu@example.com"), { user: "ubuntu", host: "example.com" });
});

test("parseAddr handles host only", () => {
  assert.deepEqual(L.parseAddr("example.com"), { user: "", host: "example.com" });
});

test("parseAddr strips trailing port", () => {
  assert.deepEqual(L.parseAddr("u@h.com:2222"), { user: "u", host: "h.com" });
});

test("parseAddr handles bracketed IPv6 with and without port", () => {
  assert.deepEqual(L.parseAddr("u@[::1]:22"), { user: "u", host: "::1" });
  assert.deepEqual(L.parseAddr("[2001:db8::1]"), { user: "", host: "2001:db8::1" });
});

// ── addrStr ───────────────────────────────────────────
test("addrStr renders user@host, host, or empty", () => {
  assert.equal(L.addrStr({ ssh: { user: "u", host: "h" } }), "u@h");
  assert.equal(L.addrStr({ ssh: { host: "h" } }), "h");
  assert.equal(L.addrStr({}), "");
});

// ── fmtNum ────────────────────────────────────────────
test("fmtNum formats K/M thresholds", () => {
  assert.equal(L.fmtNum(500), "500");
  assert.equal(L.fmtNum(1500), "1.5K");
  assert.equal(L.fmtNum(2_500_000), "2.5M");
});

test("fmtRate formats cache hit rates and preserves missing data", () => {
  assert.equal(L.fmtRate(null), "—");
  assert.equal(L.fmtRate(0.42), "42%");
});

// ── quota display (balance quick view) ────────────────
test("quotaPeriodLabel 直显 period（全 API 后无本地聚合行）", () => {
  assert.equal(L.quotaPeriodLabel({ period: "每月" }), "每月");
  assert.equal(L.quotaPeriodLabel({ period: "5小时" }), "5小时");
});

test("quotaNumsText omits the pct segment when pct is null", () => {
  assert.equal(L.quotaNumsText({ pct: 42, used: 42, limit: 100 }), "已用 42% · 42/100");
  assert.equal(L.quotaNumsText({ pct: null, used: 220, limit: null }), "220/—");
  assert.equal(L.quotaNumsText({ pct: null, used: 1523456, limit: null }), "1.5M/—");
  assert.equal(L.quotaNumsText({ pct: 0, used: 0, limit: 100 }), "已用 0% · 0/100");
});

test("quotaNumsText renders call count on rows carrying calls", () => {
  assert.equal(
    L.quotaNumsText({ pct: null, used: 243311832, limit: null, calls: 621 }),
    "243.3M/— · 621 次");
  assert.equal(
    L.quotaNumsText({ pct: null, used: 0, limit: null, calls: 0 }),
    "0/— · 0 次");
});

test("usageRangeOptions marks the selected statistics range", () => {
  assert.deepEqual(L.usageRangeOptions("7d"), [
    { value: "today", label: "今日", active: false },
    { value: "7d", label: "近 7 天", active: true },
    { value: "all", label: "全部", active: false },
  ]);
  assert.equal(L.usageRangeOptions("invalid")[2].active, true);
});

test("routeSourceLabel maps persisted scenarios to domain vocabulary", () => {
  assert.equal(L.routeSourceLabel("inline"), "内联覆盖");
  assert.equal(L.routeSourceLabel("subagent"), "SUBAGENT-MODEL 标签");
  assert.equal(L.routeSourceLabel("rule"), "模型规则");
  assert.equal(L.routeSourceLabel("default"), "默认路由");
  assert.equal(L.routeSourceLabel("future"), "future");
});

test("dailyRowsDesc sorts newest day first without mutating input", () => {
  const daily = [{ date: "2026-08-10" }, { date: "2026-08-19" }, { date: "2026-08-12" }];
  assert.deepEqual(L.dailyRowsDesc(daily).map((d) => d.date),
    ["2026-08-19", "2026-08-12", "2026-08-10"]);
  assert.deepEqual(daily.map((d) => d.date),
    ["2026-08-10", "2026-08-19", "2026-08-12"]);
  assert.deepEqual(L.dailyRowsDesc([]), []);
});

test("pctClass maps usage percent to severity thresholds", () => {
  assert.equal(L.pctClass(null), "");
  assert.equal(L.pctClass(0), "pct-ok");
  assert.equal(L.pctClass(69.9), "pct-ok");
  assert.equal(L.pctClass(70), "pct-warn");
  assert.equal(L.pctClass(89.9), "pct-warn");
  assert.equal(L.pctClass(90), "pct-danger");
});

test("balanceRowModel classifies provider-level states", () => {
  assert.deepEqual(L.balanceRowModel({ provider: "a", enabled: false }),
    { name: "a", state: "disabled", note: "已停用" });
  assert.deepEqual(L.balanceRowModel({ provider: "a", supported: false }),
    { name: "a", state: "unsupported", note: "不支持" });
  assert.deepEqual(L.balanceRowModel({ provider: "a", supported: false, note: "无余额 API" }),
    { name: "a", state: "unsupported", note: "无余额 API" });
  assert.deepEqual(L.balanceRowModel({ provider: "a", supported: true, error: "超时" }),
    { name: "a", state: "error", note: "超时" });
});

test("balanceRowModel splits apis into quota and money zones", () => {
  const m = L.balanceRowModel({ provider: "a", supported: true, apis: [
    { label: "Coding Plan", quotas: [] },
    { label: "账户余额", primary: "¥1.00" },
    { label: "套餐", error: "拉取失败" },
  ] });
  assert.equal(m.state, "ok");
  assert.deepEqual(m.quotaApis.map((a) => a.label), ["Coding Plan", "套餐"]);
  assert.deepEqual(m.moneyApis.map((a) => a.label), ["账户余额"]);
  assert.deepEqual(L.balanceRowModel({ provider: "a", supported: true }).quotaApis, []);
});

// C3：原生端点集合派生自 PROVIDER_TEMPLATES（服务端注册表单一真源）——
// 测试先种子模板（镜像真实 boot 载荷），结束恢复 custom-only 默认
function seedNativeTemplates() {
  L.setProviderTemplates([
    {id: "anthropic", label: "Anthropic", base_url: "https://api.anthropic.com", anthropic_native: true},
    {id: "glm", label: "GLM", base_url: "https://open.bigmodel.cn/api/anthropic", anthropic_native: true},
    {id: "kimi", label: "KIMI", base_url: "https://api.kimi.com/coding", anthropic_native: true},
    {id: "deepseek", label: "DeepSeek", base_url: "https://api.deepseek.com", anthropic_native: false},
  ]);
}
function resetTemplates() {
  L.setProviderTemplates([{id: "custom", label: "自定义"}]);
}

test("providerCacheHint only flags uncached traffic without native mode", () => {
  seedNativeTemplates();
  try {
    const uncached = {
      calls: 2, input_tokens: 100, cache_read_tokens: 0,
      cache_creation_tokens: 0, cache_hit_rate: 0,
    };
    assert.equal(
      L.providerCacheHint({
        base_url: "https://open.bigmodel.cn/api/anthropic",
        anthropic_native: false,
      }, uncached),
      "检查该供应商是否开启 Anthropic 原生模式",
    );
    assert.equal(L.providerCacheHint({
      base_url: "https://open.bigmodel.cn/api/anthropic",
      anthropic_native: true,
    }, uncached), "");
    assert.equal(L.providerCacheHint({
      base_url: "https://open.bigmodel.cn/api/anthropic",
      anthropic_native: false,
    }, {
      ...uncached, cache_hit_rate: null,
    }), "");
    assert.equal(L.providerCacheHint({
      base_url: "https://api.deepseek.com/anthropic", anthropic_native: false,
    }, uncached), "");
    assert.equal(L.providerCacheHint({
      base_url: "https://relay.example.com", anthropic_native: false,
      models: ["deepseek-v4-pro"],
    }, uncached), "");
  } finally {
    resetTemplates();
  }
});

test("supportsAnthropicPromptCaching uses endpoint capabilities, not labels", () => {
  seedNativeTemplates();
  try {
    assert.equal(L.supportsAnthropicPromptCaching({
      base_url: "https://open.bigmodel.cn/api/anthropic/v1",
    }), true);
    assert.equal(L.supportsAnthropicPromptCaching({
      base_url: "https://api.kimi.com/coding",
    }), true);
    assert.equal(L.supportsAnthropicPromptCaching({
      base_url: "https://api.deepseek.com/anthropic",
    }), false);
    assert.equal(L.supportsAnthropicPromptCaching({
      base_url: "https://relay.example.com", models: ["glm-5.2"],
    }), false);
  } finally {
    resetTemplates();
  }
});

test("supportsAnthropicPromptCaching derives from templates, not a parallel table", () => {
  // custom-only（模板未到/纯自定义）= 恒 false；模板声明即生效——
  // 服务端注册表是端点知识的唯一真相，JS 不再平行硬编码
  assert.equal(L.supportsAnthropicPromptCaching({
    base_url: "https://open.bigmodel.cn/api/anthropic",
  }), false);
  L.setProviderTemplates([
    {id: "glm", label: "GLM", base_url: "https://open.bigmodel.cn/api/anthropic", anthropic_native: true},
  ]);
  try {
    assert.equal(L.supportsAnthropicPromptCaching({
      base_url: "https://open.bigmodel.cn/api/anthropic",
    }), true);
    assert.equal(L.supportsAnthropicPromptCaching({
      base_url: "https://open.bigmodel.cn/api/paas/v4",  // openai 卡路径不匹配
    }), false);
  } finally {
    resetTemplates();
  }
});

// ── ctxSuffix ─────────────────────────────────────────
test("ctxSuffix annotates known models only", () => {
  assert.equal(L.ctxSuffix("k3"), "（1M）");
  assert.equal(L.ctxSuffix("unknown-model"), "");
});

// ── routeTargetOptionsFor ─────────────────────────────
test("routeTargetOptionsFor groups by provider and marks current selected", () => {
  const providers = {
    GLM: { enabled: true, models: ["glm-5.2", "glm-5.1"] },
    KIMI: { enabled: true, models: ["k3"] },
  };
  const html = L.routeTargetOptionsFor(providers, "KIMI/k3", "（未设置）");
  assert.match(html, /<optgroup label="GLM">/);
  assert.match(html, /<option value="KIMI\/k3" selected>/);
  assert.match(html, /k3（1M）/);
  assert.match(html, /^<option value="">（未设置）<\/option>/);
});

test("routeTargetOptionsFor keeps unlisted current value so saving cannot drop it", () => {
  const html = L.routeTargetOptionsFor({ A: { enabled: true, models: ["m1"] } }, "OLD/gone", "x");
  assert.match(html, /<option value="OLD\/gone" selected>OLD\/gone（未在清单）<\/option>/);
});

test("routeTargetOptionsFor hides disabled providers unless referenced", () => {
  const providers = {
    OFF: { enabled: false, models: ["m"] },
    ON: { enabled: true, models: ["x"] },
  };
  const hidden = L.routeTargetOptionsFor(providers, "", "x");
  assert.ok(!hidden.includes('label="OFF"'));
  const referenced = L.routeTargetOptionsFor(providers, "OFF/m", "x");
  assert.match(referenced, /<optgroup label="OFF（已停用）">/);
});

test("routeTargetOptionsFor skips providers with no models", () => {
  const html = L.routeTargetOptionsFor({ A: { enabled: true, models: [] } }, "", "x");
  assert.ok(!html.includes("optgroup"));
});

// ── apiHeaders ─────────────────────────────────────────
test("apiHeaders carries no credential — cookie auth only (#10)", () => {
  // issue #10：cookie 承载后 token 形参仅兼容旧签名，不产生头
  assert.deepEqual(L.apiHeaders("abc"), {});
});

test("apiHeaders merges extra headers", () => {
  assert.deepEqual(L.apiHeaders("abc", { "Content-Type": "application/json" }),
    { "Content-Type": "application/json" });
});

// ── ccRoleHint ─────────────────────────────────────────
test("ccRoleHint returns subagent guidance", () => {
  assert.equal(L.ccRoleHint("subagent"), "建议选更便宜模型，区分主任务与子代理");
});

test("ccRoleHint is empty for other roles", () => {
  assert.equal(L.ccRoleHint("sonnet"), "");
  assert.equal(L.ccRoleHint("default"), "");
});

// ── ccRoleRows ────────────────────────────────────────
test("ccRoleRows builds table rows from payload metadata", () => {
  const meta = {
    order: ["sonnet", "subagent"],
    labels: { sonnet: "Sonnet", subagent: "Subagent" },
    readonly: ["subagent"],
  };
  assert.deepEqual(L.ccRoleRows(meta), [
    { key: "sonnet", label: "Sonnet", readonly: false },
    { key: "subagent", label: "Subagent", readonly: true },
  ]);
});

test("ccRoleRows tolerates missing metadata", () => {
  assert.deepEqual(L.ccRoleRows(undefined), []);
  assert.deepEqual(L.ccRoleRows({}), []);
});

test("ccRoleRows falls back to key when label missing", () => {
  assert.deepEqual(L.ccRoleRows({ order: ["opus"], labels: {} }),
    [{ key: "opus", label: "opus", readonly: false }]);
});

// ── ccDriftInfo ───────────────────────────────────────
test("ccDriftInfo shows the banner only when synced AND drifting", () => {
  assert.equal(L.ccDriftInfo({ synced: true, drift: true }).show, true);
  assert.equal(L.ccDriftInfo({ synced: true, drift: false }).show, false);
  // never synced → a fresh draft has nothing to diverge from
  assert.equal(L.ccDriftInfo({ synced: false, drift: true }).show, false);
});

test("ccDriftInfo tolerates payloads without the flags", () => {
  assert.equal(L.ccDriftInfo(undefined).show, false);
  assert.equal(L.ccDriftInfo({}).show, false);
  assert.equal(L.ccDriftInfo({ order: ["opus"] }).show, false);
});

test("ccDriftInfo carries non-empty explanatory text when shown", () => {
  const info = L.ccDriftInfo({ synced: true, drift: true });
  assert.equal(typeof info.text, "string");
  assert.ok(info.text.includes("重置"), "text must point at the reset action");
  assert.equal(info.reset, true, "reset action offered by default");
});

test("ccDriftInfo after an acknowledged reset swaps text, drops the button", () => {
  const info = L.ccDriftInfo({ synced: true, drift: true }, { acked: true });
  assert.equal(info.show, true);
  assert.equal(info.reset, false);
  assert.ok(info.text.includes("保存并同步"), "text points at the save action");
});

// ── ccRoleEntry ───────────────────────────────────────
test("ccRoleEntry creates the sentinel when missing", () => {
  const roles = {};
  const e = L.ccRoleEntry(roles, "opus");
  assert.deepEqual(e, { model: "", ctx_1m: true });
  assert.equal(roles.opus, e); // registered on the state object
});

test("ccRoleEntry returns the existing entry unchanged", () => {
  const existing = { model: "KIMI/k3", ctx_1m: false };
  const roles = { opus: existing };
  assert.equal(L.ccRoleEntry(roles, "opus"), existing);
});

// ── settings dirty-state snapshots ────────────────────
test("countChanges reports real leaf differences and clears on revert", () => {
  const baseline = { enabled: false, nested: { port: 9527 }, models: ["a"] };
  const changed = structuredClone(baseline);
  changed.enabled = true;
  changed.nested.port = 9528;
  changed.models.push("b");
  assert.equal(L.countChanges(baseline, changed), 3);
  assert.equal(L.countChanges(baseline, structuredClone(baseline)), 0);
});

test("viewSnapshot normalizes rendered defaults to avoid false dirty state", () => {
  const loaded = L.normalizeState({});
  const collected = L.normalizeState({
    mp: {
      socks5_port: 1080,
      http_listen_port: 8888,
      capture_port: 8080,
      capture_dir: "~/.magic-proxy-captures",
      retention_days: 7,
      config_port: 9528,
      system_proxy_default: false,
      prevent_sleep: false,
      launch_at_login: false,
    },
    sp: { listen_port: 9527 },
  });
  assert.equal(
    L.countChanges(L.viewSnapshot("proxy", loaded), L.viewSnapshot("proxy", collected)),
    0,
  );
  assert.equal(
    L.countChanges(L.viewSnapshot("system", loaded), L.viewSnapshot("system", collected)),
    0,
  );
});

test("viewSnapshot detects server edits but treats implicit defaults equally", () => {
  const loaded = L.normalizeState({ mp: { servers: [{ ssh: { host: "host" } }] } });
  const rendered = L.normalizeState({
    mp: {
      proxy_server_id: "",
      servers: [{
        name: "",
        ssh: { user: "", host: "host", port: 22, auth_type: "key",
               ssh_key: "", compression: true },
        services: { ssh: { forwards: [], autostart: false } },
      }],
    },
  });
  assert.equal(
    L.countChanges(L.viewSnapshot("servers", loaded), L.viewSnapshot("servers", rendered)),
    0,
  );
  rendered.mp.servers[0].ssh.host = "other";
  assert.equal(
    L.countChanges(L.viewSnapshot("servers", loaded), L.viewSnapshot("servers", rendered)),
    1,
  );
});

test("viewSnapshot isolates provider and Claude Code changes by view", () => {
  const state = L.normalizeState({
    sp: {
      providers: { P: { base_url: "https://example.com", enabled: true } },
      router: { default: "P/m" },
      rules: [],
    },
  });
  const providerBaseline = L.viewSnapshot("providers", state);
  const rulesBaseline = L.viewSnapshot("rules", state, {
    sonnet: { model: "P/m", name: "m", ctx_1m: true },
  });

  state.sp.providers.P.enabled = false;
  assert.equal(L.countChanges(providerBaseline, L.viewSnapshot("providers", state)), 1);
  assert.equal(
    L.countChanges(rulesBaseline, L.viewSnapshot("rules", state, {
      sonnet: { model: "P/m", name: "m", ctx_1m: true },
    })),
    0,
  );

  const changedRoles = {
    sonnet: { model: "P/other", name: "other", ctx_1m: true },
  };
  assert.equal(L.countChanges(rulesBaseline, L.viewSnapshot("rules", state, changedRoles)), 2);
});

test("validateConfig range-checks every service port, not just three", () => {
  // 回归：HTTP 监听曾漏检，99999 一路绿灯写入真实配置（实测踩坑）
  const base = { mp: { servers: [] }, sp: {} };
  const cases = [
    [{ socks5_port: 99999 }, "SOCKS5 端口无效"],
    [{ http_listen_port: 99999 }, "HTTP 监听端口无效"],
    [{ capture_port: 99999 }, "抓包端口无效"],
    [{ config_port: 99999 }, "配置服务端口无效"],
  ];
  for (const [patch, msg] of cases) {
    const errs = L.validateConfig({ mp: { servers: [], ...patch }, sp: {} });
    assert.ok(errs.includes(msg), `${JSON.stringify(patch)} → ${msg}`);
  }
  const gw = L.validateConfig({ mp: { servers: [] }, sp: { listen_port: 70000 } });
  assert.ok(gw.includes("路由网关端口无效"));
  // 合法值不误报
  assert.deepEqual(L.validateConfig({
    mp: { servers: [], socks5_port: 1080, http_listen_port: 8888 },
    sp: { listen_port: 9527 },
  }), []);
  void base;
});

// ── PROVIDER_TEMPLATES / applyProviderTemplate（#3 模板应用语义）──
// #51：模板数据单一真源已移 Python（PROVIDER_REGISTRY，经
// /api/provider-templates；端点测试在 tests/test_cov_config.py）。
// 此处 seed 注册表形状的夹具，钉「应用」语义。
test("供应商模板应用：原生端点并开启 Anthropic 原生协议", () => {
  L.setProviderTemplates([
    {id: "glm", label: "GLM", base_url: "https://open.bigmodel.cn/api/anthropic", anthropic_native: true},
    {id: "kimi", label: "KIMI", base_url: "https://api.kimi.com/coding", anthropic_native: true},
    {id: "deepseek", label: "DeepSeek", base_url: "https://api.deepseek.com", anthropic_native: false},
    {id: "custom", label: "自定义"},
  ]);
  const p = L.applyProviderTemplate({}, "glm");
  assert.equal(p.base_url, "https://open.bigmodel.cn/api/anthropic");
  assert.equal(p.anthropic_native, true);
  const k = L.applyProviderTemplate({}, "kimi");
  assert.equal(k.base_url, "https://api.kimi.com/coding");
  assert.equal(k.anthropic_native, true);
  const d = L.applyProviderTemplate({}, "deepseek");
  assert.equal(d.base_url, "https://api.deepseek.com");
  assert.equal(d.anthropic_native, false);
});

test("setProviderTemplates 拒绝空数组（不清空既有模板）", () => {
  L.setProviderTemplates([]);
  const p = L.applyProviderTemplate({}, "glm");
  assert.equal(p.base_url, "https://open.bigmodel.cn/api/anthropic",
               "空 seed 不得清空现有模板");
  L.setProviderTemplates("not-an-array");
  assert.equal(L.applyProviderTemplate({}, "glm").base_url,
               "https://open.bigmodel.cn/api/anthropic");
});

test("自定义与未知模板不改动任何现有字段", () => {
  const p = { base_url: "", anthropic_native: true, api_key: "k", models: ["m"] };
  L.applyProviderTemplate(p, "custom");
  assert.equal(p.base_url, "");
  assert.equal(p.anthropic_native, true);
  L.applyProviderTemplate(p, "no-such-template");
  assert.deepEqual(p, { base_url: "", anthropic_native: true, api_key: "k", models: ["m"] });
});

test("模板只填端点与协议模式，凭证与模型保持原样", () => {
  const p = { base_url: "", api_key: "secret", api_key_env: "K", models: ["m"], enabled: false };
  L.applyProviderTemplate(p, "glm");
  assert.equal(p.api_key, "secret");
  assert.equal(p.api_key_env, "K");
  assert.deepEqual(p.models, ["m"]);
  assert.equal(p.enabled, false);
});

// ── credentialMode（#3 凭证互斥）─────────────────────
test("凭证模式：直接 key 与环境变量都有值时 key 优先（对齐 resolve_api_key）", () => {
  assert.equal(L.credentialMode({ api_key: "sk-x", api_key_env: "ENV" }), "key");
  assert.equal(L.credentialMode({ api_key: "sk-x" }), "key");
});

test("凭证模式：仅环境变量有值时进入 env 模式（掩码 key 不参与推导）", () => {
  assert.equal(L.credentialMode({ api_key: null, api_key_env: "ENV" }), "env");
  assert.equal(L.credentialMode({ api_key: null, api_key_set: true, api_key_env: "ENV" }), "env");
});

test("凭证模式：两者皆空与空 provider 默认直接填写", () => {
  assert.equal(L.credentialMode({}), "key");
  assert.equal(L.credentialMode({ api_key: null, api_key_env: null }), "key");
  assert.equal(L.credentialMode(null), "key");
});

// ── providerReferences / providerRefsHint（#3 删除保护）──
test("引用统计：规则条数、默认路由与角色名逐类返回", () => {
  const sp = {
    rules: [
      { match_prefix: "a", route_to: "GLM/m1" },
      { match_prefix: "b", route_to: "GLM/m2" },
      { match_prefix: "c", route_to: "KIMI/m3" },
    ],
    router: { default: "GLM/m1" },
  };
  const roles = { sonnet: { model: "GLM/x" }, subagent: { model: "KIMI/y" } };
  assert.deepEqual(L.providerReferences("GLM", sp, roles), { rules: 2, default: true, roles: ["sonnet"] });
  assert.deepEqual(L.providerReferences("KIMI", sp, roles), { rules: 1, default: false, roles: ["subagent"] });
});

test("引用统计：前缀匹配不误伤同前缀供应商（GLMX 不算 GLM）", () => {
  const sp = { rules: [{ route_to: "GLMX/m" }], router: { default: "GLMX/m" } };
  assert.deepEqual(L.providerReferences("GLM", sp, {}), { rules: 0, default: false, roles: [] });
});

test("引用统计：空 route_to 与空角色不产生引用", () => {
  const sp = { rules: [{ match_prefix: "x", route_to: "" }], router: {} };
  assert.deepEqual(L.providerReferences("GLM", sp, { sonnet: { model: "" } }), { rules: 0, default: false, roles: [] });
});

test("引用统计：容忍缺失的 sp 与 roles 容器", () => {
  assert.deepEqual(L.providerReferences("GLM", null, null), { rules: 0, default: false, roles: [] });
});

test("引用提示：拼出可操作的中文提示，无引用时为空", () => {
  assert.equal(
    L.providerRefsHint("GLM_MAX", { rules: 2, default: true, roles: [] }),
    "GLM_MAX 被 2 条模型规则和默认路由引用，先修改映射",
  );
  assert.equal(
    L.providerRefsHint("GLM_MAX", { rules: 0, default: false, roles: ["sonnet", "subagent"] }),
    "GLM_MAX 被 Claude Code 角色 sonnet、subagent引用，先修改映射",
  );
  assert.equal(
    L.providerRefsHint("GLM_MAX", { rules: 1, default: true, roles: ["subagent"] }),
    "GLM_MAX 被 1 条模型规则、默认路由和Claude Code 角色 subagent引用，先修改映射",
  );
  assert.equal(L.providerRefsHint("GLM_MAX", { rules: 0, default: false, roles: [] }), "");
});

// ── validateConfig：环境变量凭证与悬空引用（#3）──────
test("校验：环境变量凭证未选认证头时报错（对齐服务端 ProviderConfig 校验）", () => {
  const S = L.normalizeState({ sp: { providers: {
    DS: { base_url: "https://api.deepseek.com", api_key: null, api_key_env: "DS_KEY", auth_header: null },
  } } });
  const errs = L.validateConfig(S);
  assert.ok(errs.some((e) => e.includes("DS") && e.includes("认证头")));
});

test("校验：规则与默认路由指向不存在的供应商时报告悬空引用", () => {
  const S = L.normalizeState({
    sp: {
      providers: { GLM: { base_url: "https://x" } },
      rules: [
        { match_prefix: "claude-", route_to: "GONE/m" },
        { match_prefix: "ok-", route_to: "GLM/m" },
      ],
      router: { default: "MISSING/m" },
    },
  });
  const errs = L.validateConfig(S);
  assert.ok(errs.some((e) => e.includes("规则 1 引用了不存在的供应商 GONE")));
  assert.ok(errs.some((e) => e.includes("默认路由引用了不存在的供应商 MISSING")));
  assert.ok(!errs.some((e) => e.includes("GLM")));
});

test("校验：规则与默认路由都指向现存供应商时无悬空报错", () => {
  const S = L.normalizeState({
    sp: {
      providers: { GLM: { base_url: "https://x" } },
      rules: [{ match_prefix: "a", route_to: "GLM/m" }],
      router: { default: "GLM/m" },
    },
  });
  assert.deepEqual(L.validateConfig(S), []);
});

// ── dailyBarData（每日趋势柱状图）─────────────────────
test("dailyBarData sorts days chronologically without mutating input", () => {
  const daily = [
    { date: "2026-08-19", input_tokens: 100, cache_read_tokens: 300, cache_creation_tokens: 100 },
    { date: "2026-08-17", input_tokens: 50 },
    { date: "2026-08-18" },
  ];
  const bars = L.dailyBarData(daily, "input");
  assert.deepEqual(bars.map((b) => b.date), ["2026-08-17", "2026-08-18", "2026-08-19"]);
  assert.deepEqual(daily.map((d) => d.date), ["2026-08-19", "2026-08-17", "2026-08-18"]);
});

test("dailyBarData input metric totals input + cache read + cache write", () => {
  const bars = L.dailyBarData([
    { date: "2026-08-01", input_tokens: 1000, cache_read_tokens: 2000, cache_creation_tokens: 700, output_tokens: 999 },
  ], "input");
  assert.equal(bars[0].ratio, 1);  // 3700 is the window max
  assert.equal(bars[0].title, "2026-08-01 · 3.7K");
});

test("dailyBarData normalizes against the window maximum", () => {
  const daily = [
    { date: "2026-08-17", input_tokens: 50 },
    { date: "2026-08-18" },
    { date: "2026-08-19", input_tokens: 100, cache_read_tokens: 300, cache_creation_tokens: 100 },
  ];
  assert.deepEqual(L.dailyBarData(daily, "input").map((b) => b.ratio), [0.1, 0, 1]);
});

test("dailyBarData supports output and calls metrics", () => {
  const daily = [
    { date: "2026-08-01", output_tokens: 30, calls: 3 },
    { date: "2026-08-02", output_tokens: 90, calls: 9 },
  ];
  assert.deepEqual(L.dailyBarData(daily, "output").map((b) => b.ratio), [1 / 3, 1]);
  assert.deepEqual(L.dailyBarData(daily, "calls").map((b) => b.ratio), [1 / 3, 1]);
});

test("dailyBarData falls back to the input metric for unknown metric", () => {
  const daily = [
    { date: "2026-08-01", input_tokens: 10, output_tokens: 99 },
    { date: "2026-08-02", input_tokens: 20, output_tokens: 99 },
  ];
  assert.deepEqual(L.dailyBarData(daily, "bogus").map((b) => b.ratio), [0.5, 1]);
});

test("dailyBarData all-zero days normalize to 0 without NaN", () => {
  const bars = L.dailyBarData([{ date: "2026-08-01" }, { date: "2026-08-02" }], "input");
  assert.deepEqual(bars.map((b) => b.ratio), [0, 0]);
  assert.deepEqual(bars.map((b) => b.title), ["2026-08-01 · 0", "2026-08-02 · 0"]);
});

test("dailyBarData tolerates empty and null input", () => {
  assert.deepEqual(L.dailyBarData([], "input"), []);
  assert.deepEqual(L.dailyBarData(null, "input"), []);
  assert.deepEqual(L.dailyBarData(undefined, "calls"), []);
});

// ── CC 同步预览（#3 验收 9）─────────────────────────────

test("ccChangeBadge maps server actions to Chinese badges", () => {
  assert.equal(L.ccChangeBadge("add"), "新增");
  assert.equal(L.ccChangeBadge("remove"), "移除");
  assert.equal(L.ccChangeBadge("replace"), "修改");
});

test("ccPreviewRows turns the preview payload into dialog rows", () => {
  const pv = {
    ok: true, already: false,
    changes: [
      { key: "ANTHROPIC_BASE_URL", action: "add", old: null, new: "http://127.0.0.1:9527" },
      { key: "ANTHROPIC_AUTH_TOKEN", action: "replace", old: "（已设置，不回显）", new: "mage-router" },
      { key: "CLAUDE_CODE_SUBAGENT_MODEL", action: "remove", old: "stale[1M]", new: null },
    ],
  };
  assert.deepEqual(L.ccPreviewRows(pv), [
    { key: "ANTHROPIC_BASE_URL", badge: "新增", old: "—", new: "http://127.0.0.1:9527" },
    { key: "ANTHROPIC_AUTH_TOKEN", badge: "修改", old: "（已设置，不回显）", new: "mage-router" },
    { key: "CLAUDE_CODE_SUBAGENT_MODEL", badge: "移除", old: "stale[1M]", new: "—" },
  ]);
});

test("ccPreviewRows degrades safely on bad payloads", () => {
  assert.deepEqual(L.ccPreviewRows(null), []);
  assert.deepEqual(L.ccPreviewRows({}), []);
  assert.deepEqual(L.ccPreviewRows({ ok: false, msg: "boom" }), []);
  assert.deepEqual(L.ccPreviewRows({ ok: true, changes: null }), []);
});

// ── ccRuleRows / ccUnlistedNote（规则=持久真相：规则 diff 与软警告）──
test("ccRuleRows turns rule_changes into dialog rows", () => {
  const pv = { ok: true, rule_changes: [
    { match_prefix: "claude-opus", action: "replace", old: "GLM_MAX/glm-5.2", new: "GLM_MAX/glm-5.3" },
    { match_prefix: "claude-sonnet", action: "add", old: null, new: "KIMI/k3" },
  ] };
  assert.deepEqual(L.ccRuleRows(pv), [
    { prefix: "claude-opus", badge: "修改", old: "GLM_MAX/glm-5.2", new: "GLM_MAX/glm-5.3" },
    { prefix: "claude-sonnet", badge: "新增", old: "—", new: "KIMI/k3" },
  ]);
});

test("ccRuleRows degrades safely and empty means no rule section", () => {
  assert.deepEqual(L.ccRuleRows(null), []);
  assert.deepEqual(L.ccRuleRows({}), []);
  assert.deepEqual(L.ccRuleRows({ ok: true, rule_changes: [] }), []);
});

test("ccUnlistedNote formats warnings and stays empty otherwise", () => {
  const pv = { ok: true, unlisted: [
    { role: "opus", target: "GLM_MAX/glm-5.2" },
    { role: "haiku", target: "GLM_MAX/glm-5.2" },
  ] };
  assert.equal(L.ccUnlistedNote(pv),
    "⚠ opus→GLM_MAX/glm-5.2、haiku→GLM_MAX/glm-5.2 不在对应供应商的模型清单——请确认上游仍提供该模型");
  assert.equal(L.ccUnlistedNote({ ok: true, unlisted: [] }), "");
  assert.equal(L.ccUnlistedNote(null), "");
});

test("ccBackupNote renders both backup branches", () => {
  const will = { ok: true, backup: { will: true, path: "/x/settings.json.bak", note: "首次接入网关：写入前当前文件先备份为 .bak（之后的重复同步不再覆盖该备份）" } };
  assert.equal(
    L.ccBackupNote(will),
    "写入前备份 → /x/settings.json.bak（首次接入网关：写入前当前文件先备份为 .bak（之后的重复同步不再覆盖该备份））",
  );
  const wont = { ok: true, backup: { will: false, path: null, note: "目标文件不存在，将新建（无需备份）" } };
  assert.equal(L.ccBackupNote(wont), "目标文件不存在，将新建（无需备份）");
  assert.equal(L.ccBackupNote(null), "");
  assert.equal(L.ccBackupNote({ ok: false }), "");
});

test("validateConfig blocks save when sp has _load_error (#8)", () => {
  const errs = L.validateConfig(
    L.normalizeState({ sp: { _load_error: "重复 id" } }));
  assert.ok(errs.some((e) => e.includes("已阻止保存")));
});

// ── 保存流（saveFlow）：两阶段保存状态机（架构候选 1）──────────────
// 事故回归：99999 端口写库、幽灵 api_key 保存都发生在这条流上。

function flowDeps(over = {}) {
  const calls = { toasts: [], saving: [], stamps: [], setups: [], previews: [], puts: [] };
  const deps = {
    api: "/api/state",
    token: "T0KEN",
    fetch: over.fetch ?? (async () => { throw new Error("unexpected fetch"); }),
    confirmSync: over.confirmSync ?? (async (pv) => { calls.confirmed = pv; return true; }),
    toast: (m, e) => calls.toasts.push({ m, e: !!e }),
    gotoFirstError: (err) => { calls.goto = err; },
    viewTitle: (v) => ({ servers: "服务器", proxy: "网络设置", providers: "供应商", rules: "Claude Code 同步" }[v] || v),
    commitConfig: (st) => { calls.commitConfig = st; },
    commitRoles: (rl) => { calls.commitRoles = rl; },
    stampSaved: (at) => { calls.stamps.push(at); },
    setSaving: (on, label) => { calls.saving.push([on, label]); },
    now: () => "12:00:00",
  };
  return { deps, calls };
}
function fetchStub(routes) {
  return async (url, opts) => {
    const key = `${url}:${opts && opts.method}`;
    const hit = routes[key];
    if (!hit) throw new Error("unexpected fetch " + key);
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    const payload = typeof hit === "function" ? hit(body) : hit;
    return { json: async () => payload };
  };
}
function flowSnap(mutate) {
  const base = L.normalizeState({ mp: { servers: [
    { name: "t1", ssh: { host: "h", port: 22, auth_type: "key" },
      services: { ssh: { forwards: [], autostart: false } } }] } });
  const S = L.cloneData(base);
  if (mutate) mutate(S, base);
  return { S, baselineState: base, ccRoles: {}, baselineRoles: {} };
}

test("saveFlow rebuilds forwards by id even when the proxy role changes", async () => {
  const snap = flowSnap((S, base) => {
    for (const st of [S, base]) {
      st.mp.servers[0].id = "t-1";
      st.mp.servers.push({ id: "t-other", name: "t2", ssh: { host: "h2" },
        services: { ssh: { forwards: [] } } });
      st.mp.servers[0].forward_running = true;
      st.mp.servers[0].services.ssh.forwards = [
        { local_port: 10001, remote_host: "127.0.0.1", remote_port: 80 },
      ];
      st.mp.proxy_server_id = "t-1";
    }
    S.mp.servers[0].services.ssh.forwards[0].remote_port = 81;
  });
  const { deps } = flowDeps({ fetch: fetchStub({ "/api/state:PUT": { ok: true } }) });
  const targets = [];
  deps.reconnectForwards = id => targets.push(id);
  await L.saveFlow(snap, deps, false);
  assert.deepEqual(targets, ["t-1"]);
  targets.length = 0;
  snap.S.mp.proxy_server_id = "t-other";
  await L.saveFlow(snap, deps, false);
  assert.deepEqual(targets, ["t-1"], "proxy role does not own -L sessions");
});

test("saveFlow uses the same role snapshot for preview, setup and baseline", async () => {
  const snap = flowSnap();
  snap.ccRoles = { opus: { model: "provider/old" } };
  const expected = structuredClone(snap.ccRoles);
  let release, setup;
  const confirmation = new Promise(resolve => { release = resolve; });
  const { deps, calls } = flowDeps({
    fetch: fetchStub({
      "/api/state:PUT": { ok: true },
      "/api/cc-sync-preview:POST": { ok: true, already: false },
      "/api/setup-claude-code:POST": body => { setup = body.roles; return { ok: true }; },
    }),
    confirmSync: async () => {
      snap.ccRoles.opus.model = "provider/new";
      release(true);
      return confirmation;
    },
  });
  await L.saveFlow(snap, deps, true);
  assert.deepEqual(setup, expected);
  assert.deepEqual(calls.commitRoles, expected);
  assert.notDeepEqual(calls.commitRoles, snap.ccRoles);
});

test("saveFlow: clean snapshot with no force is a no-op", async () => {
  const { deps, calls } = flowDeps();
  const out = await L.saveFlow(flowSnap(), deps, false);
  assert.equal(out.configSaved, false);
  assert.deepEqual(calls.toasts.map(t => t.m), ["没有需要保存的更改"]);
  assert.equal(calls.saving.length, 0, "early return must not flip saving state");
});

test("saveFlow 事故回归A: 99999 端口在校验处被拦截，绝不发 PUT", async () => {
  const snap = flowSnap((S) => { S.mp.http_listen_port = 99999; });
  let fetched = 0;
  const { deps, calls } = flowDeps({ fetch: async () => { fetched++; throw new Error("must not fetch"); } });
  const out = await L.saveFlow(snap, deps, false);
  assert.equal(fetched, 0, "invalid port must never reach the wire");
  assert.ok(calls.toasts[0].e);
  assert.match(calls.toasts[0].m, /HTTP 监听端口无效/);
  assert.equal(calls.goto, "HTTP 监听端口无效");
  assert.equal(out.configSaved, false);
  assert.equal(calls.commitConfig, undefined, "no baseline advance");
});

test("saveFlow 事故回归B: 清空密钥后的快照与 baseline 同形 ⇒ 零网络、零写入", async () => {
  // 幽灵 api_key 事故的模型层断言：collect 后 api_key=null 与掩码 baseline
  // 不可构成 dirty——保存流因此一个请求都不发。
  const snap = flowSnap();
  snap.baselineState.sp.providers = { p1: { name: "p1", api_key: null, api_key_set: true } };
  snap.S.sp.providers = { p1: { name: "p1", api_key: null, api_key_set: true } };
  let fetched = 0;
  const { deps, calls } = flowDeps({ fetch: async () => { fetched++; throw new Error("must not fetch"); } });
  await L.saveFlow(snap, deps, false);
  assert.equal(fetched, 0);
  assert.match(calls.toasts[0].m, /没有需要保存的更改/);
});

test("saveFlow: config-only happy path commits clone and composes toast", async () => {
  const snap = flowSnap((S) => { S.mp.socks5_port = 1081; });
  const { deps, calls } = flowDeps({ fetch: fetchStub({ "/api/state:PUT": { ok: true } }) });
  const out = await L.saveFlow(snap, deps, false);
  assert.equal(out.configSaved, true);
  assert.deepEqual(calls.commitConfig, snap.S, "PUT ok advances baseline to a clone of S");
  assert.equal(calls.commitRoles, undefined, "no sync when roles unchanged");
  assert.deepEqual(calls.saving, [[true, "保存中…"], [false, undefined]]);
  assert.equal(calls.stamps.length, 1);
  assert.match(calls.toasts.at(-1).m, /已保存 1 项（网络设置）/);
});

test("saveFlow: cross-page save names both pages and both effects", async () => {
  const snap = flowSnap((S) => { S.mp.socks5_port = 1081; S.sp.providers = { p1: { name: "p1", enabled: true } }; });
  const { deps, calls } = flowDeps({ fetch: fetchStub({ "/api/state:PUT": { ok: true } }) });
  await L.saveFlow(snap, deps, false);
  const m = calls.toasts.at(-1).m;
  assert.match(m, /已保存 2 项（网络设置、供应商）/);
  assert.match(m, /代理需重新连接/);
  assert.match(m, /AI 路由已自动重载/);
});

test("saveFlow: PUT failure toasts server errors and never commits", async () => {
  const snap = flowSnap((S) => { S.mp.socks5_port = 1081; });
  const { deps, calls } = flowDeps({ fetch: fetchStub({ "/api/state:PUT": { ok: false, errors: ["端口冲突"] } }) });
  const out = await L.saveFlow(snap, deps, false);
  assert.equal(out.configSaved, false);
  assert.ok(calls.toasts[0].e);
  assert.match(calls.toasts[0].m, /端口冲突/);
  assert.equal(calls.commitConfig, undefined);
  assert.deepEqual(calls.saving.at(-1), [false, undefined], "finally still releases saving");
});

test("saveFlow: preview failure is fail-closed — setup never fires", async () => {
  const snap = flowSnap();
  let setups = 0;
  const { deps, calls } = flowDeps({
    fetch: fetchStub({
      "/api/cc-sync-preview:POST": { ok: false, msg: "boom" },
      "/api/setup-claude-code:POST": () => { setups++; return { ok: true }; },
    }),
  });
  const out = await L.saveFlow(snap, deps, true);
  assert.equal(setups, 0, "no diff ⇒ no write");
  assert.equal(out.cancelled, true);
  assert.ok(calls.toasts.some(t => /同步预览失败：boom/.test(t.m)));
});

test("saveFlow: user cancel after config save keeps roles dirty", async () => {
  const snap = flowSnap((S) => { S.mp.socks5_port = 1081; });
  snap.ccRoles = { opus: { model: "GLM_MAX/glm-5.2", ctx_1m: true } };
  const { deps, calls } = flowDeps({
    fetch: fetchStub({ "/api/state:PUT": { ok: true }, "/api/cc-sync-preview:POST": { ok: true, already: false, changes: [] } }),
    confirmSync: async () => false,
  });
  const out = await L.saveFlow(snap, deps, true);
  assert.equal(out.cancelled, true);
  assert.equal(out.configSaved, true);
  assert.ok(calls.commitConfig, "config baseline advanced before the modal");
  assert.equal(calls.commitRoles, undefined, "roles baseline NOT advanced — stays dirty");
  assert.match(calls.toasts.at(-1).m, /网关配置已保存；已取消写入/);
  assert.equal(calls.stamps.length, 1);
});

test("saveFlow: confirmed sync writes once and stamps roles baseline", async () => {
  const snap = flowSnap();
  snap.ccRoles = { opus: { model: "GLM_MAX/glm-5.2", ctx_1m: true } };
  let setupBody = null;
  const { deps, calls } = flowDeps({
    fetch: fetchStub({
      "/api/state:PUT": { ok: true },
      "/api/cc-sync-preview:POST": { ok: true, already: false, changes: [{ key: "X", action: "add", old: null, new: "y" }] },
      "/api/setup-claude-code:POST": (b) => { setupBody = b; return { ok: true, msg: "已配置 → 网关" }; },
    }),
  });
  const out = await L.saveFlow(snap, deps, false);
  assert.equal(out.syncOk, true);
  assert.equal(out.configSaved, true, "roles diff marks the rules view dirty — config PUT rides along (parity with the original flow)");
  assert.deepEqual(setupBody.roles, snap.ccRoles);
  assert.deepEqual(calls.commitRoles, snap.ccRoles);
  assert.ok(calls.confirmed, "preview shown to the user before write");
  assert.match(calls.toasts.at(-1).m, /已配置 → 网关/);
});

test("saveFlow: already-configured preview skips the modal (idempotent path)", async () => {
  const snap = flowSnap();
  snap.ccRoles = { opus: { model: "GLM_MAX/glm-5.2", ctx_1m: true } };
  const { deps, calls } = flowDeps({
    fetch: fetchStub({
      "/api/state:PUT": { ok: true },
      "/api/cc-sync-preview:POST": { ok: true, already: true, changes: [] },
      "/api/setup-claude-code:POST": { ok: true, action: "already", msg: "已指向本网关" },
    }),
  });
  const out = await L.saveFlow(snap, deps, true);
  assert.equal(out.syncOk, true);
  assert.equal(calls.confirmed, undefined, "no modal for a no-op write");
  assert.match(calls.toasts.at(-1).m, /已指向本网关/);
});

test("saveFlow: setup failure after config save reports both facts", async () => {
  const snap = flowSnap((S) => { S.mp.socks5_port = 1081; });
  snap.ccRoles = { opus: { model: "GLM_MAX/glm-5.2", ctx_1m: true } };
  const { deps, calls } = flowDeps({
    fetch: fetchStub({
      "/api/state:PUT": { ok: true },
      "/api/cc-sync-preview:POST": { ok: true, already: false, changes: [{}] },
      "/api/setup-claude-code:POST": { ok: false, msg: "disk full" },
    }),
  });
  const out = await L.saveFlow(snap, deps, true);
  assert.equal(out.configSaved, true);
  assert.equal(out.syncOk, false);
  assert.ok(calls.commitConfig);
  assert.equal(calls.commitRoles, undefined);
  assert.match(calls.toasts.at(-1).m, /网关配置已保存，但 同步失败：disk full/);
});

test("saveFlow: network exception surfaces as save-failed toast", async () => {
  const snap = flowSnap((S) => { S.mp.socks5_port = 1081; });
  const { deps, calls } = flowDeps({ fetch: async () => { throw new Error("offline"); } });
  const out = await L.saveFlow(snap, deps, false);
  assert.equal(out.configSaved, false);
  assert.match(calls.toasts.at(-1).m, /保存失败：offline/);
  assert.deepEqual(calls.saving.at(-1), [false, undefined]);
});

test("dirtyProjection: single-field change marks exactly that view", () => {
  const snap = flowSnap((S) => { S.mp.socks5_port = 1081; });
  const p = L.dirtyProjection(snap.baselineState, snap.S, {}, {});
  assert.deepEqual([...p.views], ["proxy"]);
  assert.equal(p.total, 1);
});


// ── parseTarget（#70 文法镜像）──
test("parseTarget mirrors parse_route_target dual-separator grammar", () => {
  assert.deepEqual(L.parseTarget("glm/glm-4.6"), ["glm", "glm-4.6"]);
  assert.deepEqual(L.parseTarget("glm,glm-4.6"), ["glm", "glm-4.6"],
                   "逗号兜底文法（schema 双分隔符）");
  assert.deepEqual(L.parseTarget("glm"), ["glm", ""]);
  assert.deepEqual(L.parseTarget(""), ["", ""]);
});

test("validateConfig accepts comma-form router default", () => {
  const S = {
    mp: { servers: [] },
    sp: {
      providers: { glm: { base_url: "https://open.bigmodel.cn/api/anthropic", api_key: "k" } },
      router: { default: "glm,glm-4.6" },
      rules: [],
      listen_port: 9527,
    },
  };
  assert.deepEqual(L.validateConfig(S), [],
                   "schema 合法的逗号 default 不得被 UI 误拦");
});

// ── port forwards (ssh -L) ────────────────────────────
test("viewSnapshot projects server forwards with collect-compatible defaults", () => {
  const loaded = L.normalizeState({ mp: { servers: [
    { ssh: { host: "h" }, services: { ssh: { forwards: [
      { local_port: 9000, remote_host: "127.0.0.1", remote_port: 8000 },
    ] } } }] } });
  assert.deepEqual(L.viewSnapshot("servers", loaded).servers[0].forwards, [
    { local_port: 9000, remote_host: "127.0.0.1", remote_port: 8000,
      enabled: true },
  ]);
  assert.equal(L.viewSnapshot("servers", loaded).servers[0].autostart, false,
    "services.ssh.autostart 入快照（用户可编辑）；is_proxy/forward_running 不入");
  // 缺省口径：空端口=0、空地址=127.0.0.1——collect 填回后不产生假 dirty
  const bare = L.viewSnapshot("servers",
    L.normalizeState({ mp: { servers: [
      { ssh: { host: "h" }, services: { ssh: { forwards: [{}] } } }] } }));
  assert.deepEqual(bare.servers[0].forwards, [
    { local_port: 0, remote_host: "127.0.0.1", remote_port: 0,
      enabled: true },
  ]);
  const noKey = L.viewSnapshot("servers",
    L.normalizeState({ mp: { servers: [{ ssh: { host: "h" } }] } }));
  const emptyList = L.viewSnapshot("servers",
    L.normalizeState({ mp: { servers: [
      { ssh: { host: "h" }, services: { ssh: { forwards: [] } } }] } }));
  assert.equal(L.countChanges(noKey, emptyList), 0,
    "missing forwards key and empty forwards are equally clean");
});

test("forwardsChanged detects edits, additions and removals per server id", () => {
  const mk = (fw) => ({ mp: { servers: [{ id: "t-1", services: {
    ssh: { forwards: fw } } }] } });
  const one = [{ local_port: 9000, remote_host: "127.0.0.1", remote_port: 8000 }];
  assert.equal(L.forwardsChanged(mk(one), mk(one)), false);
  const edited = [{ local_port: 9001, remote_host: "127.0.0.1", remote_port: 8000 }];
  assert.equal(L.forwardsChanged(mk(one), mk(edited)), true);
  const added = [...one, { local_port: 9001, remote_host: "db", remote_port: 5432 }];
  assert.equal(L.forwardsChanged(mk(one), mk(added)), true);
  assert.equal(L.forwardsChanged(mk(one), mk([])), true);
});

test("changedForwardTunnels returns per-server diff ids (multi-active)", () => {
  const base = { mp: { proxy_server_id: "t-1", servers: [
    { id: "t-1", name: "prod", services: { ssh: { forwards: [
      { local_port: 9000, remote_host: "127.0.0.1", remote_port: 8000 }] } } },
    { id: "t-2", services: { ssh: { forwards: [] } } }] } };
  const mk = (t1fw, t2fw) => ({ mp: { proxy_server_id: "t-1", servers: [
    { id: "t-1", name: "renamed", services: { ssh: { forwards: t1fw } } },
    { id: "t-2", services: { ssh: { forwards: t2fw } } }] } });
  const sameFw = [{ local_port: 9000, remote_host: "127.0.0.1", remote_port: 8000 }];
  // 改名/其他字段不触发；逐隧道精确定位有变者（路由守卫在 saveFlow 分流）
  assert.deepEqual(L.changedForwardTunnels(base, mk(sameFw, [])), [],
    "其他字段变化不触发");
  assert.deepEqual(L.changedForwardTunnels(base, mk(sameFw, [
    { local_port: 9001, remote_host: "db", remote_port: 5432 }])), ["t-2"],
    "非代理服务器的转发变更被精确定位（运行中的将由 saveFlow 定向守卫）");
  assert.deepEqual(L.changedForwardTunnels(base, mk([
    { local_port: 9000, remote_host: "10.0.0.5", remote_port: 8000 }], [])), ["t-1"],
    "代理服务器 forwards 变更同样入列");
  assert.deepEqual(L.changedForwardTunnels(base, mk(sameFw, [
    { local_port: 9001, remote_host: "db", remote_port: 5432 }])), ["t-2"]);
  // v0.8 兼容别名
  assert.equal(L.forwardsChanged(base, mk(sameFw, [])), false);
  assert.equal(L.forwardsChanged(base, mk(sameFw, [
    { local_port: 9001, remote_host: "db", remote_port: 5432 }])), true);
});

test("validateConfig rejects invalid forward rows", () => {
  const mk = fw => L.normalizeState({ mp: { servers: [
    { ssh: { host: "h", port: 22 }, name: "t1",
      services: { ssh: { forwards: [fw] } } }] } });
  assert.ok(L.validateConfig(mk({ local_port: 0, remote_host: "127.0.0.1", remote_port: 8000 }))
    .some(e => e.includes("本地端口")));
  assert.ok(L.validateConfig(mk({ local_port: 70000, remote_host: "127.0.0.1", remote_port: 8000 }))
    .some(e => e.includes("本地端口")));
  assert.ok(L.validateConfig(mk({ local_port: 9000, remote_host: "127.0.0.1", remote_port: 0 }))
    .some(e => e.includes("远程端口")));
  assert.ok(L.validateConfig(mk({ local_port: 9000, remote_host: "::1", remote_port: 80 }))
    .some(e => e.includes("远程地址")));
  assert.ok(L.validateConfig(mk({ local_port: 9000, remote_host: "a b", remote_port: 80 }))
    .some(e => e.includes("远程地址")));
});

test("validateConfig rejects duplicate local ports within AND across servers", () => {
  const dup = L.normalizeState({ mp: { servers: [
    { ssh: { host: "h", port: 22 }, name: "t1", services: { ssh: {
      forwards: [
        { local_port: 9000, remote_host: "a", remote_port: 1 },
        { local_port: 9000, remote_host: "b", remote_port: 2 }] } } }] } });
  assert.ok(L.validateConfig(dup).some(e => e.includes("重复")));
  // 多活（v0.9）：任意服务器可并行——跨服务器同本地端口会让两条 ssh 互顶
  const cross = L.normalizeState({ mp: { servers: [
    { ssh: { host: "h1", port: 22 }, name: "t1", services: { ssh: {
      forwards: [{ local_port: 9000, remote_host: "a", remote_port: 1 }] } } },
    { ssh: { host: "h2", port: 22 }, name: "t2", services: { ssh: {
      forwards: [{ local_port: 9000, remote_host: "b", remote_port: 2 }] } } }] } });
  assert.ok(L.validateConfig(cross).some(e => e.includes("端口冲突")),
    "跨服务器同端口必须拦（v0.8 单活豁免随多活作废）");
  const distinct = L.normalizeState({ mp: { servers: [
    { ssh: { host: "h1", port: 22 }, name: "t1", services: { ssh: {
      forwards: [{ local_port: 9000, remote_host: "a", remote_port: 1 }] } } },
    { ssh: { host: "h2", port: 22 }, name: "t2", services: { ssh: {
      forwards: [{ local_port: 9001, remote_host: "b", remote_port: 2 }] } } }] } });
  assert.deepEqual(L.validateConfig(distinct), []);
});

test("validateConfig rejects forward local port conflicting with reserved ports", () => {
  const S = L.normalizeState({ mp: { socks5_port: 1080, http_listen_port: 8888,
    servers: [{ ssh: { host: "h", port: 22 }, name: "t1", services: {
      ssh: { forwards: [
        { local_port: 8888, remote_host: "a", remote_port: 1 }] } } }] } });
  assert.ok(L.validateConfig(S).some(e => e.includes("端口冲突")));
});

test("validateConfig hardens forward row shape like prepare (direct-state edges)", () => {
  const mk = fw => L.normalizeState({ mp: { servers: [
    { ssh: { host: "h", port: 22 }, name: "t1",
      services: { ssh: { forwards: [fw] } } }] } });
  // UI 路径 collect 已 parseInt，仅直改 state 可达——但口径与 prepare 对齐
  assert.ok(L.validateConfig(mk({ local_port: 9000.5, remote_host: "a", remote_port: 80 }))
    .some(e => e.includes("本地端口")), "小数端口须拦（prepare 拒非 int）");
  assert.ok(L.validateConfig(mk({ local_port: 9000, remote_host: "a", remote_port: 80.5 }))
    .some(e => e.includes("远程端口")));
  assert.ok(Array.isArray(L.validateConfig(mk({ local_port: 9000, remote_host: 12345, remote_port: 80 }))),
    "数字形态 remote_host 不得抛 TypeError");
});

// ── 代理角色显式化：角色切换是服务器页的一个可计数变更（经 setProxyServer）──
test("viewSnapshot counts an explicit proxy-role switch as one servers-page change", () => {
  const mk = cid => L.normalizeState({ mp: {
    proxy_server_id: cid,
    servers: [
      { id: "t-a", ssh: { host: "a", port: 22 } },
      { id: "t-b", ssh: { host: "b", port: 22 } },
    ] } });
  const base = mk("t-a"), changed = mk("t-b");
  const p = L.dirtyProjection(base, changed, {}, {});
  assert.equal(p.total, 1);
  assert.deepEqual([...p.views], ["servers"]);
  assert.equal(
    L.countChanges(L.viewSnapshot("servers", base).servers,
                   L.viewSnapshot("servers", changed).servers),
    0,
    "角色切换只动 proxy_server_id，服务器本体零变更",
  );
});

test("proxyIndexOf: id 真相优先，悬空/缺省回退首条（与服务端 merge 同序）", () => {
  const mk = (cid, servers) => ({ mp: {
    proxy_server_id: cid, servers } });
  const ts = [{ id: "t-a" }, { id: "t-b" }, { id: "t-c" }];
  assert.equal(L.proxyIndexOf(mk("t-c", ts)), 2, "id 真相优先");
  assert.equal(L.proxyIndexOf(mk("t-gone", ts)), 0, "悬空 id 回退首条");
  assert.equal(L.proxyIndexOf(mk("", ts)), 0, "无 id 回退首条");
  assert.equal(L.proxyIndexOf({ mp: { servers: [] } }), 0, "空列表钳 0");
  // 删除前方服务器后 id 仍指向同一条——下标漂移类 bug 的形态学钉子
  assert.equal(L.proxyIndexOf(mk("t-c", ts.slice(1))), 1);
});

// ── NFS face inside the merged servers snapshot (ADR-007 → v0.13.0) ──
test("viewSnapshot('servers') treats default nfs node as no change", () => {
  // merge 给每条服务器填默认 nfs（enabled=false/无挂载/12049）——视图
  // 切换与保存不得因此产生假 dirty
  const loaded = L.normalizeState({ mp: { servers: [{ id: "t1", ssh: { host: "h" } }] } });
  const collected = L.normalizeState({
    mp: { servers: [{ id: "t1", ssh: { host: "h" },
      services: { nfs: { enabled: false, local_port: 12049, squash_to_ssh_user: false, mounts: [] } } }] },
  });
  assert.equal(
    L.countChanges(L.viewSnapshot("servers", loaded), L.viewSnapshot("servers", collected)),
    0,
  );
});

test("viewSnapshot('servers') counts mount edits as changes", () => {
  const base = L.normalizeState({
    mp: { servers: [{ id: "t1", services: { nfs: { enabled: false, local_port: 12049, mounts: [] } } }] },
  });
  const edited = L.normalizeState({
    mp: { servers: [{ id: "t1", services: { nfs: { enabled: true, local_port: 13000, mounts: [{ name: "data", remote_path: "/data", local_dir: "", auto_mount: true }] } } }] },
  });
  const diff = L.countChanges(L.viewSnapshot("servers", base), L.viewSnapshot("servers", edited));
  // 新增挂载行按设计算 1 项（countChanges 语义）+ enabled + 端口 = 3
  assert.equal(diff, 3);
});

test("nfsProjection normalizes missing/malformed rows defensively", () => {
  const p = L.nfsProjection(null);
  assert.deepEqual(p, { enabled: false, local_port: 12049, squash_to_ssh_user: false, mounts: [] });
  const q = L.nfsProjection({ local_port: "", mounts: [null, { name: "a" }] });
  assert.equal(q.local_port, 12049);
  assert.deepEqual(q.mounts[0], { name: "", remote_path: "", local_dir: "", auto_mount: false });
  assert.equal(q.mounts[1].name, "a");
});

test("viewSnapshot('servers') ignores runtime decorations", () => {
  const a = L.normalizeState({ mp: { servers: [{ id: "t1" }] } });
  const b = L.normalizeState({
    mp: { servers: [{ id: "t1", nfs_states: { data: "mounted" } }] },
  });
  assert.equal(
    L.countChanges(L.viewSnapshot("servers", a), L.viewSnapshot("servers", b)),
    0,
  );
});

// ── 挂载运行态可见性（fixable 链路 + 定向合并不变量）──────────
test("nfsStateOf normalizes object/string/absent shapes", () => {
  assert.deepEqual(L.nfsStateOf({}, "x"), {status:"unmounted",error:"",fixable:""});
  assert.deepEqual(L.nfsStateOf({x:"mounted"}, "x"), {status:"mounted",error:"",fixable:""});
  assert.deepEqual(
    L.nfsStateOf({x:{status:"error",error:"远程路径不存在或未导出",fixable:"exports"}}, "x"),
    {status:"error",error:"远程路径不存在或未导出",fixable:"exports"});
});

test("nfsStateValue normalizes a single decoration value", () => {
  assert.deepEqual(L.nfsStateValue(undefined), {status:"unmounted",error:"",fixable:""});
  assert.deepEqual(L.nfsStateValue("mounted"), {status:"mounted",error:"",fixable:""});
  assert.deepEqual(L.nfsStateValue({status:"error",error:"x",fixable:"exports"}),
    {status:"error",error:"x",fixable:"exports"});
});

test("_nfsMountedCount counts both decoration shapes", () => {
  // 生产形状是对象；字符串 = 旧夹具形状——计数必须两种都认
  const objs = {nfs_states:{a:{status:"mounted"},
    b:{status:"error",error:"x"},c:{status:"unmounted"},d:{status:"mounting"}}};
  assert.equal(L._nfsMountedCount(objs), 1);
  const strs = {nfs_states:{a:"mounted", b:"error"}};
  assert.equal(L._nfsMountedCount(strs), 1);
  assert.equal(L._nfsMountedCount({}), 0);
  assert.equal(L._nfsMountedCount({nfs_states:null}), 0);
});

test("mergeRuntimeDecorations updates decorations but never form fields", () => {
  const S = L.normalizeState({mp:{servers:[
    {id:"t-1", name:"本地编辑中的名字", ssh:{host:"h"},
     services:{nfs:{enabled:true,local_port:12049,mounts:[]}}}]}});
  const fetched = {mp:{capture_active:true, servers:[
    {id:"t-1", name:"远端名字", ssh:{host:"OTHER"}, is_proxy:true, forward_running:true,
     has_password:true, nfs_states:{data:{status:"error",error:"boom",fixable:"exports"}}}]}};
  L.mergeRuntimeDecorations(S, fetched);
  const t = S.mp.servers[0];
  // 表单字段（dirty 真相）绝不覆盖
  assert.equal(t.name, "本地编辑中的名字");
  assert.equal(t.ssh.host, "h");
  // 装饰字段定向更新
  assert.equal(t.is_proxy, true);
  assert.equal(t.forward_running, true);
  assert.equal(t.has_password, true);
  assert.deepEqual(t.nfs_states.data, {status:"error",error:"boom",fixable:"exports"});
  assert.equal(S.mp.capture_active, true);
});

test("mergeRuntimeDecorations skips unmatched/anonymous servers", () => {
  const S = L.normalizeState({mp:{servers:[
    {id:"t-1", name:"keep"}, {name:"no-id"}]}});
  const fetched = {mp:{servers:[{id:"t-2", name:"别的"}]}};
  L.mergeRuntimeDecorations(S, fetched);
  assert.equal(S.mp.servers[0].name, "keep");
  assert.equal(S.mp.servers[1].name, "no-id");
});

// ── 逐条启停（enabled）：投影/签名/校验镜像 ──────────────────────
test("viewSnapshot preserves explicit disabled forwards", () => {
  const loaded = L.normalizeState({ mp: { servers: [
    { ssh: { host: "h" }, services: { ssh: { forwards: [
      { local_port: 9000, remote_port: 80, enabled: false },
    ] } } }] } });
  assert.equal(L.viewSnapshot("servers", loaded).servers[0].forwards[0].enabled,
    false);
});

test("changedForwardTunnels signature includes enabled flips", () => {
  // 翻转 enabled 即触发保存后守卫重连——签名必须感知
  const mk = (fw) => ({ mp: { servers: [{ id: "t-1", ssh: { host: "h" },
    services: { ssh: { forwards: fw } } }] }, sp: {} });
  const base = mk([{ local_port: 9000, remote_host: "127.0.0.1",
    remote_port: 80, enabled: true }]);
  const flipped = mk([{ local_port: 9000, remote_host: "127.0.0.1",
    remote_port: 80, enabled: false }]);
  assert.deepEqual(L.changedForwardTunnels(base, flipped), ["t-1"]);
  assert.deepEqual(L.changedForwardTunnels(base, mk(
    [{ local_port: 9000, remote_host: "127.0.0.1", remote_port: 80 }])), [],
    "缺省 enabled 与 true 同签名（读兼容——不产生假重连）");
});

test("validateConfig excludes disabled forwards from port conflicts", () => {
  const mk = (fw) => L.normalizeState({ mp: {
    socks5_port: 1080, http_listen_port: 8888, capture_port: 8080,
    config_port: 9528,
    servers: [
      { ssh: { host: "a" }, services: { ssh: { forwards: fw } } },
      { ssh: { host: "b" }, services: { ssh: { forwards: [
        { local_port: 9000, remote_host: "127.0.0.1", remote_port: 81 }] } } },
    ] }, sp: { listen_port: 9527 } });
  // 同端口 9000：一行停用 → 不冲突（腾挪端口放行）
  const withDisabled = mk([{ local_port: 9000, remote_host: "127.0.0.1",
    remote_port: 80, enabled: false }]);
  assert.equal(L.validateConfig(withDisabled).some(e => e.includes("端口冲突")), false);
  // 双启用 → 冲突照报
  const bothOn = mk([{ local_port: 9000, remote_host: "127.0.0.1",
    remote_port: 80, enabled: true }]);
  assert.equal(L.validateConfig(bothOn).some(e => e.includes("端口冲突")), true);
});
