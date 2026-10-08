"""P6 attempt 2 (docs/research/2026-10-08-p6-attempt-2-predeclaration.md): the four pre-declared
ablations — A2-F cross-step JEPA inputs, A2-E error-conditioned chain states, A2-D day regime
(baseline + JEPA), A2-W every-epoch sweep bounds — and the evaluate guard on input flags."""
from __future__ import annotations

import json
import math
import zlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from apex_router.worldmodel import baselines as B
from apex_router.worldmodel import chains as C
from apex_router.worldmodel import evaluate as E
from apex_router.worldmodel import features as F
from apex_router.worldmodel import fixtures
from apex_router.worldmodel import jepa as J
from apex_router.worldmodel import protocol as P
from apex_router.worldmodel import train as T

needs_mlx = pytest.mark.skipif(not J.mlx_available(), reason="mlx not installed ([worldmodel] extra)")
D0 = 86400.0 * 20000              # a UTC midnight


def _s(act="edit", err=0, failed=None, spawn=0, ts=None, sid="s1", task="s1:0", i=0, agent=None):
    tests = ({"ran": 1, "failed": failed, "passed": 10} if failed is not None
             else {"ran": 0, "failed": None, "passed": None})
    return {"sid": sid, "task": task, "agent": agent, "i": i, "ts": ts, "act": act, "err": err,
            "tests": tests, "spawn": spawn, "phase": "other", "in_b": 0, "out_b": 0, "dt": None}


# ---- A2-F ------------------------------------------------------------------------------------------

def test_feature_sets_and_widths():
    assert F.n_feat() == F.N_FEAT == len(F.FEATURE_NAMES)
    assert F.n_feat("a2f") == F.N_FEAT + 6
    assert F.n_feat("a2f", regime=True) == F.N_FEAT + 8
    assert F.n_feat("a1", regime=True) == F.N_FEAT + 2
    assert F.feature_names("a2f", True)[-8:] == F.A2F_NAMES + F.REGIME_NAMES
    with pytest.raises(ValueError):
        F.task_features([_s()], features="a3")
    with pytest.raises(ValueError):
        T.TrainConfig.from_dict({"features": "bogus"})
    # attempt-1 configs (no new keys) load with attempt-1 inputs
    assert T.TrainConfig.from_dict({}).features == "a1" and not T.TrainConfig.from_dict({}).regime


def _seq():
    return [_s("test", err=1, failed=4), _s("edit"), _s("test", failed=2), _s("delegate", spawn=1),
            _s("run", err=1), _s("test", failed=0)]


def test_a2f_hand_values():
    X = F.task_features(_seq(), "a2f")
    np.testing.assert_array_equal(X[:, :F.N_FEAT], F.task_features(_seq()))   # a1 block unchanged
    a = X[:, F.N_FEAT:]
    col = {n: a[:, k] for k, n in enumerate(F.A2F_NAMES)}
    np.testing.assert_allclose(col["a2f.progress"], [0, 0, 0.5, 0.5, 0.5, 1.0])
    np.testing.assert_allclose(col["a2f.progress_defined"], [1, 1, 1, 1, 1, 1])
    np.testing.assert_allclose(col["a2f.err_share"], [1, 1 / 2, 1 / 3, 1 / 4, 2 / 5, 2 / 6], rtol=1e-6)
    np.testing.assert_allclose(col["a2f.tests_ran"], [1, 1, 1, 1, 1, 1])
    np.testing.assert_allclose(col["a2f.spawned"], [0, 0, 0, 1, 1, 1])
    np.testing.assert_allclose(col["a2f.log1p_pos"], np.log1p(np.arange(6)), rtol=1e-6)


def test_a2f_f_ref_is_first_nonzero_and_carried_forward():
    st = [_s("read"), _s("test", failed=0), _s("test", failed=3), _s("edit"), _s("test", failed=6)]
    a = F.a2f_features(st)
    np.testing.assert_allclose(a[:, 0], [0, 0, 0, 0, -1.0])          # 1 − 6/3
    np.testing.assert_allclose(a[:, 1], [0, 0, 1, 1, 1])
    np.testing.assert_allclose(a[:, 3], [0, 1, 1, 1, 1])             # a 0-failing run counts


