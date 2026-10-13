"""Task-outcome predictor prototype (worldmodel/outcome.py) — synthetic fixtures only."""
from __future__ import annotations

import json
import random

import numpy as np
import pytest

from apex_router.worldmodel import outcome as oc


def _step(sid, idx, i, act, *, agent=None, err=0, ts=None, tests=None, phase="explore", spawn=0):
    return {"sid": sid, "src": "claude", "task": f"{sid}:{idx}", "agent": agent, "i": i,
            "ts": float(i if ts is None else ts), "act": act, "tool": "x", "err": err,
            "tests": tests or {"ran": 0, "failed": None, "passed": None}, "out_b": 0,
            "in_b": 0, "dt": None, "phase": phase, "model": "m", "spawn": spawn}


def _seq(acts, errs=None, sid="s", idx=0):
    errs = errs or [0] * len(acts)
    return [_step(sid, idx, i, a, err=e) for i, (a, e) in enumerate(zip(acts, errs))]


# ---- feature extraction ---------------------------------------------------------------------

def test_first_edit_index_normalised():
    st = _seq(["read", "search", "read", "edit", "test"])
    f = oc.structure_features(st, {"task_type": None, "workflow": "W0", "src": "claude"}, [])
    assert f["first_edit"] == pytest.approx(3 / 5)
    st2 = _seq(["read", "read"])
    f2 = oc.structure_features(st2, {"task_type": None, "workflow": "W0", "src": "claude"}, [])
    assert f2["first_edit"] == 1.0 and f2["has_edit"] == 0.0


def test_error_response_rate():
    # err at 0 answered by read at 2 (within 3); err at 4 followed only by edit/run -> not answered
    acts = ["run", "edit", "read", "edit", "run", "edit", "run", "edit", "write"]
    errs = [1, 0, 0, 0, 1, 0, 0, 0, 0]
    f = oc.structure_features(_seq(acts, errs), {"workflow": "W0", "src": "claude"}, [])
    assert f["err_response"] == pytest.approx(0.5)
    assert f["err_share"] == pytest.approx(2 / 9)


def test_error_response_stays_within_stream():
    st = [_step("s", 0, 0, "run", err=1, ts=0),
          _step("s", 0, 0, "read", agent="a1", ts=1),   # other stream: must not count
          _step("s", 0, 1, "edit", ts=2)]
    f = oc.structure_features(oc.order_steps(st), {"workflow": "W0", "src": "claude"}, [])
    assert f["err_response"] == 0.0
    assert f["n_sub_steps"] == pytest.approx(np.log1p(1))


def test_prefix_truncation_and_tests_last():
    st = _seq(["read"] * 4 + ["test"] * 4)
    st[4]["tests"] = {"ran": 1, "failed": 2, "passed": 5}
    st[7]["tests"] = {"ran": 1, "failed": 0, "passed": 9}
    assert len(oc.prefix(st, 0.25)) == 2
    assert len(oc.prefix(st, 0.5)) == 4
    assert len(oc.prefix(st, 1.0)) == 8
    assert len(oc.prefix(st[:1], 0.25)) == 1
    # absolute first-k prefixes (values > 1): the online quantity, capped by the task's length
    assert len(oc.prefix(st, 3)) == 3 and len(oc.prefix(st, 16)) == 8
    assert oc.structure_features(oc.prefix(st, 3), {"workflow": "W0", "src": "claude"}, [])["n_steps"] \
        == pytest.approx(np.log1p(3))
    full = oc.structure_features(st, {"workflow": "W0", "src": "claude"}, [])
    assert full["tests_failed_last"] == 0.0 and full["tests_passed_last"] == pytest.approx(np.log1p(9))
    p = oc.structure_features(oc.prefix(st, 0.625), {"workflow": "W0", "src": "claude"}, [])
    assert p["tests_failed_last"] == pytest.approx(np.log1p(2))


def test_tail_errors_and_early_patch():
    acts = ["edit"] * 3 + ["read"] * 9 + ["run"] * 3
    errs = [0] * 12 + [1, 0, 1]
    f = oc.structure_features(_seq(acts, errs), {"workflow": "W2", "src": "pi"}, [])
    assert f["tail_errors"] == 2.0
    assert f["early_patch"] == pytest.approx(0.3)
    assert f["w2"] == 1.0 and f["src_pi"] == 1.0


# ---- Markov surprise --------------------------------------------------------------------------

def test_markov_surprise_values():
    m = oc.Markov(alpha=0.5)
    m.fit([["read", "read", "read"]] * 10)
    s = m.surprisal(["read", "read", "edit"])
    assert s[0] < s[2] and s[1] < s[2]
    assert all(x > 0 for x in s)


def _synthetic_tasks(n=40, seed=0, informative=True):
    rng = random.Random(seed)
    tasks = []
    for k in range(n):
        y = k % 3 == 0
        n_steps = rng.randint(6, 20)
        acts = [rng.choice(["read", "search", "edit", "test", "run"]) for _ in range(n_steps)]
        errs = [int(informative and y and rng.random() < 0.5) for _ in range(n_steps)]
        st = _seq(acts, errs, sid=f"s{k}", idx=0)
        tasks.append(oc.Task(key=f"s{k}:0", y=int(y), steps=oc.order_steps(st),
                             meta={"task_type": None, "workflow": "W0", "src": "claude",
                                   "split": "train"},
                             judge={"outcome": "fail" if y else "success", "confidence": 0.9},
                             votes={}))
    return tasks


