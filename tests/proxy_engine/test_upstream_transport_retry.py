"""Transient transport-error retry BEFORE the first byte reaches the client (extends Finding 4).

Live finding (2026-10-02, ~/.apex/telemetry.jsonl, 7 days): 402 `SSLError` + 278 `ReadError` rows on
the anthropic endpoint, all `connect_retries=0`, ~157 s of upstream_error_wait_ms in total (~230 ms
each — fast failures, the signature of a dead pooled keep-alive connection). The connect-only retry
(ConnectError/ConnectTimeout) never fired for them: a raw `ssl.SSLError` (anyio re-raises non-EOF TLS
errors unmapped, httpcore's read/write exc_map doesn't cover it) and `httpx.ReadError` were excluded.

Contract pinned here:
  - SSLError / ReadError / WriteError / RemoteProtocolError raised by `send_stream` (i.e. before
    response headers, before the handler has yielded ANY byte to the client) are retried with the SAME
    bounded, jittered backoff as connect errors, on BOTH wires — but ONLY for a fast failure
    (< APEX_RETRY_FAST_FAIL_MS) on a POST/GET completion endpoint (review U1/U2, tests at the bottom);
  - ConnectError / ConnectTimeout (never wrote the request) stay retried on any method/path;
  - ReadTimeout / PoolTimeout are NOT retried (a 600 s read-timeout x3 would be a 30-minute hang);
  - an error AFTER the first body byte reached the client is NEVER retried (the handler cannot
    un-send bytes; the client owns that retry);
  - attempts land in `connect_retries`, the slept backoff in `connect_backoff_ms`.
"""
from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest

from apex_router.proxy_engine.config import Config
from apex_router.proxy_engine.proxy.handlers import passthrough, shadow
from apex_router.proxy_engine.proxy.upstream import Upstream


def _ssl_error():
    return ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac")


_TRANSIENT = [
    pytest.param(lambda req: _ssl_error(), "SSLError", id="ssl"),
    pytest.param(lambda req: httpx.ReadError("reset", request=req), "ReadError", id="read"),
    pytest.param(lambda req: httpx.WriteError("broken pipe", request=req), "WriteError", id="write"),
    pytest.param(lambda req: httpx.RemoteProtocolError("server disconnected", request=req),
                 "RemoteProtocolError", id="remote-protocol"),
]


def _upstream(tmp_path, handler, *, retries=2, backoff=0.0):
    cfg = Config(home=tmp_path, upstream_connect_retries=retries, upstream_connect_backoff_s=backoff)
    up = Upstream(cfg)
    up._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return up


# ---- send_stream level ---------------------------------------------------------------------------

@pytest.mark.parametrize("make_exc,_name", _TRANSIENT)
def test_transient_error_retried_then_ok(tmp_path, make_exc, _name):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise make_exc(request)
        return httpx.Response(200, content=b"ok")

    async def run():
        up = _upstream(tmp_path, handler)
        stats: dict = {}
        r = await up.send_stream("POST", "http://x/v1/messages", headers=[(b"host", b"x")],
                                 content=b"{}", stats=stats)
        body = await r.aread()
        await r.aclose()
        await up.aclose()
        return body, stats

    body, stats = asyncio.run(run())
    assert body == b"ok"
    assert calls["n"] == 3
    assert stats["connect_retries"] == 2


@pytest.mark.parametrize("make_exc,name", _TRANSIENT)
def test_transient_error_exhausts_and_raises_original(tmp_path, make_exc, name):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise make_exc(request)

    async def run():
        up = _upstream(tmp_path, handler)
        stats: dict = {}
        try:
            await up.send_stream("POST", "http://x/v1/messages", headers=[(b"host", b"x")],
                                 content=b"{}", stats=stats)
            return None, stats
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__, stats
        finally:
            await up.aclose()

    raised, stats = asyncio.run(run())
    assert raised == name, "the ORIGINAL exception class must propagate (error_cause stays honest)"
    assert calls["n"] == 3
    assert stats["connect_retries"] == 2


@pytest.mark.parametrize("exc_cls", [httpx.ReadTimeout, httpx.PoolTimeout])
def test_timeouts_are_not_retried(tmp_path, exc_cls):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise exc_cls("slow", request=request)

    async def run():
        up = _upstream(tmp_path, handler)
        try:
            await up.send_stream("POST", "http://x", headers=[(b"host", b"x")], content=b"{}")
        except exc_cls:
            pass
        finally:
            await up.aclose()

    asyncio.run(run())
    assert calls["n"] == 1


# ---- handler level (telemetry + no-retry-after-first-byte) ---------------------------------------

