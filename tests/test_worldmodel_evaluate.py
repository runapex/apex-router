"""P6 E4: the G1 scorecard — criterion rules, start-prior parity, stream view, test scored once,
gold-only enforcement for criteria 2 and 5, scorecard JSON; a tiny real JEPA run when mlx exists."""
from __future__ import annotations

import json
import math
import os
import zlib
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from apex_router.worldmodel import evaluate as E
from apex_router.worldmodel import fixtures
from apex_router.worldmodel import jepa as J
from apex_router.worldmodel import protocol as P
from apex_router.worldmodel import train as T

needs_mlx = pytest.mark.skipif(not J.mlx_available(), reason="mlx not installed ([worldmodel] extra)")


# ---- the five rules on hand-built inputs ---------------------------------------------------------

@pytest.mark.parametrize("rel, lo, want", [
    (0.05, 0.001, "PASS"), (0.20, 0.01, "PASS"),
    (0.0499, 0.01, "FAIL"),          # below 5%
    (0.08, 0.0, "FAIL"),             # CI touches 0
    (0.08, -0.01, "FAIL"),
    (-0.3, -0.4, "FAIL"),
    (None, None, "INCONCLUSIVE"), (0.08, None, "INCONCLUSIVE"),
])
def test_rule1(rel, lo, want):
    assert E.rule1(rel, lo) == want


def _gold(diff, ci, q="gold", n=30):
    return {"n": n, "quality": q, "diff": diff, "diff_ci": ci}


@pytest.mark.parametrize("g, want", [
    (_gold(-0.02, (-0.04, -0.001)), "PASS"),
    (_gold(-0.02, (-0.04, 0.0)), "FAIL"),         # CI does not exclude 0
    (_gold(-0.02, (-0.04, 0.01)), "FAIL"),
    (_gold(0.03, (0.01, 0.05)), "FAIL"),
    (_gold(-0.02, (-0.04, -0.01), q="provisional"), "INCONCLUSIVE"),   # any weak label
    (_gold(None, (None, None), q="inconclusive", n=0), "INCONCLUSIVE"),
    (_gold(-0.02, (None, None)), "INCONCLUSIVE"),  # one session: no CI
    ({}, "INCONCLUSIVE"),
])
def test_rule2_gold_only(g, want):
    assert E.rule2(g) == want


@pytest.mark.parametrize("j, c, n, want", [
    (0.75, 0.75, 4, "PASS"), (1.0, 0.5, 2, "PASS"), (0.4, 0.5, 2, "FAIL"),
    (None, None, 0, "INCONCLUSIVE"), (0.5, 0.5, 0, "INCONCLUSIVE"),
])
def test_rule3(j, c, n, want):
    assert E.rule3(j, c, n) == want


def test_rule4_every_checkpoint():
    ok = {"within_bounds": True}
    bad = {"within_bounds": False}
    assert E.rule4([ok, ok, ok]) == "PASS"
    assert E.rule4([bad, ok, ok]) == "FAIL"          # an early epoch outside counts
    assert E.rule4([ok, ok, bad]) == "FAIL"          # the test-z checkpoint counts
    assert E.rule4([]) == "INCONCLUSIVE"


def test_rule5_gold_only():
    assert E.rule5({"verdict": "PASS"}, "gold") == "PASS"
    assert E.rule5({"verdict": "FAIL"}, "gold") == "FAIL"
    assert E.rule5({"verdict": "PASS"}, "provisional") == "INCONCLUSIVE"
    assert E.rule5(None, "gold") == "INCONCLUSIVE"


def test_combine_and_overall():
    assert E.combine(["PASS", "PASS"]) == "PASS"
    assert E.combine(["PASS", "FAIL"]) == "FAIL"
    assert E.combine(["PASS", "INCONCLUSIVE"]) == "INCONCLUSIVE"
    assert E.combine([]) == "INCONCLUSIVE"
    assert E.overall({k: "PASS" for k in range(1, 6)}) == "PASS"
    assert E.overall({1: "PASS", 2: "INCONCLUSIVE", 3: "PASS", 4: "PASS", 5: "PASS"}) == "INCONCLUSIVE"
    assert E.overall({1: "FAIL", 2: "INCONCLUSIVE", 3: "PASS", 4: "PASS", 5: "PASS"}) == "FAIL"


# ---- stream view -----------------------------------------------------------------------------------

def _st(task, agent, i, act, ts, sid="s1"):
    return {"sid": sid, "agent": agent, "task": task, "i": i, "ts": ts, "act": act, "err": 0,
            "tests": {"ran": 0, "failed": None, "passed": None}, "spawn": 0}


