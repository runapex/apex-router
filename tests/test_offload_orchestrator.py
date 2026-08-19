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

from apex_router.ornith.offload_orchestrator import orchestrate, SubTask  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
