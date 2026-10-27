"""Unit tests for apex_router.explore (fake backend, fake oracle; no network, no model)."""
from __future__ import annotations

import dataclasses
import json
import sys
import textwrap
from pathlib import Path

import pytest

from apex_router.explore import metrics, operators as ops, select as sel
from apex_router.explore.archive import Archive, Candidate, OracleResult, fingerprint
from apex_router.explore.evidence import EvidenceStore
from apex_router.explore.oracle import assert_isolated, run_oracle
from apex_router.explore.run import Task, cheap_check, run_task
from apex_router.explore.scheduler import n_explore, plan_round


# --------------------------------------------------------------------------- fakes
class FakeBackend:
    """Returns a valid python candidate with a unique marker per call; records each prompt."""
    model = "fake"

    def __init__(self, fail_every: int = 0):
        self.prompts: list[str] = []
        self.fail_every = fail_every

    def __call__(self, prompt: str, seed: int) -> str:
        self.prompts.append(prompt)
        i = len(self.prompts)
        if self.fail_every and i % self.fail_every == 0:
            raise ConnectionError("fake down")
        op = next((o for o in ops.OPERATORS if f"Operator: {o}." in prompt), "plain")
        k = 1 + (i % 4)
        return (f"MECHANISM: {op}, step-{k}\nCANDIDATE:\n```python\n"
                f"# MARKER_{i:03d}_SECRET\ndef solve(x):\n    return x * {k}\n```\n")


def fake_oracle(text: str) -> OracleResult:
    # score = the multiplier; valid when it is not 2 (a "counterexample" mechanism)
    k = int(text.strip().rsplit("*", 1)[1])
    if k == 2:
        return OracleResult(False, 0.0, "counterexample: x=3 gives 6, want 9")
    return OracleResult(True, float(k), "ok")


def _task(tmp_path: Path, tau: float = 4.0) -> Task:
    root = tmp_path / "task"
    (root / "oracle").mkdir(parents=True)
    (root / "goal.md").write_text("Write solve(x).\n")
    (root / "task.json").write_text(json.dumps({"name": "t", "tau": tau, "entry": "solve"}))
    return Task.load(root)


# --------------------------------------------------------------------------- archive
def test_fingerprint_uses_tags_not_wording():
    assert fingerprint(["Binary Search", "DP"]) == fingerprint(["dp", "binary_search "]) == "binary-search+dp"
    a = Candidate.new("inversion", "text one, long wording", ["Binary Search", "DP"])
    b = Candidate.new("inversion", "completely other words", ["dp", "binary-search"])
    assert a.id != b.id and a.niche == b.niche
    c = Candidate.new("inversion", "text one, long wording", ["hashing"])
    assert c.niche != a.niche
    assert fingerprint([]) == "untagged"