def test_stream_view_separates_subagents():
    steps = [_st("s1:0", None, 0, "read", 1.0), _st("s1:0", None, 1, "delegate", 2.0),
             _st("s1:0", "a1", 0, "search", 3.0), _st("s1:0", "a1", 1, "search", 4.0),
             _st("s1:0", None, 2, "vcs", 5.0)]
    tasks = [{"task": "s1:0", "sid": "s1", "split": "train", "outcome": "success",
              "outcome_src": "gold", "t0": 1.0}]
    raw = P.build(steps, tasks)
    assert [s["act"] for s in raw.steps["s1:0"]][:2] == ["read", "search"]   # interleaved by i
    ds = E.stream_view(raw)
    assert [s["act"] for s in ds.steps["s1:0"]] == ["read", "delegate", "vcs"]
    assert [s["act"] for s in ds.steps["s1:0@a1"]] == ["search", "search"]
    sub = ds.tasks["s1:0@a1"]
    assert sub["sid"] == "s1" and sub["split"] == "train" and sub["outcome"] == "unknown"
    assert ds.tasks["s1:0"]["outcome"] == "success"
    assert E.main_ids(ds) == ["s1:0"]


# ---- a fake WorldModel (no mlx) --------------------------------------------------------------------

class FakeWM:
    """WorldModel stand-in: deterministic per-step outputs; records every score() call."""

    def __init__(self, run_dir, d=8, dc=4, seed=0):
        self.run_dir = Path(run_dir)
        self.cfg = SimpleNamespace(macro=False, embed=False, d=d, d_control=dc, erank_min=2.0,
                                   sigreg_max=10.0, seed=seed, device="cpu", w_reg=1.0)
        self.summary = {"best_epoch": 0, "best": {"val_next_ce": 2.0}}
        self.calls = []

    def score(self, tasks):
        self.calls.append(sorted(tasks))
        out = {}
        for tid, st in tasks.items():
            n = len(st)
            rng = np.random.default_rng(zlib.crc32(tid.encode()))
            v = np.clip(0.5 + 0.02 * np.cumsum(rng.normal(size=n)), 0.01, 0.99)
            z = rng.normal(size=(n, self.cfg.d))
            out[tid] = {"z": z, "z_control": z[:, -self.cfg.d_control:],
                        "next_action_probs": np.full((n, P.V), 1.0 / P.V), "p_success": v,
                        "steps": st, "offline_only": False}
        return out


@pytest.fixture
def fake_run(tmp_path, monkeypatch):
    rd = tmp_path / "runs" / "fake-run"
    rd.mkdir(parents=True)
    (rd / "metrics.jsonl").write_text("\n".join(json.dumps(
        {"epoch": e, "collapse": {"effective_rank": 5.0, "sigreg": 0.05, "within_bounds": True}})
        for e in range(2)) + "\n")
    made = []

    def load(run):
        wm = FakeWM(rd)
        made.append(wm)
        return wm

    monkeypatch.setattr(T.WorldModel, "load", staticmethod(load))
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    return made


def _ds(n=40, seed=1, **kw):
    raw, _ = fixtures.dataset(n, seed=seed, **kw)
    return E.stream_view(raw)


def test_start_prior_parity_between_models(fake_run):
    ds = _ds()
    sp = E.start_prior(ds, ds.ids("train"))
    base = E.fit_baselines(ds, sp)
    jepa = E.Jepa(FakeWM("/nonexistent"), sp, 0.5)
    te = ds.ids("test")
    jepa.prepare(ds, te)
    for t in te[:10]:
        st, row = ds.steps[t], ds.tasks[t]
        want = math.log(sp[P.act_id(st[0])])
        assert jepa.task_logprobs(st, row)[0] == pytest.approx(want)
        for m in base["models"]:
            assert m.task_logprobs(st, row)[0] == pytest.approx(want), m.name
            assert np.allclose(m.predict_proba([], row), sp)
    # the shared prior is E3's convention: features.start_prior over the train sequences
    from apex_router.worldmodel import features as F
    assert np.allclose(sp, F.start_prior({t: ds.steps[t] for t in ds.ids("train")}))


def test_test_split_scored_once_and_selection_on_val(fake_run, monkeypatch):
    ds = _ds()
    te = set(ds.ids("test"))
    calls = Counter()
    real = E.StartPrior.task_logprobs

    def spy(self, steps, task=None):
        calls[(self.name, task["task"])] += 1
        lp = real(self, steps, task)
        if task["task"] in te and self.name.startswith("prior"):
            lp = np.zeros_like(lp)                   # on TEST the prior looks perfect …
        return lp

    monkeypatch.setattr(E.StartPrior, "task_logprobs", spy)
    card = E.scorecard(ds, "synthetic: 40", ["fake-run"], seed=0)
    wm = fake_run[0]
    # one WorldModel.score pass contains the test sequences, exactly once each
    test_hits = Counter(t for c in wm.calls for t in c if t in te)
    assert set(test_hits) == te and set(test_hits.values()) == {1}
    assert sum(1 for c in wm.calls if te & set(c)) == 1
    # every baseline scores every test sequence exactly once
    for m in card["baselines"]["val_ce"]:
        assert all(calls[(m, t)] == 1 for t in te), m
    # … but the best baseline is chosen on val, not test
    vce = card["baselines"]["val_ce"]
    assert card["baselines"]["best_next_action"] == min(vce, key=vce.get)
    assert not card["baselines"]["best_next_action"].startswith("prior")


