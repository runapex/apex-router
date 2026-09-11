"""Δ3 (LLM Systems Digest, MetaKV lesson) — filter-then-optimize invariant.

The digest's MetaKV takeaway: eliminate actions that violate a HARD constraint FIRST, then optimize
expected value among the FEASIBLE set — never collapse correctness/safety and savings into one
blended score. apex already honors this (its own doctrine: "cache safety is a wall, not a weight"):
`decide()` is a sequence of independent hard walls (freeze → addressable → capability → floor), and
the only *optimize* criterion (`_saved_fraction >= ratio_floor`) runs strictly INSIDE the feasible
set. The compiler mirrors it (evidence/efficacy/sign-stability/token-safety/capability walls, then
`compile_min_bytes`/break-even ceiling among survivors).

These tests PIN the precedence so it can't silently regress into a blended score: a hard wall must
beat a would-be-large saving every time. Each case constructs a block the transform WOULD compress a
lot, then asserts the wall ships it RAW regardless. Companion to test_m5bS_delta1_capability.py
(which tests each wall in isolation); this asserts the *ordering* — wall dominates saving.
"""

from __future__ import annotations

from apex_router.proxy_engine.pipeline import decide as decide_mod
from apex_router.proxy_engine.pipeline.decide import decide
from apex_router.proxy_engine.policy import (
    CONTENT_CLASSES,
    ClassRule,
    ExpectedReport,
    PolicyVersion,
    T2Policy,
)

_STRATA = ("xs", "s", "m", "l", "xl")


def _policy_with(rule: ClassRule, route_class: str = "json") -> PolicyVersion:
    """A total policy where `route_class` routes to `rule` in every stratum; everything else is the
    never-match raw rule. Mirrors test_m5bS_delta1_capability._total_policy."""
    raw = ClassRule(transform=None, enabled=False, min_bytes=1 << 30, ratio_floor=0.0)
    rules = {c: {st: raw for st in _STRATA} for c in CONTENT_CLASSES}
    rules[route_class] = {st: rule for st in _STRATA}
    return PolicyVersion(
        version=1, compiled_at=1.0, compiler_hash="h", corpus_hash="c",
        band=(6.0, 30.0), rules=rules,
        t2=T2Policy(consolidate_on=("ttl",), min_turn_count=5),
        expected=ExpectedReport(0.0, {}),
    )


def _highly_crushable_json() -> str:
    # A big, uniform JSON array json_crush would compress a LOT — the "large saving" bait.
    return "[" + ",".join(f'{{"id":{i},"name":"item_{i}"}}' for i in range(200)) + "]"


def _enabled_lossless_rule(**over) -> ClassRule:
    base = {
        "transform": "json_crush", "enabled": True, "min_bytes": 1, "ratio_floor": 0.0,
        "knobs": {}, "transform_version": "", "validator_id": None, "validator_version": "",
        "fidelity_class": "wire_canonicalization",
    }
    base.update(over)
    return ClassRule(**base)


def test_freeze_wall_beats_large_saving():
    """A frozen block ships its STORED bytes verbatim even though the live transform would compress
    it a lot. Freeze is the outermost wall — no optimize runs behind it (§5.1)."""
    content = _highly_crushable_json()
    stored = "FROZEN-PREFIX-BYTES"  # deliberately different from what the transform would emit
    em = decide(
        content, _policy_with(_enabled_lossless_rule()),
        context_bytes=13000, tool_name="Read", frozen=True, frozen_text=stored,
    )
    assert em.transformed is False
    assert em.reason == "frozen"
    assert em.text == stored  # stored bytes win over any saving the transform could have booked


def test_capability_wall_beats_large_saving(monkeypatch):
    """A LOSSY cell with no registered resolver ships raw (`capability_missing`) even on a maximally
    crushable block. The safety wall dominates the saving — deny-by-default."""
    monkeypatch.setattr(decide_mod, "_RESOLVERS", {}, raising=False)
    lossy = _enabled_lossless_rule(
        fidelity_class="ccr_retrieval", validator_id="json_entity_floor_v1", validator_version="1",
    )
    content = _highly_crushable_json()
    em = decide(content, _policy_with(lossy), context_bytes=13000, tool_name="Read")
    assert em.transformed is False
    assert em.reason == "capability_missing"
    assert em.text == content  # raw — the elided bytes couldn't be served back, so saving is refused


def test_not_addressable_wall_beats_large_saving():
    """A disabled cell (or a block under min_bytes) ships raw regardless of how compressible it is:
    the addressability wall is checked before any transform runs."""
    disabled = _enabled_lossless_rule(enabled=False)
    content = _highly_crushable_json()
    em = decide(content, _policy_with(disabled), context_bytes=13000, tool_name="Read")
    assert em.transformed is False
    assert em.reason == "not_addressable"
    assert em.text == content


def test_min_bytes_wall_beats_large_saving():
    """min_bytes is a hard wall: a block below it ships raw even though, per-byte, it's crushable.
    Pins that the size gate precedes the optimize step (not folded into a blended score)."""
    high_floor = _enabled_lossless_rule(min_bytes=1 << 20)  # 1 MiB floor — the block won't clear it
    content = _highly_crushable_json()  # a few KB — crushable but under the floor
    em = decide(content, _policy_with(high_floor), context_bytes=13000, tool_name="Read")
    assert em.transformed is False
    assert em.reason == "not_addressable"
    assert em.text == content


def test_optimize_runs_only_inside_the_feasible_set(monkeypatch):
    """The positive control: when EVERY hard wall passes (enabled, addressable, lossless→no resolver
    needed), the optimize criterion is finally allowed to act and the block is compressed. Proves the
    walls aren't just blocking everything — feasibility → optimize, exactly the intended ordering."""
    monkeypatch.setattr(decide_mod, "_RESOLVERS", {}, raising=False)
    em = decide(
        _highly_crushable_json(), _policy_with(_enabled_lossless_rule()),
        context_bytes=13000, tool_name="Read",
    )
    assert em.reason not in ("frozen", "capability_missing", "not_addressable")
    # feasible → the optimize step ran; on this maximally-crushable block it emits a compression.
    assert em.transformed is True
    assert em.reason == "emit"
    assert len(em.text) < len(_highly_crushable_json())
