"""P6 evaluation protocol: split handling, CE/Brier/ECE by hand, session bootstrap, label flags."""
from __future__ import annotations

import json
import math

import pytest

np = pytest.importorskip("numpy")
from apex_router.worldmodel import fixtures, protocol as P  # noqa: E402


def _step(sid, task, i, act="read", ts=None, **kw):
    return {"sid": sid, "task": task, "i": i, "ts": ts if ts is not None else 1000.0 + i,
            "act": act, "tool": "Read", "err": 0, "tests": {"ran": 0}, **kw}


def _task(task, sid, split, outcome="success", src="gold", t0=1000.0, **kw):
    return {"task": task, "sid": sid, "split": split, "outcome": outcome, "outcome_src": src,
            "t0": t0, **kw}


class Fixed:
    """A model with one fixed next-action distribution and one P(success)."""

    def __init__(self, p, ps=0.5):
        self.p, self.ps = np.asarray(p, dtype=float), ps

    def predict_proba(self, history, task=None):
        return self.p

    def outcome_proba(self, steps, task=None):
        return self.ps


# ---- data + split ------------------------------------------------------------------------------

def test_build_split_inheritance_and_drops():
    steps = [_step("s1", "s1:0", 1), _step("s1", "s1:0", 0),          # out of order → sorted
             _step("s1", "s1:9", 0),                                     # no task row → session split
             _step("s2", "s2:0", 0),                                     # session unknown → dropped
             {"sid": "s1", "task": "s1:0", "i": "x"}]                    # malformed i → dropped
    tasks = [_task("s1:0", "s1", "train"), _task("s3:0", "s3", "bogus")]
    ds = P.build(steps, tasks)
    assert [s["i"] for s in ds.steps["s1:0"]] == [0, 1]
    assert ds.tasks["s1:9"]["split"] == "train" and ds.tasks["s1:9"]["outcome"] == "unknown"
    assert "s2:0" not in ds.tasks and "s3:0" not in ds.tasks
    assert ds.dropped == 2


def test_ids_filters_and_time_order():
    steps = [_step("a", "a:0", 0), _step("b", "b:0", 0), _step("c", "c:0", 0)]
    tasks = [_task("a:0", "a", "test", t0=30.0), _task("b:0", "b", "test", src="weak", t0=10.0),
             _task("c:0", "c", "test", outcome="unknown", src="none", t0=20.0)]
    ds = P.build(steps, tasks)
    assert ds.ids("test") == ["b:0", "c:0", "a:0"]
    assert ds.ids("test", labeled=True) == ["b:0", "a:0"]
    assert ds.ids("test", labeled=True, gold_only=True) == ["a:0"]
    assert ds.ids("train") == []


def test_synthetic_split_follows_session_start():
    ds, _ = fixtures.dataset(50, seed=3)
    order = sorted({ds.sid(t) for t in ds.tasks},
                   key=lambda s: min(ds.tasks[t]["t0"] for t in ds.tasks if ds.sid(t) == s))
    splits = [ds.tasks[next(t for t in ds.tasks if ds.sid(t) == s)]["split"] for s in order]
    assert splits == ["train"] * 35 + ["val"] * 5 + ["test"] * 10
    for t in ds.tasks.values():          # every task of a session shares its split
        assert t["split"] == splits[order.index(t["sid"])]


