"""Authority defaults REFUSE — the F5 plane invariant made mechanical at the type level.

The generalization of the CorpusStats.canonical fail-open catch (Codex, closure review): **a default
value is the policy for every caller who doesn't state one**, so an authority-class boolean whose
default is the PERMISSIVE value is fail-open no matter what the mechanism around it says. The fix
closed the instance; this test closes the CLASS — it enumerates every authority-class boolean that
carries a default and asserts the default is the refusing one, so a future authority flag cannot
regress to permissive without a test failing.

"Authority-class" = the boolean gates a security/safety decision (signing, cache safety, capability,
admission, epoch downgrade). Its safe default REFUSES; only an explicit assertion grants. Mechanical
state booleans (a `frozen` block flag, `is_new`, a `_closed` latch, offline econ modelling) are NOT
authority and are out of scope — they're documented in `MECHANICAL` below so the list is a decision,
not an omission.

If you ADD an authority boolean with a default, add it to `AUTHORITY` with the refusing value and it
gets pinned here. If a review is unsure whether a new boolean is authority, the standing question is
the one from the closure log: "is this a policy or a bound?" — a flag that gates who-may-sign /
what-may-emit / what-may-merge is authority; a flag that records a state is not.
"""
from __future__ import annotations

import dataclasses
import inspect

from apex_router.proxy_engine.policy import ClassRule, PolicyRegistry
from apex_router.proxy_engine.tuner.compiler import compile_policy
from fixtures.build_replay_corpus import CorpusStats


# (label, callable returning the resolved default, expected refusing value). Each entry names an
# authority-class boolean and the value its default MUST take to fail closed.
def _param_default(fn, name):
    return inspect.signature(fn).parameters[name].default


def _field_default(cls, name):
    for f in dataclasses.fields(cls):
        if f.name == name:
            if f.default is not dataclasses.MISSING:
                return f.default
            return dataclasses.MISSING
    raise AssertionError(f"{cls.__name__} has no field {name}")


AUTHORITY = [
    # signing grade — a compile may not CLAIM evidence grade unless explicitly asked (else a probe
    # bundle is quoted as signed). Default must be False.
    ("compile_policy.evidence_grade",
     lambda: _param_default(compile_policy, "evidence_grade"), False),
    # epoch downgrade — activate() may not accept an older epoch unless rollback is explicitly set.
    ("PolicyRegistry.activate.rollback",
     lambda: _param_default(PolicyRegistry.activate, "rollback"), False),
    # corpus provenance — an unlabeled corpus may not authorize an evidence-grade sign (the fixed
    # fail-open hole). Default must be False.
    ("CorpusStats.canonical", lambda: _field_default(CorpusStats, "canonical"), False),
]

# Authority booleans that are REQUIRED (no default) — even stronger than a refusing default: you
# cannot construct the object without stating the value, so there is no permissive default to leak.
AUTHORITY_REQUIRED = [
    ("ClassRule.enabled", ClassRule, "enabled"),
]

# Documented NON-authority booleans (mechanical state / offline modelling) — recorded so the audit
# is a decision, not an omission. If one of these ever starts gating a safety decision, move it up.
MECHANICAL = (
    "decide.frozen (a block-state input, not a gate)",
    "Match.is_new (records a matcher outcome)",
    "OffloadPool._closed (a lifecycle latch)",
    "block_econs.enabled (offline reference-arm modelling, never a runtime gate)",
    "Store check_same_thread (sqlite threading, not policy)",
    "PolicyVersion.seal='' (empty → verify() False; a string not a bool, but fails closed)",
)


def test_authority_boolean_defaults_refuse():
    """Every authority-class boolean with a default refuses by default (fail-closed)."""
    for label, get_default, refusing in AUTHORITY:
        actual = get_default()
        assert actual is refusing, (
            f"{label}: authority default is {actual!r}, must be {refusing!r} — a default is the "
            f"policy for every caller who doesn't state one, so an authority flag defaulting to "
            f"the permissive value is fail-OPEN (F5: authority fails closed)."
        )


def test_authority_required_booleans_have_no_default():
    """Authority booleans that should be REQUIRED carry no default — unconstructable-without-stating
    is strictly stronger than a refusing default."""
    for label, cls, name in AUTHORITY_REQUIRED:
        assert _field_default(cls, name) is dataclasses.MISSING, (
            f"{label}: gained a default — an admission-authority flag must be REQUIRED so a caller "
            f"cannot construct a rule without stating enabled (else the default admits)."
        )


def test_evidence_grade_default_actually_fails_closed_end_to_end():
    """Behavioural pin (not just the literal): the evidence_grade default path does not sign an
    evidence bundle from an unlabeled corpus — the audit's claim, exercised."""
    import json

    from apex_router.proxy_engine.tuner.replay import Request

    corpus = []
    for t in range(6):
        c = json.dumps([{"id": i} for i in range(200)], indent=2).encode()
        corpus.append(Request("s", c, max(1, len(c) // 4), ts=float(t), model="claude-opus-4-8"))
    # default call (no evidence_grade) compiles fine but is stamped non-evidence
    res = compile_policy(corpus, version=1, compiled_at=1e9)
    assert res.evidence.get("evidence_grade") is False
