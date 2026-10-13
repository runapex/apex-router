"""pce-core verdict — one decision schema for every arm-vs-arm decision apex makes.

A `Verdict` is the card a consumer emits when it decides between an incumbent and one or more candidate
arms: the per-arm summary, the pre-declared contrasts (each with an interval, a status and, for families
of tests, a Benjamini-Hochberg decision), the out-of-sample gate rows (`gate.run_gate`), the kill
criteria, and the decision itself. Consumers so far: the retry-action A/B (`retry_ab.report`, a
two-proportion contrast) and the panel/deliberation studies (`scripts/panel*.py`, paired Brier deltas
through the gate). The same status vocabulary is used everywhere:

  better | worse                — the interval excludes 0 on that side
  equivalent                    — the interval lies inside a declared ±margin (only when a margin is given;
                                  checked first, so an interval inside the margin is never better/worse)
  no detectable difference      — the interval spans 0 (and no margin, or it is wider than the margin)
  INCONCLUSIVE                  — below the sample floor; no interval is interpreted

Stdlib only; built on core.stats (no new estimators) and the existing gate.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field

from apex_router.core import stats

SCHEMA = 1
BETTER, WORSE, EQUIVALENT, NO_DIFF, INCONCLUSIVE = (
    "better", "worse", "equivalent", "no detectable difference", "INCONCLUSIVE")
STATUSES = (BETTER, WORSE, EQUIVALENT, NO_DIFF, INCONCLUSIVE)


@dataclass
class Contrast:
    """`estimate` is oriented so that POSITIVE means `arm` is better than `vs`."""
    name: str
    arm: str
    vs: str
    metric: str
    estimate: float | None
    ci: tuple
    n: int
    status: str
    p: float | None = None          # two-sided; None for interval-only contrasts
    fdr_reject: bool | None = None  # set by apply_fdr for contrasts in a declared family


@dataclass
class Verdict:
    question: str
    incumbent: str
    decision: str
    arms: dict
    contrasts: list
    gate: list = field(default_factory=list)
    kills: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    schema: int = SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), default=str, **kw)

    def contrast(self, name: str) -> Contrast | None:
        return next((c for c in self.contrasts if c.name == name), None)


def classify(lo, hi, enough: bool = True, margin: float | None = None) -> str:
    """Status of an interval for a positive-is-better estimate."""
    if not enough:
        return INCONCLUSIVE
    if lo is None or hi is None or not (math.isfinite(lo) and math.isfinite(hi)):
        return INCONCLUSIVE
    if margin is not None and -margin <= lo and hi <= margin:
        return EQUIVALENT   # a declared margin is checked first: an interval inside it is practically null
    if lo > 0:
        return BETTER
    if hi < 0:
        return WORSE
    return NO_DIFF


def paired_contrast(name: str, arm: str, vs: str, deltas, *, metric: str, min_n: int = 30,
                    margin: float | None = None, n_boot: int = 4000, seed: int = 1) -> Contrast:
    """Paired per-unit deltas (positive = `arm` better). Percentile bootstrap CI and a two-sided,
    null-imposed bootstrap p (twice the smaller one-sided p, capped at 1)."""
    d = [float(x) for x in deltas]
    if not d:
        return Contrast(name, arm, vs, metric, None, (None, None), 0, INCONCLUSIVE, None)
    lo, hi = stats.paired_bootstrap_ci(d, n_boot=n_boot, seed=seed)
    pg = stats.paired_bootstrap_pvalue(d, n_boot=n_boot, seed=seed, alternative="greater")
    pl = stats.paired_bootstrap_pvalue(d, n_boot=n_boot, seed=seed, alternative="less")
    return Contrast(name, arm, vs, metric, sum(d) / len(d), (lo, hi), len(d),
                    classify(lo, hi, len(d) >= min_n, margin), min(1.0, 2 * min(pg, pl)))


def proportion_contrast(name: str, arm: str, vs: str, k1: int, n1: int, k0: int, n0: int, *,
                        metric: str, min_n: int = 30, z: float = 1.96) -> Contrast:
    """Two independent proportions, arm − vs, Newcombe hybrid-score CI (no p-value: interval only)."""
    d, lo, hi = stats.newcombe_diff(k1, n1, k0, n0, z)
    return Contrast(name, arm, vs, metric, d, (lo, hi), min(n1, n0),
                    classify(lo, hi, n1 >= min_n and n0 >= min_n))


def apply_fdr(contrasts, alpha: float = 0.05) -> list:
    """Benjamini-Hochberg across every contrast that carries a p-value (the declared family), in place.
    Contrasts without a p are left with fdr_reject=None. Returns the contrasts."""
    fam = [c for c in contrasts if c.p is not None]
    for c, r in zip(fam, stats.benjamini_hochberg([c.p for c in fam], alpha=alpha)):
        c.fdr_reject = r
    return contrasts


def gate_rows(cells, *, k: int, m_windows: int, alpha: float = 0.05) -> list:
    """gate.run_gate over CellEvidence, as plain dicts for the card."""
    from apex_router import gate
    return [asdict(r) for r in gate.run_gate(cells, k=k, m_windows=m_windows, alpha=alpha)]


def decide(incumbent: str, *, gate: list | None = None, primary: Contrast | None = None,
           kills: dict | None = None) -> str:
    """One decision rule for every consumer.

    A fired kill (any kills value that is exactly True) wins: stay on the incumbent. Otherwise a gate
    promotion decides; with no gate, the primary contrast decides (better -> adopt, INCONCLUSIVE ->
    INCONCLUSIVE, anything else -> stay)."""
    fired = sorted(k for k, v in (kills or {}).items() if v is True)
    if fired:
        return f"stay on {incumbent} (kill: {', '.join(fired)})"
    if gate:
        promoted = [g for g in gate if g.get("promoted")]
        if promoted:
            return "; ".join(f"promote {g['chosen_model']} ({g['cell_id']})" for g in promoted)
        return f"stay on {incumbent} (gate: no promotion)"
    if primary is None:
        return INCONCLUSIVE
    if primary.status == BETTER:
        return f"adopt {primary.arm} over {primary.vs}"
    if primary.status == INCONCLUSIVE:
        return INCONCLUSIVE
    return f"stay on {incumbent} ({primary.status})"
