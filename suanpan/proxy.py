"""Streaming proxy: forwards bytes to the backend, tees them through a
read-only SSE parser to extract authoritative usage from message_start /
message_delta events.

镖师：押车上路，把客人交给目的地，沿途记一笔账。
"""

from __future__ import annotations

import time
from typing import Literal

import httpx
import logging
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from suanpan.compat import (
    OpenAIChatToAnthropicSSE,
    anthropic_to_openai_request,
    normalize_body,
    openai_chat_response_to_anthropic,
    openai_error_to_anthropic,
    openai_strip_subagent_marker,
)
from suanpan.config import AppConfig
from suanpan.router import RouteDecision, strip_marker
from suanpan.usage_extractor import UsageExtractor
from suanpan.usage_log import UsageEntry, UsageLogger, usage_entry_from_wire
from shared.provider_auth import build_outbound_headers as _build_headers_openai

_log = logging.getLogger("magic-proxy.suanpan.proxy")

# ── RetryPolicy（issue #7）────────────────────────────────────────────
# 无法证明请求未送达时，非幂等请求绝不自动重放：ReadError /
# RemoteProtocolError / WriteError / 读写超时都可能发生在上游已经处理
# 之后——重放即重复推理/计费/tool side effect。
# 可重试的充分条件：
#   1. pre-send-proven——连接建立/等池阶段失败（ConnectError/
#      ConnectTimeout/PoolTimeout/ProxyError/UnsupportedProtocol），
#      请求一个字节都没出本机；
#   2. idempotent-transport——幂等方法（GET/HEAD/PUT/DELETE/OPTIONS 或显式
#      幂等键）遇传输层错误，有界一次。
# 超时例外沿既有理由：慢上游不靠加倍修复（幂等也不重试）。
_PRE_SEND_PROVEN = (httpx.ConnectError, httpx.ConnectTimeout,
                    httpx.PoolTimeout, httpx.ProxyError,
                    httpx.UnsupportedProtocol)
_TRANSPORT_ERRORS = (httpx.NetworkError, httpx.ProtocolError,
                     httpx.ProxyError, httpx.UnsupportedProtocol)
_IDEMPOTENT_METHODS = ("GET", "HEAD", "PUT", "DELETE", "OPTIONS")
_MAX_RETRIES = 1


def should_retry(method, error, *, idempotent=None, attempt=0):
    """→ (是否重试, reason)。判定见模块 RetryPolicy 注释。"""
    if isinstance(error, _PRE_SEND_PROVEN) and attempt < _MAX_RETRIES:
        return True, "pre-send-proven"
    if isinstance(error, httpx.TimeoutException):
        return False, "timeout-ambiguous"
    idem = (idempotent if idempotent is not None
            else method.upper() in _IDEMPOTENT_METHODS)
    if idem and isinstance(error, _TRANSPORT_ERRORS) and attempt < _MAX_RETRIES:
        return True, "idempotent-transport"
    return False, "post-send-ambiguous"


async def _send_with_retry(
    http_client: httpx.AsyncClient, req: httpx.Request, *, idempotent=None,
) -> httpx.Response:
    """Send once; auto-retry only per RetryPolicy（issue #7）.

    req 必须可重发（content 为完整 bytes，非已消费 stream）——调用方
    构造的 upstream_req 即此形态。幂等性：方法族自动判定，或显式传
    ``idempotent=True``（如携带服务端幂等键的 POST）。
    """
    attempt = 0
    while True:
        try:
            return await http_client.send(req, stream=True)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            retry, reason = should_retry(req.method, e,
                                         idempotent=idempotent, attempt=attempt)
            if not retry:
                raise
            attempt += 1
            _log.warning("transport_retry url=%s error=%s reason=%s attempt=%s",
                         req.url, type(e).__name__, reason, attempt)


