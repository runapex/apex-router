"""Upstream error bodies must be CAPTURED, not discarded — the missing half of `error_cause` (v7).

Live finding (2026-09-30): 247 rows logged `error_cause="http_429"` in a single day, all of them the
Claude Code auto-mode Bash-safety classifier, and telemetry could not say WHICH limit was hit. v5 gave
the error's mechanism (`http_429` vs `PoolTimeout` vs `ReadTimeout`) — provable, but not actionable:
`http_429` alone does not distinguish an input-token-per-minute ceiling from a request-per-minute
ceiling from a subscription session cap, and those have different fixes. The provider says exactly
which one in the response body (`error.type` / `error.message`) and in its `anthropic-ratelimit-*` /
`retry-after` headers — and apex was throwing both away: `UsageScanner` only tees the body looking for
`usage`, and on a 4xx/5xx there is no usage, so `captured` stays False and every byte is dropped.

This is the same class of gap as v5 itself (an error you can see but not diagnose) and as v3's
`content_encoding` (a null field with no attributable cause). A measuring proxy that records that a
request failed, but not why the provider said it failed, forces the operator to reproduce the failure
by hand with curl — which is precisely what the telemetry exists to make unnecessary.

Pinned here: for any response with status >= 400, the event carries `error_detail` with
  - `body`:    the decoded error body, BOUNDED (never unbounded buffering on a hostile/huge body)
  - `headers`: the rate-limit/retry headers the provider used to explain itself
and the forwarded bytes stay byte-identical. Clean 2xx rows keep `error_detail = None`.
"""
from __future__ import annotations

import asyncio
import gzip
import json

import httpx

from apex_router.proxy_engine.proxy.handlers import passthrough, shadow
from apex_router.proxy_engine.telemetry.events import TelemetryEvent

# A real Anthropic rate-limit envelope — the shape whose `error.type` distinguishes the limits.
_ERROR_BODY = json.dumps({
    "type": "error",
    "error": {"type": "rate_limit_error",
              "message": "This request would exceed your organization's rate limit."},
}).encode()

_RATE_HEADERS = {
    "content-type": "application/json",
    "content-encoding": "gzip",
    "retry-after": "31",
    "anthropic-ratelimit-requests-remaining": "0",
    "anthropic-ratelimit-requests-reset": "2026-09-30T15:30:00Z",
    "request-id": "req_abc123",
}


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
    """Fake upstream returning an arbitrary status + gzip body, in two chunks (so the capture must
    survive chunk boundaries the way the real streamed wire delivers them)."""

    def __init__(self, status: int, raw_body: bytes, headers: dict):
        self._status, self._raw, self._headers = status, raw_body, headers

    def build_url(self, k, p, q):
        return "http://up" + p

    def endpoint_id(self, client_kind):
        return "anthropic"

    async def inject_auth(self, headers, client_kind, *, raw_headers=None):
        return headers

    async def send_stream(self, m, u, *, headers, content, stats=None):
        status, raw, hdrs = self._status, self._raw, self._headers

        class _Resp:
            status_code = status
            headers = httpx.Headers(hdrs)

            async def aiter_raw(self):
                mid = max(1, len(raw) // 2)
                yield raw[:mid]
                yield raw[mid:]

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


def _assert_captures_rate_limit_detail(handler_module):
    raw = gzip.compress(_ERROR_BODY)
    ev, forwarded = _drive(handler_module, _Upstream(429, raw, _RATE_HEADERS))

    # the v5 mechanism label is unchanged …
    assert ev.error_cause == "http_429"
    # … and v7 adds the provider's own explanation.
    assert ev.error_detail is not None, "a >=400 response must capture the provider's error detail"
    assert "rate_limit_error" in ev.error_detail["body"], (
        f"error body not captured: {ev.error_detail['body']!r}"
    )
    hdrs = ev.error_detail["headers"]
    assert hdrs.get("retry-after") == "31"
    assert hdrs.get("anthropic-ratelimit-requests-remaining") == "0"
    assert hdrs.get("request-id") == "req_abc123"
    # capture is a COPY — the client still receives the exact upstream bytes.
    assert forwarded == raw


def test_passthrough_captures_rate_limit_detail():
    _assert_captures_rate_limit_detail(passthrough)


def test_shadow_captures_rate_limit_detail():
    _assert_captures_rate_limit_detail(shadow)


def _assert_clean_response_has_no_detail(handler_module):
    body = b'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
    ev, forwarded = _drive(handler_module, _Upstream(200, body, {"content-type": "text/event-stream"}))
    assert ev.error_cause is None
    assert ev.error_detail is None, "a clean 2xx must not carry an error_detail"
    assert forwarded == body


def test_passthrough_clean_response_has_no_detail():
    _assert_clean_response_has_no_detail(passthrough)


def test_shadow_clean_response_has_no_detail():
    _assert_clean_response_has_no_detail(shadow)


def _assert_error_body_is_bounded(handler_module):
    """A hostile/huge error body must not be buffered in full into telemetry (unbounded memory on
    the data plane, and a telemetry line that dwarfs every other row)."""
    huge = b'{"type":"error","error":{"message":"' + (b"x" * 200_000) + b'"}}'
    ev, forwarded = _drive(handler_module, _Upstream(500, huge, {"content-type": "application/json"}))
    assert ev.error_detail is not None
    assert len(ev.error_detail["body"]) <= 1024, (
        f"error body capture is unbounded: {len(ev.error_detail['body'])} chars"
    )
    assert forwarded == huge  # still forwarded in full, untouched


def test_passthrough_error_body_is_bounded():
    _assert_error_body_is_bounded(passthrough)


def test_shadow_error_body_is_bounded():
    _assert_error_body_is_bounded(shadow)
