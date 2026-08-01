"""Known-answer + property tests for amr.stats — the §5.0 estimators.

These encode the statistical contract the routing gate depends on (the exact
part the Codex cross-validation found broken in design draft 1):

- wilson_ci        : pass-rate CI that never leaves [0,1] (unlike normal approx)
- benjamini_hochberg: FDR step-up, incl. the middle-rejection property
- bradley_terry    : transitive strengths from pairwise judge wins (no cycles)
- paired_bootstrap_ci: paired delta CI (variance from data, tests via properties)

Written test-first; the module is implemented to satisfy them.
"""
import math
import random

import pytest

from apex_router import stats


# --------------------------------------------------------------------------- #
# wilson_ci(k, n, z) -> (lo, hi)
# --------------------------------------------------------------------------- #
def test_wilson_ci_symmetric_half_center():
    # 5/10 successes: center is exactly 0.5 by symmetry; known closed-form half-width.
    lo, hi = stats.wilson_ci(5, 10, z=1.96)
    assert (lo + hi) / 2 == pytest.approx(0.5, abs=1e-9)
    assert lo == pytest.approx(0.2365, abs=1e-3)
    assert hi == pytest.approx(0.7635, abs=1e-3)


def test_wilson_ci_zero_successes_lower_bound_nonnegative():
    # 0/10: the whole point of Wilson over normal-approx — lower bound stays >= 0.
    lo, hi = stats.wilson_ci(0, 10, z=1.96)
    assert lo >= 0.0
    assert lo == pytest.approx(0.0, abs=1e-6)
    assert 0.0 < hi < 0.35


def test_wilson_ci_all_successes_upper_bound_at_most_one():
    lo, hi = stats.wilson_ci(10, 10, z=1.96)
    assert hi <= 1.0
    assert hi == pytest.approx(1.0, abs=1e-6)
    assert 0.65 < lo < 1.0


def test_wilson_ci_more_data_narrows_interval():
    lo_small, hi_small = stats.wilson_ci(5, 10)
    lo_big, hi_big = stats.wilson_ci(50, 100)
    assert (hi_big - lo_big) < (hi_small - lo_small)


def test_wilson_ci_zero_trials_raises():
    with pytest.raises(ValueError):
        stats.wilson_ci(0, 0)


# --------------------------------------------------------------------------- #
# benjamini_hochberg(pvalues, alpha) -> list[bool]  (True = reject / survives FDR)
# --------------------------------------------------------------------------- #
def test_bh_rejects_only_below_stepup_threshold():
    # sorted p: .001 .008 .039 .041 .9 ; thresholds i/m*alpha: .01 .02 .03 .04 .05
    # largest k with p(k) <= thresh is k=2 -> reject the two smallest only.
    pvals = [0.001, 0.008, 0.039, 0.041, 0.9]
    rejected = stats.benjamini_hochberg(pvals, alpha=0.05)
    assert rejected == [True, True, False, False, False]


def test_bh_stepup_rejects_middle_that_fails_its_own_threshold():
    # The defining BH property: once the LARGEST passing rank is found, everything
    # below it is rejected — even a middle p that individually exceeds its threshold.
    # sorted .001 .04 .045 ; thresholds .0167 .0333 .05 ; largest k = 3 -> reject ALL.
    pvals = [0.001, 0.04, 0.045]
    rejected = stats.benjamini_hochberg(pvals, alpha=0.05)
    assert rejected == [True, True, True]


def test_bh_preserves_input_order():
    # unsorted input; result mask must align to ORIGINAL positions, not sorted ones.
    pvals = [0.9, 0.001, 0.041, 0.008, 0.039]  # same multiset as first test, shuffled
    rejected = stats.benjamini_hochberg(pvals, alpha=0.05)
    assert rejected == [False, True, False, True, False]


def test_bh_none_survive_when_all_large():
    assert stats.benjamini_hochberg([0.6, 0.7, 0.8], alpha=0.05) == [False, False, False]


