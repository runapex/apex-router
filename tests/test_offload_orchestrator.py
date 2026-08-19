"""Tests for the frontier-orchestrated offload of ONE delegable sub-task.

Pins the two corrected invariants from the design's Codex refute-pass:
  F3 — route local ONLY on route_advise verdict COST_FAVORS_CHEAP_START; INCONCLUSIVE / HEAVY keep
       the caller default (escalate). The orchestrator must NOT reimplement the Wilson threshold.
  F1 — the route LABEL is written only AFTER final adjudication: a gate-pass that the adjudicator
       (gate ∧ later cross-validation) rejects is logged 'escalated', never 'ok'.
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith.offload_orchestrator import (  # noqa: E402
    orchestrate, SubTask, composed_adjudicate,
)


class _LR:  # stand-in LaneResult
    def __init__(self, ok, escalate, output="", usage=None, gated=True):
        self.ok, self.escalate, self.output, self.usage = ok, escalate, output, usage
        self.gated = gated


class TestOrchestrator(unittest.TestCase):
    def test_inconclusive_verdict_keeps_default_escalate(self):
        # F3: INCONCLUSIVE must NOT offload; the lane must never be dispatched.
        logged = []
        def _no_dispatch(st):
            raise AssertionError("must not dispatch local on INCONCLUSIVE")
        r = orchestrate(
            SubTask(type="codegen", payload={}),
            advise_fn=lambda tt: {"verdict": "inconclusive"},
            dispatch_fn=_no_dispatch,
            adjudicate_fn=lambda st, lr: True,
            log_fn=lambda tt, outcome: logged.append((tt, outcome)),
        )
        self.assertEqual(r["routed"], "frontier")
        self.assertTrue(r["escalated"])
        self.assertEqual(logged, [("codegen", "escalated")])

    def test_heavy_start_verdict_keeps_default_escalate(self):
        logged = []
        r = orchestrate(
            SubTask(type="codegen", payload={}),
            advise_fn=lambda tt: {"verdict": "cost_favors_heavy_start"},
            dispatch_fn=lambda st: (_ for _ in ()).throw(AssertionError("no dispatch")),
            adjudicate_fn=lambda st, lr: True,
            log_fn=lambda tt, outcome: logged.append((tt, outcome)),
        )
        self.assertEqual(r["routed"], "frontier")
        self.assertEqual(logged, [("codegen", "escalated")])

    def test_gatepass_but_adjudication_rejects_logs_escalated(self):
        # F1: a gate-pass that the FINAL adjudicator (e.g. later cross-validation) rejects must log
        # 'escalated', never 'ok' — nothing the pipeline rejects trains the prior toward local.
        logged = []
        r = orchestrate(
            SubTask(type="codegen", payload={}),
            advise_fn=lambda tt: {"verdict": "cost_favors_cheap_start"},
            dispatch_fn=lambda st: _LR(ok=True, escalate=False, output="local ans"),
            adjudicate_fn=lambda st, lr: False,   # cross-validation later REJECTS it
            log_fn=lambda tt, outcome: logged.append((tt, outcome)),
        )
        self.assertFalse(r["accepted"])
        self.assertTrue(r["escalated"])
        self.assertEqual(r["output"], "")         # a rejected result is not returned as accepted
        self.assertEqual(logged, [("codegen", "escalated")])

    def test_cheap_start_and_adjudication_accepts_logs_ok(self):
        logged = []
        r = orchestrate(
            SubTask(type="codegen", payload={}),
            advise_fn=lambda tt: {"verdict": "cost_favors_cheap_start"},
            dispatch_fn=lambda st: _LR(ok=True, escalate=False, output="local ans"),
            adjudicate_fn=lambda st, lr: True,
            log_fn=lambda tt, outcome: logged.append((tt, outcome)),
        )
        self.assertTrue(r["accepted"])
        self.assertEqual(r["routed"], "local")
        self.assertEqual(r["output"], "local ans")
        self.assertEqual(logged, [("codegen", "ok")])

    def test_default_adjudicate_is_the_gate_verdict(self):
        # With no adjudicate_fn injected, the default authority is the lane's own gate.
        logged = []
        r = orchestrate(
            SubTask(type="codegen", payload={}),
            advise_fn=lambda tt: {"verdict": "cost_favors_cheap_start"},
            dispatch_fn=lambda st: _LR(ok=False, escalate=True, output="bad"),
            log_fn=lambda tt, outcome: logged.append((tt, outcome)),
        )
        self.assertFalse(r["accepted"])
        self.assertEqual(logged, [("codegen", "escalated")])

    def test_default_adjudicate_honors_lane_escalate_and_gated(self):
        # Codex xval: a lane may return ok=True WHILE marking escalate=True / gated=False (the review
        # pre-filter does exactly this — findings=True → ok=True, but it always escalates for triage
        # and runs no correctness gate). The default adjudicator must NOT accept such a result on
        # `ok` alone; accept only ok ∧ gated ∧ not escalate.
        logged = []
        lr = _LR(ok=True, escalate=True, output="findings")
        lr.gated = False
        r = orchestrate(
            SubTask(type="review", payload={}),
            advise_fn=lambda tt: {"verdict": "cost_favors_cheap_start"},
            dispatch_fn=lambda st: lr,
            log_fn=lambda tt, outcome: logged.append((tt, outcome)),
        )
        self.assertFalse(r["accepted"])                 # ungated + escalate → NOT accepted
        self.assertEqual(logged, [("review", "escalated")])


def _fake_ground(applicable, has_problem, has_grounded):
    return lambda text: type("G", (), {"applicable": applicable, "has_problem": has_problem,
                                       "has_grounded": has_grounded})()


class TestComposedAdjudicator(unittest.TestCase):
    def test_accept_needs_both_lane_contract_and_verifier(self):
        # codegen: passed the lane gate (ok∧gated∧not-escalate) AND its verifier -> accepted.
        lr = _LR(ok=True, escalate=False, output="ok"); lr.gated = True
        self.assertTrue(composed_adjudicate(SubTask(type="codegen", payload={}), lr))

    def test_reject_when_lane_contract_fails(self):
        lr = _LR(ok=False, escalate=True, output=""); lr.gated = True
        self.assertFalse(composed_adjudicate(SubTask(type="codegen", payload={}), lr))

    def test_reject_when_verifier_fails_even_if_lane_ok(self):
        # citation: lane says gated-ok, but the cited code doesn't ground -> verifier fails -> reject.
        lr = _LR(ok=True, escalate=False, output="cites repo_a/gone.py:999"); lr.gated = True
        r = composed_adjudicate(SubTask(type="citation", payload={}), lr,
                                ground_fn=_fake_ground(True, has_problem=True, has_grounded=False))
        self.assertFalse(r)

    def test_accept_citation_when_grounds_and_lane_ok(self):
        lr = _LR(ok=True, escalate=False, output="cites repo_a/mod.py:1"); lr.gated = True
        r = composed_adjudicate(SubTask(type="citation", payload={}), lr,
                                ground_fn=_fake_ground(True, has_problem=False, has_grounded=True))
        self.assertTrue(r)

    def test_reject_when_no_verifier_for_type(self):
        lr = _LR(ok=True, escalate=False); lr.gated = True
        self.assertFalse(composed_adjudicate(SubTask(type="subjective", payload={}), lr))

    def test_reject_when_verifier_not_applicable(self):
        lr = _LR(ok=True, escalate=False, output="prose"); lr.gated = True
        r = composed_adjudicate(SubTask(type="citation", payload={}), lr,
                                ground_fn=_fake_ground(False, has_problem=False, has_grounded=False))
        self.assertFalse(r)

    def test_accept_verifier_gated_citation_lane(self):
        # Stage 2.5: a citation lane result (ok=True, gated=False, not escalate) whose grounding
        # verifier passes must be ACCEPTED — the verifier is its gate, so the lane's gated=False must
        # not block it via the lane-contract check.
        lr = _LR(ok=True, escalate=False, output="cites repo_a/mod.py:1"); lr.gated = False
        r = composed_adjudicate(SubTask(type="citation", payload={}), lr,
                                ground_fn=_fake_ground(True, has_problem=False, has_grounded=True))
        self.assertTrue(r)

    def test_verifier_gated_citation_that_fails_grounding_is_rejected(self):
        lr = _LR(ok=True, escalate=False, output="cites repo_a/gone.py:999"); lr.gated = False
        r = composed_adjudicate(SubTask(type="citation", payload={}), lr,
                                ground_fn=_fake_ground(True, has_problem=True, has_grounded=False))
        self.assertFalse(r)

    def test_citation_lane_that_escalates_is_rejected(self):
        lr = _LR(ok=True, escalate=True, output="x"); lr.gated = False
        r = composed_adjudicate(SubTask(type="citation", payload={}), lr,
                                ground_fn=_fake_ground(True, has_problem=False, has_grounded=True))
        self.assertFalse(r)

    def test_codegen_still_needs_lane_gated(self):
        # codegen's gate is its tests; an ungated codegen result (gated=False) never earned
        # acceptance and must still be rejected — the verifier-gated relaxation is citation-only.
        lr = _LR(ok=True, escalate=False, output="code"); lr.gated = False
        self.assertFalse(composed_adjudicate(SubTask(type="codegen", payload={}), lr))

    def test_xval_seam_can_reject_a_necessary_gate_pass(self):
        # Codex xval F1: a result that passes the necessary gates but is SEMANTICALLY false (real cite,
        # wrong claim) must be rejectable by the cross-validation seam for committed work.
        lr = _LR(ok=True, escalate=False, output="cites repo_a/mod.py:1"); lr.gated = True
        gf = _fake_ground(True, has_problem=False, has_grounded=True)
        # necessary gates pass, but xval says the claim is wrong -> reject.
        self.assertFalse(composed_adjudicate(SubTask(type="citation", payload={}), lr,
                                             ground_fn=gf, xval_fn=lambda st, r: False))
        # xval agrees -> accept.
        self.assertTrue(composed_adjudicate(SubTask(type="citation", payload={}), lr,
                                            ground_fn=gf, xval_fn=lambda st, r: True))
        # no xval seam (non-committed sub-result) -> necessary gates are final.
        self.assertTrue(composed_adjudicate(SubTask(type="citation", payload={}), lr, ground_fn=gf))


if __name__ == "__main__":
    unittest.main()
