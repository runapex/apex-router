"""Shadow handler — passthrough emission + full-pipeline compute + provider-usage capture.

Wire-switch rung A (M6b Stage A). This is the passthrough handler with two teed side-computations
bolted on, neither of which touches the bytes on the wire:

  1. BEFORE the forward: parse a COPY of the request body, run `decide()` over the frontier under
     the loaded policy, and attach the byte-only prediction to telemetry (`event.shadow`). The body
     forwarded upstream is the ORIGINAL bytes, unchanged — shadow predicts, never rewrites.
  2. DURING the response stream: tee each forwarded chunk through a `UsageScanner` to capture the
     provider's `usage` accounting (`event.usage`), then yield the chunk onward untouched.

Together `event.shadow.bytes_by_class` (R1's X) and `event.usage.input_tokens` (R1's y) are logged
on the SAME line from request one, so the wire-usage regression calibrates immediately — the whole
reason usage capture cannot be retrofitted after a week of shadow.

Byte-identity with the M0 passthrough is preserved: same raw_path/query/header handling, same
`aiter_raw` forward, same fail-open 502. The only additions are pure side-computations wrapped in
their own try/except (a shadow-compute or usage-scan failure must never degrade the forward — the
fail-open-for-blocks / fail-closed-for-authority split: shadow is a BLOCK-side observation, so any
doubt drops the observation, never the traffic).
"""
from __future__ import annotations

import json
import time

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from apex_router.proxy_engine.config import APEX_VERSION
from apex_router.proxy_engine.pipeline.shadow import run_shadow
from apex_router.proxy_engine.policy import PolicyVersion
from apex_router.proxy_engine.proxy.handlers.passthrough import _session_id, detect_client
from apex_router.proxy_engine.proxy.upstream import Upstream, filter_request_headers, filter_response_headers
from apex_router.proxy_engine.proxy.usage import UsageScanner
from apex_router.proxy_engine.telemetry.events import TelemetryEvent, TelemetryWriter


