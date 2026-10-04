"""route_join: claude-code dispatch rows ⋈ proxy telemetry + offline escalation inference.

Hermetic: tiny fixture route log / conformance / telemetry files in a temp dir.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router import route_join, route_log  # noqa: E402
from apex_router import cli as amr_cli  # noqa: E402

S1 = "03aa612d-7c16-4bd5-8db8-2cc6481e81e0"
S2 = "11111111-2222-3333-4444-555555555555"


def _w(path, rows):
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))


def _dispatch(ts, tier, desc, agent_id, tool_use_id, *, sid=S1, tt="explore", outcome="ok",
              resolved=None):
    return {"ts": ts, "task_type": tt, "model": tier, "passed": None, "escalated": False,
            "note": "agent:general-purpose", "session_id": sid, "label_pending": True,
            "outcome": outcome, "surface": "claude-code", "agent_id": agent_id,
            "tool_use_id": tool_use_id, "start_tier": tier, "description": desc,
            **({"resolved_model": resolved} if resolved else {})}


def _tel(ts, sid, agent_id, model, out=10, is_error=False):
    return {"ts": ts, "session_id": sid, "agent_id": agent_id, "client": "claude-code",
            "model_requested": model, "model_resolved": model, "schema_version": 7,
            "is_error": is_error, "error_cause": "http_429" if is_error else None,
            "tokens_out": out, "usage": {"captured": True, "output_tokens": out}}


class TestClaudeCodeJoin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.log = d / "route_log.jsonl"
        self.conf = d / "conformance.jsonl"
        self.tel = d / "telemetry.jsonl"
        _w(self.conf, [])
        _w(self.log, [
            # classic pi row (no conformance partner) — must be unaffected
            {"ts": 50.0, "task_type": "debug", "model": "gpt", "passed": True,
             "escalated": False, "note": "turn"},
            _dispatch(100.0, "haiku", "Fix the flaky test!", "a1", "t1", tt="generate"),
            _dispatch(200.0, "sonnet", "fix the  flaky test", "a2", "t2", tt="generate"),
            _dispatch(300.0, "opus", "Fix the flaky test", "a3", "t3", tt="generate"),
            # same tier re-dispatch is NOT an escalation
            _dispatch(400.0, "sonnet", "Map the code", "a4", "t4"),
            _dispatch(500.0, "sonnet", "Map the code", "a5", "t5"),
            # inherit: effective tier from the resolved model
            _dispatch(600.0, "inherit", "Audit schema", "a6", "t6", tt="review"),
            _dispatch(700.0, "fable", "audit schema", "a7", "t7", tt="review"),
            # different session, same description: no cross-session escalation
            _dispatch(150.0, "haiku", "Fix the flaky test", "a8", "t8", sid=S2, tt="generate"),
            # no agent id + async: no telemetry, outcome stays async
            _dispatch(800.0, "haiku", "Lonely", None, "t9", outcome="async"),
            "{not json",
        ])
        _w(self.tel, [
            _tel(101, S1, "a1", "claude-haiku-4-5", out=5),
            _tel(102, S1, "a1", "claude-haiku-4-5", out=7, is_error=True),
            _tel(201, S1, "a2", "claude-sonnet-5", out=40),
            {"ev": "hb", "ts": 202, "session_id": S1, "agent_id": "a2"},
            _tel(601, S1, "a6", "claude-opus-5-5", out=3),
            _tel(602, S1, "a6", "claude-opus-5-5", out=4),
            _tel(603, S1, None, "claude-opus-5-5", out=99),  # main thread
            "garbage line with \"agent_id\"",
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def _join(self):
        res = route_join.join_labels(route_log_path=self.log, conformance_path=self.conf,
                                     telemetry_path=self.tel)
        by = {r.get("tool_use_id"): r for r in res["table"] if r.get("surface") == "claude-code"}
        return res, by

    def test_stats(self):
        res, _ = self._join()
        st = res["stats"]
        self.assertEqual(st["route_rows"], 10)
        self.assertEqual(st["route_skipped"], 1)
        self.assertEqual(st["joined"], 0)          # conformance join unaffected
        self.assertEqual(st["no_partner"], 1)      # the classic pi row
        self.assertEqual(st["claude_code_rows"], 9)
        self.assertEqual(st["telemetry_joined"], 3)
        # t1 (→t2), t2 (→t3); t6 is an inherit start, never a cheap start (R3)
        self.assertEqual(st["escalated_inferred"], 2)
        self.assertEqual(st["unlabeled"], 2)           # t1 (last request errored), t9 (async)
        self.assertEqual(st["table_rows"], 9)

    def test_telemetry_join_attaches_model_counts_errors(self):
        _, by = self._join()
        a1 = by["t1"]
        self.assertTrue(a1["telemetry_joined"])
        self.assertEqual(a1["resolved_model"], "claude-haiku-4-5")
        self.assertEqual(a1["requests"], 2)
        self.assertEqual(a1["output_tokens"], 12)
        self.assertEqual(a1["error_count"], 1)
        self.assertEqual(a1["outcome_effective"], "error")  # last request errored
        self.assertTrue(a1["matched"])
        a2 = by["t2"]
        self.assertEqual(a2["requests"], 1)                 # heartbeat excluded
        self.assertEqual(a2["outcome_effective"], "ok")
        a6 = by["t6"]
        self.assertEqual(a6["requests"], 2)                 # main-thread row not counted
        self.assertEqual(a6["effective_tier"], "opus")
        self.assertIsNone(a6["matched"])                    # inherit: nothing to conform to
        lonely = by["t9"]
        self.assertFalse(lonely["telemetry_joined"])
        self.assertEqual(lonely["requests"], 0)
        self.assertEqual(lonely["outcome_effective"], "async")
        self.assertEqual(lonely["label_status"], "unlabeled")

    def test_escalation_inference(self):
        _, by = self._join()
        self.assertTrue(by["t1"]["escalated"])
        self.assertEqual(by["t1"]["escalated_by"], "t2")    # earliest higher-tier redo
        self.assertEqual(by["t1"]["label"], "hard")
        self.assertTrue(by["t2"]["escalated"])
        self.assertEqual(by["t2"]["escalated_by"], "t3")
        self.assertFalse(by["t3"]["escalated"])
        self.assertFalse(by["t4"]["escalated"])             # same tier
        self.assertFalse(by["t5"]["escalated"])
        self.assertFalse(by["t6"]["escalated"])             # inherit start: not a cheap start
        self.assertFalse(by["t8"]["escalated"])             # other session
        self.assertEqual(by["t8"]["label"], "easy")

    def test_route_join_main_writes_labeled_table_and_readout_sees_it(self):
        old = {k: os.environ.get(k) for k in
               ("APEX_ROUTER_LOG", "APEX_CONFORMANCE_LOG", "APEX_TELEMETRY", "APEX_LABELED_TABLE")}
        os.environ["APEX_ROUTER_LOG"] = str(self.log)
        os.environ["APEX_CONFORMANCE_LOG"] = str(self.conf)
        os.environ["APEX_TELEMETRY"] = str(self.tel)
        os.environ.pop("APEX_LABELED_TABLE", None)
        try:
            self.assertEqual(amr_cli.main(["route-join"]), 0)
            labeled = self.log.parent / "labeled_table.jsonl"
            self.assertTrue(labeled.is_file())
            self.assertEqual(len(labeled.read_text().splitlines()), 9)
            rates = route_log.read_rates()
            # raw: classic debug row only; labeled: haiku/sonnet claude-code rows
            self.assertEqual(rates["debug"]["n"], 1)
            # generate: t2 sonnet esc, t8 haiku ok (t1 unlabeled: errored; t3 opus excluded)
            self.assertEqual(rates["generate"]["n"], 2)
            self.assertEqual(rates["generate"]["escalated"], 1)
            # explore: t4, t5 sonnet ok (t9 unlabeled: async, never joined)
            self.assertEqual(rates["explore"]["n"], 2)
            self.assertNotIn("review", rates)  # t6 opus-effective, t7 fable: not cheap starts
            # route-advise / route-readout must not crash on the new surface
            self.assertEqual(amr_cli.main(["route-readout"]), 0)
            self.assertEqual(amr_cli.main(["route-advise", "--json"]), 0)
            # --no-write leaves the table alone
            labeled.unlink()
            self.assertEqual(amr_cli.main(["route-join", "--no-write"]), 0)
            self.assertFalse(labeled.exists())
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_missing_telemetry_is_fail_safe(self):
        res = route_join.join_labels(route_log_path=self.log, conformance_path=self.conf,
                                     telemetry_path=Path(self.tmp.name) / "nope.jsonl")
        self.assertEqual(res["stats"]["telemetry_joined"], 0)
        self.assertEqual(res["stats"]["escalated_inferred"], 2)  # t1, t2 by explicit tier only
        # without telemetry the inherit row has no effective tier → cannot escalate
        by = {r["tool_use_id"]: r for r in res["table"] if r.get("surface") == "claude-code"}
        self.assertFalse(by["t6"]["escalated"])

    def test_bad_dispatch_field_types_are_malformed(self):
        _w(self.log, [dict(_dispatch(1.0, "haiku", "x", "a", "t"), agent_id=5),
                      dict(_dispatch(1.0, "haiku", "x", "a", "t"), label_pending="yes")])
        res = route_join.join_labels(route_log_path=self.log, conformance_path=self.conf,
                                     telemetry_path=self.tel)
        self.assertEqual(res["stats"]["route_skipped"], 2)
        self.assertEqual(res["table"], [])


class TestReviewFixes(unittest.TestCase):
    """R1-R4 review findings: telemetry path, table clobbering, escalation FPs, fake ok labels."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.log = self.d / "route_log.jsonl"
        self.conf = self.d / "conformance.jsonl"
        self.tel = self.d / "telemetry.jsonl"
        _w(self.conf, [])
        _w(self.tel, [])

    def tearDown(self):
        self.tmp.cleanup()

    def _join(self, rows, tel_rows=None, tel_path=None):
        _w(self.log, rows)
        if tel_rows is not None:
            _w(self.tel, tel_rows)
        res = route_join.join_labels(route_log_path=self.log, conformance_path=self.conf,
                                     telemetry_path=tel_path or self.tel)
        return res, {r.get("tool_use_id"): r for r in res["table"]
                     if r.get("surface") == "claude-code"}

    # R1 -----------------------------------------------------------------------------
    def test_default_telemetry_path_honours_apex_home(self):
        old = {k: os.environ.get(k) for k in ("APEX_TELEMETRY", "APEX_HOME")}
        try:
            os.environ.pop("APEX_TELEMETRY", None)
            os.environ["APEX_HOME"] = str(self.d / "home")
            self.assertEqual(route_join.default_telemetry_path(),
                             self.d / "home" / "telemetry.jsonl")
            os.environ["APEX_TELEMETRY"] = str(self.tel)
            self.assertEqual(route_join.default_telemetry_path(), self.tel)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    # R3 -----------------------------------------------------------------------------
    def test_inherit_start_is_never_a_cheap_start_or_escalation(self):
        # sonnet-resolved inherit row followed by an explicit opus redo: the inherit row's tier
        # came from the parent session, so it is not an escalation.
        res, by = self._join([
            _dispatch(100.0, "inherit", "Audit schema", "a1", "t1", resolved="claude-sonnet-5"),
            _dispatch(200.0, "opus", "Audit schema", "a2", "t2"),
        ])
        self.assertFalse(by["t1"]["escalated"])
        self.assertEqual(res["stats"]["escalated_inferred"], 0)

    def test_inherit_later_without_resolved_tier_does_not_escalate(self):
        _, by = self._join([
            _dispatch(100.0, "sonnet", "Audit schema", "a1", "t1"),
            _dispatch(200.0, "inherit", "Audit schema", "a2", "t2"),  # no resolved model
        ])
        self.assertFalse(by["t1"]["escalated"])

    def test_escalation_time_bound_two_hours(self):
        _, by = self._join([
            _dispatch(1000.0, "sonnet", "Fix the flaky test", "a1", "t1"),
            _dispatch(1000.0 + 7201, "opus", "Fix the flaky test", "a2", "t2"),
        ])
        self.assertFalse(by["t1"]["escalated"])

    def test_explicit_sonnet_to_opus_within_window_is_escalated(self):
        _, by = self._join([
            _dispatch(1000.0, "sonnet", "Fix the flaky test", "a1", "t1"),
            _dispatch(1000.0 + 3600, "opus", "Fix the flaky test", "a2", "t2"),
        ])
        self.assertTrue(by["t1"]["escalated"])
        self.assertEqual(by["t1"]["escalated_by"], "t2")
        self.assertEqual(by["t1"]["label_status"], "labeled")

    def test_read_rates_excludes_inherit_rows_from_labeled_table(self):
        labeled = self.d / "labeled_table.jsonl"
        _w(self.log, [])
        _w(labeled, [
            {"ts": 1.0, "task_type": "explore", "escalated": False, "surface": "claude-code",
             "requested_tier": "inherit", "effective_tier": "sonnet", "label_status": "labeled"},
            {"ts": 2.0, "task_type": "explore", "escalated": True, "surface": "claude-code",
             "requested_tier": "haiku", "effective_tier": "haiku", "label_status": "labeled"},
        ])
        rates = route_log.read_rates(log_path=self.log, labeled_path=labeled)
        self.assertEqual(rates["explore"]["n"], 1)
        self.assertEqual(rates["explore"]["escalated"], 1)

    # R4 -----------------------------------------------------------------------------
    def test_error_empty_and_unjoined_async_rows_are_unlabeled(self):
        res, by = self._join([
            _dispatch(100.0, "haiku", "one", "a1", "t1", outcome="error"),
            _dispatch(110.0, "haiku", "two", "a2", "t2", outcome="empty"),
            _dispatch(120.0, "haiku", "three", "a3", "t3", outcome="async"),  # no telemetry
            _dispatch(130.0, "haiku", "four", "a4", "t4", outcome="async"),   # joined, ok
            _dispatch(140.0, "haiku", "five", "a5", "t5"),                    # sync ok
        ], tel_rows=[_tel(131, S1, "a4", "claude-haiku-4-5")])
        for t in ("t1", "t2", "t3"):
            self.assertEqual(by[t]["label_status"], "unlabeled", t)
        for t in ("t4", "t5"):
            self.assertEqual(by[t]["label_status"], "labeled", t)
        self.assertEqual(by["t4"]["outcome_effective"], "ok")
        self.assertEqual(len(res["table"]), 5)  # unlabeled rows are kept in the table
        rates = route_join.cell_rates(res["table"])
        self.assertEqual(rates["explore"]["n"], 2)

    def test_outcome_effective_error_on_429_or_upstream_rejected(self):
        tel = [
            dict(_tel(101, S1, "a1", "claude-haiku-4-5"), error_cause="http_429"),
            dict(_tel(201, S1, "a2", "claude-haiku-4-5"), upstream_rejected=True),
            dict(_tel(301, S1, "a3", "claude-haiku-4-5"), upstream_rejected=False),
        ]
        _, by = self._join([
            _dispatch(100.0, "haiku", "one", "a1", "t1", outcome="async"),
            _dispatch(200.0, "haiku", "two", "a2", "t2", outcome="async"),
            _dispatch(300.0, "haiku", "three", "a3", "t3", outcome="async"),
        ], tel_rows=tel)
        self.assertEqual(by["t1"]["outcome_effective"], "error")
        self.assertEqual(by["t2"]["outcome_effective"], "error")
        self.assertEqual(by["t3"]["outcome_effective"], "ok")
        self.assertEqual(by["t1"]["label_status"], "unlabeled")
        self.assertEqual(by["t3"]["label_status"], "labeled")

    def test_read_rates_excludes_unlabeled_rows(self):
        labeled = self.d / "labeled_table.jsonl"
        _w(self.log, [])
        _w(labeled, [
            {"ts": 1.0, "task_type": "explore", "escalated": False, "surface": "claude-code",
             "requested_tier": "haiku", "effective_tier": "haiku", "label_status": "unlabeled"},
            {"ts": 2.0, "task_type": "explore", "escalated": False, "surface": "claude-code",
             "requested_tier": "haiku", "effective_tier": "haiku", "label_status": "labeled"},
        ])
        rates = route_log.read_rates(log_path=self.log, labeled_path=labeled)
        self.assertEqual(rates["explore"]["n"], 1)

    # R2 -----------------------------------------------------------------------------
    def test_read_errors_are_visible_in_stats(self):
        res = route_join.join_labels(route_log_path=self.d / "missing.jsonl",
                                     conformance_path=self.conf, telemetry_path=self.tel)
        self.assertIs(res["stats"]["route_log_error"], True)
        self.assertEqual(res["stats"]["route_log_error_name"], "FileNotFoundError")
        res, _ = self._join([_dispatch(100.0, "haiku", "x", "a1", "t1")],
                            tel_path=self.d / "no_tel.jsonl")
        self.assertIs(res["stats"]["telemetry_error"], True)
        self.assertEqual(res["stats"]["telemetry_error_name"], "FileNotFoundError")
        self.assertIs(res["stats"]["route_log_error"], False)

    def test_no_dispatch_rows_means_no_telemetry_error(self):
        res, _ = self._join([], tel_path=self.d / "no_tel.jsonl")
        self.assertIs(res["stats"]["telemetry_error"], False)

    def _seed_table(self):
        p = self.d / "labeled_table.jsonl"
        _w(p, [{"task_type": "explore", "surface": "claude-code", "escalated": False}])
        return p, p.read_text()

    def test_refresh_refuses_empty_table_over_existing(self):
        p, before = self._seed_table()
        res = {"table": [], "stats": {"route_log_error": False, "telemetry_error": False}}
        written, note = route_join.refresh_labeled_table(res, p)
        self.assertFalse(written)
        self.assertIn("kept previous table", note)
        self.assertEqual(p.read_text(), before)

    def test_refresh_refuses_on_read_error(self):
        p, before = self._seed_table()
        for flag in ("route_log_error", "telemetry_error"):
            st = {"route_log_error": False, "telemetry_error": False, flag: True,
                  flag + "_name": "PermissionError"}
            res = {"table": [{"task_type": "x"}], "stats": st}
            written, note = route_join.refresh_labeled_table(res, p)
            self.assertFalse(written, flag)
            self.assertIn("kept previous table", note)
            self.assertIn("PermissionError", note)
            self.assertEqual(p.read_text(), before)

    def test_refresh_normal_path_writes(self):
        p, _ = self._seed_table()
        res = {"table": [{"task_type": "debug"}],
               "stats": {"route_log_error": False, "telemetry_error": False}}
        written, note = route_join.refresh_labeled_table(res, p)
        self.assertTrue(written)
        self.assertEqual(json.loads(p.read_text())["task_type"], "debug")
        # no existing table: an empty result is still written (nothing to clobber)
        fresh = self.d / "fresh.jsonl"
        written, _ = route_join.refresh_labeled_table(
            {"table": [], "stats": {"route_log_error": False, "telemetry_error": False}}, fresh)
        self.assertTrue(written)

    def test_cli_route_join_keeps_table_when_route_log_missing(self):
        import contextlib
        import io
        p, before = self._seed_table()
        old = {k: os.environ.get(k) for k in
               ("APEX_ROUTER_LOG", "APEX_CONFORMANCE_LOG", "APEX_TELEMETRY", "APEX_LABELED_TABLE")}
        os.environ["APEX_ROUTER_LOG"] = str(self.d / "missing.jsonl")
        os.environ["APEX_CONFORMANCE_LOG"] = str(self.conf)
        os.environ["APEX_TELEMETRY"] = str(self.tel)
        os.environ["APEX_LABELED_TABLE"] = str(p)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(amr_cli.main(["route-join"]), 0)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(p.read_text(), before)
        self.assertIn("kept previous table", err.getvalue())