# --------------------------------------------------------------------------- #
# bradley_terry(pairwise) -> dict[str, float]   pairwise[(winner, loser)] = count
# --------------------------------------------------------------------------- #
def test_bt_symmetric_gives_equal_strengths():
    pairwise = {("a", "b"): 5, ("b", "a"): 5}
    s = stats.bradley_terry(pairwise)
    assert s["a"] == pytest.approx(s["b"], abs=1e-6)


def test_bt_transitive_dominance_orders_models():
    # a beats b beats c, consistently -> strength a > b > c (no cycle).
    pairwise = {
        ("a", "b"): 9, ("b", "a"): 1,
        ("b", "c"): 9, ("c", "b"): 1,
        ("a", "c"): 9, ("c", "a"): 1,
    }
    s = stats.bradley_terry(pairwise)
    assert s["a"] > s["b"] > s["c"]


def test_bt_strengths_are_normalized():
    pairwise = {("a", "b"): 7, ("b", "a"): 3}
    s = stats.bradley_terry(pairwise)
    assert sum(s.values()) == pytest.approx(1.0, abs=1e-6)
    assert all(v > 0 for v in s.values())


def test_bt_dominant_winner_ranks_first_among_three():
    # 'x' wins every matchup -> highest strength of the three.
    pairwise = {
        ("x", "y"): 10, ("y", "x"): 0,
        ("x", "z"): 10, ("z", "x"): 0,
        ("y", "z"): 5, ("z", "y"): 5,
    }
    s = stats.bradley_terry(pairwise)
    assert s["x"] == max(s.values())


# --------------------------------------------------------------------------- #
# paired_bootstrap_ci(deltas, n_boot, alpha, seed) -> (lo, hi)
# --------------------------------------------------------------------------- #
def test_paired_bootstrap_all_positive_excludes_zero():
    deltas = [0.2, 0.3, 0.25, 0.4, 0.35, 0.28, 0.31]
    lo, hi = stats.paired_bootstrap_ci(deltas, n_boot=2000, alpha=0.05, seed=1)
    assert lo > 0.0  # CI credibly above zero -> a real positive effect


def test_paired_bootstrap_all_negative_excludes_zero():
    deltas = [-0.2, -0.3, -0.25, -0.4, -0.35]
    lo, hi = stats.paired_bootstrap_ci(deltas, n_boot=2000, alpha=0.05, seed=1)
    assert hi < 0.0


def test_paired_bootstrap_symmetric_straddles_zero():
    deltas = [-0.3, 0.3, -0.2, 0.2, -0.1, 0.1, 0.0]
    lo, hi = stats.paired_bootstrap_ci(deltas, n_boot=2000, alpha=0.05, seed=1)
    assert lo < 0.0 < hi

def test_paired_bootstrap_seed_is_reproducible():
    deltas = [0.1, -0.2, 0.3, 0.05, -0.1, 0.22]
    a = stats.paired_bootstrap_ci(deltas, n_boot=1000, alpha=0.05, seed=42)
    b = stats.paired_bootstrap_ci(deltas, n_boot=1000, alpha=0.05, seed=42)
    assert a == b


def test_paired_bootstrap_empty_raises():
    with pytest.raises(ValueError):
        stats.paired_bootstrap_ci([], n_boot=100, alpha=0.05, seed=1)


