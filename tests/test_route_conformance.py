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
            self.assertFalse(p.exists())

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


class TestReadConformance(unittest.TestCase):
    def _write(self, p, rows):
        Path(p).write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_drift_rate_excludes_intent_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.jsonl"
            self._write(p, [
                {"surface":"resolve","task_type":"synthesis","requested_tier":"opus",
                 "resolved_model":"claude-opus-4-8","matched":True,"ts":1,"note":""},
                {"surface":"resolve","task_type":"synthesis","requested_tier":"opus",
                 "resolved_model":"wrong","matched":False,"ts":2,"note":""},
                {"surface":"agent","task_type":"synthesis","requested_tier":"opus",
                 "resolved_model":None,"matched":None,"ts":3,"note":""},
            ])
            agg = rc.read_conformance(log_path=p)
            r = agg["resolve\tsynthesis"]
            self.assertEqual(r["n"], 2)
            self.assertEqual(r["observed"], 2)
            self.assertEqual(r["mismatches"], 1)
            self.assertAlmostEqual(r["drift_rate"], 0.5)
            a = agg["agent\tsynthesis"]
            self.assertEqual(a["n"], 1)
            self.assertEqual(a["observed"], 0)   # intent-only: no denominator
            self.assertEqual(a["drift_rate"], 0.0)

    def test_malformed_line_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.jsonl"
            Path(p).write_text('{"surface":"resolve","task_type":"t","requested_tier":"opus","matched":true,"ts":1}\n{ bad\n')
            agg = rc.read_conformance(log_path=p)
            self.assertEqual(agg["resolve\tt"]["observed"], 1)

    def test_empty_or_missing_is_empty_dict(self):
        self.assertEqual(rc.read_conformance(log_path=Path("/nonexistent/c.jsonl")), {})

    def test_invalid_utf8_log_is_empty_dict(self):
        # a log with undecodable bytes raises UnicodeDecodeError (not OSError) — must not propagate
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.jsonl"
            p.write_bytes(b'\xff\xfe not valid utf-8 \x80\x81\n')
            self.assertEqual(rc.read_conformance(log_path=p), {})


class TestExpectedModels(unittest.TestCase):
    def test_known_tier_returns_its_model(self):
        from apex_router import model_registry as mr
        self.assertEqual(rc.expected_models("opus"), {mr.tier_model("opus")})

    def test_unknown_tier_returns_empty_set(self):
        self.assertEqual(rc.expected_models("nope"), set())
