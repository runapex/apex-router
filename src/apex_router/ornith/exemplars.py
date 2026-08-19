"""RAG exemplar store over frontier corrections (feedback/approved.jsonl).

On escalation the frontier's correction is captured (record_feedback). Here we retrieve the k nearest
prior corrections as few-shot exemplars for the next same-type sub-task. The snapshot + lineage
controls (spec F5) keep any IMPROVEMENT MEASUREMENT honest: retrieval can be pinned to a corpus
snapshot taken BEFORE an eval set is frozen, and can exclude a query's own task lineage so a case can
never retrieve its own correction.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..embed import cosine, embed


def load_corrections(path: Path, *, before: str | None = None) -> list[dict]:
    """Read approved corrections from a jsonl file. When `before` (an ISO timestamp) is given, keep
    only corrections with `created_at < before` — the SNAPSHOT cutoff (F5-b) so a measurement only
    retrieves corpus captured before the eval set was frozen. Skips unapproved/malformed rows."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not r.get("approved_for_training"):
            continue
        if before is not None and str(r.get("created_at", "")) >= before:
            continue    # captured at/after the snapshot cutoff -> excluded
        out.append(r)
    return out


def _query_text(rec: dict) -> str:
    msgs = rec.get("messages") or []
    return " ".join(m.get("content", "") for m in msgs if isinstance(m, dict))


def retrieve_exemplars(query: str, corrections: list[dict], k: int = 3, *,
                       embed_fn=embed, exclude_lineage: frozenset = frozenset()) -> list[dict]:
    """The k corrections nearest to `query` by cosine of their message text, EXCLUDING any whose
    `source_job_id` is in `exclude_lineage` (F5-c: a case must never retrieve its own lineage).
    Deterministic given a fixed `embed_fn`."""
    if not corrections:
        return []
    qv = embed_fn(query)
    scored: list[tuple[float, dict]] = []
    for r in corrections:
        if r.get("source_job_id") in exclude_lineage:
            continue
        try:
            sim = cosine(qv, embed_fn(_query_text(r)))
        except ValueError:
            continue   # a zero-norm / empty exemplar can't be scored; skip it
        scored.append((sim, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in scored[:k]]
