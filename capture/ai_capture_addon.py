"""Magic Stack — AI capture mitmproxy addon (ADR-001 Task 3).

Loaded read-only into a frozen ``mitmdump`` via ``-s ai_capture_addon.py``.
Identifies AI chat traffic (OpenAI / Anthropic / DeepSeek / Doubao / Qwen /
MiniMax), extracts request prompts + reassembles the (possibly SSE-streamed)
response, and appends one JSON line per flow to
``$MAGIC_PROXY_CAPTURE_DIR/<local-date>.jsonl``. Non-AI traffic passes through
untouched. Zero IPC: config in via env at spawn, captures out to disk only.

The pure functions below carry no mitmproxy dependency so they unit-test under
plain pytest; the addon hooks are thin duck-typed adapters over them.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

try:
    from capture.capture_store import append_json
except ImportError:  # frozen：app Resources 内是扁平布局（--add-data dest="."），无包结构
    from capture_store import append_json

log = logging.getLogger("ai_capture_addon")
MAX_CAPTURE_FLOW_BYTES = 5 * 1024 * 1024
MAX_CAPTURE_TEXT_CHARS = 1_000_000


# ── 厂商 host 表（表驱动真相；drift 由 tests/test_capture_host_mirror.py 钉住）──
# 与 shared/provider_auth.PROVIDER_REGISTRY 的 hosts 刻意分叉（frozen mitmdump
# 子进程零仓内 import 是资源契约；本表是抓包域知识），分叉关系须在漂移报警
# 测试的白名单里显式声明——两侧任何一边新增厂商/host 未挂号即红。
# 键序即匹配优先序（现无交叠，防将来新增时静默换序）。
# doubao 的额外 ``"ark" in host`` 约束见 match_provider（方舟 API 子域，
# console.volces.com 等非 API 子域不抓）。
CAPTURE_HOST_SUFFIXES = {
    "openai": ("api.openai.com",),
    "anthropic": ("api.anthropic.com",),
    "deepseek": ("api.deepseek.com",),
    "doubao": ("volces.com",),
    "qwen": ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"),
    "minimax": ("api.minimaxi.com", "api.minimax.io", "api.minimax.chat"),
}


def match_provider(host):
    """host → capture 厂商键（None = 非 AI 域，放行）。

    后缀匹配（对子域/区域站稳健）；表驱动自 CAPTURE_HOST_SUFFIXES。
    """
    host = (host or "").lower()
    for provider, suffixes in CAPTURE_HOST_SUFFIXES.items():
        if any(host.endswith(s) for s in suffixes):
            if provider == "doubao" and "ark" not in host:
                continue
            return provider
    return None


# 端点表（R8-C7b：原 identify() 的 40 行路径梯子表化——纯数据穿代码
# 衣是同文件两形态：host 半边本就是表）。match: "eq"（精确）|"suffix"
# （deepseek 的区域前缀端点）。新增厂商 = host 表加行 + 此表加行，
# 与 CAPTURE_HOST_SUFFIXES 同屏对读。
CAPTURE_PATHS = {
    "openai": (
        ("/v1/chat/completions", "chat.completions", "eq"),
        ("/v1/responses", "responses", "eq"),
    ),
    "anthropic": (
        ("/v1/messages", "messages", "eq"),
    ),
    "deepseek": (
        ("/chat/completions", "chat.completions", "suffix"),
    ),
    "doubao": (
        ("/api/v3/chat/completions", "chat.completions", "eq"),
    ),
    "qwen": (
        ("/compatible-mode/v1/chat/completions", "chat.completions", "eq"),
        ("/api/v1/services/aigc/text-generation/generation",
         "dashscope.native", "eq"),
    ),
    "minimax": (
        ("/v1/text/chatcompletion_v2", "chat.completions", "eq"),
        ("/v1/text/chatcompletion_pro", "minimax.pro", "eq"),
    ),
}


def identify(host, path):
    """Map (host, path) to (provider, variant), or None to pass through.

    Host matched by suffix (robust to sub-domains / regions); path pins the
    chat endpoint so non-chat calls (embeddings / images / models) pass
    through（端点知识表驱动 CAPTURE_PATHS——与 host 表同一屏可对读）.
    """
    provider = match_provider(host)
    if provider is None:
        return None
    path = (path or "").split("?", 1)[0].split("#", 1)[0]
    for endpoint, variant, mode in CAPTURE_PATHS.get(provider, ()):
        if path == endpoint if mode == "eq" else path.endswith(endpoint):
            return (provider, variant)
    return None


# --------------------------------------------------------------------------
# Request extraction (verbatim prompt fidelity; non-text parts placeholdered)
# --------------------------------------------------------------------------

# Non-text modality carriers -> human-readable placeholder. Matched by content
# block "type" (OpenAI/Anthropic) or by bare modality key (DashScope native).
_PLACEHOLDER = {
    "image": "[image]", "image_url": "[image]", "input_image": "[image]",
    "audio": "[audio]", "input_audio": "[audio]",
    "video": "[video]", "input_video": "[video]",
    "file": "[file]", "document": "[file]", "input_file": "[file]",
    "tool_result": "[tool_result]", "tool_use": "[tool_use]",
}


def _part_to_text(part):
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    if isinstance(part.get("text"), str):  # text/input_text blocks + {"text": ...}
        return part["text"]
    kind = part.get("type", "")
    if kind in _PLACEHOLDER:
        return _PLACEHOLDER[kind]
    for key, placeholder in _PLACEHOLDER.items():  # DashScope bare-key parts
        if key in part:
            return placeholder
    return ""


def _content_to_text(content):
    """Normalize a message ``content`` (str | block-array | None) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_part_to_text(p) for p in content)
    if content is None:
        return ""
    return str(content)


