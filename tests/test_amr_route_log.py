"""Tests for amr.route_log — the write-only escalation outcome log (Phase 1).

This is NOT a router and NOT the (circular, deferred) `resolve` consultation. It only
RECORDS, after the fact, whether a cheap-started subtask succeeded or escalated to the
frontier tier — so we can measure the per-task-type escalation rate. Its load-bearing
property: logging must be FAIL-SAFE — a logging failure (unwritable dir, bad args) must
NEVER raise into or block a dispatch. The log path is an injected seam (arg > env >
home) so tests are hermetic.
"""
import json
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router import route_log  # noqa: E402
from apex_router import cli as amr_cli  # noqa: E402


class TestLogOutcome(unittest.TestCase):
    def _read(self, path):
        return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]

    def test_ok_outcome_records_passed_true_escalated_false(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            ok = route_log.log_outcome("explore", "sonnet", "ok", log_path=p, ts="T0")
            self.assertTrue(ok)
            rows = self._read(p)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["task_type"], "explore")
            self.assertEqual(rows[0]["model"], "sonnet")
            self.assertTrue(rows[0]["passed"])
            self.assertFalse(rows[0]["escalated"])

    def test_escalated_outcome_records_passed_false_escalated_true(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            route_log.log_outcome("generate", "sonnet", "escalated", log_path=p)
            row = self._read(p)[0]
            self.assertFalse(row["passed"])
            self.assertTrue(row["escalated"])

    def test_invalid_outcome_rejected_nothing_appended(self):
        # A bad --outcome must NOT be silently recorded as a pass/fail — reject it,
        # write nothing, return False (fail-safe, no raise).
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            ok = route_log.log_outcome("explore", "sonnet", "bogus", log_path=p)
            self.assertFalse(ok)
            self.assertFalse(p.exists())

    def test_repeated_calls_append_never_truncate(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            route_log.log_outcome("explore", "sonnet", "ok", log_path=p)
            route_log.log_outcome("explore", "sonnet", "escalated", log_path=p)
            self.assertEqual(len(self._read(p)), 2)

    def test_unwritable_path_is_fail_safe_returns_false(self):
        # A logging failure must NEVER raise into a dispatch. Point at a path whose
        # parent is a FILE (mkdir will fail) → return False, no exception.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "not_a_dir"
            blocker.write_text("i am a file")
            p = blocker / "route_log.jsonl"   # parent is a file → unwritable
            ok = route_log.log_outcome("explore", "sonnet", "ok", log_path=p)
            self.assertFalse(ok)

    def test_default_path_from_env_when_log_path_omitted(self):
        # arg > env > home. With no log_path, honor APEX_ROUTER_LOG for hermetic tests.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "route_log.jsonl"
            old = os.environ.get("APEX_ROUTER_LOG")
            os.environ["APEX_ROUTER_LOG"] = str(p)
            try:
                ok = route_log.log_outcome("explore", "sonnet", "ok")
                self.assertTrue(ok)
                self.assertTrue(p.exists())
            finally:
                if old is None:
                    os.environ.pop("APEX_ROUTER_LOG", None)
                else:
                    os.environ["APEX_ROUTER_LOG"] = old


class TestLogOutcomeHardening(unittest.TestCase):
    """Adversarial paths (Codex code cross-validation): a logging call must never HANG
    or raise, and must not record a non-string outcome or emit invalid JSON."""

    def test_fifo_path_is_fail_safe_never_hangs(self):
        # Opening a FIFO for append blocks until a reader appears — that would stall a
        # dispatch. A non-regular-file target must be refused (False), never opened.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            fifo = Path(d) / "route_log.jsonl"
            os.mkfifo(fifo)
            ok = route_log.log_outcome("explore", "sonnet", "ok", log_path=fifo)
            self.assertFalse(ok)

    def test_non_string_outcome_rejected(self):
        # A non-string outcome whose __eq__ could spoof "escalated" must not be recorded.
        class Spoof:
            def __eq__(self, other):
                return True
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            ok = route_log.log_outcome("explore", "sonnet", Spoof(), log_path=p)
            self.assertFalse(ok)
            self.assertFalse(p.exists())

    def test_non_finite_ts_does_not_emit_invalid_json(self):
        # NaN/Infinity are not valid JSON; rather than write an unparseable line, refuse.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            ok = route_log.log_outcome("explore", "sonnet", "ok", log_path=p,
                                       ts=float("nan"))
            self.assertFalse(ok)
            self.assertFalse(p.exists())


class TestReadRates(unittest.TestCase):
    """The readout side: aggregate the write-only log into per-task-type escalation
    rates — the honest Phase-1 payoff. Fail-safe like the writer: a missing/garbled
    log yields empty rates, never a raise."""

    def _write(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_rate_counts_n_and_escalations_per_task_type(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            self._write(p, [
                {"task_type": "explore", "escalated": False},
                {"task_type": "explore", "escalated": True},
                {"task_type": "explore", "escalated": False},
                {"task_type": "generate", "escalated": True},
            ])
            rates = route_log.read_rates(log_path=p)
            self.assertEqual(rates["explore"]["n"], 3)
            self.assertEqual(rates["explore"]["escalated"], 1)
            self.assertAlmostEqual(rates["explore"]["rate"], 1 / 3)
            self.assertEqual(rates["generate"]["n"], 1)
            self.assertAlmostEqual(rates["generate"]["rate"], 1.0)

    def test_missing_log_yields_empty_rates_no_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nope.jsonl"
            self.assertEqual(route_log.read_rates(log_path=p), {})

    def test_malformed_lines_are_skipped_not_fatal(self):
        # A corrupt/partial trailing line (disk-full mid-append) must not sink the read.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            p.write_text(
                json.dumps({"task_type": "explore", "escalated": False}) + "\n"
                + "{ partial broken line\n"
                + json.dumps({"task_type": "explore", "escalated": True}) + "\n")
            rates = route_log.read_rates(log_path=p)
            self.assertEqual(rates["explore"]["n"], 2)
            self.assertEqual(rates["explore"]["escalated"], 1)


class TestRouteLogCLI(unittest.TestCase):
    def _read(self, path):
        return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]

    def _run(self, args, log_path):
        import os
        old = os.environ.get("APEX_ROUTER_LOG")
        os.environ["APEX_ROUTER_LOG"] = str(log_path)
        try:
            return amr_cli.main(args)
        finally:
            if old is None:
                os.environ.pop("APEX_ROUTER_LOG", None)
            else:
                os.environ["APEX_ROUTER_LOG"] = old

    def test_cli_appends_record_and_exits_zero(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            rc = self._run(
                ["route-log", "--task-type", "explore", "--start-tier", "sonnet",
                 "--outcome", "escalated"], p)
            self.assertEqual(rc, 0)
            row = self._read(p)[0]
            self.assertEqual(row["task_type"], "explore")
            self.assertEqual(row["model"], "sonnet")
            self.assertTrue(row["escalated"])

    def test_cli_invalid_outcome_exits_zero_writes_nothing(self):
        # Fail-safe at the CLI boundary too: a bad outcome must not break a dispatch.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            rc = self._run(
                ["route-log", "--task-type", "explore", "--start-tier", "sonnet",
                 "--outcome", "bogus"], p)
            self.assertEqual(rc, 0)
            self.assertFalse(p.exists())


class TestRouteReadoutCLI(unittest.TestCase):
    def _run_capture(self, args, log_path):
        import io
        import os
        from contextlib import redirect_stdout
        old = os.environ.get("APEX_ROUTER_LOG")
        os.environ["APEX_ROUTER_LOG"] = str(log_path)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = amr_cli.main(args)
            return rc, buf.getvalue()
        finally:
            if old is None:
                os.environ.pop("APEX_ROUTER_LOG", None)
            else:
                os.environ["APEX_ROUTER_LOG"] = old

    def test_readout_prints_rate_per_task_type(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            route_log.log_outcome("explore", "sonnet", "ok", log_path=p)
            route_log.log_outcome("explore", "sonnet", "escalated", log_path=p)
            rc, out = self._run_capture(["route-readout"], p)
            self.assertEqual(rc, 0)
            self.assertIn("explore", out)
            self.assertIn("2", out)  # n=2 appears in the output

    def test_readout_json_shape(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            route_log.log_outcome("generate", "sonnet", "escalated", log_path=p)
            rc, out = self._run_capture(["route-readout", "--json"], p)
            self.assertEqual(rc, 0)
            data = json.loads(out)
            self.assertEqual(data["generate"]["n"], 1)
            self.assertAlmostEqual(data["generate"]["rate"], 1.0)

    def test_readout_empty_log_exits_zero(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"  # never created
            rc, out = self._run_capture(["route-readout"], p)
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
