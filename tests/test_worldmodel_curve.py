"""P6 `worldmodel curve`: a TRAIN/VAL learning curve (train-session fraction × seed) for the
JEPA-vs-logistic verdict. The test split is stripped before anything runs and never scored;
the ledger is never touched. Synthetic data only."""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from apex_router.worldmodel import evaluate as E
from apex_router.worldmodel import fixtures
from apex_router.worldmodel import jepa as J
from apex_router.worldmodel import protocol as P
from apex_router.worldmodel import train as T

needs_mlx = pytest.mark.skipif(not J.mlx_available(), reason="mlx not installed ([worldmodel] extra)")


def _ds(n=30, seed=1):
    raw, _ = fixtures.dataset(n, seed=seed)
    return E.stream_view(raw)


def _train_sids(ds):
    return {ds.sid(t) for t in ds.ids("train")}


# ---- subsample_train -------------------------------------------------------------------------------

def test_subsample_train_nested_for_the_same_seed():
    ds = _ds()
    n = len(_train_sids(ds))
    prev = set()
    for f in (0.1, 0.25, 0.5, 0.75, 1.0):
        sub = P.subsample_train(ds, f, seed=3)
        s = _train_sids(sub)
        assert len(s) == max(1, math.ceil(f * n))
        assert prev <= s                                   # nested
        prev = s
    assert prev == _train_sids(ds)                         # f = 1 keeps every train session
    # a different seed draws a different subset (same size)
    a, b = (_train_sids(P.subsample_train(ds, 0.25, seed=k)) for k in (0, 1))
    assert len(a) == len(b) and a != b


def test_subsample_train_session_granularity_and_steps_dropped():
    ds = _ds()
    sub = P.subsample_train(ds, 0.5, seed=0)
    keep = _train_sids(sub)
    for t in ds.ids("train"):                              # whole sessions: all streams or none
        assert (t in sub.tasks) == (ds.sid(t) in keep)
    assert set(sub.steps) == set(sub.tasks)
    assert sum(len(sub.steps[t]) for t in sub.ids("train")) < sum(len(ds.steps[t]) for t in ds.ids("train"))


def test_subsample_train_min_one_session_and_val_untouched():
    ds = _ds()
    sub = P.subsample_train(ds, 1e-9, seed=0)
    assert len(_train_sids(sub)) == 1
    for split in ("val", "test"):
        assert sub.ids(split) == ds.ids(split)
        for t in ds.ids(split):
            assert sub.tasks[t] == ds.tasks[t] and sub.steps[t] == ds.steps[t]
    assert ds.ids("train") and len(ds.ids("train")) > len(sub.ids("train"))   # input not mutated


@pytest.mark.parametrize("f", [0, -0.5, 1.5, float("nan")])
def test_subsample_train_refuses_bad_fraction(f):
    with pytest.raises(ValueError):
        P.subsample_train(_ds(), f, seed=0)


# ---- strip test ------------------------------------------------------------------------------------

def test_strip_test_drops_every_test_sequence():
    ds = _ds()
    out = E.strip_test(ds)
    assert not out.ids("test") and ds.ids("test")
    assert out.ids("train") == ds.ids("train") and out.ids("val") == ds.ids("val")
    assert set(out.steps) == set(out.tasks)


# ---- slope -----------------------------------------------------------------------------------------

def _cell(steps, gap):
    return {"train_steps": steps, "jepa_minus_logistic": {"mean": gap, "ci": [None, None]}}


def test_curve_slope_least_squares_on_log_steps():
    cells = [_cell(100, 0.30), _cell(100, 0.32), _cell(400, 0.20), _cell(1600, 0.10)]
    x = np.log([100, 100, 400, 1600])
    y = np.array([0.30, 0.32, 0.20, 0.10])
    want = float(np.sum((x - x.mean()) * (y - y.mean())) / np.sum((x - x.mean()) ** 2))
    s = E.curve_slope(cells)
    assert s["gap_vs_log_train_steps"] == pytest.approx(want)
    assert s["n_cells"] == 4 and s["note"]
    # gap hits 0 at exp(-intercept/slope) when the slope is negative
    b = y.mean() - want * x.mean()
    assert s["zero_gap_train_steps"] == pytest.approx(math.exp(-b / want))
    assert s["crossing"] == "gap falls to 0 from above"
    assert s["efolds_beyond_data"] == pytest.approx(-b / want - math.log(1600))
    assert s["measured_train_steps"] == [100, 1600]


