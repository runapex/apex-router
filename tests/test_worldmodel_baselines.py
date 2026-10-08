"""P6 baselines: hand-computed Dirichlet/back-off/hierarchy, BIC order recovery, logistic regression."""
from __future__ import annotations

import math

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
from apex_router.worldmodel import baselines as B, fixtures, protocol as P  # noqa: E402

I = P.A_INDEX


def _ds(seqs, types=None, split="train"):
    steps, tasks = [], []
    for k, acts in enumerate(seqs):
        tid = f"s{k}:0"
        for i, a in enumerate(acts):
            steps.append({"sid": f"s{k}", "task": tid, "i": i, "ts": 100.0 * k + i, "act": a,
                          "err": 0, "tests": {"ran": 0}})
        row = {"task": tid, "sid": f"s{k}", "split": split, "outcome": "success",
               "outcome_src": "gold", "t0": 100.0 * k}
        if types and types[k]:
            row["task_type"] = types[k]
        tasks.append(row)
    return P.build(steps, tasks)


SEQS = [["read", "edit", "read"], ["read", "read"]]


def test_unigram_additive_half():
    ds = _ds(SEQS)
    m = B.Unigram().fit(ds, ds.ids("train"))
    assert m.p[I["read"]] == pytest.approx(4.5 / (5 + 0.5 * P.V))
    assert m.p[I["edit"]] == pytest.approx(1.5 / (5 + 0.5 * P.V))
    assert m.p.sum() == pytest.approx(1.0)
    assert m.outcome_proba([]) == pytest.approx(2.5 / 3)          # (2 successes + ½)/(2 + 1)


def test_markov_order1_plain_dirichlet_by_hand():
    ds = _ds(SEQS)
    m = B.Markov(order=1, backoff=False, by_type=False).fit(ds, ds.ids("train"))
    st = ds.steps["s0:0"]
    row = m.predict_proba(st[:1])                                   # after "read"
    assert row[I["edit"]] == pytest.approx(1.5 / 8.5)
    assert row[I["read"]] == pytest.approx(1.5 / 8.5)
    assert row[I["search"]] == pytest.approx(0.5 / 8.5)
    start = m.predict_proba([])
    assert start[I["read"]] == pytest.approx(2.5 / 8.5)
    lp = m.task_logprobs(st)
    assert lp[1] == pytest.approx(math.log(1.5 / 8.5))


def test_markov_backoff_centres_on_lower_order():
    ds = _ds(SEQS)
    m = B.Markov(order=1, by_type=False).fit(ds, ds.ids("train"))
    p0_read = 4.5 / 11.5
    row = m.predict_proba(ds.steps["s0:0"][:1])
    assert row[I["read"]] == pytest.approx((1 + 6.5 * p0_read) / 8.5)
    assert row.sum() == pytest.approx(1.0)
    m2 = B.Markov(order=2, by_type=False).fit(ds, ds.ids("train"))
    hist = [{"act": "edit"}, {"act": "edit"}]                       # context never seen
    assert np.allclose(m2.predict_proba(hist), m.predict_proba(hist[-1:]))


def test_markov_hierarchical_shrinkage_and_untyped_task():
    ds = _ds(SEQS, types=["fix", "chore"])
    m = B.Markov(order=1, backoff=False, beta=4.0).fit(ds, ds.ids("train"))
    pool = B.Markov(order=1, backoff=False, by_type=False).fit(ds, ds.ids("train"))
    hist = ds.steps["s0:0"][:1]
    p_pool = pool.predict_proba(hist)
    fix = m.predict_proba(hist, {"task_type": "fix"})
    assert fix[I["edit"]] == pytest.approx((1 + 4 * p_pool[I["edit"]]) / 5)
    assert np.allclose(m.predict_proba(hist, {}), p_pool)                       # no type
    assert np.allclose(m.predict_proba(hist, {"task_type": "unseen"}), p_pool)  # unseen type


def test_markov_beta_picked_on_val():
    ds, _ = fixtures.dataset(60, seed=4)
    m = B.Markov(order=1).fit(ds, ds.ids("train"), ds.ids("val"))
    assert m.beta in B.Markov.BETAS


