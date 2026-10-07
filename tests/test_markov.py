"""Two-state Markov chain over call outcomes: hand-computed fits, degenerate inputs, held-out split."""
from __future__ import annotations

import math

import pytest

from apex_router import markov

# 1 = failed call. Transitions: s1 0>0 0>1 1>1 1>0; s2 0>0 0>0; s3 (single call) none.
#   ok->ok 3, ok->fail 1, fail->fail 1, fail->ok 1; first calls: ok 2 (s1, s2), fail 1 (s3)
SEQS = [[0, 0, 1, 1, 0], [0, 0, 0], [1]]


def test_fit_chain_counts_and_probabilities_without_prior():
    ch = markov.fit_chain(SEQS, prior=0.0)
    assert ch["counts"] == {"ok_ok": 3, "ok_fail": 1, "fail_ok": 1, "fail_fail": 1,
                            "first_ok": 2, "first_fail": 1}
    assert (ch["sessions"], ch["pairs"]) == (3, 6)
    assert ch["p_ok_ok"] == pytest.approx(3 / 4)
    assert ch["p_fail_fail"] == pytest.approx(1 / 2)
    assert ch["p_first_ok"] == pytest.approx(2 / 3)


def test_fit_chain_additive_prior():
    ch = markov.fit_chain(SEQS, prior=0.5)
    assert ch["p_ok_ok"] == pytest.approx(3.5 / 5)
    assert ch["p_fail_fail"] == pytest.approx(1.5 / 3)
    assert ch["p_first_ok"] == pytest.approx(2.5 / 4)


def test_autocorr_hand_computed():
    # pairs (x, y): (0,0) (0,1) (1,1) (1,0) (0,0) (0,0)
    # mx = 2/6, my = 2/6; sxy = sum (x-1/3)(y-1/3) = 1/9 - 2/9 + 4/9 - 2/9 + 1/9 + 1/9 = 3/9
    # sxx = syy = 2*(2/3)^2 + 4*(1/3)^2 = 12/9 -> r1 = (3/9) / (12/9) = 0.25
    r1, pairs = markov.autocorr(SEQS)
    assert pairs == 6 and r1 == pytest.approx(0.25)
    # perfectly alternating -> -1; perfectly persistent runs -> close to +1
    assert markov.autocorr([[0, 1, 0, 1, 0, 1]])[0] == pytest.approx(-1.0)
    assert markov.autocorr([[0] * 10 + [1] * 10])[0] > 0.8


def test_p_clean_mean_run_stationary():
    ch = markov.fit_chain(SEQS, prior=0.0)
    assert markov.p_clean(ch, 1) == pytest.approx(2 / 3)
    assert markov.p_clean(ch, 3) == pytest.approx(2 / 3 * 0.75 ** 2)
    assert markov.mean_fail_run(0.5) == pytest.approx(2.0)
    # (1-a) / ((1-a)+(1-b)) = 0.25 / 0.75
    assert markov.stationary_fail(0.75, 0.5) == pytest.approx(1 / 3)
    # an independent chain (b = 1-a) has stationary rate = 1-a, mean run 1/a
    assert markov.stationary_fail(0.9, 0.1) == pytest.approx(0.1)


def test_degenerate_inputs_return_none_not_raise():
    empty = markov.fit_chain([])
    assert (empty["sessions"], empty["pairs"]) == (0, 0)
    assert empty["p_ok_ok"] is empty["p_fail_fail"] is empty["p_first_ok"] is None
    assert markov.autocorr([]) == (None, 0)
    assert markov.p_clean(empty, 5) is None
    assert markov.stationary_fail(None, 0.5) is None
    assert markov.mean_fail_run(None) is None and markov.mean_fail_run(1.0) is None
    # no failures at all: P(ok|ok) is fitted, P(fail|fail) has nothing to stand on; r1 undefined
    clean = markov.fit_chain([[0, 0, 0], [0, 0]])
    assert clean["p_fail_fail"] is None and 0 < clean["p_ok_ok"] < 1
    assert markov.autocorr([[0, 0, 0], [0, 0]]) == (None, 3)
    assert markov.stationary_fail(clean["p_ok_ok"], clean["p_fail_fail"]) is None
    # single-call sessions only: no transitions, but P(clean | 1 call) is still the first-call rate
    single = markov.fit_chain([[0], [1], [0], [0]], prior=0.0)
    assert single["pairs"] == 0 and single["p_ok_ok"] is None
    assert markov.p_clean(single, 1) == pytest.approx(0.75)
    assert markov.p_clean(single, 2) is None
    assert markov.autocorr([[0], [1]]) == (None, 0)
    assert markov.clean_loglik([], lambda n: 0.5) is None
    assert markov.clean_brier([], lambda n: 0.5) is None
    h = markov.holdout([])
    assert (h["n_train"], h["n_test"], h["iid_loglik"], h["markov_brier"]) == (0, 0, None, None)


def test_clean_loglik_and_brier():
    seqs = [[0, 0], [0, 1]]  # one clean, one not
    assert markov.clean_loglik(seqs, lambda n: 0.8) == pytest.approx(math.log(0.8) + math.log(0.2))
    assert markov.clean_brier(seqs, lambda n: 0.8) == pytest.approx((0.2 ** 2 + 0.8 ** 2) / 2)
    # a confident miss is clipped: large, finite penalty
    assert math.isfinite(markov.clean_loglik([[1]], lambda n: 1.0))


def test_holdout_is_deterministic_and_splits_by_order():
    bursty = [[0] * 30 + [1] * 3 + [0] * 30 if i % 3 == 0 else [0] * 60 for i in range(20)]
    a = markov.holdout(bursty)
    assert a == markov.holdout(bursty)
    assert (a["n_train"], a["n_test"]) == (14, 6)
    # the split is positional: training on the first 14 is what the test half is scored against
    reordered = markov.holdout(bursty[::-1])
    assert (reordered["n_train"], reordered["n_test"]) == (14, 6)
    # failures arrive in bursts: the chain beats p^n on held-out clean/not
    assert a["markov_loglik"] > a["iid_loglik"]
    assert a["markov_brier"] < a["iid_brier"]
    # only one session: nothing to score
    one = markov.holdout([[0, 1]])
    assert one["n_test"] == 0 and one["markov_loglik"] is None