def test_a2f_matches_the_logistic_hand_features():
    """A2-F gives the JEPA what prefix_features has: JEPA row t == baseline row t + 1."""
    st = _seq()
    a = F.a2f_features(st)
    X = B.prefix_features(st)
    V = P.V
    o_tests = B.N_FEAT - 5                       # ran, failing_last, prog, has_prog, spawned
    for t in range(len(st)):
        r = X[t + 1]
        assert a[t, 0] == pytest.approx(r[o_tests + 2])            # progress
        assert a[t, 1] == pytest.approx(r[o_tests + 3])            # progress defined
        assert a[t, 3] == pytest.approx(r[o_tests + 0])            # tests ran so far
        assert a[t, 2] == pytest.approx(r[V + 1])                  # error share
        assert a[t, 4] == pytest.approx(r[o_tests + 4])            # spawned so far


def test_a2f_is_causal_and_per_sequence():
    st = _seq()
    full = F.task_features(st, "a2f")
    for k in range(1, len(st)):
        np.testing.assert_array_equal(F.task_features(st[:k], "a2f"), full[:k])
    # two streams are featurised independently (state never crosses sequences)
    W = F.build_windows({"a": st, "b": [_s("read")]}, L=8, features="a2f")
    assert W.X.shape[-1] == F.n_feat("a2f")
    b = W.X[W.task_idx == 1][0, 0, F.N_FEAT:]
    np.testing.assert_allclose(b, [0, 0, 0, 0, 0, 0])


def test_a2f_fail_open():
    bad = [{"tests": "x", "err": "y", "spawn": None},
           {"tests": {"ran": 1, "failed": True}, "err": 1},             # bool is not a count
           {"tests": {"ran": 1, "failed": "3"}}]
    X = F.task_features(bad, "a2f", regime=True)
    assert X.shape == (3, F.n_feat("a2f", True)) and np.isfinite(X).all()
    assert X[:, F.N_FEAT + 3].tolist() == [0, 0, 0]                    # no parsed test result


# ---- A2-D ------------------------------------------------------------------------------------------

def _two_streams():
    a = [_s("run", err=1, ts=D0 + 0, task="s1:0"), _s("read", ts=D0 + 100, task="s1:0", i=1),
         _s("run", err=1, ts=D0 + 1000, task="s1:0", i=2)]
    b = [_s("run", err=1, ts=D0 + 50, task="s1:0", agent="x"),
         _s("run", err=1, ts=D0 + 1000, task="s1:0", agent="x", i=1),
         _s("read", ts=D0 + 2000, task="s1:0", agent="x", i=2)]
    return {"s1:0": a, "s1:0@x": b}


def test_regime_hand_values():
    seqs = _two_streams()
    assert F.annotate_regime(seqs) == 6
    a, b = seqs["s1:0"], seqs["s1:0@x"]
    # A@0: itself only
    assert a[0]["_regime"] == [1.0, 1.0]
    # A@100: own [0 e, 100 ok]; sibling B@50 (err) is strictly earlier and in the window
    assert a[1]["_regime"] == [2.0, pytest.approx(2 / 3)]
    # A@1000: window [100, 1000] — own 100 ok + 1000 err; B@50 is outside; B@1000 is NOT read
    # (same instant). Day: own 2 errs / 3 + B@50 → 3 / 4
    assert a[2]["_regime"] == [1.0, pytest.approx(0.75)]
    # B@1000: own 1000 err; sibling A@100 ok in window; A@1000 not read. Day: 50e,1000e + 0e,100
    assert b[1]["_regime"] == [1.0, pytest.approx(0.75)]
    # B@2000: window [1100, 2000] empty of errors. Day: own 2 errs / 3 + siblings 2 / 3 → 4 / 6
    assert b[2]["_regime"] == [0.0, pytest.approx(4 / 6)]


