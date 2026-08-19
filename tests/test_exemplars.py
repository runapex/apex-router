"""Tests for the RAG exemplar store — snapshot + lineage-aware correction retrieval (spec F5)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith.exemplars import load_corrections, retrieve_exemplars  # noqa: E402


def _rec(job_id, text, ts, approved=True):
    return {"source_job_id": job_id, "created_at": ts, "approved_for_training": approved,
            "messages": [{"role": "user", "content": text}], "corrected_answer": f"fix:{text}"}


def _fake_embed(text):
    vocab = ["auth", "flow", "login", "token", "summary"]
    return [1.0 if w in text else 0.0 for w in vocab]


class TestExemplars(unittest.TestCase):
    def test_snapshot_cutoff_excludes_later_corrections(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in [
                _rec("j1", "early", "2026-08-01T00:00:00+00:00"),
                _rec("j2", "late", "2026-08-20T00:00:00+00:00"),
            ]))
            got = load_corrections(p, before="2026-08-10T00:00:00+00:00")
            self.assertEqual([r["source_job_id"] for r in got], ["j1"])

    def test_load_skips_unapproved_and_malformed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved.jsonl"
            p.write_text("\n".join([
                json.dumps(_rec("j1", "ok", "t")),
                json.dumps(_rec("j2", "no", "t", approved=False)),
                "{ not json",
            ]))
            got = load_corrections(p)
            self.assertEqual([r["source_job_id"] for r in got], ["j1"])

    def test_missing_file_is_empty(self):
        self.assertEqual(load_corrections(Path("/no/such/approved.jsonl")), [])

    def test_snapshot_cutoff_is_tz_aware_not_string_compare(self):
        # Codex xval F1: '2026-08-09T20:30:00-04:00' is 2026-08-10 00:30 UTC — AFTER a
        # '2026-08-10T00:00:00+00:00' cutoff, but sorts BEFORE it as a string. Must be excluded.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in [
                _rec("j1", "before", "2026-08-09T00:00:00+00:00"),
                _rec("j2", "post-but-lexically-early", "2026-08-09T20:30:00-04:00"),
                _rec("j3", "no-ts", None),          # missing/invalid ts -> fail-closed exclude
            ]))
            got = load_corrections(p, before="2026-08-10T00:00:00+00:00")
            self.assertEqual([r["source_job_id"] for r in got], ["j1"])

    def test_invalid_before_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "approved.jsonl"
            p.write_text(json.dumps(_rec("j1", "x", "2026-08-01T00:00:00+00:00")))
            with self.assertRaises(ValueError):
                load_corrections(p, before="garbage")

    def test_retrieve_returns_k_nearest_by_cosine(self):
        corr = [_rec("j1", "auth login", "t"), _rec("j2", "flow summary", "t"),
                _rec("j3", "auth token", "t")]
        got = retrieve_exemplars("auth login help", corr, k=2, embed_fn=_fake_embed)
        ids = {r["source_job_id"] for r in got}
        self.assertEqual(len(got), 2)
        self.assertIn("j1", ids)               # "auth login" nearest to the query
        self.assertNotIn("j2", ids)            # "flow summary" shares nothing

    def test_exclude_lineage_drops_same_job(self):
        corr = [_rec("j1", "auth login", "t"), _rec("j3", "auth token", "t")]
        got = retrieve_exemplars("auth", corr, k=5, embed_fn=_fake_embed,
                                 exclude_lineage=frozenset({"j1"}))
        self.assertNotIn("j1", {r["source_job_id"] for r in got})
        self.assertIn("j3", {r["source_job_id"] for r in got})

    def test_retrieve_empty_corpus_is_empty(self):
        self.assertEqual(retrieve_exemplars("q", [], k=3, embed_fn=_fake_embed), [])


if __name__ == "__main__":
    unittest.main()
