"""§4.2 single-step paired replay bench — the authoritative comparator.

For each corpus step x each candidate model: replay the FIXED-context step, score the
output (objective oracle, §5.1), and append a per-(step, model) reward row to the store.
`deltas_from_rows` then pairs each candidate against the incumbent ON THE SAME STEP,
WITHIN ONE CELL, to produce the {model: [deltas]} that amr.gate consumes.

Scope (design §1/§4.2, finding #8): this measures SINGLE-STEP quality on a fixed
captured context — it is NOT a full-trajectory counterfactual. Variance/stability come
from FRESH corpus steps over time, never from re-running the same frozen step
(pseudo-replication, finding #2).

Hardened after Codex adversarial cross-validation (2026-07-31):
- pairing is CELL-LOCAL (keyed on cell_id+step_id), so rows from different cells or runs
  that happen to share a step_id can never cross-pair (Codex #1/#2);
- a SCORER contract violation raises (never a silent, biasing dropped row); only a
  genuine REPLAY/infra failure is tolerated and drops that row (Codex #3);
- rows carry window_id + provenance so gate evidence is reconstructed, not fabricated
  (Codex #4);
- deltas are ordered deterministically by step_id, so results don't depend on row order
  (Codex #5);
- candidate_set is materialized once (a generator isn't drained after the first model)
  and the numeric-score contract is validated at write time (Codex #6).

Network and scoring are injected seams (`replay_fn`, `score_fn`) so the orchestration is
hermetic and testable, mirroring codeqa/ab.py. The real `replay_fn` must run in a sandbox
with read-only/ephemeral tool backends (hermetic replay, §4.2); the bench core here is
transport-agnostic and does not itself call a model.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import store


class ScoreContractError(ValueError):
    """A score_fn returned a malformed outcome (no numeric 'score'). This is a scorer
    BUG, surfaced loudly — never swallowed into a silently dropped row (Codex #3)."""


@dataclass(frozen=True)
class Step:
    """One replayable corpus step (the fixed context for a single turn)."""
    step_id: str
    venue: str
    cell_id: str
    split: str                    # discovery | promotion | confirmation
    context: dict                 # {messages, tools, params} — the FIXED context
    oracle: dict = field(default_factory=dict)   # {kind, spec_ref?, repo_snapshot?}
    window_id: str = "w0"         # capture-window id (gate replication needs this)
    provenance: str = "objective"  # objective | judge (gate sample floor needs this)

    def __post_init__(self):
        # A blank window_id must never reach the gate as if it were a real capture
        # window — the gate counts distinct windows for replication (Codex pass2 #3).
        if not (isinstance(self.window_id, str) and self.window_id.strip()):
            raise ValueError(f"window_id must be a non-empty string, got {self.window_id!r}")


@dataclass(frozen=True)
class Replay:
    """The result of replaying a step through one model."""
    output: str
    cost_usd: float
    tokens_in: int
    tokens_out: int
    latency: float


def objective_score(passed: bool) -> dict:
    """§5.1 objective oracle score: pass -> 1.0, fail -> 0.0 (a bounded outcome).
    Returned as an outcome dict so a bench row's `outcome` is uniform across scorers."""
    return {"score": 1.0 if passed else 0.0, "pass": bool(passed)}


def _validate_outcome(outcome, step, model):
    """A score_fn must return a dict with a finite numeric 'score'. Anything else is a
    scorer contract violation and raises (Codex #3/#6) — never a silent drop."""
    if not isinstance(outcome, dict) or "score" not in outcome:
        raise ScoreContractError(
            f"score_fn returned no 'score' for step={step.step_id!r} model={model!r}: {outcome!r}")
    sc = outcome["score"]
    if not isinstance(sc, (int, float)) or isinstance(sc, bool):
        raise ScoreContractError(
            f"score_fn 'score' is not numeric for step={step.step_id!r} "
            f"model={model!r}: {sc!r}")
    # Guard values that are numeric but not representable as a finite float — e.g. a
    # huge Python int (10**10000) passes isinstance but OverflowErrors when the gate
    # converts it via math.isfinite (Codex pass2 #6). Force the float conversion here.
    try:
        f = float(sc)
    except (OverflowError, ValueError):
        # Don't repr(sc) here — a huge int's own repr can raise (int-str-conversion
        # limit). Report the type instead.
        raise ScoreContractError(
            f"score_fn 'score' is not float-representable for step={step.step_id!r} "
            f"model={model!r}: {type(sc).__name__}")
    if f != f or f in (float("inf"), float("-inf")):
        raise ScoreContractError(
            f"score_fn 'score' is not finite for step={step.step_id!r} "
            f"model={model!r}: {sc!r}")


def run_bench(steps, *, candidate_set, replay_fn, score_fn, bench_run_id,
              corpus_snapshot, store_path, now_fn=None, provider_meta=None):
    """Replay every step through every candidate model, score, and append reward rows.

    - replay_fn(step, model) -> Replay        (may raise on infra/transport failure)
    - score_fn(step, model, replay) -> outcome dict with a finite numeric 'score'
    - now_fn() -> ISO timestamp string (injected for determinism/testing)

    A replay_fn that raises drops THAT (step, model) row and continues — a single flaky
    upstream must not abort the bench. A score_fn that violates its contract (no numeric
    'score') RAISES: a scorer bug turned into a dropped row would bias the deltas by
    selective missingness (Codex #3), which is worse than fewer samples.

    `store_path=None` skips persistence (used in tests); otherwise each row is appended
    to the bench-reward stream. Returns the list of appended rows.
    """
    ts = (now_fn or _default_now)()
    candidates = list(candidate_set)   # materialize once — a generator would drain (Codex #6)
    rows = []
    # Build + validate ALL rows first; persist only after the whole run validates, so a
    # scorer failure mid-run cannot leave a partially-persisted run on disk (Codex pass2
    # #4). A genuine replay/infra failure still just drops its row (not fatal).
    for step in steps:
        for model in candidates:
            try:
                rep = replay_fn(step, model)
            except Exception:
                continue
            # Scoring is OUTSIDE the infra try/except: a scorer bug must surface, not
            # masquerade as a dropped row. _validate_outcome raises on a contract breach,
            # which aborts run_bench BEFORE anything is persisted.
            outcome = score_fn(step, model, rep)
            _validate_outcome(outcome, step, model)
            rows.append({
                "step_id": step.step_id,
                "venue": step.venue,
                "model": model,
                "cell_id": step.cell_id,
                "split": step.split,
                "window_id": step.window_id,
                "provenance": step.provenance,
                "outcome": outcome,
                "cost_usd": rep.cost_usd,
                "tokens_in": rep.tokens_in,
                "tokens_out": rep.tokens_out,
                "latency": rep.latency,
                "bench_run_id": bench_run_id,
                "corpus_snapshot": corpus_snapshot,
                "candidate_set": list(candidates),
                "provider_meta": dict(provider_meta or {}),
                "ts": ts,
            })

    if store_path is not None:
        for row in rows:
            store.append_reward(store_path, row)   # bench-reward stream (finding #16)
    return rows


def deltas_from_rows(rows, *, incumbent, split, cell_id,
                     bench_run_id=None, corpus_snapshot=None):
    """Pair each candidate against the incumbent ON THE SAME STEP -> {model: [deltas]}.

    Pairing is scoped to a single `cell_id` and `split`, and (when provided)
    `bench_run_id` + `corpus_snapshot`. If bench_run_id/corpus_snapshot are omitted they
    are INFERRED from the rows and required to be unique — pairing across different runs
    or corpus snapshots is never allowed (Codex #1 + pass2 #1), because scores from
    different runs/snapshots aren't a valid single-step paired comparison. Rows from
    other cells (even sharing a step_id) are ignored (cell-local, Codex #1/#2).

    A delta is (candidate_score - incumbent_score) for a step where BOTH sides are
    present in scope. Steps missing either side are skipped. Both the delta lists AND the
    candidate key order are deterministic (sorted), independent of row order (Codex #5 +
    pass2 #5). Raises ValueError on a duplicate (step_id, model) in scope.
    """
    scoped = [r for r in rows if r.get("split") == split and r.get("cell_id") == cell_id]

    # Resolve the run/snapshot scope: explicit args win; otherwise infer and require
    # uniqueness so we never silently merge multiple runs/snapshots.
    def _resolve(field_name, given):
        if given is not None:
            return given
        vals = {r.get(field_name) for r in scoped if field_name in r}
        vals.discard(None)
        if len(vals) > 1:
            raise ValueError(
                f"rows span multiple {field_name} {sorted(map(str, vals))!r}; "
                f"pass {field_name} explicitly to scope the pairing")
        return next(iter(vals)) if vals else None

    run = _resolve("bench_run_id", bench_run_id)
    snap = _resolve("corpus_snapshot", corpus_snapshot)
    if run is not None:
        scoped = [r for r in scoped if r.get("bench_run_id") == run]
    if snap is not None:
        scoped = [r for r in scoped if r.get("corpus_snapshot") == snap]

    # index: step_id -> {model: score}, guarding duplicates.
    by_step: dict = {}
    for r in scoped:
        sid, model = r["step_id"], r["model"]
        score = r["outcome"]["score"]
        slot = by_step.setdefault(sid, {})
        if model in slot:
            raise ValueError(
                f"duplicate (step_id={sid!r}, model={model!r}) in cell {cell_id!r} split {split!r}")
        slot[model] = score

    deltas: dict = {}
    for sid in sorted(by_step):           # deterministic step order -> delta order
        model_scores = by_step[sid]
        if incumbent not in model_scores:
            continue                      # unpaired: no incumbent on this step
        base = model_scores[incumbent]
        for model in sorted(model_scores):    # deterministic candidate KEY order (pass2 #5)
            if model == incumbent:
                continue
            deltas.setdefault(model, []).append(model_scores[model] - base)
    return deltas


def cell_evidence_from_rows(rows, *, cell_id, parent_task_type, incumbent,
                            bench_run_id=None, corpus_snapshot=None):
    """Assemble a gate CellEvidence directly from bench rows — no hand-fed windows or
    provenance (Codex pass2 #2). Reconstructs, for `cell_id`:
      - promo_deltas / confirm_deltas   (candidate-vs-incumbent, via deltas_from_rows)
      - confirm_windows                 (per-candidate set of window_ids on the
                                         CONFIRMATION split, so replication is measured
                                         from real evidence)
      - provenance                      (the cell's provenance, required consistent)

    This is the bridge that lets the gate consume bench output without any metadata being
    fabricated at the call site. Imported lazily to avoid a circular import with gate.
    """
    from .gate import CellEvidence

    scoped = [r for r in rows if r.get("cell_id") == cell_id]
    if bench_run_id is not None:
        scoped = [r for r in scoped if r.get("bench_run_id") == bench_run_id]
    if corpus_snapshot is not None:
        scoped = [r for r in scoped if r.get("corpus_snapshot") == corpus_snapshot]

    # Provenance must be consistent within the cell (it drives the sample floor).
    provs = {r.get("provenance", "objective") for r in scoped}
    if len(provs) > 1:
        raise ValueError(f"cell {cell_id!r} has mixed provenance {sorted(map(str, provs))!r}")
    provenance = next(iter(provs)) if provs else "objective"

    promo = deltas_from_rows(scoped, incumbent=incumbent, split="promotion",
                             cell_id=cell_id, bench_run_id=bench_run_id,
                             corpus_snapshot=corpus_snapshot)
    confirm = deltas_from_rows(scoped, incumbent=incumbent, split="confirmation",
                               cell_id=cell_id, bench_run_id=bench_run_id,
                               corpus_snapshot=corpus_snapshot)

    # Per-candidate confirmation windows, drawn from the rows (a candidate cannot borrow
    # another's windows — the per-candidate dict is exactly what the gate expects).
    windows: dict = {}
    for r in scoped:
        if r.get("split") != "confirmation":
            continue
        model = r["model"]
        if model == incumbent:
            continue
        wid = r.get("window_id")
        if wid:
            windows.setdefault(model, set()).add(wid)

    return CellEvidence(
        cell_id=cell_id, parent_task_type=parent_task_type, incumbent_model=incumbent,
        promo_deltas=promo, confirm_deltas=confirm, confirm_windows=windows,
        provenance=provenance,
    )


def _default_now() -> str:
    # The bench prefers an injected now_fn (determinism); this fallback is only used in
    # a real run. Import is local so the module has no import-time clock dependency.
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
