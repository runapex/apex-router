"""Tests for apex_router.route_join — Phase-0 labeled training table join.

Joins route_log outcomes with conformance rows. Hermetic: never touches real
home-directory logs.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router import route_join  # noqa: E402


class TestRouteJoin(unittest.TestCase):
    def _write_jsonl(self, path, rows):
        """Write a list of dicts as JSONL, creating parent dirs if needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def _read_jsonl(self, path):
        return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]

    def _set_env_paths(self, log_path, conf_path):
        """Point default paths at hermetic files; return previous env values."""
        old_log = os.environ.get("APEX_ROUTER_LOG")
        old_conf = os.environ.get("APEX_CONFORMANCE_LOG")
        os.environ["APEX_ROUTER_LOG"] = str(log_path)
        os.environ["APEX_CONFORMANCE_LOG"] = str(conf_path)
        return old_log, old_conf

    def _restore_env_paths(self, old_log, old_conf):
        if old_log is None:
            os.environ.pop("APEX_ROUTER_LOG", None)
        else:
            os.environ["APEX_ROUTER_LOG"] = old_log
        if old_conf is None:
            os.environ.pop("APEX_CONFORMANCE_LOG", None)
        else:
            os.environ["APEX_CONFORMANCE_LOG"] = old_conf

    def test_session_id_preferred_over_closer_ts(self):
        # Two conformance candidates are inside the 300 s window; the one sharing
        # session_id with the route_log row must win even though it is farther in ts.
        with tempfile.TemporaryDirectory() as d:
            log_p = Path(d) / "route_log.jsonl"
            conf_p = Path(d) / "conformance.jsonl"
            self._write_jsonl(log_p, [{
                "ts": 100.0,
                "task_type": "explore",
                "model": "sonnet",
                "escalated": False,
                "session_id": "sess-A",
            }])
            self._write_jsonl(conf_p, [
                {
                    "ts": 105.0,
                    "surface": "pi",
                    "task_type": "explore",
                    "requested_tier": "sonnet",
                    "resolved_model": "sonnet",
                    "matched": True,
                    "session_id": "sess-B",
                },
                {
                    "ts": 130.0,
                    "surface": "pi",
                    "task_type": "explore",
                    "requested_tier": "sonnet",
                    "resolved_model": "sonnet",
                    "matched": True,
                    "session_id": "sess-A",
                },
            ])
            result = route_join.join_labels(log_p, conf_p)
            self.assertEqual(len(result["table"]), 1)
            self.assertEqual(result["table"][0]["session_id"], "sess-A")
            self.assertEqual(result["stats"]["joined"], 1)
            self.assertEqual(result["stats"]["no_partner"], 0)

    def test_ts_window_fallback_when_no_session_id(self):
        # When neither side has a session_id, the nearest ts within the window wins.
        with tempfile.TemporaryDirectory() as d:
            log_p = Path(d) / "route_log.jsonl"
            conf_p = Path(d) / "conformance.jsonl"
            self._write_jsonl(log_p, [{
                "ts": 100.0,
                "task_type": "explore",
                "model": "sonnet",
                "escalated": False,
            }])
            self._write_jsonl(conf_p, [
                {
                    "ts": 105.0,
                    "surface": "pi",
                    "task_type": "explore",
                    "requested_tier": "sonnet",
                    "resolved_model": "sonnet",
                    "matched": True,
                },
                {
                    "ts": 200.0,
                    "surface": "pi",
                    "task_type": "explore",
                    "requested_tier": "sonnet",
                    "resolved_model": "sonnet",
                    "matched": True,
                },
            ])
            result = route_join.join_labels(log_p, conf_p)
            self.assertEqual(len(result["table"]), 1)
            self.assertEqual(result["table"][0]["ts"], 100.0)
            # Matched to the closer 105.0 row.
            self.assertEqual(result["stats"]["joined"], 1)

    def test_agent_surface_matched_none_excluded_from_join(self):
        # Agent-surface intent-only rows (matched=None) must not be used as partners.
        with tempfile.TemporaryDirectory() as d:
            log_p = Path(d) / "route_log.jsonl"
            conf_p = Path(d) / "conformance.jsonl"
            self._write_jsonl(log_p, [{
                "ts": 100.0,
                "task_type": "explore",
                "model": "sonnet",
                "escalated": False,
            }])
            self._write_jsonl(conf_p, [{
                "ts": 100.0,
                "surface": "agent",
                "task_type": "explore",
                "requested_tier": "sonnet",
                "resolved_model": None,
                "matched": None,
            }])
            result = route_join.join_labels(log_p, conf_p)
            self.assertEqual(result["table"], [])
            self.assertEqual(result["stats"]["excluded_agent_intent"], 1)
            self.assertEqual(result["stats"]["joined"], 0)
            self.assertEqual(result["stats"]["no_partner"], 1)

    def test_null_ts_route_row_counted_unjoinable_not_crashed(self):
        # route_log rows with missing or null ts are unjoinable and counted in stats.
        with tempfile.TemporaryDirectory() as d:
            log_p = Path(d) / "route_log.jsonl"
            conf_p = Path(d) / "conformance.jsonl"
            self._write_jsonl(log_p, [
                {"ts": None, "task_type": "explore", "model": "sonnet", "escalated": False},
                {"task_type": "explore", "model": "sonnet", "escalated": False},
                {"ts": "not-a-number", "task_type": "explore", "model": "sonnet", "escalated": False},
            ])
            self._write_jsonl(conf_p, [{
                "ts": 100.0,
                "surface": "pi",
                "task_type": "explore",
                "requested_tier": "sonnet",
                "resolved_model": "sonnet",
                "matched": True,
            }])
            result = route_join.join_labels(log_p, conf_p)
            self.assertEqual(result["table"], [])
            self.assertEqual(result["stats"]["route_rows"], 3)
            self.assertEqual(result["stats"]["null_ts"], 3)
            self.assertEqual(result["stats"]["joined"], 0)
            self.assertEqual(result["stats"]["no_partner"], 0)

    def test_malformed_json_lines_are_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as d:
            log_p = Path(d) / "route_log.jsonl"
            conf_p = Path(d) / "conformance.jsonl"
            # One valid route row, one malformed JSON, one wrong shape (missing escalated).
            log_p.write_text(
                json.dumps({"ts": 100.0, "task_type": "explore", "model": "sonnet", "escalated": False}) + "\n"
                + "{ not valid json\n"
                + json.dumps({"ts": 101.0, "task_type": "explore", "model": "sonnet"}) + "\n"
            )
            # One valid conformance row, one malformed JSON, one wrong shape (missing surface).
            conf_p.write_text(
                json.dumps({"ts": 100.0, "surface": "pi", "task_type": "explore",
                            "requested_tier": "sonnet", "resolved_model": "sonnet", "matched": True}) + "\n"
                + "{ broken\n"
                + json.dumps({"ts": 101.0, "task_type": "explore",
                              "requested_tier": "sonnet", "resolved_model": "sonnet", "matched": True}) + "\n"
            )
            result = route_join.join_labels(log_p, conf_p)
            self.assertEqual(result["stats"]["route_rows"], 1)
            self.assertEqual(result["stats"]["route_skipped"], 2)
            self.assertEqual(result["stats"]["conformance_rows"], 1)
            self.assertEqual(result["stats"]["conformance_skipped"], 2)
            self.assertEqual(result["stats"]["joined"], 1)

    def test_cell_rates_returns_wilson_ci_and_with_context(self):
        table = [
            {"task_type": "explore", "escalated": False, "context_size": 100},
            {"task_type": "explore", "escalated": True},
            {"task_type": "generate", "escalated": False, "context_size": 200},
        ]
        rates = route_join.cell_rates(table)
        self.assertEqual(rates["explore"]["n"], 2)
        self.assertEqual(rates["explore"]["escalated"], 1)
        self.assertAlmostEqual(rates["explore"]["rate"], 0.5)
        self.assertEqual(rates["explore"]["with_context"], 1)
        lo, hi = rates["explore"]["ci"]
        self.assertIsInstance(lo, float)
        self.assertIsInstance(hi, float)
        self.assertLessEqual(lo, hi)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(hi, 1.0)

        self.assertEqual(rates["generate"]["n"], 1)
        self.assertEqual(rates["generate"]["escalated"], 0)
        self.assertEqual(rates["generate"]["with_context"], 1)
        gen_lo, gen_hi = rates["generate"]["ci"]
        self.assertIsInstance(gen_lo, float)
        self.assertIsInstance(gen_hi, float)

    def test_main_out_writes_valid_jsonl_of_joined_rows(self):
        with tempfile.TemporaryDirectory() as d:
            log_p = Path(d) / "route_log.jsonl"
            conf_p = Path(d) / "conformance.jsonl"
            out_p = Path(d) / "joined.jsonl"
            self._write_jsonl(log_p, [{
                "ts": 100.0,
                "task_type": "explore",
                "model": "sonnet",
                "escalated": True,
                "context_size": 256,
                "session_id": "sess-1",
            }])
            self._write_jsonl(conf_p, [{
                "ts": 102.0,
                "surface": "pi",
                "task_type": "explore",
                "requested_tier": "sonnet",
                "resolved_model": "sonnet",
                "matched": True,
                "session_id": "sess-1",
            }])
            old_log, old_conf = self._set_env_paths(log_p, conf_p)
            try:
                rc = route_join.main(["--out", str(out_p)])
                self.assertEqual(rc, 0)
            finally:
                self._restore_env_paths(old_log, old_conf)
            self.assertTrue(out_p.exists())
            rows = self._read_jsonl(out_p)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["task_type"], "explore")
            self.assertEqual(rows[0]["label"], "hard")
            self.assertTrue(rows[0]["escalated"])
            self.assertEqual(rows[0]["context_size"], 256)
            self.assertEqual(rows[0]["session_id"], "sess-1")

    def test_empty_and_missing_log_files_yield_empty_table_and_main_returns_zero(self):
        with tempfile.TemporaryDirectory() as d:
            empty_log = Path(d) / "empty_route_log.jsonl"
            empty_conf = Path(d) / "empty_conformance.jsonl"
            missing_log = Path(d) / "missing_route_log.jsonl"
            missing_conf = Path(d) / "missing_conformance.jsonl"
            # Create empty files.
            empty_log.write_text("")
            empty_conf.write_text("")

            result = route_join.join_labels(empty_log, empty_conf)
            self.assertEqual(result["table"], [])
            self.assertEqual(result["stats"]["route_rows"], 0)
            self.assertEqual(result["stats"]["conformance_rows"], 0)
            self.assertEqual(result["stats"]["joined"], 0)

            result_missing = route_join.join_labels(missing_log, missing_conf)
            self.assertEqual(result_missing["table"], [])

            old_log, old_conf = self._set_env_paths(empty_log, empty_conf)
            try:
                rc = route_join.main([])
                self.assertEqual(rc, 0)
            finally:
                self._restore_env_paths(old_log, old_conf)


if __name__ == "__main__":
    unittest.main()
