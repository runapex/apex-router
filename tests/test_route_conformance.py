import json, os, stat, tempfile, unittest
from pathlib import Path
from apex_router import route_conformance as rc

class TestLogConformance(unittest.TestCase):
    def _read(self, p):
        return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]

    def test_happy_row_written(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.jsonl"
            ok = rc.log_conformance("resolve", "synthesis", "opus",
                                    resolved_model="claude-opus-4-8", matched=True,
                                    log_path=p, ts=1.0)
            self.assertTrue(ok)
            row = self._read(p)[0]
            self.assertEqual(row["surface"], "resolve")
            self.assertEqual(row["task_type"], "synthesis")
            self.assertEqual(row["requested_tier"], "opus")
            self.assertEqual(row["resolved_model"], "claude-opus-4-8")
            self.assertTrue(row["matched"])

    def test_intent_only_agent_row(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.jsonl"
            rc.log_conformance("agent", "explore", "sonnet", log_path=p, ts=1.0)
            row = self._read(p)[0]
            self.assertIsNone(row["resolved_model"])
            self.assertIsNone(row["matched"])

    def test_bad_surface_rejected_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.jsonl"
            self.assertFalse(rc.log_conformance("bogus", "t", "opus", log_path=p))
            self.assertFalse(p.exists())

    def test_nonstr_task_type_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.jsonl"
            self.assertFalse(rc.log_conformance("resolve", 123, "opus", log_path=p))

    def test_nan_ts_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.jsonl"
            self.assertFalse(rc.log_conformance("resolve", "t", "opus", log_path=p, ts=float("nan")))
            self.assertFalse(p.exists())

    def test_nonregular_target_refused(self):
        # a directory is not a regular file → refuse, return False, never raise
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(rc.log_conformance("resolve", "t", "opus", log_path=Path(d)))

    def test_default_path_env_override(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["APEX_CONFORMANCE_LOG"] = str(Path(d) / "x.jsonl")
            try:
                self.assertEqual(rc.default_conformance_path(), Path(d) / "x.jsonl")
            finally:
                del os.environ["APEX_CONFORMANCE_LOG"]
