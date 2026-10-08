"""Absorbing chain: hand-computed N, P(absorb), variance, ρ(Q); singular guard; window ρ; ranking."""
from __future__ import annotations

import math

import pytest

np = pytest.importorskip("numpy")
from apex_router.worldmodel import chains as C, fixtures, protocol as P  # noqa: E402


def _hand_chain():
    # states a=read, b=edit.  read → edit .5, read → SUCCESS .5;  edit → read .25, edit → FAIL .75
    Pm = np.array([[0.0, 0.5, 0.5, 0.0, 0.0, 0.0],
                   [0.25, 0.0, 0.0, 0.75, 0.0, 0.0]])
    return C.Chain(("read", "edit"), Pm, np.array([1.0, 0.0]))


def test_fundamental_matrix_by_hand():
    an = C.analyse(_hand_chain())
    # I − Q = [[1, −.5], [−.25, 1]], det 7/8 → N = 8/7·[[1, .5], [.25, 1]]
    assert np.allclose(an["N"], [[8 / 7, 4 / 7], [2 / 7, 8 / 7]])
    assert np.allclose(an["t"], [12 / 7, 10 / 7])
    assert np.allclose(an["B"][:, :2], [[4 / 7, 3 / 7], [1 / 7, 6 / 7]])
    assert np.allclose(an["B"].sum(axis=1), 1.0)
    assert np.allclose(an["var"], [44 / 49, 38 / 49])
    assert an["rho"] == pytest.approx(math.sqrt(0.125))
    assert an["expected_steps"] == pytest.approx(12 / 7)
    assert an["p_absorb"]["SUCCESS"] == pytest.approx(4 / 7)
    assert an["var_steps"] == pytest.approx(44 / 49)
    assert C.p_success_from(_hand_chain(), "edit") == pytest.approx(1 / 7)
    assert C.p_success_from(_hand_chain(), "vcs") is None


def test_singular_guard():
    # read ⇄ edit with no exit: ρ(Q) = 1, steps diverge
    Pm = np.array([[0.0, 1.0, 0, 0, 0, 0], [1.0, 0.0, 0, 0, 0, 0]])
    an = C.analyse(C.Chain(("read", "edit"), Pm, np.array([1.0, 0.0])))
    assert an["singular"] and an["expected_steps"] == math.inf and an["B"] is None
    assert an["rho"] == pytest.approx(1.0)


def test_absorbing_state_mapping():
    st = [{"act": "edit"}, {"act": "test"}]
    assert C.absorbing_state({"outcome": "success"}, st) == "SUCCESS"
    assert C.absorbing_state({"outcome": "fail"}, st) == "FAIL"
    assert C.absorbing_state({"outcome": "partial"}, st) == "ABANDON"
    assert C.absorbing_state({"outcome": "partial"}, st + [{"act": "ask"}]) == "ESCALATE"
    assert C.absorbing_state({"outcome": "partial"}, [{"act": "read", "spawn": 1}] + st) == "ESCALATE"
    assert C.absorbing_state({"outcome": "unknown"}, st) is None
    assert C.absorbing_state({}, st) is None


def test_fit_counts_without_smoothing():
    walks = [(["read", "edit"], "SUCCESS"), (["read"], "FAIL"), (["edit", "edit"], "SUCCESS")]
    ch = C.fit(walks, alpha=0.0)
    assert ch.states == ("read", "edit")
    # read: → edit 1, → FAIL 1;  edit: → SUCCESS 2, → edit 1
    assert np.allclose(ch.P[0], [0, 0.5, 0, 0.5, 0, 0])
    assert np.allclose(ch.P[1], [0, 1 / 3, 2 / 3, 0, 0, 0])
    assert np.allclose(ch.pi0, [2 / 3, 1 / 3])
    assert (ch.n_tasks, ch.n_transitions) == (3, 5)
    assert np.allclose(ch.P.sum(axis=1), 1.0)


def test_fit_smoothing_keeps_exit_mass_and_outcome_mix():
    walks = [(["read", "read", "read"], "SUCCESS")] * 3 + [(["read"], "FAIL")]
    ch = C.fit(walks)
    an = C.analyse(ch)
    assert not an["singular"]
    assert ch.R.sum() > 0
    assert an["p_absorb"]["SUCCESS"] > an["p_absorb"]["FAIL"] > an["p_absorb"]["ABANDON"]


