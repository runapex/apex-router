"""Tests for the Claude Code Agent-dispatch label source.

hooks/agent-route-log.sh (PostToolUse, matcher "Agent") → `python -m apex_router.route_log
--hook` → one label-pending row in the route log. Plus the route_log extensions: new
dispatch fields, back-compat of the classic ok|escalated contract, and read_rates folding in
resolved claude-code rows from the route-join labeled table. Hermetic: every path is temp.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from apex_router import route_log  # noqa: E402
from apex_router import cli as amr_cli  # noqa: E402

HOOK = ROOT / "hooks" / "agent-route-log.sh"

ASYNC_RESPONSE = {
    "isAsync": True, "status": "async_launched", "agentId": "a89a3bf5d2cf1916e",
    "description": "Map apex-router architecture", "resolvedModel": "claude-opus-5-5",
    "prompt": "Very thorough exploration ...", "outputFile": "/tmp/x.output",
    "canReadOutputFile": True,
}


def _payload(**over):
    d = {
        "session_id": "03aa612d-7c16-4bd5-8db8-2cc6481e81e0",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/tmp",
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_use_id": "toolu_01ABC",
        "tool_input": {
            "subagent_type": "general-purpose",
            "model": "sonnet",
            "description": "Fix the flaky join test",
            "prompt": "Implement a fix for the flaky test in tests/test_route_join.py. " * 20,
        },
        "tool_response": {
            "status": "completed",
            "agentId": "aa256fd6f2b88ebce",
            "content": [{"type": "text", "text": "Done. Fixed it."}],
        },
    }
    d.update(over)
    return d


def _rows(p):
    p = Path(p)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


class TestHookSubprocess(unittest.TestCase):
    def _run(self, payload, log, extra_env=None):
        env = dict(os.environ)
        env["APEX_ROUTER_LOG"] = str(log)
        env.pop("APEX_ROUTE_LOG_PYTHON", None)
        env.update(extra_env or {})
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        t0 = time.monotonic()
        proc = subprocess.run(["bash", str(HOOK)], input=raw, capture_output=True,
                              text=True, env=env, timeout=10)
        return proc, time.monotonic() - t0

    def test_hook_appends_one_row_with_the_dispatch_schema(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "route_log.jsonl"
            proc, elapsed = self._run(_payload(), log)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")  # never alters the tool result
            self.assertEqual(proc.stderr, "")
            self.assertLess(elapsed, 2.0)
            rows = _rows(log)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r["surface"], "claude-code")
            self.assertEqual(r["session_id"], "03aa612d-7c16-4bd5-8db8-2cc6481e81e0")
            self.assertEqual(r["agent_id"], "aa256fd6f2b88ebce")
            self.assertEqual(r["tool_use_id"], "toolu_01ABC")
            self.assertEqual(r["task_type"], "generate")  # "fix"/"implement"
            self.assertEqual(r["start_tier"], "sonnet")
            self.assertEqual(r["model"], "sonnet")
            self.assertEqual(r["outcome"], "ok")
            self.assertEqual(r["description"], "Fix the flaky join test")
            self.assertIs(r["label_pending"], True)
            self.assertIs(r["escalated"], False)
            self.assertIsNone(r["passed"])
            self.assertIsInstance(r["ts"], float)
            self.assertNotIn("prompt", r)  # prompt head is used to classify, not stored

    def test_async_launch_response_inherit_tier_and_resolved_model(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "route_log.jsonl"
            ti = {"subagent_type": "Explore", "description": "Map the architecture",
                  "prompt": "Very thorough exploration"}
            proc, _ = self._run(_payload(tool_input=ti, tool_response=ASYNC_RESPONSE), log)
            self.assertEqual(proc.returncode, 0)
            r = _rows(log)[0]
            self.assertEqual(r["start_tier"], "inherit")
            self.assertEqual(r["outcome"], "async")
            self.assertEqual(r["task_type"], "explore")
            self.assertEqual(r["agent_id"], "a89a3bf5d2cf1916e")
            self.assertEqual(r["resolved_model"], "claude-opus-5-5")

    def test_agent_id_parsed_from_text_result(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "route_log.jsonl"
            resp = "Report...\nagentId: a1186a4050075d1aa\n"
            self._run(_payload(tool_response=resp), log)
            self.assertEqual(_rows(log)[0]["agent_id"], "a1186a4050075d1aa")

    def test_agent_id_not_captured_from_nested_free_text(self):
        # R5: a dict response whose subagent text mentions ANOTHER agent's id must not yield it.
        nested = {"status": "completed",
                  "content": [{"type": "text",
                               "text": "I spawned a helper.\nagentId: anested0000000001\n"}]}
        self.assertIsNone(route_log._agent_id_from(nested))
        nested["agentId"] = "aouter0000000001"
        self.assertEqual(route_log._agent_id_from(nested), "aouter0000000001")
        # strings: anchored to a line start, so inline mentions don't match
        self.assertIsNone(route_log._agent_id_from('see {"agentId": "ainline000000001"}'))
        self.assertEqual(route_log._agent_id_from("done\nagentId: aline00000000001"),
                         "aline00000000001")

    def test_non_agent_tool_and_garbage_input_write_nothing_and_exit_0(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "route_log.jsonl"
            for payload in (_payload(tool_name="Bash"), "not json {", "", "[]"):
                proc, _ = self._run(payload, log)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
            self.assertEqual(_rows(log), [])

    def test_unwritable_log_still_exits_0_silently(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "dir_not_file"
            log.mkdir()
            proc, _ = self._run(_payload(), log)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout + proc.stderr, "")

    def test_missing_python_override_exits_0(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "route_log.jsonl"
            proc, _ = self._run(_payload(), log,
                                {"APEX_ROUTE_LOG_PYTHON": "/nonexistent/python"})
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(_rows(log), [])


class TestClassifyAndOutcome(unittest.TestCase):
    def test_classify_dispatch_rules(self):
        c = route_log.classify_dispatch
        self.assertEqual(c("Explore", "Fix everything", "implement"), "explore")
        self.assertEqual(c("general-purpose", "Review the diff", ""), "review")
        self.assertEqual(c(None, "Audit telemetry schema", None), "review")
        self.assertEqual(c(None, "Build the hook", None), "generate")
        self.assertEqual(c(None, "Find root cause of 429s", None), "debug")
        self.assertEqual(c(None, "Rename the module", None), "refactor")
        self.assertEqual(c(None, "Look around", None), "explore")
        self.assertEqual(c("Plan", "Design the join", None), "explore")  # classify marker prior
        self.assertEqual(c(None, None, None), "explore")

    def test_dispatch_outcome(self):
        o = route_log.dispatch_outcome
        self.assertEqual(o(None), "empty")
        self.assertEqual(o({"content": []}), "empty")
        self.assertEqual(o({"content": [{"type": "text", "text": "  "}]}), "empty")
        self.assertEqual(o({"status": "error", "content": "x"}), "error")
        self.assertEqual(o("Error: agent crashed"), "error")
        self.assertEqual(o("<tool_use_error>bad</tool_use_error>"), "error")
        self.assertEqual(o(ASYNC_RESPONSE), "async")
        self.assertEqual(o({"content": [{"type": "text", "text": "All good"}]}), "ok")


class TestLogOutcomeDispatchFields(unittest.TestCase):
    def test_label_pending_row_accepts_dispatch_outcomes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.jsonl"
            for oc in ("ok", "error", "empty", "async"):
                self.assertTrue(route_log.log_outcome(
                    "explore", "haiku", oc, log_path=p, label_pending=True,
                    surface="claude-code", agent_id="a1", tool_use_id="t1",
                    start_tier="haiku", description="x" * 500))
            rows = _rows(p)
            self.assertEqual([r["outcome"] for r in rows], ["ok", "error", "empty", "async"])
            self.assertEqual(len(rows[0]["description"]), 200)

    def test_label_pending_rejects_escalated_and_bad_field_types(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.jsonl"
            self.assertFalse(route_log.log_outcome("explore", "haiku", "escalated",
                                                   log_path=p, label_pending=True))
            self.assertFalse(route_log.log_outcome("explore", "haiku", "ok", log_path=p,
                                                   label_pending=True, agent_id=123))
            self.assertFalse(route_log.log_outcome("explore", "haiku", "ok", log_path=p,
                                                   label_pending="yes"))
            self.assertFalse(p.exists())

    def test_classic_contract_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.jsonl"
            self.assertFalse(route_log.log_outcome("explore", "haiku", "error", log_path=p))
            self.assertTrue(route_log.log_outcome("explore", "haiku", "escalated", log_path=p,
                                                  ts=1.0, note="n", session_id="s"))
            self.assertEqual(_rows(p), [{"ts": 1.0, "task_type": "explore", "model": "haiku",
                                         "passed": False, "escalated": True, "note": "n",
                                         "session_id": "s"}])

    def test_cli_route_log_old_flags_and_new_flags(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.jsonl"
            old = os.environ.get("APEX_ROUTER_LOG")
            os.environ["APEX_ROUTER_LOG"] = str(p)
            try:
                amr_cli.main(["route-log", "--task-type", "debug", "--start-tier", "sonnet",
                              "--outcome", "ok"])
                amr_cli.main(["route-log", "--task-type", "review", "--start-tier", "haiku",
                              "--outcome", "async", "--label-pending", "--surface",
                              "claude-code", "--agent-id", "a2", "--tool-use-id", "t2",
                              "--description", "Review it", "--session-id", "s1"])
            finally:
                if old is None:
                    os.environ.pop("APEX_ROUTER_LOG", None)
                else:
                    os.environ["APEX_ROUTER_LOG"] = old
            rows = _rows(p)
            self.assertEqual(len(rows), 2)
            self.assertNotIn("surface", rows[0])
            self.assertTrue(rows[0]["passed"])
            self.assertEqual(rows[1]["start_tier"], "haiku")
            self.assertEqual(rows[1]["agent_id"], "a2")
            self.assertIs(rows[1]["label_pending"], True)


class TestReadRatesWithLabeledTable(unittest.TestCase):
    def test_pending_rows_skipped_and_labeled_cheap_rows_folded_in(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "route_log.jsonl"
            route_log.log_outcome("explore", "sonnet", "escalated", log_path=log)
            route_log.log_outcome("explore", "haiku", "ok", log_path=log,
                                  label_pending=True, surface="claude-code")
            labeled = Path(d) / "labeled_table.jsonl"
            rows = [
                {"ts": 1.0, "task_type": "explore", "escalated": True,
                 "surface": "claude-code", "requested_tier": "haiku",
                 "effective_tier": "haiku"},
                {"ts": 2.0, "task_type": "review", "escalated": False,
                 "surface": "claude-code", "requested_tier": "sonnet",
                 "effective_tier": "sonnet"},
                # heavy start: not a cheap start → excluded from rates
                {"ts": 3.0, "task_type": "review", "escalated": False,
                 "surface": "claude-code", "requested_tier": "opus",
                 "effective_tier": "opus"},
                # non-claude-code rows in the table are already counted from the raw log
                {"ts": 4.0, "task_type": "explore", "escalated": False, "surface": "pi"},
                "garbage",
            ]
            labeled.write_text("\n".join(json.dumps(r) for r in rows) + "\n{bad\n")
            rates = route_log.read_rates(log_path=log)
            self.assertEqual(rates["explore"]["n"], 2)
            self.assertEqual(rates["explore"]["escalated"], 2)
            self.assertEqual(rates["review"]["n"], 1)

    def test_old_rows_still_readable(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "route_log.jsonl"
            log.write_text(json.dumps({"ts": None, "task_type": "extract", "model": "ornith",
                                       "passed": True, "escalated": False, "note": ""}) + "\n")
            rates = route_log.read_rates(log_path=log)
            self.assertEqual(rates["extract"]["n"], 1)
            self.assertEqual(rates["extract"]["null_ts"], 1)


if __name__ == "__main__":
    unittest.main()
