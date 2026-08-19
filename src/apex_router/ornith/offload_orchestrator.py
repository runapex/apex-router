"""Frontier-orchestrated offload of ONE delegable sub-task.

The frontier decomposes a task and hands each delegable, gateable sub-task here. This module:
  1. consults route_advise for a cost verdict on the sub-task TYPE,
  2. routes local ONLY on COST_FAVORS_CHEAP_START (else keeps the caller default = escalate),
  3. dispatches to the offload lane and gets a LaneResult,
  4. asks the FINAL adjudicator (gate, and in later stages gate ∧ cross-validation) whether to accept,
  5. writes the route-log outcome ONLY AFTER adjudication.

Two corrected invariants from the design's cross-validation pass:
  F3 — the routing decision DEFERS to route_advise's verdict; it does NOT reimplement the Wilson-bound
       threshold. INCONCLUSIVE / COST_FAVORS_HEAVY_START keep the caller default (escalate), which is
       the safe common case until evidence accrues.
  F1 — the two-authority wall's real invariant: the route LABEL is written by the final adjudicator,
       never by the gate alone and never before adjudication. A gate-pass that cross-validation later
       rejects is logged 'escalated', not 'ok' — nothing the pipeline rejects trains the prior toward
       local. The prior is advisory for ROUTING only; it is never an input to ACCEPTANCE.

All external calls are injectable seams so the invariants are unit-testable without a live model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# route_advise's verdict string for "cheap-first is significantly cheaper" — the ONLY verdict that
# licenses a local attempt. Imported lazily in the default seam to keep this module import-light.
_CHEAP_START = "cost_favors_cheap_start"


@dataclass
class SubTask:
    type: str
    payload: dict = field(default_factory=dict)
    verifier: Callable | None = None   # reserved for Stage 2 per-type verifiers


def _default_advise(task_type: str) -> dict:
    """Compose the REAL route_advise API (never reimplement the threshold): read_rates() gives the
    per-type (n, escalated) counts; advise_one() returns the verdict record."""
    from .. import route_advise
    from ..route_log import read_rates
    cell = read_rates().get(task_type, {"n": 0, "escalated": 0})
    return route_advise.advise_one(int(cell.get("n", 0)), int(cell.get("escalated", 0)))


def _default_dispatch(subtask: SubTask):
    from .dispatch import run_job
    return run_job({"lane": subtask.type, **subtask.payload})


def _default_adjudicate(subtask: SubTask, lane_result) -> bool:
    """Stage 1: acceptance follows the lane's OWN contract — accept only a result that passed a real
    correctness gate AND is not itself asking to escalate: `ok ∧ gated ∧ not escalate` (Codex xval).

    `ok` alone is NOT sufficient: a lane may set `ok=True` while `escalate=True, gated=False` — the
    review pre-filter does exactly this (findings→ok=True, but it runs no correctness gate and always
    escalates for triage). Accepting on `ok` alone would log an ungated result 'ok' and train the
    routing prior toward local on work that never earned acceptance — the two-authority corruption
    the design forbids. Stage 2 injects a cross-validation-aware adjudicator here WITHOUT changing the
    orchestrator; this seam keeps the label-after-adjudication invariant (F1) intact as it grows."""
    ok = bool(getattr(lane_result, "ok", False))
    gated = bool(getattr(lane_result, "gated", False))
    escalate = bool(getattr(lane_result, "escalate", False))
    return ok and gated and not escalate


def composed_adjudicate(subtask: SubTask, lane_result, *, ground_fn=None, xval_fn=None) -> bool:
    """Stage 2 default adjudicator: accept iff ALL applicable necessary gates pass:
      1. the lane contract (`ok ∧ gated ∧ not escalate`, Stage 1),
      2. the sub-task type's verifier (applicable AND passed), and
      3. — when provided — the SEMANTIC cross-validation gate `xval_fn(subtask, lane_result) -> bool`.

    The verifier is NECESSARY-not-sufficient (the grounding oracle is semantic-blind — a real-but-
    unrelated cite still grounds; spec F7). So for a COMMITTED result the caller passes `xval_fn` (the
    cross-validate-codex semantic gate) and it must ALSO pass — this is what actually prevents a
    semantically-false-but-grounded result being accepted (Codex xval F1). For a NON-committed
    sub-result the caller omits `xval_fn` and the necessary gates are final, per the spec. Either an
    absent verifier or an inapplicable verifier means NOT accepted (escalate)."""
    if not _default_adjudicate(subtask, lane_result):
        return False
    from .verifiers import verify
    v = verify(subtask.type, lane_result, ground_fn=ground_fn)
    if not (v.applicable and v.passed):
        return False
    if xval_fn is not None and not bool(xval_fn(subtask, lane_result)):
        return False   # semantic cross-validation rejected a necessary-gate pass
    return True


def _default_log(task_type: str, outcome: str) -> None:
    from ..route_log import log_outcome
    log_outcome(task_type, "qwen3.8", outcome)   # outcome ∈ {"ok","escalated"}


def orchestrate(subtask: SubTask, *, advise_fn: Callable = _default_advise,
                dispatch_fn: Callable = _default_dispatch,
                adjudicate_fn: Callable = composed_adjudicate,
                log_fn: Callable = _default_log) -> dict:
    """Run one sub-task through the loop. Returns
    {routed: "local"|"frontier", accepted: bool, escalated: bool, output: str}.

    - routed="frontier" means the loop declined to try local (verdict not COST_FAVORS_CHEAP_START);
      the frontier owns the sub-task. Logged 'escalated'.
    - routed="local" + accepted means the local result cleared final adjudication. Logged 'ok'.
    - routed="local" + not accepted means local was tried but the adjudicator rejected it. Logged
      'escalated'; the frontier redoes it (and the caller captures a correction, out of this seam).
    """
    verdict = (advise_fn(subtask.type) or {}).get("verdict")
    if verdict != _CHEAP_START:
        log_fn(subtask.type, "escalated")          # keep the caller default: escalate
        return {"routed": "frontier", "accepted": False, "escalated": True, "output": ""}

    lane_result = dispatch_fn(subtask)
    accepted = bool(adjudicate_fn(subtask, lane_result))   # FINAL authority (gate ∧ later xval)
    log_fn(subtask.type, "ok" if accepted else "escalated")   # label AFTER adjudication (F1)
    return {
        "routed": "local",
        "accepted": accepted,
        "escalated": not accepted,
        "output": getattr(lane_result, "output", "") if accepted else "",
    }
