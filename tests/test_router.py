import sys, unittest
from pathlib import Path
from unittest import mock
ML = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ML))
import apex_router.ornith.model_router as r
from apex_router.ornith import local_tier


class TestRouter(unittest.TestCase):
    # ── Backward compatibility (pre-existing contract — must not regress) ──────
    def test_select_returns_ornith_route(self):
        route = r.select(task="narrate")
        self.assertEqual(route.backend, "ornith-http")
        # CONTRACT CHANGE (MLX -> ollama): this used to assert `model is None`, because the MLX
        # server had one start-time model and clients omitted the field. ollama REQUIRES an
        # explicit model id — omitting it is a 400 — so the Route now always names one.
        self.assertIsInstance(route.model, str)
        self.assertTrue(route.model)

    def test_select_ornith_override_ok(self):
        self.assertEqual(r.select(task="x", override="ornith").backend, "ornith-http")

    def test_select_qwen_override_rejected(self):
        # Isolate from the machine-local families overlay: r.select() builds its known-family set
        # via local_tier.load_families(), which reads ~/.apex-router/models.json by default. Patch
        # it to the committed-only set so the override is genuinely not a known family.
        with mock.patch.object(local_tier, "load_families",
                               return_value={"ornith": dict(local_tier.FAMILIES["ornith"])}):
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
        # The reason now also names the tier the verdict picked.
        self.assertEqual(route.reason, "capability match: synthesis → large tier")
        self.assertEqual(route.tier, "large")

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

    def test_bulk_reasoning_task_routes_to_the_small_tier(self):
        # CONTRACT CHANGE: this used to be a hard DECLINE. Qwen had been the bulk/triage tier and
        # its retirement left only the big model up, so bulk work was a mis-route we named rather
        # than accepted. Tiers restore that lane — bulk is now the SMALL tier's job, so the honest
        # verdict is "fits, on small", not "decline".
        route = r.select(task="bulk_triage", items=8, item_bytes=1_000)
        self.assertTrue(route.fits)
        self.assertEqual(route.tier, "small")
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
