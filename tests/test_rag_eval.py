"""Tests for the provenance-isolated RAG improvement measurement (spec F5)."""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith.rag_eval import run_condition, measure_improvement  # noqa: E402


def _fake_embed(text):
    return [1.0 if w in text else 0.0 for w in ["auth", "flow", "login", "token"]]


def _corr(job_id, text):
    return {"source_job_id": job_id, "created_at": "t",
            "messages": [{"role": "user", "content": text}], "corrected_answer": "x"}


# ask: report whether any exemplar was injected (messages longer than [system, user]).
def _ask(messages):
    return {"injected": len(messages) > 2}


# judge: escalate iff nothing was injected (simulates RAG helping).
def _judge(ans, case):
    return not ans["injected"]


class TestRagEval(unittest.TestCase):
    def test_run_condition_counts_escalations(self):
        cases = [{"query": "auth login", "lineage": "cX"}, {"query": "flow", "lineage": "cY"}]
        corr = [_corr("j1", "auth login")]
        r = run_condition(cases, corr, inject=True, ask_fn=_ask, judge_fn=_judge,
                          snapshot_before=None, k=2, embed_fn=_fake_embed)
        self.assertEqual(r["n"], 2)
        self.assertIn("escalation_rate", r)

    def test_measure_improvement_flags_real_drop(self):
        cases = [{"query": "auth", "lineage": "cX"}]
        corr = [_corr("j1", "auth")]
        r = measure_improvement(cases, corr, ask_fn=_ask, judge_fn=_judge, snapshot_before=None,
                                k=2, embed_fn=_fake_embed)
        self.assertEqual(r["baseline_rate"], 1.0)   # no injection -> escalates
        self.assertEqual(r["inject_rate"], 0.0)     # injection -> no escalate
        self.assertTrue(r["improved"])

    def test_no_improvement_when_delta_below_noise_floor(self):
        cases = [{"query": "auth", "lineage": "cX"}]
        corr = [_corr("j1", "auth")]
        r = measure_improvement(cases, corr, ask_fn=_ask, judge_fn=lambda a, c: False,
                                snapshot_before=None, k=2, noise_floor=0.05, embed_fn=_fake_embed)
        self.assertEqual(r["delta"], 0.0)
        self.assertFalse(r["improved"])             # 0 <= noise floor -> not improved

    def test_case_never_retrieves_its_own_lineage(self):
        # F5-c: even though the correction text matches the query, a case must not get its OWN
        # correction as an exemplar (lineage == the correction's source_job_id).
        cases = [{"query": "auth", "lineage": "j1"}]
        corr = [_corr("j1", "auth")]   # the only correction IS this case's lineage
        r = run_condition(cases, corr, inject=True, ask_fn=_ask, judge_fn=_judge,
                          snapshot_before=None, k=5, embed_fn=_fake_embed)
        # nothing injectable (own lineage excluded) -> the ask sees no exemplar -> escalates.
        self.assertEqual(r["escalated"], 1)

    def test_eval_does_not_mutate_the_corrections_input(self):
        # F5-a: eval mode must not write feedback. The corrections list passed in is unchanged.
        cases = [{"query": "auth", "lineage": "cX"}]
        corr = [_corr("j1", "auth")]
        before = [dict(c) for c in corr]
        measure_improvement(cases, corr, ask_fn=_ask, judge_fn=_judge, snapshot_before=None,
                            k=2, embed_fn=_fake_embed)
        self.assertEqual(corr, before)              # no append/mutation during eval


if __name__ == "__main__":
    unittest.main()
