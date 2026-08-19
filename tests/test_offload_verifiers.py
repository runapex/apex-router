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

    def test_dispatchable_types_cover_all_verifiers_after_stage_2_5(self):
        # Stage 2.5 closes Codex xval F2: run_job now produces citation/search/extraction lanes, so
        # every HAS_VERIFIER type gates live traffic and PENDING_DISPATCH is empty.
        from apex_router.ornith.verifiers import DISPATCHABLE_TYPES, PENDING_DISPATCH
        self.assertEqual(DISPATCHABLE_TYPES, HAS_VERIFIER)
        self.assertEqual(PENDING_DISPATCH, frozenset())

    def test_all_citations_must_ground_via_real_verdict(self):
        # Codex xval F3: a result mixing a grounded cite with an unverified/advisory one must NOT pass.
        # Use a real GroundVerdict-shaped object exposing a per-citation list.
        def _cite(verdict):
            return type("C", (), {"verdict": verdict})()
        def ground_mixed(text):
            return type("G", (), {"applicable": True,
                                  "citations": [_cite("grounded"), _cite("unverified")]})()
        r = verify("citation", _LR(output="mixed"), ground_fn=ground_mixed)
        self.assertFalse(r.passed)   # one unverified cite -> not a pass
        def ground_all(text):
            return type("G", (), {"applicable": True,
                                  "citations": [_cite("grounded"), _cite("grounded")]})()
        self.assertTrue(verify("citation", _LR(output="all"), ground_fn=ground_all).passed)


if __name__ == "__main__":
    unittest.main()
