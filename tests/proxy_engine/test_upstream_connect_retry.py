"""Upstream connect-only retry (Finding 4).

A ConnectError/ConnectTimeout is raised BEFORE the request reaches the upstream socket, so the
POST was never received — retrying cannot double-submit. A ReadError is NOT retried (the request
may already be in flight upstream). These pin both halves of that contract.
"""
from __future__ import annotations

import asyncio

import httpx

from apex_router.proxy_engine.config import Config
from apex_router.proxy_engine.proxy.upstream import Upstream


def _upstream(cfg, handler):
    up = Upstream(cfg)
    up._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return up


def test_connect_error_is_retried_then_succeeds(tmp_path):
    cfg = Config(home=tmp_path, upstream_connect_retries=2, upstream_connect_backoff_s=0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:  # fail attempts 1 and 2, succeed on 3
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, content=b"ok")

    async def run():
        up = _upstream(cfg, handler)
        resp = await up.send_stream("POST", "http://x/y", headers=[(b"host", b"x")], content=b"{}")
        body = await resp.aread()
        await resp.aclose()
        await up.aclose()
        return body

    assert asyncio.run(run()) == b"ok"
    assert calls["n"] == 3  # 1 initial + 2 retries


def test_connect_error_exhausts_and_raises(tmp_path):
    cfg = Config(home=tmp_path, upstream_connect_retries=2, upstream_connect_backoff_s=0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("always", request=request)

    async def run():
        up = _upstream(cfg, handler)
        try:
            await up.send_stream("POST", "http://x/y", headers=[(b"host", b"x")], content=b"{}")
            return "no-raise"
        except httpx.ConnectError:
            return "raised"
        finally:
            await up.aclose()

    assert asyncio.run(run()) == "raised"
    assert calls["n"] == 3  # 1 + 2 retries, then propagates


def test_read_error_is_NOT_retried(tmp_path):
    # A ReadError may mean the request is already processing upstream — retrying risks a duplicate
    # non-idempotent completion. It must propagate on the first attempt.
    cfg = Config(home=tmp_path, upstream_connect_retries=2, upstream_connect_backoff_s=0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadError("mid-flight", request=request)

    async def run():
        up = _upstream(cfg, handler)
        try:
            await up.send_stream("POST", "http://x/y", headers=[(b"host", b"x")], content=b"{}")
            return "no-raise"
        except httpx.ReadError:
            return "raised"
        finally:
            await up.aclose()

    assert asyncio.run(run()) == "raised"
    assert calls["n"] == 1  # NOT retried


def test_retry_budget_is_clamped_against_hostile_config(tmp_path):
    # inf backoff + huge retry count must NOT hang or overflow: attempts are capped and each sleep
    # is bounded (xval F3). With inf clamped to 0 backoff, retries are fast; we just assert it
    # terminates with a bounded number of attempts, not an astronomical sleep.
    cfg = Config(home=tmp_path, upstream_connect_retries=10_000,
                 upstream_connect_backoff_s=float("inf"))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("always", request=request)

    async def run():
        up = _upstream(cfg, handler)
        try:
            await up.send_stream("POST", "http://x/y", headers=[(b"host", b"x")], content=b"{}")
        except httpx.ConnectError:
            pass
        finally:
            await up.aclose()

    asyncio.run(run())
    # _MAX_CONNECT_RETRIES=10 → at most 11 attempts, never 10_001
    assert calls["n"] <= 11


def test_negative_backoff_does_not_break_retry(tmp_path):
    cfg = Config(home=tmp_path, upstream_connect_retries=1, upstream_connect_backoff_s=-5.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, content=b"ok")

    async def run():
        up = _upstream(cfg, handler)
        r = await up.send_stream("POST", "http://x/y", headers=[(b"host", b"x")], content=b"{}")
        body = await r.aread()
        await r.aclose()
        await up.aclose()
        return body

    assert asyncio.run(run()) == b"ok"


def test_stats_records_backoff_for_latency_attribution(tmp_path):
    # F4: the caller needs the backoff duration so it can bill apex, not upstream. send_stream must
    # populate the stats out-param with the total slept seconds and the retry count.
    cfg = Config(home=tmp_path, upstream_connect_retries=3, upstream_connect_backoff_s=0.01)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:  # fail twice → two backoff sleeps
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, content=b"ok")

    async def run():
        up = _upstream(cfg, handler)
        stats: dict = {}
        r = await up.send_stream("POST", "http://x/y", headers=[(b"host", b"x")],
                                 content=b"{}", stats=stats)
        await r.aclose()
        await up.aclose()
        return stats

    stats = asyncio.run(run())
    assert stats.get("connect_retries") == 2  # two retries happened
    # equal-jitter: each sleep ∈ [capped/2, capped]. Sleep0 cap=0.01→[0.005,0.01],
    # sleep1 cap=0.02→[0.01,0.02]. Total ∈ [0.015, 0.03].
    total = stats.get("connect_backoff_s", 0.0)
    assert 0.015 - 1e-9 <= total <= 0.03 + 1e-9, total


def test_backoff_is_jittered_not_deterministic(tmp_path):
    # equal-jitter must introduce randomness so clients don't reconnect in lockstep (thundering herd).
    from apex_router.proxy_engine.proxy.upstream import Upstream

    cfg = Config(home=tmp_path, upstream_connect_retries=1, upstream_connect_backoff_s=1.0)

    async def one_backoff():
        seen = {"n": 0}

        def handler(request):
            seen["n"] += 1
            if seen["n"] < 2:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, content=b"ok")

        up = Upstream(cfg)
        up._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        # patch sleep to capture the delay without actually waiting
        import apex_router.proxy_engine.proxy.upstream as um
        captured = []
        real_sleep = um.asyncio.sleep

        async def fake_sleep(d):
            captured.append(d)
            return await real_sleep(0)

        um.asyncio.sleep = fake_sleep
        try:
            stats: dict = {}
            r = await up.send_stream("POST", "http://x", headers=[(b"host", b"x")],
                                     content=b"{}", stats=stats)
            await r.aclose()
        finally:
            um.asyncio.sleep = real_sleep
            await up.aclose()
        return captured[0]

    async def run():
        return [await one_backoff() for _ in range(8)]

    delays = asyncio.run(run())
    # cap=1.0 → each delay ∈ [0.5, 1.0]; and they must not all be identical (jitter present)
    assert all(0.5 - 1e-9 <= d <= 1.0 + 1e-9 for d in delays), delays
    assert len(set(delays)) > 1, f"backoff not jittered: {delays}"


def test_total_backoff_budget_caps_cumulative_sleep(tmp_path):
    # cumulative backoff across retries must never exceed _MAX_CONNECT_TOTAL_BACKOFF_S.
    from apex_router.proxy_engine.proxy.upstream import _MAX_CONNECT_TOTAL_BACKOFF_S

    cfg = Config(home=tmp_path, upstream_connect_retries=10, upstream_connect_backoff_s=1000.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("always", request=request)

    async def run():
        import apex_router.proxy_engine.proxy.upstream as um
        real_sleep = um.asyncio.sleep

        async def fake_sleep(d):
            return await real_sleep(0)  # don't actually wait; we only assert the recorded totals

        um.asyncio.sleep = fake_sleep
        up = _upstream(cfg, handler)
        stats: dict = {}
        try:
            await up.send_stream("POST", "http://x", headers=[(b"host", b"x")],
                                 content=b"{}", stats=stats)
        except httpx.ConnectError:
            pass
        finally:
            um.asyncio.sleep = real_sleep
            await up.aclose()
        return stats

    stats = asyncio.run(run())
    assert stats.get("connect_backoff_s", 0.0) <= _MAX_CONNECT_TOTAL_BACKOFF_S + 1e-6