async def handle(
    request: Request,
    upstream: Upstream,
    telemetry: TelemetryWriter,
    policy: PolicyVersion | None,
    store=None,
) -> Response:
    t0 = time.perf_counter()
    client_kind = detect_client(request)
    event = TelemetryEvent.start(apex_version=APEX_VERSION, client=client_kind)
    event.session_id = _session_id(request)
    event.agent_id = request.headers.get("x-claude-code-agent-id")  # sub-agent attribution (P0.2)
    event.request_id = request.headers.get("x-request-id")
    event.endpoint_id = upstream.endpoint_id(client_kind)  # where the wire is reached (not a const)

    body = await request.body()  # raw inbound bytes — forwarded UNCHANGED (shadow = passthrough)

    # requested vs resolved model attribution: `model_requested` is what the client ASKED for (the
    # body's `model` field, same key on both wires); `model_resolved` (set post-forward from the
    # upstream `x-model` header) is what actually served. Today they match — but logging both from
    # shadow line one is the substrate a future session-granular router's counterfactual study needs
    # (a routing decision IS requested≠resolved; data not logged now is a study you can't run).
    # Read-only over a copy: the bytes forwarded upstream are still the untouched original.
    event.model_requested = _requested_model(body)

    # §4 matcher wiring (same contract as the passthrough handler — telemetry contract: no
    # field is handler-exclusive): content-derived identity for header-less traffic; the
    # client header stays the telemetry session_id when present. Fail-open inside.
    if store is not None:
        from apex_router.proxy_engine.session.wire import identify_into_store
        ident = identify_into_store(
            body=body, client=client_kind, wire_hint=event.session_id,
            agent_id=event.agent_id, store=store,
            epoch_id=policy.policy_epoch if policy is not None else "m0",
        )
        if ident is not None:
            derived_sid, turn, mev = ident
            event.turn = turn
            event.matcher_event = mev
            if event.session_id is None:
                event.session_id = derived_sid

    # (1) Shadow compute: full pipeline decision over a COPY of the body. Byte-only, plane-clean,
    # fail-open — a parse/decide failure drops the prediction, never the request.
    try:
        report = run_shadow(body, policy)
        event.shadow = report.to_dict()
        event.stratum = _stratum_of(report)
        event.tokens_in = 0  # tokens are R1's offline job (plane separation); bytes live in .shadow
    except Exception:  # noqa: BLE001 — shadow is a block-side observation: any doubt → drop it
        event.shadow = None

    raw_path = request.scope.get("raw_path") or request.url.path.encode("latin-1")
    if isinstance(raw_path, bytes):
        raw_path = raw_path.split(b"?", 1)[0].decode("latin-1")
    url = upstream.build_url(client_kind, raw_path, request.scope.get("query_string", b""))
    raw_req_headers = request.scope.get("headers", [])
    fwd_headers = filter_request_headers(raw_req_headers)
    # Opt-in Azure-AD auth injection (strict superset): adds Authorization ONLY when enabled AND the
    # client sent none (checked on the RAW pre-filter headers); a no-op passthrough otherwise. Same
    # call the active handler makes. See upstream.inject_auth.
    fwd_headers = await upstream.inject_auth(fwd_headers, client_kind, raw_headers=raw_req_headers)
    pre_forward_ms = (time.perf_counter() - t0) * 1000.0

    t_send = time.perf_counter()  # AROUND the upstream call — for t_upstream_ttfb_ms at first byte
    try:
        response = await upstream.send_stream(
            request.method, url, headers=fwd_headers, content=body
        )
    except Exception:
        event.is_error = True
        # apex's OWN cost is pre_forward_ms (matches the success path + the field's contract); the
        # upstream wait-until-failure goes to upstream_error_wait_ms, not apex_added_ms — else a
        # 600s read-timeout is mis-billed as apex latency (the reference window finding, 42/127 errors).
        event.apex_added_ms = pre_forward_ms
        event.upstream_error_wait_ms = (time.perf_counter() - t_send) * 1000.0
        telemetry.emit(event)
        return Response(b'{"error":"apex upstream unreachable"}', status_code=502,
                        media_type="application/json")

    # model_resolved: prefer the upstream's x-model header; fall back to the client-requested model
    # when the upstream omits it (the anthropic wire returns no x-model → the field was 100% null on a measurement window
    # shadow window, blinding per-model attribution). A real x-model still wins, so a future
    # multi-endpoint world isn't masked by the guess.
    event.model_resolved = response.headers.get("x-model") or event.model_requested

    # (2) Usage capture: tee the streamed body through the scanner. content-encoding drives the
    # decoder; the forwarded chunk is always the original bytes. Record the encoding on the event so
    # a `usage=null` row is attributable to it (brotli acceptance test).
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
            event.is_error = True
            raise
        finally:
            event.apex_added_ms = pre_forward_ms
            event.is_error = event.is_error or response.status_code >= 500
            if scanner.usage.captured:
                event.usage = scanner.usage.to_dict()
                event.tokens_in = scanner.usage.input_tokens  # provider truth, not a token guess
                event.cache_read_tokens = scanner.usage.cache_read_tokens
                event.cache_write_tokens = scanner.usage.cache_creation_tokens
                event.tokens_out = scanner.usage.output_tokens
            await response.aclose()
            telemetry.emit(event)

    streamed = StreamingResponse(body_stream(), status_code=response.status_code)
    streamed.raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in filter_response_headers(response.headers)
    ]
    return streamed


def _stratum_of(report) -> str:
    """The request's context-size stratum for the top-level telemetry column (blocks carry their own
    cell keys). Falls back to 'unknown' on an empty report."""
    return report.blocks[0].stratum if report.blocks else "unknown"


def _requested_model(body: bytes) -> str | None:
    """The client-requested model from the request body (`model` key — same on the Anthropic and
    OpenAI wires). Read-only and TOTALLY fail-open: any parse failure yields None (logged as absent,
    never guessed), never mutates or re-serializes the body. The catch is broad on purpose — this
    runs on the hot path BEFORE the shadow-compute try/except, so a raise here would break the
    request itself, violating the handler's "a parse failure drops the prediction, never the
    request" contract. `json.loads` can raise more than ValueError/TypeError: a deeply-nested
    adversarial body raises RecursionError (a RuntimeError, NOT a ValueError) — so catch Exception,
    the same discipline `run_shadow`'s own except uses."""
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001 — total fail-open: any parse error → absent, never breaks traffic
        return None
    if not isinstance(obj, dict):
        return None
    model = obj.get("model")
    return model if isinstance(model, str) and model else None
