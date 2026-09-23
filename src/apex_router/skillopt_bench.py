"""Skill-quality benchmark — does a skill document raise a frozen model's answer quality,
proven over time behind apex's promotion gate?

WHAT THIS BORROWS FROM SkillOpt (microsoft/SkillOpt), AND WHAT IT CHANGES
------------------------------------------------------------------------
SkillOpt's genuine contribution is the one axis apex-router structurally lacks: treat a
compact skill document as the *trainable state of a frozen model* and measure whether it
lifts task success (apex is measure-only on cost/cache/routing — it can pick a cheaper
model but never make an answer better). We borrow that MECHANISM — a skill doc as a
trainable artifact, scored by paired A/B rollouts on a fixed task set.

We deliberately DO NOT borrow SkillOpt's acceptance gate. SkillOpt accepts an edit on a
POINT ESTIMATE (`cand_score > current_score`, `skillopt/evaluation/gate.py`), with an
optional `semantic_density` bonus that rewards MUST/ALWAYS/NEVER keyword frequency — a
Goodhart surface. SkillOpt's own rigorous instrument (`skillopt_sleep/evalkit.py`:
McNemar + bootstrap CI) is reporting-only by its own docstring ("It does not change the
nightly gate"). apex already has the stronger gate SkillOpt declined to wire in:
`gate.run_gate` — sample floor + OUT-OF-SAMPLE confirmation split + Benjamini-Hochberg
FDR across the tested family + REPLICATION across >= M capture windows. So the borrow is:
SkillOpt's quality axis, put behind apex's gate, with a paired bootstrap CI report
(apex's `stats.paired_bootstrap_ci`, the evalkit analog apex already ships).

THE REFRAME (why apex's model-vs-model gate works unchanged for skills)
----------------------------------------------------------------------
apex's gate pairs "candidate MODEL vs incumbent MODEL on the SAME step". For skills it
becomes "candidate SKILL vs no-skill BASELINE, SAME frozen model, SAME step":
    delta_step = score(model WITH skill) - score(model WITHOUT skill)
We encode the two arms as pseudo-model ids: `skill:baseline` (incumbent, no skill) and
`skill:<name>` (candidate). The step CONTEXT is identical for both arms (fair test); the
ONLY difference is the injected skill, added per-arm in the replay function. This feeds
`run_bench` -> `cell_evidence_from_rows` -> `run_gate` with zero gate changes.

"HOW WELL IT WORKS OVER TIME" (the request)
-------------------------------------------
Each run is a capture WINDOW (`window_id`, defaults to the run date). A skill is only
declared a real quality lift when it clears apex's gate, which REQUIRES confirmation
across >= M distinct windows — i.e. it must keep winning on fresh tasks across days, not
once. Successive runs accumulate windows in a ledger; the verdict strengthens over time.
This is the same doctrine as the rest of apex: the harness is the product; re-run it.

MEASURE-ONLY (the safety boundary)
----------------------------------
This harness REPORTS whether a skill lifts quality. It does NOT deploy the skill into live
traffic. apex's proxy is measure-only, and autonomous skill deployment is exactly the
`DESIGN-whitepaper-research-loop.md` self-evolution loop that is BLOCKED pending an
evidence verifier + capability manifest + deploy supervisor. A human reads the verdict and
decides.

HONESTY SCOPE OF THE CLAIMS (hardened after a Codex/astra adversarial pass)
--------------------------------------------------------------------------
* IMMUTABLE EXPERIMENT IDENTITY (astra F5): a campaign is bound to (skill BYTES, model id).
  The corpus_snapshot hashes the skill text + model id, so combining a *different* skill
  revision or a *different* model under the same name won't pair as one campaign. Changed
  skill/model => different snapshot => the gate won't merge the evidence (apex doctrine:
  "a changed baseline voids the certificate").
* REAL REPLICATION, NOT PSEUDOREPLICATION (astra F4): each window must be a DISTINCT set of
  tasks (fresh evidence captured over time), NOT the same task file re-run with a new label.
  step_id is the raw task id (NOT window-qualified), so the base bench's own duplicate guard
  REJECTS re-running the same task across windows — replication can only come from new tasks.
* PAIRED WINDOWS ONLY (astra F3): a window counts toward replication only if the task has
  BOTH arms present (a real paired delta); a candidate-only window (baseline row dropped)
  contributes nothing.
* ONE SKILL = ONE HYPOTHESIS (astra F6): grading a single skill is one predeclared test
  (BH over a family of one is just the raw threshold). Testing MANY skills is a multiple-
  comparison campaign — use `grade_skills_family`, which runs apex's gate over ALL skill
  cells together so BH-FDR is real. A single `grade_skill` does NOT claim campaign-wide FDR.
* GRADING IS OBJECTIVE, NOT ADVERSARY-PROOF (astra F2/F7): scoring runs the caller's
  executable tests (`run_python_tests`) — no LLM judge, no vibes. But that runner's verdict
  is a subprocess EXIT CODE and is NOT an OS sandbox: hostile generated code (`os._exit(0)`,
  a `compile` override) can forge a pass. This is fine for measuring a NON-hostile skill's
  effect on a trusted model; it is NOT a security boundary against adversarial code. The
  hash split (`_split_for`) is a fixed holdout for a FIXED benchmark, not protection against
  a human adaptively over-fitting a skill to the confirmation tasks.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import bench, stats
from .gate import run_gate

BASELINE_ID = "skill:baseline"  # the no-skill arm (gate incumbent)

# System-prompt frame the skill is injected under. Kept minimal + fixed so the ONLY
# variable between arms is the skill text itself (fair paired comparison).
_SKILL_FRAME = (
    "You are a precise Python engineer. Apply the following skill guidance to the task.\n"
    "--- SKILL ---\n{skill}\n--- END SKILL ---"
)


@dataclass(frozen=True)
class SkillTask:
    """One benchmark task: a spec to satisfy and the executable tests that grade it."""
    id: str
    spec: str
    tests: str


@dataclass(frozen=True)
class SkillVerdict:
    """The report for one skill on one benchmark run."""
    skill_id: str
    n_tasks: int
    baseline_pass_rate: float
    skill_pass_rate: float
    mean_delta: float
    ci_low: float
    ci_high: float
    promoted: bool          # cleared apex's FULL gate (floor + OOS + FDR + replication)
    windows_seen: int
    reason: str


def load_tasks(path: str | Path) -> list[SkillTask]:
    """Load a skill benchmark JSONL ({id, spec, tests} per line). Reuses the codegen
    benchmark format so `benchmarks/codegen_probe.jsonl` works as a task set."""
    tasks: list[SkillTask] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            tasks.append(SkillTask(id=d["id"], spec=d["spec"], tests=d["tests"]))
    if not tasks:
        raise ValueError(f"no tasks loaded from {path}")
    return tasks


def _split_for(task_id: str) -> str:
    """Deterministic promotion/confirmation split by task id (stable across runs, so a
    task always lands on the same side — the gate's out-of-sample guarantee needs the
    confirmation set disjoint from the set the winner was chosen on)."""
    h = int(hashlib.sha256(task_id.encode()).hexdigest(), 16)
    return "promotion" if (h & 1) == 0 else "confirmation"


def _default_window_id() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def build_steps(tasks: list[SkillTask], *, skill_id: str, cell_id: str,
                window_id: str) -> list[bench.Step]:
    """Build apex bench Steps for a skill benchmark. The step CONTEXT holds the task
    (identical for both arms); the tests live in the oracle for objective grading."""
    steps: list[bench.Step] = []
    for t in tasks:
        steps.append(bench.Step(
            # step_id is the RAW task id (NOT window-qualified). This deliberately KEEPS the
            # base bench's duplicate-(step_id, model) guard active: re-running the SAME task
            # in another window raises, so pseudoreplication is rejected (astra F4). Real
            # replication comes only from DISTINCT tasks captured in different windows.
            step_id=t.id,
            venue="skill",
            cell_id=cell_id,
            split=_split_for(t.id),  # split by the task id → stable holdout for a fixed set
            context={"messages": [{"role": "user", "content": t.spec}],
                     "tools": [], "params": {}},
            oracle={"kind": "pytest", "tests": t.tests},
            window_id=window_id,
            provenance="objective",   # executable grader -> objective floor (not judge)
        ))
    return steps


def make_replay(skill_text: str, skill_id: str, *,
                model_call: Callable[[list, str], str]) -> Callable:
    """Build a replay_fn(step, model) for run_bench.

    `model_call(messages, arm)` runs the FROZEN target model on `messages` and returns its
    text answer. `arm` is the pseudo-model id (BASELINE_ID or skill_id) so a caller can
    log/route by arm if needed; the skill injection itself is done HERE so the step context
    stays identical across arms. The candidate arm prepends the skill as a system message;
    the baseline arm sends the bare task. That single-message difference IS the treatment.
    """
    def replay_fn(step: bench.Step, model: str) -> bench.Replay:
        base_msgs = list(step.context.get("messages", []))
        if model == skill_id:
            msgs = [{"role": "system", "content": _SKILL_FRAME.format(skill=skill_text)}] + base_msgs
        else:
            msgs = base_msgs
        answer = model_call(msgs, model)
        # cost/latency are not the point of a quality bench; record zeros (the gate's cost
        # tiebreak is off unless cost data is supplied). tokens are best-effort.
        return bench.Replay(output=answer, cost_usd=0.0,
                            tokens_in=0, tokens_out=0, latency=0.0)
    return replay_fn


def make_score_fn() -> Callable:
    """Objective grader: extract code from the arm's answer, run the task's executable
    tests, score 1.0 iff every test passes (bench.objective_score). No LLM judge."""
    from .ornith.offload_lanes import run_python_tests
    from .ornith.ornith_code import extract_code

    def score_fn(step: bench.Step, model: str, replay: bench.Replay) -> dict:
        tests = step.oracle.get("tests", "")
        code = extract_code(replay.output or "")
        passed, _detail = run_python_tests(code, tests)
        return bench.objective_score(passed)
    return score_fn


def campaign_snapshot(skill_text: str, model_id: str) -> str:
    """Immutable campaign identity (astra F5): bind the skill BYTES and the frozen model id.
    A changed skill revision or a changed model produces a different snapshot, so the gate
    cannot merge their evidence into one campaign (apex: a changed baseline voids the cert).
    Deliberately does NOT hash the task set — windows legitimately carry DIFFERENT tasks over
    time (that is real replication); binding tasks here would forbid fresh evidence."""
    h = hashlib.sha256()
    h.update(b"skill\0")
    h.update(skill_text.encode())
    h.update(b"\0model\0")
    h.update(model_id.encode())
    return h.hexdigest()[:16]


def run_skill_bench(tasks: list[SkillTask], *, skill_id: str, skill_text: str, model_id: str,
                    model_call: Callable[[list, str], str],
                    cell_id: str | None = None, window_id: str | None = None,
                    bench_run_id: str | None = None, corpus_snapshot: str | None = None,
                    store_path=None) -> list[dict]:
    """Run the paired A/B rollout for one skill vs the no-skill baseline. Returns bench
    rows (candidate_set = [baseline, skill]); pass store_path to persist the reward stream.
    `model_id` names the frozen target model and is BOUND into the campaign snapshot (F5).
    Pure w.r.t. the injected `model_call` (a fake makes the whole path unit-testable)."""
    cell_id = cell_id or f"skill/{skill_id}"
    window_id = window_id or _default_window_id()
    # Campaign identity (bench_run_id + corpus_snapshot) is stable across windows so
    # accumulated rows pair as one campaign — but BOUND to (skill bytes, model id), so a
    # changed skill/model won't merge (astra F5). Windows carry DIFFERENT tasks (real
    # replication); the base bench's duplicate guard rejects re-running the same task (F4).
    corpus_snapshot = corpus_snapshot or campaign_snapshot(skill_text, model_id)
    bench_run_id = bench_run_id or f"skillbench-{skill_id}-{corpus_snapshot}"

    steps = build_steps(tasks, skill_id=skill_id, cell_id=cell_id, window_id=window_id)
    replay_fn = make_replay(skill_text, skill_id, model_call=model_call)
    score_fn = make_score_fn()
    return bench.run_bench(
        steps, candidate_set=[BASELINE_ID, skill_id],
        replay_fn=replay_fn, score_fn=score_fn,
        bench_run_id=bench_run_id, corpus_snapshot=corpus_snapshot,
        store_path=store_path,
    )


def _pass_rate(rows: list[dict], model: str) -> tuple[float, int]:
    scored = [r for r in rows if r.get("model") == model]
    if not scored:
        return (0.0, 0)
    hits = sum(1 for r in scored if r["outcome"].get("score", 0.0) >= 1.0)
    return (hits / len(scored), len(scored))


def _paired_cell_evidence(rows: list[dict], *, cell_id: str, skill_id: str):
    """Build a gate CellEvidence where a confirmation WINDOW counts toward replication ONLY
    if it produced a real PAIRED delta (both baseline and skill present for that step).

    astra F3: `bench.cell_evidence_from_rows` draws confirmation windows from ALL candidate
    rows, so a candidate-only window (baseline row dropped by a flaky rollout) would inflate
    the replication count with no actual paired evidence. Here windows are derived from the
    PAIRED steps only, so replication measures genuine confirmation evidence. Delta pairing
    itself is delegated to the base bench (which also enforces the no-duplicate guard, F4).
    """
    from .gate import CellEvidence

    scoped = [r for r in rows if r.get("cell_id") == cell_id]

    # (F5, pass2) CAMPAIGN ISOLATION: every row in the cell must share ONE identity
    # (corpus_snapshot + bench_run_id). Otherwise promotion evidence from skill revision A and
    # confirmation evidence from revision B (or a forced corpus_snapshot) merge into one fake
    # campaign. A changed skill/model produces a different snapshot, so a mismatch here means
    # "two campaigns" and must be refused — not silently paired across the split boundary.
    snaps = {r.get("corpus_snapshot") for r in scoped}
    runs = {r.get("bench_run_id") for r in scoped}
    if len(snaps) > 1 or len(runs) > 1:
        raise ValueError(
            f"cell {cell_id!r} mixes campaigns (snapshots={sorted(map(str, snaps))}, "
            f"runs={sorted(map(str, runs))}); a changed skill/model must not merge evidence")

    # (F1, pass2) WINDOW ISOLATION: a task may appear in AT MOST ONE window. Pairing keys on
    # step_id, so a task whose baseline lands in window w2 and whose skill lands in w3 would
    # otherwise pair ACROSS windows (complementary-failure bypass) and mis-credit a window it
    # was never fully evaluated in. One task = one window makes every paired delta strictly
    # within a single window; it also subsumes the same-task-reran-across-windows guard (F4).
    task_windows: dict = {}
    for r in scoped:
        task_windows.setdefault(r["step_id"], set()).add(r.get("window_id"))
    straddlers = {t: sorted(map(str, w)) for t, w in task_windows.items() if len(w) > 1}
    if straddlers:
        raise ValueError(
            f"cell {cell_id!r} has tasks spanning multiple windows {straddlers} "
            f"(pseudoreplication / cross-window pairing); each task belongs to ONE window")

    promo = bench.deltas_from_rows(scoped, incumbent=BASELINE_ID, split="promotion",
                                   cell_id=cell_id)
    confirm = bench.deltas_from_rows(scoped, incumbent=BASELINE_ID, split="confirmation",
                                     cell_id=cell_id)

    # Per-candidate confirmation windows, counted ONLY from steps that HAVE BOTH ARMS.
    scores: dict = {}   # (step_id) -> {model: score}
    win_of: dict = {}   # step_id -> window_id
    for r in scoped:
        if r.get("split") != "confirmation":
            continue
        scores.setdefault(r["step_id"], {})[r["model"]] = r["outcome"].get("score", 0.0)
        win_of[r["step_id"]] = r.get("window_id")
    windows: dict = {}
    for step_id, arms in scores.items():
        if BASELINE_ID in arms and skill_id in arms:   # a REAL paired delta this step
            wid = win_of.get(step_id)
            if wid:
                windows.setdefault(skill_id, set()).add(wid)

    provs = {r.get("provenance", "objective") for r in scoped}
    provenance = next(iter(provs)) if len(provs) == 1 else "judge"  # mixed => stricter floor

    return CellEvidence(
        cell_id=cell_id, parent_task_type=f"skill/{skill_id}", incumbent_model=BASELINE_ID,
        promo_deltas=promo, confirm_deltas=confirm, confirm_windows=windows,
        provenance=provenance,
    )


def grade_skill(all_rows: list[dict], *, skill_id: str, cell_id: str | None = None,
                k: int = 5, m_windows: int = 2, alpha: float = 0.05) -> SkillVerdict:
    """Turn accumulated bench rows (one or MORE windows) into a verdict behind apex's gate.

    `all_rows` may span multiple runs/windows — that is how the replication requirement is
    met over time. The gate promotes the skill only if it clears the sample floor, beats
    baseline OUT-OF-SAMPLE, survives FDR, and confirms across >= m_windows distinct
    windows. The paired bootstrap CI (SkillOpt-evalkit analog) is reported alongside.

    NOTE: rows across windows share a corpus_snapshot only if the task set is unchanged;
    cell_evidence pairs within (cell, split) per its own run/snapshot scoping. We pass the
    rows straight through — pairing is done per-step by the bench layer.
    """
    cell_id = cell_id or f"skill/{skill_id}"
    rows = [r for r in all_rows if r.get("cell_id") == cell_id]

    base_rate, _ = _pass_rate(rows, BASELINE_ID)
    skill_rate, n = _pass_rate(rows, skill_id)

    # Paired per-step deltas across ALL splits, for the CI + mean (the report metric).
    # (The gate does its own promotion/confirmation-scoped pairing internally.)
    by_step: dict = {}
    for r in rows:
        by_step.setdefault(r["step_id"], {})[r["model"]] = r["outcome"].get("score", 0.0)
    deltas = [v[skill_id] - v[BASELINE_ID]
              for v in by_step.values() if skill_id in v and BASELINE_ID in v]
    mean_delta = sum(deltas) / len(deltas) if deltas else 0.0
    if deltas:
        ci_low, ci_high = stats.paired_bootstrap_ci(deltas, n_boot=2000, alpha=alpha, seed=12345)
    else:
        ci_low = ci_high = 0.0

    windows_seen = len({r.get("window_id") for r in rows if r.get("window_id")})

    # apex gate verdict. cell_evidence must scope pairing to one run/snapshot; when rows
    # span multiple runs the gate reconstructs promotion/confirmation deltas + per-candidate
    # windows from the rows. We build the evidence via _paired_cell_evidence, which counts a
    # confirmation window toward replication ONLY when that window produced a real PAIRED
    # delta (both arms present) — a candidate-only window must not satisfy replication (F3).
    try:
        evidence = _paired_cell_evidence(rows, cell_id=cell_id, skill_id=skill_id)
        results = run_gate([evidence], k=k, m_windows=m_windows, alpha=alpha)
        gr = results[0] if results else None
        promoted = bool(gr and gr.promoted)
        reason = gr.reason if gr else "no gate result"
    except ValueError as e:
        # Rejected by a soundness guard: a duplicate (step_id, model) re-run (F4), a task
        # spanning multiple windows / cross-window pairing (pass2 F1), or mixed campaign
        # identity across the split (pass2 F5). All are pseudoreplication / identity leaks the
        # gate must refuse rather than promote on.
        promoted = False
        reason = f"rejected (pseudoreplication/identity guard): {e}"

    return SkillVerdict(
        skill_id=skill_id, n_tasks=n, baseline_pass_rate=base_rate,
        skill_pass_rate=skill_rate, mean_delta=mean_delta,
        ci_low=ci_low, ci_high=ci_high, promoted=promoted,
        windows_seen=windows_seen, reason=reason,
    )


def grade_skills_family(all_rows: list[dict], skill_ids: list[str], *,
                        k: int = 5, m_windows: int = 2,
                        alpha: float = 0.05) -> list[SkillVerdict]:
    """Grade a FAMILY of skills together so BH-FDR is real across the campaign (astra F6).

    A single `grade_skill` runs the gate on one cell — a family of one, where BH reduces to
    the raw threshold. When you have tried MANY skills you are running a multiple-comparison
    campaign, and promoting the best one on its own p-value is exactly the repeated-holdout
    search apex's gate exists to correct. This feeds ALL skill cells to `run_gate` in one
    call, so the Benjamini-Hochberg step corrects across the whole family. Per-skill report
    fields (pass rates, CI) mirror `grade_skill`.
    """
    # (F6, pass2) A skill id counted TWICE would get two BH slots, diluting the correction
    # (three copies of a marginal p turns a family of 4 into an easy pass). One id = one
    # hypothesis: dedup while preserving order.
    seen: set = set()
    skill_ids = [s for s in skill_ids if not (s in seen or seen.add(s))]

    evidences = []
    present = []
    for sid in skill_ids:
        cell_id = f"skill/{sid}"
        rows = [r for r in all_rows if r.get("cell_id") == cell_id]
        if not rows:
            continue
        try:
            evidences.append(_paired_cell_evidence(rows, cell_id=cell_id, skill_id=sid))
            present.append(sid)
        except ValueError:
            continue  # pseudoreplication in this cell — excluded from the family
    results = run_gate(evidences, k=k, m_windows=m_windows, alpha=alpha)
    by_cell = {r.cell_id: r for r in results}

    verdicts = []
    for sid in present:
        cell_id = f"skill/{sid}"
        rows = [r for r in all_rows if r.get("cell_id") == cell_id]
        base_rate, _ = _pass_rate(rows, BASELINE_ID)
        skill_rate, n = _pass_rate(rows, sid)
        by_step: dict = {}
        for r in rows:
            by_step.setdefault(r["step_id"], {})[r["model"]] = r["outcome"].get("score", 0.0)
        deltas = [v[sid] - v[BASELINE_ID]
                  for v in by_step.values() if sid in v and BASELINE_ID in v]
        mean_delta = sum(deltas) / len(deltas) if deltas else 0.0
        ci_low, ci_high = (stats.paired_bootstrap_ci(deltas, n_boot=2000, alpha=alpha,
                                                     seed=12345) if deltas else (0.0, 0.0))
        gr = by_cell.get(cell_id)
        windows_seen = len({r.get("window_id") for r in rows if r.get("window_id")})
        verdicts.append(SkillVerdict(
            skill_id=sid, n_tasks=n, baseline_pass_rate=base_rate, skill_pass_rate=skill_rate,
            mean_delta=mean_delta, ci_low=ci_low, ci_high=ci_high,
            promoted=bool(gr and gr.promoted), windows_seen=windows_seen,
            reason=(gr.reason if gr else "no gate result") + " [family-FDR corrected]",
        ))
    return verdicts


def append_ledger(ledger_path: str | Path, verdict: SkillVerdict, *,
                  window_id: str | None = None) -> None:
    """Append one run's summary to the over-time ledger (JSONL). Successive runs across
    days accumulate the windows the gate's replication requirement consumes."""
    p = Path(ledger_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "window_id": window_id or _default_window_id(),
        "skill_id": verdict.skill_id,
        "n_tasks": verdict.n_tasks,
        "baseline_pass_rate": round(verdict.baseline_pass_rate, 4),
        "skill_pass_rate": round(verdict.skill_pass_rate, 4),
        "mean_delta": round(verdict.mean_delta, 4),
        "ci": [round(verdict.ci_low, 4), round(verdict.ci_high, 4)],
        "promoted": verdict.promoted,
        "windows_seen": verdict.windows_seen,
        "reason": verdict.reason,
    }
    with open(p, "a") as f:
        f.write(json.dumps(row) + "\n")


def format_verdict(v: SkillVerdict) -> str:
    """Human-readable one-block report (the deliverable a person reads before adopting)."""
    ci = f"[{v.ci_low:+.3f}, {v.ci_high:+.3f}]"
    lift = f"{v.baseline_pass_rate:.1%} -> {v.skill_pass_rate:.1%} (Δ {v.mean_delta:+.3f})"
    gate = "PROMOTED (cleared apex gate)" if v.promoted else "NOT promoted"
    return (
        f"skill: {v.skill_id}\n"
        f"  tasks: {v.n_tasks} | windows: {v.windows_seen}\n"
        f"  pass rate: {lift}\n"
        f"  paired bootstrap 95% CI on Δ: {ci}\n"
        f"  gate: {gate} — {v.reason}\n"
        f"  (measure-only: a PROMOTED skill is a candidate for a human to adopt at the\n"
        f"   skill/agent layer; this harness never deploys it into live traffic.)"
    )