def test_markov_fit_excludes_held_out_and_cv_never_trains_on_held_out():
    tasks = _synthetic_tasks()
    markov_log, fit_log = [], []
    oc.nested_cv(tasks, oc.MODELS["F"], 1.0, repeats=2, k=5, seed=0,
                 on_markov_fit=lambda keys, held: markov_log.append((set(keys), set(held))),
                 on_fit=lambda train, test: fit_log.append((set(train), set(test))))
    assert markov_log and fit_log
    for keys, held in markov_log:
        assert not keys & held
    outer = [(tr, te) for tr, te in fit_log if len(te) and len(tr) + len(te) == len(tasks)]
    assert outer
    for train, test in fit_log:
        assert not train & test
    # every task is held out exactly once per repeat
    held = [k for tr, te in outer for k in te]
    assert len(held) == 2 * len(tasks) and set(held) == {t.key for t in tasks}


def test_stratified_folds_cover_and_balance():
    y = np.array([1] * 10 + [0] * 30)
    folds = oc.stratified_folds(y, 5, np.random.default_rng(0))
    allidx = np.concatenate(folds)
    assert sorted(allidx.tolist()) == list(range(40))
    assert all(y[f].sum() == 2 for f in folds)


# ---- baselines on random labels ----------------------------------------------------------------

def test_prior_and_length_baselines_near_chance_on_random_labels():
    tasks = _synthetic_tasks(n=200, seed=3, informative=False)
    rng = np.random.default_rng(1)
    for t in tasks:
        t.y = int(rng.random() < 0.3)
    y = np.array([t.y for t in tasks])
    for name in ("A", "B"):
        p = oc.nested_cv(tasks, oc.MODELS[name], 1.0, repeats=1, k=5, seed=0).mean(0)
        assert abs(oc.auroc(y, p) - 0.5) < 0.12, name


def test_logreg_learns_signal_and_metrics():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(300, 3))
    y = (X[:, 0] + 0.3 * rng.normal(size=300) > 0).astype(int)
    w, b = oc.fit_logreg(X, y, lam=0.01)
    assert w[0] > 1.0 and abs(w[1]) < 0.5
    assert oc.auroc(y, X[:, 0]) > 0.9
    assert oc.auroc(np.array([0, 1]), np.array([0.5, 0.5])) == 0.5
    assert oc.brier(np.array([0, 1]), np.array([0.0, 1.0])) == 0.0
    assert oc.ece(np.array([0, 1]), np.array([0.0, 1.0]), bins=10)[0] == 0.0


# ---- card shape --------------------------------------------------------------------------------

def _write_fixture(tmp_path, tasks):
    steps = tmp_path / "steps.jsonl"
    wtasks = tmp_path / "tasks.jsonl"
    gold = tmp_path / "gold.jsonl"
    ltasks = tmp_path / "ltasks.jsonl"
    with steps.open("w") as f:
        for t in tasks:
            for s in t.steps:
                f.write(json.dumps(s) + "\n")
    with wtasks.open("w") as f, gold.open("w") as g, ltasks.open("w") as lt:
        for j, t in enumerate(tasks):
            sid, idx = t.key.split(":")
            split = "test" if j == 0 else "train"
            f.write(json.dumps({"task": t.key, "sid": sid, "src": "claude", "split": split,
                                "task_type": None, "workflow": "W0"}) + "\n")
            lid = f"L{j}"
            lt.write(json.dumps({"id": lid, "session": sid, "index": int(idx),
                                 "judge": t.judge, "votes": {"tests": "fail"} if t.y else {}})
                     + "\n")
            g.write(json.dumps({"id": lid, "outcome": "unknown", "ts": 1}) + "\n")
            g.write(json.dumps({"id": lid, "outcome": "fail" if t.y else "success", "ts": 2})
                    + "\n")
        g.write(json.dumps({"id": "L1", "outcome": "unknown", "ts": 3}) + "\n")  # last wins
    return steps, wtasks, gold, ltasks


def test_load_last_gold_wins_drops_unknown_and_p6_test(tmp_path):
    tasks = _synthetic_tasks(n=12)
    steps, wtasks, gold, ltasks = _write_fixture(tmp_path, tasks)
    got, info = oc.load(steps, wtasks, gold, ltasks)
    keys = {t.key for t in got}
    assert tasks[0].key not in keys          # P6 test split excluded
    assert tasks[1].key not in keys          # last gold row is unknown
    assert len(got) == 10 and info["excluded_p6_test"] == 1 and info["unknown"] == 1
    got2, _ = oc.load(steps, wtasks, gold, ltasks, include_p6_test=True)
    assert len(got2) == 11


def test_card_shape(tmp_path):
    tasks = _synthetic_tasks(n=30)
    steps, wtasks, gold, ltasks = _write_fixture(tmp_path, tasks)
    out = tmp_path / "card.json"
    rc = oc.main(["--steps", str(steps), "--tasks", str(wtasks), "--gold", str(gold),
                  "--label-tasks", str(ltasks), "--out", str(out), "--repeats", "1",
                  "--boot", "50"])
    assert rc == 0
    card = json.loads(out.read_text())
    assert set(card) >= {"meta", "results", "paired", "calibration", "top_features"}
    assert card["meta"]["n"] == 28 and card["meta"]["n_bad"] + card["meta"]["n_success"] == 28
    names = {(r["model"], r["prefix"]) for r in card["results"]}
    for m in "DEF":
        for p in (0.25, 0.5, 1.0):
            assert (m, p) in names
    assert {"A", "B", "C"} <= {r["model"] for r in card["results"]}
    r = card["results"][0]
    assert {"auroc", "auroc_ci", "brier", "brier_ci", "n"} <= set(r)
    assert len(r["auroc_ci"]) == 2
    assert {p["vs"] for p in card["paired"]} == {"C", "B"}
    assert "ece10" in card["calibration"]
    assert len(card["top_features"]) == 8