def test_curve_slope_degenerate():
    assert E.curve_slope([_cell(100, 0.3), _cell(100, 0.2)])["gap_vs_log_train_steps"] is None
    assert E.curve_slope([])["gap_vs_log_train_steps"] is None
    # cells without a gap are left out
    s = E.curve_slope([_cell(100, None), _cell(100, 0.3), _cell(1000, 0.1)])
    assert s["n_cells"] == 2 and s["gap_vs_log_train_steps"] < 0


def test_curve_slope_needs_two_distinct_train_sizes_despite_float_noise():
    # several seeds at fraction 1 share one train size; x - mean(x) is not exactly 0 in floats
    cells = [_cell(998, g) for g in (0.30, 0.31, 0.29, 0.33, 0.28)]
    s = E.curve_slope(cells)
    assert s["gap_vs_log_train_steps"] is None and s["zero_gap_train_steps"] is None
    assert s["n_cells"] == 5


def test_curve_slope_drops_nonfinite_gaps():
    s = E.curve_slope([_cell(1000, 0.3), _cell(4000, float("nan")), _cell(8000, 0.1)])
    assert s["n_cells"] == 2 and s["cells_dropped_nonfinite"] == 1
    assert math.isfinite(s["gap_vs_log_train_steps"]) and s["gap_vs_log_train_steps"] < 0


def test_curve_slope_reports_a_crossing_only_beyond_the_data():
    # gap already negative and still falling: the line crossed 0 in the past, not a prediction
    s = E.curve_slope([_cell(1000, -0.05), _cell(4000, -0.10)])
    assert s["gap_vs_log_train_steps"] < 0 and s["zero_gap_train_steps"] is None
    assert s["crossing"] is None
    # gap negative and rising: a real future crossing, reported with its direction
    s = E.curve_slope([_cell(1000, -0.10), _cell(4000, -0.05)])
    assert s["zero_gap_train_steps"] == pytest.approx(16000, rel=1e-6)
    assert s["crossing"] == "gap rises to 0 from below"
    assert s["efolds_beyond_data"] == pytest.approx(math.log(4))


def test_curve_slope_per_seed():
    cells = [dict(_cell(100, 0.3), seed=0), dict(_cell(1000, 0.1), seed=0),
             dict(_cell(100, 0.3), seed=1), dict(_cell(1000, 0.2), seed=1),
             dict(_cell(100, 0.3), seed=2)]                  # seed 2: one size only -> None
    s = E.curve_slope(cells)
    assert s["per_seed"]["0"] == pytest.approx(-0.2 / math.log(10))
    assert s["per_seed"]["1"] == pytest.approx(-0.1 / math.log(10))
    assert s["per_seed"]["2"] is None


# ---- end to end ------------------------------------------------------------------------------------

