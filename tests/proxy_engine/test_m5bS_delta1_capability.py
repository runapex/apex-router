"""Δ1 — capability-gated lossy (roadmap §1). A lossy (`lossy_ccr`) cell is UNREPRESENTABLE unless
capabilities are present: the compiler refuses to SIGN it without validator + evidence fields, and
the runtime refuses to EXECUTE it without a registered resolver (ships raw, reason
`capability_missing`).

This converts "floor-validator-first" sequencing into an unrepresentable state (house style): a
signed policy that names a lossy transform but can't validate/resolve it can never exist, and even a
hand-forged one is inert at runtime.
"""

from __future__ import annotations

import pytest

from apex_router.proxy_engine.pipeline import decide as decide_mod
from apex_router.proxy_engine.pipeline.decide import decide
from apex_router.proxy_engine.policy import ClassRule, InvalidPolicy

# ── runtime: a lossy rule without a registered resolver is inert (ships raw) ──────────────────────


def _lossy_rule(**over):
    base = dict(
        transform="json_crush",
        enabled=True,
        min_bytes=1,
        ratio_floor=0.0,
        knobs={},
        transform_version="",
        validator_id="json_entity_floor_v1",
        validator_version="1",
        fidelity_class="ccr_retrieval",
    )
    base.update(over)
    return ClassRule(**base)


def _total_policy(rule, route_class="json"):
    from apex_router.proxy_engine.policy import CONTENT_CLASSES, ExpectedReport, PolicyVersion, T2Policy

    strata = ("xs", "s", "m", "l", "xl")
    raw = ClassRule(transform=None, enabled=False, min_bytes=1 << 30, ratio_floor=0.0)
    rules = {c: {st: raw for st in strata} for c in CONTENT_CLASSES}
    rules[route_class] = {st: rule for st in strata}
    return PolicyVersion(
        version=1,
        compiled_at=1.0,
        compiler_hash="h",
        corpus_hash="c",
        band=(6.0, 30.0),
        rules=rules,
        t2=T2Policy(consolidate_on=("ttl",), min_turn_count=5),
        expected=ExpectedReport(0.0, {}),
    )


def test_runtime_lossy_unreachable_without_resolver(monkeypatch):
    """A hand-forged lossy rule → decide() ships raw with reason `capability_missing`, because no
    resolver is registered for the CCR retrieval path (the store is unbuilt, Δ12)."""
    monkeypatch.setattr(decide_mod, "_RESOLVERS", {}, raising=False)
    content = "[" + ",".join(f'{{"id":{i},"v":"x"}}' for i in range(60)) + "]"  # crushable json
    em = decide(content, _total_policy(_lossy_rule()), context_bytes=13000, tool_name="Read")
    assert em.transformed is False
    assert em.reason == "capability_missing"


def test_runtime_lossy_runs_when_resolver_registered(monkeypatch):
    """With a resolver registered for the lossy transform, decide() is allowed to emit (the rest of
    the gate still applies — this asserts the capability check no longer blocks)."""
    monkeypatch.setattr(decide_mod, "_RESOLVERS", {"json_crush": object()}, raising=False)
    content = "[" + ",".join(f'{{"id":{i},"name":"item_{i}"}}' for i in range(60)) + "]"
    em = decide(content, _total_policy(_lossy_rule()), context_bytes=13000, tool_name="Read")
    # it either emits or falls below floor — but NOT capability_missing (the gate passed)
    assert em.reason != "capability_missing"


def test_runtime_lossless_rule_needs_no_resolver(monkeypatch):
    """A lossless (`wire_canonicalization`/`lossless`) cell must NOT require a resolver — only lossy
    (ccr) cells do. Compaction with no resolver registered still runs."""
    monkeypatch.setattr(decide_mod, "_RESOLVERS", {}, raising=False)
    rule = _lossy_rule(
        transform="compaction",
        fidelity_class="wire_canonicalization",
        validator_id=None,
        validator_version="",
    )
    content = '{"a":  1,   "b":   2, "padding":"' + "x" * 300 + '"}'  # compactable json
    em = decide(content, _total_policy(rule), context_bytes=13000, tool_name="Read")
    assert em.reason != "capability_missing"


