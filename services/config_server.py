"""Lightweight config server for the web-based settings UI.

Serves a single-page HTML config panel + JSON API for reading/writing both
Magic Stack (~/.magic-proxy.json) and Suanpan (~/.suanpan.yaml) configs.
Uses stdlib http.server — no FastAPI/uvicorn dependency. Runs on 127.0.0.1:9528
in a daemon thread, always available while the app is running.

Security: validates Host header (DNS-rebinding guard), masks API keys in GET
responses (restores on write), validates Suanpan config before persisting.
"""
import json
import logging
import secrets
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

from shared import keychain
from services import sp_config, server_check
from shared import i18n
from mpconf.config_state import ConfigStateStore
from tunnel import ssh_launch
from mount import remote_setup
from services import claude_code_setup
from capture import capture_store
from mpconf.config import (load_config, merge_config, decorate_runtime_state,
                           server_openvpn)
from vpn import privilege as vpn_privilege
from vpn import profile as vpn_profile
from vpn import profile_store as vpn_profile_store
from services.balance_usage import fetch_balance
from services.provider_probe import (
    fetch_models,
    probe_provider,
    test_provider,
)
from services.usage_stats import USAGE_RANGES, fetch_usage
from util import resource_path as _resource_path

logger = logging.getLogger("magic-proxy.config_server")

CONFIG_PORT = 9528
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB — reject oversized POST/PUT bodies
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