def test_unknown_tasks_are_excluded():
    ds, _ = fixtures.dataset(30, seed=1, unknown_share=0.3)
    ids = ds.ids("train")
    n_known = sum(1 for t in ids if ds.tasks[t]["outcome"] != "unknown")
    assert 0 < n_known < len(ids)
    assert C.fit_tasks(ds, ids).n_tasks == n_known


def test_by_type_shrinks_to_pooled_and_table():
    ds, _ = fixtures.dataset(60, seed=2)
    by = C.by_key(ds, ds.ids("train"))
    assert set(by) == {"pooled", "fix", "feature", "chore"}
    assert all(ch.states == by["pooled"].states for ch in by.values())
    rows = C.table(by)
    for r in rows:
        assert 0 < r["rho"] < 1 and r["expected_steps"] > 1
        assert sum(r["p_absorb"].values()) == pytest.approx(1.0)
    # untyped data → pooled only
    for t in ds.tasks.values():
        t.pop("task_type", None)
    assert set(C.by_key(ds, ds.ids("train"))) == {"pooled"}


def test_window_rho_rises_on_a_loop():
    ds, _ = fixtures.dataset(60, seed=3)
    ch = C.fit_tasks(ds, ds.ids("train"))
    base = C.analyse(ch)["rho"]
    loop = [{"act": "search" if i % 2 else "read"} for i in range(30)]
    rho = C.window_rho(ch, loop)
    assert len(rho) == 30
    assert rho[0] == pytest.approx(base)                       # no transition yet
    assert rho[-1] > rho[5] > base
    assert np.all(np.isnan(C.window_rho(None, loop)))


def test_ranking_accuracy_by_hand():
    vals = {("fix", "W0"): {"value": 0.6}, ("fix", "W2"): {"value": 0.8},
            ("feat", "W0"): {"value": 0.7}, ("feat", "W2"): {"value": 0.7},
            ("chore", "W0"): {"value": 0.9}, ("chore", "W2"): {"value": 0.1}}
    real = {("fix", "W0"): (5, 10), ("fix", "W2"): (8, 10),       # concordant → 1
            ("feat", "W0"): (9, 10), ("feat", "W2"): (3, 10),     # value tie → ½
            ("chore", "W0"): (5, 10), ("chore", "W2"): (5, 10)}   # realized tie → skipped
    rk = C.ranking_accuracy(vals, real)
    assert rk["n_pairs"] == 2 and rk["accuracy"] == pytest.approx(0.75)
    assert C.ranking_accuracy(vals, {k: (1, 2) for k in real})["accuracy"] is None


def test_workflow_values_on_synthetic():
    ds, _ = fixtures.dataset(120, seed=0)
    vals = C.workflow_values(ds, ds.ids("train"))
    assert ("fix", "W2") in vals and ("feature", "W0") in vals
    v = vals[("fix", "W0")]
    assert v["value"] == pytest.approx(v["p_success"])            # λ = 0
    lam = C.workflow_values(ds, ds.ids("train"), lam=0.01)[("fix", "W0")]
    assert lam["value"] == pytest.approx(v["p_success"] - 0.01 * v["expected_steps"])
    assert C.workflow_of({}, [{"spawn": 1}]) == "W2" and C.workflow_of({}, [{}]) == "W0"


def test_burst_chain_is_imported_not_copied():
    from apex_router import markov
    assert C.burst is markov
    assert P.ACTIONS[0] == "search"


def test_unknown_actions_are_counted_as_other_not_dropped():
    walks = [(["read", "other", "edit"], "SUCCESS"), (["read", "edit"], "FAIL")]
    pooled = C.fit(walks, alpha=0.0)
    child = C.fit([(["read", "vcs", "mystery", "edit"], "SUCCESS")], prior=pooled)
    # vcs is outside the pooled state set, "mystery" outside §2: both count as `other`
    assert child.n_mapped_other == 2 and child.n_dropped == 0
    assert child.counts[pooled.index("read"), pooled.index("other")] == 1
    assert child.counts[pooled.index("other"), pooled.index("other")] == 1
    no_other = C.fit([(["read", "edit"], "SUCCESS")], alpha=0.0)
    assert C.fit([(["read", "vcs"], "SUCCESS")], prior=no_other).n_dropped == 1


def test_table_reports_observed_length_for_misspecification():
    walks = [(["read"] * n, "SUCCESS") for n in (1, 2, 3, 10)]
    row = C.table({"pooled": C.fit(walks)})[0]
    assert row["observed_mean"] == pytest.approx(4.0)
    assert row["observed_sd"] == pytest.approx(np.std([1, 2, 3, 10], ddof=1))
    assert row["expected_steps"] > 1 and row["sd_steps"] > 0
