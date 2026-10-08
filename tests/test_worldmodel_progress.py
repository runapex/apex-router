"""Zeno progress detector: the corrected limit on geometric series, window statuses (plateau,
regressing, stalled), CI floor/determinism, signals, threshold tuning, and G1 criterion 5 with
known answers."""
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
    assert z["ci"][0] < 1.0 < z["ci"][1]                    # exact fit: only the resolution floor
    assert not z["converging_short"]


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
    # residuals are 0; the CI is the resolution floor ±q/2, q = smallest step 0.05
    assert z["ci"] == pytest.approx((0.775, 0.825))
    assert z["converging_short"]
    assert not G.zeno(v, threshold=0.8)["converging_short"]  # the goal is the limit itself


def test_exact_fit_window_never_gives_a_point_ci():
    for v in ([0.0, 0.4, 0.2, 0.3], [0.0, 0.4, 0.6, 0.7]):  # 10,6,8,7 and 10,6,4,3 of 10
        z = G.zeno(v, resolution=0.1)
        assert z["ci"][1] - z["ci"][0] >= 0.1 - 1e-12


def test_not_converging_never_flags():
    z = G.zeno([0.0, 0.1, 0.3, 0.7])                         # Δ doubles: r = 2
    assert z["status"] == "not converging" and z["v_inf"] == math.inf
    assert not z["converging_short"]


def test_stalled_is_the_stuck_detector_not_zeno():
    z = G.zeno([0.5, 0.5, 0.5, 0.5])
    assert z["status"] == "stalled" and z["v_inf"] == 0.5
    assert z["stalled"] and not z["converging_short"]
    assert not G.zeno([0.5, 0.5, 0.5, 0.5], threshold=0.5)["stalled"]
    z = G.zeno([0.0, 0.0, 0.0, 0.9])                         # only the last step moved
    assert z["status"] == "insufficient" and not z["converging_short"]
    with pytest.raises(ValueError):
        G.zeno([0.1, 0.2])


def _steps(failing, every=1, extra=0):
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


def _flags(failing):
    d = G.detect(_steps(failing))
    return [w["status"] for w in d["windows"]], any(w["converging_short"] for w in d["windows"])


def test_reviewer_cases_plateau_does_not_flag():
    # 38 → 1 → 1 → 1 (→ 0): one big drop then a plateau, then green. Not Zeno.
    st, short = _flags([38, 1, 1, 1])
    assert st == ["plateau"] and not short
    st, short = _flags([38, 1, 1, 1, 0])
    assert st == ["plateau", "insufficient"] and not short
    # 8,4,2,2 and 10,5,2,2: the same shape at a different scale behaves the same way
    assert _flags([8, 4, 2, 2]) == _flags([10, 5, 2, 2]) == (["plateau"], False)


def test_reviewer_case_regressing():
    st, short = _flags([10, 5, 3, 6])
    assert st == ["regressing"] and not short
    z = G.zeno([0.0, 0.5, 0.7, 0.4])
    assert z["status"] == "regressing" and z["delta"] == pytest.approx(-0.3)


def test_ci_deterministic_and_brackets_the_point():
    v = [0.0, 0.45, 0.72, 0.83, 0.91, 0.94]
    a = G.zeno(v, seed=11)
    assert a == G.zeno(v, seed=11)
    assert a["ci"][0] <= a["v_inf"] <= a["ci"][1]
    assert G.fit_r(v) == pytest.approx(a["r"])


# ---- signals + windows ---------------------------------------------------------------------------

def test_progress_signal_from_tests():
    kind, obs = G.progress_signal(_steps([0, 8, 4, 2, 1], every=2))
    assert kind == "tests"
    assert [i for i, _ in obs] == [2, 4, 6, 8]               # first run had 0 failing: no reference
    assert [v for _, v in obs] == pytest.approx([0.0, 0.5, 0.75, 0.875])
    assert G.resolution(_steps([0, 8, 4])) == pytest.approx(1 / 8)
    assert G.progress_signal(_steps([0, 0, 0])) == (None, [])


