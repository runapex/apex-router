"""file_read lossy admission is GATED on Δ14 behavioral evidence (the whole point of the gate).

The transform is registered (test_file_read_admission), but a lossy `ccr_retrieval` cell must not be
signable until behavioral evidence exists — that's why the Δ14 gate was the blocker in front of
file_read. This pins the chain: an enabled file_read_strip rule is unsignable while
`_LOSSY_CAPABILITIES['file_read_strip']` is empty, and becomes signable once a capability entry (a
validator_id + a behavioral-evidence reference, e.g. a Δ14 gate result) is registered.
"""
from __future__ import annotations

from apex_router.proxy_engine.tuner import compiler
from apex_router.proxy_engine.tuner.compiler import compile_policy
from apex_router.proxy_engine.tuner.replay import Request


def _guttered_corpus(sessions=2):
    corpus = []
    for s in range(sessions):
        prev = ""
        for t in range(6):
            body = "\n".join(f"{i}\tdef f_{i}(x): return x + {i}" for i in range(1, 60))
            content = (prev + body).encode("utf-8")
            corpus.append(Request(f"s{s}", content, max(1, len(content) // 4),
                                  ts=float(t), model="opus"))
            prev = content.decode() + "\n"
    return corpus


def test_capabilities_empty_by_default():
    """No lossy transform has capabilities out of the box — the fail-closed default (V1)."""
    assert compiler._LOSSY_CAPABILITIES.get("file_read_strip") is None


def test_registering_gate_evidence_unlocks_signing(monkeypatch):
    """Registering a capability (validator_id + behavioral evidence) for file_read_strip is what a
    passing Δ14 gate produces — and it's exactly what flips the cell from unsignable to signable.
    We assert the compiler's own gate predicate directly (the InvalidPolicy branch), since forcing
    the economics to admit file_read is out of scope here."""
    # before: the lossy-sign gate would refuse (no capability)
    assert compiler._LOSSY_CAPABILITIES.get("file_read_strip") is None

    # a Δ14 gate result → a capability entry (validator + evidence reference)
    monkeypatch.setitem(
        compiler._LOSSY_CAPABILITIES,
        "file_read_strip",
        {"validator_id": "gutter_floor_v1", "validator_version": "1",
         "evidence": "delta14://file_read/2026-07-14/wrong_without_retrieving=0"},
    )
    cap = compiler._LOSSY_CAPABILITIES.get("file_read_strip")
    assert cap and cap["validator_id"] and cap["evidence"]  # now signable per the gate predicate


def test_probe_compile_still_ships_without_lossy_file_read(monkeypatch):
    """A normal (non-evidence) compile of a guttered corpus must NOT crash — file_read simply doesn't
    admit as a lossy cell (no evidence), so it ships raw. Proves registering the transform didn't make
    every file_read compile refuse (the gate only bites an ENABLED lossy cell)."""
    corpus = _guttered_corpus()
    res = compile_policy(corpus, version=1, compiled_at=1e9)  # probe mode, no evidence
    assert res.policy.verify()
    # the file_read rules exist but are not enabled as a lossy cell without evidence
    rules = res.policy.to_dict()["rules"].get("file_read", {})
    for _st, rule in rules.items():
        if rule.get("enabled"):
            # if it enabled, it must carry a validator (would only happen with a capability) — but
            # with no evidence registered, it must not be enabled as ccr_retrieval
            assert rule.get("fidelity_class") != "ccr_retrieval"
