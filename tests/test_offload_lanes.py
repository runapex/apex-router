"""Lane gates for local-model offload.

The load-bearing test is the CODEGEN GATE: local codegen only saves frontier work when the
generated code is CORRECT, and "correct" is decided by running the caller-supplied tests — not by
trusting the model. A gate that runs real tests against real code (correct -> pass, buggy -> fail,
timeout -> fail) is what makes Lane 2 net-positive. These tests use real subprocess execution, no
model, no live server.
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith.offload_lanes import run_python_tests  # noqa: E402


class TestCodegenGate(unittest.TestCase):
    def test_correct_code_passes(self):
        code = "def is_even(n):\n    return n % 2 == 0\n"
        tests = (
            "def test_even():\n"
            "    assert is_even(4) is True\n"
            "    assert is_even(3) is False\n"
        )
        ok, detail = run_python_tests(code, tests)
        self.assertTrue(ok, detail)

    def test_buggy_code_fails(self):
        code = "def is_even(n):\n    return n % 2 == 1\n"  # inverted
        tests = "def test_even():\n    assert is_even(4) is True\n"
        ok, detail = run_python_tests(code, tests)
        self.assertFalse(ok)
        self.assertIn("assert", detail.lower() + " assert")  # some failure signal present

    def test_syntax_error_fails_not_raises(self):
        ok, detail = run_python_tests("def f(:\n  pass\n", "def test_x():\n    assert True\n")
        self.assertFalse(ok)

    def test_infinite_loop_times_out_and_fails(self):
        code = "def f():\n    while True:\n        pass\n"
        tests = "def test_x():\n    f()\n"
        ok, detail = run_python_tests(code, tests, timeout_s=2)
        self.assertFalse(ok)
        self.assertIn("timeout", detail.lower())

    def test_import_in_generated_code_available_to_tests(self):
        code = "import math\n\ndef area(r):\n    return math.pi * r * r\n"
        tests = "def test_area():\n    assert round(area(1), 2) == 3.14\n"
        ok, detail = run_python_tests(code, tests)
        self.assertTrue(ok, detail)

    def test_systemexit_zero_in_code_is_failure_not_pass(self):
        # Codex xval #1: `raise SystemExit(0)` exits the harness 0 -> old code returned True.
        code = "raise SystemExit(0)\n"
        tests = "def test_x():\n    assert True\n"
        ok, detail = run_python_tests(code, tests)
        self.assertFalse(ok, "SystemExit(0) in generated code must NOT count as a pass")

    def test_code_self_test_cannot_mask_empty_caller_suite(self):
        # RISK2: generated code defines its own passing test_*; caller supplied NO tests.
        # Only caller tests count -> empty caller suite is a failure, not a free pass.
        code = "def test_fake():\n    pass\ndef f():\n    return 1\n"
        tests = ""
        ok, detail = run_python_tests(code, tests)
        self.assertFalse(ok, "code's own test_* must not satisfy an empty caller suite")

    def test_code_self_test_cannot_mask_failing_caller_test(self):
        # Generated code injects a passing test_ AND the real function is wrong; caller test fails.
        code = "def test_sneaky():\n    pass\ndef add(a, b):\n    return a - b\n"
        tests = "def test_add():\n    assert add(2, 3) == 5\n"
        ok, detail = run_python_tests(code, tests)
        self.assertFalse(ok, "a real failing caller test must not be masked by code's own test_")

    def test_os_exit_zero_in_code_is_failure_not_pass(self):
        # Codex xval-2 #1: os._exit(0) exits the harness 0 WITHOUT raising -> return-code trust would
        # false-pass. The parent-controlled sentinel must not be written, so this fails.
        code = "import os\nos._exit(0)\n"
        tests = "def test_x():\n    assert True\n"
        ok, detail = run_python_tests(code, tests)
        self.assertFalse(ok, "os._exit(0) must not count as a pass (sentinel not written)")

    def test_same_named_caller_test_overrides_code_test_and_runs(self):
        # Codex xval-2 #2: code defines test_add (passing); caller ALSO defines test_add (failing on
        # a wrong function). The caller's test must run and fail — not be dropped as a dup.
        code = "def test_add():\n    pass\ndef add(a, b):\n    return a - b\n"
        tests = "def test_add():\n    assert add(2, 3) == 5\n"
        ok, detail = run_python_tests(code, tests)
        self.assertFalse(ok, "a same-named caller test must override the code's and actually run")


class TestReviewTruncation(unittest.TestCase):
    def test_review_keeps_partial_findings_on_truncation(self):
        # A truncated review (finish_reason=length) must NOT be discarded — the partial findings
        # still escalate usefully (regression: the old path raised -> empty output -> cold escalate).
        import apex_router.ornith.offload_lanes as offload_lanes
        import apex_router.ornith.ornith_client as oc

        class _Trunc:
            answer = "Bug 1: divide by zero at x.py:2\nBug 2: index error at"  # cut off
            reasoning = None
            finish_reason = "length"
            usage = {"prompt_tokens": 100, "completion_tokens": 512}

        orig = oc.chat_messages
        oc.chat_messages = lambda *a, **k: _Trunc()
        try:
            res = offload_lanes.review_lane("preamble", "some diff")
        finally:
            oc.chat_messages = orig
        self.assertTrue(res.ok, "partial findings should count as ok (recall)")
        self.assertIn("Bug 1", res.output)          # partial content preserved, not dropped
        self.assertTrue(res.escalate)               # still escalates for triage
        self.assertTrue(res.truncated)              # STRUCTURED flag, not just detail string (Codex #1)
        self.assertIn("truncated", res.detail)

    def test_review_unknown_finish_reason_marked_truncated(self):
        # Codex xval finding 2: a None/unknown finish_reason must be treated as NOT-confirmed-complete.
        import apex_router.ornith.offload_lanes as offload_lanes
        import apex_router.ornith.ornith_client as oc

        class _Unknown:
            answer = "some findings"
            reasoning = None
            finish_reason = None       # server omitted it
            usage = {"prompt_tokens": 50, "completion_tokens": 10}

        orig = oc.chat_messages
        oc.chat_messages = lambda *a, **k: _Unknown()
        try:
            res = offload_lanes.review_lane("preamble", "diff")
        finally:
            oc.chat_messages = orig
        self.assertTrue(res.truncated, "unknown finish_reason must not be assumed complete")

    def test_review_clean_stop_not_truncated(self):
        import apex_router.ornith.offload_lanes as offload_lanes
        import apex_router.ornith.ornith_client as oc

        class _Stop:
            answer = "findings"
            reasoning = None
            finish_reason = "stop"
            usage = {"prompt_tokens": 50, "completion_tokens": 10}

        orig = oc.chat_messages
        oc.chat_messages = lambda *a, **k: _Stop()
        try:
            res = offload_lanes.review_lane("preamble", "diff")
        finally:
            oc.chat_messages = orig
        self.assertFalse(res.truncated)
        self.assertNotIn("truncated", res.detail)


if __name__ == "__main__":
    unittest.main()
