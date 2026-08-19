"""Provenance-isolated RAG improvement measurement (spec F5).

Measures whether injecting retrieved corrections lowers the escalation rate on a HELD-OUT set — with
the controls that make 'held-out' EVIDENCE-GRADE (hardened after a Codex refute-pass that manufactured
the improvement number three different ways):
  - SNAPSHOT is ENFORCED here, not just accepted: corrections at/after `snapshot_before` are dropped,
    with timezone-aware timestamp parsing and invalid/missing timestamps rejected fail-closed (F5-b).
  - the corrections are DEEP-COPIED into an immutable snapshot before running, so an `ask_fn`/`judge_fn`
    callback cannot inject the confirmation set's own answers between the baseline and inject passes
    (F5-a: no feedback writes during eval).
  - per-case LINEAGE exclusion requires a valid string lineage; missing/invalid fails closed (F5-c).
  - dev/confirmation DISJOINTNESS is checked when the caller supplies `dev_case_ids`; the harness
    cannot enforce single-use across process runs — that remains a caller discipline, documented (F5-d).
`improved` requires the drop to exceed a finite, NON-NEGATIVE noise floor by an epsilon (measured-
first: a drop within noise, a worse result, or an empty set is never credited).
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from .exemplar_inject import build_messages_with_exemplars
from .exemplars import retrieve_exemplars

_SYS = "You are a code assistant. Answer with grounded file:line citations."
_EPS = 1e-9   # guard the float boundary at exactly noise_floor


def _parse_ts(v) -> datetime | None:
    """Parse an ISO-8601 timestamp to a timezone-aware UTC datetime, or None if unparseable. A naive
    timestamp (no tzinfo) is treated as UTC. Returns None (fail-closed at the call site) on anything
    invalid — so a missing/garbage created_at is EXCLUDED from a snapshot, never lexically slipped in."""
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        dt = datetime.fromisoformat(v.strip())
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _apply_snapshot(corrections: list[dict], snapshot_before) -> list[dict]:
    """Return a DEEP COPY of `corrections` with the snapshot cutoff enforced (F5-a immutability +
    F5-b real timestamp compare). When `snapshot_before` is set, keep only corrections whose parsed
    created_at is strictly BEFORE the parsed cutoff; a correction with an unparseable created_at is
    DROPPED (fail-closed). When `snapshot_before` is None, keep all (still deep-copied)."""
    corr = copy.deepcopy(corrections)
    if snapshot_before is None:
        return corr
    cutoff = _parse_ts(snapshot_before)
    if cutoff is None:
        raise ValueError(f"snapshot_before is not a valid ISO timestamp: {snapshot_before!r}")
    out = []
    for r in corr:
        ts = _parse_ts(r.get("created_at"))
        if ts is not None and ts < cutoff:
            out.append(r)
    return out


def run_condition(cases: list[dict], corrections: list[dict], *, inject: bool, ask_fn, judge_fn,
                  snapshot_before=None, k: int = 3, embed_fn=None) -> dict:
    """Run every case once. When `inject`, retrieve exemplars from the SNAPSHOT-enforced, deep-copied
    corpus (excluding the case's own lineage) and prepend them; else run the bare prompt. The corpus
    is isolated per call, so callbacks cannot mutate the corrections used by a later pass."""
    from ..embed import embed as _embed
    ef = embed_fn or _embed
    corpus = _apply_snapshot(corrections, snapshot_before)
    escalated = 0
    for case in cases:
        if inject:
            lineage = case.get("lineage")
            if not isinstance(lineage, str) or not lineage:
                # F5-c: without a valid string lineage we cannot guarantee a case won't retrieve its
                # own correction, so we fail closed — no injection for this case.
                messages = build_messages_with_exemplars(_SYS, case["query"], [])
            else:
                ex = retrieve_exemplars(case["query"], corpus, k=k, embed_fn=ef,
                                        exclude_lineage=frozenset({lineage}))
                messages = build_messages_with_exemplars(_SYS, case["query"], ex)
        else:
            messages = build_messages_with_exemplars(_SYS, case["query"], [])
        ans = ask_fn(messages)
        if judge_fn(ans, case):
            escalated += 1
    n = len(cases)
    return {"n": n, "escalated": escalated, "escalation_rate": (escalated / n) if n else None}


def measure_improvement(confirmation_cases: list[dict], corrections: list[dict], *, ask_fn, judge_fn,
                        snapshot_before=None, k: int = 3, noise_floor: float = 0.0,
                        embed_fn=None, dev_case_ids: frozenset | None = None) -> dict:
    """Run the confirmation set BOTH ways (baseline vs inject) and return the escalation-rate delta.
    `improved` is True only when `baseline_rate - inject_rate > noise_floor + eps` — a drop within
    the floor, a WORSE result, or an empty set is never credited (measured-first).

    Guards (Codex xval): non-empty confirmation set required; noise_floor must be finite and >= 0 (a
    negative floor would credit a worse result); when `dev_case_ids` is given, the confirmation cases'
    ids must be DISJOINT from it (F5-d) — a number tuned on the dev set can't be reported as held-out.
    The harness cannot enforce SINGLE-USE across process runs; that stays a caller discipline."""
    import math
    if not confirmation_cases:
        raise ValueError("confirmation_cases must be non-empty")
    if not math.isfinite(noise_floor) or noise_floor < 0:
        raise ValueError(f"noise_floor must be finite and >= 0, got {noise_floor!r}")
    if dev_case_ids:
        conf_ids = {c.get("id") for c in confirmation_cases if c.get("id") is not None}
        overlap = conf_ids & {i for i in dev_case_ids if i is not None}
        if overlap:
            raise ValueError(f"confirmation set overlaps the dev set (not held-out): {sorted(overlap)}")

    base = run_condition(confirmation_cases, corrections, inject=False, ask_fn=ask_fn,
                         judge_fn=judge_fn, snapshot_before=snapshot_before, k=k, embed_fn=embed_fn)
    inj = run_condition(confirmation_cases, corrections, inject=True, ask_fn=ask_fn,
                        judge_fn=judge_fn, snapshot_before=snapshot_before, k=k, embed_fn=embed_fn)
    b = base["escalation_rate"] or 0.0
    i = inj["escalation_rate"] or 0.0
    delta = b - i
    return {"baseline_rate": b, "inject_rate": i, "delta": delta,
            "improved": delta > noise_floor + _EPS}
