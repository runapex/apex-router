"""route_advise — escalation-rate → COST-EFFICIENCY verdict, gated on Wilson-CI significance vs a
cost-derived break-even AND a Benjamini-Hochberg multiplicity correction.

These tests assert the guardrails an independent (Codex) review demanded after refuting a naive
first design: the threshold is the cost break-even (not a magic 0.5), inputs are validated (z, cost
ratio, min_n), the multiplicity correction fires, and every actionable verdict carries the
quality-precondition it cannot itself verify. The verdict is COST-ONLY and observational by
construction — the tests pin that scope, not a claim that a route is 'better'.
"""
from __future__ import annotations

import unittest

from apex_router import route_advise as ra


class TestNullTsProvenanceWarning(unittest.TestCase):
    def test_reason_warns_when_null_ts_rows_present(self):
        rates = {"generate": {"n": 100, "escalated": 95, "null_ts": 7}}
        out = ra.advise(rates=rates)
        self.assertEqual(out["generate"]["verdict"], ra.COST_FAVORS_HEAVY_START)
        self.assertEqual(out["generate"]["null_ts"], 7)
        self.assertIn("7 null-ts rows (provenance unknown)", out["generate"]["reason"])

    def test_no_warning_when_null_ts_zero(self):
        rates = {"generate": {"n": 100, "escalated": 95, "null_ts": 0}}
        out = ra.advise(rates=rates)
        self.assertNotIn("null-ts", out["generate"]["reason"])
        self.assertNotIn("null_ts", out["generate"])

    def test_verdict_logic_unchanged_by_null_ts(self):
        # Same counts with and without null_ts should yield the same verdict.
        base = {"n": 100, "escalated": 95}
        with_null = {"n": 100, "escalated": 95, "null_ts": 12}
        self.assertEqual(
            ra.advise(rates={"t": base})["t"]["verdict"],
            ra.advise(rates={"t": with_null})["t"]["verdict"])


class TestBreakEven(unittest.TestCase):
    def test_break_even_tracks_cost_ratio(self):
        # cost_ratio 5 → break-even 0.80; ratio 2 → 0.50; ratio 10 → 0.90.
        self.assertAlmostEqual(ra._break_even(5.0), 0.80)
        self.assertAlmostEqual(ra._break_even(2.0), 0.50)
        self.assertAlmostEqual(ra._break_even(10.0), 0.90)


class TestAdviseOne(unittest.TestCase):
    def test_majority_escalation_below_break_even_favors_cheap(self):
        # THE fix for Codex finding #1: a 65% escalation rate looks "mostly failing" and the naive
        # 0.5-threshold design flipped it to START_HEAVY. But with heavy costing 5× cheap, break-even
        # is 0.80: cheap-first expected cost = 0.2 + 0.65 = 0.85× heavy — still CHEAPER. The economic
        # verdict is COST_FAVORS_CHEAP_START, the opposite of the naive flip. This is the whole point.
        r = ra.advise_one(100, 65)   # rate 0.65, CI [0.55,0.74], upper 0.74 < break-even 0.80
        self.assertEqual(r["verdict"], ra.COST_FAVORS_CHEAP_START)
        self.assertTrue(r["significant"])

    def test_rate_straddling_break_even_is_inconclusive(self):
        # 78/100: CI [0.69,0.85] straddles the 0.80 cost break-even → honest INCONCLUSIVE, keep default.
        r = ra.advise_one(100, 78)
        self.assertEqual(r["verdict"], ra.INCONCLUSIVE)
        self.assertFalse(r["significant"])

    def test_escalation_significantly_above_break_even_favors_heavy(self):
        # 95/100 → CI lower bound above 0.80 → cheap-first genuinely costs more.
        r = ra.advise_one(100, 95)
        self.assertEqual(r["verdict"], ra.COST_FAVORS_HEAVY_START)
        self.assertTrue(r["significant"])
        self.assertGreater(r["ci_low"], r["break_even"])
        self.assertIn("acceptable", r["assumes"].lower())  # carries the quality precondition

    def test_low_escalation_favors_cheap(self):
        r = ra.advise_one(60, 2)     # rate 0.03, CI upper well below 0.80
        self.assertEqual(r["verdict"], ra.COST_FAVORS_CHEAP_START)
        self.assertTrue(r["significant"])
        self.assertLess(r["ci_high"], r["break_even"])
        self.assertTrue(r["assumes"])

    def test_cost_ratio_moves_the_decision(self):
        # 78/100 escalation: at cost_ratio 2 (break-even 0.50) the CI [0.69,0.85] is fully above 0.50
        # → favors heavy; at cost_ratio 5 (break-even 0.80) the same CI straddles 0.80 → inconclusive.
        # Identical data, opposite verdict — the threshold is economic, not a fixed 0.5.
        heavy = ra.advise_one(100, 78, cost_ratio=2.0)
        incon = ra.advise_one(100, 78, cost_ratio=5.0)
        self.assertEqual(heavy["verdict"], ra.COST_FAVORS_HEAVY_START)
        self.assertEqual(incon["verdict"], ra.INCONCLUSIVE)

    def test_sample_floor_holds_extreme_rate(self):
        r = ra.advise_one(3, 3)      # rate 1.0 but n < min_n
        self.assertEqual(r["verdict"], ra.INCONCLUSIVE)
        self.assertIn("min_n", r["reason"])

    def test_input_validation_z_and_cost_ratio(self):
        # Codex #8: z<=0 gives a zero-width "always significant" CI; cost_ratio<=1 is nonsensical.
        self.assertEqual(ra.advise_one(30, 16, z=0)["verdict"], ra.INCONCLUSIVE)
        self.assertEqual(ra.advise_one(30, 16, z=-1.96)["verdict"], ra.INCONCLUSIVE)
        self.assertEqual(ra.advise_one(100, 95, cost_ratio=1.0)["verdict"], ra.INCONCLUSIVE)
        self.assertEqual(ra.advise_one(100, 95, cost_ratio=0.5)["verdict"], ra.INCONCLUSIVE)

    def test_min_n_floor_cannot_be_defeated_by_negative(self):
        # Codex #8: min_n=-1 must not let 4/4 through; it clamps to 1 but the Wilson CI on 4/4 is
        # wide, so the verdict is driven by significance, not the broken floor.
        r = ra.advise_one(4, 4, min_n=-1)
        # 4/4 Wilson lower bound at z=1.96 is ~0.51 < break-even 0.80 → NOT favor-heavy.
        self.assertEqual(r["verdict"], ra.INCONCLUSIVE)

    def test_invalid_counts_inconclusive_not_raise(self):
        for n, esc in [(0, 0), (5, -1), (5, 9)]:
            self.assertEqual(ra.advise_one(n, esc)["verdict"], ra.INCONCLUSIVE)