def _norm_messages(messages):
    """role/content messages -> [{role, content-as-text}] (verbatim roles)."""
    return [
        {"role": m.get("role"), "content": _content_to_text(m.get("content"))}
        for m in (messages or [])
    ]


def _first_system(messages):
    return next((m["content"] for m in messages if m["role"] in ("system", "developer")), None)


def extract_request(variant, body):
    """Normalize a request body to {model, stream, system, messages}.

    ``messages`` is the verbatim conversation (system kept in-band per the
    ADR-001 example); ``system`` mirrors the system/instructions text at the
    top level for uniform grep across providers (top-level for anthropic /
    responses, in-band system role otherwise). ``None`` when absent.
    """
    body = body or {}
    model = body.get("model")
    stream = bool(body.get("stream", False))

    if variant == "chat.completions":
        msgs = _norm_messages(body.get("messages"))
        return {"model": model, "stream": stream, "system": _first_system(msgs), "messages": msgs}

    if variant == "responses":
        inp = body.get("input")
        if isinstance(inp, str):
            msgs = [{"role": "user", "content": inp}]
        elif isinstance(inp, list):
            msgs = [{"role": it.get("role", "user"), "content": _content_to_text(it.get("content"))}
                    for it in inp]
        else:
            msgs = []
        return {"model": model, "stream": stream, "system": body.get("instructions"), "messages": msgs}

    if variant == "messages":  # Anthropic — system is top-level
        system = _content_to_text(body.get("system")) if body.get("system") is not None else None
        return {"model": model, "stream": stream, "system": system,
                "messages": _norm_messages(body.get("messages"))}

    if variant == "dashscope.native":  # Qwen — messages nested under input
        inp = body.get("input") or {}
        msgs = _norm_messages(inp.get("messages"))
        system = _first_system(msgs)
        if system is None and isinstance(inp.get("system"), str):
            system = inp["system"]
        params = body.get("parameters") or {}
        return {"model": model, "stream": bool(params.get("incremental_output", False)),
                "system": system, "messages": msgs}

    if variant == "minimax.pro":  # legacy — sender_type + bot_setting
        role_map = {"USER": "user", "BOT": "assistant", "SYSTEM": "system", "FUNCTION": "tool"}
        msgs = []
        for m in body.get("messages") or []:
            content = m["text"] if "text" in m else _content_to_text(m.get("content"))
            msgs.append({"role": role_map.get(m.get("sender_type"), "user"), "content": content})
        bot_setting = body.get("bot_setting") or []
        system = bot_setting[0].get("content") if bot_setting and isinstance(bot_setting[0], dict) else None
        return {"model": model, "stream": stream, "system": system, "messages": msgs}

    return {"model": model, "stream": stream, "system": None, "messages": []}