async def drain_and_log(
    response: httpx.Response,
    extractor: UsageExtractor,
    logger: UsageLogger,
    *,
    provider: str,
    source_model: str,
    target_model: str,
    scenario: str,
    started: float,
    translator: OpenAIChatToAnthropicSSE | None = None,
    agent: str = "",
):
    """Stream response bytes to the caller while extracting usage.

    Feeds each raw chunk to *extractor* for SSE parsing, yields the bytes
    unchanged for the StreamingResponse, then logs a UsageEntry in the
    finally block — even if the consumer disconnects mid-stream.

    *translator*（ADR-010 转换 A）：openai 出站车道先把上游分块译成
    Anthropic SSE 再下发——extractor 与客户端看到的都是翻译后字节，
    直通车道（None）维持字节原样。

    Extracted from a closure so the SSE→extractor→usage chain is testable
    with real byte arrays (e.g. GLM ``data:`` vs KIMI ``data:`` prefix).
    """
    stream_error = None
    try:
        if translator is None:
            async for chunk in response.aiter_raw():
                extractor.feed(chunk)
                yield chunk
        else:
            async for chunk in response.aiter_raw():
                out = translator.feed(chunk)
                if out:
                    extractor.feed(out)
                    yield out
            tail = translator.finish()
            if tail:
                extractor.feed(tail)
                yield tail
    except Exception as e:  # noqa: BLE001 — recorded, then re-raised to caller
        stream_error = f"{type(e).__name__}: {e}"
        raise
    finally:
        try:
            await response.aclose()
        except Exception:  # noqa: BLE001 — closing must never mask the stream
            pass
        try:
            logger.write(
                UsageEntry(
                    provider=provider,
                    source_model=source_model,
                    target_model=target_model,
                    scenario=scenario,
                    agent=agent,
                    input_tokens=extractor.input_tokens,
                    output_tokens=extractor.output_tokens,
                    cache_read_tokens=extractor.cache_read_tokens,
                    cache_creation_tokens=extractor.cache_creation_tokens,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    status=response.status_code,
                    error=stream_error,
                )
            )
        except Exception:  # noqa: BLE001 — usage logging must never truncate the stream
            logger_exc = logger.__class__.__name__
            _log.warning("usage_log.write failed (%s): %s", logger_exc, stream_error)


def filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    drop = {"content-length", "transfer-encoding", "connection"}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


# ── 来源 Agent 判别（ADR-010 M5：统计的 agent 维度）─────────────────
# User-Agent 子串 → Agent id；序敏感（codex 先于其它）。未识别 = ""。
_UA_AGENTS = (
    ("codex", "codex"),
    ("claude-cli", "claude-code"),
    ("claude", "claude-code"),
    ("opencode", "opencode"),
    ("zcode", "zcode"),
)