def test_regime_is_causal_appending_never_changes_earlier_values():
    seqs = _two_streams()
    F.annotate_regime(seqs)
    before = {k: [list(s["_regime"]) for s in v] for k, v in seqs.items()}
    more = _two_streams()
    # later in time than every existing step (a step earlier in time than t IS part of t's past,
    # whichever stream it sits in — that is the hand-value test above)
    more["s1:0"] += [_s("run", err=1, ts=D0 + 2500 + j, task="s1:0", i=3 + j) for j in range(4)]
    more["s1:0@x"] += [_s("run", err=1, ts=D0 + 2001, task="s1:0", agent="x", i=3)]
    more["s1:1"] = [_s("test", err=1, ts=D0 + 3000 + j, task="s1:1", i=j) for j in range(3)]
    F.annotate_regime(more)
    for k, vals in before.items():
        assert [s["_regime"] for s in more[k][:len(vals)]] == vals, k
    # … and the appended steps do see the earlier errors
    assert more["s1:1"][0]["_regime"][1] > 0


def test_regime_is_per_session_and_per_day():
    seqs = _two_streams()
    seqs["s2:0"] = [_s("read", ts=D0 + 1001, sid="s2", task="s2:0")]   # another session
    seqs["s1:1"] = [_s("read", ts=D0 + 86400 + 10, task="s1:1")]        # next UTC day
    F.annotate_regime(seqs)
    assert seqs["s2:0"][0]["_regime"] == [0.0, 0.0]
    assert seqs["s1:1"][0]["_regime"] == [0.0, 0.0]                    # day rate resets


def test_regime_missing_ts_and_idempotent():
    seqs = {"t": [_s("run", err=1, ts=None), _s("run", err=1, ts=D0 + 5, i=1)]}
    F.annotate_regime(seqs)
    assert seqs["t"][0]["_regime"] == [0.0, 0.0]
    assert seqs["t"][1]["_regime"] == [1.0, 1.0]                        # the ts-less step not counted
    first = [list(s["_regime"]) for s in seqs["t"]]
    F.annotate_regime(seqs)
    assert [s["_regime"] for s in seqs["t"]] == first


def test_regime_columns_shared_by_jepa_and_logistic():
    seqs = _two_streams()
    F.annotate_regime(seqs)
    st = seqs["s1:0"]
    Xj = F.task_features(st, "a1", regime=True)[:, F.N_FEAT:]
    Xb = B.prefix_features(st, regime=True)
    assert Xb.shape == (len(st) + 1, B.N_FEAT + 2)
    np.testing.assert_array_equal(Xb[0, B.N_FEAT:], [0, 0])
    np.testing.assert_allclose(Xb[1:, B.N_FEAT:], Xj, rtol=1e-6)        # baseline row t+1 = JEPA t
    np.testing.assert_allclose(Xj[:, 0], np.log1p([1, 2, 1]), rtol=1e-6)
    np.testing.assert_array_equal(B.prefix_features(st)[:, :B.N_FEAT], Xb[:, :B.N_FEAT])


def test_regime_baselines_need_annotation_and_learn_with_it():
    raw, _ = fixtures.dataset(40, seed=1)
    ds = E.stream_view(raw)
    with pytest.raises(ValueError, match="annotate_regime"):
        B.Logistic(regime=True).fit(ds, ds.ids("train"), ds.ids("val"))
    E.annotate_regime(ds)
    m = B.Logistic(regime=True).fit(ds, ds.ids("train"), ds.ids("val"))
    assert m.W.shape[0] == B.N_FEAT + 2 and m.name.endswith("+ regime)")
    t = ds.ids("val")[0]
    assert np.isfinite(m.task_logprobs(ds.steps[t], ds.tasks[t])).all()
    names = [b.name for b in B.all_baselines((1, 2), regime=True, err_states=True)]
    assert names[-1] == "logistic (hand features + regime)"


# ---- A2-E ------------------------------------------------------------------------------------------

def test_state_of():
    assert C.state_of({"act": "test", "err": 1}) == "test"
    assert C.state_of({"act": "test", "err": 1}, True) == "test!err"
    assert C.state_of({"act": "test", "err": 0}, True) == "test"
    assert C.state_of({"act": "bogus", "err": 1}, True) == "other!err"


def _walks():
    return [(["test!err", "test"], "SUCCESS"), (["test!err", "test!err", "test"], "SUCCESS"),
            (["test"], "SUCCESS")]