# --------------------------------------------------------------------------
# Response reassembly — 4 SSE families (+ non-streaming direct parse)
# --------------------------------------------------------------------------

def _iter_sse(text):
    """Yield (event_name, data_str) per SSE event; comment / id / retry lines ignored.

    Normalize CRLF -> LF first: SSE line endings are spec'd as CRLF, and a raw
    ``\\n\\n`` split would fail to separate ``\\r\\n\\r\\n`` events (merging the
    whole stream into one block)."""
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data_lines = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield name, "\n".join(data_lines)


def _blank_result():
    return {"reassembled": "", "reasoning": None, "tool_calls": [],
            "finish_reason": None, "usage": None, "event_count": 0}


def _tool_list(acc):
    return [{"name": v["name"], "arguments": "".join(v["args"])} for _, v in sorted(acc.items())]


def _reassemble_openai_stream(text, r):
    answer, reasoning, tools = [], [], {}
    for _, data in _iter_sse(text):
        r["event_count"] += 1
        if data.strip() == "[DONE]":
            continue
        try:
            j = json.loads(data)
        except ValueError:
            continue
        if j.get("usage"):
            r["usage"] = j["usage"]
        choices = j.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            r["finish_reason"] = choice["finish_reason"]
        delta = choice.get("delta") or {}
        if isinstance(delta.get("content"), str):
            answer.append(delta["content"])
        if isinstance(delta.get("reasoning_content"), str):
            reasoning.append(delta["reasoning_content"])
        for call in delta.get("tool_calls") or []:
            slot = tools.setdefault(call.get("index", 0), {"name": None, "args": []})
            fn = call.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if isinstance(fn.get("arguments"), str):
                slot["args"].append(fn["arguments"])
    r["reassembled"] = "".join(answer)
    r["reasoning"] = "".join(reasoning) or None
    r["tool_calls"] = _tool_list(tools)
    return r


def _reassemble_anthropic_stream(text, r):
    blocks, usage = {}, {}
    for name, data in _iter_sse(text):
        r["event_count"] += 1
        try:
            j = json.loads(data)
        except ValueError:
            continue
        etype = j.get("type") or name
        if etype == "message_start":
            usage.update((j.get("message") or {}).get("usage") or {})
        elif etype in ("content_block_start", "content_block_delta"):
            slot = blocks.setdefault(j.get("index", 0),
                                     {"type": None, "text": [], "thinking": [], "name": None, "args": []})
            if etype == "content_block_start":
                cb = j.get("content_block") or {}
                slot["type"] = cb.get("type")
                if cb.get("name"):
                    slot["name"] = cb["name"]
            else:
                delta = j.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    slot["text"].append(delta.get("text", ""))
                elif dtype == "thinking_delta":
                    slot["thinking"].append(delta.get("thinking", ""))
                elif dtype == "input_json_delta":
                    slot["args"].append(delta.get("partial_json", ""))
        elif etype == "message_delta":
            if (j.get("delta") or {}).get("stop_reason"):
                r["finish_reason"] = j["delta"]["stop_reason"]
            usage.update(j.get("usage") or {})
    ordered = [blocks[i] for i in sorted(blocks)]
    r["reassembled"] = "".join(t for b in ordered for t in b["text"])
    r["reasoning"] = "".join(t for b in ordered for t in b["thinking"]) or None
    r["tool_calls"] = [{"name": b["name"], "arguments": "".join(b["args"])}
                       for b in ordered if b["type"] == "tool_use" or b["args"]]
    r["usage"] = usage or None
    return r


