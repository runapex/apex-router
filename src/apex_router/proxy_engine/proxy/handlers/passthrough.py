"""Passthrough handler — M0 byte-identical forwarding for both wires, with SSE streaming.

This is the walking skeleton's data plane: read the inbound request, forward it unchanged to
the upstream, stream the response back verbatim. The transform pipeline (M3+) will wrap this
by rewriting `content` before the forward and the response is untouched. Byte-identity here is
the M0 acceptance gate, so this path does NOT parse or re-serialize the body — it forwards raw
bytes.
"""
from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from apex_router.proxy_engine.config import APEX_VERSION
from apex_router.proxy_engine.proxy.upstream import Upstream, filter_request_headers, filter_response_headers
from apex_router.proxy_engine.proxy.usage import UsageScanner
from apex_router.proxy_engine.telemetry.events import TelemetryEvent, TelemetryWriter

_ANTHROPIC_ENDPOINTS = ("/messages",)
_OPENAI_ENDPOINTS = ("/chat/completions", "/completions", "/responses")


def detect_client(request: Request) -> str:
    """claude-code vs codex. Route by PATH first (authoritative), UA/headers as tiebreak
    (xval #10: a Codex-compatible client without 'codex' in its UA must still route right).

    The Anthropic wire ends in `/messages`; the OpenAI/Codex wire ends in `/chat/completions`,
    `/completions`, `/responses`. Matched as a path SEGMENT SUFFIX, not a `/v1/` prefix, because the
    real deployments carry a service prefix: Codex→a gateway `/<your-openai-path>` (2026-07-17
    wiring) and Anthropic→`/claude/v1/messages`, neither of which starts with `/v1/`. The suffix is
    the deterministic signal; headers only disambiguate an ambiguous non-endpoint probe (e.g. root,
    `/v1/models`). `/completions` is checked so that `/chat/completions` (which also ends in it) is
    caught either way — both are the OpenAI wire.
    """
    path = request.url.path
    # Anthropic FIRST: a path ending in /messages is the Anthropic wire even if it also contains an
    # openai-looking prefix (it won't, but ordering makes the intent explicit and future-proof).
    if path.endswith(_ANTHROPIC_ENDPOINTS):
        return "claude-code"
    if path.endswith(_OPENAI_ENDPOINTS):
        return "codex"
    h = request.headers
    if "x-claude-code-session-id" in h or h.get("anthropic-version"):
        return "claude-code"
    ua = h.get("user-agent", "").lower()
    if "codex" in ua or "openai" in ua:
        return "codex"
    return "claude-code"  # dominant traffic; Anthropic wire


def _session_id(request: Request) -> str | None:
    return request.headers.get("x-claude-code-session-id")


def _requested_model(body: bytes) -> str | None:
    """The client-requested model from the request body `model` key (same on both wires). Read-only,
    TOTALLY fail-open: any parse failure → None (logged as absent, never guessed), never mutates the
    body. Broad catch on purpose — this runs before the compute try/except, and a raise here would
    break the request. Mirrors shadow's `_requested_model` (kept local to avoid a passthrough↔shadow
    import cycle: shadow already imports detect_client/_session_id from here)."""
    import json as _json
    try:
        obj = _json.loads(body)
    except Exception:  # noqa: BLE001 — total fail-open: any parse error → absent, never breaks traffic
        return None
    if not isinstance(obj, dict):
        return None
    model = obj.get("model")
    return model if isinstance(model, str) and model else None


