"""Every 'thinking-OFF' lane relies on the backend honouring reasoning_effort=none. If an ollama
build or chat template ignores it, every local call is thinking-ON (measured: hangs). The probe
makes that a deterministic check."""
import contextlib
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import apex_router.ornith.ornith_client as oc  # noqa: E402


def _resp(content, reasoning=None, extra=None):
    msg = {"content": content}
    if reasoning is not None:
        msg["reasoning"] = reasoning
    if extra:
        msg.update(extra)
    return {"choices": [{"message": msg, "finish_reason": "stop"}], "usage": {"total_tokens": 3}}


class TestThinkingProbe(unittest.TestCase):
    def setUp(self):
        self._post, self._lock = oc._post, oc.inference_lock
        oc.inference_lock = lambda *a, **k: contextlib.nullcontext()

    def tearDown(self):
        oc._post, oc.inference_lock = self._post, self._lock

    def test_sends_reasoning_effort_none(self):
        seen = {}
        oc._post = lambda p, body, **k: seen.update(body) or _resp("ok")
        ok, why = oc.thinking_off_probe()
        self.assertTrue(ok, why)
        self.assertEqual(seen.get("reasoning_effort"), "none")

    def test_reasoning_field_means_on(self):
        oc._post = lambda *a, **k: _resp("ok", reasoning="let me think")
        ok, why = oc.thinking_off_probe()
        self.assertFalse(ok); self.assertIn("reasoning", why)

    def test_legacy_think_tag_means_on(self):
        oc._post = lambda *a, **k: _resp("<think>hmm</think>ok")
        ok, why = oc.thinking_off_probe()
        self.assertFalse(ok); self.assertIn("reasoning", why)

    def test_reasoning_content_field_means_on(self):
        oc._post = lambda *a, **k: _resp("ok", extra={"reasoning_content": "hmm"})
        ok, why = oc.thinking_off_probe()
        self.assertFalse(ok); self.assertIn("reasoning", why)

    def test_transport_error_is_false_not_raise(self):
        def boom(*a, **k): raise oc.OrnithNotListening("down")
        oc._post = boom
        ok, why = oc.thinking_off_probe()
        self.assertFalse(ok); self.assertIn("OrnithNotListening", why)
