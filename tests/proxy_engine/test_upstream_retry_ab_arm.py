"""Retry-action A/B instrumentation (telemetry v11) — no behaviour change by default.

Pinned: the default policy is today's jittered backoff, now labelled immediate/immediate/1.0; `wait`
raises each pre-retry sleep to a bounded APEX_RETRY_WAIT_MS (<= 2 s); `switch` only logs (one
upstream per wire); APEX_RETRY_AB=1 draws one arm per request with its propensity; a request that
never retried carries no arm; both handlers copy the arm onto the row.
"""
from __future__ import annotations

import asyncio
import random

import httpx
import pytest

from apex_router.proxy_engine.config import Config
from apex_router.proxy_engine.proxy import upstream as up_mod
from apex_router.proxy_engine.proxy.handlers import passthrough
from apex_router.proxy_engine.proxy.handlers import shadow as shadow_h
from apex_router.proxy_engine.proxy.upstream import Upstream
from apex_router.proxy_engine.telemetry.events import TELEMETRY_SCHEMA_VERSION, TelemetryEvent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("APEX_RETRY_POLICY", "APEX_RETRY_AB", "APEX_RETRY_AB_ARMS", "APEX_RETRY_WAIT_MS"):
        monkeypatch.delenv(k, raising=False)


def _upstream(tmp_path, fail_first: int, *, retries=2, backoff=0.0):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= fail_first:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, content=b"ok")

    cfg = Config(home=tmp_path, upstream_connect_retries=retries, upstream_connect_backoff_s=backoff)
    up = Upstream(cfg)
    up._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return up, calls


def _send(up):
    async def run():
        stats: dict = {}
        try:
            r = await up.send_stream("POST", "http://x/v1/messages", headers=[(b"host", b"x")],
                                     content=b"{}", stats=stats)
            await r.aread()
            await r.aclose()
        finally:
            await up.aclose()
        return stats
    return asyncio.run(run())


def test_default_is_today_behaviour_labelled_immediate(tmp_path):
    up, calls = _upstream(tmp_path, fail_first=1)
    stats = _send(up)
    assert calls["n"] == 2 and stats["connect_retries"] == 1
    assert (stats["retry_policy"], stats["retry_arm"], stats["retry_propensity"]) == \
        ("immediate", "immediate", 1.0)
    assert stats["connect_backoff_s"] < 0.05          # backoff 0 → no added wait


def test_no_retry_no_arm(tmp_path):
    up, _ = _upstream(tmp_path, fail_first=0)
    stats = _send(up)
    assert "retry_arm" not in stats and "connect_retries" not in stats