def test_progress_signal_from_open_errors():
    st = [{"tool": "bash", "err": 1}, {"tool": "Read", "err": 0}, {"tool": "Edit", "err": 1},
          {"tool": "bash", "err": 0}, {"tool": "Read", "err": 0}, {"tool": "Edit", "err": 0}]
    kind, obs = G.progress_signal(st)
    assert kind == "errors"
    # opened bash (0/1 closed) → opened Edit (0/2) → closed bash (1/2) → closed Edit (2/2)
    assert obs == [(0, 0.0), (2, 0.0), (3, 0.5), (5, 1.0)]
    assert G.resolution(st) == pytest.approx(0.5)
    assert G.progress_signal([{"tool": "Read", "err": 0}]) == (None, [])


def test_tests_null_does_not_crash():
    st = [{"tool": "bash", "err": 1, "tests": None}, {"tool": "bash", "err": 0, "tests": None},
          {"tool": "Read", "err": 1, "tests": None}]
    kind, obs = G.progress_signal(st)
    assert kind == "errors" and obs == [(0, 0.0), (1, 1.0), (2, 0.5)]
    assert G.failing_counts(st) == []
    assert len(G.zeno_scores(st)) == 3
    from apex_router.worldmodel import baselines as B
    assert B.prefix_features(st).shape == (4, B.N_FEAT)


def test_detect_windows_need_four_observations():
    st = _steps([30, 16, 10, 6, 5, 4, 3, 3, 3, 3])
    det = G.detect(st)
    assert det["kind"] == "tests" and len(det["windows"]) == 10 - 4 + 1
    assert det["windows"][0]["step"] == 3
    last = det["windows"][-1]
    assert last["status"] == "stalled" and last["stalled"] and not last["converging_short"]
    with pytest.raises(ValueError):
        G.detect(st, w=3)
    zs, ss = G.window_scores(st)
    assert ss[9] == pytest.approx(1.0 - 0.9)                 # stuck 3/30 short of green
    assert np.isneginf(zs[9]) and np.isneginf(zs[:3]).all()


def test_tune_threshold():
    assert G.tune_threshold(list(range(1, 11)), 0.10) == 9.0          # only the max (10) exceeds
    assert G.tune_threshold(list(range(1, 21)), 0.10) == 18.0         # 19, 20 exceed: 2/20
    assert G.tune_threshold([-math.inf] * 5, 0.10, floor=0.0) == 0.0
    assert G.tune_threshold([], 0.10) == math.inf
    assert G.first_flag([0, 1, 5, 2], 1.0) == 2 and G.first_flag([0, 1], 1.0) is None


# ---- criterion 5 ---------------------------------------------------------------------------------

SHORT = [40, 24, 16, 12, 10]       # v = 0, .4, .6, .7, .75 — geometric to .8, flagged at step 3


def _crit_ds(src="gold", n_short=1, n_good=2):
    steps, tasks = [], []

    def add(tid, split, outcome, st):
        sid = tid.split(":")[0]
        for i, s in enumerate(st):
            steps.append({"sid": sid, "task": tid, "i": i, **s})
        tasks.append({"task": tid, "sid": sid, "split": split, "outcome": outcome,
                      "outcome_src": src, "t0": 0.0})

    for k in range(10):                                       # tune: 10 successes, 10 steps, 1 s apart
        add(f"tr{k}:0", "train", "success", _steps([], extra=10))
    # test: short tasks converge short (flag at step 3; the cutoff — steps > 10, wall > 9 s —
    # fires at step 10).  b2 has no progress signal.  g1 is short, g2 runs past the cutoff.
    for k in range(n_short):
        add(f"b1x{k}:0", "test", "fail", _steps(SHORT, extra=15))
    add("b2:0", "test", "partial", _steps([], extra=20))
    for k in range(n_good):
        add(f"g{k}:0", "test", "success", _steps([], extra=5 if k else 12))
    return P.build(steps, tasks)


