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

    def test_review_lane_is_default_off_and_escalates_without_local_tokens(self):
        # Measured net-negative (-5,383 tokens on the live log, 100% escalation): the lane
        # must not spend local tokens unless explicitly re-enabled.
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORNITH_REVIEW_LANE", None)
            res = self._run({"lane": "review", "diff": "def g(): return 1/0"})
        self.assertEqual(res.lane, "review")
        self.assertFalse(res.gated)
        self.assertTrue(res.escalate)            # frontier still does the review
        self.assertFalse(res.ok)
        self.assertIn("disabled", res.detail)
        self.assertEqual(self.s.calls, [])       # no local model call was made

    def test_review_lane_reenabled_by_env_routes_to_review(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"ORNITH_REVIEW_LANE": "on"}):
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

    def test_citation_lane_produces_verifiable_result(self):
        # Stage 2.5 (Codex xval F2): a citation sub-task must reach a real lane (not fall to adhoc),
        # so its grounding VERIFIER can gate it. ok=False AND gated=False (Codex xval P1b): the lane
        # ran no correctness gate, so it has no EARNED verdict — an ungated ok would corrupt telemetry
        # and let the async worker record an unverified citation as a pass. The grounding verifier is
        # the gate, applied by the orchestrator's composed_adjudicate.
        res = self._run({"lane": "citation", "task": "where is X"})
        self.assertEqual(res.lane, "citation")
        self.assertEqual(res.output, "raw answer")
        self.assertFalse(res.ok)                 # ungated -> no earned ok; verifier decides acceptance
        self.assertFalse(res.escalate)
        self.assertFalse(res.gated)              # no in-lane gate; grounding verifier is the gate
        self.assertEqual(self.s.calls[0], ("chat", False))   # thinking-OFF

    def test_search_and_extraction_route_like_citation(self):
        for lane in ("search", "extraction"):
            res = self._run({"lane": lane, "task": "q"})
            self.assertEqual(res.lane, lane)
            self.assertFalse(res.ok)             # ungated: no earned verdict
            self.assertFalse(res.gated)

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

    def test_adhoc_default_chat_tolerates_truncation(self):
        # regression: adhoc jobs were failing ~43% on finish_reason=length because _default_chat
        # let the truncation exception propagate. It must pass raise_on_truncation=False.
        import inspect
        from apex_router.ornith import dispatch
        src = inspect.getsource(dispatch._default_chat)
        self.assertIn("raise_on_truncation=False", src)

    def test_adhoc_honors_explicit_thinking_opt_in(self):
        self._run({"lane": "adhoc", "messages": [{"role": "user", "content": "hi"}],
                   "enable_thinking": True})
        self.assertEqual(self.s.calls[0], ("chat", True))


if __name__ == "__main__":
    unittest.main()