def test_read_jsonl_fails_open(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\nnot json\n[1, 2]\n\n{"b": 2}\n')
    assert P.read_jsonl(p) == [{"a": 1}, {"b": 2}]
    assert P.read_jsonl(tmp_path / "missing.jsonl") == []


def test_load_honours_router_home(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    d = tmp_path / "worldmodel"
    d.mkdir()
    (d / "steps.jsonl").write_text(json.dumps(_step("s", "s:0", 0)) + "\n")
    (d / "tasks.jsonl").write_text(json.dumps(_task("s:0", "s", "val")) + "\n")
    ds = P.load()
    assert ds.ids("val") == ["s:0"]


# ---- metrics by hand ---------------------------------------------------------------------------

def _two_session_ds():
    steps = [_step("s1", "s1:0", 0, "read"), _step("s1", "s1:0", 1, "edit"),
             _step("s2", "s2:0", 0, "test"), _step("s2", "s2:0", 1, "bogus-class")]
    tasks = [_task("s1:0", "s1", "test"), _task("s2:0", "s2", "test", outcome="fail")]
    return P.build(steps, tasks)


def test_ce_and_perplexity_uniform_model():
    ds = _two_session_ds()
    ce, ppl = P.next_action_scores(Fixed(np.full(P.V, 1 / P.V)), "uniform", ds, "test")
    assert ce.value == pytest.approx(math.log(P.V))
    assert ppl.value == pytest.approx(P.V)
    assert (ce.n, ce.n_sessions, ce.quality) == (4, 2, "label-free")
    assert ce.ci[0] == pytest.approx(math.log(P.V)) and ce.ci[1] == pytest.approx(math.log(P.V))


def test_ce_unknown_class_scores_as_other_and_clips():
    ds = _two_session_ds()
    p = np.zeros(P.V)
    p[P.A_INDEX["other"]] = 1.0
    loss, _, _ = P.step_losses(Fixed(p), ds, ds.ids("test"))
    assert loss[-1] == pytest.approx(0.0)                     # "bogus-class" → other
    assert loss[0] == pytest.approx(-math.log(P.EPS))          # p = 0 → clipped, finite


def test_ece_by_hand():
    # bin 1: p .1, y {0,1} → |.1 − .5|·(2/4); bin 9: p .9, y {1,1} → |.9 − 1|·(2/4)
    assert P.ece([0.1, 0.1, 0.9, 0.9], [0, 1, 1, 1]) == pytest.approx(0.25)
    assert P.ece([1.0], [1]) == pytest.approx(0.0)                # p = 1 falls in the last bin
    assert P.ece([], []) is None


def test_outcome_brier_and_label_quality():
    ds = _two_session_ds()
    br, ec = P.outcome_scores(Fixed(np.full(P.V, 1 / P.V), ps=0.8), "m", ds, "test")
    assert br.value == pytest.approx(((0.8 - 1) ** 2 + 0.8 ** 2) / 2)
    assert (br.n, br.n_sessions, br.quality) == (2, 2, "gold")
    ds.tasks["s2:0"]["outcome_src"] = "weak"
    br, _ = P.outcome_scores(Fixed(np.full(P.V, 1 / P.V)), "m", ds, "test")
    assert br.quality == "provisional"
    br, _ = P.outcome_scores(Fixed(np.full(P.V, 1 / P.V)), "m", ds, "test", gold_only=True)
    assert br.quality == "gold" and br.n == 1
    for t in ds.tasks.values():
        t["outcome"] = "unknown"
    br, ec = P.outcome_scores(Fixed(np.full(P.V, 1 / P.V)), "m", ds, "test")
    assert br.value is None and br.quality == ec.quality == "inconclusive"


def test_outcome_prefix_never_reads_the_last_step():
    st = [_step("s", "s:0", i) for i in range(5)]
    assert len(P.prefix_of(st, 8)) == 4
    assert len(P.prefix_of(st, 2)) == 2
    assert len(P.prefix_of(st, None)) == 5
    assert P.prefix_of(st[:1], 8) == []


# ---- bootstrap ---------------------------------------------------------------------------------

def test_cluster_bootstrap_deterministic_and_seed_dependent():
    rng = np.random.default_rng(0)
    x = rng.normal(size=300)
    cl = [i // 10 for i in range(300)]
    a = P.weighted_mean_ci(x, cl, seed=7)
    assert a == P.weighted_mean_ci(x, cl, seed=7)
    assert a != P.weighted_mean_ci(x, cl, seed=8)
    assert a[1] < a[0] < a[2]
    g = P.cluster_bootstrap(lambda w: float(w @ x / w.sum()), cl, seed=7)
    assert g[0] == pytest.approx(a[0]) and g[1] == pytest.approx(a[1], abs=1e-9)


def test_cluster_bootstrap_resamples_sessions_not_items():
    # two sessions with constant values 0 and 1: every draw's mean is 0, 1/2 or 1 (by session),
    # never an item-level mixture like 0.3
    x = [0.0] * 5 + [1.0] * 5
    cl = ["a"] * 5 + ["b"] * 5
    _, lo, hi = P.weighted_mean_ci(x, cl, seed=1)
    assert lo in (0.0, 0.5) and hi in (0.5, 1.0)


def test_single_session_has_no_ci():
    assert P.weighted_mean_ci([1.0, 2.0], ["s", "s"]) == (1.5, None, None)
    assert P.cluster_bootstrap(lambda w: 1.0, ["s"]) == (1.0, None, None)


def test_paired_ce_of_a_model_with_itself_is_zero():
    ds, _ = fixtures.dataset(20, seed=1)
    m = Fixed(np.full(P.V, 1 / P.V))
    d = P.paired_ce(m, m, "m − m", ds, "test")
    assert d.value == pytest.approx(0.0) and d.extra["rel"] == pytest.approx(0.0)
    assert d.n == sum(len(ds.steps[t]) for t in ds.ids("test"))


def test_per_day_rows():
    ds, _ = fixtures.dataset(30, seed=2)
    rows = P.per_day(Fixed(np.full(P.V, 1 / P.V)), "u", ds, "test")
    assert rows and sum(r["n"] for r in rows) == sum(len(ds.steps[t]) for t in ds.ids("test"))
    assert all(r["ce"] == pytest.approx(math.log(P.V)) for r in rows)
    assert P.day_of(0) == "1970-01-01" and P.day_of(None) is None


# ---- fixtures + CLI ----------------------------------------------------------------------------

def _runs(syn, tid):
    return [s["tests"]["failed"] for s in syn.steps if s["task"] == tid and s["tests"]["ran"]]


def test_synthetic_is_deterministic_and_consistent():
    a, b = fixtures.synthetic(15, seed=9), fixtures.synthetic(15, seed=9)
    assert a.steps == b.steps and a.tasks == b.tasks
    assert fixtures.synthetic(15, seed=10).steps != a.steps
    for t in a.tasks:
        tr = a.truth["tasks"][t["task"]]
        if t["outcome"] != "unknown":
            assert t["outcome"] == tr["outcome"]
        runs = _runs(a, t["task"])
        if tr["kind"] == "converge" and tr["outcome"] == "success":
            assert runs[-1] == 0 and all(x >= y for x, y in zip(runs, runs[1:]))
        if tr["kind"] == "short" and runs:
            assert min(runs) >= tr["residual"] >= 1
        if tr["kind"] == "notest":
            assert runs == []
    for s in a.steps:
        assert s["act"] in P.ACTIONS and s["phase"] in P.PHASES
        if s["tests"]["ran"]:
            assert s["err"] == int(s["tests"]["failed"] > 0)
    with pytest.raises(ValueError):
        fixtures.synthetic(2, order=3)


@pytest.mark.parametrize("cmd", ["baseline", "chains", "progress"])
def test_cli_synthetic_text_and_json(cmd, capsys):
    from apex_router.worldmodel.readout import COMMANDS
    assert COMMANDS[cmd](["--synthetic", "30"]) == 0
    out = capsys.readouterr().out
    assert "synthetic: 30 sessions" in out and "split (by session start)" in out
    if cmd == "progress":
        assert "planted dynamics" in out and "pending owner sign-off" in out
    assert COMMANDS[cmd](["--synthetic", "30", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["source"]["splits"]["train"]["tasks"] > 0


def test_commands_table_and_no_top_level_dispatch():
    from apex_router.worldmodel import readout
    assert readout.COMMANDS == {"baseline": readout.cmd_baseline, "chains": readout.cmd_chains,
                                "progress": readout.cmd_progress}
    assert readout.main(["bogus"]) == 2


def test_outcome_bar_is_chosen_on_val(monkeypatch):
    """The best-outcome model must be picked on val Brier, never test."""
    from apex_router.worldmodel import readout
    ds, desc = fixtures.dataset(40, seed=1)[0], "synthetic: 40"
    real = P.outcome_scores
    calls = []

    def spy(model, name, ds_, split, **kw):
        calls.append(split)
        out = real(model, name, ds_, split, **kw)
        if split == "test":                          # make test prefer the prior, val not
            out[0].value = 0.0 if name.startswith("prior") else 0.9
        return out

    monkeypatch.setattr(P, "outcome_scores", spy)
    rep = readout.baseline_report(ds, desc)
    vals = {r["model"]: r["brier_val"]["value"] for r in rep["rows"]}
    assert rep["best_outcome"] == min(vals, key=vals.get)
    assert "val" in calls


def test_cli_no_data_is_an_error(tmp_path, monkeypatch, capsys):
    from apex_router.worldmodel.readout import cmd_baseline
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    assert cmd_baseline([]) == 1
    assert "no train/test tasks" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["numpy", "scipy.optimize"])
def test_missing_optional_dependency_is_a_message(missing, monkeypatch, capsys):
    import sys
    from apex_router.worldmodel import readout
    if missing == "numpy":
        # simulate a fresh interpreter without numpy: the protocol import is the first to fail
        for m in [m for m in sys.modules if m.startswith("apex_router.worldmodel.protocol")]:
            monkeypatch.delitem(sys.modules, m)
        monkeypatch.setitem(sys.modules, "numpy", None)
    else:
        monkeypatch.setitem(sys.modules, "scipy.optimize", None)
    assert readout.cmd_baseline(["--synthetic", "20"]) == 1
    err = capsys.readouterr().err
    assert "needs numpy and scipy" in err and missing in err


def test_cli_progress_task_trace(capsys):
    from apex_router.worldmodel.readout import cmd_progress
    syn = fixtures.synthetic(30)
    tid = next(t for t, v in syn.truth["tasks"].items() if v["kind"] == "short")
    assert cmd_progress(["--synthetic", "30", "--task", tid]) == 0
    out = capsys.readouterr().out
    assert f"task {tid}" in out and "failing tests per run" in out
