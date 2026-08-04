"""chat_messages raise_on_truncation param.

Default True raises OrnithProtocolError on finish_reason=length (correct for codegen/extraction);
False returns the partial ChatResult (the review pre-filter's partial findings still escalate
usefully). Patches _post + inference_lock so no server/lock is needed.
"""
import contextlib
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import apex_router.ornith.ornith_client as oc  # noqa: E402


class TestTruncationParam(unittest.TestCase):
    def setUp(self):
        self._orig_post = oc._post
        self._orig_lock = oc.inference_lock
        oc._post = lambda *a, **k: {
            "choices": [{"message": {"content": "partial answer"}, "finish_reason": "length"}],
            "usage": {"total_tokens": 5}}
        oc.inference_lock = lambda: contextlib.nullcontext()

    def tearDown(self):
        oc._post = self._orig_post
        oc.inference_lock = self._orig_lock

    def test_default_raises_on_truncation(self):
        with self.assertRaises(oc.OrnithProtocolError):
            oc.chat_messages([{"role": "user", "content": "x"}])

    def test_opt_out_returns_partial(self):
        r = oc.chat_messages([{"role": "user", "content": "x"}], raise_on_truncation=False)
        self.assertEqual(r.answer, "partial answer")
        self.assertEqual(r.finish_reason, "length")


if __name__ == "__main__":
    unittest.main()