def _synthetic_lossy_corpus():
    """Hermetic corpus with independently addressable, highly crushable JSON frontiers.

    Multi-turn sessions (set `frontier_block` explicitly so each turn's priced block is a fresh,
    standalone crushable JSON value while the cached prefix grows via `context_bytes`). The blocks
    must have a real amortization horizon R > 0 — a one-request-per-session corpus makes every block
    terminal (R=0), which under real-per-block-R pricing is economically inert and admits nothing
    (the phantom-horizon the stock-vs-flow fix removed). Two sessions × several turns keeps the test
    about capability gating while giving the early blocks a real horizon.
    """
    import json

    from apex_router.proxy_engine.tuner.replay import Request
    from apex_router.proxy_engine.tuner.tokens import true_token_count

    corpus = []
    for sess in ("lossy-A", "lossy-B"):
        ctx = 0
        for t in range(5):
            text = json.dumps(
                [{"id": j, "name": f"item_{j}_{t}", "vals": list(range(8))} for j in range(100)],
                indent=2,
            )
            block = text.encode("utf-8")
            corpus.append(
                Request(
                    session_id=sess,
                    content=b"",
                    tokens=max(1, ctx // 4),
                    ts=float(t),
                    model="claude-opus-4-8",
                    frontier_block=block,
                    context_bytes=ctx,
                )
            )
            ctx += len(block)
    return corpus


# ── compiler: refuses to sign a lossy cell without validator + evidence ───────────────────────────


def test_uncapable_lossy_cell_is_blocked_on_evidence_not_a_crash(monkeypatch):
    """An economically-admissible lossy cell WITHOUT a registered capability must NOT crash the
    compile. It is a first-class `blocked_on_evidence` cell: compile SUCCEEDS, the rule ships DISABLED
    (deny-by-default preserved — nothing lossy can fire, _LOSSY_CAPABILITIES stays empty), the sealed
    policy carries no lossy rule, and the economic case is recorded as a standing demand for a
    behavioral campaign. Economics proposes; evidence disposes. (Was: raise InvalidPolicy — the wrong
    failure mode; it conflated 'the economics propose this cell' with 'the policy may sign it'.)"""
    import apex_router.proxy_engine.tuner.compiler as C
    from apex_router.proxy_engine.pipeline.transforms import json_crush

    monkeypatch.setitem(C._TRANSFORMS, "json", (json_crush, None))
    monkeypatch.setattr(C, "_LOSSY_CAPABILITIES", {}, raising=False)
    corpus = _synthetic_lossy_corpus()

    res = C.compile_policy(corpus, version=1, compiled_at=1_720_600_000.0)  # must NOT raise
    assert res.policy.verify()

    # no lossy cell is enabled (deny-by-default intact)
    lossy_enabled = [
        r
        for strata in res.policy.rules.values()
        for r in strata.values()
        if r.enabled and r.fidelity_class == "ccr_retrieval"
    ]
    assert not lossy_enabled

    # but the economic case is surfaced: a blocked_on_evidence record naming the cell + its Δ$ demand
    blocked = res.evidence.get("blocked_on_evidence")
    assert blocked, "an economically-admissible uncapable lossy cell must be recorded, not dropped"
    assert any(b["transform"] == "json_crush" for b in blocked)
    b = next(b for b in blocked if b["transform"] == "json_crush")
    assert b["reason"] == "no_registered_capability"
    assert b["expected_delta"] > 0  # the dollar case for buying the evidence
    assert "stratum" in b and "content_class" in b
    # the Δ$ is priced on POSITIONAL R (an upper bound) — the record must carry that provenance so no
    # reader quotes it as a signed figure (it is unsignable until repriced on measured R_eff).
    assert b["r_basis"] == "positional_upper_bound"


def test_compiler_signs_lossy_when_capable_still_raises_never(monkeypatch):
    """Sanity twin: WITH a capability the same cell signs (no blocked record). Guards that
    blocked_on_evidence is strictly the no-capability branch, not always-on."""
    import apex_router.proxy_engine.tuner.compiler as C
    from apex_router.proxy_engine.pipeline.transforms import json_crush

    monkeypatch.setitem(C._TRANSFORMS, "json", (json_crush, None))
    monkeypatch.setattr(
        C,
        "_LOSSY_CAPABILITIES",
        {"json_crush": {"validator_id": "json_entity_floor_v1", "validator_version": "1",
                        "evidence": "q6-reference-mining"}},
        raising=False,
    )
    res = C.compile_policy(_synthetic_lossy_corpus(), version=1, compiled_at=1_720_600_000.0)
    assert not res.evidence.get("blocked_on_evidence")  # capable → nothing blocked


def test_compiler_signs_lossy_when_capable(monkeypatch):
    """With the validator+resolver capability registered, the same lossy admission signs cleanly and
    the sealed rule carries the validator_id."""
    import apex_router.proxy_engine.tuner.compiler as C
    from apex_router.proxy_engine.pipeline.transforms import json_crush

    monkeypatch.setitem(C._TRANSFORMS, "json", (json_crush, None))
    monkeypatch.setattr(
        C,
        "_LOSSY_CAPABILITIES",
        {
            "json_crush": {
                "validator_id": "json_entity_floor_v1",
                "validator_version": "1",
                "evidence": "q6-reference-mining",
            }
        },
        raising=False,
    )
    corpus = _synthetic_lossy_corpus()
    res = C.compile_policy(corpus, version=1, compiled_at=1_720_600_000.0)
    lossy = [
        r
        for strata in res.policy.rules.values()
        for r in strata.values()
        if r.enabled and r.fidelity_class == "ccr_retrieval"
    ]
    assert lossy, "expected at least one admitted lossy cell on this corpus"
    assert all(r.validator_id == "json_entity_floor_v1" for r in lossy)