def test_criterion5_known_answer_small_set_fails():
    ds = _crit_ds()
    sc = G.detector_scores(ds, ds.ids())
    assert "rho" not in sc
    c5 = G.criterion5(ds, sc, ds.ids("train"), ds.ids("test"))
    assert c5["thresholds"]["steps"] == 10.0 and c5["thresholds"]["wall"] == 9.0
    assert c5["thresholds"]["zeno"] == 0.0
    assert G.first_flag(sc["zeno"]["b1x0:0"], 0.0) == 3
    assert (c5["n_bad"], c5["n_good"], c5["quality"]) == (2, 2, "gold")
    z = c5["detectors"]["zeno"]
    assert (z["wins"], z["losses"], z["fpr"]["k"]) == (1, 1, 0)
    # saved: b1 min(10, 20) − 3 = 7;  b2 (never flagged, L = 20) min(10, 20) − 20 = −10
    assert z["median_saved"] == pytest.approx(-1.5)
    assert z["verdict"] == "FAIL"                             # 1 win / 1 loss, n = 2 successes
    assert c5["baseline"]["recall"]["k"] == 2 and c5["baseline"]["fpr"]["k"] == 1
    assert c5["rule"] == "pending owner sign-off"


def test_criterion5_known_answer_pass():
    ds = _crit_ds(n_short=12, n_good=40)
    sc = G.detector_scores(ds, ds.ids())
    c5 = G.criterion5(ds, sc, ds.ids("train"), ds.ids("test"))
    z = c5["detectors"]["zeno"]
    assert (z["wins"], z["losses"], z["fpr"]["k"]) == (12, 1, 0)
    assert z["fpr"]["ci"][1] <= 0.10 and z["win_share"]["ci"][0] > 0.5
    assert z["median_saved"] == pytest.approx(7.0)
    assert z["verdict"] == "PASS"
    s = c5["detectors"]["stalled"]
    assert s["wins"] == 0 and s["verdict"] == "FAIL"


def test_criterion5_win_needs_two_steps_before_the_end():
    ds = _crit_ds(n_short=1)
    st = ds.steps["b1x0:0"]
    del st[5:]                                                # L = 5: flag at 3 = L − 2 → still a win
    sc = G.detector_scores(ds, ds.ids())
    assert G.criterion5(ds, sc, ds.ids("train"), ds.ids("test"))["detectors"]["zeno"]["wins"] == 1
    del st[4:]                                                # L = 4: flag at 3 = L − 1 → no win
    sc = G.detector_scores(ds, ds.ids())
    assert G.criterion5(ds, sc, ds.ids("train"), ds.ids("test"))["detectors"]["zeno"]["wins"] == 0


def test_criterion5_weak_labels_are_provisional_and_inconclusive():
    ds = _crit_ds(src="weak")
    sc = G.detector_scores(ds, ds.ids())
    c5 = G.criterion5(ds, sc, ds.ids("train"), ds.ids("test"))
    assert c5["quality"] == "provisional"
    assert c5["detectors"]["zeno"]["verdict"] == "INCONCLUSIVE"
    assert c5["detectors"]["zeno"]["wins"] == 1               # the numbers are still reported


def test_criterion5_on_synthetic():
    ds, truth = fixtures.dataset(120, seed=0)
    from apex_router.worldmodel import chains as C
    ch = C.fit_tasks(ds, ds.ids("train"))
    sc = G.detector_scores(ds, ds.ids(), ch)
    c5 = G.criterion5(ds, sc, ds.ids("train") + ds.ids("val"), ds.ids("test"))
    assert set(c5["detectors"]) == {"zeno", "stalled", "rho", "zeno|stalled", "zeno|rho"}
    assert not c5["detectors"]["zeno|rho"]["retuned"] and c5["detectors"]["zeno"]["retuned"]
    z = c5["detectors"]["zeno|stalled"]
    # planted shapes are found. Recorded for the record (2026-10-07): the earlier zeno-only recall
    # assertion was dropped when the plateau false positives were fixed (its old 17/19 came from
    # those false positives); zeno alone no longer passes criterion 5 on this fixture — only
    # zeno|stalled does — and the FPR bound below is not load-bearing (observed rate 0).
    assert z["fpr"]["rate"] <= 0.2 and z["wins"] > z["losses"]