def test_arm_drawn_once_per_request(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_RETRY_AB", "1")
    draws = []
    real = up_mod.retry_assignment
    monkeypatch.setattr(up_mod, "retry_assignment", lambda rng=None: draws.append(1) or real(rng))
    up, calls = _upstream(tmp_path, fail_first=2)
    stats = _send(up)
    assert calls["n"] == 3 and stats["connect_retries"] == 2 and len(draws) == 1


def test_wait_raises_the_sleep_to_the_bounded_floor(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_RETRY_POLICY", "wait")
    monkeypatch.setenv("APEX_RETRY_WAIT_MS", "60")
    up, _ = _upstream(tmp_path, fail_first=1)
    stats = _send(up)
    assert stats["retry_arm"] == "wait" and stats["retry_policy"] == "wait"
    assert stats["connect_backoff_s"] >= 0.055


def test_switch_is_a_logged_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_RETRY_POLICY", "switch")
    up, calls = _upstream(tmp_path, fail_first=1)
    stats = _send(up)
    assert stats["retry_arm"] == "switch" and calls["n"] == 2
    assert stats["connect_backoff_s"] < 0.05          # behaves like immediate


@pytest.mark.parametrize("raw,expect", [(None, 1.0), ("99999", 2.0), ("-5", 0.0), ("abc", 1.0),
                                        ("inf", 1.0), ("250", 0.25)])
def test_wait_ms_is_clamped(monkeypatch, raw, expect):
    if raw is not None:
        monkeypatch.setenv("APEX_RETRY_WAIT_MS", raw)
    assert up_mod._retry_wait_s() == pytest.approx(expect)


def test_assignment_env_parsing(monkeypatch):
    assert up_mod.retry_assignment() == ("immediate", "immediate", 1.0)
    monkeypatch.setenv("APEX_RETRY_POLICY", "bogus")
    assert up_mod.retry_assignment() == ("immediate", "immediate", 1.0)
    monkeypatch.setenv("APEX_RETRY_AB", "1")
    rng = random.Random(7)
    arms = [up_mod.retry_assignment(rng) for _ in range(400)]
    assert {a[0] for a in arms} == {"ab"} and {a[2] for a in arms} == {0.5}
    share = sum(a[1] == "wait" for a in arms) / len(arms)
    assert {a[1] for a in arms} == {"immediate", "wait"} and 0.4 < share < 0.6
    monkeypatch.setenv("APEX_RETRY_AB_ARMS", "immediate, wait,switch,nope,wait")
    arms = {up_mod.retry_assignment(rng) for _ in range(200)}
    assert {a[1] for a in arms} == {"immediate", "wait", "switch"}
    assert all(a[2] == pytest.approx(1 / 3) for a in arms)
    monkeypatch.setenv("APEX_RETRY_AB_ARMS", "wait")            # < 2 valid arms → default pair
    assert {up_mod.retry_assignment(rng)[1] for _ in range(100)} == {"immediate", "wait"}


def test_schema_v11_fields_default_none():
    assert TELEMETRY_SCHEMA_VERSION == 11
    ev = TelemetryEvent.start(apex_version="t", client="claude-code")
    assert ev.retry_policy is None and ev.retry_arm is None and ev.retry_propensity is None
    ev.record_retry_arm({"connect_retries": 0})
    assert ev.retry_arm is None
    ev.record_retry_arm({"retry_policy": "ab", "retry_arm": "wait", "retry_propensity": 0.5})
    assert (ev.retry_policy, ev.retry_arm, ev.retry_propensity) == ("ab", "wait", 0.5)


# ---- both handlers copy the arm onto the row (telemetry contract) -------------------------------

class _URL:
    path = "/v1/messages"


class _Req:
    method = "POST"
    url = _URL()
    headers = {"x-claude-code-session-id": "s"}
    scope = {"raw_path": b"/v1/messages", "query_string": b"", "headers": []}

    async def body(self):
        return b'{"model":"m"}'


class _Tel:
    def __init__(self):
        self.ev = []

    def emit(self, e):
        self.ev.append(e)


class _Resp:
    status_code = 200
    headers = httpx.Headers({})

    async def aiter_raw(self):
        yield b"x"

    async def aclose(self):
        pass


def _fake_up(raise_after: bool):
    class _Up:
        def build_url(self, k, p, q):
            return "http://up" + p

        def endpoint_id(self, k):
            return "anthropic"

        async def inject_auth(self, headers, client_kind, *, raw_headers=None):
            return headers

        async def send_stream(self, m, u, *, headers, content, stats=None):
            stats.update(connect_retries=1, connect_backoff_s=0.0, retry_policy="ab",
                         retry_arm="wait", retry_propensity=0.5)
            if raise_after:
                raise httpx.ConnectError("refused")
            return _Resp()
    return _Up()


@pytest.mark.parametrize("handler", ["passthrough", "shadow"])
@pytest.mark.parametrize("raise_after", [False, True])
def test_handlers_record_the_arm(handler, raise_after):
    async def go():
        tel = _Tel()
        mod = passthrough if handler == "passthrough" else shadow_h
        args = (_Req(), _fake_up(raise_after), tel) + (() if handler == "passthrough" else (None,))
        resp = await mod.handle(*args)
        if not raise_after:
            async for _ in resp.body_iterator:
                pass
        return tel.ev[0]
    ev = asyncio.run(go())
    assert (ev.retry_policy, ev.retry_arm, ev.retry_propensity) == ("ab", "wait", 0.5)
    assert ev.connect_retries == 1 and ev.is_error is raise_after