# --------------------------------------------------------------------------- #
# Regression tests — confirmed by Codex empirical cross-validation (2026-07-31)
# --------------------------------------------------------------------------- #
def test_bt_default_budget_equals_fully_converged():
    # BUG (Codex): at the default tolerance the MM loop stopped EARLY (its stop test
    # used max-change on drifting UNNORMALIZED iterates), so the ranking depended on
    # the iteration budget. After normalizing each iterate, the DEFAULT call must
    # return the same strengths as a heavily-converged run. Realistic counts (<=1000).
    d = {("a", "b"): 1, ("b", "a"): 1000, ("a", "c"): 1, ("c", "a"): 100,
         ("a", "d"): 1, ("d", "a"): 1000, ("b", "c"): 100, ("c", "b"): 1000,
         ("b", "d"): 10, ("d", "b"): 10, ("c", "d"): 1000, ("d", "c"): 100}
    # Default tol=1e-9 leaves a residual of a few * tol; assert agreement well
    # inside that (1e-7) — tight enough to catch a real divergence, loose enough
    # not to demand more precision than the default tolerance delivers.
    default = stats.bradley_terry(d)
    converged = stats.bradley_terry(d, max_iter=2_000_000, tol=0.0)
    for m in default:
        assert default[m] == pytest.approx(converged[m], abs=1e-7)


def test_bt_ranking_invariant_to_tolerance_and_budget():
    # The ranking must be a function of the DATA, not the iteration budget/tol.
    d = {("a", "b"): 1, ("b", "a"): 1000, ("a", "c"): 1, ("c", "a"): 100,
         ("a", "d"): 1, ("d", "a"): 1000, ("b", "c"): 100, ("c", "b"): 1000,
         ("b", "d"): 10, ("d", "b"): 10, ("c", "d"): 1000, ("d", "c"): 100}
    def rank(**kw):
        s = stats.bradley_terry(d, **kw)
        return tuple(sorted(s, key=s.get, reverse=True))
    orders = {rank(), rank(max_iter=1000, tol=1e-9), rank(max_iter=5000, tol=1e-14),
              rank(max_iter=200_000, tol=1e-15)}
    assert len(orders) == 1  # all budgets agree


def test_paired_bootstrap_ci_never_inverted():
    # BUG (Codex): asymmetric percentile indices (int(p*n) low vs int(p*n)-1 high)
    # returned lo > hi at extreme alpha with small n_boot (alpha=0.9, n_boot=3 gave
    # (1.0, 0.667)). lo must always be <= hi for any valid alpha and n_boot.
    for a in (0.05, 0.1, 0.5, 0.9):
        for nb in (3, 4, 10, 200):
            lo, hi = stats.paired_bootstrap_ci([0.0, 1.0, 10.0], n_boot=nb, alpha=a, seed=0)
            assert lo <= hi, f"inverted CI at alpha={a}, n_boot={nb}: ({lo}, {hi})"


def test_paired_bootstrap_zero_nboot_raises():
    # BUG (Codex): n_boot=0 crashed with IndexError after index clamping.
    with pytest.raises(ValueError):
        stats.paired_bootstrap_ci([0.1, 0.2, 0.3], n_boot=0, alpha=0.05, seed=0)


def test_bh_boundary_robust_to_float_representation():
    # BUG (Codex): p == threshold decided oppositely for 0.01/3 vs (1/3)*0.01
    # (a 1-ULP difference). Both are mathematically AT the rank-1 threshold for
    # m=3, alpha=0.01, so both must reject rank 1.
    assert stats.benjamini_hochberg([0.01 / 3, 1.0, 1.0], alpha=0.01)[0] is True
    assert stats.benjamini_hochberg([(1 / 3) * 0.01, 1.0, 1.0], alpha=0.01)[0] is True


# --------------------------------------------------------------------------- #
# paired_bootstrap_pvalue(deltas, n_boot, seed, alternative) -> float
# One-sided bootstrap p-value for the mean of paired deltas (feeds the gate's FDR).
# --------------------------------------------------------------------------- #
def test_bootstrap_pvalue_strong_positive_is_small_for_greater():
    p = stats.paired_bootstrap_pvalue([0.3, 0.4, 0.35, 0.28, 0.31, 0.29],
                                      n_boot=2000, seed=1, alternative="greater")
    assert p < 0.05


def test_bootstrap_pvalue_negative_is_large_for_greater():
    # deltas all negative -> H1(mean>0) is not supported -> large p under 'greater'.
    p = stats.paired_bootstrap_pvalue([-0.3, -0.2, -0.25, -0.4],
                                      n_boot=2000, seed=1, alternative="greater")
    assert p > 0.5


