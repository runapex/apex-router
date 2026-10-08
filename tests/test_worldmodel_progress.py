"""Zeno progress detector: the corrected limit on geometric series, CI/window behaviour, signals,
threshold tuning and G1 criterion 5 on a hand-built set with a known answer."""
from __future__ import annotations

import math

import pytest

np = pytest.importorskip("numpy")
from apex_router.worldmodel import fixtures, progress as G, protocol as P  # noqa: E402


# ---- the limit -----------------------------------------------------------------------------------

def test_geometric_series_converging_to_the_goal():
    z = G.zeno([0.5, 0.75, 0.875])
    assert z["r"] == pytest.approx(0.5)
    assert z["v_inf"] == pytest.approx(1.0)                 # 0.75 + 0.125/(1 − 0.5)
    assert z["status"] == "converging"
    assert z["ci"][1] == pytest.approx(1.0)
    assert not z["converging_short"]                        # upper bound not < 1


def test_corrected_formula_not_the_old_one():
    # v_∞ = v_t + Δ_t/(1 − r); the dropped-increment form v_t + Δ_t·r/(1 − r) would say 0.875
    assert G.limit(0.75, 0.125, 0.5) == pytest.approx(1.0)
    assert G.tail_limit(0.875, 0.125, 0.5) == pytest.approx(1.0)
    assert G.limit(0.75, 0.125, 1.0) == math.inf


def test_geometric_series_converging_short():
    v = [0.8 * (1 - 0.5 ** k) for k in range(5)]           # 0, .4, .6, .7, .75 → limit .8
    z = G.zeno(v)
    assert z["r"] == pytest.approx(0.5)
    assert z["v_inf"] == pytest.approx(0.8)
    assert z["ci"] == pytest.approx((0.8, 0.8))
    assert z["converging_short"]
    assert not G.zeno(v, threshold=0.8)["converging_short"]  # the goal is the limit itself


def test_not_converging_never_flags():
    z = G.zeno([0.0, 0.1, 0.3, 0.7])                         # Δ doubles: r = 2
    assert z["status"] == "not converging" and z["v_inf"] == math.inf
    assert not z["converging_short"]


def test_stalled_and_insufficient():
    z = G.zeno([0.5, 0.5, 0.5, 0.5])
    assert z["status"] == "stalled" and z["v_inf"] == 0.5 and z["converging_short"]
    assert not G.zeno([0.5, 0.5, 0.5, 0.5], threshold=0.5)["converging_short"]
    z = G.zeno([0.5, 0.5, 0.5, 0.9])                         # only the last step moved
    assert z["status"] == "insufficient" and not z["converging_short"]
    with pytest.raises(ValueError):
        G.zeno([0.1, 0.2])


def test_fitted_last_delta_ignores_a_single_plateau():
    # raw last Δ = 0 would give v∞ = v_t and a zero-width CI below 1; the fit does not
    z = G.zeno([0.0, 0.6, 0.84, 0.84])
    assert z["delta"] > 0 and z["v_inf"] > 0.84
    assert z["ci"][1] >= 0.9


def test_ci_deterministic_and_brackets_the_point():
    v = [0.0, 0.45, 0.72, 0.83, 0.91, 0.94]
    a = G.zeno(v, seed=11)
    assert a == G.zeno(v, seed=11)
    assert a["ci"][0] <= a["v_inf"] <= a["ci"][1]
    assert G.fit_r(v) == pytest.approx(a["r"])


# ---- signals + windows ---------------------------------------------------------------------------

def _steps(failing, every=1, extra=0, err=None):
    st, k = [], 0
    for i in range(len(failing) * every + extra):
        if i % every == 0 and k < len(failing):
            f = failing[k]
            k += 1
            st.append({"act": "test", "tool": "bash", "err": int(f > 0), "ts": float(i),
                       "tests": {"ran": 1, "failed": f, "passed": 10}})
        else:
            st.append({"act": "read", "tool": "Read", "err": 0, "ts": float(i),
                       "tests": {"ran": 0}})
    return st


def test_progress_signal_from_tests():
    kind, obs = G.progress_signal(_steps([0, 8, 4, 2, 1], every=2))
    assert kind == "tests"
    assert [i for i, _ in obs] == [2, 4, 6, 8]               # first run had 0 failing: no reference
    assert [v for _, v in obs] == pytest.approx([0.0, 0.5, 0.75, 0.875])
    assert G.progress_signal(_steps([0, 0, 0])) == (None, [])


def test_progress_signal_from_open_errors():
    st = [{"tool": "bash", "err": 1}, {"tool": "Read", "err": 0}, {"tool": "Edit", "err": 1},
          {"tool": "bash", "err": 0}, {"tool": "Read", "err": 0}, {"tool": "Edit", "err": 0}]
    kind, obs = G.progress_signal(st)
    assert kind == "errors"
    # opened bash (0/1 closed) → opened Edit (0/2) → closed bash (1/2) → closed Edit (2/2)
    assert obs == [(0, 0.0), (2, 0.0), (3, 0.5), (5, 1.0)]
    assert G.progress_signal([{"tool": "Read", "err": 0}]) == (None, [])


