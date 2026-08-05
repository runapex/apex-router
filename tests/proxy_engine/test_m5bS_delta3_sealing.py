"""Δ3 — knob & digest sealing (roadmap §1). ClassRule seals the exact knobs, transform digest, and
validator id it was compiled against; the runtime rejects a policy whose digests don't match the
installed code, and passes the sealed knobs (never `{}`) into the transform.

Why: a code-level change to `max_leaf` or the marker format changes emitted bytes under an UNCHANGED
signed policy — breaking cross-session reproducibility and contaminating G (compiler `expected` was
priced at compile-time knob values the runtime no longer runs). module.run(block,{}) is the symptom.
"""

from __future__ import annotations

import pytest

from apex_router.proxy_engine.pipeline import decide as decide_mod
from apex_router.proxy_engine.pipeline.decide import decide
from apex_router.proxy_engine.policy import ClassRule, InvalidPolicy, PolicyVersion, transform_digest

# ── the new sealed fields exist and fold into the seal ───────────────────────────────────────────


def test_classrule_carries_sealed_knobs_and_digests():
    r = ClassRule(
        transform="json_crush",
        enabled=True,
        min_bytes=200,
        ratio_floor=0.1,
        retrieval_ceiling=0.05,
        knobs={"json_max_leaf": 300},
        transform_version="abc123",
        validator_id="json_entity_floor_v1",
        validator_version="1",
        fidelity_class="ccr_retrieval",
    )
    assert r.knobs == {"json_max_leaf": 300}
    assert r.transform_version == "abc123"
    assert r.validator_id == "json_entity_floor_v1"
    assert r.fidelity_class == "ccr_retrieval"
    # the new fields are in the canonical dict → they get signed
    d = r.to_dict()
    for k in ("knobs", "transform_version", "validator_id", "validator_version", "fidelity_class"):
        assert k in d


def test_knobs_change_the_seal(_policy_factory):
    """Two policies identical except for a rule's knobs must seal DIFFERENTLY — otherwise a knob
    change is invisible to provenance."""
    p_a = _policy_factory(knobs={"json_max_leaf": 200}).sealed()
    p_b = _policy_factory(knobs={"json_max_leaf": 300}).sealed()
    assert p_a.seal != p_b.seal


# ── decide() passes the sealed knobs, not {} ─────────────────────────────────────────────────────


def test_decide_passes_sealed_knobs_not_empty(monkeypatch):
    """The runtime must call module.run(block, rule.knobs). Capture the knobs the transform sees."""
    seen = {}

    class _StubModule:
        name = "stub"

        def applies(self, block):
            return True

        def run(self, block, knobs):
            seen["knobs"] = knobs
            from apex_router.proxy_engine.pipeline.transforms.base import Rendering

            return Rendering(text=block.content[:10], fidelity="wire_canonicalization")

    monkeypatch.setitem(decide_mod._BY_NAME, "stub", _StubModule())
    digest = "deadbeef"
    rule = ClassRule(
        transform="stub",
        enabled=True,
        min_bytes=1,
        ratio_floor=0.0,
        knobs={"k": 7},
        transform_version=digest,
        fidelity_class="wire_canonicalization",
    )
    policy = _total_policy_with(rule, transform_name="stub", digest=digest)
    decide("x" * 50, policy, context_bytes=13000, tool_name="Read")
    assert seen["knobs"] == {"k": 7}, "decide() must thread rule.knobs into the transform, not {}"


# ── the runtime rejects a policy whose transform digest ≠ installed code ──────────────────────────


def test_bundle_rejects_transform_digest_mismatch(_policy_factory):
    """A policy sealed against a stale transform digest must fail load — installed code no longer
    matches what the compiler priced."""
    policy = _policy_factory(
        knobs={"json_max_leaf": 200}, transform_version="STALE_DIGEST"
    ).sealed()
    d = policy.to_dict()
    with pytest.raises(InvalidPolicy):
        PolicyVersion.load_verified(d)


def test_bundle_accepts_matching_transform_digest(_policy_factory):
    """A policy sealed against the REAL installed digest loads cleanly."""
    real = transform_digest("json_crush")
    policy = _policy_factory(knobs={"json_max_leaf": 200}, transform_version=real).sealed()
    loaded = PolicyVersion.load_verified(policy.to_dict())
    assert loaded.rules["json"]["xl"].transform_version == real


def test_transform_digest_changes_when_module_changes():
    """The digest is a content hash of the installed transform module — deterministic, and different
    for different modules (so a swap or edit is detectable)."""
    d1 = transform_digest("json_crush")
    d2 = transform_digest("compaction")
    assert d1 and d2 and d1 != d2
    assert transform_digest("json_crush") == d1  # deterministic


def test_disabled_rules_not_digest_checked(_policy_factory):
    """A disabled cell carries no live transform, so its (absent) digest must not block load — only
    enabled cells are checked against installed code."""
    real = transform_digest("json_crush")
    # xl enabled with the right digest; everything else disabled with empty digest
    policy = _policy_factory(knobs={"json_max_leaf": 200}, transform_version=real).sealed()
    loaded = PolicyVersion.load_verified(policy.to_dict())
    assert loaded is not None


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────


def _total_policy_with(rule: ClassRule, *, transform_name: str, digest: str) -> PolicyVersion:
    """A minimal total policy (all classes present) with one enabled cell = `rule`, sealed, with the
    transform digest matching so load_verified passes. Uses stub-safe digests where needed."""
    from apex_router.proxy_engine.policy import CONTENT_CLASSES, ExpectedReport, T2Policy

    strata = ("xs", "s", "m", "l", "xl")
    raw = ClassRule(transform=None, enabled=False, min_bytes=1 << 30, ratio_floor=0.0)
    rules = {c: {st: raw for st in strata} for c in CONTENT_CLASSES}
    # classify("x"*50) == "prose" → put the enabled stub rule there so decide() routes to it.
    rules["prose"] = {st: rule for st in strata}
    pol = PolicyVersion(
        version=1,
        compiled_at=1.0,
        compiler_hash="h",
        corpus_hash="c",
        band=(6.0, 30.0),
        rules=rules,
        t2=T2Policy(consolidate_on=("ttl",), min_turn_count=5),
        expected=ExpectedReport(0.0, {}),
    )
    return pol.sealed()


@pytest.fixture
def _policy_factory():
    """Factory for a total, json/xl-enabled policy parameterized by knobs + transform_version."""
    from apex_router.proxy_engine.policy import CONTENT_CLASSES, ExpectedReport, T2Policy

    def make(*, knobs, transform_version=None):
        if transform_version is None:
            transform_version = transform_digest("json_crush")
        strata = ("xs", "s", "m", "l", "xl")
        raw = ClassRule(transform=None, enabled=False, min_bytes=1 << 30, ratio_floor=0.0)
        rules = {c: {st: raw for st in strata} for c in CONTENT_CLASSES}
        rules["json"]["xl"] = ClassRule(
            transform="json_crush",
            enabled=True,
            min_bytes=200,
            ratio_floor=0.1,
            retrieval_ceiling=0.05,
            knobs=dict(knobs),
            transform_version=transform_version,
            validator_id="json_entity_floor_v1",
            validator_version="1",
            fidelity_class="ccr_retrieval",
        )
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

    return make