def _forbid_test_scoring(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("curve must never score the test split or touch the ledger")

    for name in ("record_test_scoring", "_TestPass", "scorecard", "evaluate_run",
                 "backfill_ledger", "_append_ledger"):
        monkeypatch.setattr(E, name, boom)
    seen = {"fit": 0, "train": 0, "score": 0, "scored_ids": set()}
    real_fit, real_train, real_score = E.fit_baselines, T.train, T.WorldModel.score

    def fit(ds, *a, **k):
        assert not ds.ids("test")
        seen["fit"] += 1
        return real_fit(ds, *a, **k)

    def train(cfg, tds, *a, **k):
        assert not [t for t, m in tds.meta.items() if m.get("split") == "test"]
        seen["train"] += 1
        return real_train(cfg, tds, *a, **k)

    def score(self, tasks):
        seen["scored_ids"].update(str(t) for t in tasks)   # the test asserts these are val only
        seen["score"] += 1
        return real_score(self, tasks)

    monkeypatch.setattr(E, "fit_baselines", fit)
    monkeypatch.setattr(T, "train", train)
    monkeypatch.setattr(T.WorldModel, "score", score)
    return seen


@needs_mlx
def test_learning_curve_end_to_end_json_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path / "home"))
    seen = _forbid_test_scoring(monkeypatch)
    ds = _ds()
    res = E.learning_curve(ds, fractions=(0.5, 1.0), seeds=(0,),
                           overrides={"epochs": 1, "d": 16, "hidden": 32, "pred_hidden": 32,
                                      "w_reg": 1.0},
                           device="cpu", log=lambda m: None)
    assert seen["fit"] == 2 and seen["train"] == 2 and seen["score"] >= 2
    assert seen["scored_ids"] and seen["scored_ids"] <= {str(t) for t in ds.ids("val")}
    assert res["fractions"] == [0.5, 1.0] and res["seeds"] == [0]
    assert len(res["cells"]) == 2
    n_all = sum(len(ds.steps[t]) for t in ds.ids("train"))
    for c in res["cells"]:
        assert set(c) >= {"fraction", "seed", "train_sessions", "train_steps", "val_ce",
                          "jepa_minus_logistic"}
        assert set(c["val_ce"]) == {"prior", "markov1", "markov2", "logistic", "jepa", "raw_probe"}
        for k, v in c["val_ce"].items():
            assert v is not None and math.isfinite(v) and v > 0, k
        g = c["jepa_minus_logistic"]
        assert g["mean"] == pytest.approx(c["val_ce"]["jepa"] - c["val_ce"]["logistic"])
        assert len(g["ci"]) == 2 and g["ci"][0] <= g["ci"][1]   # percentile CI need not contain the mean
    full = next(c for c in res["cells"] if c["fraction"] == 1.0)
    half = next(c for c in res["cells"] if c["fraction"] == 0.5)
    assert full["train_steps"] == n_all and half["train_steps"] < n_all
    assert "gap_vs_log_train_steps" in res["slope"] and res["slope"]["note"]
    assert res["test_scored"] is False
    assert not (tmp_path / "home").exists()                 # nothing written under the home
    json.dumps(res)                                         # JSON-serialisable
    txt = E.render_curve(res)
    assert "logistic" in txt and "jepa" in txt and "slope" in txt


@needs_mlx
def test_cmd_curve_strips_test_writes_out_and_never_touches_ledger(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("APEX_ROUTER_HOME", str(home))
    _forbid_test_scoring(monkeypatch)
    raw, _ = fixtures.dataset(30, seed=1)
    assert raw.ids("test")
    loaded = []

    def load(*a, **k):
        loaded.append(raw)
        return raw

    monkeypatch.setattr(P, "load", load)
    real_lc = E.learning_curve

    def lc(ds, *a, **k):
        assert not ds.ids("test")                           # stripped before anything runs
        return real_lc(ds, *a, **k)

    monkeypatch.setattr(E, "learning_curve", lc)
    out = tmp_path / "curve.json"
    rc = E.cmd_curve(["--fractions", "0.5,1", "--seeds", "0", "--epochs", "1", "--device", "cpu",
                      "--config", json.dumps({"w_reg": 1, "d": 16, "hidden": 32,
                                              "pred_hidden": 32}),
                      "--z-norm", "center", "--w-reg-schedule", "anneal", "--w-reg-start", "30",
                      "--w-reg-anneal-epochs", "4", "--features", "a2f", "--regime",
                      "--out", str(out)])
    assert rc == 0 and loaded
    res = json.loads(out.read_text())
    assert [c["fraction"] for c in res["cells"]] == [0.5, 1.0]
    assert res["config"]["z_norm"] == "center" and res["config"]["features"] == "a2f"
    assert res["config"]["regime"] is True and res["config"]["w_reg"] == 1
    assert res["splits"]["test"] == E.TEST_STRIPPED and "stripped" in E.TEST_STRIPPED
    assert not (home / "worldmodel" / "eval" / "ledger.jsonl").exists()
    assert not out.with_suffix(".partial.json").exists()    # checkpoint removed on success
    assert "slope" in capsys.readouterr().out


def test_cmd_curve_refuses_bad_flags(capsys):
    assert E.cmd_curve(["--fractions", "0,1"]) == 2
    assert E.cmd_curve(["--fractions", "a"]) == 2
    assert E.cmd_curve(["--seeds", ""]) == 2
    assert E.cmd_curve(["--config", "{\"seed\": 1}"]) == 2
    assert "apex-router worldmodel curve:" in capsys.readouterr().err


def test_curve_registered():
    from apex_router.worldmodel import cli
    assert E.COMMANDS["curve"] is E.cmd_curve
    assert "curve" in cli.COMMANDS
