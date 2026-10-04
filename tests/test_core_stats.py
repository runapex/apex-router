"""pce-core stats: moved functions are re-exported unchanged; Welford/EWMA/nearest-rank match
their reference formulas (two-pass moments, s = a·x + (1-a)·s, s[ceil(q·n)-1])."""
import importlib.util
import math
from pathlib import Path

from apex_router import stats as shim
from apex_router.core import stats as core

ROOT = Path(__file__).resolve().parents[1]
XS = [10, 11, 9, 10, 50, 52, 49, 51, 10, 10]


def test_shim_reexports_the_moved_functions():
    assert shim.wilson_ci is core.wilson_ci
    assert shim.benjamini_hochberg is core.benjamini_hochberg
    assert shim.paired_bootstrap_ci is core.paired_bootstrap_ci
    assert shim.paired_bootstrap_pvalue is core.paired_bootstrap_pvalue


def test_bradley_terry_removed():
    assert not hasattr(shim, "bradley_terry")
    assert not hasattr(core, "bradley_terry")


def test_welford_matches_two_pass():
    w = core.welford_new()
    for x in XS:
        w = core.welford_push(w, x)
    mean = sum(XS) / len(XS)
    var = sum((x - mean) ** 2 for x in XS) / (len(XS) - 1)
    assert w["n"] == 10
    assert math.isclose(w["mean"], mean, rel_tol=1e-12)
    assert math.isclose(core.welford_variance(w), var, rel_tol=1e-12)


def test_welford_variance_needs_two_samples():
    assert core.welford_variance(core.welford_push(core.welford_new(), 5.0)) == 0.0


def test_welford_covariance_matches_two_pass():
    s = core.welford_cov_new(2)
    for row in ([1, 2], [2, 4], [3, 7]):
        s = core.welford_cov_push(s, row)
    cov = core.covariance(s)
    assert math.isclose(cov[0][0], 1.0)
    assert math.isclose(cov[1][1], 19 / 3)
    assert math.isclose(cov[0][1], 2.5) and math.isclose(cov[1][0], 2.5)


def test_ewma_reference_series_alpha_0_2():
    s, out = None, []
    for x in XS:
        s = core.ewma(s, x, 0.2)
        out.append(round(s, 1))
    assert out == [10.0, 10.2, 10.0, 10.0, 18.0, 24.8, 29.6, 33.9, 29.1, 25.3]


def test_half_life():
    assert round(core.half_life(0.2), 1) == 3.1
    assert round(core.half_life(0.5), 1) == 1.0


def test_ewma_q16_tracks_the_float_ewma():
    q = f = None
    for x in XS:
        q = core.ewma_q16(q, x)
        f = core.ewma(f, x, 0.2)
        assert abs(q / core.Q16 - f) < 1e-3


def test_to_q16_rounds_half_up_not_to_even():
    assert core.to_q16(0.5 / core.Q16) == 1
    assert core.to_q16(2.5 / core.Q16) == 3
    assert core.to_q16(-0.5 / core.Q16) == 0


def test_nearest_rank_textbook_example():
    v = [15, 20, 35, 40, 50]
    assert [core.nearest_rank(v, q) for q in (0.05, 0.3, 0.4, 0.5, 1.0)] == [15, 20, 20, 35, 50]
    assert core.nearest_rank([], 0.5) is None


def test_wilson_all_pass_lower_bound_is_n_over_n_plus_z_squared():
    # The READY boundary in §5: with target 0.9, n/(n+z²) first clears 0.9 at n = 35.
    for n in (30, 34, 35):
        lo, _ = core.wilson_ci(n, n)
        assert math.isclose(lo, n / (n + 1.96 ** 2), rel_tol=1e-12)
    assert core.wilson_ci(34, 34)[0] < 0.9 <= core.wilson_ci(35, 35)[0]


def test_state_bench_wilson_delegates_to_core():
    from apex_router.ornith import state_bench
    assert state_bench.wilson_ci(0, 0) == (0.0, 1.0)
    assert state_bench.wilson_ci(7, 10) == core.wilson_ci(7, 10)


def test_handoff_script_percentile_matches_core():
    spec = importlib.util.spec_from_file_location("handoff_threshold", ROOT / "scripts" / "handoff_threshold.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    v = [3, 1, 4, 1, 5, 9, 2, 6]
    s = sorted(v)
    for pct in (5, 30, 40, 50, 80, 100):
        assert script.nearest_rank_percentile(s, pct) == core.nearest_rank(s, pct / 100.0)
