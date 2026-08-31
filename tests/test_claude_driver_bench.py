"""Claude-CLI transferability bench — offline tests (scripted model_call, no `claude` spawned).

Pins the token-accounting seam (usage summed from the CLI's JSON report) and that the module
reuses the shared codex_driver_bench harness so behavior/parity logic stays single-sourced.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.proxy_engine.tuner.claude_driver_bench import (  # noqa: E402
    claude_call_factory)


class TestClaudeCall(unittest.TestCase):
    def _proc(self, result, usage):
        m = mock.Mock()
        m.returncode = 0
        m.stdout = json.dumps({"result": result, "usage": usage})
        m.stderr = ""
        return m

    def test_tokens_sum_all_input_classes_plus_output(self):
        usage = {"input_tokens": 100, "cache_read_input_tokens": 50,
                 "cache_creation_input_tokens": 1700, "output_tokens": 30}
        with mock.patch("subprocess.run", return_value=self._proc("ANSWER: done", usage)):
            reply, tokens = claude_call_factory("sonnet")("prompt")
        self.assertEqual(reply, "ANSWER: done")
        self.assertEqual(tokens, 100 + 50 + 1700 + 30)

    def test_missing_usage_fields_default_zero(self):
        with mock.patch("subprocess.run",
                        return_value=self._proc("RETRIEVE ccr://a", {"output_tokens": 5})):
            reply, tokens = claude_call_factory()("p")
        self.assertEqual((reply, tokens), ("RETRIEVE ccr://a", 5))

    def test_nonzero_exit_raises_not_scored_as_model_failure(self):
        m = mock.Mock(returncode=1, stdout="", stderr="boom: auth failed")
        with mock.patch("subprocess.run", return_value=m):
            with self.assertRaises(RuntimeError) as cm:
                claude_call_factory()("p")
        self.assertIn("boom", str(cm.exception))

    def test_zero_exit_error_envelope_raises(self):
        # a zero-exit JSON result with is_error:true is a transport/API failure, not model
        # behavior — it must raise, not be returned as a normal reply.
        m = mock.Mock()
        m.returncode = 0
        m.stdout = json.dumps({"is_error": True, "result": "overloaded", "usage": {}})
        m.stderr = ""
        with mock.patch("subprocess.run", return_value=m):
            with self.assertRaises(RuntimeError) as cm:
                claude_call_factory()("p")
        self.assertIn("error envelope", str(cm.exception))

    def test_tools_fail_closed_and_neutral_cwd(self):
        # probe integrity: fail-CLOSED empty allowlist (`--tools ""`) so the model gets ZERO
        # tools (can't read the codes from source, incl. Task/Skill a denylist would miss), and
        # the call must run in a temp dir OUTSIDE this repo.
        import os
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"], captured["cwd"] = cmd, kw.get("cwd")
            return self._proc("ANSWER: x", {"input_tokens": 1})
        with mock.patch("subprocess.run", side_effect=fake_run):
            claude_call_factory()("p")
        # empty allowlist: the flag is present and its value is the empty string
        self.assertIn("--tools", captured["cmd"])
        self.assertEqual(captured["cmd"][captured["cmd"].index("--tools") + 1], "")
        self.assertNotIn("--disallowedTools", captured["cmd"])  # not the weaker denylist
        repo = str(Path(__file__).resolve().parents[1])
        self.assertIsNotNone(captured["cwd"])
        self.assertFalse(os.path.realpath(captured["cwd"]).startswith(repo))  # outside repo

    def test_bare_flag_present_for_fixed_system_prompt(self):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return self._proc("ANSWER: x", {"input_tokens": 1})
        with mock.patch("subprocess.run", side_effect=fake_run):
            claude_call_factory("haiku")("p")
        self.assertIn("--bare", captured["cmd"])
        self.assertIn("haiku", captured["cmd"])


if __name__ == "__main__":
    unittest.main()
