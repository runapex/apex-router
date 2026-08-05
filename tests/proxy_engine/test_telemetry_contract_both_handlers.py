"""Telemetry-contract regression — every side-read is captured on BOTH handlers.

The contract hole has recurred THREE times: (1) agent_id + t_upstream_ttfb_ms wired into one
handler, null on the other (Step-2 hardening — pinned by test_m6b_shadow.py:430); (2) passthrough
model_requested; (3) usage/cache/content_encoding captured only by shadow.handle, DARK on
passthrough.handle — surfaced on cutover night as "the launch gates have no data on the shipping
path". A field added to one of N emitters is a contract HOLE. This pins the class: drive BOTH
handlers through the same mock upstream (carrying real usage + x-model + optional content-encoding)
and assert the FULL side-read contract on each. A future field or handler that regresses fails CI.

Uses the direct-handler-driver pattern (not the ASGI app) — the same pattern as
test_m6b_shadow.py's agent_id/endpoint_id contract check, extended to the y (usage) + cache split +
content_encoding the launch gates consume.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from apex_router.proxy_engine.proxy.handlers import passthrough
from apex_router.proxy_engine.proxy.handlers import shadow as shadow_h

_USAGE = {
    "input_tokens": 12345,
    "cache_read_input_tokens": 98000,
    "cache_creation_input_tokens": 1500,
    "output_tokens": 42,
}


def _sse_bytes() -> bytes:
    return (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":'
        + json.dumps(_USAGE).encode() + b'}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":42}}\n\n'
        b'event: message_stop\ndata: {}\n\n'
    )


def _encode(raw: bytes, content_encoding: str | None) -> bytes:
    if content_encoding in (None, "", "identity"):
        return raw
    if content_encoding == "gzip":
        import gzip
        return gzip.compress(raw)
    if content_encoding == "br":
        import brotli
        return brotli.compress(raw)
    raise AssertionError(content_encoding)


def _make_resp(content_encoding: str | None):
    body = _encode(_sse_bytes(), content_encoding)
    hdrs = {"content-type": "text/event-stream", "x-model": "claude-opus-4-8-resolved"}
    if content_encoding not in (None, "", "identity"):
        hdrs["content-encoding"] = content_encoding

    class _Resp:
        def __init__(self):
            self.status_code = 200
            self.headers = httpx.Headers(hdrs)

        async def aiter_raw(self):
            # split mid-stream so the incremental decode/line-buffer path is exercised
            half = len(body) // 2
            yield body[:half]
            yield body[half:]

        async def aclose(self):
            pass

    return _Resp


def _make_upstream(content_encoding: str | None):
    Resp = _make_resp(content_encoding)

    class _Up:
        def build_url(self, k, p, q):
            return "http://up" + p

        def endpoint_id(self, client_kind):
            return "anthropic"

        async def send_stream(self, m, u, *, headers, content):
            return Resp()

    return _Up()


class _Tel:
    def __init__(self):
        self.ev = []

    def emit(self, e):
        self.ev.append(e)


class _URL:
    path = "/v1/messages"


class _Req:
    method = "POST"
    url = _URL()
    headers = {"x-request-id": "r", "x-claude-code-session-id": "s",
               "x-claude-code-agent-id": "agent-9"}
    scope = {"raw_path": b"/v1/messages", "query_string": b"",
             "headers": [(b"content-type", b"application/json")]}

    async def body(self):
        return b'{"model":"claude-opus-4-8","messages":[{"role":"user","content":"hi"}]}'


def _drive(handler_name: str, content_encoding: str | None):
    """Run one request through the named handler; return the emitted TelemetryEvent."""
    up = _make_upstream(content_encoding)

    async def go():
        tel = _Tel()
        if handler_name == "passthrough":
            resp = await passthrough.handle(_Req(), up, tel)
        else:
            resp = await shadow_h.handle(_Req(), up, tel, None)
        async for _ in resp.body_iterator:
            pass
        return tel.ev[0]

    return asyncio.run(go())


HANDLERS = ["passthrough", "shadow"]


@pytest.mark.parametrize("handler", HANDLERS)
def test_usage_and_cache_captured_on_both_handlers(handler):
    ev = _drive(handler, None)
    assert ev.usage is not None, f"usage DARK on {handler} handler"
    assert ev.usage["input_tokens"] == _USAGE["input_tokens"]
    assert ev.cache_read_tokens == _USAGE["cache_read_input_tokens"]
    assert ev.cache_write_tokens == _USAGE["cache_creation_input_tokens"]
    assert ev.tokens_out == _USAGE["output_tokens"]


@pytest.mark.parametrize("handler", HANDLERS)
def test_content_encoding_recorded_on_both_handlers(handler):
    ev = _drive(handler, "gzip")
    assert ev.content_encoding == "gzip", f"content_encoding DARK on {handler}"


@pytest.mark.parametrize("handler", HANDLERS)
def test_gzip_usage_decoded_and_captured_on_both_handlers(handler):
    ev = _drive(handler, "gzip")
    assert ev.usage is not None, f"gzip usage must decode+capture on {handler}"
    assert ev.cache_read_tokens == _USAGE["cache_read_input_tokens"]


@pytest.mark.parametrize("handler", HANDLERS)
def test_model_resolved_from_x_model_on_both_handlers(handler):
    ev = _drive(handler, None)
    assert ev.model_resolved == "claude-opus-4-8-resolved"


@pytest.mark.parametrize("handler", HANDLERS)
def test_model_requested_parsed_from_body_on_both_handlers(handler):
    # model_requested is read from the request body `model` key. Shadow sets it; passthrough
    # referenced it (in the model_resolved fallback) but never SET it — a 4th dark field. Without
    # it, model_resolved falls back to None whenever the upstream omits x-model (the gateway does).
    ev = _drive(handler, None)
    assert ev.model_requested == "claude-opus-4-8"


@pytest.mark.parametrize("handler", HANDLERS)
def test_composition_bytes_by_class_present_on_both_handlers(handler):
    # bytes_by_class (R1's regressor X) — the shadow compute side-read. Ported to active so the
    # measurement-always-on thesis holds on the shipping path (X continues alongside y=usage).
    ev = _drive(handler, None)
    assert ev.shadow is not None, f"shadow/composition compute DARK on {handler}"
    assert "bytes_by_class" in ev.shadow


@pytest.mark.parametrize("handler", HANDLERS)
def test_shared_side_reads_present_on_both_handlers(handler):
    """The already-shared fields must stay present — a total contract so a new field can be added to
    this list and pinned across both handlers at once (the agent_id/endpoint_id lesson)."""
    ev = _drive(handler, None)
    assert ev.session_id == "s"
    assert ev.agent_id == "agent-9"
    assert ev.endpoint_id == "anthropic"
    assert ev.request_id == "r"
    assert ev.ttft_ms > 0
    assert ev.t_upstream_ttfb_ms >= 0.0
