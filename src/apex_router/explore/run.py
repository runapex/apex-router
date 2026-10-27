"""The `apex-router explore` loop and the `baseline` arm.

explore: diverge -> cheap check -> archive -> select for test -> oracle -> evidence -> evolve -> report.
baseline: B independent samples of one plain prompt; best oracle score wins. Same call budget B.

Budget B counts model calls. A failed call counts. The oracle runs on every candidate that passes the
cheap check in both arms (unless `oracle_per_round` limits the explore arm), so the arms differ only in
how the calls are spent.

The generator works in its own directory (`workdir`). It holds only a copy of goal.md. The oracle
directory must be outside it (checked). The harness never adopts a candidate; it reports.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import metrics
from . import operators as ops
from . import select as sel
from .archive import Archive, Candidate, OracleResult
from .evidence import EvidenceStore
from .oracle import assert_isolated, run_oracle
from .scheduler import DEFAULT_FLOOR, Slot, plan_round

Backend = Callable[[str, int], str]
OracleFn = Callable[[str], OracleResult]
MAX_CANDIDATE_CHARS = 20000


@dataclass
class Task:
    name: str
    root: Path
    goal: str
    tau: float
    entry: str = ""
    lang: str = "python"
    constraints: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    oracle_dir: Path | None = None
    oracle_cmd: list[str] = field(default_factory=list)
    oracle_timeout: float = 300.0

    @staticmethod
    def load(root: str | Path) -> "Task":
        root = Path(root).resolve()
        spec = json.loads((root / "task.json").read_text())
        o = spec.get("oracle") or {}
        odir = (root / o.get("dir", "oracle")).resolve()
        return Task(name=spec.get("name", root.name), root=root, goal=(root / "goal.md").read_text(),
                    tau=float(spec["tau"]), entry=spec.get("entry", ""), lang=spec.get("lang", "python"),
                    constraints=list(spec.get("constraints", [])), facts=list(spec.get("facts", [])),
                    oracle_dir=odir, oracle_cmd=list(o.get("cmd", [])),
                    oracle_timeout=float(o.get("timeout", 300)))

    def oracle_fn(self) -> OracleFn:
        cmd = [c.replace("{python}", sys.executable) for c in self.oracle_cmd]

        def _run(text: str) -> OracleResult:
            return run_oracle(cmd, text, cwd=self.oracle_dir, timeout=self.oracle_timeout)
        return _run


def prepare_workdir(task: Task, base: Path | None = None) -> Path:
    """A fresh generator directory with goal.md only. Raises if it overlaps the oracle dir."""
    wd = Path(tempfile.mkdtemp(prefix=f"explore-gen-{task.name}-", dir=base))
    shutil.copy(task.root / "goal.md", wd / "goal.md")
    if task.oracle_dir is not None:
        assert_isolated(task.oracle_dir, wd)
    return wd


def cheap_check(task: Task, text: str, seen: set[str]) -> str | None:
    """No execution. -> None if OK, else the reject reason."""
    if not text.strip():
        return "empty candidate"
    if len(text) > MAX_CANDIDATE_CHARS:
        return f"candidate longer than {MAX_CANDIDATE_CHARS} chars"
    norm = re.sub(r"\s+", " ", text).strip()
    if norm in seen:
        return "duplicate of an earlier candidate"
    if task.lang == "python":
        try:
            compile(text, "<candidate>", "exec")
        except (SyntaxError, ValueError) as e:
            return f"syntax error: {type(e).__name__} line {getattr(e, 'lineno', '?')}"
        if task.entry and not re.search(rf"^\s*def\s+{re.escape(task.entry)}\s*\(", text, re.M):
            return f"no function {task.entry}()"
    return None


class _Run:
    """Shared state of one arm run on one task."""

    def __init__(self, task: Task, arm: str, budget: int, backend: Backend, oracle: OracleFn,
                 run_dir: Path, seed: int, deadline: float | None):
        self.task, self.arm, self.budget = task, arm, budget
        self.backend, self.oracle, self.seed, self.deadline = backend, oracle, seed, deadline
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.archive = Archive()
        self.evidence = EvidenceStore(self.run_dir / "evidence.jsonl")
        self.calls = 0
        self.call_cands: list[str | None] = []   # call index -> candidate id (or None)
        self.seen: set[str] = set()
        self.check_rejects = 0
        self.call_errors = 0
        self.oracle_calls = 0
        self.truncated = False
        self.t0 = time.time()

    def out_of_time(self) -> bool:
        if self.deadline is not None and time.time() >= self.deadline:
            self.truncated = True
            return True
        return False

    def generate(self, prompt: str, operator: str, parents: tuple[str, ...], round_no: int) -> Candidate | None:
        self.calls += 1
        call_seed = self.seed * 100003 + self.calls
        try:
            reply = self.backend(prompt, call_seed)
        except Exception as e:  # transport/backend error: the call counts, no candidate
            self.call_errors += 1
            self.call_cands.append(None)
            self.evidence.append("call_error", arm=self.arm, call=self.calls, operator=operator,
                                 detail=type(e).__name__)
            return None
        tags, text = ops.parse_reply(reply)
        cand = Candidate.new(operator, text, tags, parents, round=round_no, arm=self.arm,
                             salt=f"{self.arm}:{self.calls}")
        why = cheap_check(self.task, text, self.seen)
        self.seen.add(re.sub(r"\s+", " ", text).strip())
        if why is not None:
            self.check_rejects += 1
            self.call_cands.append(None)
            # shape only (no text): reply length, fence count, MECHANISM line seen -> diagnose a format miss
            self.evidence.append("check_reject", arm=self.arm, call=self.calls, id=cand.id,
                                 operator=operator, tags=list(cand.tags), fingerprint=cand.fingerprint,
                                 detail=why, reply_chars=len(reply or ""),
                                 reply_fences=(reply or "").count("```"),
                                 reply_has_mechanism=bool(re.search(r"MECHANISM", reply or "", re.I)))
            return None
        self.archive.add(cand)
        self.call_cands.append(cand.id)
        self.evidence.append("candidate", arm=self.arm, call=self.calls, id=cand.id, operator=operator,
                             tags=list(cand.tags), fingerprint=cand.fingerprint, parents=list(parents),
                             round=round_no, text=cand.text)
        return cand

    def test(self, cand: Candidate) -> OracleResult:
        self.oracle_calls += 1
        res = self.oracle(cand.text)
        self.archive.record(cand.id, res)
        kind = "oracle_pass" if res.valid else "oracle_reject"
        self.evidence.append(kind, arm=self.arm, id=cand.id, operator=cand.operator, tags=list(cand.tags),
                             fingerprint=cand.fingerprint, valid=res.valid, score=res.score, detail=res.detail)
        return res

    def report(self) -> dict:
        tau = self.task.tau
        scores = []
        for cid in self.call_cands:
            r = self.archive.results.get(cid) if cid else None
            scores.append(r.score if (r and r.valid) else None)
        best = self.archive.best()
        valid_scores = [r.score for r in self.archive.results.values() if r.valid]
        rep = {
            "task": self.task.name, "arm": self.arm, "budget": self.budget, "calls": self.calls,
            "call_errors": self.call_errors, "check_rejects": self.check_rejects,
            "oracle_calls": self.oracle_calls, "tau": tau,
            "best_score": best[1].score if best else None, "best_id": best[0].id if best else None,
            "hit": metrics.hit(best[1].score if best else None, tau),
            "first_hit_call": metrics.first_hit(scores, tau),
            "niches": len(self.archive.niches()),
            "valid": sum(1 for r in self.archive.results.values() if r.valid),
            "valid_scores": valid_scores,
            "seed": self.seed, "truncated": self.truncated,
            "elapsed_s": round(time.time() - self.t0, 1),
        }
        (self.run_dir / "report.json").write_text(json.dumps(rep, indent=2) + "\n")
        if best:
            (self.run_dir / "best.txt").write_text(best[0].text + "\n")
        return rep


def run_baseline(task: Task, budget: int, backend: Backend, oracle: OracleFn, run_dir: Path,
                 seed: int = 0, deadline: float | None = None) -> dict:
    r = _Run(task, "baseline", budget, backend, oracle, run_dir, seed, deadline)
    prompt = ops.baseline_prompt(task.goal, task.constraints, task.facts)
    while r.calls < budget and not r.out_of_time():
        cand = r.generate(prompt, "baseline", (), round_no=r.calls)
        if cand is not None:
            r.test(cand)
    return r.report()


def _slot_prompt(r: _Run, slot: Slot, negative: list[str]) -> str:
    t = r.task
    if slot.kind == "diverge":
        return ops.diverge_prompt(slot.operator, t.goal, t.constraints, t.facts, negative)
    parents = []
    for pid in slot.parents:
        c = r.archive.candidates[pid]
        res = r.archive.results.get(pid)
        detail = f"valid, score {res.score:.4g}. {res.detail}" if res else "untested"
        parents.append((c.text, ops.tags_text(c.tags), detail))
    return ops.evolve_prompt(slot.kind, t.goal, parents, t.constraints, negative)


def run_explore(task: Task, budget: int, backend: Backend, oracle: OracleFn, run_dir: Path,
                seed: int = 0, floor: float = DEFAULT_FLOOR, round_size: int = 6,
                oracle_per_round: int | None = None, deadline: float | None = None) -> dict:
    r = _Run(task, "explore", budget, backend, oracle, run_dir, seed, deadline)
    round_no = 0
    while r.calls < budget and not r.out_of_time():
        n = min(round_size, budget - r.calls)
        slots = plan_round(r.archive, n, floor=floor, round_no=round_no)
        r.evidence.append("round", arm=r.arm, round=round_no,
                          slots=[{"kind": s.kind, "operator": s.operator, "parents": list(s.parents)}
                                 for s in slots])
        negative = r.evidence.negative()
        new: list[Candidate] = []
        for slot in slots:
            if r.out_of_time():
                break
            c = r.generate(_slot_prompt(r, slot, negative), slot.operator, slot.parents, round_no)
            if c is not None:
                new.append(c)
        # select for test: gate + Pareto order; None = test every new candidate
        chosen = sel.select(r.archive, new, k=oracle_per_round)
        for s in chosen:
            r.test(s.cand)
        round_no += 1
    rep = r.report()
    rep["rounds"] = round_no
    rep["floor"] = floor
    (r.run_dir / "report.json").write_text(json.dumps(rep, indent=2) + "\n")
    return rep


def run_task(task: Task, arm: str, budget: int, backend: Backend, run_dir: Path, seed: int = 0,
             oracle: OracleFn | None = None, deadline: float | None = None, **kw) -> dict:
    """Run one arm on one task in a fresh generator workdir. The oracle dir must be outside it."""
    wd = prepare_workdir(task)  # raises if the oracle dir overlaps the generator dir
    set_cwd = hasattr(backend, "cwd") and getattr(backend, "cwd") is None
    try:
        orc = oracle or task.oracle_fn()
        if set_cwd:
            backend.cwd = str(wd)  # a cmd agent starts in this run's generator dir (goal.md only)
        if arm == "baseline":
            rep = run_baseline(task, budget, backend, orc, run_dir, seed=seed, deadline=deadline)
        elif arm == "explore":
            rep = run_explore(task, budget, backend, orc, run_dir, seed=seed, deadline=deadline, **kw)
        else:
            raise ValueError(f"unknown arm {arm!r}")
    finally:
        if set_cwd:
            backend.cwd = None  # the next run gets its own fresh dir
        shutil.rmtree(wd, ignore_errors=True)
    rep["model"] = getattr(backend, "model", getattr(backend, "name", "custom"))
    return rep


def report_markdown(rows: list[dict]) -> str:
    head = ("| task | arm | B | calls | best score | tau | hit | first hit (call) | niches | valid | "
            "check rejects | call errors | time s |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    body = ""
    for x in rows:
        bs = "-" if x.get("best_score") is None else f"{x['best_score']:.4g}"
        body += (f"| {x['task']} | {x['arm']} | {x['budget']} | {x['calls']} | {bs} | {x['tau']:g} | "
                 f"{'yes' if x['hit'] else 'no'} | {x.get('first_hit_call') or '-'} | {x['niches']} | "
                 f"{x['valid']} | {x['check_rejects']} | {x['call_errors']} | {x['elapsed_s']}"
                 f"{' (truncated)' if x.get('truncated') else ''} |\n")
    summ = metrics.summary(rows)
    tail = "\n| arm | HitRate_tau@B (k/n) | 95% Wilson CI | p99 score |\n|---|---|---|---|\n"
    for arm, s in summ.items():
        h = s["hit_rate"]
        tail += (f"| {arm} | {h['k']}/{h['n']} | [{h['ci'][0]:.2f}, {h['ci'][1]:.2f}] | "
                 f"{s['p99_score']} |\n")
    return head + body + tail
