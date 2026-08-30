"""SKILL.state codegen lane + A/B bench — offline tests, no server.

The pure pieces (merge/validate/parse/prompt) are pinned directly; the lane loop and both
bench arms run against scripted fake chats. The load-bearing claims under test:
  - patch-only updates + server-side merge make the paper's 68% overwrite mode impossible
    to commit silently (a delete of "code" is REJECTED and counted, state preserved);
  - invalid patches never touch the state and are fed back as observations, bounded;
  - the repair loop's second prompt carries (spec, state, test output) and NO prior reasoning;
  - verdict doctrine is unchanged: ok == caller tests passed == gated; usage is SUMMED across
    calls so retry cost is booked honestly;
  - the bench aggregates both arms and surfaces the pass-rate delta + token economics.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith import dispatch  # noqa: E402
from apex_router.ornith.state_codegen import (  # noqa: E402
    TAX_JSON, TAX_OVERWRITE, TAX_SCHEMA, build_prompt, merge_patch, parse_model_patch,
    state_codegen_lane, validate_patch)
from apex_router.ornith.state_bench import (  # noqa: E402
    BUILTIN_SUITE, FakeModel, Task, run_bench, summarize, wilson_ci, render_report)


# ------------------------------------------------------------------------------------------
# Pure pieces
# ------------------------------------------------------------------------------------------

class TestMergePatch(unittest.TestCase):
    def test_add_overwrite_delete(self):
        s = {"code": "v1", "open_issues": ["a"]}
        s = merge_patch(s, {"code": "v2"})
        self.assertEqual(s["code"], "v2")
        self.assertEqual(s["open_issues"], ["a"])  # unmentioned keys preserved
        s = merge_patch(s, {"open_issues": None})
        self.assertNotIn("open_issues", s)


class TestValidatePatch(unittest.TestCase):
    def test_valid(self):
        self.assertIsNone(validate_patch({"code": "x=1", "fix_summary": "init"}, {}))

    def test_unknown_key_is_schema_error(self):
        kind, _ = validate_patch({"cod": "x=1"}, {})
        self.assertEqual(kind, TAX_SCHEMA)

    def test_wrong_type_is_schema_error(self):
        kind, _ = validate_patch({"code": 42}, {})
        self.assertEqual(kind, TAX_SCHEMA)

    def test_open_issues_must_be_str_list(self):
        kind, _ = validate_patch({"code": "x=1", "open_issues": [1, 2]}, {})
        self.assertEqual(kind, TAX_SCHEMA)

    def test_delete_code_is_premature_overwrite(self):
        kind, _ = validate_patch({"code": None}, {"code": "x=1"})
        self.assertEqual(kind, TAX_OVERWRITE)

    def test_first_patch_without_code_rejected(self):
        kind, _ = validate_patch({"fix_summary": "thinking"}, {})
        self.assertEqual(kind, TAX_SCHEMA)

    def test_later_patch_without_code_ok(self):
        # code survives via merge — only updating other keys is legal
        self.assertIsNone(validate_patch({"open_issues": ["x"]}, {"code": "x=1"}))


class TestParseModelPatch(unittest.TestCase):
    def test_clean_json(self):
        patch, salvage = parse_model_patch('{"code": "x=1", "fix_summary": "s"}')
        self.assertEqual(patch["code"], "x=1")
        self.assertIsNone(salvage)

    def test_fenced_json(self):
        patch, salvage = parse_model_patch('```json\n{"code": "x=1"}\n```')
        self.assertEqual(patch["code"], "x=1")
        self.assertIsNone(salvage)

    def test_prose_wrapped_json(self):
        patch, _ = parse_model_patch('Here you go:\n{"code": "x=1"}\nDone.')
        self.assertEqual(patch["code"], "x=1")

    def test_bare_fenced_code_salvaged_and_counted(self):
        patch, salvage = parse_model_patch('```python\ndef f():\n    return 1\n```')
        self.assertIn("def f()", patch["code"])
        self.assertEqual(salvage, TAX_JSON)

    def test_garbage_unusable(self):
        patch, salvage = parse_model_patch("I cannot help with that.")
        self.assertIsNone(patch)
        self.assertEqual(salvage, TAX_JSON)


class TestPrompt(unittest.TestCase):
    def test_order_and_contents(self):
        p = build_prompt("SPEC-HERE", {"code": "x=1"}, "OBS-HERE")
        self.assertLess(p.index("SPEC-HERE"), p.index("CURRENT STATE"))
        self.assertLess(p.index("CURRENT STATE"), p.index("OBS-HERE"))
        self.assertIn('"code": "x=1"', p)
        self.assertIn("PATCH", p)


# ------------------------------------------------------------------------------------------
# Lane loop with scripted chats
# ------------------------------------------------------------------------------------------

GOOD = "def add(a, b):\n    return a + b\n"
BUGGY = "def add(a, b):\n    return a - b\n"
TESTS = "def test_add():\n    assert add(2, 3) == 5\n    assert add(-1, 1) == 0\n"
SPEC = "Write add(a, b)."


class _R:
    def __init__(self, answer, p=100, c=50, k=20):
        self.answer = answer
        self.usage = {"prompt_tokens": p, "completion_tokens": c,
                      "prompt_tokens_details": {"cached_tokens": k}}


def _script(replies):
    """Chat stub replaying `replies` in order, recording prompts."""
    calls = {"prompts": [], "i": 0}

    def chat(messages, *, max_tokens, enable_thinking):
        assert enable_thinking is False  # measured doctrine: codegen stays thinking-OFF
        calls["prompts"].append(messages[-1]["content"])
        r = replies[min(calls["i"], len(replies) - 1)]
        calls["i"] += 1
        if isinstance(r, Exception):
            raise r
        return _R(r)
    return chat, calls


class TestStateLane(unittest.TestCase):
    def test_pass_first_attempt(self):
        chat, calls = _script([json.dumps({"code": GOOD, "fix_summary": "init"})])
        res = state_codegen_lane(SPEC, TESTS, chat=chat)
        self.assertTrue(res.ok)
        self.assertFalse(res.escalate)
        self.assertTrue(res.gated)
        self.assertEqual(res._extra["attempts"], 1)
        self.assertEqual(res._extra["calls"], 1)
        self.assertEqual(res.usage["prompt_tokens"], 100)
        self.assertEqual(res.usage["completion_tokens"], 50)

    def test_repair_loop_passes_on_second_attempt(self):
        chat, calls = _script([
            json.dumps({"code": BUGGY, "fix_summary": "draft"}),
            json.dumps({"code": GOOD, "fix_summary": "fixed subtraction"}),
        ])
        res = state_codegen_lane(SPEC, TESTS, chat=chat)
        self.assertTrue(res.ok)
        self.assertEqual(res._extra["attempts"], 2)
        # second prompt = (spec, state, test observation): failure fed back, state carried,
        # and NO first-attempt reasoning anywhere (the prompt is built from state alone).
        p2 = calls["prompts"][1]
        self.assertIn("TESTS FAILED (attempt 1/3)", p2)
        self.assertIn('"fix_summary": "draft"', p2)
        self.assertIn("AssertionError", p2)
        # usage summed across both calls — retry cost is booked
        self.assertEqual(res.usage["prompt_tokens"], 200)
        self.assertEqual(res.usage["prompt_tokens_details"]["cached_tokens"], 40)

    def test_never_passes_escalates_with_structured_state(self):
        chat, _ = _script([json.dumps({"code": BUGGY, "fix_summary": "still wrong",
                                       "open_issues": ["operator"]})])
        res = state_codegen_lane(SPEC, TESTS, chat=chat, max_attempts=2)
        self.assertFalse(res.ok)
        self.assertTrue(res.escalate)
        self.assertTrue(res.gated)  # tests ran; the fail verdict is earned
        self.assertEqual(res._extra["attempts"], 2)
        self.assertEqual(res.output, BUGGY)  # best candidate rides up with the escalation
        payload = json.loads(res.detail.split("state lane escalated: ", 1)[1])
        self.assertEqual(payload["attempts"], 2)
        self.assertEqual(payload["open_issues"], ["operator"])
        self.assertIn("AssertionError", payload["last_failure"])

    def test_invalid_patches_rejected_counted_bounded(self):
        chat, calls = _script([
            "no json here at all",
                '{"code": null}',  # premature overwrite attempt — must NOT clear state
            json.dumps({"code": GOOD}),
        ])
        res = state_codegen_lane(SPEC, TESTS, chat=chat)
        self.assertTrue(res.ok)
        self.assertEqual(res._extra["rejected"], 2)
        self.assertEqual(res._extra["taxonomy"][TAX_JSON], 1)
        self.assertEqual(res._extra["taxonomy"][TAX_OVERWRITE], 1)
        self.assertEqual(res._extra["attempts"], 1)
        # rejections were fed back as observations
        self.assertIn("PATCH REJECTED", calls["prompts"][1])

    def test_all_invalid_escalates_ungated(self):
        chat, _ = _script(["garbage"])
        res = state_codegen_lane(SPEC, TESTS, chat=chat, max_attempts=3, max_patch_retries=2)
        self.assertTrue(res.escalate)
        self.assertFalse(res.gated)  # tests never ran — no earned verdict
        self.assertEqual(res._extra["attempts"], 0)
        self.assertEqual(res.usage["prompt_tokens"], 300)  # 3 calls, all booked

    def test_salvaged_fenced_code_counts_taxonomy_and_runs(self):
        chat, _ = _script([f"```python\n{GOOD}```"])  # ignores JSON contract, code is right
        res = state_codegen_lane(SPEC, TESTS, chat=chat)
        self.assertTrue(res.ok)
        self.assertEqual(res._extra["taxonomy"][TAX_JSON], 1)

    def test_model_exception_escalates_never_raises(self):
        chat, _ = _script([RuntimeError("server down")])
        res = state_codegen_lane(SPEC, TESTS, chat=chat)
        self.assertTrue(res.escalate)
        self.assertIn("state_lane_call_failed", res.detail)


# ------------------------------------------------------------------------------------------
# Dispatch wiring
# ------------------------------------------------------------------------------------------

class TestDispatchWiring(unittest.TestCase):
    JOB = {"lane": "codegen", "spec": SPEC, "tests": TESTS}

    def test_default_off_uses_injected_stub(self):
        seen = []

        def stub(spec, tests, *, max_tokens=1200, timeout_s=30):
            seen.append(spec)
            from apex_router.ornith.offload_lanes import LaneResult
            return LaneResult("codegen", ok=True, escalate=False, output="", gated=True)

        with mock.patch.dict(os.environ, {"ORNITH_CODEGEN_STATE_LANE": "on"}):
            dispatch.run_job(self.JOB, codegen=stub)
        self.assertEqual(seen, [SPEC])  # injected runner is never bypassed by the flag

    def test_flag_helper_fails_safe(self):
        self.assertFalse(dispatch._state_lane_enabled(env={}))
        self.assertFalse(dispatch._state_lane_enabled(env={"ORNITH_CODEGEN_STATE_LANE": "onish"}))
        self.assertTrue(dispatch._state_lane_enabled(env={"ORNITH_CODEGEN_STATE_LANE": "ON"}))

    def test_flag_on_default_runner_calls_state_lane(self):
        import apex_router.ornith.state_codegen as sc
        with mock.patch.dict(os.environ, {"ORNITH_CODEGEN_STATE_LANE": "1"}), \
                mock.patch.object(sc, "state_codegen_lane",
                                  wraps=lambda *a, **k: dispatch._default_codegen(*a, **k)) as m:
            # wraps would call the live lane; instead just verify dispatch routed to it
            m.side_effect = lambda spec, tests, *, max_tokens: "STATE-LANE-RAN"
            res = dispatch.run_job(self.JOB)
        self.assertEqual(res, "STATE-LANE-RAN")
        m.assert_called_once()


# ------------------------------------------------------------------------------------------
# Bench
# ------------------------------------------------------------------------------------------

class TestBench(unittest.TestCase):
    ADD_TASK = Task("add", SPEC, TESTS)

    def test_ab_state_repairs_oneshot_cannot(self):
        chat = FakeModel({"add": (BUGGY, GOOD)})
        rows = run_bench([self.ADD_TASK], chat=chat)
        by_arm = {r["arm"]: r for r in rows}
        self.assertFalse(by_arm["oneshot"]["ok"])      # one-shot: buggy, no repair path
        self.assertTrue(by_arm["state"]["ok"])         # state: repaired on attempt 2
        self.assertEqual(by_arm["state"]["attempts"], 2)
        # token booking: state spent 2 calls = 300 tokens, oneshot 1 call = 150
        self.assertEqual(by_arm["oneshot"]["prompt_tokens"], 100)
        self.assertEqual(by_arm["state"]["prompt_tokens"], 200)

        rep = summarize(rows)
        self.assertEqual(rep["arms"]["state"]["pass_rate"], 1.0)
        self.assertEqual(rep["arms"]["oneshot"]["pass_rate"], 0.0)
        self.assertEqual(rep["signals"]["pass_delta"], 1.0)
        text = render_report(rep)
        self.assertIn("POSITIVE", text)

    def test_rows_written(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "rows.jsonl"
            rows = run_bench([self.ADD_TASK], chat=FakeModel({}), out_path=out)
            lines = out.read_text().strip().splitlines()
            self.assertEqual(len(lines), len(rows))
            for line in lines:
                r = json.loads(line)
                self.assertIn(r["arm"], ("oneshot", "state"))
                self.assertIn("taxonomy", r)

    def test_unscripted_task_records_salvage_taxonomy(self):
        # FakeModel falls back to a bare fenced block for unscripted specs -> state arm's
        # parse fails over to salvage, counted as json_syntax. Tests reference a function the
        # fallback code never defines, so all 3 attempts run and all 3 calls are salvaged.
        rows = run_bench([Task("x", "unscripted spec",
                               "def test_x():\n    assert missing_fn() == 1\n")],
                         arms=("state",), chat=FakeModel({}))
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(rows[0]["attempts"], 3)
        self.assertEqual(rows[0]["taxonomy"].get(TAX_JSON), 3)  # every call salvaged

    def test_wilson(self):
        self.assertEqual(wilson_ci(0, 0), (0.0, 1.0))
        lo, hi = wilson_ci(10, 10)
        self.assertGreater(lo, 0.5)
        self.assertEqual(hi, 1.0 if hi > 1 else hi)
        lo0, _ = wilson_ci(0, 10)
        self.assertEqual(lo0, 0.0)

    def test_builtin_suite_wellformed(self):
        self.assertGreaterEqual(len(BUILTIN_SUITE), 5)
        for t in BUILTIN_SUITE:
            self.assertTrue(t.spec and t.tests and t.id)


if __name__ == "__main__":
    unittest.main()
