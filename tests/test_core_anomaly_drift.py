"""pce-core anomaly + drift against hand-derived reference values."""
import math

from apex_router.core import anomaly, drift


def test_chi2_limit_k2_is_minus_two_ln_alpha():
    assert math.isclose(anomaly.CHI2_99[2], -2 * math.log(0.01), rel_tol=1e-12)
    assert anomaly.t2_limit(2) == anomaly.CHI2_99[2]
    assert anomaly.t2_limit(9) is None


def test_max_abs_scales_and_zscore():
    assert anomaly.max_abs_scales([[1, -4, 0], [2, 2, 0]]) == [2, 4, 1]
    assert anomaly.prescale([1, -4, 0], [2, 4, 1]) == [0.5, -1.0, 0.0]
    assert anomaly.zscore([3.0, 5.0], [1.0, 5.0], [2.0, 0.0]) == [1.0, 0.0]


def test_median_mad_robust_z():
    xs = [1, 2, 3, 4, 100]
    assert anomaly.median(xs) == 3
    assert anomaly.median([1, 2, 3, 4]) == 2.5
    assert anomaly.mad(xs) == 1
    assert anomaly.robust_z(100, 3, 1) > 3.5
    assert math.isclose(anomaly.robust_z(4, 3, 1), 0.6745)
    assert anomaly.robust_z(9, 3, 0) == 0.0


def test_q_and_t2_handmade():
    V = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert math.isclose(anomaly.q_residual([2.0, 1.0, 3.0], V), 9.0)
    assert math.isclose(anomaly.t_squared([2.0, 1.0, 3.0], V, [4.0, 1.0]), 2.0)
    assert math.isclose(anomaly.t_squared([2.0, 1.0, 3.0], V, [4.0, 0.0]), 1.0)


def test_correlation_and_pca_fit():
    corr, sd = anomaly.correlation_of([[4.0, 2.0], [2.0, 9.0]])
    assert sd == [2.0, 3.0]
    assert math.isclose(corr[0][1], 1 / 3) and corr[0][0] == 1.0
    values, vectors = anomaly.pca_fit(corr, 1)
    assert len(values) == 1 and len(vectors) == 1
    assert math.isclose(values[0], 4 / 3)


def test_ecdf_and_explain():
    assert anomaly.ecdf(2.5, [1, 2, 3, 4]) == 0.5
    assert anomaly.ecdf(1.0, []) == 0.0
    card = anomaly.explain(10.0, 1.0, [1, 2, 3, 4, 5, 6, 7, 8, 9, 11], [1.0, 2.0])
    assert card == {"score": 0.9, "term": "Q", "q": 0.9, "t2": 0.5}
    card = anomaly.explain(0.0, 3.0, [1.0], [1.0, 2.0])
    assert card["term"] == "T2" and card["score"] == 1.0


def test_variance_l1():
    assert math.isclose(drift.variance_l1([3.0, 1.0], [2.0, 2.0]), 0.5)
    assert drift.variance_l1([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cusum_lower_trace():
    s, trace = 0.0, []
    for z in [-1.5] * 5:
        s, alarm = drift.cusum_lower(s, z)
        trace.append((s, alarm))
    assert trace == [(1.0, False), (2.0, False), (3.0, False), (4.0, False), (5.0, True)]
    assert drift.cusum_lower(2.0, 0.0) == (1.5, False)
    assert drift.cusum_lower(0.2, 1.0) == (0.0, False)


def test_penalty_ramp():
    assert [drift.penalty_ramp(t) for t in (1, 2, 4, 5, 6, 9)] == [0, 0, 5, 7, 10, 10]


def test_penalty_run_trace():
    trace = drift.penalty_run([1, 9, 1, 1.5, 3, 5, 7, 1, 1, 1, 1])
    assert trace == [
        (1, "COLD", 0), (9, "COLD", 0), (1, "WARM", 0), (1.5, "WARM", 0), (3, "WARM", 0),
        (5, "DRIFTING", 7), (7, "DRIFTING", 10), (1, "DRIFTING", 0), (1, "DRIFTING", 0),
        (1, "WARM", 0), (1, "WARM", 0),
    ]


def test_subspace_angle_is_the_principal_angle():
    assert math.isclose(drift.subspace_angle([1, 0], [1, 1]), math.pi / 4)