def _reassemble_responses_stream(text, r):
    answer, reasoning, tools = [], [], {}
    for name, data in _iter_sse(text):
        r["event_count"] += 1
        try:
            j = json.loads(data)
        except ValueError:
            continue
        etype = j.get("type") or name
        if etype == "response.output_text.delta":
            answer.append(j.get("delta", ""))
        elif etype in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
            reasoning.append(j.get("delta", ""))
        elif etype == "response.output_item.added":
            item = j.get("item") or {}
            if item.get("type") == "function_call":
                slot = tools.setdefault(j.get("output_index", 0), {"name": None, "args": []})
                if item.get("name"):
                    slot["name"] = item["name"]
        elif etype == "response.function_call_arguments.delta":
            tools.setdefault(j.get("output_index", 0), {"name": None, "args": []})["args"].append(j.get("delta", ""))
        elif etype in ("response.completed", "response.incomplete"):
            resp = j.get("response") or {}
            r["usage"] = resp.get("usage") or r["usage"]
            r["finish_reason"] = resp.get("status") or r["finish_reason"]
    r["reassembled"] = "".join(answer)
    r["reasoning"] = "".join(reasoning) or None
    r["tool_calls"] = _tool_list(tools)
    return r


def _reassemble_dashscope_stream(text, r):
    parts, last, cumulative, prev = [], "", True, ""
    for _, data in _iter_sse(text):
        r["event_count"] += 1
        try:
            j = json.loads(data)
        except ValueError:
            continue
        if j.get("usage"):
            r["usage"] = j["usage"]
        out = j.get("output") or {}
        txt = out.get("text")
        finish = out.get("finish_reason")
        if txt is None and out.get("choices"):
            choice = out["choices"][0]
            txt = (choice.get("message") or {}).get("content")
            finish = choice.get("finish_reason") or finish
        if finish and finish != "null":
            r["finish_reason"] = finish
        if txt is not None:
            if prev and not txt.startswith(prev):
                cumulative = False
            parts.append(txt)
            last, prev = txt, txt
    r["reassembled"] = last if cumulative else "".join(parts)
    return r


