"""Δ8 — PolicyBundle compatibility metadata (roadmap §1). The signed policy gains validity window
(`valid_from`/`expires_at`), a monotonic `policy_epoch`, and a required corpus fingerprint; a small
`PolicyRegistry` provides ATOMIC activation with epoch-monotonicity enforcement.

Why: a runtime must never (a) run an expired policy, (b) silently downgrade to an older epoch w/o a
signed rollback, or (c) observe a half-swapped bundle mid-activation. Bundle provenance without a
validity/epoch contract is a signed artifact with no lifecycle.
"""

from __future__ import annotations

import pytest

from apex_router.proxy_engine.policy import (
    CONTENT_CLASSES,
    ClassRule,
    ExpectedReport,
    InvalidPolicy,
    PolicyRegistry,
    PolicyVersion,
    T2Policy,
)


def _policy(*, epoch=1, valid_from=0.0, expires_at=1e18, corpus_hash="c0ffee", version=1):
    strata = ("xs", "s", "m", "l", "xl")
    raw = ClassRule(transform=None, enabled=False, min_bytes=1 << 30, ratio_floor=0.0)
    rules = {c: {st: raw for st in strata} for c in CONTENT_CLASSES}
    return PolicyVersion(
        version=version,
        compiled_at=1.0,
        compiler_hash="h",
        corpus_hash=corpus_hash,
        band=(6.0, 30.0),
        rules=rules,
        t2=T2Policy(consolidate_on=("ttl",), min_turn_count=5),
        expected=ExpectedReport(0.0, {}),
        policy_epoch=epoch,
        valid_from=valid_from,
        expires_at=expires_at,
    ).sealed()


# ── the new fields fold into the seal ──


def test_metadata_fields_are_sealed():
    a = _policy(epoch=1)
    b = _policy(epoch=2)
    assert a.seal != b.seal  # epoch change → different seal
    assert _policy(expires_at=10.0).seal != _policy(expires_at=20.0).seal


def test_load_verified_roundtrips_metadata():
    p = _policy(epoch=3, valid_from=100.0, expires_at=200.0)
    loaded = PolicyVersion.load_verified(p.to_dict())
    assert loaded.policy_epoch == 3
    assert loaded.valid_from == 100.0 and loaded.expires_at == 200.0


# ── expiry ──


def test_expired_policy_rejected_at_activation():
    reg = PolicyRegistry()
    p = _policy(valid_from=0.0, expires_at=100.0)
    with pytest.raises(InvalidPolicy):
        reg.activate(p, now=150.0)  # now past expires_at


def test_not_yet_valid_policy_rejected():
    reg = PolicyRegistry()
    p = _policy(valid_from=100.0, expires_at=200.0)
    with pytest.raises(InvalidPolicy):
        reg.activate(p, now=50.0)  # now before valid_from


def test_in_window_policy_activates():
    reg = PolicyRegistry()
    p = _policy(valid_from=0.0, expires_at=200.0)
    reg.activate(p, now=100.0)
    assert reg.current() is p


# ── epoch monotonicity ───────────────────────────────────────────────────────────────────────────


def test_older_epoch_rejected_without_signed_rollback():
    reg = PolicyRegistry()
    reg.activate(_policy(epoch=5), now=1.0)
    with pytest.raises(InvalidPolicy):
        reg.activate(_policy(epoch=4), now=2.0)  # downgrade without authorization


def test_same_epoch_rejected():
    reg = PolicyRegistry()
    reg.activate(_policy(epoch=5), now=1.0)
    with pytest.raises(InvalidPolicy):
        reg.activate(_policy(epoch=5), now=2.0)  # re-activating same epoch is ambiguous


def test_newer_epoch_activates():
    reg = PolicyRegistry()
    reg.activate(_policy(epoch=5), now=1.0)
    reg.activate(_policy(epoch=6), now=2.0)
    assert reg.current().policy_epoch == 6


def test_signed_rollback_allows_older_epoch():
    reg = PolicyRegistry()
    reg.activate(_policy(epoch=5), now=1.0)
    reg.activate(_policy(epoch=4), now=2.0, rollback=True)  # explicit authorized downgrade
    assert reg.current().policy_epoch == 4


# ── atomic activation ──


def test_failed_activation_leaves_current_untouched():
    """A rejected activation must not swap out the live policy — readers never see a half-swap."""
    reg = PolicyRegistry()
    good = _policy(epoch=5)
    reg.activate(good, now=1.0)
    with pytest.raises(InvalidPolicy):
        reg.activate(_policy(epoch=3), now=2.0)  # rejected (older epoch)
    assert reg.current() is good  # unchanged


# ── corpus fingerprint required ──────────────────────────────────────────────────────────────────


def test_missing_corpus_fingerprint_rejected():
    with pytest.raises(InvalidPolicy):
        PolicyVersion.load_verified(_policy(corpus_hash="").to_dict())


# ── cross-validation: empty sealed fields on an ENABLED rule fail CLOSED, not open ──

def _policy_with_json_rule(rule):
    strata = ("xs", "s", "m", "l", "xl")
    raw = ClassRule(transform=None, enabled=False, min_bytes=1 << 30, ratio_floor=0.0)
    rules = {c: {st: raw for st in strata} for c in CONTENT_CLASSES}
    rules["json"]["s"] = rule
    return PolicyVersion(
        version=1, compiled_at=1.0, compiler_hash="h", corpus_hash="c0ffee",
        band=(6.0, 30.0), rules=rules,
        t2=T2Policy(consolidate_on=("ttl",), min_turn_count=5),
        expected=ExpectedReport(0.0, {})).sealed()


def test_enabled_rule_with_empty_transform_version_rejected():
    """cross-validation: an enabled rule that names a transform but seals NO transform_version is forged/
    malformed (the compiler always seals the digest) — it must be refused, not silently skip the
    digest gate (fail-closed provenance)."""
    rule = ClassRule(transform="json_crush", enabled=True, min_bytes=200, ratio_floor=0.1,
                     transform_version="", fidelity_class="ccr_retrieval")
    with pytest.raises(InvalidPolicy):
        PolicyVersion.load_verified(_policy_with_json_rule(rule).to_dict())


def test_enabled_rule_with_empty_fidelity_class_rejected():
    """cross-validation: likewise an enabled transform rule with an empty fidelity_class is refused — the
    capability/taxonomy gate must not be bypassable by leaving the field blank."""
    from apex_router.proxy_engine.policy import transform_digest
    rule = ClassRule(transform="json_crush", enabled=True, min_bytes=200, ratio_floor=0.1,
                     transform_version=transform_digest("json_crush"), fidelity_class="")
    with pytest.raises(InvalidPolicy):
        PolicyVersion.load_verified(_policy_with_json_rule(rule).to_dict())
