"""Metrics for `apex-router explore`: HitRate_tau@B with a Wilson CI, time to first hit, niches covered,
calls. p99 impact is given only when n >= 100; else the text "n too small"."""
from __future__ import annotations

import math
from typing import Sequence

P99_MIN_N = 100


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials. n=0 -> (0, 1)."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def hit(best_score: float | None, tau: float) -> bool:
    return best_score is not None and best_score >= tau


def hit_rate(runs: Sequence[dict], tau: float | None = None) -> dict:
    """runs: result rows with 'best_score' and 'tau' (or a common tau). -> {k, n, rate, ci}."""
    n = len(runs)
    k = sum(1 for r in runs if hit(r.get("best_score"), r["tau"] if tau is None else tau))
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci": [round(lo, 4), round(hi, 4)]}


def first_hit(scores_in_call_order: Sequence[float | None], tau: float) -> int | None:
    """1-based call index of the first valid score >= tau, else None."""
    for i, s in enumerate(scores_in_call_order, 1):
        if s is not None and s >= tau:
            return i
    return None


def p99(values: Sequence[float]) -> float | str:
    """Nearest-rank 99th percentile. Only for n >= 100."""
    vals = sorted(v for v in values if v is not None)
    if len(vals) < P99_MIN_N:
        return "n too small"
    rank = math.ceil(0.99 * len(vals))
    return vals[rank - 1]


def summary(rows: Sequence[dict]) -> dict:
    """Per arm: HitRate_tau@B, mean first-hit call, niches, calls, p99 of valid candidate scores."""
    out: dict[str, dict] = {}
    for arm in sorted({r["arm"] for r in rows}):
        rs = [r for r in rows if r["arm"] == arm]
        fh = [r["first_hit_call"] for r in rs if r.get("first_hit_call")]
        scores = [s for r in rs for s in (r.get("valid_scores") or [])]
        out[arm] = {
            "hit_rate": hit_rate(rs),
            "mean_first_hit_call": (sum(fh) / len(fh)) if fh else None,
            "niches": sum(r.get("niches", 0) for r in rs),
            "calls": sum(r.get("calls", 0) for r in rs),
            "p99_score": p99(scores),
        }
    return out