class _URL:
    def __init__(self, path):
        self.path = path


def _req(path=b"/v1/messages"):
    class _Req:
        method = "POST"
        url = _URL(path.decode())
        headers = {"x-request-id": "r", "x-claude-code-session-id": "s"}
        scope = {"raw_path": path, "query_string": b"",
                 "headers": [(b"content-type", b"application/json")]}

        async def body(self):
            return b'{"model":"m","messages":[{"role":"user","content":"hi"}]}'

    return _Req()


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        pass


class _Recorder:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _drive(mod, up, path=b"/v1/messages"):
    rec = _Recorder()
    seen = bytearray()
    raised = {"exc": None}

    async def go():
        resp = await mod.handle(_req(path), up, rec, None)
        try:
            if hasattr(resp, "body_iterator"):
                async for chunk in resp.body_iterator:
                    seen.extend(chunk)
            else:
                seen.extend(resp.body)
        except Exception as exc:  # noqa: BLE001
            raised["exc"] = exc
        finally:
            await up.aclose()
        return resp.status_code

    status = asyncio.run(go())
    assert len(rec.events) == 1
    return rec.events[0], status, bytes(seen), raised["exc"]


@pytest.mark.parametrize("mod", [passthrough, shadow], ids=["passthrough", "shadow"])
@pytest.mark.parametrize("path", [b"/v1/messages", b"/v1/responses"], ids=["anthropic", "openai"])
def test_handler_retried_then_ok_records_retries(tmp_path, mod, path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _ssl_error()
        # an un-read async stream (Response(content=...) pre-reads, so aiter_raw would raise
        # StreamConsumed — the real wire is a lazy stream)
        return httpx.Response(200, stream=_Chunks([b"data: {}\n\n"]),
                              headers={"content-type": "text/event-stream"})

    up = _upstream(tmp_path, handler, retries=2, backoff=0.002)
    ev, status, body, exc = _drive(mod, up, path)
    assert exc is None and status == 200 and body == b"data: {}\n\n"
    assert calls["n"] == 2
    assert ev.connect_retries == 1
    assert ev.connect_backoff_ms > 0.0
    assert ev.is_error is False and ev.error_cause is None


@pytest.mark.parametrize("mod", [passthrough, shadow], ids=["passthrough", "shadow"])
def test_handler_retried_then_exhausted(tmp_path, mod):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadError("reset", request=request)

    up = _upstream(tmp_path, handler, retries=2, backoff=0.002)
    ev, status, _, _ = _drive(mod, up)
    assert status == 502
    assert calls["n"] == 3
    assert ev.is_error is True
    assert ev.error_cause == "ReadError"
    assert ev.connect_retries == 2
    assert ev.connect_backoff_ms > 0.0
    assert ev.upstream_rejected is False  # no upstream response existed


class _CutAfterFirstChunk(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"event: message_start\ndata: {}\n\n"
        raise httpx.ReadError("cut mid-stream")

    async def aclose(self):
        pass


@pytest.mark.parametrize("mod", [passthrough, shadow], ids=["passthrough", "shadow"])
def test_handler_never_retries_after_first_byte(tmp_path, mod):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, stream=_CutAfterFirstChunk(),
                              headers={"content-type": "text/event-stream"})

    up = _upstream(tmp_path, handler, retries=2, backoff=0.0)
    ev, status, body, exc = _drive(mod, up)
    assert body == b"event: message_start\ndata: {}\n\n"  # client already got bytes
    assert isinstance(exc, httpx.ReadError)  # propagated to the client, not swallowed/retried
    assert calls["n"] == 1, "a request whose bytes reached the client must NEVER be retried"
    assert ev.connect_retries == 0
    assert ev.is_error is True


# ---- review U1/U2: body-sent retries are fast-fail-only and completion-endpoint-only ------------
#
# ConnectError/ConnectTimeout never wrote the request, so they stay retried on ANY method/path. The
# body-sent classes (SSLError/ReadError/WriteError/RemoteProtocolError) MAY follow a written request:
# they are retried only when (a) the failed attempt took < APEX_RETRY_FAST_FAIL_MS (default 3000 ms),
# (b) the path is a completion endpoint (/v1/messages, /v1/chat/completions, /responses; never
# /batches or /files), and (c) the method is POST or GET.

import apex_router.proxy_engine.proxy.upstream as _um  # noqa: E402


class _FakeClock:
    """Each call advances by `step` seconds — so one attempt (2 reads) measures exactly `step` s."""

    def __init__(self, step):
        self.t = 1000.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def _count_attempts(tmp_path, make_exc, *, method="POST", url="http://x/v1/messages", retries=2):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise make_exc(request)

    async def run():
        up = _upstream(tmp_path, handler, retries=retries)
        stats: dict = {}
        try:
            await up.send_stream(method, url, headers=[(b"host", b"x")], content=b"{}", stats=stats)
            raised = None
        except Exception as exc:  # noqa: BLE001
            raised = type(exc).__name__
        finally:
            await up.aclose()
        return raised, stats

    raised, stats = asyncio.run(run())
    return calls["n"], raised, stats


def test_slow_read_error_is_not_retried(tmp_path, monkeypatch):
    # U1: a reset after minutes of upstream generation must NOT be replayed (3x tokens + latency).
    monkeypatch.setattr(_um, "_attempt_clock", _FakeClock(step=180.0))
    n, raised, stats = _count_attempts(tmp_path, lambda r: httpx.ReadError("reset", request=r))
    assert n == 1
    assert raised == "ReadError"  # original class propagates -> handler books error_cause
    assert stats.get("connect_retries", 0) == 0


def test_read_error_just_over_custom_guard_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_RETRY_FAST_FAIL_MS", "500")
    monkeypatch.setattr(_um, "_attempt_clock", _FakeClock(step=0.6))
    n, _, _ = _count_attempts(tmp_path, lambda r: httpx.ReadError("reset", request=r))
    assert n == 1


def test_fast_read_error_on_messages_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(_um, "_attempt_clock", _FakeClock(step=0.23))
    n, raised, stats = _count_attempts(tmp_path, lambda r: httpx.ReadError("reset", request=r))
    assert n == 3 and raised == "ReadError"
    assert stats["connect_retries"] == 2


@pytest.mark.parametrize("url", ["http://x/v1/chat/completions", "http://x/v1/responses",
                                 "http://x/backend-api/codex/responses"])
def test_fast_read_error_on_other_completion_endpoints_is_retried(tmp_path, monkeypatch, url):
    monkeypatch.setattr(_um, "_attempt_clock", _FakeClock(step=0.23))
    n, _, _ = _count_attempts(tmp_path, lambda r: httpx.ReadError("reset", request=r), url=url)
    assert n == 3


def test_fast_read_error_on_batches_is_not_retried(tmp_path, monkeypatch):
    # U2: /v1/messages/batches creates server-side state — a replay could double-create a batch.
    monkeypatch.setattr(_um, "_attempt_clock", _FakeClock(step=0.23))
    n, raised, _ = _count_attempts(tmp_path, lambda r: httpx.ReadError("reset", request=r),
                                   url="http://x/v1/messages/batches")
    assert n == 1 and raised == "ReadError"


def test_fast_ssl_error_on_files_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(_um, "_attempt_clock", _FakeClock(step=0.23))
    n, raised, _ = _count_attempts(tmp_path, lambda r: _ssl_error(), url="http://x/v1/files")
    assert n == 1 and raised == "SSLError"


@pytest.mark.parametrize("method", ["DELETE", "PATCH"])
def test_fast_read_error_non_post_get_method_is_not_retried(tmp_path, monkeypatch, method):
    monkeypatch.setattr(_um, "_attempt_clock", _FakeClock(step=0.23))
    n, _, _ = _count_attempts(tmp_path, lambda r: httpx.ReadError("reset", request=r),
                              method=method)
    assert n == 1


def test_connect_error_on_batches_is_still_retried(tmp_path, monkeypatch):
    # ConnectError never sent a body -> no double-submit risk -> retried on ANY path, even slow.
    monkeypatch.setattr(_um, "_attempt_clock", _FakeClock(step=180.0))
    n, raised, stats = _count_attempts(tmp_path, lambda r: httpx.ConnectError("boom", request=r),
                                       url="http://x/v1/messages/batches")
    assert n == 3 and raised == "ConnectError"
    assert stats["connect_retries"] == 2


@pytest.mark.parametrize("method", ["DELETE", "PATCH"])
def test_connect_timeout_any_method_is_still_retried(tmp_path, method):
    n, _, _ = _count_attempts(tmp_path, lambda r: httpx.ConnectTimeout("slow", request=r),
                              method=method, url="http://x/v1/files")
    assert n == 3


def test_handler_slow_read_error_not_retried_records_error_cause(tmp_path, monkeypatch):
    monkeypatch.setattr(_um, "_attempt_clock", _FakeClock(step=180.0))
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadError("reset", request=request)

    up = _upstream(tmp_path, handler, retries=2, backoff=0.0)
    ev, status, _, _ = _drive(passthrough, up)
    assert status == 502 and calls["n"] == 1
    assert ev.error_cause == "ReadError" and ev.connect_retries == 0