def test_candidate_is_immutable_and_results_once():
    c = Candidate.new("simplification", "def solve(x): return x", ["identity"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.text = "changed"  # type: ignore[misc]
    assert isinstance(c.tags, tuple) and isinstance(c.parents, tuple)
    ar = Archive()
    ar.add(c)
    assert ar.add(c) is False  # same content, same id: no-op
    ar.record(c.id, OracleResult(True, 1.0))
    with pytest.raises(ValueError):
        ar.record(c.id, OracleResult(True, 9.0))
    assert ar.candidates[c.id] is c and ar.results[c.id].score == 1.0


def test_child_has_parent_lineage_and_parent_unchanged():
    ar = Archive()
    p = Candidate.new("inversion", "def solve(x): return x", ["a"])
    ar.add(p)
    with pytest.raises(KeyError):
        ar.add(Candidate.new("inversion", "x", ["b"], parents=["nope"]))
    ch = Candidate.new("inversion", "def solve(x): return 2*x", ["a", "b"], parents=[p.id])
    gc = Candidate.new("inversion", "def solve(x): return 3*x", ["c"], parents=[ch.id])
    ar.add(ch)
    ar.add(gc)
    assert ar.lineage(gc.id) == [ch.id, p.id]
    assert ar.candidates[p.id].text == "def solve(x): return x"


def test_one_champion_per_niche_by_oracle():
    ar = Archive()
    a = Candidate.new("adversarial", "A", ["m"])
    b = Candidate.new("adversarial", "B", ["M"])
    ar.add(a)
    ar.add(b)
    assert ar.niches() == [("adversarial", "m")]
    ar.record(a.id, OracleResult(True, 1.0))
    ar.record(b.id, OracleResult(True, 3.0))
    assert ar.champion(("adversarial", "m")) is b
    c = Candidate.new("adversarial", "C", ["m"])
    ar.add(c)  # untested never replaces a valid champion
    ar.record(c.id, OracleResult(False, 99.0))
    assert ar.champion(("adversarial", "m")) is b


# --------------------------------------------------------------------------- evidence
def test_evidence_append_only_and_negative_without_text(tmp_path):
    ev = EvidenceStore(tmp_path / "ev.jsonl")
    ev.append("candidate", id="c1", tags=["x"], text="SECRET_TEXT")
    before = (tmp_path / "ev.jsonl").read_bytes()
    ev.append("oracle_reject", id="c1", tags=["x"], fingerprint="x", detail="counterexample: n=0")
    ev.append("check_reject", id="c2", tags=["y"], fingerprint="y", detail="syntax error")
    after = (tmp_path / "ev.jsonl").read_bytes()
    assert after.startswith(before) and len(after.splitlines()) == 3
    neg = ev.negative()
    assert len(neg) == 2 and "counterexample: n=0" in neg[1] and "syntax error" in neg[0]
    assert all("SECRET_TEXT" not in x for x in neg)
    assert ev.disproven_fingerprints() == {"x"}
    # a second store on the same file keeps old rows
    assert len(EvidenceStore(tmp_path / "ev.jsonl").rows()) == 3


# --------------------------------------------------------------------------- operators
def test_six_operators_and_diverge_has_no_candidate_parameter():
    assert list(ops.OPERATORS) == ["first-principles", "inversion", "cross-domain",
                                   "constraint-mutation", "adversarial", "simplification"]
    import inspect
    params = set(inspect.signature(ops.diverge_prompt).parameters)
    assert params == {"operator", "goal", "constraints", "facts", "negative"}
    p = ops.diverge_prompt("inversion", "GOAL", ["C1"], ["F1"], ["N1"])
    for s in ("GOAL", "C1", "F1", "N1", "Operator: inversion", "MECHANISM:"):
        assert s in p
    with pytest.raises(KeyError):
        ops.diverge_prompt("vote", "g")


def test_parse_reply():
    tags, text = ops.parse_reply("<think>x</think>**MECHANISM:** Hash Map; two pointers\nCANDIDATE:\n"
                                 "```python\ndef solve(a):\n    return 1\n```\nmore")
    assert tags == ["Hash Map", "two pointers"] and text == "def solve(a):\n    return 1"
    tags, text = ops.parse_reply("no format at all")
    assert tags == [] and text == "no format at all"


def test_isolation_diverge_prompts_never_hold_other_candidates(tmp_path):
    task = _task(tmp_path, tau=99)  # no hit: run all rounds
    fb = FakeBackend()
    rep = run_task(task, "explore", 18, fb, tmp_path / "run", oracle=fake_oracle, round_size=6)
    assert rep["calls"] == 18
    ev = EvidenceStore(tmp_path / "run" / "evidence.jsonl")
    texts = [r["text"] for r in ev.rows("candidate")]
    assert texts
    diverge = [p for p in fb.prompts if p.startswith("You are one independent explorer")]
    evolve = [p for p in fb.prompts if not p.startswith("You are one independent explorer")]
    assert diverge and evolve
    for p in diverge:
        assert "MARKER_" not in p and "def solve" not in p
        for t in texts:
            assert t not in p
    # negative evidence (the k=2 counterexample) reaches later prompts, as tags + detail only
    assert any("counterexample: x=3" in p for p in fb.prompts[6:])


# --------------------------------------------------------------------------- select
def test_pareto_keeps_a_minority_against_a_majority():
    ar = Archive()
    majority = [Candidate.new("simplification", f"maj {i}", ["loop", "cache"]) for i in range(9)]
    minority = Candidate.new("cross-domain", "min", ["fft"])
    for c in majority + [minority]:
        ar.add(c)
        ar.record(c.id, OracleResult(True, 5.0 if c is not minority else 1.0))
    front = sel.front(ar, list(ar.candidates.values()))
    assert minority.id in {s.cand.id for s in front}
    # a cut to k=1 still keeps the whole first front
    kept = sel.select(ar, list(ar.candidates.values()), k=1)
    assert minority.id in {s.cand.id for s in kept}


def test_gate_removes_only_oracle_invalid():
    ar = Archive()
    a, b, c = (Candidate.new("inversion", t, [t]) for t in ("a", "b", "c"))
    for x in (a, b, c):
        ar.add(x)
    ar.record(a.id, OracleResult(False, 0.0, "bad"))
    ar.record(b.id, OracleResult(True, 0.1))
    assert {x.id for x in sel.gate(ar, [a, b, c])} == {b.id, c.id}
    assert a.id not in {s.cand.id for s in sel.select(ar, [a, b, c])}


# --------------------------------------------------------------------------- scheduler
def test_floor_reserves_diverge_slots_for_untested_niches():
    assert n_explore(6, 0.3) == 2 and n_explore(10, 0.3) == 3 and n_explore(6, 0.0) == 0
    ar = Archive()
    assert all(s.kind == "diverge" for s in plan_round(ar, 6))  # empty archive: all explore
    assert len({s.operator for s in plan_round(ar, 6)}) == 6   # each empty island once
    for op, sc in (("inversion", 2.0), ("adversarial", 1.0)):
        c = Candidate.new(op, op, [op])
        ar.add(c)
        ar.record(c.id, OracleResult(True, sc))
    slots = plan_round(ar, 6, floor=0.3)
    div = [s for s in slots if s.kind == "diverge"]
    assert len(div) == 2
    assert {s.operator for s in div}.isdisjoint({"inversion", "adversarial"})  # empty islands first
    assert all(s.parents for s in slots if s.kind != "diverge")
    assert any(s.kind == "combine" for s in slots)
    assert len([s for s in plan_round(ar, 6, floor=1.0) if s.kind == "diverge"]) == 6
    with pytest.raises(ValueError):
        plan_round(ar, 6, floor=1.5)


# --------------------------------------------------------------------------- metrics
def test_metrics():
    lo, hi = metrics.wilson(0, 10)
    assert lo == 0.0 and abs(hi - 0.2775) < 1e-3
    lo, hi = metrics.wilson(5, 10)
    assert abs(lo - 0.2366) < 1e-3 and abs(hi - 0.7634) < 1e-3
    assert metrics.first_hit([None, 1.0, 5.0, 9.0], 4.0) == 3
    assert metrics.p99([1.0] * 99) == "n too small"
    assert metrics.p99(list(range(100))) == 98
    hr = metrics.hit_rate([{"best_score": 5, "tau": 4}, {"best_score": None, "tau": 4}])
    assert hr["k"] == 1 and hr["n"] == 2


# --------------------------------------------------------------------------- oracle + run
def test_oracle_fails_closed(tmp_path):
    ok = [sys.executable, "-c", "import sys,json; sys.stdin.read(); print(json.dumps({'valid': True, 'score': 2.5}))"]
    assert run_oracle(ok, "x") == OracleResult(True, 2.5, "")
    for code in ("print('not json')", "import sys; sys.exit(3)", "print('{\"valid\": \"yes\", \"score\": 1}')",
                 "print('{\"valid\": true, \"score\": NaN}')"):
        r = run_oracle([sys.executable, "-c", code], "x")
        assert r.valid is False and r.score == 0.0
    assert run_oracle([str(tmp_path / "missing")], "x").valid is False


def test_oracle_dir_must_be_outside_generator_dir(tmp_path):
    (tmp_path / "gen" / "oracle").mkdir(parents=True)
    with pytest.raises(ValueError):
        assert_isolated(tmp_path / "gen" / "oracle", tmp_path / "gen")
    with pytest.raises(ValueError):
        assert_isolated(tmp_path, tmp_path / "gen")
    assert_isolated(tmp_path / "o", tmp_path / "gen")


def test_cheap_check():
    t = Task(name="t", root=Path("."), goal="", tau=1, entry="solve")
    seen: set[str] = set()
    assert cheap_check(t, "def solve(x):\n    return x\n", seen) is None
    assert "syntax" in cheap_check(t, "def solve(x)\n", seen)
    assert "no function" in cheap_check(t, "def other(x):\n    return x\n", seen)
    assert cheap_check(t, "", seen) == "empty candidate"
    seen.add("def solve(x): return x")
    assert "duplicate" in cheap_check(t, "def solve(x):   return x", seen)


def test_baseline_equal_budget_and_failed_calls_count(tmp_path):
    task = _task(tmp_path, tau=4.0)
    fb = FakeBackend(fail_every=3)
    rep = run_task(task, "baseline", 7, fb, tmp_path / "b", oracle=fake_oracle)
    assert rep["calls"] == 7 == len(fb.prompts) and rep["call_errors"] == 2
    assert len(set(fb.prompts)) == 1  # one plain prompt
    assert "Operator:" not in fb.prompts[0]
    assert rep["best_score"] == 4.0 and rep["hit"] is True and rep["first_hit_call"] == 7
    rep2 = run_task(task, "explore", 7, FakeBackend(), tmp_path / "e", oracle=fake_oracle)
    assert rep2["calls"] == 7 and rep2["niches"] >= 1 and rep2["best_score"] == 4.0


def test_deadline_truncates(tmp_path):
    task = _task(tmp_path)
    rep = run_task(task, "explore", 12, FakeBackend(), tmp_path / "r", oracle=fake_oracle, deadline=0.0)
    assert rep["calls"] == 0 and rep["truncated"] is True


def test_cli_run_with_cmd_backend_and_task_oracle(tmp_path, capsys):
    from apex_router.cli import main
    task = tmp_path / "task"
    (task / "oracle").mkdir(parents=True)
    (task / "goal.md").write_text("Write solve(x).\n")
    (task / "oracle" / "check.py").write_text(textwrap.dedent("""
        import json, sys
        src = sys.stdin.read()
        ok = "def solve" in src
        print(json.dumps({"valid": ok, "score": 3.0 if ok else 0.0, "detail": "fake"}))
    """))
    (task / "task.json").write_text(json.dumps({
        "name": "clitask", "tau": 2.0, "entry": "solve",
        "oracle": {"dir": "oracle", "cmd": ["{python}", "check.py"]}}))
    agent = tmp_path / "agent.py"
    agent.write_text("import sys, os\nsys.stdin.read()\nassert os.listdir('.') == ['goal.md'], os.listdir('.')\n"
                     "print('MECHANISM: plain\\nCANDIDATE:\\n```\\ndef solve(x):\\n    return x  # '"
                     " + os.environ['EXPLORE_SEED'] + '\\n```')\n")
    out = tmp_path / "res.jsonl"
    rc = main(["explore", "run", "--task", str(task), "--arm", "baseline", "--budget", "2",
               "--backend", "cmd", "--cmd", f"{sys.executable} {agent}", "--run-dir", str(tmp_path / "rd"),
               "--out", str(out)])
    assert rc == 0
    rep = json.loads(out.read_text().splitlines()[0])
    assert rep["calls"] == 2 and rep["call_errors"] == 0 and rep["best_score"] == 3.0 and rep["hit"]
    capsys.readouterr()
    assert main(["explore", "table", str(out)]) == 0
    md = capsys.readouterr().out
    assert "| clitask | baseline | 2 |" in md and "HitRate_tau@B" in md


def test_one_cmd_backend_reused_across_runs_gets_a_fresh_dir_each_time(tmp_path):
    from apex_router.explore.backends import CmdBackend
    task = _task(tmp_path)
    agent = tmp_path / "agent.py"
    agent.write_text("import sys, os\nsys.stdin.read()\nassert os.listdir('.') == ['goal.md']\n"
                     "print('MECHANISM: m\\nCANDIDATE:\\n```\\ndef solve(x):\\n    return x * 5  # '"
                     " + os.environ['EXPLORE_SEED'] + '\\n```')\n")
    be = CmdBackend(f"{sys.executable} {agent}")
    for i, arm in enumerate(("explore", "baseline", "explore")):
        rep = run_task(task, arm, 2, be, tmp_path / f"r{i}", seed=i,
                       oracle=lambda t: OracleResult(True, 5.0, "ok"))
        assert rep["call_errors"] == 0 and rep["best_score"] == 5.0, (arm, rep)
    assert be.cwd is None