def test_detect_windows_need_four_observations():
    st = _steps([30, 16, 10, 6, 5, 4, 3, 3, 3, 3])
    det = G.detect(st)
    assert det["kind"] == "tests" and len(det["windows"]) == 10 - 4 + 1
    assert det["windows"][0]["step"] == 3
    assert det["windows"][-1]["status"] == "stalled" and det["windows"][-1]["converging_short"]
    with pytest.raises(ValueError):
        G.detect(st, w=3)
    sc = G.zeno_scores(st)
    assert sc[9] == pytest.approx(1.0 - 0.9)                 # threshold − CI upper (3/30 left)
    assert np.isneginf(sc[:3]).all()


def test_tune_threshold():
    assert G.tune_threshold(list(range(1, 11)), 0.10) == 9.0          # only the max (10) exceeds
    assert G.tune_threshold(list(range(1, 21)), 0.10) == 18.0         # 19, 20 exceed: 2/20
    assert G.tune_threshold([-math.inf] * 5, 0.10, floor=0.0) == 0.0
    assert G.tune_threshold([], 0.10) == math.inf
    assert G.first_flag([0, 1, 5, 2], 1.0) == 2 and G.first_flag([0, 1], 1.0) is None


# ---- criterion 5 ---------------------------------------------------------------------------------

def _crit_ds(src="gold"):
    steps, tasks = [], []

    def add(tid, split, outcome, st):
        sid = tid.split(":")[0]
        for i, s in enumerate(st):
            steps.append({"sid": sid, "task": tid, "i": i, **s})
        tasks.append({"task": tid, "sid": sid, "split": split, "outcome": outcome,
                      "outcome_src": src, "t0": 0.0})

    for k in range(10):                                       # tune: 10 successes, 10 steps, 1 s apart
        add(f"tr{k}:0", "train", "success", _steps([], extra=10))
    # test: bad1 converges short (10 → 5 → 5 → 5 …) and is flagged at step 3; the cutoff (steps >
    # 10, wall > 9 s) fires at step 10.  bad2 has no progress signal.  good2 runs past the cutoff.
    add("b1:0", "test", "fail", _steps([10, 5, 5, 5, 5], extra=15))
    add("b2:0", "test", "partial", _steps([], extra=20))
    add("g1:0", "test", "success", _steps([], extra=5))
    add("g2:0", "test", "success", _steps([], extra=12))
    return P.build(steps, tasks)


def test_criterion5_known_answer():
    ds = _crit_ds()
    ids = ds.ids()
    sc = G.detector_scores(ds, ids)
    assert "rho" not in sc
    c5 = G.criterion5(ds, sc, ds.ids("train"), ds.ids("test"))
    assert c5["thresholds"]["steps"] == 10.0 and c5["thresholds"]["wall"] == 9.0
    assert c5["thresholds"]["zeno"] == 0.0
    assert G.first_flag(sc["zeno"]["b1:0"], 0.0) == 3          # at the 4th test run
    assert (c5["n_bad"], c5["n_good"], c5["quality"]) == (2, 2, "gold")
    z = c5["detectors"]["zeno"]
    assert (z["earlier"]["k"], z["recall"]["k"], z["fpr"]["k"]) == (1, 1, 0)
    assert z["verdict"] == "PASS"                             # 1/2 ≥ 0.5 at FPR 0
    assert c5["baseline"]["recall"]["k"] == 2 and c5["baseline"]["fpr"]["k"] == 1
    strict = G.criterion5(ds, sc, ds.ids("train"), ds.ids("test"), min_share=0.75)
    assert strict["detectors"]["zeno"]["verdict"] == "FAIL"


def test_criterion5_weak_labels_are_provisional_and_inconclusive():
    ds = _crit_ds(src="weak")
    sc = G.detector_scores(ds, ds.ids())
    c5 = G.criterion5(ds, sc, ds.ids("train"), ds.ids("test"))
    assert c5["quality"] == "provisional"
    assert c5["detectors"]["zeno"]["verdict"] == "INCONCLUSIVE"
    assert c5["detectors"]["zeno"]["earlier"]["k"] == 1       # the number is still reported


def test_criterion5_on_synthetic_beats_the_cutoff():
    ds, truth = fixtures.dataset(120, seed=0)
    from apex_router.worldmodel import chains as C
    ch = C.fit_tasks(ds, ds.ids("train"))
    sc = G.detector_scores(ds, ds.ids(), ch)
    c5 = G.criterion5(ds, sc, ds.ids("train") + ds.ids("val"), ds.ids("test"))
    z = c5["detectors"]["zeno"]
    assert z["fpr"]["rate"] <= 0.15
    assert z["earlier"]["rate"] >= 0.5                        # the generator's short/stuck tasks
    assert set(c5["detectors"]) == {"zeno", "rho", "zeno|rho"}
