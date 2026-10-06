"""v8: `upstream_rejected` + case-insensitive rate-limit header capture (incl. the SSE path).

Why a NEW field instead of flipping `is_error` on a 429: `is_error` is deliberately >=500-only
(events.py `error_cause` comment: "a 429 rate-limit is captured even though it does NOT set is_error";
both handlers: "the is_error>=500 rule intentionally ignores" 4xx). The doctor's error-rate panel and
`TelemetryWriter.errors` are built on that meaning. But analysis still needs to COUNT upstream
rejections without string-matching `error_cause` — live finding (2026-10-02): 478 `http_429` rows in 7
days, all `is_error=false`. `upstream_rejected` is True for ANY upstream status >= 400 (4xx and 5xx),
False on success and on the upstream-raise path (no response existed there).
"""
from __future__ import annotations

import asyncio
import json

import httpx

from apex_router.proxy_engine.proxy.handlers import passthrough, shadow
from apex_router.proxy_engine.proxy.usage import capture_error_headers
from apex_router.proxy_engine.telemetry.events import TELEMETRY_SCHEMA_VERSION, TelemetryEvent


class _URL:
    path = "/v1/messages"


class _Req:
    method = "POST"
    url = _URL()
    headers = {"x-request-id": "r", "x-claude-code-session-id": "s"}
    scope = {"raw_path": b"/v1/messages", "query_string": b"",
             "headers": [(b"content-type", b"application/json")]}

    async def body(self):
        return b'{"model":"claude-opus-5","messages":[{"role":"user","content":"hi"}]}'


class _Recorder:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class _Upstream:
    def __init__(self, status: int, chunks: list[bytes], headers):
        self._status, self._chunks, self._headers = status, chunks, headers

    def build_url(self, k, p, q):
        return "http://up" + p

    def endpoint_id(self, client_kind):
        return "anthropic"

    async def inject_auth(self, headers, client_kind, *, raw_headers=None):
        return headers

    async def send_stream(self, m, u, *, headers, content, stats=None):
        status, chunks, hdrs = self._status, self._chunks, self._headers

        class _Resp:
            status_code = status
            headers = httpx.Headers(hdrs)

            async def aiter_raw(self):
                for c in chunks:
                    yield c

            async def aclose(self):
                pass

        return _Resp()


def _drive(handler_module, upstream) -> tuple[TelemetryEvent, bytes]:
    rec = _Recorder()
    seen = bytearray()

    async def go():
        resp = await handler_module.handle(_Req(), upstream, rec, None)
        if hasattr(resp, "body_iterator"):
            async for chunk in resp.body_iterator:
                seen.extend(chunk)

    asyncio.run(go())
    assert len(rec.events) == 1
    return rec.events[0], bytes(seen)


_ERR = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "Error"}}).encode()


# ---- upstream_rejected -------------------------------------------------------------------------

def _assert_rejected_semantics(mod):
    ev, _ = _drive(mod, _Upstream(429, [_ERR], {"content-type": "application/json"}))
    assert ev.upstream_rejected is True
    assert ev.is_error is False, "is_error stays >=500-only (deliberate; doctor error panel)"
    assert ev.error_cause == "http_429"

    ev, _ = _drive(mod, _Upstream(400, [_ERR], {"content-type": "application/json"}))
    assert ev.upstream_rejected is True and ev.is_error is False

    ev, _ = _drive(mod, _Upstream(529, [_ERR], {"content-type": "application/json"}))
    assert ev.upstream_rejected is True and ev.is_error is True

    ev, _ = _drive(mod, _Upstream(200, [b"data: {}\n\n"], {"content-type": "text/event-stream"}))
    assert ev.upstream_rejected is False and ev.is_error is False


def test_passthrough_upstream_rejected():
    _assert_rejected_semantics(passthrough)


def test_shadow_upstream_rejected():
    _assert_rejected_semantics(shadow)


def test_upstream_rejected_defaults_false_and_serializes():
    ev = TelemetryEvent.start(apex_version="t", client="claude-code")
    d = json.loads(ev.to_json())
    assert d["upstream_rejected"] is False


def test_schema_bumped_for_upstream_rejected():
    # v8 introduced upstream_rejected; later bumps keep it (v9 added cache_key_rewrite).
    assert TELEMETRY_SCHEMA_VERSION >= 8


def test_doctor_accepts_schema_v8():
    from apex_router.proxy_engine.readout.doctor import SUPPORTED_SCHEMA, unsupported_schema_versions
    assert 8 in SUPPORTED_SCHEMA
    assert unsupported_schema_versions([{"schema_version": 8, "usage": None}]) == set()


# ---- header capture ----------------------------------------------------------------------------

_MIXED_CASE = [
    ("Content-Type", "application/json"),
    ("Retry-After", "17"),
    ("Anthropic-RateLimit-Tokens-Remaining", "0"),
    ("anthropic-ratelimit-unified-reset", "1759450000"),
    ("X-RateLimit-Limit-Requests", "500"),
    ("x-ratelimit-remaining-tokens", "12"),
    ("X-Should-Retry", "true"),
    ("Request-Id", "req_1"),
    ("Set-Cookie", "secret=1"),
    ("X-Unrelated", "nope"),
]
_EXPECTED = {
    "retry-after": "17",
    "anthropic-ratelimit-tokens-remaining": "0",
    "anthropic-ratelimit-unified-reset": "1759450000",
    "x-ratelimit-limit-requests": "500",
    "x-ratelimit-remaining-tokens": "12",
    "x-should-retry": "true",
    "request-id": "req_1",
}


def test_capture_error_headers_is_case_insensitive_on_plain_mapping():
    # independent of httpx's own lowercasing: a plain mixed-case mapping must match too
    assert capture_error_headers(dict(_MIXED_CASE)) == _EXPECTED


def _assert_handler_captures_allowlist(mod, status, ctype, chunks):
    hdrs = [(k, v) for k, v in _MIXED_CASE if k != "Content-Type"] + [("Content-Type", ctype)]
    ev, fwd = _drive(mod, _Upstream(status, chunks, hdrs))
    assert ev.error_detail is not None
    assert ev.error_detail["headers"] == _EXPECTED
    assert fwd == b"".join(chunks)


def test_passthrough_captures_ratelimit_headers_json():
    _assert_handler_captures_allowlist(passthrough, 429, "application/json", [_ERR])


def test_shadow_captures_ratelimit_headers_json():
    _assert_handler_captures_allowlist(shadow, 429, "application/json", [_ERR])


_SSE_ERR = [b"event: error\n", b"data: " + _ERR + b"\n\n"]


def test_passthrough_captures_ratelimit_headers_sse():
    # a streamed (text/event-stream) rejection must not skip header/body capture
    _assert_handler_captures_allowlist(passthrough, 429, "text/event-stream", _SSE_ERR)


def test_shadow_captures_ratelimit_headers_sse():
    _assert_handler_captures_allowlist(shadow, 529, "text/event-stream", _SSE_ERR)
