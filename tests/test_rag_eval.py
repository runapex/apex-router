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

    # --- Codex refute-pass regressions (hardening) ---

    def test_callback_cannot_inject_corpus_between_passes(self):
        # Codex xval F2: an ask_fn that APPENDS to the corrections list must not affect the inject
        # pass — the corpus is deep-copied per condition, so a hostile callback can't manufacture the
        # improvement. (The old test used inert callbacks and missed this.)
        cases = [{"query": "auth", "lineage": "cX"}]
        corr = []   # start empty; a leak callback would add the case's own correction
        def leaky_ask(messages):
            injected = len(messages) > 2
            if not injected:
                corr.append(_corr("cX", "auth"))   # try to poison the corpus for the inject pass
            return {"injected": injected}
        r = measure_improvement(cases, corr, ask_fn=leaky_ask, judge_fn=_judge,
                                snapshot_before=None, k=2, embed_fn=_fake_embed)
        # the inject pass sees a deep copy taken at call time (empty), so no exemplar -> still escalates
        # -> no fabricated improvement.
        self.assertFalse(r["improved"])

    def test_snapshot_is_enforced_with_tz_aware_compare(self):
        # Codex xval F1: a post-freeze correction that sorts lexically BEFORE the cutoff must still be
        # excluded (tz-aware parse), and snapshot_before must be APPLIED inside measure_improvement.
        cases = [{"query": "auth", "lineage": "cX"}]
        post = {"source_job_id": "j2", "created_at": "2026-08-09T20:30:00-04:00",  # = 08-10 00:30 UTC
                "messages": [{"role": "user", "content": "auth"}], "corrected_answer": "x"}
        r = measure_improvement(cases, [post], ask_fn=_ask, judge_fn=_judge,
                                snapshot_before="2026-08-10T00:00:00+00:00", k=2, embed_fn=_fake_embed)
        # the only correction is post-freeze -> excluded -> nothing to inject -> no improvement.
        self.assertEqual(r["inject_rate"], 1.0)

    def test_invalid_snapshot_timestamp_raises(self):
        with self.assertRaises(ValueError):
            measure_improvement([{"query": "q", "lineage": "c"}], [], ask_fn=_ask, judge_fn=_judge,
                                snapshot_before="not-a-date", embed_fn=_fake_embed)

    def test_missing_or_invalid_lineage_fails_closed(self):
        # Codex xval F4: a case with no valid string lineage must NOT retrieve exemplars (could be its
        # own correction) — it runs bare. Corpus text matches, but nothing is injected.
        for bad in ({"query": "auth"}, {"query": "auth", "lineage": None},
                    {"query": "auth", "lineage": ""}):
            r = run_condition([bad], [_corr("j1", "auth")], inject=True, ask_fn=_ask,
                              judge_fn=_judge, snapshot_before=None, k=2, embed_fn=_fake_embed)
            self.assertEqual(r["escalated"], 1, f"lineage {bad.get('lineage')!r} should fail closed")

    def test_negative_noise_floor_rejected(self):
        # Codex xval F5: a negative floor would credit a WORSE result as improved.
        with self.assertRaises(ValueError):
            measure_improvement([{"query": "q", "lineage": "c"}], [], ask_fn=_ask, judge_fn=_judge,
                                snapshot_before=None, noise_floor=-0.1, embed_fn=_fake_embed)

    def test_empty_confirmation_set_rejected(self):
        with self.assertRaises(ValueError):
            measure_improvement([], [], ask_fn=_ask, judge_fn=_judge, snapshot_before=None,
                                embed_fn=_fake_embed)

    def test_dev_confirmation_overlap_rejected(self):
        # Codex xval F3: a number tuned on the dev set can't be reported as held-out.
        cases = [{"query": "auth", "lineage": "c1", "id": "case-7"}]
        with self.assertRaises(ValueError):
            measure_improvement(cases, [_corr("j1", "auth")], ask_fn=_ask, judge_fn=_judge,
                                snapshot_before=None, embed_fn=_fake_embed,
                                dev_case_ids=frozenset({"case-7"}))

    # --- Codex refute-pass 2 regressions ---

    def test_callback_cannot_poison_via_nested_mutation(self):
        # Codex xval pass 2 F1: a callback that mutates a NESTED dict of the corpus during baseline
        # must not affect the inject pass — each pass gets its own deep-copy of the frozen snapshot.
        cases = [{"query": "auth", "lineage": "cX"}]
        corr = [_corr("j1", "irrelevant")]   # doesn't match 'auth'
        def poison_ask(messages):
            injected = len(messages) > 2
            if not injected:
                corr[0]["messages"][0]["content"] = "auth"   # try to make it match for inject pass
            return {"injected": injected}
        r = measure_improvement(cases, corr, ask_fn=poison_ask, judge_fn=_judge,
                                snapshot_before=None, k=2, embed_fn=_fake_embed)
        self.assertFalse(r["improved"])

    def test_correction_without_string_lineage_is_not_retrievable(self):
        # Codex xval pass 2 F2: a correction with source_job_id None/non-string is unattributable —
        # it could be the case's own correction — so it must never be injected.
        cases = [{"query": "auth", "lineage": "cX"}]
        no_sid = {"created_at": "2026-08-01T00:00:00+00:00",
                  "messages": [{"role": "user", "content": "auth"}], "corrected_answer": "x"}
        r = run_condition(cases, [no_sid], inject=True, ask_fn=_ask, judge_fn=_judge,
                          snapshot_before=None, k=2, embed_fn=_fake_embed)
        self.assertEqual(r["escalated"], 1)   # nothing retrievable -> escalates

    def test_naive_timestamp_is_rejected_from_snapshot(self):
        # Codex xval pass 2 F4: a naive (offset-less) created_at is ambiguous -> fail closed (dropped).
        cases = [{"query": "auth", "lineage": "cX"}]
        naive = {"source_job_id": "j1", "created_at": "2026-08-01T00:00:00",   # no offset
                 "messages": [{"role": "user", "content": "auth"}], "corrected_answer": "x"}
        r = measure_improvement(cases, [naive], ask_fn=_ask, judge_fn=_judge,
                                snapshot_before="2026-09-01T00:00:00+00:00", k=2, embed_fn=_fake_embed)
        self.assertEqual(r["inject_rate"], 1.0)   # naive-ts correction dropped -> nothing to inject

    def test_idless_confirmation_case_rejected_when_dev_ids_given(self):
        # Codex xval pass 2 F3: an id-less confirmation case would bypass disjointness -> rejected.
        cases = [{"query": "auth", "lineage": "c1"}]   # no "id"
        with self.assertRaises(ValueError):
            measure_improvement(cases, [_corr("j1", "auth")], ask_fn=_ask, judge_fn=_judge,
                                snapshot_before=None, embed_fn=_fake_embed,
                                dev_case_ids=frozenset({"dev-7"}))


if __name__ == "__main__":
    unittest.main()
