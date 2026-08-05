"""M0 exit criterion 2 — apex's own added latency p99 < 20ms → sets the real ttft budget.

Drives N requests through the real ASGI app + handler with an instant mock upstream, then
reads the `apex_added_ms` field apex recorded in its own telemetry (its work *excluding*
upstream time). The mock returns immediately, so any measured time IS apex overhead.
"""
from __future__ import annotations

import json
import statistics

import httpx
import pytest
from starlette.testclient import TestClient

from apex_router.proxy_engine.config import Config
from apex_router.proxy_engine.proxy import upstream as upstream_mod

N = 200


class _AStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_AStream([b"data: {}\n\n"]))

    real_init = upstream_mod.Upstream.__init__

    def patched_init(self, cfg):
        real_init(self, cfg)
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(upstream_mod.Upstream, "__init__", patched_init)
    from apex_router.proxy_engine.proxy.app import create_app
    cfg = Config(home=tmp_path)
    return create_app(cfg), cfg


def test_apex_added_ms_p99_under_budget(app):
    application, cfg = app
    body = json.dumps({"messages": [{"role": "user", "content": "x" * 5000}]}).encode()
    with TestClient(application) as client:
        for _ in range(N):
            r = client.post("/v1/messages", content=body,
                            headers={"anthropic-version": "2023-06-01",
                                     "x-claude-code-session-id": "bench"})
            assert r.status_code == 200

    events = [json.loads(ln) for ln in cfg.telemetry_path.read_text().splitlines()]
    added = sorted(e["apex_added_ms"] for e in events)
    assert len(added) == N
    p50 = statistics.median(added)
    p99 = added[int(0.99 * len(added)) - 1]
    print(f"\napex_added_ms  p50={p50:.3f}  p99={p99:.3f}  max={added[-1]:.3f}  (n={N})")
    # M0 gate: apex's own overhead is small enough that ttft_budget_ms=150 is comfortable.
    assert p99 < 20.0, f"apex overhead p99 {p99:.2f}ms exceeds 20ms M0 budget"
