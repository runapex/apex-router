"""Digest–artifact identity — compile and serve MUST run the same artifact form (pre-obfuscation).

`transform_digest(name)` hashes the INSTALLED transform module's SOURCE BYTES (policy.py — sha256
over `spec.origin`). `load_verified` (policy.py) recomputes it for every ENABLED cell and refuses a
policy whose sealed `transform_version` != the installed digest (Δ3). That is correct provenance —
but it makes a hard distribution invariant: **obfuscation rewrites source bytes, so a policy
compiled against the source checkout will FAIL that digest check on the obfuscated runtime** — dead
enabled cell (fail-closed, so the deployment is dead), or tempting an operator to "fix" it by
disabling the gate (catastrophic — the whole provenance chain).

The invariant, pinned here so a build that breaks it fails a TEST not a deployment:
  1. ROUND-TRIP: a policy sealed with digest == `transform_digest(name)` load_verifies on the SAME
     artifact. (Holds today; the pin is what guards it.)
  2. THE GATE ACTUALLY CATCHES an artifact-form change between compile and load — this is the
     obfuscation scenario simulated: if the module bytes differ at load from compile, load_verified
     REFUSES. So on the real obfuscated build, this suite passing IS the acceptance test that the
     digest chain survived (compile-then-obfuscate, or compile inside the obfuscated artifact).
  3. `transform_digest` reflects the LOADED module's bytes (identity), not a name or a constant.

DISTRIBUTION DOCTRINE (see the module note at bottom): compile and serve run the SAME artifact form.
Either obfuscate the wheel FIRST then compile against it, or ship the compiler inside the same
(obfuscated) artifact. Never compile on the source checkout and serve an obfuscated wheel.
"""
from __future__ import annotations

import hashlib

from apex_router.proxy_engine.policy import (
    CONTENT_CLASSES,
    ClassRule,
    ExpectedReport,
    InvalidPolicy,
    PolicyVersion,
    T2Policy,
    transform_digest,
)

# A real transform with a registered lossless rule path (json → json_crush) so an ENABLED cell is
# valid and its digest is checked at load.
_TRANSFORM = "json_crush"


def _policy_with_enabled_cell(digest: str) -> PolicyVersion:
    """A total policy with ONE enabled json/xl cell carrying the given transform digest — the cell
    whose digest load_verified checks. All other cells disabled/raw."""
    strata = ("xs", "s", "m", "l", "xl")
    raw = ClassRule(transform=None, enabled=False, min_bytes=1 << 30, ratio_floor=0.0)
    rules = {c: {st: raw for st in strata} for c in CONTENT_CLASSES}
    rules["json"]["xl"] = ClassRule(
        transform=_TRANSFORM, enabled=True, min_bytes=200, ratio_floor=0.1,
        retrieval_ceiling=0.05, knobs={"json_max_leaf": 300},
        transform_version=digest, validator_id="json_entity_floor_v1",
        validator_version="1", fidelity_class="ccr_retrieval",
    )
    return PolicyVersion(
        version=1, compiled_at=1.0, compiler_hash="h", corpus_hash="c", band=(6.0, 30.0),
        rules=rules, t2=T2Policy(consolidate_on=("ttl",), min_turn_count=5),
        expected=ExpectedReport(0.0, {}),
    ).sealed()


# 0 — transform_digest FAILS CLOSED on an unreadable/zip/absent origin (cross-validation): it must NOT
# escape a raw OSError into load_verified, and must NOT return a value matching a real sealed
# digest. It returns a distinctive UNREADABLE sentinel that a real 16-hex digest can never equal, so
# an enabled cell mismatches → refused. (Absent module → "" is separately refused as unsigned.)
def test_transform_digest_fails_closed_on_unreadable_origin(monkeypatch):
    import importlib.util

    class _ZipSpec:
        origin = "/nonexistent/obfuscated.zip/apex/pipeline/transforms/json_crush.pyc"

    monkeypatch.setattr(importlib.util, "find_spec", lambda _n: _ZipSpec())
    d = transform_digest(_TRANSFORM)
    # must be a non-raising, non-empty sentinel that no real sha256[:16] can equal
    assert d and not _looks_like_a_real_digest(d), (
        f"transform_digest on unreadable origin returned {d!r} — must be a distinctive unreadable "
        "sentinel (fail-closed), not a raw raise and not a value that could match a sealed digest"
    )