def _plugin(ts, tier, desc, agent_id, tool_use_id, *, sid=S1, outcome="ok"):
    """A datapce plugin route row: same schema plus inject_arm/injected (and end_reason when finished)."""
    row = _dispatch(ts, tier, desc, agent_id, tool_use_id, sid=sid, outcome=outcome)
    row.update({"inject_arm": "evidence", "injected": "no", "applied": "no"})
    if outcome in ("ok", "error"):
        row["end_reason"] = "answer" if outcome == "ok" else "error"
    return row


class TestDispatchDedupe(unittest.TestCase):
    """The still-wired agent-route-log.sh hook and the plugin both log one Agent dispatch:
    route_join keeps one row per (session_id, tool_use_id), the finished one first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.log = self.d / "route_log.jsonl"
        self.conf = self.d / "conformance.jsonl"
        self.tel = self.d / "telemetry.jsonl"
        _w(self.conf, [])
        _w(self.tel, [])

    def tearDown(self):
        self.tmp.cleanup()

    def _cc(self, rows):
        _w(self.log, rows)
        res = route_join.join_labels(route_log_path=self.log, conformance_path=self.conf,
                                     telemetry_path=self.tel)
        return res, [r for r in res["table"] if r.get("surface") == "claude-code"]

    def test_hook_async_and_plugin_ok_for_one_dispatch_is_one_row(self):
        res, cc = self._cc([
            _dispatch(100.0, "haiku", "Map the code", "a1", "t1", outcome="async"),  # hook, background
            _plugin(160.0, "haiku", "Map the code", "a1", "t1", outcome="ok"),        # plugin, finished
        ])
        self.assertEqual(len(cc), 1)
        self.assertEqual(cc[0]["outcome"], "ok")
        self.assertEqual(cc[0]["label_status"], "labeled")
        self.assertEqual(res["stats"]["claude_code_rows"], 1)
        self.assertEqual(res["stats"]["dispatch_deduped"], 1)

    def test_finished_row_wins_whichever_order(self):
        _, cc = self._cc([
            _plugin(100.0, "sonnet", "Fix it", "a1", "t1", outcome="error"),
            _dispatch(101.0, "sonnet", "Fix it", "a1", "t1", outcome="async"),
        ])
        self.assertEqual([r["outcome"] for r in cc], ["error"])

    def test_both_finished_prefers_the_plugin_row(self):
        _, cc = self._cc([
            _dispatch(100.0, "sonnet", "Fix it", "a1", "t1", outcome="ok"),
            _plugin(140.0, "sonnet", "Fix it", "a1", "t1", outcome="error"),
        ])
        self.assertEqual([(r["outcome"], r["ts"]) for r in cc], [("error", 140.0)])

    def test_a_duplicate_never_self_escalates(self):
        # hook rows log the requested tier, the plugin row the same dispatch: one row, no escalation
        res, cc = self._cc([
            _dispatch(100.0, "haiku", "Fix the flaky test", "a1", "t1", outcome="async"),
            _plugin(130.0, "haiku", "Fix the flaky test", "a1", "t1", outcome="ok"),
            _dispatch(200.0, "opus", "Fix the flaky test", "a2", "t2"),
        ])
        self.assertEqual([r["tool_use_id"] for r in cc], ["t1", "t2"])
        self.assertEqual(res["stats"]["escalated_inferred"], 1)
        self.assertEqual(cc[0]["escalated_by"], "t2")

    def test_rows_without_a_key_are_never_merged(self):
        _, cc = self._cc([
            _dispatch(100.0, "haiku", "A", None, None, outcome="async"),
            _dispatch(110.0, "haiku", "A", None, None, outcome="async"),
            _dispatch(120.0, "haiku", "B", "a3", "t3", sid=S1),
            _dispatch(130.0, "haiku", "B", "a3", "t3", sid=S2),  # same tool_use_id, other session
        ])
        self.assertEqual(len(cc), 4)


if __name__ == "__main__":
    unittest.main()


class TestResumedSessionSecondStretch(unittest.TestCase):
    """After /resume the engine reuses the resumed session's ORIGINAL id (live probe 2026-10-03):
    a second stretch of rows lands under an id that already has rows. Distinct dispatches stay
    distinct; escalation still needs the 2 h window."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.log, self.conf, self.tel = (self.d / n for n in ("route_log.jsonl", "conformance.jsonl", "telemetry.jsonl"))
        _w(self.conf, [])
        _w(self.tel, [])

    def tearDown(self):
        self.tmp.cleanup()

    def _cc(self, rows):
        _w(self.log, rows)
        res = route_join.join_labels(route_log_path=self.log, conformance_path=self.conf, telemetry_path=self.tel)
        return res, [r for r in res["table"] if r.get("surface") == "claude-code"]

    def test_second_stretch_keeps_every_dispatch_and_never_escalates_across_the_gap(self):
        res, cc = self._cc([
            _plugin(100.0, "haiku", "Fix the parser", "a1", "t1", sid=S1),
            _plugin(500.0, "sonnet", "Other work", "a2", "t2", sid=S2),            # after /clear
            _plugin(100.0 + 3 * 3600, "opus", "Fix the parser", "a3", "t3", sid=S1),  # after /resume S1
        ])
        self.assertEqual(sorted(r["tool_use_id"] for r in cc), ["t1", "t2", "t3"])
        self.assertEqual(res["stats"]["dispatch_deduped"], 0)
        self.assertEqual(res["stats"]["escalated_inferred"], 0)

    def test_second_stretch_inside_the_window_is_the_same_session_continuing(self):
        res, cc = self._cc([
            _plugin(100.0, "haiku", "Fix the parser", "a1", "t1", sid=S1),
            _plugin(400.0, "sonnet", "Other work", "a2", "t2", sid=S2),
            _plugin(1300.0, "opus", "Fix the parser", "a3", "t3", sid=S1),
        ])
        self.assertEqual(res["stats"]["escalated_inferred"], 1)
        self.assertEqual([r["escalated_by"] for r in cc if r["tool_use_id"] == "t1"], ["t3"])


