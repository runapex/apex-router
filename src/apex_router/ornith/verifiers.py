"""Per-type verifiers for offloaded sub-tasks — deterministic NECESSARY-condition gates.

A verifier answers "did this local result pass the checkable precondition for its type?" — NOT "is
it semantically correct" (spec F7). A grounding pass means the cited file:line exist, not that the
claim is true. So a verifier PASS is a necessary gate that lets a result be provisionally accepted;
the semantic trust gate is cross-validation downstream. A type with no verifier is un-offloadable
(the composed adjudicator escalates it, never accepts).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class VerifierResult:
    passed: bool
    applicable: bool     # False when this verifier cannot judge the result -> escalate, don't accept
    detail: str = ""


def _verify_codegen(lane_result, **_) -> VerifierResult:
    # codegen's real gate (caller tests) already ran inside the lane; trust its gated verdict. NOT
    # applicable when the lane wasn't gated (no correctness gate ran -> nothing to trust).
    ok = bool(getattr(lane_result, "ok", False))
    gated = bool(getattr(lane_result, "gated", False))
    escalate = bool(getattr(lane_result, "escalate", False))
    passed = ok and gated and not escalate
    return VerifierResult(passed=passed, applicable=gated,
                          detail="codegen lane gate" if gated else "codegen not gated")


def _verify_citations(lane_result, *, ground_fn: Callable | None = None, **_) -> VerifierResult:
    # citation/search/extraction: every file:line the output cites must ground to real code. This is
    # a NECESSARY check only — the oracle is semantic-blind (a real-but-unrelated cite still grounds),
    # so a pass means "citations are real", not "claim is true". Not applicable when nothing groundable.
    from ..codeqa.ground_claims import ground_text
    gf = ground_fn or ground_text
    g = gf(getattr(lane_result, "output", "") or "")
    if not getattr(g, "applicable", False):
        return VerifierResult(passed=False, applicable=False, detail="no groundable citation")
    # Require EVERY citation to be 'grounded' — not merely "at least one grounded and none stale"
    # (Codex xval F3: that laxer check passed a result mixing a real cite with an unverified/advisory
    # one). A single stale/unverified cite means the result cited something the oracle can't confirm.
    cites = getattr(g, "citations", None)
    if cites is not None:
        passed = bool(cites) and all(getattr(c, "verdict", "") == "grounded" for c in cites)
    else:
        # fall back to the aggregate flags when a fake verdict object exposes no citation list.
        passed = bool(getattr(g, "has_grounded", False)) and not bool(getattr(g, "has_problem", False))
    return VerifierResult(passed=passed, applicable=True,
                          detail="all citations grounded" if passed else "stale/unverified/ungrounded cite")


# task_type -> verifier fn. Types absent here are un-offloadable (auto-escalate).
_REGISTRY: dict[str, Callable[..., VerifierResult]] = {
    "codegen": _verify_codegen,
    "citation": _verify_citations,
    "search": _verify_citations,
    "extraction": _verify_citations,
}

# DISPATCH WIRING (Stage 2.5, closes Codex xval F2): a verifier only gates REAL traffic if
# `dispatch.run_job` produces that lane. run_job now routes codegen (in-lane test gate) AND
# citation/search/extraction (verifier-gated: no in-lane gate, the grounding verifier is the gate,
# applied by offload_orchestrator.composed_adjudicate). So every HAS_VERIFIER type reaches its verifier
# end-to-end; PENDING_DISPATCH is empty. Keep this in lock-step with dispatch.run_job's lanes and
# offload_orchestrator._VERIFIER_GATED.
DISPATCHABLE_TYPES = frozenset({"codegen", "citation", "search", "extraction"})
HAS_VERIFIER = frozenset(_REGISTRY)
PENDING_DISPATCH = HAS_VERIFIER - DISPATCHABLE_TYPES      # now empty — all verifiers gate live traffic


def verify(task_type: str, lane_result, *, ground_fn: Callable | None = None) -> VerifierResult:
    """Run the registered verifier for `task_type`. Unknown/ungateable type -> not applicable, so the
    composed adjudicator escalates rather than accepting an unverified result."""
    fn = _REGISTRY.get(task_type)
    if fn is None:
        return VerifierResult(passed=False, applicable=False, detail=f"no verifier for {task_type!r}")
    return fn(lane_result, ground_fn=ground_fn)