def test_err_chain_hand_computed():
    """α = 0 (maximum likelihood) so the numbers are exact.
    err chain: Q = [[0, 0], [2/3, 1/3]] over (test, test!err), π0 = (1/3, 2/3) → t = (1, 2.5),
    E[steps] = 2, Var = 1. Class chain: P(stay) = 1/2 → E = 2, Var = 2. Observed 1, 2, 3: sd 1."""
    ch = C.fit(_walks(), alpha=0.0)
    assert ch.err_states and ch.states == ("test", "test!err")
    np.testing.assert_allclose(ch.Q, [[0, 0], [2 / 3, 1 / 3]])
    np.testing.assert_allclose(ch.pi0, [1 / 3, 2 / 3])
    an = C.analyse(ch)
    np.testing.assert_allclose(an["t"], [1.0, 2.5])
    assert an["expected_steps"] == pytest.approx(2.0)
    assert an["var_steps"] == pytest.approx(1.0)
    plain = C.fit([([a.replace("!err", "") for a in w], z) for w, z in _walks()], alpha=0.0)
    assert not plain.err_states and plain.states == ("test",)
    ap = C.analyse(plain)
    assert ap["expected_steps"] == pytest.approx(2.0) and ap["var_steps"] == pytest.approx(2.0)
    assert np.std([2, 3, 1], ddof=1) == pytest.approx(1.0)


def _chain_ds():
    steps, tasks = [], []
    for k, (w, _) in enumerate(_walks()):
        tid = f"s{k}:0"
        tasks.append({"task": tid, "sid": f"s{k}", "split": "train", "outcome": "success",
                      "outcome_src": "gold", "t0": float(k)})
        for i, a in enumerate(w):
            steps.append(_s("test", err=int(a.endswith("!err")), ts=float(k) + i, sid=f"s{k}",
                            task=tid, i=i))
    return P.build(steps, tasks)


def test_err_chain_from_steps_and_report():
    ds = _chain_ds()
    ids = ds.ids("train")
    assert C.walks_of(ds, ids, err_states=True)[1][0] == ["test!err", "test!err", "test"]
    assert C.walks_of(ds, ids)[1][0] == ["test", "test", "test"]           # default unchanged
    ch = C.fit_tasks(ds, ids, alpha=0.0, err_states=True)
    assert C.analyse(ch)["expected_steps"] == pytest.approx(2.0)
    rep = C.expected_steps_report(ds, ids)
    assert set(rep) == {"classes", "classes+err"}
    for r in rep.values():
        assert r["observed_mean"] == pytest.approx(2.0) and r["observed_sd"] == pytest.approx(1.0)
        assert r["n_tasks"] == 3
    assert rep["classes+err"]["states"] == 2 and rep["classes"]["states"] == 1
    # shrinkage keeps the setting of the prior; window ρ and P(success) read err states
    sub = C.fit_tasks(ds, ids[:1], prior=ch)
    assert sub.err_states and sub.states == ch.states
    assert np.isfinite(C.window_rho(ch, ds.steps[ids[1]])).all()
    assert C.p_success_from(ch, "test!err") == pytest.approx(1.0)


def test_markov_outcome_uses_err_states():
    raw, _ = fixtures.dataset(40, seed=1)
    ds = E.stream_view(raw)
    m = B.Markov(order=1, err_states=True).fit(ds, ds.ids("train"))
    assert m.chain.err_states and any(s.endswith("!err") for s in m.chain.states)
    t = next(t for t in ds.ids("test") if ds.steps[t][-1].get("err"))
    st = ds.steps[t]
    want = C.p_success_from(m.chain, C.state_of(st[-1], True))
    assert m.outcome_proba(st) == pytest.approx(want)
    assert not B.Markov(order=1).fit(ds, ds.ids("train")).chain.err_states


# ---- A2-W ------------------------------------------------------------------------------------------

def _fake_summaries(monkeypatch, tmp_path):
    """w_reg 1: best CE, best epoch within, epoch 3 outside; w_reg 3: every epoch within;
    w_reg 10: best epoch outside."""
    monkeypatch.setattr(T, "data_home", lambda: tmp_path)
    table = {1.0: (1.80, True, [3]), 3.0: (1.85, True, []), 10.0: (1.70, False, [0, 1]),
             30.0: (1.90, True, [0]), 100.0: (1.95, True, [])}
    trained = []

    def fake_train(cfg, ds, run_dir=None, run_id=None, log=print):
        ce, ok, outside = table[cfg.w_reg]
        trained.append((run_id, cfg.w_reg))
        (tmp_path / "runs" / run_id).mkdir(parents=True, exist_ok=True)
        s = {"best_epoch": 1, "best": {"val_next_ce": ce, "collapse": {
            "effective_rank": 9.0, "sigreg": 0.05, "within_bounds": ok}},
            "collapse_outside_bounds_epochs": outside}
        (tmp_path / "runs" / run_id / "summary.json").write_text(json.dumps(s))
        return s

    monkeypatch.setattr(T, "train", fake_train)
    return trained


