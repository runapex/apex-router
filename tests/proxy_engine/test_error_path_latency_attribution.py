"""Error-path latency attribution — `apex_added_ms` must mean what its name says on EVERY path.

Live-telemetry finding (the reference window): 42 of 127 upstream errors recorded `apex_added_ms ≈ 600_000`
— the full 600s read-timeout wait billed to apex, though apex added ~30ms of real work and spent the
rest BLOCKED on the upstream. Same class as the doctor wire-asymmetry bug: a field whose recorded
value contradicts its name. The schema already documents the contract (events.py: apex_added_ms is
"apex's own request-path cost"; pre_forward is "named pre_forward, not overhead: it excludes ...
upstream wait, which are not apex's cost, xval #9") — the SUCCESS path honors it (apex_added_ms =
pre_forward_ms), the error-raise path did not.

Fix (pinned here): on the `send_stream` raise path, apex_added_ms = apex's own pre-forward cost, and
the upstream wait-until-failure is recorded SEPARATELY in `upstream_error_wait_ms` (0 on success), so
a 600s-timeout row is attributable to the upstream, not to apex — and a latency panel can't be misled
into charting a 600s apex tail.
"""
from __future__ import annotations

import asyncio

import httpx

from apex_router.proxy_engine.proxy.handlers import passthrough, shadow
from apex_router.proxy_engine.telemetry.events import TelemetryEvent

_UPSTREAM_STALL_S = 0.20  # the fake upstream blocks this long, THEN raises (a timeout in miniature)


class _SlowBoom:
    """Upstream that BLOCKS for _UPSTREAM_STALL_S then fails the forward — the small-scale shape of
    the live 600s read-timeout. apex's own cost is ~ms; the stall is the upstream's, not apex's."""

    def build_url(self, k, p, q):
        return "http://up" + p

    def endpoint_id(self, client_kind):
        return "anthropic"

    async def inject_auth(self, headers, client_kind, *, raw_headers=None):
        return headers  # injection disabled by default → passthrough no-op

    async def send_stream(self, m, u, *, headers, content, stats=None):
        await asyncio.sleep(_UPSTREAM_STALL_S)
        raise httpx.ConnectError("upstream unreachable", request=httpx.Request(m, u))


class _URL:
    path = "/v1/messages"


class _Req:
    method = "POST"
    url = _URL()
    headers = {"x-request-id": "r", "x-claude-code-session-id": "s"}
    scope = {"raw_path": b"/v1/messages", "query_string": b"",
             "headers": [(b"content-type", b"application/json")]}

    async def body(self):
        return b'{"model":"m","messages":[{"role":"user","content":"hi"}]}'


class _Recorder:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _drive(handler_module) -> TelemetryEvent:
    rec = _Recorder()

    async def go():
        resp = await handler_module.handle(_Req(), _SlowBoom(), rec, None)
        # drain any streamed body (error path returns a plain 502 Response)
        if hasattr(resp, "body_iterator"):
            async for _ in resp.body_iterator:
                pass
        return resp

    resp = asyncio.run(go())
    assert resp.status_code == 502
    assert len(rec.events) == 1
    return rec.events[0]


def _assert_error_attribution(ev: TelemetryEvent):
    stall_ms = _UPSTREAM_STALL_S * 1000.0
    assert ev.is_error is True
    # apex_added_ms is apex's OWN cost — it must NOT include the upstream stall.
    assert ev.apex_added_ms < stall_ms * 0.5, (
        f"apex_added_ms={ev.apex_added_ms:.1f}ms wrongly includes the {stall_ms:.0f}ms upstream stall"
    )
    # the upstream wait-until-failure is captured SEPARATELY and reflects the stall.
    assert ev.upstream_error_wait_ms >= stall_ms * 0.8, (
        f"upstream_error_wait_ms={ev.upstream_error_wait_ms:.1f}ms must capture the upstream stall"
    )
    # ttfb stays 0 — no first byte ever arrived (its documented invariant).
    assert ev.t_upstream_ttfb_ms == 0.0


def test_passthrough_error_path_attributes_wait_to_upstream_not_apex():
    _assert_error_attribution(_drive(passthrough))


def test_shadow_error_path_attributes_wait_to_upstream_not_apex():
    _assert_error_attribution(_drive(shadow))


# --- F4: connect-retry backoff must be billed to apex, not upstream (xval) ---

_BACKOFF_S = 0.15


class _BackoffThenOk:
    """Upstream that reports a connect-retry backoff via stats, then returns a normal 200 stream.
    The backoff sleep happens INSIDE send_stream (which the handler times as the upstream window),
    so the handler must subtract it from upstream TTFB and add it to apex_added_ms (xval F4)."""

    def build_url(self, k, p, q):
        return "http://up" + p

    def endpoint_id(self, client_kind):
        return "anthropic"

    async def inject_auth(self, headers, client_kind, *, raw_headers=None):
        return headers

    async def send_stream(self, m, u, *, headers, content, stats=None):
        # Simulate a connect failure recovered after one backoff sleep: actually sleep so the
        # handler's wall-clock upstream window really contains the backoff we then attribute away.
        await asyncio.sleep(_BACKOFF_S)
        if stats is not None:
            stats["connect_backoff_s"] = _BACKOFF_S
            stats["connect_retries"] = 1

        class _Resp:
            status_code = 200
            headers = httpx.Headers({"content-type": "text/plain"})

            async def aiter_raw(self):
                yield b"hello"

            async def aclose(self):
                pass

        return _Resp()


def _drive_ok(handler_module) -> TelemetryEvent:
    rec = _Recorder()

    async def go():
        resp = await handler_module.handle(_Req(), _BackoffThenOk(), rec, None)
        if hasattr(resp, "body_iterator"):
            async for _ in resp.body_iterator:
                pass

    asyncio.run(go())
    assert len(rec.events) == 1
    return rec.events[0]


def _assert_backoff_billed_to_apex(ev: TelemetryEvent):
    backoff_ms = _BACKOFF_S * 1000.0
    # apex CHOSE to sleep the backoff → it is apex-added latency, not upstream latency.
    assert ev.apex_added_ms >= backoff_ms * 0.8, (
        f"apex_added_ms={ev.apex_added_ms:.1f}ms must include the {backoff_ms:.0f}ms connect backoff"
    )
    # the upstream first-byte window must NOT include the backoff apex slept.
    assert ev.t_upstream_ttfb_ms < backoff_ms * 0.5, (
        f"t_upstream_ttfb_ms={ev.t_upstream_ttfb_ms:.1f}ms wrongly includes apex's backoff sleep"
    )
    # client-observed ttft legitimately includes the backoff (the client really waited).
    assert ev.ttft_ms >= backoff_ms * 0.8


def test_passthrough_bills_connect_backoff_to_apex_not_upstream():
    _assert_backoff_billed_to_apex(_drive_ok(passthrough))


def test_shadow_bills_connect_backoff_to_apex_not_upstream():
    _assert_backoff_billed_to_apex(_drive_ok(shadow))