def test_bootstrap_pvalue_symmetric_is_near_half():
    p = stats.paired_bootstrap_pvalue([-0.3, 0.3, -0.2, 0.2, -0.1, 0.1],
                                      n_boot=4000, seed=1, alternative="greater")
    assert 0.3 < p < 0.7


def test_bootstrap_pvalue_floored_never_zero():
    # A huge, unambiguous effect must not report exactly 0; floor at 1/n_boot.
    p = stats.paired_bootstrap_pvalue([10.0, 11.0, 9.5, 10.5], n_boot=1000, seed=1,
                                      alternative="greater")
    assert p >= 1.0 / 1000
    assert p > 0.0


def test_bootstrap_pvalue_less_mirrors_greater():
    d = [0.3, 0.25, 0.4, 0.28]
    p_greater = stats.paired_bootstrap_pvalue(d, n_boot=2000, seed=2, alternative="greater")
    p_less = stats.paired_bootstrap_pvalue(d, n_boot=2000, seed=2, alternative="less")
    assert p_greater < 0.05 and p_less > 0.5


def test_bootstrap_pvalue_reproducible():
    d = [0.1, -0.2, 0.3, 0.05, -0.1, 0.22]
    a = stats.paired_bootstrap_pvalue(d, n_boot=1000, seed=7)
    b = stats.paired_bootstrap_pvalue(d, n_boot=1000, seed=7)
    assert a == b


def test_bootstrap_pvalue_empty_raises():
    with pytest.raises(ValueError):
        stats.paired_bootstrap_pvalue([], n_boot=100, seed=1)


def test_bootstrap_pvalue_bad_alternative_raises():
    with pytest.raises(ValueError):
        stats.paired_bootstrap_pvalue([0.1, 0.2], n_boot=100, seed=1, alternative="sideways")


def test_bootstrap_pvalue_imposes_null_not_anticonservative_on_skew():
    # BUG (Codex F4): the p-value must IMPOSE the mean-zero null (center the sample)
    # rather than resample the raw sample and take its tail beyond zero. A skewed
    # sample whose mean is exactly 0 (Codex's example: mostly +1 with a rare large
    # negative) is genuinely NOT evidence of a positive effect, so a calibrated test
    # must return a LARGE p — the null-centered version does; the uncentered one did not.
    skew_mean_zero = [1.0] * 9 + [-9.0]  # sum == 0.0, mean == 0.0
    assert abs(sum(skew_mean_zero)) < 1e-9
    p = stats.paired_bootstrap_pvalue(skew_mean_zero, n_boot=4000, seed=1,
                                      alternative="greater")
    assert p > 0.3, f"anti-conservative on skewed mean-zero sample: p={p}"


def test_bootstrap_pvalue_nan_deltas_raises():
    # BUG (Codex F2 upstream): NaN comparisons are all-false, so a NaN sample silently
    # returned 0.0005 (max significance). NaN input must raise, not fabricate a p.
    with pytest.raises(ValueError):
        stats.paired_bootstrap_pvalue([float("nan")] * 8, n_boot=100, seed=1)


def test_bootstrap_pvalue_overflowing_mean_raises():
    # BUG (Codex pass2 #4): each element is finite (1e308) but the MEAN overflows to
    # inf, centered values become -inf, and the p floored to 0.0005 (false max
    # significance). A non-finite mean after summation must raise, not fabricate a p.
    with pytest.raises(ValueError):
        stats.paired_bootstrap_pvalue([1e308] * 8, n_boot=100, seed=1)


def test_bootstrap_pvalue_real_effect_still_significant():
    # Calibration sanity: a genuine effect with real within-sample spread stays small.
    d = [0.30, 0.10, 0.45, 0.20, 0.35, 0.25, 0.40, 0.15, 0.28, 0.33]
    p = stats.paired_bootstrap_pvalue(d, n_boot=4000, seed=3, alternative="greater")
    assert p < 0.05