def _looks_like_a_real_digest(d: str) -> bool:
    return len(d) == 16 and all(c in "0123456789abcdef" for c in d)


# 1 + 3 — the digest is the installed module's byte identity, and the round-trip load_verifies
def test_digest_is_the_installed_module_byte_identity():
    import importlib.util
    spec = importlib.util.find_spec(f"apex_router.proxy_engine.pipeline.transforms.{_TRANSFORM}")
    assert spec is not None and spec.origin, f"{_TRANSFORM} transform module not importable"
    with open(spec.origin, "rb") as f:
        expected = hashlib.sha256(f.read()).hexdigest()[:16]
    assert transform_digest(_TRANSFORM) == expected, (
        "transform_digest is not sha256 of the installed source — the artifact-identity assumption "
        "the round-trip rests on is broken"
    )


def test_roundtrip_loads_when_compile_and_serve_share_the_artifact():
    # digest@compile == transform_digest(installed) — the same-artifact case (today, and the target
    # state for distribution). load_verified must ACCEPT.
    pol = _policy_with_enabled_cell(transform_digest(_TRANSFORM))
    loaded = PolicyVersion.load_verified(pol.to_dict())
    assert loaded.rules["json"]["xl"].enabled is True


# 2 — the gate CATCHES a genuine artifact CHANGE between seal-time and load-time (cross-validation). This ACTUALLY preserves a
# digest sealed against form-A, then makes transform_digest return a DIFFERENT value at load
# (form-B, the obfuscation), and asserts refusal — the real cross-artifact failure mode.
def test_gate_refuses_when_the_artifact_changes_between_seal_and_load(monkeypatch):
    import apex_router.proxy_engine.policy as pol_mod

    # SEAL TIME (artifact form A): capture the real installed digest and seal a policy against it.
    form_a = transform_digest(_TRANSFORM)
    assert _looks_like_a_real_digest(form_a)
    pol = _policy_with_enabled_cell(form_a)  # a valid, load-verifiable policy on form A

    # It round-trips on form A (control — proves the policy itself is well-formed).
    assert PolicyVersion.load_verified(pol.to_dict()).rules["json"]["xl"].enabled is True

    # LOAD TIME (artifact form B — obfuscation rewrote the transform's bytes → a different digest).
    form_b = "abcdef0123456789"  # a DIFFERENT real-shaped digest; the obfuscated build's bytes
    assert form_b != form_a
    monkeypatch.setattr(pol_mod, "transform_digest", lambda _n: form_b)
    try:
        PolicyVersion.load_verified(pol.to_dict())
        raise AssertionError(
            "load_verified ACCEPTED a form-A policy on form B — a source-compiled policy would be "
            "silently served on (or brick) the obfuscated wheel. The digest gate must refuse."
        )
    except InvalidPolicy as e:
        assert "digest mismatch" in str(e)


# The obfuscation acceptance criterion, non-tautological: a policy sealed against form A, loaded on
# form A, round-trips; loaded on form B, refuses. Both directions in one test so neither is vacuous.
# Run against the obfuscated wheel with form A == obfuscated digest → the compile-then-serve path
# must round-trip (proves the SAME-FORM ordering; a DIFFERENT form still refuses).
def test_obfuscation_acceptance_same_form_roundtrips_different_form_refuses(monkeypatch):
    import apex_router.proxy_engine.policy as pol_mod

    form_a = transform_digest(_TRANSFORM)
    assert form_a and form_a != _DIGEST_UNREADABLE_SENTINEL
    pol = _policy_with_enabled_cell(form_a)
    # SAME form → accept
    loaded = PolicyVersion.load_verified(pol.to_dict())
    assert loaded.rules["json"]["xl"].transform_version == form_a
    # DIFFERENT form (obfuscation changed bytes) → refuse
    monkeypatch.setattr(pol_mod, "transform_digest", lambda _n: "fedcba9876543210")
    try:
        PolicyVersion.load_verified(pol.to_dict())
        raise AssertionError("acceptance test is vacuous: a different-form policy was NOT refused")
    except InvalidPolicy:
        pass


# single source of truth for the sentinel (don't mirror the literal)
from apex_router.proxy_engine.policy import _DIGEST_UNREADABLE as _DIGEST_UNREADABLE_SENTINEL  # noqa: E402