def agent_from_user_agent(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    for needle, agent in _UA_AGENTS:
        if needle in ua:
            return agent
    return ""


def make_502(ctx: "_LaneCtx", error: str, logger: "UsageLogger | None" = None, *,
             wire: Literal["anthropic", "openai"] = "anthropic") -> JSONResponse:
    """502 失败响应 + 全零用量记账（四车道共用塑形；R8-C7 记账半边
    按需——logger=None 跳过，count_tokens 等无用量端点不再手拼孪生）。

    ``wire`` 决定错误体形状：anthropic 入站客户端认平铺 error；openai
    入站（chat/responses 直通）认 ``error.message`` 对象——曾两份孪生
    （make_502 vs make_502_openai），签名漂移使四车道发送块无法机械
    合一，现收敛为一个塑形器。
    """
    if logger is not None:
        logger.write(
        UsageEntry(
            provider=ctx.provider,
            source_model=ctx.source_model,
            target_model=ctx.target_model,
            scenario=ctx.scenario,
            agent=ctx.agent,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            latency_ms=int((time.monotonic() - ctx.started) * 1000),
            status=502,
            error=error,
        )
    )
    if wire == "openai":
        payload = {"error": {"message": "backend request failed",
                             "type": "api_error", "provider": ctx.provider,
                             "last_error": error}}
    else:
        payload = {"error": "backend request failed",
                   "provider": ctx.provider, "last_error": error}
    return JSONResponse(
        payload,
        status_code=502,
        headers={"x-suanpan-provider": ctx.provider},
    )


def _annotate_fallback(out_headers, decision, provider_name):
    """#50：显式意图 fall-through 可感知——响应头 + 日志宣告原意图，
    绝不静默误投。意图串已在 router 捕获时消毒（无 CR/LF/控制字符）。"""
    if decision.fallback_from:
        out_headers["x-suanpan-fallback"] = decision.fallback_from
        _log.warning("route_fallback intent=%s provider=%s scenario=%s",
                     decision.fallback_from, provider_name, decision.scenario)


# ── 车道共用骨架（架构评审 R2-1 + R5 候选 2）─────────────────────
# 四个 forward_* 共享同一发送纪律（build → RetryPolicy → 幂等探针 →
# 传输错误/5xx 的 502 塑形 → 响应头三件套 → 流式尾）——曾四份手抄
# （幂等探针逐字 ×4、发送→502→5xx 前置块 ×4、make_502 孪生双份）。
# 骨架在此单一归宿；各车道只剩真差异：请求整备、URL、错误体形状
# （wire=anthropic/openai）、响应塑形（直通/转换）。

_IDEMPOTENCY_HEADERS = ("idempotency-key", "x-idempotency-key")


class _LaneCtx:
    """一条转发请求的记账上下文（四车道同款 prologue 的单一归宿）。"""

    __slots__ = ("provider", "source_model", "target_model", "scenario",
                 "started", "agent")

    def __init__(self, request, decision, source_model, started):
        self.provider = decision.provider
        self.source_model = source_model
        self.target_model = decision.target_model
        self.scenario = decision.scenario
        self.started = started
        self.agent = agent_from_user_agent(request.headers.get("user-agent"))

    @classmethod
    def from_body(cls, request, body, decision):
        """source_model 取请求体原 model（须在改写 target_model 之前）。"""
        return cls(request, decision, body.get("model", ""), time.monotonic())


async def _send_upstream(http_client, request, url, out_body, headers):
    """build_request + RetryPolicy 发送 + 幂等探针（车道共用）。

    明确幂等语义（issue #7 验收③）：客户端携带幂等键头即视为可安全
    重放——探针判定单一归宿，不再逐车道手抄。"""
    upstream_req = http_client.build_request(
        "POST", url, json=out_body, headers=headers)
    return await _send_with_retry(
        http_client, upstream_req,
        idempotent=any(h in request.headers for h in _IDEMPOTENCY_HEADERS))


async def _reject_5xx(upstream_resp, provider_name):
    """5xx → 关流并返回错误串（无重试、无后端切换——车道共用纪律）。"""
    status = upstream_resp.status_code
    await upstream_resp.aclose()
    error = f"HTTP {status}"
    _log.error("upstream_5xx provider=%s status=%s", provider_name, status)
    return error


async def _send_lane(http_client, request, url, out_body, headers, ctx,
                     logger, *, wire: Literal["anthropic", "openai"] = "anthropic"):
    """车道发送前置块（R5 候选 2）：发送 → 传输错误/5xx 的 502 塑形。

    曾四份手抄（只差 502 塑形器形状）——步骤骨架之外，编排也归一处。
    返回上游响应（调用方继续响应塑形），或已成形的 502 JSONResponse
    （调用方 isinstance 判别即返）。``wire`` 透传 make_502 决定错误体
    形状（anthropic 平铺 / openai error.message 对象）。
    """
    try:
        upstream_resp = await _send_upstream(
            http_client, request, url, out_body, headers)
    except httpx.HTTPError as e:
        error = f"{type(e).__name__}: {e}"
        _log.error("upstream_error provider=%s error=%s", ctx.provider, error)
        return make_502(ctx, error, logger, wire=wire)
    if upstream_resp.status_code >= 500:
        error = await _reject_5xx(upstream_resp, ctx.provider)
        return make_502(ctx, error, logger, wire=wire)
    return upstream_resp


def _lane_out_headers(upstream_headers, ctx, decision):
    """响应头三件套：hop 头过滤 + 宣告 provider + fallback 可感知。"""
    out_headers = filter_response_headers(upstream_headers)
    out_headers["x-suanpan-provider"] = ctx.provider
    _annotate_fallback(out_headers, decision, ctx.provider)
    return out_headers


def _stream_response(upstream_resp, extractor, logger, ctx, out_headers, *,
                     translator=None, media_type):
    """流式尾（车道共用）：drain_and_log tee 用量 + 原样下发字节。"""
    return StreamingResponse(
        drain_and_log(
            upstream_resp, extractor, logger,
            provider=ctx.provider, source_model=ctx.source_model,
            target_model=ctx.target_model, scenario=ctx.scenario,
            started=ctx.started, translator=translator, agent=ctx.agent,
        ),
        status_code=upstream_resp.status_code,
        headers=out_headers,
        media_type=media_type,
    )


async def forward_request(
    request: Request,
    body: dict,
    decision: RouteDecision,
    config: AppConfig,
    logger: UsageLogger,
    http_client: httpx.AsyncClient,
) -> StreamingResponse | JSONResponse:
    if decision.strip_marker:
        strip_marker(body)

    ctx = _LaneCtx.from_body(request, body, decision)

    provider_cfg = config.providers[ctx.provider]
    api_key = provider_cfg.resolve_api_key()

    body["model"] = ctx.target_model
    if provider_cfg.protocol == "openai":
        # ADR-010 转换 A：Anthropic 入站 × openai 端点（Claude Code 用
        # OpenAI/OpenRouter/通义/硅基流动等）。anthropic 主路径零改动。
        return await _forward_request_openai(
            request, body, decision, provider_cfg, api_key, logger,
            http_client, ctx=ctx)
    normalize_body(body, ctx.provider,
                   anthropic_native=provider_cfg.anthropic_native)
    headers = provider_cfg.build_outbound_headers(
        dict(request.headers), api_key)
    url = f"{provider_cfg.base_url.rstrip('/')}/v1/messages"

    upstream_resp = await _send_lane(
        http_client, request, url, body, headers, ctx, logger)
    if isinstance(upstream_resp, JSONResponse):
        return upstream_resp

    out_headers = _lane_out_headers(upstream_resp.headers, ctx, decision)

    # Non-streaming upstreams answer application/json with usage at the
    # top level — the SSE scanner would read zeros (DeepSeek stream:false
    # requests logged empty rows from 8/15 until this content-type switch).
    content_type = upstream_resp.headers.get("content-type", "")
    extractor = UsageExtractor(json_mode="json" in content_type.lower())

    return _stream_response(
        upstream_resp, extractor, logger, ctx, out_headers,
        media_type=upstream_resp.headers.get("content-type"))


async def _forward_request_openai(
    request: Request,
    body: dict,
    decision: RouteDecision,
    provider_cfg,
    api_key: str | None,
    logger: UsageLogger,
    http_client: httpx.AsyncClient,
    *,
    ctx: _LaneCtx,
) -> StreamingResponse | JSONResponse:
    """转换 A 的 openai 出站半边（ADR-010）：请求向转换 → {base}/chat/
    completions → 响应向转换（非流式 JSON / 流式 SSE 翻译）。

    翻译后的字节流即合法 Anthropic 响应——客户端 SDK 与 UsageExtractor
    都无需感知 openai 线格式。
    """
    out_body = anthropic_to_openai_request(body)
    # openai 车道认证恒 Bearer（ADR-010）：auth_header 仅对 anthropic
    # 车道有意义，绕开 Provider 方法直用共享实现的默认 Bearer 形态。
    headers = _build_headers_openai(dict(request.headers), api_key)
    url = f"{provider_cfg.base_url.rstrip('/')}/chat/completions"

    upstream_resp = await _send_lane(
        http_client, request, url, out_body, headers, ctx, logger)
    if isinstance(upstream_resp, JSONResponse):
        return upstream_resp

    content_type = upstream_resp.headers.get("content-type", "")
    streaming = ("event-stream" in content_type.lower()
                 and bool(out_body.get("stream")))
    if not streaming:
        # 非流式（或流式请求被上游以 JSON 错误回绝）：读全响应体后转换
        try:
            await upstream_resp.aread()
        except httpx.HTTPError as e:
            try:
                await upstream_resp.aclose()
            except Exception:  # noqa: BLE001 — 关闭失败不得掩过原始流错误
                pass
            error = f"{type(e).__name__}: {e}"
            _log.error("upstream_error provider=%s error=%s",
                       ctx.provider, error)
            return make_502(ctx, error, logger)
        try:
            payload = upstream_resp.json()
        except ValueError:
            payload = None
        status = upstream_resp.status_code
        out_headers = _lane_out_headers(upstream_resp.headers, ctx, decision)
        if status >= 400:
            converted = openai_error_to_anthropic(payload, status)
            usage = {"input_tokens": 0, "output_tokens": 0}
        else:
            converted = openai_chat_response_to_anthropic(
                payload, model=ctx.target_model)
            usage = converted.get("usage") or {}
        logger.write(usage_entry_from_wire(
            usage,
            provider=ctx.provider, source_model=ctx.source_model,
            target_model=ctx.target_model, scenario=ctx.scenario,
            agent=ctx.agent,
            latency_ms=int((time.monotonic() - ctx.started) * 1000),
            status=status, error=None))
        return JSONResponse(converted, status_code=status, headers=out_headers)

    # 流式：SSE 翻译生成器（输出即 Anthropic 事件流，extractor 无感）
    out_headers = _lane_out_headers(upstream_resp.headers, ctx, decision)
    translator = OpenAIChatToAnthropicSSE(model=ctx.target_model)
    extractor = UsageExtractor()
    return _stream_response(
        upstream_resp, extractor, logger, ctx, out_headers,
        translator=translator, media_type="text/event-stream")


async def forward_chat_passthrough(
    request: Request,
    body: dict,
    decision: RouteDecision,
    config: AppConfig,
    logger: UsageLogger,
    http_client: httpx.AsyncClient,
) -> StreamingResponse | JSONResponse:
    """ADR-010 直通车道：openai 入站（/v1/chat/completions）× openai 端点。

    请求仅改 ``model`` + 注入 ``stream_options.include_usage``（拿末块
    用量）；响应流**字节直通**（openai 客户端拿到原生 openai 响应），
    用量经 wire=openai_chat 的 UsageExtractor tee 提取。
    """
    provider_cfg = config.providers[decision.provider]
    ctx = _LaneCtx.from_body(request, body, decision)

    if decision.strip_marker:
        openai_strip_subagent_marker(body)
    body["model"] = ctx.target_model
    if body.get("stream"):
        opts = body.get("stream_options")
        if isinstance(opts, dict):
            opts.setdefault("include_usage", True)
        else:
            body["stream_options"] = {"include_usage": True}

    api_key = provider_cfg.resolve_api_key()
    headers = _build_headers_openai(dict(request.headers), api_key)
    url = f"{provider_cfg.base_url.rstrip('/')}/chat/completions"

    upstream_resp = await _send_lane(
        http_client, request, url, body, headers, ctx, logger, wire="openai")
    if isinstance(upstream_resp, JSONResponse):
        return upstream_resp

    out_headers = _lane_out_headers(upstream_resp.headers, ctx, decision)

    content_type = upstream_resp.headers.get("content-type", "")
    extractor = UsageExtractor(json_mode="json" in content_type.lower(),
                               wire="openai_chat")
    return _stream_response(
        upstream_resp, extractor, logger, ctx, out_headers,
        media_type=content_type or None)


async def forward_responses_passthrough(
    request: Request,
    body: dict,
    decision: RouteDecision,
    config: AppConfig,
    logger: UsageLogger,
    http_client: httpx.AsyncClient,
) -> StreamingResponse | JSONResponse:
    """ADR-010 M3a 直通车道：Responses 入站（/v1/responses，Codex）×
    厂商原生 Responses 端点（provider.responses_base_url）。

    请求仅改 ``model``（Responses 无 stream_options——用量固定出现在
    response.completed 事件）；响应流字节直通，用量经 wire=responses
    的 UsageExtractor tee 提取。认证恒 Bearer（ADR-010 端点卡契约）。
    """
    from suanpan.compat import responses_strip_subagent_marker

    provider_cfg = config.providers[decision.provider]
    ctx = _LaneCtx.from_body(request, body, decision)

    if decision.strip_marker:
        responses_strip_subagent_marker(body)
    body["model"] = ctx.target_model

    api_key = provider_cfg.resolve_api_key()
    headers = _build_headers_openai(dict(request.headers), api_key)
    url = f"{provider_cfg.responses_base_url.rstrip('/')}/responses"

    upstream_resp = await _send_lane(
        http_client, request, url, body, headers, ctx, logger, wire="openai")
    if isinstance(upstream_resp, JSONResponse):
        return upstream_resp

    out_headers = _lane_out_headers(upstream_resp.headers, ctx, decision)

    content_type = upstream_resp.headers.get("content-type", "")
    extractor = UsageExtractor(json_mode="json" in content_type.lower(),
                               wire="responses")
    return _stream_response(
        upstream_resp, extractor, logger, ctx, out_headers,
        media_type=content_type or None)


async def forward_count_tokens(
    request: Request,
    body: dict,
    decision: RouteDecision,
    config: AppConfig,
    http_client: httpx.AsyncClient,
) -> JSONResponse:
    """Non-streaming variant — hits /v1/messages/count_tokens."""
    provider_name = decision.provider
    target_model = decision.target_model
    provider_cfg = config.providers[provider_name]
    if provider_cfg.protocol == "openai":
        # ADR-010：count_tokens 仅服务 Anthropic 端点——OpenAI 协议无等价
        # 端点，不做 token 估算（错误可定位，静默估假数更糟）
        return JSONResponse(
            {"type": "error",
             "error": {"type": "invalid_request_error",
                       "message": "count_tokens 不适用于 openai 协议供应商"
                                  "（OpenAI 协议无该端点）"}},
            status_code=400,
            headers={"x-suanpan-provider": provider_name},
        )
    api_key = provider_cfg.resolve_api_key()
    if decision.strip_marker:
        strip_marker(body)
    ctx = _LaneCtx.from_body(request, body, decision)
    body["model"] = target_model
    normalize_body(body, provider_name,
                   anthropic_native=provider_cfg.anthropic_native)
    headers = provider_cfg.build_outbound_headers(
        dict(request.headers), api_key)
    url = f"{provider_cfg.base_url.rstrip('/')}/v1/messages/count_tokens"

    try:
        upstream_req = http_client.build_request("POST", url, json=body, headers=headers)
        r = await _send_with_retry(http_client, upstream_req)
        # _send_with_retry 恒以 stream=True 发送（forward_request 的流式
        # 转发需要）；count_tokens 是非流式语义，读全响应体后再消费——
        # 未读即 .json()/.text 会抛 httpx.ResponseNotRead（它不是
        # HTTPError 子类，会直穿成 500）。读取放在同一 try 内：读体中途
        # 的传输错误（ReadError 等）与发送失败同样走 502 塑形，失败即关流。
        try:
            await r.aread()
        except httpx.HTTPError:
            try:
                await r.aclose()
            except Exception:  # noqa: BLE001 — 关闭失败不得掩过原始流错误
                pass
            raise
    except httpx.HTTPError as e:
        error = f"{type(e).__name__}: {e}"
        _log.error("upstream_error provider=%s error=%s", provider_name, error)
        # count_tokens 无用量可记——logger=None 走纯塑形（R8-C7：此前
        # 与 make_502 的 anthropic 线格逐字手拼孪生，契约第二归宿）
        return make_502(ctx, error)

    out_headers = _lane_out_headers(r.headers, ctx, decision)
    return JSONResponse(
        content=r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text},
        status_code=r.status_code,
        headers=out_headers,
    )