def _day(d: int, sec: float = 3600.0) -> float:
    return datetime(2026, 10, d, tzinfo=timezone.utc).timestamp() + sec


def _wf(ts, agent_id):
    row = _plugin(ts, "haiku", "", agent_id, None)
    row["source"] = "workflow"
    return row


class TestWriterParity(unittest.TestCase):
    """0.4.1 retirement gate: plugin vs hook dispatch rows per UTC day, counted before dedupe."""

    def test_counts_both_writers_per_day_and_keeps_workflow_apart(self):
        p = route_join.writer_parity([
            _dispatch(_day(1), "haiku", "A", "a1", "t1", outcome="async"),
            _plugin(_day(1, 4000), "haiku", "A", "a1", "t1"),
            _wf(_day(1, 5000), "w1"),
            _dispatch(_day(2), "sonnet", "B", "a2", "t2", outcome="async"),
            _plugin(_day(2, 4000), "sonnet", "B", "a2", "t2"),
            _dispatch(_day(3), "opus", "C", "a3", "t3", outcome="async"),  # plugin missed it
        ])
        self.assertEqual(p["days"], {
            "2026-10-01": {"plugin": 1, "hook": 1, "workflow": 1},
            "2026-10-02": {"plugin": 1, "hook": 1, "workflow": 0},
            "2026-10-03": {"plugin": 0, "hook": 1, "workflow": 0},
        })
        self.assertIsNone(p["parity_since"])
        self.assertEqual(p["parity_span_days"], 0)

    def test_fourteen_days_of_parity_open_the_gate_and_quiet_days_do_not_break_it(self):
        rows = []
        for d in range(1, 16):
            if d == 8:
                continue  # a day with no dispatches at all
            rows += [_dispatch(_day(d), "haiku", f"D{d}", f"a{d}", f"t{d}", outcome="async"),
                     _plugin(_day(d, 60), "haiku", f"D{d}", f"a{d}", f"t{d}")]
        p = route_join.writer_parity(rows)
        self.assertEqual(p["parity_since"], "2026-10-01")
        self.assertEqual(p["parity_span_days"], 15)
        self.assertEqual(p["parity_days"], 14)

    def test_a_workflow_only_day_is_quiet_not_a_streak_break(self):
        rows = []
        for d in range(1, 15):
            if d == 5:
                rows.append(_wf(_day(d), "w5"))  # plugin=0 and hook=0: Workflow-only
                continue
            rows += [_dispatch(_day(d), "haiku", f"D{d}", f"a{d}", f"t{d}", outcome="async"),
                     _plugin(_day(d, 60), "haiku", f"D{d}", f"a{d}", f"t{d}")]
        p = route_join.writer_parity(rows)
        self.assertEqual(p["days"]["2026-10-05"], {"plugin": 0, "hook": 0, "workflow": 1})
        self.assertEqual((p["parity_since"], p["parity_span_days"]), ("2026-10-01", 14))
        self.assertEqual(p["parity_days"], 13)  # the quiet day spans but does not count

    def test_parity_days_counts_matched_days_in_the_streak(self):
        rows = [_dispatch(_day(1), "haiku", "X", "a0", "t0", outcome="async")]  # hook only: breaks
        for d in (2, 3, 4):
            rows += [_dispatch(_day(d), "haiku", f"D{d}", f"a{d}", f"t{d}", outcome="async"),
                     _plugin(_day(d, 60), "haiku", f"D{d}", f"a{d}", f"t{d}")]
        self.assertEqual(route_join.writer_parity(rows)["parity_days"], 3)
        self.assertEqual(route_join.writer_parity([])["parity_days"], 0)

    def test_gate_needs_recency_not_just_a_long_old_streak(self):
        rows = []
        for d in range(1, 15):
            rows += [_dispatch(_day(d), "haiku", f"D{d}", f"a{d}", f"t{d}", outcome="async"),
                     _plugin(_day(d, 60), "haiku", f"D{d}", f"a{d}", f"t{d}")]
        from datetime import date
        fresh = route_join.writer_parity(rows, today=date(2026, 10, 16))
        self.assertEqual((fresh["parity_until"], fresh["gate_open"]), ("2026-10-14", True))
        stale = route_join.writer_parity(rows, today=date(2026, 10, 17))
        self.assertEqual((stale["parity_span_days"], stale["parity_days"], stale["gate_open"]), (14, 14, False))
        short = route_join.writer_parity(rows[:20], today=date(2026, 10, 10))
        self.assertFalse(short["gate_open"])
        self.assertFalse(route_join.writer_parity([], today=date(2026, 10, 10))["gate_open"])

    def test_dispatch_spanning_midnight_utc_matches_by_key_not_by_day_counts(self):
        rows = []
        for d in range(1, 15):
            rows += [_dispatch(_day(d), "haiku", f"D{d}", f"a{d}", f"t{d}", outcome="async"),
                     _plugin(_day(d, 60), "haiku", f"D{d}", f"a{d}", f"t{d}")]
        # plugin ts = spawn (23:59 on day 7), hook ts = PostToolUse end (00:05 on day 8)
        rows = [r for r in rows if r["tool_use_id"] != "t7"]
        rows += [_plugin(_day(7, 86340), "haiku", "D7", "a7", "t7"),
                 _dispatch(_day(8, 300), "haiku", "D7", "a7", "t7", outcome="async")]
        p = route_join.writer_parity(rows)
        self.assertEqual((p["parity_since"], p["parity_span_days"], p["parity_days"]), ("2026-10-01", 14, 14))
        self.assertEqual(p["days"]["2026-10-07"]["hook"], 1)  # the hook row lands on the plugin row's day
        self.assertEqual(p["days"]["2026-10-08"]["hook"], 1)

    def test_a_hook_dispatch_with_no_plugin_row_breaks_even_if_counts_match(self):
        rows = [_dispatch(_day(1), "haiku", "X", "a1", "t1", outcome="async"),
                _plugin(_day(1, 60), "haiku", "Y", "a2", "t2")]
        p = route_join.writer_parity(rows)
        self.assertIsNone(p["parity_since"])

    def test_long_span_with_too_few_matched_days_keeps_the_gate_closed(self):
        from datetime import date
        rows = [_wf(_day(d), f"w{d}") for d in range(2, 15)]  # Workflow-only days span but do not count
        rows += [_dispatch(_day(1), "haiku", "D1", "a1", "t1", outcome="async"),
                 _plugin(_day(1, 60), "haiku", "D1", "a1", "t1"),
                 _dispatch(_day(15), "haiku", "D15", "a15", "t15", outcome="async"),
                 _plugin(_day(15, 60), "haiku", "D15", "a15", "t15")]
        p = route_join.writer_parity(rows, today=date(2026, 10, 16))
        self.assertEqual((p["parity_span_days"], p["parity_days"]), (15, 2))
        self.assertFalse(p["gate_open"])

    def test_an_older_shortfall_only_moves_the_start(self):
        rows = [_dispatch(_day(1), "haiku", "X", "a0", "t0", outcome="async")]  # hook only
        for d in (2, 3, 4):
            rows += [_dispatch(_day(d), "haiku", f"D{d}", f"a{d}", f"t{d}", outcome="async"),
                     _plugin(_day(d, 60), "haiku", f"D{d}", f"a{d}", f"t{d}")]
        p = route_join.writer_parity(rows)
        self.assertEqual((p["parity_since"], p["parity_span_days"]), ("2026-10-02", 3))

    def test_join_labels_reports_writer_parity(self):
        d = Path(tempfile.mkdtemp())
        log, conf, tel = d / "route_log.jsonl", d / "conformance.jsonl", d / "telemetry.jsonl"
        _w(conf, [])
        _w(tel, [])
        _w(log, [_dispatch(_day(1), "haiku", "A", "a1", "t1", outcome="async"), _plugin(_day(1, 9), "haiku", "A", "a1", "t1")])
        st = route_join.join_labels(route_log_path=log, conformance_path=conf, telemetry_path=tel)["stats"]
        self.assertEqual(st["writer_parity"]["days"], {"2026-10-01": {"plugin": 1, "hook": 1, "workflow": 0}})
        self.assertEqual(st["dispatch_deduped"], 1)
        self.assertEqual(st["writer_parity"]["parity_days"], 1)


class TestModuleEntryPoint(unittest.TestCase):
    """`python -m apex_router.route_join` must run main(), not import silently (U1 finding)."""

    def test_python_dash_m_prints_json_and_writes_nothing(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            log = d / "route_log.jsonl"
            _w(log, [_dispatch(1_700_000_000, "haiku", "Find X", "a1", "toolu_1")])
            labeled = d / "labeled_table.jsonl"
            env = {**os.environ, "PYTHONPATH": str(SRC), "APEX_ROUTER_LOG": str(log),
                   "APEX_LABELED_TABLE": str(labeled), "APEX_TELEMETRY": str(d / "telemetry.jsonl"),
                   "APEX_HOME": str(d), "HOME": str(d)}
            p = subprocess.run([sys.executable, "-m", "apex_router.route_join", "--no-write", "--json"],
                               capture_output=True, text=True, env=env, timeout=60, cwd=d)
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout)
            self.assertEqual(out["stats"]["claude_code_rows"], 1)
            self.assertFalse(labeled.exists())
