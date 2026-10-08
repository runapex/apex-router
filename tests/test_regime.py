"""Regime model (L1): 2-state HMM over call outcomes, burst variant, day mixture, held-out protocol."""
from __future__ import annotations

import datetime
import itertools
import math
import random

import pytest

from apex_router import markov as mk
from apex_router import regime as rg

DAY = 86400.0


def _regime_stream(n=20000, seed=1, p01=0.0005, p10=0.005, f=(0.002, 0.08)):
    rng = random.Random(seed)
    s, xs, states = 0, [], []
    for _ in range(n):
        if s == 0:
            s = 1 if rng.random() < p01 else 0
        else:
            s = 0 if rng.random() < p10 else 1
        states.append(s)
        xs.append(1 if rng.random() < f[s] else 0)
    return xs, states


def test_plain_hmm_recovers_simulated_regimes():
    xs, states = _regime_stream()
    fit = rg.fit_hmm([xs])
    assert fit["converged"]
    assert fit["fail"][0] == pytest.approx(0.002, abs=0.0015)
    assert fit["fail"][1] == pytest.approx(0.08, rel=0.25)
    assert fit["stationary_degraded"] == pytest.approx(sum(states) / len(states), abs=0.03)
    assert fit["delta_bic"] > 10
    path = rg.viterbi(xs, fit)
    agree = sum(a == b for a, b in zip(path, states)) / len(xs)
    assert agree > 0.95


def test_fit_none_without_failures_and_states_are_ordered():
    assert rg.fit_hmm([[0] * 500]) is None
    assert rg.fit_hmm([]) is None
    xs, _ = _regime_stream(seed=3)
    fit = rg.fit_hmm([xs])
    assert fit["fail"][0] < fit["fail"][1]


def _brute_p_clean(p, n, start):
    """Sum over every hidden path of P(path) * P(all ok | path)."""
    tot = 0.0
    for path in itertools.product((0, 1), repeat=n):
        pr = start[path[0]] * (1 - p["fail"][path[0]])
        for a, b in zip(path, path[1:]):
            pr *= p["A"][a][b] * (1 - p["fail"][b])
        tot += pr
    return tot


def test_p_clean_matches_brute_force():
    p = {"pi": [0.5, 0.5], "A": [[0.9, 0.1], [0.3, 0.7]], "fail": [0.05, 0.4],
         "fail_after_fail": [0.05, 0.4], "burst": False}
    for n in (1, 2, 5):
        assert rg.p_clean(p, n, [0.8, 0.2]) == pytest.approx(_brute_p_clean(p, n, [0.8, 0.2]))
    sd = rg.stationary_degraded(p["A"])
    assert sd == pytest.approx(0.1 / 0.4)
    assert rg.p_clean(p, 4) == pytest.approx(_brute_p_clean(p, 4, [1 - sd, sd]))
    assert rg.p_clean(p, 0) is None
    assert rg.expected_dwell(p["A"]) == pytest.approx((10.0, 1 / 0.3))
    assert rg.predict_next(p, 1.0) == pytest.approx([0.3, 0.7])


def _three_days(seed=5, calls=3000):
    """Day 1 and 3 at a 0.2% base rate, day 2 at 4% — each with the same burst tendency."""
    rng = random.Random(seed)
    # 01:00 local on a fixed date, 10 s apart: each day's calls stay inside that local day
    stream, ts = [], datetime.datetime(2026, 9, 1, 1, 0).timestamp()
    for day, base in enumerate((0.002, 0.04, 0.002)):
        prev = 0
        for i in range(calls):
            r = 0.4 if prev else base
            x = 1 if rng.random() < r else 0
            stream.append((ts + day * DAY + i * 10.0, x))
            prev = x
    return stream


def test_burst_variant_decodes_the_bad_day():
    stream = _three_days()
    fit = rg.fit_hmm([[x for _, x in stream]], burst=True)
    assert fit["burst"] and fit["fail"][1] > 5 * fit["fail"][0]
    assert fit["fail_after_fail"][1] > fit["fail"][1]
    d = rg.decode(stream, fit)
    days = [x["day"] for x in d["days"]]
    assert d["degraded_days"] == [days[1]]
    assert d["fail_degraded"] > d["fail_normal"]
    lo, hi = d["fail_degraded_ci"]
    assert lo <= d["fail_degraded"] <= hi


