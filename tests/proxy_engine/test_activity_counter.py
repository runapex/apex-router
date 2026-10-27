"""The proxy's in-progress counter (`/stats` activity): a streamed request counts until its last
byte; a dropped client and an app error still count down; the local endpoints are not counted.
The flow studio restarts a proxy only at inflight == 0, so a wrong count could break a request."""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from apex_router.proxy_engine.proxy.app import DRAIN_MAX_S, ActivityCounter, draining, set_drain

from test_m0_passthrough import app_with_mock_upstream  # noqa: F401  (fixture; rootdir-relative import)


def _act():
    return {"inflight": 0, "last_start": 0.0, "last_end": 0.0, "started": 0.0, "drain_until": 0.0}


def _run(app, path="/v1/messages", fail=False):
    seen = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        seen.append((msg["type"], app.activity["inflight"]))
        if fail and msg["type"] == "http.response.body":
            raise ConnectionResetError("client went away")

    async def go():
        await app({"type": "http", "path": path, "method": "POST", "headers": []}, receive, send)
    return seen, go


def test_a_streamed_request_counts_until_its_last_chunk():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for i in range(3):
            await send({"type": "http.response.body", "body": b"x", "more_body": i < 2})
            await asyncio.sleep(0)
    a = _act()
    app = ActivityCounter(inner, a)
    seen, go = _run(app)
    asyncio.run(go())
    assert [n for _, n in seen] == [1, 1, 1, 1]       # 1 during start + every chunk
    assert a["inflight"] == 0 and a["last_end"] >= a["last_start"] > 0


def test_a_dropped_client_and_an_app_error_still_count_down():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x", "more_body": True})
    a = _act()
    app = ActivityCounter(inner, a)
    _, go = _run(app, fail=True)
    with pytest.raises(ConnectionResetError):
        asyncio.run(go())
    assert a["inflight"] == 0

    async def boom(scope, receive, send):
        raise RuntimeError("bug")
    app2 = ActivityCounter(boom, a)
    _, go2 = _run(app2)
    with pytest.raises(RuntimeError):
        asyncio.run(go2())
    assert a["inflight"] == 0


@pytest.mark.parametrize("path", ["/healthz", "/status", "/stats"])
def test_local_endpoints_are_not_counted(path):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    a = _act()
    app = ActivityCounter(inner, a)
    seen, go = _run(app, path=path)
    asyncio.run(go())
    assert [n for _, n in seen] == [0, 0] and a["last_start"] == 0.0


def test_stats_reports_activity_through_the_real_app(app_with_mock_upstream):  # noqa: F811
    app, _, _ = app_with_mock_upstream
    with TestClient(app) as client:
        s0 = client.get("/stats").json()["activity"]
        assert s0["inflight"] == 0 and s0["idle_s"] >= 0
        r = client.post("/v1/messages", content=b'{"messages":[]}', headers={"anthropic-version": "2023-06-01"})
        assert r.status_code == 200
        s1 = client.get("/stats").json()["activity"]
        assert isinstance(s1["pid"], int) and s1["draining"] is False
        assert s1["inflight"] == 0 and s1["idle_s"] < 5


def test_drain_refuses_new_requests_but_keeps_local_paths_and_expires(monkeypatch):
    called = []

    async def inner(scope, receive, send):
        called.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    a = _act()
    app = ActivityCounter(inner, a)
    set_drain(a, True)
    assert draining(a)
    seen, go = _run(app)
    asyncio.run(go())
    assert called == [] and a["inflight"] == 0 and a["last_start"] == 0.0   # not counted, not proxied
    seen, go = _run(app, "/stats")
    asyncio.run(go())
    assert called == ["/stats"]
    set_drain(a, False)
    seen, go = _run(app)
    asyncio.run(go())
    assert called == ["/stats", "/v1/messages"]
    set_drain(a, True)                                   # a drain that nobody ends expires
    import apex_router.proxy_engine.proxy.app as appmod
    t = appmod.time.time()
    monkeypatch.setattr(appmod.time, "time", lambda: t + DRAIN_MAX_S + 1)
    assert not draining(a)


def test_drain_answers_503_with_retry_after():
    async def inner(scope, receive, send):
        raise AssertionError("must not run")
    a = _act()
    set_drain(a, True)
    msgs = []

    async def send(m):
        msgs.append(m)

    async def receive():
        return {"type": "http.request", "body": b""}
    asyncio.run(ActivityCounter(inner, a)({"type": "http", "path": "/v1/messages", "headers": []}, receive, send))
    assert msgs[0]["status"] == 503 and (b"retry-after", b"5") in msgs[0]["headers"]
