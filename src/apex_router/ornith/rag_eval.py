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
    """ISO-8601 with an EXPLICIT offset -> timezone-aware UTC datetime, or None. Fail-closed on
    non-string, garbage, AND naive (offset-less) timestamps — a naive value could be a post-freeze
    local time silently admitted if assumed UTC (Codex xval). Mirror of exemplars._parse_ts."""
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        dt = datetime.fromisoformat(v.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


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
                  snapshot_before=None, k: int = 3, embed_fn=None, _prefrozen: bool = False) -> dict:
    """Run every case once. When `inject`, retrieve exemplars from the SNAPSHOT-enforced corpus
    (excluding the case's own lineage) and prepend them; else run the bare prompt.

    Isolation (Codex xval F1): the corpus AND each case's fields consumed here (query, lineage) are
    read from FROZEN copies taken at the top of the call, so an `ask_fn`/`judge_fn` callback that
    mutates the caller's `corrections`/`cases` mid-run cannot change what a later case (or the inject
    pass, when called via measure_improvement with `_prefrozen`) sees. `_prefrozen` skips the
    re-snapshot when measure_improvement already froze the inputs ONCE for both passes."""
    from ..embed import embed as _embed
    ef = embed_fn or _embed
    corpus = corrections if _prefrozen else _apply_snapshot(corrections, snapshot_before)
    frozen_cases = cases if _prefrozen else copy.deepcopy(cases)
    escalated = 0
    for case in frozen_cases:
        query = case.get("query", "")
        if inject:
            lineage = case.get("lineage")
            if not isinstance(lineage, str) or not lineage:
                # F5-c: without a valid string lineage we cannot guarantee a case won't retrieve its
                # own correction, so we fail closed — no injection for this case.
                messages = build_messages_with_exemplars(_SYS, query, [])
            else:
                ex = retrieve_exemplars(query, corpus, k=k, embed_fn=ef,
                                        exclude_lineage=frozenset({lineage}))
                messages = build_messages_with_exemplars(_SYS, query, ex)
        else:
            messages = build_messages_with_exemplars(_SYS, query, [])
        ans = ask_fn(messages)
        if judge_fn(ans, case):
            escalated += 1
    n = len(frozen_cases)
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
        # F5-d (Codex xval pass 2 F3): every confirmation case must carry a valid id, else an
        # id-less copy of a dev case would silently bypass the disjointness check.
        conf_ids = [c.get("id") for c in confirmation_cases]
        if any(cid is None or cid == "" for cid in conf_ids):
            raise ValueError("with dev_case_ids set, every confirmation case must have a non-empty id")
        overlap = set(conf_ids) & {i for i in dev_case_ids if i is not None}
        if overlap:
            raise ValueError(f"confirmation set overlaps the dev set (not held-out): {sorted(overlap)}")

    # Snapshot the corpus ONCE (so the two passes measure against the SAME corpus), then give EACH
    # pass its OWN fresh deep-copy of both corpus and cases (Codex xval pass 2 F1): a callback that
    # mutates its pass's copy during the baseline run cannot poison the inject pass — the inject pass
    # gets an untouched copy of the same frozen snapshot.
    snap_corpus = _apply_snapshot(corrections, snapshot_before)   # deep-copies + applies the cutoff

    base = run_condition(copy.deepcopy(confirmation_cases), copy.deepcopy(snap_corpus), inject=False,
                         ask_fn=ask_fn, judge_fn=judge_fn, k=k, embed_fn=embed_fn, _prefrozen=True)
    inj = run_condition(copy.deepcopy(confirmation_cases), copy.deepcopy(snap_corpus), inject=True,
                        ask_fn=ask_fn, judge_fn=judge_fn, k=k, embed_fn=embed_fn, _prefrozen=True)
    b = base["escalation_rate"] or 0.0
    i = inj["escalation_rate"] or 0.0
    delta = b - i
    return {"baseline_rate": b, "inject_rate": i, "delta": delta,
            "improved": delta > noise_floor + _EPS}