def test_day_mixture_baseline():
    stream = _three_days()
    m = rg.fit_day_mixture(stream)
    assert m["days"] == 3 and len(m["degraded_days"]) == 1
    assert m["w"] == pytest.approx(1 / 3)
    assert m["fail_degraded"] > m["fail_normal"]
    pc = rg.day_mixture_p_clean(m, 10)
    assert pc == pytest.approx((2 / 3) * (1 - m["fail_normal"]) ** 10
                               + (1 / 3) * (1 - m["fail_degraded"]) ** 10)
    assert rg.fit_day_mixture([]) is None and rg.day_mixture_p_clean(None, 3) is None


def _sessions_from(stream, size=60):
    out = []
    for i in range(0, len(stream), size):
        chunk = stream[i:i + size]
        out.append((chunk[0][0], [x for _, x in chunk]))
    return out


def test_holdout_iid_and_markov_match_markov_holdout():
    stream = _three_days(calls=1500)
    sessions = _sessions_from(stream)
    h = rg.holdout(stream, sessions, n_boot=200)
    ref = mk.holdout([s for _, s in sessions])
    assert (h["n_train"], h["n_test"]) == (ref["n_train"], ref["n_test"])
    assert h["models"]["iid"]["brier"] == pytest.approx(ref["iid_brier"])
    assert h["models"]["markov"]["loglik"] == pytest.approx(ref["markov_loglik"])
    assert h["train_calls"] == sum(1 for ts, _ in stream if ts < h["cut_ts"])
    for name in ("day_mixture", "hmm", "hmm_filtered", "hmm_burst", "hmm_burst_filtered"):
        assert h["models"][name] is not None
        lo, hi = h["brier_diff_vs_iid"][name]["ci"]
        assert lo <= h["brier_diff_vs_iid"][name]["diff"] <= hi
    assert "markov" in h["brier_diff_vs_iid"] and "hmm" in h["brier_diff_vs_markov"]


def test_filtered_start_is_causal():
    # Changing outcomes AFTER the test sessions start must not change the training fit, and changing
    # a later session's calls must not change an earlier session's filtered prediction.
    stream = _three_days(calls=1500)
    sessions = _sessions_from(stream)
    h1 = rg.holdout(stream, sessions, n_boot=50)
    flipped = stream[:-30] + [(ts, 1) for ts, _ in stream[-30:]]
    h2 = rg.holdout(flipped, _sessions_from(flipped), n_boot=50)
    assert h1["hmm_params"]["hmm"]["fail"] == h2["hmm_params"]["hmm"]["fail"]
    post = rg.filtered([x for _, x in stream], h1["hmm_params"]["hmm"])
    post2 = rg.filtered([x for _, x in flipped], h1["hmm_params"]["hmm"])
    assert post[:-30] == post2[:-30]


def test_holdout_empty_sides():
    h = rg.holdout([(1.0, 0)], [(1.0, [0])])
    assert h["n_test"] == 0 and h["models"] == {}


def test_report_verdicts():
    few = rg.regime_report([(float(i), i % 7 == 0) for i in range(100)], [])
    assert few["verdict"] == "insufficient" and few["models"]["hmm"]["fit"] is None
    clean = rg.regime_report([(float(i), 0) for i in range(500)], [])
    assert clean["verdict"] == "insufficient"
    rng = random.Random(2)
    iid = rg.regime_report([(float(i), rng.random() < 0.03) for i in range(6000)], [])
    assert iid["models"]["hmm"]["verdict"] == "no regime structure"
    # a pure burst chain: any second state is a few-call burst, never a regime
    prev, chain = 0, []
    for i in range(20000):
        prev = 1 if rng.random() < (0.4 if prev else 0.01) else 0
        chain.append((float(i), prev))
    rep = rg.regime_report(chain, [])
    assert rep["models"]["hmm"]["verdict"] in ("bursts only", "no regime structure")
    assert rep["models"]["hmm_burst"]["verdict"] in ("bursts only", "no regime structure")
    stream = _three_days()
    rep = rg.regime_report(stream, _sessions_from(stream))
    assert rep["verdict"] == "regimes found"
    assert rep["models"]["hmm_burst"]["dwell"][1] > 30
    assert math.isfinite(rep["models"]["hmm_burst"]["fit"]["delta_bic"])
