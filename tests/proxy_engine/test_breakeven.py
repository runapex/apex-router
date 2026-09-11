"""Break-even / minimum-value gate primitive tests (Delta 1 from the LLM Systems Digest).

The primitive prices the counterfactual an optimization must clear before it is worth doing:
a saving must beat the cost of the cache bust it causes, by an uncertainty margin. UN-WIRED by
design — these tests pin the math, not any live routing. The live gate that consumes this needs a
Codex cross-validation pass first (K3 handoff guardrail #6).
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

from apex_router.proxy_engine.tuner.breakeven import BreakEven, Decision
from apex_router.proxy_engine.tuner.cachesim import Pricing


class TestBustCost(unittest.TestCase):
    def test_bust_cost_is_write_minus_read_premium(self):
        be = BreakEven()  # default pricing: p_write=1.25, p_read=0.10
        # 1000 reusable tokens busted → 1000 × (1.25 − 0.10) = 1150
        self.assertAlmostEqual(be.bust_cost_tokens(1000), 1150.0)

    def test_zero_or_negative_reusable_costs_nothing(self):
        be = BreakEven()
        self.assertEqual(be.bust_cost_tokens(0), 0.0)
        self.assertEqual(be.bust_cost_tokens(-5), 0.0)


class TestAdmit(unittest.TestCase):
    def test_saving_without_bust_admits_any_positive(self):
        be = BreakEven()
        d = be.admit(predicted_saved_tokens=10, reusable_tokens=50_000, busts_cache=False)
        # no bust → no cost → any positive saving clears
        self.assertTrue(d.admit)
        self.assertEqual(d.bust_cost, 0.0)
        self.assertEqual(d.reason, "admit")

    def test_no_saving_never_admits(self):
        be = BreakEven()
        d = be.admit(predicted_saved_tokens=0, reusable_tokens=0, busts_cache=False)
        self.assertFalse(d.admit)
        self.assertEqual(d.reason, "no_saving")

    def test_bust_rejected_when_saving_below_margin(self):
        be = BreakEven(uncertainty_margin=0.15)
        # bust of 1000 reusable → cost 1150; threshold = 1150 × 1.15 = 1322.5
        # a saving of 1200 base-tokens (value 1200) does NOT clear the margin
        d = be.admit(predicted_saved_tokens=1200, reusable_tokens=1000, busts_cache=True)
        self.assertFalse(d.admit)
        self.assertEqual(d.reason, "below_margin")
        self.assertAlmostEqual(d.bust_cost, 1150.0)

    def test_bust_admitted_when_saving_beats_margin(self):
        be = BreakEven(uncertainty_margin=0.15)
        # same 1150 cost, threshold 1322.5; a saving of 1400 clears it
        d = be.admit(predicted_saved_tokens=1400, reusable_tokens=1000, busts_cache=True)
        self.assertTrue(d.admit)
        self.assertEqual(d.reason, "admit")
        self.assertAlmostEqual(d.net, 1400.0 - 1150.0)

    def test_fail_closed_at_exact_threshold(self):
        # strict '>' → a saving exactly equal to the threshold is NOT admitted (fail-closed).
        be = BreakEven(uncertainty_margin=0.0)  # threshold == cost
        d = be.admit(predicted_saved_tokens=1150, reusable_tokens=1000, busts_cache=True)
        self.assertFalse(d.admit)  # saving 1150 == cost 1150 → not strictly greater
        self.assertEqual(d.reason, "below_margin")

    def test_margin_is_calibratable(self):
        # a stricter margin rejects what a looser one admits — the field is a per-node dial.
        strict = BreakEven(uncertainty_margin=0.50)
        loose = BreakEven(uncertainty_margin=0.05)
        kw = {"predicted_saved_tokens": 1400, "reusable_tokens": 1000, "busts_cache": True}
        self.assertFalse(strict.admit(**kw).admit)  # threshold 1725 > 1400
        self.assertTrue(loose.admit(**kw).admit)    # threshold 1207.5 < 1400

    def test_custom_pricing_flows_through(self):
        # a node with a different read discount re-prices the bust cost.
        be = BreakEven(pricing=Pricing(p_write=2.0, p_read=0.0))
        self.assertAlmostEqual(be.bust_cost_tokens(100), 200.0)

    def test_negative_margin_is_clamped_fail_closed(self):
        # a mis-entered negative margin must NOT fail-open (zero the threshold and admit everything).
        be = BreakEven(uncertainty_margin=-1.0)
        self.assertEqual(be.uncertainty_margin, 0.0)  # clamped at construction
        # with margin clamped to 0, threshold == cost; a saving <= cost is still rejected.
        d = be.admit(predicted_saved_tokens=1150, reusable_tokens=1000, busts_cache=True)
        self.assertFalse(d.admit)
        self.assertEqual(d.reason, "below_margin")

    def test_inverted_pricing_cannot_make_cost_negative(self):
        # p_write < p_read (a bad calibration) must floor bust cost at 0, never a negative cost that
        # would make the threshold negative and admit every optimization (fail-open).
        be = BreakEven(pricing=Pricing(p_write=0.10, p_read=1.25))
        self.assertEqual(be.bust_cost_tokens(1000), 0.0)
        # cost 0 → a bust with any positive saving admits (nothing to lose), but never NEGATIVE cost.
        d = be.admit(predicted_saved_tokens=1, reusable_tokens=1000, busts_cache=True)
        self.assertTrue(d.admit)
        self.assertGreaterEqual(d.bust_cost, 0.0)

    def test_decision_to_dict_roundtrip(self):
        be = BreakEven()
        d = be.admit(predicted_saved_tokens=5000, reusable_tokens=0, busts_cache=False)
        payload = d.to_dict()
        self.assertEqual(payload["reason"], "admit")
        self.assertIn("net", payload)
        self.assertIsInstance(d, Decision)


if __name__ == "__main__":
    unittest.main()
