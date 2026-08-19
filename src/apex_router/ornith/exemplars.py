"""RAG exemplar store over frontier corrections (feedback/approved.jsonl).

On escalation the frontier's correction is captured (record_feedback). Here we retrieve the k nearest
prior corrections as few-shot exemplars for the next same-type sub-task. The snapshot + lineage
controls (spec F5) keep any IMPROVEMENT MEASUREMENT honest: retrieval can be pinned to a corpus
snapshot taken BEFORE an eval set is frozen, and can exclude a query's own task lineage so a case can
never retrieve its own correction.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..embed import cosine, embed


def _parse_ts(v) -> datetime | None:
    """ISO-8601 -> timezone-aware UTC datetime, or None if unparseable (naive is treated as UTC).
    Fail-closed: a garbage/missing timestamp returns None so it can't slip past a cutoff by lexical
    string ordering (e.g. '2026-08-09T20:30:00-04:00' is really 2026-08-10 00:30 UTC, AFTER a
    '2026-08-10T00:00:00+00:00' cutoff, but sorts BEFORE it as a string)."""
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        dt = datetime.fromisoformat(v.strip())
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def load_corrections(path: Path, *, before: str | None = None) -> list[dict]:
    """Read approved corrections from a jsonl file. When `before` (an ISO timestamp) is given, keep
    only corrections whose PARSED created_at is strictly before the parsed cutoff — the SNAPSHOT
    cutoff (F5-b). Timestamps are compared as timezone-aware UTC datetimes, NOT strings; a correction
    with a missing/invalid created_at is DROPPED (fail-closed). Skips unapproved/malformed rows."""
    p = Path(path)
    if not p.exists():
        return []
    cutoff = _parse_ts(before) if before is not None else None
    if before is not None and cutoff is None:
        raise ValueError(f"`before` is not a valid ISO timestamp: {before!r}")
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
        if cutoff is not None:
            ts = _parse_ts(r.get("created_at"))
            if ts is None or ts >= cutoff:
                continue    # unparseable or at/after the cutoff -> excluded (fail-closed)
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
