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

    labelled_train = None          # set to 0 to model a run that never saw a label

    def __init__(self, run_dir, d=8, dc=4, seed=0):
        self.run_dir = Path(run_dir)
        self.cfg = SimpleNamespace(macro=False, embed=False, d=d, d_control=dc, erank_min=2.0,
                                   sigreg_max=10.0, seed=seed, device="cpu", w_reg=1.0)
        self.summary = {"best_epoch": 0, "best": {"val_next_ce": 2.0}}
        if self.labelled_train is not None:
            self.summary["data"] = {"labelled_train": self.labelled_train}
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


# ---- review fixes: provenance, view guard, untrained value head, ledger, display -----------------

def test_git_dirty_flag_sees_src_from_a_package_dir(tmp_path):
    import shutil
    import subprocess
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    pkg = repo / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "m.py").write_text("x = 1\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env, capture_output=True)
    clean = E.git_sha(pkg)
    assert clean["sha"] and clean["dirty_src"] is False
    (pkg / "m.py").write_text("x = 2\n")                 # touched file under src/, seen from pkg/
    assert E.git_sha(pkg)["dirty_src"] is True
    assert E.git_sha(tmp_path / "nowhere") == {"sha": None, "dirty_src": None}


def test_check_view_refuses_a_mismatch():
    assert E.check_view({"view": "streams"}, "streams") == "streams"
    assert E.check_view({"data": {"source": "/x/steps.jsonl:task"}}, "task") == "task"
    assert E.check_view({"data": {"source": "/x/steps.jsonl"}}, "task") == "task"   # legacy run
    assert E.check_view({"data": {"source": "synthetic:300:seed0"}}, "streams") is None
    assert E.check_view({}, "streams") is None
    with pytest.raises(E.ViewMismatch, match="--view task"):
        E.check_view({"view": "task"}, "streams")
    with pytest.raises(E.ViewMismatch):
        E.check_view({"data": {"source": "/x/steps.jsonl"}}, "streams")


def test_view_mismatch_refused_before_any_test_pass(fake_run, tmp_path):
    rd = tmp_path / "worldmodel" / "runs" / "fake-run"
    rd.mkdir(parents=True)
    (rd / "summary.json").write_text(json.dumps({"view": "task"}))
    with pytest.raises(E.ViewMismatch):
        E.scorecard(_ds(), "synthetic: 40", ["fake-run"], view="streams")
    assert fake_run == []                                # no WorldModel loaded, nothing scored
    assert not E.ledger_path().exists()


def _write_contract(home: Path, n=30):
    """fixtures data with a subagent stream per delegating task, as E1 writes it."""
    syn = fixtures.synthetic(n, seed=2)
    steps = []
    for s in syn.steps:
        steps.append(s)
        if s["act"] == "delegate":
            for j in range(3):
                steps.append(dict(s, agent=f"a{s['i']}", i=j, act="search", spawn=0,
                                  ts=s["ts"] + 0.1 * (j + 1)))
    home.mkdir(parents=True, exist_ok=True)
    (home / "steps.jsonl").write_text("\n".join(json.dumps(s) for s in steps) + "\n")
    (home / "tasks.jsonl").write_text("\n".join(json.dumps(t) for t in syn.tasks) + "\n")


def test_load_view_streams_default_and_task_escape_hatch(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    home = tmp_path / "worldmodel"
    _write_contract(home)
    st, tk = T.load_view(), T.load_view("task")
    assert st.source.endswith("steps.jsonl:streams") and tk.source.endswith("steps.jsonl:task")
    assert T.run_view(st.source) == "streams" and T.run_view(tk.source) == "task"
    assert len(st.tasks) > len(tk.tasks)                 # subagent streams are their own sequences
    sub = [t for t in st.tasks if "@" in t]
    assert sub and all(st.meta[t]["outcome"] == "unknown" for t in sub)
    assert sum(map(len, st.tasks.values())) == sum(map(len, tk.tasks.values()))
    with pytest.raises(ValueError):
        T.load_view("bogus")


def test_untrained_value_head_makes_2_3_5_inconclusive(fake_run, monkeypatch):
    monkeypatch.setattr(FakeWM, "labelled_train", 0)
    ds = _ds()                                           # gold labels on test DO exist
    assert E.main_ids(ds, "test", gold_only=True)
    card = E.scorecard(ds, "synthetic: gold", ["fake-run"])
    p = card["per_run"][0]
    assert p["value_head_trained"] is False
    for k in (2, 3, 5):
        assert card["verdicts"][k] == "INCONCLUSIVE"
        assert p["reasons"][k] == E.UNTRAINED
        assert any(b.startswith(f"C{k}: {E.UNTRAINED}") for b in card["blockers"])
    assert p["outcome_gold"]["skipped"] == E.UNTRAINED and p["outcome_all"]["n"] == 0
    assert p["ranking"]["jepa"]["accuracy"] is None
    assert p["criterion5"] is None or "jepa" not in p["criterion5"]["detectors"]
    txt = E.render(card)
    assert txt.count(E.UNTRAINED) >= 3 and "provisional" not in txt
    # the same data with a trained head is scored (the guard is the head, not the labels)
    monkeypatch.setattr(FakeWM, "labelled_train", 100)
    again = E.scorecard(ds, "synthetic: gold", ["fake-run"])
    assert again["per_run"][0]["value_head_trained"] and again["verdicts"][2] in ("PASS", "FAIL")


def test_jepa_val_ce_includes_step_0_like_the_baselines(fake_run):
    ds = _ds()
    card = E.scorecard(ds, "synthetic: 40", ["fake-run"])
    sp = np.array(card["start_prior"])
    va = ds.ids("val")
    tot = sum(-math.log(sp[P.act_id(ds.steps[t][0])]) + (len(ds.steps[t]) - 1) * math.log(P.V)
              for t in va)
    n = sum(len(ds.steps[t]) for t in va)
    p = card["per_run"][0]
    assert p["val_ce"] == pytest.approx(tot / n)
    assert p["val_next_ce_excl_step0"] == 2.0            # E3's readout kept, labelled as such
    assert f"{p['val_ce']:.3f}" in E.render(card)


def test_manifest_split_bounds_and_data_hashes(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"split_bounds": {"val": 1.0, "test": 2.0}}))
    (tmp_path / "steps.jsonl").write_text("{}\n")
    m = E.manifest_info(tmp_path)
    assert m["split_bounds"] == {"val": 1.0, "test": 2.0}
    assert len(m["steps_sha256"]) == 64 and m["tasks_sha256"] is None


