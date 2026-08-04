"""Lane dispatch for the drain worker.

run_job routes a queued job to the right lane by its `lane` field and returns a LaneResult the
worker records. The routing (which lane, what gated/escalate verdict, safe fallback, missing-field
handling) is what these tests pin — the lane runners are INJECTED as stubs so no server is needed.
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith.offload_lanes import LaneResult  # noqa: E402
from apex_router.ornith.dispatch import run_job  # noqa: E402


class _Stubs:
    """Injectable lane runners that record calls and return canned LaneResults."""
    def __init__(self):
        self.calls = []

    def chat(self, messages, *, max_tokens, enable_thinking):
        self.calls.append(("chat", enable_thinking))
        class R:  # minimal ChatResult stand-in
            answer = "raw answer"
            reasoning = None
            finish_reason = "stop"
            usage = {"prompt_tokens": 10, "completion_tokens": 4,
                     "prompt_tokens_details": {"cached_tokens": 2}}
        return R()

    def codegen(self, spec, tests, *, max_tokens=1200, timeout_s=30):
        self.calls.append(("codegen", spec, tests))
        return LaneResult("codegen", ok=True, escalate=False, output="def f(): pass",
                          usage={"prompt_tokens": 30, "completion_tokens": 20}, gated=True)

    def review(self, preamble, diff, *, max_tokens=512):
        self.calls.append(("review", diff))
        return LaneResult("review", ok=True, escalate=True, output="finding: x",
                          usage={"prompt_tokens": 50, "completion_tokens": 25}, gated=False)


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.s = _Stubs()

    def _run(self, job):
        return run_job(job, chat=self.s.chat, codegen=self.s.codegen, review=self.s.review)

    def test_codegen_job_routes_to_codegen_and_is_gated(self):
        res = self._run({"lane": "codegen", "spec": "write f", "tests": "def test_f():\n    pass\n"})
        self.assertEqual(res.lane, "codegen")
        self.assertTrue(res.gated)               # tests ran -> gated verdict earned
        self.assertTrue(res.ok)
        self.assertFalse(res.escalate)
        self.assertEqual(self.s.calls[0][0], "codegen")

    def test_review_job_routes_to_review_and_always_escalates_ungated(self):
        res = self._run({"lane": "review", "diff": "def g(): return 1/0"})
        self.assertEqual(res.lane, "review")
        self.assertFalse(res.gated)              # pre-filter has no correctness gate
        self.assertTrue(res.escalate)            # always escalate for triage
        self.assertEqual(self.s.calls[0][0], "review")

    def test_adhoc_job_is_raw_chat_ungated(self):
        res = self._run({"lane": "adhoc", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(res.lane, "adhoc")
        self.assertFalse(res.gated)
        self.assertFalse(res.ok)                 # raw completion is never an earned pass
        self.assertEqual(self.s.calls[0], ("chat", False))   # thinking-OFF default

    def test_missing_lane_defaults_to_adhoc(self):
        res = self._run({"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(res.lane, "adhoc")
        self.assertEqual(self.s.calls[0][0], "chat")

    def test_unknown_lane_falls_back_to_adhoc_not_crash(self):
        res = self._run({"lane": "banana", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(self.s.calls[0][0], "chat")   # safe fallback, raw chat

    def test_codegen_missing_tests_fails_and_escalates_without_calling_model(self):
        # a codegen job with no tests cannot be gated -> must escalate, and must NOT run ungated code
        res = self._run({"lane": "codegen", "spec": "write f"})
        self.assertFalse(res.ok)
        self.assertTrue(res.escalate)
        self.assertFalse(res.gated)
        self.assertEqual(self.s.calls, [])       # never reached the model

    def test_adhoc_honors_explicit_thinking_opt_in(self):
        self._run({"lane": "adhoc", "messages": [{"role": "user", "content": "hi"}],
                   "enable_thinking": True})
        self.assertEqual(self.s.calls[0], ("chat", True))


if __name__ == "__main__":
    unittest.main()