def test_bic_recovers_the_generating_order():
    d2, _ = fixtures.dataset(150, seed=0, order=2, n_actions=5)
    d1, _ = fixtures.dataset(150, seed=0, order=1, n_actions=5)
    b2 = B.bic_order(d2, d2.ids("train"))
    b1 = B.bic_order(d1, d1.ids("train"))
    assert b2["best"] == 2 and b1["best"] == 1
    assert b2["delta_bic"] > 0 and b1["delta_bic"] > 0
    assert [o["order"] for o in b1["orders"]] == [1, 2, 3]


def test_bic_by_hand():
    ds = _ds([["read", "edit"]])
    b = B.bic_order(ds, ds.ids("train"), orders=(1,))
    # START→read, read→edit: both deterministic → loglik 0; 2 contexts × (2 − 1) params
    o = b["orders"][0]
    assert o["loglik"] == pytest.approx(0.0) and o["params"] == 2
    assert o["bic"] == pytest.approx(2 * math.log(2))


def test_markov_beats_prior_held_out_on_synthetic():
    ds, _ = fixtures.dataset(80, seed=5)
    tr, va = ds.ids("train"), ds.ids("val")
    u = B.Unigram().fit(ds, tr)
    m = B.Markov(order=1).fit(ds, tr, va)
    cu, _ = P.next_action_scores(u, "u", ds, "test")
    cm, _ = P.next_action_scores(m, "m", ds, "test")
    assert cm.value < cu.value
    p = m.outcome_proba(ds.steps[tr[0]][:3])
    assert 0.0 <= p <= 1.0


def test_prefix_features_shape_and_values():
    st = [{"act": "read", "err": 0, "tests": {"ran": 0}, "phase": "explore", "out_b": 1,
           "in_b": 0, "dt": None},
          {"act": "test", "err": 1, "tests": {"ran": 1, "failed": 4, "passed": 10},
           "phase": "verify", "out_b": 2, "in_b": 0, "dt": 12.0},
          {"act": "test", "err": 1, "tests": {"ran": 1, "failed": 1, "passed": 13},
           "phase": "verify", "out_b": 2, "in_b": 0, "dt": 3.0, "spawn": 1}]
    X = B.prefix_features(st)
    assert X.shape == (4, B.N_FEAT)
    assert np.all(X[0, :P.V] == 0) and X[0, P.V] == 0.0
    assert X[3, I["test"]] == pytest.approx(2 / 3)
    assert X[3, P.V + 1] == pytest.approx(2 / 3)                    # error share
    tail = X[3, -5:]
    assert list(tail) == pytest.approx([1, 1, 0.75, 1, 1])          # ran, failing, 1 − 1/4, defined, spawned


def test_logistic_deterministic_normalised_and_consistent():
    ds, _ = fixtures.dataset(40, seed=6)
    tr, va = ds.ids("train"), ds.ids("val")
    a = B.Logistic().fit(ds, tr, va)
    b = B.Logistic().fit(ds, tr, va)
    assert np.array_equal(a.W, b.W) and a.lam == b.lam
    st = ds.steps[tr[0]]
    p = a.predict_proba(st[:3])
    assert p.sum() == pytest.approx(1.0) and np.all(p > 0)
    lp = a.task_logprobs(st)
    assert lp[3] == pytest.approx(math.log(a.predict_proba(st[:3])[P.act_id(st[3])]))
    u = B.Unigram().fit(ds, tr)
    assert P.next_action_scores(a, "lr", ds, "test")[0].value < \
        P.next_action_scores(u, "u", ds, "test")[0].value
    po = a.outcome_proba(st[:4])
    assert 0.0 < po < 1.0


def test_logistic_outcome_falls_back_to_base_rate_without_both_classes():
    ds = _ds(SEQS * 6)                                              # all success
    m = B.Logistic(lam=1e-2).fit(ds, ds.ids("train"))
    assert m.ow is None and m.outcome_proba([]) == pytest.approx(m.base)
