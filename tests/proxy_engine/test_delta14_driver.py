"""Δ14 real-model driver — tool-loop + retrieval wiring, with the HTTP layer faked.

The driver spends real dollars against a live model, so these tests fake the transport (`_post`) and
prove the loop mechanics: a model that calls `retrieve_elided` gets the elided bytes served from the
resolver and its ref recorded; token usage accrues to the tally; an unknown ref returns an error
result. The live end-to-end run is a separate, budget-gated script — not a unit test.
"""
from __future__ import annotations

import json

from apex_router.proxy_engine.pipeline.resolver import StubResolver
from apex_router.proxy_engine.pipeline.transforms import json_crush
from apex_router.proxy_engine.tuner import behavioral_driver
from apex_router.proxy_engine.tuner.behavioral_gate import GateTask, Outcome, run_gate


def _records(n):
    return json.dumps([{"id": i, "name": f"item-{i}", "score": i * 7} for i in range(n)])


def test_driver_serves_retrieval_from_resolver(monkeypatch):
    """A model that emits a tool_use for a real ref gets the elided bytes served back, and the ref is
    recorded in retrieved_refs; a following text turn is the answer."""
    content = _records(300)
    resolver = StubResolver()
    resolver.register(content, {})
    ref = json_crush.elisions(content, {})[0][0]

    # two API rounds: (1) model calls retrieve_elided(ref); (2) model answers with the served value.
    rounds = iter([
        {"content": [{"type": "tool_use", "id": "tu1", "name": "retrieve_elided",
                      "input": {"ref": ref}}],
         "usage": {"input_tokens": 100, "output_tokens": 10}},
        {"content": [{"type": "text", "text": "1050"}],
         "usage": {"input_tokens": 120, "output_tokens": 5}},
    ])
    monkeypatch.setattr(urllib_seam(), "urlopen", _fake_urlopen(rounds))

    ask = behavioral_driver.build_driver(token="fake-token")
    out = ask("some prompt with a marker", [_tool()], resolver=resolver)
    assert ref in out["retrieved_refs"]
    assert "1050" in out["answer"]
    assert ask.spent() == 235  # 100+10 + 120+5


def test_driver_answers_without_retrieval(monkeypatch):
    """A model that answers straight away never populates retrieved_refs (correct_without_retrieval)."""
    rounds = iter([
        {"content": [{"type": "text", "text": "14"}],
         "usage": {"input_tokens": 90, "output_tokens": 3}},
    ])
    monkeypatch.setattr(urllib_seam(), "urlopen", _fake_urlopen(rounds))
    ask = behavioral_driver.build_driver(token="fake-token")
    out = ask("prompt", [_tool()], resolver=StubResolver())
    assert out["retrieved_refs"] == []
    assert out["answer"] == "14"


def test_driver_plugs_into_run_gate(monkeypatch):
    """End-to-end through run_gate with the faked transport: a retrieved-then-correct run classifies
    as CORRECT_WITH_RETRIEVAL — proving the driver satisfies the gate's injection contract."""
    content = _records(300)
    ref = json_crush.elisions(content, {})[0][0]
    rounds = iter([
        {"content": [{"type": "tool_use", "id": "tu1", "name": "retrieve_elided",
                      "input": {"ref": ref}}], "usage": {"input_tokens": 100, "output_tokens": 10}},
        {"content": [{"type": "text", "text": "1050"}], "usage": {"input_tokens": 120, "output_tokens": 5}},
    ])
    monkeypatch.setattr(urllib_seam(), "urlopen", _fake_urlopen(rounds))
    ask = behavioral_driver.build_driver(token="fake-token")
    task = GateTask(content=content, question="score of id 150?", correct_answer="1050")
    result = run_gate([task], ask_model=ask)
    assert result.outcomes[0] == Outcome.CORRECT_WITH_RETRIEVAL


# --- test seams -----------------------------------------------------------------------------------

def urllib_seam():
    import urllib.request
    return urllib.request


def _tool():
    return {"name": "retrieve_elided", "description": "retrieve elided bytes",
            "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}},
                             "required": ["ref"]}}


def _fake_urlopen(rounds):
    class _Resp:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")
        def read(self):
            return self._payload
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=None):
        return _Resp(next(rounds))
    return _urlopen