def test_gold_only_for_criteria_2_and_5(fake_run):
    weak = _ds(weak_share=1.0)                       # every label weak
    card = E.scorecard(weak, "synthetic: weak", ["fake-run"])
    p = card["per_run"][0]
    assert card["verdicts"][2] == "INCONCLUSIVE" and card["verdicts"][5] == "INCONCLUSIVE"
    assert p["outcome_gold"]["n"] == 0
    assert p["outcome_all"]["n"] > 0 and p["outcome_all"]["quality"] == "provisional"
    assert p["criterion5"]["quality"] == "provisional"
    assert any("C2" in b for b in card["blockers"]) and any("C5" in b for b in card["blockers"])
    assert card["g1"] != "PASS"
    gold = E.scorecard(_ds(), "synthetic: gold", ["fake-run"])
    assert gold["per_run"][0]["outcome_gold"]["quality"] == "gold"
    assert gold["verdicts"][2] in ("PASS", "FAIL") and gold["verdicts"][5] in ("PASS", "FAIL")


def test_uniform_jepa_fails_criterion_1(fake_run):
    card = E.scorecard(_ds(), "synthetic: 40", ["fake-run"])
    c1 = card["per_run"][0]["c1"]
    assert c1["rel"] < 0 and card["verdicts"][1] == "FAIL"     # uniform beats nothing
    assert card["verdicts"][4] == "PASS"                       # fake checkpoints are in bounds
    assert card["g1"] == "FAIL"


def test_scorecard_json_shape_and_mode(fake_run, tmp_path):
    card = E.scorecard(_ds(), "synthetic: 40", ["fake-run"])
    path = E.save(card, home=tmp_path / "worldmodel")
    assert path.parent == tmp_path / "worldmodel" / "eval" and path.name.endswith("-fake-run.json")
    assert (os.stat(path).st_mode & 0o777) == 0o600
    j = json.loads(path.read_text())
    for k in ("kind", "schema", "created", "source", "view", "git", "manifest", "runs", "seeds",
              "splits", "start_prior", "baselines", "per_run", "verdicts", "g1", "blockers", "rules"):
        assert k in j, k
    assert set(j["verdicts"]) == {"1", "2", "3", "4", "5"}
    assert all(v in E.VERDICTS for v in j["verdicts"].values())
    pr = j["per_run"][0]
    for k in ("ce", "paired", "c1", "outcome_gold", "ranking", "collapse", "probes", "per_day"):
        assert k in pr, k
    assert j["runs"] == ["fake-run"] and j["seeds"] == [0]
    assert "sha" in j["git"]
    assert "outcome" not in json.dumps(j["per_run"][0]["per_day"])   # no text, only numbers
    assert E.render(card).count("\nC") >= 5


def test_manifest_info_hashes(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"built_at": "x", "splits": {"train": {}}}))
    m = E.manifest_info(tmp_path)
    assert len(m["sha256"]) == 64 and m["splits"] == {"train": {}}
    assert E.manifest_info(tmp_path / "missing")["sha256"] is None


def test_value_zeno_flags_converging_short():
    v = np.array([0.1, 0.2, 0.25, 0.275, 0.2875, 0.29375])     # geometric to ~0.3 < 0.5
    s = E.value_zeno_scores(v, threshold=0.5)
    assert np.isfinite(s[-1]) and s[-1] > 0 and np.all(np.isneginf(s[:3]))
    up = np.array([0.1, 0.4, 0.55, 0.625, 0.66, 0.68])         # converging to ~0.7 > 0.5
    assert np.all(np.isneginf(E.value_zeno_scores(up, threshold=0.5)))


# ---- a tiny real run (mlx) -------------------------------------------------------------------------

@needs_mlx
def test_cli_train_and_evaluate_synthetic(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    from apex_router.worldmodel.cli import main
    rc = main(["evaluate", "--synthetic", "30", "--train", "--seeds", "1", "--no-cpu",
               "--epochs", "1", "--json"])
    assert rc == 0
    card = json.loads(capsys.readouterr().out)
    assert card["g1"] in E.VERDICTS and len(card["runs"]) == 1
    run = card["runs"][0]
    assert (tmp_path / "worldmodel" / "runs" / run / "weights.safetensors").exists()
    p0 = card["per_run"][0]
    assert p0["start_prior_matches_run"] is True       # E3's start prior == the shared one
    assert p0["ce"]["JEPA"]["n"] == card["splits"]["test"]["steps"]
    assert Path(card["path"]).exists()
    # re-evaluating the saved run gives the same test CE (one deterministic scoring pass)
    rc = main(["evaluate", "--synthetic", "30", "--run", run, "--json", "--no-save"])
    again = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert again["per_run"][0]["ce"]["JEPA"]["value"] == pytest.approx(p0["ce"]["JEPA"]["value"], abs=1e-4)