async def handle(
    request: Request, upstream: Upstream, telemetry: TelemetryWriter, policy=None
) -> Response:
    t0 = time.perf_counter()
    client_kind = detect_client(request)
    event = TelemetryEvent.start(apex_version=APEX_VERSION, client=client_kind)
    event.session_id = _session_id(request)
    event.agent_id = request.headers.get("x-claude-code-agent-id")  # sub-agent attribution (P0.2)
    event.request_id = request.headers.get("x-request-id")
    event.endpoint_id = upstream.endpoint_id(client_kind)  # where the wire is reached (not a const)

    body = await request.body()  # raw inbound bytes — forwarded unchanged in M0
    event.model_requested = _requested_model(body)  # body `model` key (fail-open)
    event.tokens_in = 0  # token accounting lands in M3 (needs body parse); M0 stays byte-pure

    # Composition side-read (R1's regressor X): the SAME byte-only shadow compute the shadow handler
    # runs, ported so the active (shipping) path keeps X flowing alongside y=usage — measurement is
    # always-on, not shadow-only (telemetry-contract: no side-read is handler-exclusive). Fail-open:
    # a parse/decide failure drops the prediction, never the request. Runs over a COPY; forwarded
    # bytes are the untouched original. NOTE: this makes active mode do decompose work (~ shadow's
    # per-request cost, within the G3 latency gate) — active is no longer bare M0 passthrough.
    from apex_router.proxy_engine.pipeline.shadow import run_shadow
    try:
        report = run_shadow(body, policy)
        event.shadow = report.to_dict()
        event.stratum = report.blocks[0].stratum if report.blocks else "unknown"
    except Exception:  # noqa: BLE001 — block-side observation: any doubt → drop the prediction
        event.shadow = None

    # Build the upstream target from the RAW ASGI scope: raw_path + query_string preserve
    # percent-encoding and `?query` params byte-for-byte (xval #1/#2). Raw header list
    # preserves duplicates/casing and blocks httpx default injection (xval #3/#5).
    raw_path = request.scope.get("raw_path") or request.url.path.encode("latin-1")
    if isinstance(raw_path, bytes):
        raw_path = raw_path.split(b"?", 1)[0].decode("latin-1")
    url = upstream.build_url(client_kind, raw_path, request.scope.get("query_string", b""))
    fwd_headers = filter_request_headers(request.scope.get("headers", []))

    # Time apex's own request-path work (detect + header filter + body read) BEFORE the
    # upstream forward. Named pre_forward, not "overhead": it excludes connect/pool/upstream
    # wait, which are not apex's cost (xval #9).
    pre_forward_ms = (time.perf_counter() - t0) * 1000.0

    t_send = time.perf_counter()  # AROUND the upstream call — for t_upstream_ttfb_ms at first byte
    try:
        response = await upstream.send_stream(
            request.method, url, headers=fwd_headers, content=body
        )
    except Exception:
        event.is_error = True
        # apex's OWN cost is pre_forward_ms (matches the success path + the field's contract); the
        # time blocked on the upstream before it raised goes to upstream_error_wait_ms, NOT to
        # apex_added_ms — else a 600s read-timeout is mis-billed as apex latency (2026-07-19).
        event.apex_added_ms = pre_forward_ms
        event.upstream_error_wait_ms = (time.perf_counter() - t_send) * 1000.0
        telemetry.emit(event)
        # fail-open: a 502 the client can retry, never a hang
        return Response(b'{"error":"apex upstream unreachable"}', status_code=502,
                        media_type="application/json")

    # model_resolved: x-model header, or fall back to the client-requested model (the Anthropic gateway omits
    # x-model → the field was 100% null on the shadow window). Same as the shadow handler.
    event.model_resolved = response.headers.get("x-model") or event.model_requested

    # Usage capture — the SAME teed side-read the shadow handler runs, ported here so the shipping
    # (active) path emits the gates' inputs (usage/cache split/content_encoding), not just timing.
    # The telemetry contract must hold on EVERY handler (agent_id lesson, 3rd recurrence); pinned by
    # test_telemetry_contract_both_handlers. The scanner tees a COPY of each chunk through an
    # incremental decoder; the forwarded chunk is always the original bytes (fail-open inside — a
    # decode/parse error drops the observation, never the traffic). Byte-identity is unaffected.
    content_encoding = response.headers.get("content-encoding", "")
    event.content_encoding = content_encoding or None
    scanner = UsageScanner(content_encoding)

    async def body_stream():
        first = True
        try:
            async for chunk in response.aiter_raw():
                if first:
                    now = time.perf_counter()
                    event.ttft_ms = (now - t0) * 1000.0  # arrival → first byte to client
                    event.t_upstream_ttfb_ms = (now - t_send) * 1000.0  # send → upstream first byte
                    first = False
                scanner.feed(chunk)  # copy-scan; never raises (fail-open inside)
                yield chunk  # forward the ORIGINAL bytes, unchanged
        except Exception:
            event.is_error = True  # client disconnect / upstream stream error (xval #8)
            raise
        finally:
            event.apex_added_ms = pre_forward_ms  # apex's own request-path cost
            event.is_error = event.is_error or response.status_code >= 500
            if scanner.usage.captured:
                event.usage = scanner.usage.to_dict()
                event.tokens_in = scanner.usage.input_tokens  # provider truth, not a token guess
                event.cache_read_tokens = scanner.usage.cache_read_tokens
                event.cache_write_tokens = scanner.usage.cache_creation_tokens
                event.tokens_out = scanner.usage.output_tokens
            await response.aclose()  # release the upstream connection BEFORE sync telemetry I/O
            telemetry.emit(event)

    streamed = StreamingResponse(body_stream(), status_code=response.status_code)
    # Assign raw header pairs directly (preserves duplicates like set-cookie; dict() would
    # collapse them). StreamingResponse seeds Content-Type from media_type=None → we override.
    streamed.raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in filter_response_headers(response.headers)
    ]
    return streamed