def test_sweep_best_epoch_rule_is_attempt_1(monkeypatch, tmp_path):
    _fake_summaries(monkeypatch, tmp_path)
    sw = T.sweep_w_reg(T.TrainConfig(), None, run_id="x", log=lambda m: None)
    assert sw["chosen_w_reg"] == 1.0 and not sw["every_epoch"]       # chosen despite epoch 3


def test_sweep_every_epoch_rejects_any_epoch_outside(monkeypatch, tmp_path):
    _fake_summaries(monkeypatch, tmp_path)
    sw = T.sweep_w_reg(T.TrainConfig(), None, run_id="x", log=lambda m: None, every_epoch=True)
    assert sw["chosen_w_reg"] == 3.0 and sw["every_epoch"] and "EVERY epoch" in sw["rule"]
    rows = {r["w_reg"]: r for r in sw["candidates"]}
    assert rows[1.0]["within_bounds_best_epoch"] and not rows[1.0]["within_bounds"]
    assert rows[1.0]["epochs_outside"] == [3]
    assert not rows[10.0]["within_bounds"] and rows[3.0]["chosen"]
    saved = json.loads((tmp_path / "runs" / "x-wreg3" / "summary.json").read_text())
    assert saved["w_reg_sweep"]["chosen_w_reg"] == 3.0


def test_sweep_every_epoch_none_qualify(monkeypatch, tmp_path):
    _fake_summaries(monkeypatch, tmp_path)
    sw = T.sweep_w_reg(T.TrainConfig(), None, values=(1.0, 10.0), run_id="x", log=lambda m: None,
                       every_epoch=True)
    assert sw["chosen_w_reg"] is None
    assert sw["note"] == "C4 fails by construction: no w_reg within bounds on every epoch (grid 1,10)"
    for w in ("1", "10"):                                   # the table lands in every candidate
        saved = json.loads((tmp_path / "runs" / f"x-wreg{w}" / "summary.json").read_text())
        assert saved["w_reg_sweep"]["grid"] == [1.0, 10.0]


def test_w_reg_grid_parse_and_default():
    assert T.W_REG_GRID == (1.0, 3.0, 10.0)
    assert T.parse_grid("1,3,10,30,100") == (1.0, 3.0, 10.0, 30.0, 100.0)
    assert T.parse_grid(" 3, 3,1 ") == (3.0, 1.0)
    for bad in ("", "1,-3", "0", "x", "nan"):
        with pytest.raises(ValueError):
            T.parse_grid(bad)


def test_sweep_grid_is_recorded(monkeypatch, tmp_path):
    trained = _fake_summaries(monkeypatch, tmp_path)
    sw = T.sweep_w_reg(T.TrainConfig(), None, values=T.parse_grid("1,3,10,30,100"), run_id="x",
                       log=lambda m: None, every_epoch=True)
    assert [w for _, w in trained] == [1.0, 3.0, 10.0, 30.0, 100.0]
    assert sw["grid"] == [1.0, 3.0, 10.0, 30.0, 100.0] and sw["chosen_w_reg"] == 3.0
    assert sw["note"] is None
    saved = json.loads((tmp_path / "runs" / "x-wreg3" / "summary.json").read_text())
    assert saved["w_reg_sweep"]["grid"] == sw["grid"]
    assert not (json.loads((tmp_path / "runs" / "x-wreg100" / "summary.json").read_text())
                .get("w_reg_sweep"))                              # winner only when one exists
    default = T.sweep_w_reg(T.TrainConfig(), None, run_id="y", log=lambda m: None)
    assert default["grid"] == [1.0, 3.0, 10.0]


