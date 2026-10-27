"""Telemetry v13 `total_ms` — the END time of a proxied request, on every emit path of both handlers.

Before v13 a row had a start (`ts`) and a first byte (`ttft_ms`) but no end: the whole time of a
request was unknown and the studio's `ms_total` was always null. `total_ms` = request arrival -> the
last byte streamed to the client, or -> the error (upstream raise, mid-stream break, client gone).

Same direct-handler pattern as test_telemetry_contract_both_handlers.py: a fake upstream, a fake
request, a recording telemetry sink. Timings use real sleeps with wide margins (no flaky bounds).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from apex_router.proxy_engine.proxy.handlers import passthrough
from apex_router.proxy_engine.proxy.handlers import shadow as shadow_h
from apex_router.proxy_engine.telemetry.events import TELEMETRY_SCHEMA_VERSION, TelemetryEvent

GAP_S = 0.15        # pause between two streamed chunks (the answer keeps streaming after the first word)
STALL_S = 0.12      # upstream stall before a raise


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
    headers = {"x-request-id": "r", "x-claude-code-session-id": "s"}
    scope = {"raw_path": b"/v1/messages", "query_string": b"",
             "headers": [(b"content-type", b"application/json")]}

    async def body(self):
        return b'{"model":"m","messages":[{"role":"user","content":"hi"}]}'


def _upstream(resp_factory=None, raise_after_s=None):
    class _Up:
        def build_url(self, k, p, q):
            return "http://up" + p

        def endpoint_id(self, client_kind):
            return "anthropic"

        async def inject_auth(self, headers, client_kind, *, raw_headers=None):
            return headers

        async def send_stream(self, m, u, *, headers, content, stats=None):
            if raise_after_s is not None:
                await asyncio.sleep(raise_after_s)
                raise httpx.ReadTimeout("upstream stalled", request=httpx.Request(m, u))
            return resp_factory()

    return _Up()


def _resp(chunks, gap_s=0.0, fail_after=None):
    """A streamed 200: `chunks` with `gap_s` between them; raise ReadError after `fail_after` chunks."""
    class _Resp:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_raw(self):
            for i, c in enumerate(chunks):
                if i:
                    await asyncio.sleep(gap_s)
                if fail_after is not None and i == fail_after:
                    raise httpx.ReadError("stream broke")
                yield c

        async def aclose(self):
            pass

    return _Resp


def _handle(handler, up, tel):
    if handler == "passthrough":
        return passthrough.handle(_Req(), up, tel)
    return shadow_h.handle(_Req(), up, tel, None)


def _drive(handler, up, *, stop_after=None):
    """Run one request; drain the body (or stop after `stop_after` chunks = the client went away)."""
    tel = _Tel()

    async def go():
        resp = await _handle(handler, up, tel)
        if hasattr(resp, "body_iterator"):
            it = resp.body_iterator
            n = 0
            try:
                async for _ in it:
                    n += 1
                    if stop_after is not None and n >= stop_after:
                        await it.aclose()        # client disconnect: the generator closes early
                        break
            except httpx.ReadError:
                pass                             # the mid-stream error reaches the server, as live
        return resp

    resp = asyncio.run(go())
    assert len(tel.ev) == 1, "exactly one telemetry row per request"
    return resp, tel.ev[0]


HANDLERS = ["passthrough", "shadow"]
SSE = [b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":3}}}\n\n',
       b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
       b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n']


def test_schema_is_v13_with_total_ms_default_none():
    assert TELEMETRY_SCHEMA_VERSION == 13
    ev = TelemetryEvent.start(apex_version="t", client="claude-code")
    assert ev.total_ms is None                                   # unset until an emit path sets it
    assert json.loads(ev.to_json())["total_ms"] is None          # the key is always in the line


@pytest.mark.parametrize("handler", HANDLERS)
def test_streamed_response_total_ms_covers_the_whole_stream(handler):
    _, ev = _drive(handler, _upstream(_resp(SSE, gap_s=GAP_S)))
    assert ev.is_error is False
    assert ev.ttft_ms > 0 and ev.total_ms is not None
    # two gaps after the first chunk: the end is at least ~2 x GAP_S after the first byte
    assert ev.total_ms - ev.ttft_ms >= 2 * GAP_S * 1000 * 0.8, (ev.ttft_ms, ev.total_ms)
    assert ev.total_ms >= ev.ttft_ms >= ev.apex_added_ms
    assert json.loads(ev.to_json())["total_ms"] == pytest.approx(ev.total_ms)


@pytest.mark.parametrize("handler", HANDLERS)
def test_midstream_error_total_ms_is_the_time_of_the_error(handler):
    _, ev = _drive(handler, _upstream(_resp(SSE, gap_s=GAP_S, fail_after=2)))
    assert ev.is_error is True and ev.error_cause == "midstream_ReadError"
    assert ev.total_ms is not None and ev.ttft_ms > 0
    assert ev.total_ms - ev.ttft_ms >= 2 * GAP_S * 1000 * 0.8    # it broke after two gaps, not at once


@pytest.mark.parametrize("handler", HANDLERS)
def test_upstream_raise_total_ms_is_the_time_of_the_raise(handler):
    resp, ev = _drive(handler, _upstream(raise_after_s=STALL_S))
    assert resp.status_code == 502 and ev.is_error is True and ev.error_cause == "ReadTimeout"
    assert ev.ttft_ms == 0.0                                     # no first byte ever came
    assert ev.total_ms is not None and ev.total_ms >= STALL_S * 1000 * 0.8
    # the whole time = apex's own part + the upstream wait (one clock, same decomposition)
    assert ev.total_ms >= ev.apex_added_ms + ev.upstream_error_wait_ms - 1.0


@pytest.mark.parametrize("handler", HANDLERS)
def test_client_disconnect_still_records_an_end_time(handler):
    _, ev = _drive(handler, _upstream(_resp(SSE, gap_s=GAP_S)), stop_after=1)
    assert ev.total_ms is not None and ev.total_ms >= ev.ttft_ms > 0
    assert ev.total_ms - ev.ttft_ms < 2 * GAP_S * 1000            # it ended at the disconnect, not later


def test_writer_line_carries_total_ms(tmp_path):
    from apex_router.proxy_engine.telemetry.events import TelemetryWriter
    w = TelemetryWriter(tmp_path / "telemetry.jsonl")
    ev = TelemetryEvent.start(apex_version="t", client="claude-code")
    ev.total_ms = 5400.5
    w.emit(ev)
    row = json.loads((tmp_path / "telemetry.jsonl").read_text().splitlines()[0])
    assert row["schema_version"] == 13 and row["total_ms"] == 5400.5