def test_ledger_counts_every_real_test_scoring_and_backfills(fake_run, tmp_path):
    home = tmp_path / "worldmodel"
    (home / "eval").mkdir(parents=True)
    old = {"kind": "worldmodel-g1-scorecard", "created": "2026-10-08T00:00:00Z",
           "source": "/x/steps.jsonl", "view": "streams", "git": {"sha": "abc"},
           "manifest": {"sha256": None}, "runs": ["r0", "r1"], "cpu_replay_run": "r0-cpu"}
    syn = dict(old, source="synthetic: 300", runs=["s0"], cpu_replay_run=None)
    (home / "eval" / "a.json").write_text(json.dumps(old))
    (home / "eval" / "b.json").write_text(json.dumps(syn))
    card = E.scorecard(_ds(), "real-shaped (test)", ["fake-run"])
    rows = P.read_jsonl(E.ledger_path())
    assert [r["run_id"] for r in rows] == ["r0", "r1", "r0-cpu", "fake-run"]   # synthetic skipped
    assert all(r.get("backfilled") for r in rows[:3]) and not rows[3].get("backfilled")
    assert card["ledger"]["scorings"] == 4 and card["ledger"]["invocations"] == 2
    assert (os.stat(E.ledger_path()).st_mode & 0o777) == 0o600
    assert "test split scored 4 times for this manifest" in E.render(card)
    again = E.scorecard(_ds(), "real-shaped (test)", ["fake-run"])
    assert again["ledger"]["scorings"] == 5                       # appended, never rewritten
    assert E.backfill_ledger() == 0
    syn_card = E.scorecard(_ds(), "synthetic: 40", ["fake-run"])
    assert syn_card["ledger"] is None and len(P.read_jsonl(E.ledger_path())) == 5


def test_display_flags_small_days_and_probe_keys(fake_run):
    ds = _ds()
    card = E.scorecard(ds, "synthetic: 40", ["fake-run"])
    rows = card["per_run"][0]["per_day"]
    assert all(r["too_few"] == (r["n"] < E.DAY_MIN_N) for r in rows)
    txt = E.render(card)
    assert "near-tautological" in txt
    if any(r["too_few"] for r in rows):
        assert "[too few]" in txt
    jepa = E.Jepa(FakeWM("/nonexistent"), np.array(card["start_prior"]), 0.5)
    te = ds.ids("test")
    jepa.prepare(ds, te)
    arr = E.probe_arrays(jepa, ds, te)
    assert {"z_prefix", "x_prefix", "outcome_prefix"} <= set(arr) and "z_last" not in arr
    assert len(arr["z_prefix"]) == len(E.main_ids(ds, "test", labeled=True))


# ---- a tiny real run (mlx) -------------------------------------------------------------------------

@needs_mlx
def test_train_cli_records_view(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    _write_contract(tmp_path / "worldmodel", n=40)
    from apex_router.worldmodel.cli import main
    for args, want in (([], "streams"), (["--view", "task"], "task")):
        rc = main(["train", "--epochs", "1", "--run-id", f"v-{want}", "--json", *args])
        assert rc == 0
        capsys.readouterr()
        s = json.loads((tmp_path / "worldmodel" / "runs" / f"v-{want}" / "summary.json").read_text())
        assert s["view"] == want and s["data"]["source"].endswith(f":{want}")
    # evaluating the task-view run under the default stream view is refused with a clear error
    rc = main(["evaluate", "--run", "v-task", "--no-save"])
    assert rc == 2 and "--view task" in capsys.readouterr().err

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
