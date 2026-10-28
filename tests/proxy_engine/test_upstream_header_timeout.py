"""D2 (owner 2026-10-28): a limit on the time to the upstream response headers (APEX_HEADER_TIMEOUT, 180 s).

A stalled upstream (no header) now fails at the limit as HeaderTimeout (a ReadTimeout), not after 600 s.
A slow BODY is not cut: the limit ends when the headers arrive. Timeouts are never retried in the proxy.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from apex_router.proxy_engine.config import Config
from apex_router.proxy_engine.proxy import upstream as up_mod
from apex_router.proxy_engine.proxy.upstream import HeaderTimeout, Upstream


def _upstream(cfg, handler):
    up = Upstream(cfg)
    up._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return up


def _send(cfg, handler):
    async def run():
        up = _upstream(cfg, handler)
        t0 = time.monotonic()
        try:
            r = await up.send_stream("POST", "http://x/v1/messages", headers=[(b"host", b"x")], content=b"{}")
            body = await r.aread()
            await r.aclose()
            return "ok", body, time.monotonic() - t0
        except Exception as e:  # noqa: BLE001
            return type(e).__name__, e, time.monotonic() - t0
        finally:
            await up.aclose()
    return asyncio.run(run())


def test_a_stalled_upstream_fails_at_the_header_limit_not_the_read_limit(tmp_path):
    cfg = Config(home=tmp_path, upstream_header_timeout_s=1.0, upstream_read_timeout_s=600,
                 upstream_connect_retries=2, upstream_connect_backoff_s=0.0)
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        await asyncio.sleep(30)
        return httpx.Response(200, content=b"late")

    kind, exc, took = _send(cfg, handler)
    assert kind == "HeaderTimeout" and isinstance(exc, httpx.ReadTimeout)
    assert 0.9 <= took < 5
    assert calls["n"] == 1                       # a timeout is never retried in the proxy


def test_a_slow_body_after_the_headers_is_not_cut(tmp_path):
    cfg = Config(home=tmp_path, upstream_header_timeout_s=1.0, upstream_read_timeout_s=600)

    async def body():
        yield b"a"
        await asyncio.sleep(1.5)                 # longer than the header limit, after the headers
        yield b"b"

    async def handler(request):
        return httpx.Response(200, content=body())

    kind, data, took = _send(cfg, handler)
    assert kind == "ok" and data == b"ab" and took >= 1.4


def test_headers_inside_the_limit_pass(tmp_path):
    cfg = Config(home=tmp_path, upstream_header_timeout_s=2.0)

    async def handler(request):
        await asyncio.sleep(0.3)
        return httpx.Response(200, content=b"ok")

    assert _send(cfg, handler)[:2] == ("ok", b"ok")


def test_limit_is_clamped_and_can_be_off(tmp_path):
    f = up_mod._header_timeout_s
    assert f(Config(home=tmp_path, upstream_header_timeout_s=180, upstream_read_timeout_s=600)) == 180
    assert f(Config(home=tmp_path, upstream_header_timeout_s=900, upstream_read_timeout_s=600)) == 600
    assert f(Config(home=tmp_path, upstream_header_timeout_s=0.01)) == 1.0
    for off in (0, -5, float("nan"), float("inf"), True):
        assert f(Config(home=tmp_path, upstream_header_timeout_s=off)) is None, off


def test_default_is_180_seconds():
    import dataclasses
    import os
    f = {x.name: x for x in dataclasses.fields(Config)}["upstream_header_timeout_s"]
    expect = float(os.environ.get("APEX_HEADER_TIMEOUT", "180"))
    assert f.default == expect
    assert "APEX_HEADER_TIMEOUT" not in os.environ or expect == f.default


def test_handler_books_a_header_timeout_as_a_502(tmp_path):
    """The passthrough handler catches any send error as 502 with error_cause = the class name."""
    import inspect

    from apex_router.proxy_engine.proxy.handlers import passthrough, shadow
    for mod in (passthrough, shadow):
        src = inspect.getsource(mod)
        assert "except Exception as exc:" in src and "error_cause = type(exc).__name__" in src