class TestAdviseAggregate(unittest.TestCase):
    def test_verdicts_over_injected_rates(self):
        rates = {
            "generate": {"n": 100, "escalated": 95},   # favor heavy
            "explore":  {"n": 60,  "escalated": 2},    # favor cheap
            "refactor": {"n": 5,   "escalated": 5},    # n<min_n → inconclusive
        }
        out = ra.advise(rates=rates)
        self.assertEqual(out["generate"]["verdict"], ra.COST_FAVORS_HEAVY_START)
        self.assertEqual(out["explore"]["verdict"], ra.COST_FAVORS_CHEAP_START)
        self.assertEqual(out["refactor"]["verdict"], ra.INCONCLUSIVE)

    def test_benjamini_hochberg_demotes_marginal_family_member(self):
        # Codex #7: many task-types inflate false-flag risk. Build several marginally-significant
        # cells; BH should demote the weakest to INCONCLUSIVE with a multiplicity reason.
        rates = {f"t{i}": {"n": 40, "escalated": 40} for i in range(6)}   # all extreme → all flagged
        rates["marginal"] = {"n": 30, "escalated": 30}                    # weakest p among the family
        out = ra.advise(rates=rates, alpha=0.001)   # tight alpha forces BH to bite somewhere
        demoted = [tt for tt, r in out.items()
                   if not r["significant"] and "BH" in r["reason"]]
        self.assertTrue(demoted, "expected at least one BH-demoted cell under a tight alpha")

    def test_bh_family_is_all_tested_cells_not_just_significant(self):
        # Codex pass-2 P1: the BH family must be every cell that met min_n (was tested), not only the
        # cells that crossed the CI. One clear signal among 99 tested-but-inconclusive cells must face
        # a family size of 100 for BH ranking, not a family of 1 (which would skip BH entirely).
        rates = {"hot": {"n": 100, "escalated": 89}}                       # crosses CI (p≈.012)
        rates.update({f"mid{i}": {"n": 100, "escalated": 78} for i in range(99)})  # tested, straddle
        out = ra.advise(rates=rates, alpha=0.05)
        # All 100 are 'tested'; the lone significant cell's p≈.012 must beat the rank-1 BH cutoff
        # alpha/m = .0005 to survive — it does not, so it is demoted.
        self.assertEqual(out["hot"]["verdict"], ra.INCONCLUSIVE)
        self.assertIn("BH", out["hot"]["reason"])
        # sanity: every cell is marked tested (family membership independent of significance)
        self.assertTrue(all(r["tested"] for r in out.values()))

    def test_two_sided_pvalue_is_symmetric(self):
        # Codex pass-2 P1b: the p-value feeding BH must be direction-agnostic. Symmetric deviations
        # from break-even (0.80 at cost_ratio 5) get equal p: 90/100 (0.10 above) and 70/100 (0.10
        # below) should have ~equal two-sided p.
        hi = ra.advise_one(100, 90)["p_value"]
        lo = ra.advise_one(100, 70)["p_value"]
        self.assertAlmostEqual(hi, lo, places=6)

    def test_malformed_rows_skipped(self):
        rates = {"ok": {"n": 100, "escalated": 95}, 123: {"n": 100, "escalated": 95},
                 "bad": {"n": "x", "escalated": 1}}
        out = ra.advise(rates=rates)
        self.assertIn("ok", out)
        self.assertNotIn(123, out)
        self.assertNotIn("bad", out)

    def test_advise_fail_safe(self):
        self.assertEqual(ra.advise(rates="not a dict"), {})

    def test_end_to_end_through_log_reader(self):
        import json, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "route_log.jsonl"
            with p.open("w") as f:
                for _ in range(95):
                    f.write(json.dumps({"ts": None, "task_type": "generate", "model": "haiku",
                                        "passed": False, "escalated": True, "note": ""}) + "\n")
                for _ in range(5):
                    f.write(json.dumps({"ts": None, "task_type": "generate", "model": "haiku",
                                        "passed": True, "escalated": False, "note": ""}) + "\n")
            out = ra.advise(log_path=str(p))
        self.assertEqual(out["generate"]["verdict"], ra.COST_FAVORS_HEAVY_START)
        self.assertEqual(out["generate"]["n"], 100)


if __name__ == "__main__":
    unittest.main()
