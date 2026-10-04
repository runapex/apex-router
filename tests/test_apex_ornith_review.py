"""apex-ornith review must not use thinking (measured: thinking-ON hangs 0/3, burns the budget in
<think>) and must never block on git."""
import subprocess
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import apex_router.ornith.apex_ornith as ao  # noqa: E402
import apex_router.ornith.ornith_batch as ob  # noqa: E402


class _R:
    answer = "NO DEFECTS FOUND"


class TestReviewThinkingOff(unittest.TestCase):
    def test_real_review_passes_thinking_false(self):
        seen = {}
        def fake(preamble, items, **kw):
            seen.update(kw); return [_R()]
        orig = ob.batch_over_preamble
        ob.batch_over_preamble = fake
        try:
            out = ao._real_review("diff", budget=777)
        finally:
            ob.batch_over_preamble = orig
        self.assertEqual(out, "NO DEFECTS FOUND")
        self.assertIs(seen["enable_thinking"], False)
        self.assertEqual(seen["max_tokens"], 777)

    def test_budget_default_is_1024(self):
        seen = {}
        rc = ao.run(["review"], liveness_fn=lambda: True,
                    diff_fn=lambda s, b: "x",
                    select_fn=lambda **k: type("R", (), {"fits": True, "reason": ""})(),
                    review_fn=lambda d, *, budget: seen.setdefault("b", budget) and "ok",
                    emit=lambda s: None)
        self.assertEqual(rc, ao.EXIT_OK)
        self.assertEqual(seen["b"], 1024)


class TestGitDiffBounded(unittest.TestCase):
    def test_timeout_returns_empty(self):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        orig = subprocess.run
        subprocess.run = boom
        try:
            self.assertEqual(ao._git_diff(True, None, timeout_s=0.01), "")
        finally:
            subprocess.run = orig

    def test_passes_timeout(self):
        seen = {}
        def fake(cmd, **kw):
            seen.update(kw); return type("P", (), {"stdout": "d"})()
        orig = subprocess.run
        subprocess.run = fake
        try:
            self.assertEqual(ao._git_diff(True, None), "d")
        finally:
            subprocess.run = orig
        self.assertEqual(seen["timeout"], 30.0)


def _run_review(review_fn, emit):
    return ao.run(["review"], liveness_fn=lambda: True, diff_fn=lambda s, b: "x",
                  select_fn=lambda **k: type("R", (), {"fits": True, "reason": ""})(),
                  review_fn=review_fn, emit=emit)


class TestReviewGenFailSafe(unittest.TestCase):
    def test_review_busy_is_unavailable_not_traceback(self):
        import apex_router.ornith.ornith_client as oc
        out = []
        def busy(d, *, budget): raise oc.OrnithBusy("lock busy")
        self.assertEqual(_run_review(busy, out.append), ao.EXIT_UNAVAILABLE)
        self.assertTrue(any("use Opus" in s for s in out))

    def test_review_protocol_error_is_advisory_ok(self):
        import apex_router.ornith.ornith_client as oc
        out = []
        def bad(d, *, budget): raise oc.OrnithProtocolError("empty content")
        self.assertEqual(_run_review(bad, out.append), ao.EXIT_OK)
        self.assertTrue(any("empty content" in s for s in out))

    def test_gen_busy_is_unavailable(self):
        import apex_router.ornith.ornith_client as oc
        out = []
        def busy(spec): raise oc.OrnithBusy("lock busy")
        rc = ao.run(["gen", "spec", "--test", "t.py"], liveness_fn=lambda: True,
                    select_fn=lambda **k: type("R", (), {"fits": True, "reason": ""})(),
                    generate_fn=busy, verify_fn=lambda *a, **k: True, emit=out.append)
        self.assertEqual(rc, ao.EXIT_UNAVAILABLE)

    def test_real_review_tolerates_truncation(self):
        seen = {}
        def fake(preamble, items, **kw):
            seen.update(kw); return [_R()]
        orig = ob.batch_over_preamble
        ob.batch_over_preamble = fake
        try:
            ao._real_review("d", budget=5)
        finally:
            ob.batch_over_preamble = orig
        self.assertIs(seen.get("raise_on_truncation"), False)

    def test_git_diff_timeout_notes_stderr(self):
        import io, contextlib
        def boom(cmd, **kw): raise subprocess.TimeoutExpired(cmd, 1)
        orig = subprocess.run; subprocess.run = boom
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                ao._git_diff(True, None, timeout_s=0.01)
        finally:
            subprocess.run = orig
        self.assertIn("timed out", buf.getvalue())

    def test_gen_protocol_error_is_not_false_success(self):
        import apex_router.ornith.ornith_client as oc
        def bad(spec): raise oc.OrnithProtocolError("truncated")
        rc = ao.run(["gen", "spec", "--test", "t.py"], liveness_fn=lambda: True,
                    select_fn=lambda **k: type("R", (), {"fits": True, "reason": ""})(),
                    generate_fn=bad, verify_fn=lambda *a, **k: True, emit=lambda s: None)
        self.assertEqual(rc, ao.EXIT_VERIFY_FAIL)