def test_train_runs_falls_back_to_default_and_records(monkeypatch, tmp_path):
    trained = _fake_summaries(monkeypatch, tmp_path)
    raw, _ = fixtures.dataset(10, seed=1)
    out = E.train_runs(E.stream_view(raw), "synthetic-fixtures:10", seeds=2, sweep=True, cpu=False,
                       sweep_every_epoch=True, w_reg_grid=(1.0, 10.0), log=lambda m: None)
    assert out["sweep"]["chosen_w_reg"] is None
    seeds = [(r, w) for r, w in trained if not "-sweep-" in r]
    assert len(seeds) == 2 and all(w == T.TrainConfig().w_reg for _, w in seeds)
    for r in out["runs"]:
        s = json.loads((tmp_path / "runs" / r / "summary.json").read_text())
        assert s["w_reg_sweep"]["note"].startswith("C4 fails by construction")


def test_card_prints_c4_by_construction(tmp_path, monkeypatch):
    rd = _run_dir(tmp_path, monkeypatch, {})
    wm = _FakeWM()
    wm.run_dir = rd
    monkeypatch.setattr(T.WorldModel, "load", staticmethod(lambda r: wm))
    note = "C4 fails by construction: no w_reg within bounds on every epoch (grid 1,3,10,30,100)"
    sw = {"grid": [1.0, 3.0, 10.0, 30.0, 100.0], "every_epoch": True, "note": note,
          "chosen_w_reg": None, "rule": T.RULE_EVERY_EPOCH, "candidates": []}
    card = E.scorecard(E.stream_view(fixtures.dataset(40, seed=1)[0]), "synthetic: 40",
                       ["fake-run"], training={"sweep_every_epoch": True, "sweep": sw})
    assert card["w_reg_grid"] == sw["grid"] and card["c4_by_construction"] == note
    txt = E.render(card)
    assert note in txt and "grid [1.0, 3.0, 10.0, 30.0, 100.0]" in txt


# ---- evaluate: input guard + card ----------------------------------------------------------------

def test_check_inputs():
    assert E.run_inputs({}) == ("a1", False)                          # attempt-1 runs
    assert E.run_inputs({"config": {"features": "a2f", "regime": True}}) == ("a2f", True)
    assert E.check_inputs({"features": "a2f", "regime": True}, "a2f", True) == ("a2f", True)
    with pytest.raises(E.FeatureMismatch, match="--features a2f --regime"):
        E.check_inputs({"features": "a2f", "regime": True}, "a1", False)
    with pytest.raises(E.FeatureMismatch):
        E.check_inputs({}, "a2f", False)
    with pytest.raises(E.ViewMismatch):                               # caught like the view guard
        E.check_inputs({"regime": True}, "a1", False)


class _FakeWM:
    def __init__(self, features="a1", regime=False):
        self.run_dir = Path("/nonexistent/fake-run")
        self.cfg = SimpleNamespace(macro=False, embed=False, d=8, d_control=4, erank_min=2.0,
                                   sigreg_max=10.0, seed=0, device="cpu", w_reg=1.0,
                                   features=features, regime=regime)
        self.summary = {"best_epoch": 0, "best": {"val_next_ce": 2.0}, "features": features,
                        "regime": regime}

    def score(self, tasks):
        out = {}
        for tid, st in tasks.items():
            n = len(st)
            rng = np.random.default_rng(zlib.crc32(tid.encode()))
            z = rng.normal(size=(n, 8))
            out[tid] = {"z": z, "z_control": z[:, -4:], "steps": st, "offline_only": False,
                        "next_action_probs": np.full((n, P.V), 1.0 / P.V),
                        "p_success": np.full(n, 0.5)}
        return out


def _run_dir(tmp_path, monkeypatch, summary):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    rd = tmp_path / "worldmodel" / "runs" / "fake-run"
    rd.mkdir(parents=True)
    (rd / "summary.json").write_text(json.dumps(summary))
    (rd / "metrics.jsonl").write_text(json.dumps(
        {"epoch": 0, "collapse": {"effective_rank": 5.0, "sigreg": 0.05, "within_bounds": True}}) + "\n")
    return rd


