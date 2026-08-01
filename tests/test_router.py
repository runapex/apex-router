import sys, unittest
from pathlib import Path
ML = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ML))
import apex_router.ornith.model_router as r


class TestRouter(unittest.TestCase):
    # ── Backward compatibility (pre-existing contract — must not regress) ──────
    def test_select_returns_ornith_route(self):
        route = r.select(task="narrate")
        self.assertEqual(route.backend, "ornith-http")
        self.assertIsNone(route.model)

    def test_select_ornith_override_ok(self):
        self.assertEqual(r.select(task="x", override="ornith").backend, "ornith-http")

    def test_select_qwen_override_rejected(self):
        with self.assertRaises(ValueError):
            r.select(task="x", override="qwen")

    def test_envelope_constants_present(self):
        self.assertEqual(r.MAX_ITEM_BYTES, 100_000)
        self.assertEqual(r.ORNITH_MAX_ITEMS, 30)

    def test_warn_if_unbounded_backward_compatible(self):
        # ornith_clone_projection.py calls this positionally with a model id.
        r.warn_if_unbounded(r.ORNITH, items=1, item_bytes=1000)  # must not raise

    def test_select_still_returns_route_for_bare_call(self):
        # legacy callers pass no envelope; select must still hand back a usable Route.
        self.assertEqual(r.select().backend, "ornith-http")

    # ── New: capability scoring — Ornith is chosen WHEN IT FITS, else declines ─
    def test_fit_synthesis_within_envelope(self):
        route = r.select(task="synthesis", items=12, item_bytes=40_000)
        self.assertTrue(route.fits)
        self.assertEqual(route.reason, "capability match: synthesis")

    def test_fit_extraction_within_envelope(self):
        route = r.select(task="extract", items=5, item_bytes=8_000)
        self.assertTrue(route.fits)

    def test_decline_too_many_items(self):
        route = r.select(task="synthesis", items=200, item_bytes=1_000)
        self.assertFalse(route.fits)
        self.assertIn("200", route.reason)
        self.assertIn("30", route.reason)  # names the bound

    def test_decline_oversized_item(self):
        route = r.select(task="extract", items=1, item_bytes=250_000)
        self.assertFalse(route.fits)
        self.assertIn("250", route.reason)  # KB in the reason
        self.assertIn("100", route.reason)  # the slice bound

    def test_decline_bulk_reasoning_task(self):
        # Qwen was the bulk/triage tier; retired. Bulk-reasoning does NOT fit dense Ornith —
        # decline with a reason rather than silently accept a mis-route.
        route = r.select(task="bulk_triage", items=8, item_bytes=1_000)
        self.assertFalse(route.fits)
        self.assertIn("bulk", route.reason.lower())

    def test_fit_defaults_true_when_unspecified(self):
        # A bare/legacy call (no task-kind, no envelope) must default to fits=True so
        # existing unconditional callers keep working.
        self.assertTrue(r.select().fits)
        self.assertTrue(r.select(task="narrate").fits)

    def test_reuse_cache_flag_on_multi_item_shared_prefix(self):
        # Multi-item runs share a preamble → the route advertises prompt-cache reuse.
        route = r.select(task="synthesis", items=12, item_bytes=40_000)
        self.assertTrue(route.reuse_cache)

    def test_reuse_cache_off_for_single_item(self):
        # One item → no cross-item prefix to reuse.
        route = r.select(task="extract", items=1, item_bytes=8_000)
        self.assertFalse(route.reuse_cache)

    def test_override_bypasses_fit_gate(self):
        # An explicit ornith override forces the route even outside the envelope
        # (operator knows best), but the verdict is still reported honestly.
        route = r.select(task="synthesis", items=200, item_bytes=1_000, override="ornith")
        self.assertEqual(route.backend, "ornith-http")
        self.assertFalse(route.fits)  # honest: still reports it's out of envelope

    def test_decline_reason_is_actionable(self):
        route = r.select(task="synthesis", items=200, item_bytes=1_000)
        # reason should tell the caller what to DO (split), not just what's wrong.
        self.assertIn("split", route.reason.lower())


if __name__ == "__main__":
    unittest.main()
