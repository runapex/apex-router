"""Tests for the per-type offload verifier registry.

A verifier answers "did this local result pass the checkable NECESSARY precondition for its type?" —
NOT "is it semantically correct" (spec F7). A grounding pass means the cited file:line exist, not that
the claim is true. A type with no verifier is un-offloadable (auto-escalate).
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith.verifiers import verify, VerifierResult, HAS_VERIFIER  # noqa: E402


class _LR:
    def __init__(self, output="", ok=True, gated=True, escalate=False):
        self.output, self.ok, self.gated, self.escalate = output, ok, gated, escalate


def _fake_ground(applicable, has_problem, has_grounded):
    return lambda text: type("G", (), {"applicable": applicable, "has_problem": has_problem,
                                       "has_grounded": has_grounded})()


class TestVerifiers(unittest.TestCase):
    def test_codegen_uses_lane_gate_verdict(self):
        r = verify("codegen", _LR(ok=True, gated=True, escalate=False))
        self.assertTrue(r.passed and r.applicable)
        r2 = verify("codegen", _LR(ok=False, gated=True, escalate=True))
        self.assertFalse(r2.passed)

    def test_codegen_ungated_is_not_applicable(self):
        r = verify("codegen", _LR(ok=True, gated=False, escalate=True))
        self.assertFalse(r.passed)
        self.assertFalse(r.applicable)

    def test_citation_passes_when_citations_ground(self):
        r = verify("citation", _LR(output="see repo_a/mod.py:1"),
                   ground_fn=_fake_ground(applicable=True, has_problem=False, has_grounded=True))
        self.assertTrue(r.passed)
        self.assertTrue(r.applicable)

    def test_citation_fails_on_stale_or_hallucinated(self):
        r = verify("citation", _LR(output="see repo_a/gone.py:999"),
                   ground_fn=_fake_ground(applicable=True, has_problem=True, has_grounded=False))
        self.assertFalse(r.passed)
        self.assertTrue(r.applicable)

    def test_citation_not_applicable_when_no_groundable_cite(self):
        r = verify("citation", _LR(output="just prose, no citation"),
                   ground_fn=_fake_ground(applicable=False, has_problem=False, has_grounded=False))
        self.assertFalse(r.passed)
        self.assertFalse(r.applicable)

    def test_citation_grounded_but_no_grounded_cite_fails(self):
        # applicable but nothing actually grounded (all unverified/advisory) -> not a pass.
        r = verify("search", _LR(output="cites repo_a/maybe.py:1"),
                   ground_fn=_fake_ground(applicable=True, has_problem=False, has_grounded=False))
        self.assertFalse(r.passed)

    def test_subjective_type_has_no_verifier(self):
        r = verify("subjective", _LR(output="cleaner now"))
        self.assertFalse(r.passed)
        self.assertFalse(r.applicable)
        self.assertNotIn("subjective", HAS_VERIFIER)

    def test_unknown_type_has_no_verifier(self):
        r = verify("totally_unknown", _LR(output="x"))
        self.assertFalse(r.passed)
        self.assertFalse(r.applicable)

    def test_has_verifier_lists_gateable_types(self):
        self.assertIn("codegen", HAS_VERIFIER)
        self.assertIn("citation", HAS_VERIFIER)
        self.assertIn("search", HAS_VERIFIER)
        self.assertIn("extraction", HAS_VERIFIER)


if __name__ == "__main__":
    unittest.main()