# GET / 无 token 时的登录页（自包含 HTML，无外部资源）。填 token → JS 用
# Bearer 调 / 种 HttpOnly cookie → 成功则 reload 进配置页。仅 GET / 返回此页；
# /api/* 的 401 保持纯 JSON（curl/脚本客户端不期待 HTML）。macOS 桥接首导航
# 带 Bearer → 200 不经过此页，行为不变。
def _inject_i18n_boot(html):
    """ADR-012 M2：serve 时注入双语 catalog（浏览器直开与原生窗同路径）。
    ensure_ascii + </ 转义防 </script> 提前闭合。"""
    boot = json.dumps(
        {"lang": i18n.language(),
         "messages": {"zh-CN": i18n.catalog("zh-CN"),
                      "en": i18n.catalog("en")}},
        ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    return html.replace(
        '<script data-layer="model">',
        "<script>window.__I18N__=" + boot + "</script>\n"
        '<script data-layer="model">', 1)


def _login_html():
    """登录页（按当前语言渲染——ADR-012 M2：serve 时取词）。"""
    lang = i18n.language()
    return (
        """<!doctype html>
<html lang="@LANG@"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Magic Stack — @TITLE@</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;background:#f5f5f7;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#fff;border-radius:12px;padding:32px;box-shadow:0 4px 24px rgba(0,0,0,.08);width:320px}
h1{font-size:18px;margin:0 0 8px}p{font-size:13px;color:#666;margin:0 0 20px}
input{width:100%;padding:10px;border:1px solid #d2d2d7;border-radius:8px;font-size:14px;box-sizing:border-box;margin-bottom:12px}
button{width:100%;padding:10px;background:#007aff;color:#fff;border:0;border-radius:8px;font-size:14px;cursor:pointer}
button:disabled{background:#ccc;cursor:default}.err{color:#ff3b30;font-size:13px;margin-top:8px;display:none}
</style></head><body>
<div class="card">
<h1>Magic Stack</h1>
<p>@HINT@</p>
<input id="tok" type="password" placeholder="token" autocomplete="off" autofocus>
<button id="go">@ENTER@</button>
<div class="err" id="err">@INVALID@</div>
</div>
<script>
const tok=document.getElementById('tok'),go=document.getElementById('go'),err=document.getElementById('err');
async function login(){
  const t=tok.value.trim();if(!t)return;
  go.disabled=true;err.style.display='none';
  try{
    const r=await fetch('/',{headers:{Authorization:'Bearer '+t}});
    if(r.ok){location.reload();return;}
  }catch(e){}
  err.style.display='block';go.disabled=false;tok.select();
}
go.onclick=login;
tok.onkeydown=e=>{if(e.key==='Enter')login();};
</script></body></html>"""
        .replace("@LANG@", lang)
        .replace("@TITLE@", i18n.t("ui.login.title"))
        .replace("@HINT@", i18n.t("ui.login.hint"))
        .replace("@ENTER@", i18n.t("ui.login.enter"))
        .replace("@INVALID@", i18n.t("ui.login.invalid"))
    )


def _read_mp():
    try:
        cfg = merge_config(load_config())
    except Exception:
        # 迁移可行动错误等：降级为带 _load_error 的空态供 UI 提示，
        # /api/state 不 500（UI 保存被 validateConfig/prepare 双带阻断）
        logger.exception("_read_mp degraded")
        return {"_load_error": "Magic Proxy 配置装载失败，已阻止保存以防覆盖"}
    if not cfg:
        return {}
    for t in cfg.get("servers", []):
        # has_password 属 READONLY_DECORATED_FIELDS（prepare 剥除侧单点声明）
        _ssh = t.get("ssh") if isinstance(t.get("ssh"), dict) else {}
        t["has_password"] = bool(
            _ssh.get("auth_type") == "password" and keychain.get_password(t))
    # 掩码契约（#66 复核）：local_client_token 明文永不出进程——UI 回
    # token_set 布尔；prepare 按旧档恢复真实值（sp 侧 api_key 同款模式）
    if "local_client_token" in cfg:
        cfg.pop("local_client_token", None)
        cfg["local_client_token_set"] = True
    return cfg


def test_tunnel(tunnel):
    """One-shot SSH reachability probe for one saved tunnel config.

    本函数只持有端点职责：输入守卫与 Keychain 取用经 server_check.
    probe_inputs 共享；SSH 调用策略与真实隧道完全同源（tunnel/
    ssh_launch.probe，含超时上限）——绿结果意味着隧道本身会连上，
    未信任主机快速失败，绝不自动信任。

    Returns {"ok": True} or {"ok": False, "error": "<中文短语>"} — never raises.
    """
    normalized, password, error = server_check.probe_inputs(tunnel, keychain)
    if error:
        return {"ok": False, "error": error}
    return ssh_launch.probe(normalized, password=password)


def test_forward(tunnel, forward):
    """One-shot port-forward probe: tunnel + 一条显式转发行（表单意图）。

    本函数只持有端点职责：输入守卫与 Keychain 取用经 server_check.
    probe_inputs 共享；SSH 调用策略与真实隧道完全同源（tunnel/
    ssh_launch.probe_forward，-W 直连远端端口）。测的是请求体里的
    tunnel + forward——未保存的表单值同样可测。

    Returns {"ok": True, "latency_ms": int} or {"ok": False, "error": str}.
    """
    normalized, password, error = server_check.probe_inputs(tunnel, keychain)
    if error:
        return {"ok": False, "error": error}
    return ssh_launch.probe_forward(
        normalized, forward.get("remote_host"), forward.get("remote_port"),
        password=password)


def _nfs_credentials(tunnel, sudo_password_override=None):
    """NFS 远程操作的凭据解析：(tunnel, ssh_password, sudo_password, error)。

    sudo 密码解析序（ADR-007）：显式覆盖（UI 输入）> 密码登录复用隧道
    密码 > Keychain sudo 槽（密钥登录存过一次的）。空串 = sudo -n
    （NOPASSWD 服务器），失败由 run_remote 分类成中文短语提示补输。
    """
    normalized, password, error = server_check.probe_inputs(tunnel, keychain)
    if error:
        return None, "", "", error
    if sudo_password_override:
        return normalized, password, sudo_password_override, ""
    if (normalized.get("ssh") or {}).get("auth_type") == "password":
        return normalized, password, password, ""
    return normalized, password, keychain.get_sudo_password(normalized), ""


def nfs_check_remote(tunnel):
    """探测远程 NFS 状态（发行版/已装/监听/导出表）——只读，无副作用。"""
    normalized, password, sudo_password, error = _nfs_credentials(tunnel)
    if error:
        return {"ok": False, "error": error}
    return remote_setup.check_remote(normalized, password=password)


def nfs_setup_remote(tunnel, mounts, squash_to_ssh_user=False,
                     sudo_password_override=""):
    """一键安装 + 配置导出（幂等）。显式输入的 sudo 密码在成功后落
    Keychain（密钥登录的隧道下次免输）。"""
    normalized, password, sudo_password, error = _nfs_credentials(
        tunnel, sudo_password_override)
    if error:
        return {"ok": False, "error": error, "stage": "detect"}
    result = remote_setup.setup_remote(
        normalized, mounts, password=password, sudo_password=sudo_password,
        squash_to_ssh_user=squash_to_ssh_user)
    if (result.get("ok") and sudo_password_override
            and (normalized.get("ssh") or {}).get("auth_type") != "password"):
        keychain.set_sudo_password(normalized, sudo_password_override)
    return result


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Carries the per-server callback refs on the INSTANCE (not class
    attributes): parallel ConfigServers in tests can never cross-talk, and
    tests construct one directly without replicating start() internals."""
    daemon_threads = True

    def __init__(self, address, handler, *, expected_token=None,
                 on_sp_saved=None, on_mp_saved=None, runtime_state_fn=None,
                 instructions_fn=None):
        self.expected_token = expected_token
        self.on_sp_saved = on_sp_saved
        self.on_mp_saved = on_mp_saved
        # RuntimeProjection 单一 seam（架构评审 R3）：三回调穿参塌缩为一
        self.runtime_state_fn = runtime_state_fn
        # agent_instructions() 单一归宿的取用口（浏览器回退路由用）
        self.instructions_fn = instructions_fn
        super().__init__(address, handler)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _valid_token(self):
        """Validate bearer token from Authorization header or session cookie.

        issue #10：URL 永不带凭证——query-string 认证路径已删除。首屏
        导航由桥接构造带 Authorization 头的请求；其响应种下 HttpOnly
        的 cfgsess 会话 cookie，此后 JS 同源 fetch 自动携带（token
        不进 URL/JS/日志）。常量时间比较保留。
        """
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token:
            cookie_header = self.headers.get("Cookie", "")
            for part in cookie_header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == "cfgsess":
                    token = value
                    break
        expected = self.server.expected_token
        if not token or not expected:
            return False
        return secrets.compare_digest(token, expected)

    def _valid_host(self):
        """Reject non-loopback Host headers (DNS-rebinding guard)."""
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
        return host in _ALLOWED_HOSTS

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              extra_headers=()):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json_body(self):
        """Read and parse JSON body with a size cap (MAX_BODY_BYTES).

        Returns parsed data on success, or None if an error response was
        already sent (400 invalid JSON / 413 too large).
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._json(400, {"error": "invalid Content-Length"})
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            self._json(413, {"error": "body too large"})
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._json(400, {"error": "invalid JSON"})
            return None
        # 形状守卫（#69 S11d）：端点一律 .get() 消费 dict——非 dict body
        # （数组/标量/字符串）统一 400，不裸抛进处理器线程
        if not isinstance(data, dict):
            self._json(400, {"error": "JSON body must be an object"})
            return None
        return data

    def do_GET(self):
        if not self._valid_host():
            self._json(403, {"error": "forbidden"})
            return
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        # Browsers auto-request /favicon.ico without the token; answer 204
        # instead of 403 so the console stays clean. WKWebView never asks.
        if path == "/favicon.ico":
            self._send(204, b"")
            return
        # agent.md is public (loopback-only, no token) so AI agents can read
        # product context without the user's bearer token.
        if path == "/agent.md":
            self._serve_agent_md()
            return
        if not self._valid_token():
            # GET / 的 401 返回登录页（浏览器直接打开可用）；API 路径仍 JSON
            if path in ("/", "/index.html"):
                self._send(401, _login_html(), "text/html; charset=utf-8")
            else:
                self._json(401, {"error": "unauthorized"})
            return
        if path in ("/", "/index.html"):
            self._serve_config_html()
            return
        handler = _API_GET.get(path)
        if handler is None:
            self._json(404, {"error": "not found"})
            return
        handler(self, parsed_url)

    def do_POST(self):
        if not self._valid_host() or not self._valid_token():
            self._json(401, {"error": "unauthorized"})
            return
        handler = _API_POST.get(urlparse(self.path).path)
        if handler is None:
            self._json(404, {"error": "not found"})
            return
        data = self._read_json_body()
        if data is None:
            return
        handler(self, data)

    def do_PUT(self):
        if not self._valid_host() or not self._valid_token():
            self._json(401, {"error": "unauthorized"})
            return
        handler = _API_PUT.get(urlparse(self.path).path)
        if handler is None:
            self._json(404, {"error": "not found"})
            return
        data = self._read_json_body()
        if data is None:
            return
        handler(self, data)

    # ── 静态页（GET 非路由表面）────────────────────────────

    def _serve_agent_md(self):
        try:
            txt = open(_resource_path("agent.md"), encoding="utf-8").read()
            self._send(200, txt, "text/markdown; charset=utf-8")
        except OSError:
            self._json(404, {"error": "agent.md not found"})

    def _serve_config_html(self):
        try:
            html = open(_resource_path("config_ui.html"), encoding="utf-8").read()
            html = _inject_i18n_boot(html)
            extra = []
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                # 桥接构造的首导航（header 呈现）→ 种 HttpOnly 会话
                # cookie，刷新与后续 fetch 不再依赖 header
                extra.append((
                    "Set-Cookie",
                    f"cfgsess={self.server.expected_token}; Path=/; "
                    "HttpOnly; SameSite=Strict"))
            self._send(200, html, "text/html; charset=utf-8", extra_headers=extra)
        except OSError:
            self._json(404, {"error": "config_ui.html not found"})

    # ── GET 端点（路由表声明，签名 (self, parsed_url)）──────

    def _api_state(self, parsed_url):
        mp = _read_mp()
        sp = sp_config.sp_load_masked()
        # Read-only runtime status injected for the config UI;
        # READONLY_DECORATED_FIELDS（config_state 单点声明，运行态
        # 半边派生自 mpconf.config.RUNTIME_DECORATED_FIELDS）的剥除
        # 保证它永不回写文件。装饰形状单一归宿
        # mpconf.config.decorate_runtime_state（架构评审 C2）；
        # 运行态经 RuntimeProjection 单一 seam 读取（缺席/异常 →
        # 空投影：capture_active=False、装饰全空）
        try:
            fn = self.server.runtime_state_fn
            proj = fn() if fn else None
        except Exception:
            logger.exception("runtime_state_fn failed")
            proj = None
        mp = decorate_runtime_state(mp, proj)
        self._json(200, {"mp": mp, "sp": sp})

    def _api_balance(self, parsed_url):
        self._json(200, fetch_balance(sp_config.sp_load_raw()))

    def _api_usage(self, parsed_url):
        usage_range = parse_qs(
            parsed_url.query, keep_blank_values=True
        ).get("range", ["all"])[0]
        if usage_range not in USAGE_RANGES:
            self._json(400, {"error": "invalid range"})
            return
        self._json(200, fetch_usage(sp_config.sp_load_raw(), usage_range))

    def _api_cc_default_roles(self, parsed_url):
        # ?seed=rules：UI「按路由规则重置」——跳过实值回读，强制规则推导种子
        seed = parse_qs(
            parsed_url.query, keep_blank_values=True
        ).get("seed", [""])[0]
        if seed not in ("", "rules"):
            self._json(400, {"error": "invalid seed"})
            return
        self._json(200, claude_code_setup.default_roles(
            force_rules=seed == "rules"))

    def _api_agents(self, parsed_url):
        # ADR-010 M4：Agent 检测 + 同步态（向导的 Agent 矩阵数据面）
        self._json(200, claude_code_setup.agents_status())

    def _api_provider_templates(self, parsed_url):
        # #51：UI 供应商模板单一真源 = PROVIDER_REGISTRY（Python 侧）
        # ADR-010：载荷附端点矩阵（快速接入向导消费）；顶层
        # base_url/anthropic_native 保持兼容投影（无 anthropic 卡的
        # 厂商为 None/False）
        from shared.provider_auth import PROVIDER_REGISTRY
        templates = [
            {"id": name, "label": entry["label"],
             "base_url": entry.get("base_url"),
             "anthropic_native": entry["anthropic_native"],
             "endpoints": {proto: dict(card)
                           for proto, card in entry["endpoints"].items()}}
            for name, entry in PROVIDER_REGISTRY.items()]
        templates.append({"id": "custom", "label": "自定义"})
        self._json(200, templates)

    def _api_agent_instructions(self, parsed_url):
        # 浏览器直开设置页的「复制 AI 助手指令」回退通道——WKWebView
        # 走 bridge 由 app 拼装上剪贴板，不经此路由。文案含 Bearer
        # token，必须过认证；文本经 instructions_fn 取自
        # agent_instructions() 单一归宿，永不另抄一份。
        fn = self.server.instructions_fn
        if fn is None:
            self._json(500, {"error": "instructions unavailable"})
            return
        self._json(200, {"text": fn()})

    # ── POST 端点（路由表声明，签名 (self, data)）──────────

    def _api_fetch_models(self, data):
        self._json(200, fetch_models(sp_config.sp_load_raw(), str(data.get("provider", ""))))

    def _api_test_provider(self, data):
        self._json(200, test_provider(
            sp_config.sp_load_raw(), str(data.get("provider", "")),
            data.get("model")))

    def _api_probe_provider(self, data):
        # ADR-010 三级端点探测（免费 GET 语义）：body = provider 形态
        # dict（base_url 必填，凭证可选——无 Key 只探存在性）
        base_url = data.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            self._json(400, {"error": "需要 base_url"})
            return
        self._json(200, probe_provider(data))

    def _api_cc_sync_preview(self, data):
        roles = data.get("roles")  # {key: {model, ctx_1m}} or None
        self._json(200, claude_code_setup.preview(roles=roles))

    def _api_setup_claude_code(self, data):
        roles = data.get("roles")  # {key: {model, ctx_1m}} or None
        result = claude_code_setup.setup(roles=roles)
        # 角色表会 upsert 网关 tier 路由规则（规则=持久真相方案）——
        # 写过规则即触发 SP 段回调（网关热重载），与 PUT /api/state
        # 同一收敛口径
        if result.get("rules_written") and \
                getattr(self.server, "on_sp_saved", None):
            try:
                self.server.on_sp_saved()
            except Exception:
                logger.exception("on_sp_saved after cc setup failed")
        self._json(200, result)

    def _api_agent_setup_preview(self, data):
        # ADR-010 M4：body {"agent": id, "options": {...}|null}
        opts = data.get("options")
        self._json(200, claude_code_setup.agent_preview(
            str(data.get("agent", "")),
            opts if isinstance(opts, dict) else None))

    def _api_setup_agent(self, data):
        opts = data.get("options")
        self._json(200, claude_code_setup.agent_setup(
            str(data.get("agent", "")),
            opts if isinstance(opts, dict) else None))

    def _api_test_tunnel(self, data):
        """POST /api/test-tunnel {index} → probe saved servers[index].

        400 for bad index / no servers, 200 with {"ok", "error"?} once the
        probe actually runs（隧道解析与 test-forward/NFS 端点共用
        _saved_tunnel_by_index）。"""
        tunnel, error = self._saved_tunnel_by_index(data.get("index"))
        if error:
            self._json(400, {"ok": False, "error": error})
            return
        self._json(200, test_tunnel(tunnel))

    def _api_test_forward(self, data):
        """POST /api/test-forward {tunnel, forward} → probe_forward once.

        tunnel/forward 都取表单当前值——未保存的新隧道、新行同样可测。
        兼容旧载荷 {index, forward}：按已保存隧道解析。"""
        forward = data.get("forward")
        if not isinstance(forward, dict):
            self._json(400, {"ok": False, "error": "无效的转发行"})
            return
        tunnel, error = self._resolve_tunnel(data)
        if error:
            self._json(400, {"ok": False, "error": error})
            return
        self._json(200, test_forward(tunnel, forward))

    def _api_server_check(self, data):
        """POST /api/server-check {tunnel|index, only?} → 服务卡一键检测。

        only = SERVICE_CARDS 键之一（单卡）或缺省（全卡）；隧道解析与
        test-forward / NFS 端点共用 _resolve_tunnel，结果按服务类型
        键控（{"results": {"ssh": …, "nfs": …, "openvpn": …}}），单卡
        失败不连坐其它卡（server_check.check_server 包裹）。"""
        tunnel, error = self._resolve_tunnel(data)
        if error:
            self._json(400, {"ok": False, "error": error})
            return
        only = data.get("only")
        if only is not None and (not isinstance(only, str)
                                  or only not in server_check.SERVICE_CARDS):
            self._json(400, {"ok": False, "error": "无效的检测类型"})
            return
        self._json(200, {"ok": True, "results": server_check.check_server(
            tunnel, keychain, only=only)})

    def _api_capture_clean(self, data):
        """POST /api/capture-clean → empty the capture dir (keep the dir)."""
        cfg = _read_mp()
        capture_dir = cfg.get("capture_dir") if isinstance(cfg, dict) else None
        try:
            removed = capture_store.clean(capture_dir)
        except OSError as exc:
            logger.warning("capture clean failed: %s", exc)
            self._json(200, {"ok": False, "error": str(exc)})
            return
        self._json(200, {"ok": True, "removed": removed})

    def _api_nfs_check_remote(self, data):
        """POST /api/nfs-check-remote {tunnel|index} → 只读探测远程 NFS。"""
        tunnel, error = self._resolve_tunnel(data)
        if error:
            self._json(400, {"ok": False, "error": error})
            return
        self._json(200, nfs_check_remote(tunnel))

    def _api_nfs_setup_remote(self, data):
        """POST /api/nfs-setup-remote {tunnel|index, mounts, squash,
        sudo_password?} → 一键安装 + 配置导出（幂等）。"""
        tunnel, error = self._resolve_tunnel(data)
        if error:
            self._json(400, {"ok": False, "error": error, "stage": "detect"})
            return
        mounts = data.get("mounts")
        # None 条目不豁免——[null] 曾穿透 all() 生成器短路（空序列恒
        # True），下游 shlex.quote(None) 会在 handler 线程抛 TypeError
        if not isinstance(mounts, list) or not mounts or \
                not all(isinstance(p, str) and p.startswith("/")
                        for p in mounts):
            self._json(400, {"ok": False,
                             "error": "mounts 须为非空的绝对路径列表",
                             "stage": "detect"})
            return
        sudo_pw = data.get("sudo_password")
        if sudo_pw is not None and not isinstance(sudo_pw, str):
            sudo_pw = ""
        self._json(200, nfs_setup_remote(
            tunnel, mounts,
            squash_to_ssh_user=data.get("squash") is True,
            sudo_password_override=sudo_pw or ""))

    # ── PUT 端点（路由表声明，签名 (self, data)）──────────

    def _api_put_state(self, data):
        mp_in = data.get("mp")
        sp_in = data.get("sp")
        if not isinstance(mp_in, dict):
            mp_in = None
        if not isinstance(sp_in, dict):
            sp_in = None
        if mp_in is None and sp_in is None:
            self._json(200, {"ok": True})
            return
        store = ConfigStateStore(keychain=keychain)
        plan = store.prepare(mp=mp_in, sp=sp_in)
        if not plan.ok:
            self._json(422, {"ok": False, "errors": plan.errors})
            return
        # 提交完整成功后按「本事务涉及的段」触发回调：MP 段 → 刷新应用
        # 内存副本（app 侧 converge launch_at_login 等），SP 段 → 网关
        # reload。commit 的 on_committed 是单钩子，这里按段组合。
        committed_callbacks = []
        if mp_in is not None and getattr(self.server, "on_mp_saved", None):
            committed_callbacks.append(self.server.on_mp_saved)
        if sp_in is not None and getattr(self.server, "on_sp_saved", None):
            committed_callbacks.append(self.server.on_sp_saved)

        def _fire_committed():
            for cb in committed_callbacks:
                cb()

        on_committed = _fire_committed if committed_callbacks else None
        result = store.commit(plan, on_committed=on_committed)
        if not result.ok:
            self._json(422, {"ok": False, "errors": result.errors})
        else:
            self._json(200, {"ok": True})

    # ── VPN 端点（spec §7.2：设置窗 OpenVPN 面）───────────────────
    # profile 导入/凭证/安装是纯服务端动作（净化 + 落盘 + Keychain +
    # 特权引导）；connect/disconnect 归 app（VpnClient 持有者）——经
    # ConfigServer 上注入的回调 seam（vpn_connect_fn / vpn_disconnect_fn，
    # 与 runtime_state_fn 同款注入模式）。

    def _api_vpn_profile(self, data):
        """导入 .ovpn：净化 → 0600 落盘 → 剥除清单/凭证需求回执。
        profile_set 布尔不在此翻转——UI 按导入成功置位、随保存流持久化
        （两阶段保存契约，spec §7.2）。"""
        tunnel, error = self._saved_tunnel_by_index(data.get("index"))
        if error:
            self._json(400, {"error": error})
            return
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            self._json(400, {"error": i18n.t("vpn.err.empty_profile")})
            return
        clean, info = vpn_profile.sanitize_profile(content)
        if info.error:
            self._json(400, {"error": i18n.t("vpn.err.no_remote")})
            return
        if not vpn_profile_store.save_profile(tunnel.get("id") or "", clean):
            self._json(500, {"error": i18n.t("vpn.err.save_failed")})
            return
        self._json(200, {
            "ok": True,
            "removed": [d for d, _line in info.removed],
            "removed_lines": [line for _d, line in info.removed],
            "needs_credentials": info.needs_credentials,
            "missing_client_role": info.missing_client_role,
        })

    def _api_vpn_credentials(self, data):
        """VPN 密码 → Keychain 槽（用户名是 config 字段走正常保存流）。"""
        tunnel, error = self._saved_tunnel_by_index(data.get("index"))
        if error:
            self._json(400, {"error": error})
            return
        password = data.get("password")
        if not isinstance(password, str) or not password:
            self._json(400, {"error": i18n.t("vpn.err.empty_password")})
            return
        self._json(200, {"ok": keychain.set_vpn_password(tunnel, password)})

    def _api_vpn_install(self, data):
        """安装到系统：runtime conf + sudoers + DNS 脚本一次管理员授权
        （幂等）。pull_dns 读已保存配置——先保存再安装。"""
        tunnel, error = self._saved_tunnel_by_index(data.get("index"))
        if error:
            self._json(400, {"error": error})
            return
        text = vpn_profile_store.load_profile(tunnel.get("id") or "")
        if not text.strip():
            self._json(400, {"error": i18n.t("vpn.err.not_imported")})
            return
        pull_dns = server_openvpn(tunnel).get("pull_dns", True)
        conf_text = vpn_privilege.runtime_conf(text, pull_dns=pull_dns)
        ok, code = vpn_privilege.install(
            conf_text=conf_text,
            mgmt_password=keychain.ensure_vpn_mgmt_password())
        self._json(200, {"ok": ok, "error_code": "" if ok else code})

    def _api_vpn_connect(self, data):
        fn = getattr(self.server, "vpn_connect_fn", None)
        if fn is None:
            self._json(503, {"error": "vpn not available"})
            return
        # windowed app 的 stderr 丢失（ThreadingMixIn handle_error 无处落）
        # ——异常就地捕获：日志 + detail 回给 UI，真机定位不再靠猜
        try:
            result = fn(data.get("index"), force=data.get("force") is True)
        except Exception:
            logger.exception("vpn-connect handler failed")
            import traceback
            self._json(200, {"ok": False, "error": "exception",
                             "detail": traceback.format_exc()[-600:]})
            return
        self._json(200, result)

    def _api_vpn_disconnect(self, data):
        fn = getattr(self.server, "vpn_disconnect_fn", None)
        if fn is None:
            self._json(503, {"error": "vpn not available"})
            return
        try:
            result = fn()
        except Exception:
            logger.exception("vpn-disconnect handler failed")
            import traceback
            self._json(200, {"ok": False, "error": "exception",
                             "detail": traceback.format_exc()[-600:]})
            return
        self._json(200, result)

    # ── 隧道解析（index 路径单一归宿，test/test-forward/NFS 共用）──

    @staticmethod
    def _saved_tunnel_by_index(idx):
        """index → 已保存隧道：bool/int 守卫 + 空表/越界中文错误。
        test-tunnel / test-forward（旧载荷）/ NFS 端点的 index 解析
        共用（架构评审 R2-2：此前三处手抄同款守卫）。"""
        if isinstance(idx, bool) or not isinstance(idx, int):
            return None, "无效的服务器索引"
        cfg = _read_mp()
        rows = cfg.get("servers", []) if isinstance(cfg, dict) else []
        if not rows:
            return None, "尚未配置服务器"
        if not 0 <= idx < len(rows):
            return None, "服务器索引越界"
        return rows[idx], ""

    @staticmethod
    def _resolve_tunnel(data):
        """body 里解析隧道：显式 tunnel 优先，index 回退到已保存隧道。"""
        tunnel = data.get("tunnel")
        if tunnel is not None:
            if isinstance(tunnel, dict):
                return tunnel, ""
            return None, "无效的隧道"
        return _Handler._saved_tunnel_by_index(data.get("index"))


# ── 路由表（架构评审 R2-2）：一个端点一行声明，do_* 只剩表遍历 ──
# 认证策略统一由 walker 持有（favicon/agent.md/登录页三例外在 do_GET
# 内先行）；新增端点 = 表里加一行 + 一个 handler 方法，不再有
# 白名单 tuple ↔ elif 链双份声明。
_API_GET = {
    "/api/state": _Handler._api_state,
    "/api/balance": _Handler._api_balance,
    "/api/usage": _Handler._api_usage,
    "/api/cc-default-roles": _Handler._api_cc_default_roles,
    "/api/agents": _Handler._api_agents,
    "/api/provider-templates": _Handler._api_provider_templates,
    "/api/agent-instructions": _Handler._api_agent_instructions,
}
_API_POST = {
    "/api/fetch-models": _Handler._api_fetch_models,
    "/api/test-provider": _Handler._api_test_provider,
    "/api/setup-claude-code": _Handler._api_setup_claude_code,
    "/api/cc-sync-preview": _Handler._api_cc_sync_preview,
    "/api/test-tunnel": _Handler._api_test_tunnel,
    "/api/test-forward": _Handler._api_test_forward,
    "/api/server-check": _Handler._api_server_check,
    "/api/nfs-check-remote": _Handler._api_nfs_check_remote,
    "/api/nfs-setup-remote": _Handler._api_nfs_setup_remote,
    "/api/capture-clean": _Handler._api_capture_clean,
    "/api/probe-provider": _Handler._api_probe_provider,
    "/api/agent-setup-preview": _Handler._api_agent_setup_preview,
    "/api/setup-agent": _Handler._api_setup_agent,
    "/api/vpn-profile": _Handler._api_vpn_profile,
    "/api/vpn-credentials": _Handler._api_vpn_credentials,
    "/api/vpn-install": _Handler._api_vpn_install,
    "/api/vpn-connect": _Handler._api_vpn_connect,
    "/api/vpn-disconnect": _Handler._api_vpn_disconnect,
}
_API_PUT = {
    "/api/state": _Handler._api_put_state,
}


class ConfigServer:
    """Background HTTP server for the config UI.

    bind_host / token 是构造参数（Docker 适配的 seam）：默认绑
    127.0.0.1 + 自造随机 token（macOS 行为）；容器形态传
    bind_host="0.0.0.0" + 配置卷里的固定 token。
    """

    def __init__(self, on_sp_saved=None, on_mp_saved=None, port=CONFIG_PORT,
                 bind_host="127.0.0.1", token=None, runtime_state_fn=None):
        self._port = port
        self._bind_host = bind_host
        self._server = None
        self._thread = None
        self._token = token if token is not None else secrets.token_hex(16)
        self._on_sp_saved = on_sp_saved
        self._on_mp_saved = on_mp_saved
        # 运行态投影 getter → RuntimeProjection（架构评审 R3 单一 seam；
        # capture/forwards/mounts 三参合一）。app 注入，测试/容器缺席即
        # 空投影（capture_active=False、装饰全空）
        self._runtime_state_fn = runtime_state_fn

    @property
    def token(self):
        return self._token

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    @property
    def url(self):
        return f"http://127.0.0.1:{self._port}/"

    @property
    def port(self):
        """实际监听端口（start 后回写——port=0 时 url 不再撒谎，#71 S8）。"""
        return self._port

    def agent_instructions(self) -> str:
        """AI 助手指令文本（#70 S13）——文档知识与 API 面在此同一归宿。

        agent.md 由本服务供出，指令里的 curl 形状即本服务的 API 契约——
        文案随 API 演进只改一处。原生侧（app.py）只做剪贴板与通知；
        浏览器直开设置页无桥接，经认证 GET /api/agent-instructions 取
        同一份文本自行写剪贴板。
        """
        # ADR-009：macOS 形态配置 API 默认不常驻（复制手势自动开启）；
        # Docker 形态恒常驻，无需该提示
        hint = (
            "\n（复制本指令时配置 API 已自动开启；若稍后连接失败，请让用户"
            "在菜单 选 项 ▸ 打开「配置 API 服务」）"
            if self._bind_host == "127.0.0.1" else "")
        return (
            "我在用 Magic Stack（macOS 菜单栏应用）。\n"
            f"请先读 {self.url}agent.md 了解产品功能和配置方法。\n"
            "当前配置 API（需要 token）：\n"
            f'  curl -H "Authorization: Bearer {self.token}" {self.url}api/state\n'
            "你可以通过这个 API 读取和修改我的配置，帮我完成设置。"
            + hint)

    def start(self):
        """Start the server. Returns True on success, False if port unavailable."""
        if self.running:
            return True
        try:
            self._server = _ThreadingHTTPServer(
                (self._bind_host, self._port), _Handler,
                expected_token=self._token,
                on_sp_saved=self._on_sp_saved,
                on_mp_saved=self._on_mp_saved,
                runtime_state_fn=self._runtime_state_fn,
                instructions_fn=self.agent_instructions)
        except OSError:
            logger.warning("Config server: port %d unavailable", self._port)
            return False
        # port=0 由 OS 分配——回写真实端口（url/port 属性自此不撒谎）
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="ConfigServer", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
