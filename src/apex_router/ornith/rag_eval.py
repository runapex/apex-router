"""Provenance-isolated RAG improvement measurement (spec F5).

Measures whether injecting retrieved corrections lowers the escalation rate on a HELD-OUT set — with
the controls that make 'held-out' real:
  - a corpus SNAPSHOT taken before the eval freeze (`snapshot_before`, applied when loading the
    corrections the caller passes in);
  - per-case LINEAGE exclusion — a case never retrieves its own correction (F5-c);
  - NO feedback writes during eval — `corrections` is a read-only input; this module never appends to
    or mutates it (F5-a).
The reported number should come from a ONE-USE confirmation set, tuned separately on a dev set
(caller's responsibility, F5-d). `improved` requires the drop to exceed a noise floor — measured-
first: if it doesn't clear the floor, RAG is not credited.
"""
from __future__ import annotations

from .exemplar_inject import build_messages_with_exemplars
from .exemplars import retrieve_exemplars

_SYS = "You are a code assistant. Answer with grounded file:line citations."


def run_condition(cases: list[dict], corrections: list[dict], *, inject: bool, ask_fn, judge_fn,
                  snapshot_before=None, k: int = 3, embed_fn=None) -> dict:
    """Run every case once. When `inject`, retrieve exemplars from `corrections` (excluding the case's
    own lineage) and prepend them; else run the bare prompt. Returns escalation counts. Read-only:
    never writes to `corrections`. `snapshot_before` is accepted for signature symmetry — the caller
    is expected to have already applied the cutoff via exemplars.load_corrections(before=...)."""
    from ..embed import embed as _embed
    ef = embed_fn or _embed
    escalated = 0
    for case in cases:
        if inject:
            ex = retrieve_exemplars(case["query"], corrections, k=k, embed_fn=ef,
                                    exclude_lineage=frozenset({case.get("lineage")}))
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
                        embed_fn=None) -> dict:
    """Run the confirmation set BOTH ways (baseline vs inject) and return the escalation-rate delta.
    `improved` is True only when `baseline_rate - inject_rate > noise_floor` — a drop within the noise
    floor is NOT counted as improvement (measured-first). Eval mode: no feedback writes."""
    base = run_condition(confirmation_cases, corrections, inject=False, ask_fn=ask_fn,
                         judge_fn=judge_fn, snapshot_before=snapshot_before, k=k, embed_fn=embed_fn)
    inj = run_condition(confirmation_cases, corrections, inject=True, ask_fn=ask_fn,
                        judge_fn=judge_fn, snapshot_before=snapshot_before, k=k, embed_fn=embed_fn)
    b = base["escalation_rate"] or 0.0
    i = inj["escalation_rate"] or 0.0
    delta = b - i
    return {"baseline_rate": b, "inject_rate": i, "delta": delta,
            "improved": delta > noise_floor}