def test_feature_mismatch_refused_before_any_test_pass(tmp_path, monkeypatch):
    _run_dir(tmp_path, monkeypatch, {"features": "a2f", "regime": False})
    loaded = []
    monkeypatch.setattr(T.WorldModel, "load", staticmethod(lambda r: loaded.append(r)))
    raw, _ = fixtures.dataset(40, seed=1)
    with pytest.raises(E.FeatureMismatch):
        E.scorecard(E.stream_view(raw), "synthetic: 40", ["fake-run"])          # asks for a1
    assert loaded == [] and not E.ledger_path().exists()


def test_card_records_ablations_and_both_chains(tmp_path, monkeypatch):
    rd = _run_dir(tmp_path, monkeypatch, {"features": "a2f", "regime": True})
    wm = _FakeWM("a2f", True)
    wm.run_dir = rd
    monkeypatch.setattr(T.WorldModel, "load", staticmethod(lambda r: wm))
    raw, _ = fixtures.dataset(40, seed=1)
    ds = E.stream_view(raw)
    card = E.scorecard(ds, "synthetic: 40", ["fake-run"], features="a2f", regime=True,
                       err_states=True, training={"sweep_every_epoch": True})
    assert card["ablations"] == {"A2-F": True, "A2-E": True, "A2-D": True, "A2-W": True,
                                 "features": "a2f"}
    assert all(F.has_regime(s) for st in ds.steps.values() for s in st)
    assert card["baselines"]["best_next_action"] in card["baselines"]["val_ce"]
    assert "logistic (hand features + regime)" in card["baselines"]["val_ce"]
    cs = card["chain_steps"]
    assert set(cs) == {"classes", "classes+err"} and cs["classes+err"]["states"] > cs["classes"]["states"]
    assert card["per_run"][0]["features"] == "a2f" and card["per_run"][0]["regime"]
    txt = E.render(card)
    assert "attempt-2 ablations: A2-F, A2-E, A2-D, A2-W" in txt
    assert "classes+err" in txt and "checked every epoch (A2-W)" in txt
    # default card: attempt-1 configuration, no chain report
    wm1 = _FakeWM()
    wm1.run_dir = rd
    (rd / "summary.json").write_text(json.dumps({}))
    monkeypatch.setattr(T.WorldModel, "load", staticmethod(lambda r: wm1))
    c1 = E.scorecard(E.stream_view(fixtures.dataset(40, seed=1)[0]), "synthetic: 40", ["fake-run"])
    assert c1["chain_steps"] is None and not any(c1["ablations"][k] for k in ("A2-F", "A2-E", "A2-D"))
    assert "none (attempt-1 configuration)" in E.render(c1)


def test_cli_sweep_every_epoch_needs_sweep(capsys):
    assert E.cmd_evaluate(["--run", "x", "--sweep-every-epoch"]) == 2
    assert E.cmd_evaluate(["--train", "--w-reg-grid", "1,30"]) == 2          # needs --sweep
    assert E.cmd_evaluate(["--train", "--sweep", "--w-reg-grid", "1,-3"]) == 2


# ---- MLX: a2f + regime train, record, refuse unannotated input ------------------------------------

_SMOKE = dict(epochs=1, batch=8, warmup=10, lr=1e-3, hidden=32, layers=1, heads=2, pred_hidden=32,
              sigreg_dirs=16, patience=5, device="cpu")


@needs_mlx
def test_train_a2f_regime_records_and_guards(tmp_path):
    ds = T.synthetic_dataset(120, seed=0)
    cfg = T.TrainConfig(**_SMOKE, features="a2f", regime=True)
    s = T.train(cfg, ds, run_dir=tmp_path / "r", run_id="r", log=lambda m: None)
    assert s["features"] == "a2f" and s["regime"] is True
    assert s["ablations"] == {"A2-F": True, "A2-D": True}
    assert s["n_features"] == F.N_FEAT + 8
    wm = T.WorldModel.load(tmp_path / "r")
    tid = next(iter(ds.tasks))
    out = wm.predict(ds.tasks[tid])                      # annotated by train(): accepted
    assert len(out["p_success"]) == len(ds.tasks[tid])
    bare = [{k: v for k, v in r.items() if k != "_regime"} for r in ds.tasks[tid]]
    with pytest.raises(ValueError, match="day regime"):
        wm.predict(bare)
    assert math.isfinite(s["best"]["val_next_ce"])
