"""Tests for the verifier false-accept-rate measurement harness (spec F7 — measured, not assumed)."""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith.verifier_bench import measure_false_accept, format_bench  # noqa: E402


class _LR:
    def __init__(self, output="", ok=True, gated=True, escalate=False):
        self.output, self.ok, self.gated, self.escalate = output, ok, gated, escalate


def _fake_ground(applicable, has_problem, has_grounded):
    return lambda text: type("G", (), {"applicable": applicable, "has_problem": has_problem,
                                       "has_grounded": has_grounded})()


class TestVerifierBench(unittest.TestCase):
    def test_counts_false_accepts_on_labeled_wrong_cases(self):
        # a codegen case labeled wrong but whose lane says gated-ok -> verifier passes -> false accept;
        # a wrong case the lane escalated -> verifier rejects -> NOT a false accept.
        cases = [
            {"type": "codegen", "lane_result": _LR(ok=True, gated=True), "is_wrong": True},
            {"type": "codegen", "lane_result": _LR(ok=False, gated=True, escalate=True), "is_wrong": True},
        ]
        res = measure_false_accept(cases)
        c = res["codegen"]
        self.assertEqual(c["n"], 2)
        self.assertEqual(c["wrong"], 2)
        self.assertEqual(c["false_accepts"], 1)
        self.assertAlmostEqual(c["false_accept_rate"], 0.5)

    def test_correct_cases_do_not_count_as_false_accepts(self):
        cases = [{"type": "codegen", "lane_result": _LR(ok=True, gated=True), "is_wrong": False}]
        c = measure_false_accept(cases)["codegen"]
        self.assertEqual(c["wrong"], 0)
        self.assertEqual(c["false_accepts"], 0)
        self.assertIsNone(c["false_accept_rate"])   # no wrong cases -> rate undefined, not 0

    def test_citation_false_accept_via_ground_fn(self):
        # a wrong citation result whose cite still grounds (semantic-blind oracle) -> false accept.
        cases = [{"type": "citation", "lane_result": _LR(output="x"), "is_wrong": True,
                  "ground_fn": _fake_ground(True, has_problem=False, has_grounded=True)}]
        c = measure_false_accept(cases)["citation"]
        self.assertEqual(c["false_accepts"], 1)
        self.assertAlmostEqual(c["false_accept_rate"], 1.0)

    def test_format_notes_non_zero_rate_routes_to_xval(self):
        res = {"codegen": {"n": 2, "wrong": 2, "false_accepts": 1, "false_accept_rate": 0.5}}
        out = format_bench(res)
        self.assertIn("codegen", out)
        self.assertIn("0.5", out)
        self.assertIn("cross-validation", out.lower())

    def test_format_zero_rate_has_no_xval_note(self):
        res = {"codegen": {"n": 3, "wrong": 3, "false_accepts": 0, "false_accept_rate": 0.0}}
        out = format_bench(res)
        self.assertNotIn("cross-validation", out.lower())

    def test_non_bool_label_is_rejected(self):
        # Codex xval F4/F5: a string "false" is truthy and a missing label is silent — both must be
        # a hard error, not a silent mis-bucket.
        with self.assertRaises(ValueError):
            measure_false_accept([{"type": "codegen", "lane_result": _LR(), "is_wrong": "false"}])
        with self.assertRaises(ValueError):
            measure_false_accept([{"type": "codegen", "lane_result": _LR()}])   # missing label


if __name__ == "__main__":
    unittest.main()
