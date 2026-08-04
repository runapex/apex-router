"""Unified weekly report — folds the offload log plus codeqa's two separate logs into one view.

codeqa has its own intact telemetry (impact + validate logs) with different schemas; the report
READS them rather than rewiring codeqa's runtime. These tests pin the codeqa summarizers so the
weekly report shows total local-model activity in one place.
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith.offload_report import summarize_codeqa_impact, summarize_codeqa_validate  # noqa: E402


class TestCodeqaImpact(unittest.TestCase):
    def test_summarizes_grounding_and_tokens(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "impact.jsonl"
            log.write_text(
                json.dumps({"prompt_tokens": 9000, "cached_tokens": 0,
                            "grounding": {"grounded": 4, "stale": 0, "hallucinated": 1}}) + "\n"
                + json.dumps({"prompt_tokens": 6000, "cached_tokens": 6000,
                              "grounding": {"grounded": 2, "stale": 0, "hallucinated": 0}}) + "\n"
            )
            s = summarize_codeqa_impact(log)
            self.assertEqual(s["n_questions"], 2)
            self.assertEqual(s["grounded"], 6)
            self.assertEqual(s["hallucinated"], 1)
            self.assertEqual(s["prompt_tokens"], 15000)
            self.assertEqual(s["cached_tokens"], 6000)

    def test_missing_file_is_zero_not_crash(self):
        s = summarize_codeqa_impact(Path("/tmp/nope-impact-xyz.jsonl"))
        self.assertEqual(s["n_questions"], 0)

    def test_malformed_line_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "impact.jsonl"
            log.write_text('not json\n{"grounding":{"grounded":1}}\n')
            s = summarize_codeqa_impact(log)
            self.assertEqual(s["n_questions"], 1)
            self.assertEqual(s["grounded"], 1)


class TestCodeqaValidate(unittest.TestCase):
    def test_sums_local_vs_frontier_and_saved(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "validate.jsonl"
            log.write_text(
                json.dumps({"n_local": 5, "n_frontier": 2, "n_struck": 1,
                            "est_frontier_tokens": 552}) + "\n"
                + json.dumps({"n_local": 3, "n_frontier": 0, "n_struck": 0,
                              "est_frontier_tokens": 0}) + "\n"
            )
            s = summarize_codeqa_validate(log)
            self.assertEqual(s["n_local"], 8)      # calls kept on the free local model
            self.assertEqual(s["n_frontier"], 2)
            self.assertEqual(s["n_struck"], 1)
            # local share of routed verifier calls
            self.assertAlmostEqual(s["local_share"], 8 / 10)

    def test_missing_file_zero(self):
        s = summarize_codeqa_validate(Path("/tmp/nope-validate-xyz.jsonl"))
        self.assertEqual(s["n_local"], 0)
        self.assertIsNone(s["local_share"])


if __name__ == "__main__":
    unittest.main()