def _reassemble_non_stream(variant, text, r):
    r["event_count"] = 1
    try:
        j = json.loads(text)
    except ValueError:
        return r

    if variant == "messages":  # Anthropic
        texts, thinks, tools = [], [], []
        for block in j.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "thinking":
                thinks.append(block.get("thinking", ""))
            elif btype == "tool_use":
                tools.append({"name": block.get("name"),
                              "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)})
        r.update(reassembled="".join(texts), reasoning="".join(thinks) or None,
                 tool_calls=tools, finish_reason=j.get("stop_reason"), usage=j.get("usage"))
        return r

    if variant == "responses":  # OpenAI Responses
        texts = [c.get("text", "") for item in j.get("output") or []
                 for c in item.get("content") or [] if c.get("type") in ("output_text", "text")]
        r.update(reassembled="".join(texts), finish_reason=j.get("status"), usage=j.get("usage"))
        return r

    if variant == "dashscope.native":  # Qwen native
        out = j.get("output") or {}
        txt, finish = out.get("text"), out.get("finish_reason")
        if txt is None and out.get("choices"):
            choice = out["choices"][0]
            txt = (choice.get("message") or {}).get("content")
            finish = choice.get("finish_reason") or finish
        r.update(reassembled=txt or "", finish_reason=finish, usage=j.get("usage"))
        return r

    if variant == "minimax.pro":  # legacy
        choices = j.get("choices") or []
        txt = ""
        if choices:
            messages = choices[0].get("messages") or []
            txt = messages[0].get("text", "") if messages else ""
            r["finish_reason"] = choices[0].get("finish_reason")
        r.update(reassembled=txt or j.get("reply", ""), usage=j.get("usage"))
        return r

    # chat.completions (openai / deepseek / doubao / qwen-compat / minimax-v2)
    choices = j.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        r["reassembled"] = msg.get("content") or ""
        if isinstance(msg.get("reasoning_content"), str):
            r["reasoning"] = msg["reasoning_content"]
        r["tool_calls"] = [{"name": (c.get("function") or {}).get("name"),
                            "arguments": (c.get("function") or {}).get("arguments", "")}
                           for c in msg.get("tool_calls") or []]
        r["finish_reason"] = choices[0].get("finish_reason")
    r["usage"] = j.get("usage")
    return r


# variant -> streaming reassembler; default is the OpenAI-style data: family.
_STREAM_REASSEMBLERS = {
    "messages": _reassemble_anthropic_stream,
    "responses": _reassemble_responses_stream,
    "dashscope.native": _reassemble_dashscope_stream,
}


def reassemble(variant, is_stream, body_text):
    """Reassemble a response body into {reassembled, reasoning, tool_calls,
    finish_reason, usage, event_count}. ``body_text`` is the SSE stream when
    ``is_stream`` else the JSON response text."""
    r = _blank_result()
    if not body_text:
        return r
    if is_stream:
        return _STREAM_REASSEMBLERS.get(variant, _reassemble_openai_stream)(body_text, r)
    return _reassemble_non_stream(variant, body_text, r)


# --------------------------------------------------------------------------
# Record assembly + JSONL sink
# --------------------------------------------------------------------------

def build_record(meta, status_code, resp_text, bytes_down, capture_raw_sse=False):
    """Assemble one JSONL record. Preserves the ADR-001 field set and adds
    additive fields. Response reassembly is fault-isolated: on failure the
    record carries ``capture_error`` and the raw body is kept unconditionally
    (raw kept opt-in via ``capture_raw_sse`` otherwise)."""
    record = {
        "ts": meta.get("ts"), "flow_id": meta.get("flow_id"), "method": meta.get("method"),
        "url": meta.get("url"), "host": meta.get("host"), "provider": meta.get("provider"),
        "api_variant": meta.get("variant"), "model": meta.get("model"),
        "stream": meta.get("stream", False), "status_code": status_code,
        "request": {"system": meta.get("system"), "messages": meta.get("messages", [])},
        "usage": None, "bytes_up": meta.get("bytes_up"), "bytes_down": bytes_down,
        "duration_ms": meta.get("duration_ms"), "capture_error": None,
    }
    is_stream = meta.get("stream", False)
    try:
        rr = reassemble(meta.get("variant"), is_stream, resp_text)
        response = {"reassembled": rr["reassembled"], "reasoning": rr["reasoning"],
                    "tool_calls": rr["tool_calls"], "finish_reason": rr["finish_reason"],
                    "event_count": rr["event_count"]}
        record["usage"] = rr["usage"]
        if capture_raw_sse and is_stream:
            response["sse_chunks"] = [data for _, data in _iter_sse(resp_text)]
    except Exception as exc:  # noqa: BLE001 — capture must never break the proxy path
        record["capture_error"] = f"{type(exc).__name__}: {exc}"
        response = {"reassembled": None}
    if record["capture_error"] is not None:
        response["raw"] = resp_text  # keep raw unconditionally for failed reassembly
    record["response"] = response
    return record


def write_jsonl(record, capture_dir):
    """Append one JSON line to ``<capture_dir>/<local-date>.jsonl`` (created on
    demand). ``ensure_ascii=False`` keeps CJK prompts human-readable for grep/jq."""
    return append_json(record, capture_dir)


# --------------------------------------------------------------------------
# mitmproxy addon — thin duck-typed adapter over the pure functions above
# --------------------------------------------------------------------------

_DEFAULT_DIR = os.path.expanduser("~/.magic-proxy-captures")


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _utc_now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _truncate_value(value):
    """Bound captured request text while preserving its JSON shape."""
    truncated = False
    if isinstance(value, str):
        if len(value) > MAX_CAPTURE_TEXT_CHARS:
            return value[:MAX_CAPTURE_TEXT_CHARS], True
        return value, False
    if isinstance(value, list):
        result = []
        for item in value:
            bounded, cut = _truncate_value(item)
            result.append(bounded)
            truncated = truncated or cut
        return result, truncated
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            bounded, cut = _truncate_value(item)
            result[key] = bounded
            truncated = truncated or cut
        return result, truncated
    return value, False


class AICaptureAddon:
    """Read-only mitmproxy addon. Config flows in via env at ``load``; captures
    flow out to JSONL only (zero IPC). Hooks never raise — a capture failure
    must not disturb the proxied traffic."""

    def __init__(self):
        self.capture_dir = _DEFAULT_DIR
        self.capture_raw_sse = False
        self.preserve_streaming = False

    def load(self, loader=None):
        self.capture_dir = os.environ.get("MAGIC_PROXY_CAPTURE_DIR") or _DEFAULT_DIR
        self.capture_raw_sse = _env_flag("MAGIC_PROXY_CAPTURE_RAW_SSE")
        self.preserve_streaming = _env_flag("MAGIC_PROXY_PRESERVE_STREAMING")

    # -- hooks -------------------------------------------------------------
    def request(self, flow):
        # Hooks never raise（#69 S11b：response()/error() 有守卫、request()
        # 没有——畸形 body 曾让该 flow 静默永不被抓包）
        try:
            self._request_inner(flow)
        except Exception:
            log.exception("ai_capture request hook dropped flow")

    def _request_inner(self, flow):
        ident = identify(flow.request.pretty_host, flow.request.path)
        if ident is None:
            return  # non-AI (or non-chat) traffic passes through untouched
        provider, variant = ident
        # Decode UTF-8 explicitly rather than flow.request.get_text(): these APIs
        # are always UTF-8 (SSE mandates it), but the Content-Type often omits an
        # explicit charset, and mitmproxy's charset guess garbles multibyte CJK.
        # .content is content-encoding (gzip/br) decoded; we own the charset.
        try:
            body = json.loads((flow.request.content or b"").decode("utf-8", "replace") or "{}")
        except ValueError:
            body = {}
        req = extract_request(variant, body)
        bounded_messages, request_truncated = _truncate_value(req["messages"])
        bounded_system, system_truncated = _truncate_value(req["system"])
        acc = CaptureAccumulator(budget=MAX_CAPTURE_FLOW_BYTES)
        acc.reserve_request_snapshot(
            json.dumps({"system": bounded_system, "messages": bounded_messages},
                       ensure_ascii=False).encode("utf-8"))
        flow.metadata["ai_capture"] = {
            "_acc": acc,
            "ts": _utc_now_iso(), "flow_id": flow.id, "method": flow.request.method,
            "url": flow.request.url, "host": flow.request.pretty_host,
            "provider": provider, "variant": variant, "model": req["model"],
            "stream": req["stream"], "system": bounded_system, "messages": bounded_messages,
            "request_truncated": request_truncated or system_truncated,
            "bytes_up": len(flow.request.raw_content or b""), "t0": time.monotonic(),
        }

    def responseheaders(self, flow):
        # issue #11：AI 流式响应默认 tee/pass-through——首块延迟不再等待
        # 完整响应（此前需 MAGIC_PROXY_PRESERVE_STREAMING 选择性开启）。
        # 捕获异常或预算耗尽都不影响下发：tee 原样返回 chunk。
        meta = flow.metadata.get("ai_capture")
        if not meta:
            return
        if not meta["stream"] and not self.capture_raw_sse:
            return  # 非流式走 buffered 路径（response 钩子读 content）
        acc = meta.get("_acc")
        if acc is None:
            acc = CaptureAccumulator(budget=MAX_CAPTURE_FLOW_BYTES)
            meta["_acc"] = acc
        flow.response.stream = acc.tee

    def response(self, flow):
        meta = flow.metadata.get("ai_capture")
        if not meta or meta.get("_written"):
            return
        try:
            acc = meta.get("_acc")
            if acc is not None and acc.total_seen:
                resp_text = acc.captured().decode("utf-8", "replace")
                bytes_down = acc.total_seen
                meta["response_truncated"] = acc.truncated
            else:
                content = flow.response.content or b""
                bytes_down = len(flow.response.raw_content or b"")
                meta["response_truncated"] = len(content) > MAX_CAPTURE_FLOW_BYTES
                resp_text = content[:MAX_CAPTURE_FLOW_BYTES].decode("utf-8", "replace")
            meta["duration_ms"] = int((time.monotonic() - meta["t0"]) * 1000)
            record = build_record(meta, flow.response.status_code, resp_text,
                                  bytes_down, capture_raw_sse=self.capture_raw_sse)
            record["request"]["truncated"] = bool(meta.get("request_truncated"))
            record["response"]["truncated"] = bool(meta.get("response_truncated"))
            record["response"]["captured_bytes"] = len(resp_text.encode("utf-8"))
            write_jsonl(record, self.capture_dir)
            meta["_written"] = True
        except Exception:  # noqa: BLE001 — never raise out of a hook
            log.exception("ai_capture: failed to record flow %s", meta.get("flow_id"))

    def error(self, flow):
        meta = flow.metadata.get("ai_capture")
        if not meta or meta.get("_written"):
            return
        try:
            meta["duration_ms"] = int((time.monotonic() - meta["t0"]) * 1000)
            acc0 = meta.get("_acc")
            raw = acc0.captured().decode("utf-8", "replace") if acc0 is not None else ""
            record = build_record(meta, None, raw, len(raw), capture_raw_sse=self.capture_raw_sse)
            record["capture_error"] = record["capture_error"] or "flow error / aborted before completion"
            record.setdefault("response", {})["raw"] = raw
            write_jsonl(record, self.capture_dir)
            meta["_written"] = True
        except Exception:  # noqa: BLE001
            log.exception("ai_capture: failed to record errored flow %s", meta.get("flow_id"))


addons = [AICaptureAddon()]


class CaptureAccumulator:
    """每 flow 单一聚合内存预算（issue #11）.

    tee() 原样立即下发（对代理字节流零影响），同时把 chunk 收进预算内
    缓冲；request 快照先预留同一预算池。截断按字节累计——大量小
    message 也无法绕过。captured() 保证 UTF-8 安全（截点回退到字符
    边界由消费方 replace 兜底，此处按字节预算硬停）。
    """

    def __init__(self, budget=MAX_CAPTURE_FLOW_BYTES):
        self.budget = budget
        self._buf = bytearray()       # 仅响应字节（resp_text 来源）
        self._reserved = 0            # 请求快照占去的预算（不进 _buf）
        self.total_seen = 0
        self.truncated = False

    def reserve_request_snapshot(self, data: bytes):
        """请求快照占用预算池（与响应累计共享同一总额，但不进响应缓冲）。"""
        take = min(len(data), self.budget)
        self._reserved = take
        if len(data) > take:
            self.truncated = True

    def tee(self, chunk: bytes) -> bytes:
        """流量字节原样下发；预算内收集。绝不修改 chunk。"""
        self.total_seen += len(chunk)
        remaining = self.budget - self._reserved - len(self._buf)
        if remaining <= 0:
            self.truncated = True
            return chunk
        take = min(len(chunk), remaining)
        self._buf += chunk[:take]
        if take < len(chunk):
            self.truncated = True
        return chunk

    def total_budgeted(self) -> int:
        """聚合占用 = 请求快照预留 + 响应缓冲。"""
        return self._reserved + len(self._buf)

    def captured(self) -> bytes:
        """预算内响应缓冲；截点回退到 UTF-8 字符边界（不产生半个字符）。"""
        buf = bytes(self._buf)
        if not self.truncated:
            return buf
        # 回退到字符边界：先跳过尾部的后续字节（10xxxxxx），再检查
        # 其起始字节（>=0xC0）——被截断的序列按其前缀位数判断是否完整
        end = len(buf)
        while end > 0 and (buf[end - 1] & 0xC0) == 0x80:
            end -= 1
        if end > 0 and buf[end - 1] >= 0xC0:
            start = buf[end - 1]
            need = (2 if start < 0xE0 else 3 if start < 0xF0 else 4)
            if end - 1 + need > len(buf):
                end -= 1  # 序列不完整——丢弃整个残字符
        return buf[:end]
