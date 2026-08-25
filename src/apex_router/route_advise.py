"""Escalation-rate COST observation — turn the Phase-1 route log into a cost-efficiency verdict
per task-type, gated on statistical significance AND an explicit cost model.

WHAT THIS IS (and, just as importantly, what it is NOT).

`route_log.py` records cheap-start outcomes (`ok` / `escalated`) for tasks that were *selected* to
start cheap. `read_rates()` aggregates them into a per task-type escalation frequency. This module
asks ONE narrow, honest question of that data:

    "On COST ALONE, and ASSUMING the cheap tier's kept (non-escalated) output is acceptable, is
     starting this task-type cheap-then-escalate cheaper or more expensive than starting heavy?"

That question has a real answer, because cheap-first expected cost is `C_cheap + p·C_heavy` (you
always pay cheap; with escalation probability `p` you also pay heavy), versus `C_heavy` for
heavy-first. Cheap-first wins iff `p < 1 − C_cheap/C_heavy`. So the decision threshold is the
COST-BREAK-EVEN escalation rate `break_even = 1 − 1/cost_ratio` (cost_ratio = C_heavy / C_cheap),
NOT a magic 0.5. We compare the Wilson interval of the measured rate to that break-even:

    COST_FAVORS_HEAVY_START — Wilson LOWER bound > break_even: escalation is significantly above the
                              cost break-even, so cheap-first's expected cost exceeds heavy-first's.
    COST_FAVORS_CHEAP_START — Wilson UPPER bound < break_even: significantly below → cheap-first is
                              cheaper.
    INCONCLUSIVE            — n below the floor, the CI straddles break_even, or the multiplicity
                              correction (Benjamini-Hochberg across task-types) doesn't survive.
                              This is the safe, common case: keep the caller's static default.

WHAT IT IS NOT — read before trusting a verdict (these are the confounders an independent review
                 surfaced; they are real and this data cannot resolve them, so they are stated,
                 not hidden — see [[measurement-over-attribution-lesson]]):
  1. NOT a quality/correctness judgment. `passed` is defined as `not escalated`; NOTHING here
     measures whether a kept cheap answer was correct. A low escalation rate can mean the cheap
     output was good OR that the escalation trigger was lax. Every non-INCONCLUSIVE verdict carries
     an `assumes` field stating this precondition. If cheap-when-kept is NOT reliably acceptable for
     a task-type, ignore the verdict.
  2. NOT a counterfactual. The log contains ONLY tasks already selected to start cheap. A
     COST_FAVORS_CHEAP_START verdict does not license *widening* cheap-start to the harder tasks the
     current policy starts heavy — those were never observed. It speaks only to the cheap-eligible
     population.
  3. NOT confounder-adjusted. Records carry model id / tier / timestamp, but aggregation pools them:
     an obsolete weak cheap model and the current one are summed. A verdict mixing eras is suspect.
     Slice the log yourself (by model/period) if a routing change is at stake.
  4. NOT a superiority proof, and NOT self-executing. It RECOMMENDS a cost lens; a human or the model
     reading `model-routing` decides. Nothing here rewrites a skill, a config, or a route table.

The authoritative "which model is BEST" decision (quality AND cost, with a counterfactual) belongs
to a gated benchmark (route_table.py), not to this observational rate. This module is only for the
escalation on-ramp, which has no bench.
"""
from __future__ import annotations

import math

from . import stats

# Verdict labels (public API — keep stable).
COST_FAVORS_HEAVY_START = "cost_favors_heavy_start"
COST_FAVORS_CHEAP_START = "cost_favors_cheap_start"
INCONCLUSIVE = "inconclusive"

# Defaults.
_DEFAULT_MIN_N = 30       # sample floor — a fluke like 4/4 must not cross a threshold
_DEFAULT_COST_RATIO = 5.0  # C_heavy / C_cheap (e.g. opus:haiku input ≈ 5:1). break_even = 1 - 1/ratio
_DEFAULT_Z = 1.96         # 95% Wilson interval (matches route_table's convention)
_DEFAULT_ALPHA = 0.05     # Benjamini-Hochberg family-wise level across task-types

# The precondition every actionable verdict must carry — the #1 confounder, stated up front.
_ASSUMES = ("cost-only: assumes the cheap tier's kept (non-escalated) output is ACCEPTABLE QUALITY "
            "(NOT measured here) and that this cheap-eligible sample generalizes to the task-type")


def _break_even(cost_ratio: float) -> float:
    """Cost break-even escalation rate: cheap-first is cheaper iff p < 1 - 1/cost_ratio."""
    return 1.0 - 1.0 / cost_ratio


def _two_sided_p(escalated: int, n: int, break_even: float) -> float:
    """Normal-approximation TWO-SIDED p-value for the measured rate vs the fixed break-even null.

    Two-sided on purpose (Codex pass-2): choosing the tail from the observed rate and using that
    one-sided p is picking the smaller of two tails post-hoc, which understates p and makes the BH
    correction anti-conservative. A direction-agnostic two-sided p is the honest family input; the
    Wilson CI (in advise_one) supplies the DIRECTION separately. Variance uses the NULL proportion
    (standard one-sample proportion test)."""
    se = math.sqrt(break_even * (1.0 - break_even) / n) if 0.0 < break_even < 1.0 else 0.0
    if se == 0.0:
        return 0.0 if escalated / n != break_even else 1.0
    z = abs(escalated / n - break_even) / se
    # two-sided p = 2·P(Z > |z|) = erfc(|z|/√2).
    return math.erfc(z / math.sqrt(2.0))


def advise_one(n: int, escalated: int, *, min_n: int = _DEFAULT_MIN_N,
               cost_ratio: float = _DEFAULT_COST_RATIO, z: float = _DEFAULT_Z,
               null_ts: int = 0) -> dict:
    """Cost-efficiency verdict for one task-type's (n, escalated) counts. Pure, deterministic.

    Returns {verdict, rate, ci_low, ci_high, break_even, n, significant, p_value, assumes, reason}.
    Never raises for ordinary inputs; invalid counts/params yield INCONCLUSIVE rather than throwing.
    `significant` here reflects only the per-cell CI-vs-break_even test; the multiplicity (BH) gate is
    applied in `advise()` across task-types (a lone `advise_one` has no family to correct against).
    """
    # Input validation — an independent review showed z<=0 gives a zero-width "always significant" CI,
    # min_n<1 / cost_ratio<=1 defeat the guards, and a non-finite z/cost_ratio (argparse accepts
    # `nan`/`inf`) sails past the comparisons into a NaN break-even + a non-JSON `NaN` token. Reject
    # anything non-finite or out of range rather than emit a spurious verdict.
    if n <= 0 or escalated < 0 or escalated > n:
        return _rec(INCONCLUSIVE, 0.0, 0.0, 0.0, 0.0, max(n, 0), False, 1.0, "",
                    "no or invalid samples", null_ts=null_ts)
    if not math.isfinite(z) or z <= 0:
        return _rec(INCONCLUSIVE, escalated / n, 0.0, 1.0, 0.0, n, False, 1.0, "",
                    f"invalid z={z} (must be finite and > 0)", null_ts=null_ts)
    if not math.isfinite(cost_ratio) or cost_ratio <= 1.0:
        return _rec(INCONCLUSIVE, escalated / n, 0.0, 1.0, 0.0, n, False, 1.0, "",
                    f"invalid cost_ratio={cost_ratio} (must be finite and > 1: heavy must cost more than cheap)",
                    null_ts=null_ts)
    if min_n < 1:
        min_n = 1
    if isinstance(null_ts, bool) or not isinstance(null_ts, int) or null_ts < 0:
        null_ts = 0

    rate = escalated / n
    be = _break_even(cost_ratio)
    lo, hi = stats.wilson_ci(escalated, n, z=z)

    if n < min_n:
        return _rec(INCONCLUSIVE, rate, lo, hi, be, n, False, 1.0, "",
                    f"n={n} < min_n={min_n} (insufficient evidence)", null_ts=null_ts)
    # The Wilson CI supplies the DIRECTION; the two-sided p feeds the multiplicity correction. n>=min_n
    # cells that don't clear the CI are still "tested" (part of the BH family) — advise() decides that.
    p = _two_sided_p(escalated, n, be)
    if lo > be:
        return _rec(COST_FAVORS_HEAVY_START, rate, lo, hi, be, n, True, p, _ASSUMES,
                    f"escalation CI lower {lo:.2f} > cost break-even {be:.2f}: cheap-first costs more on average",
                    tested=True, null_ts=null_ts)
    if hi < be:
        return _rec(COST_FAVORS_CHEAP_START, rate, lo, hi, be, n, True, p, _ASSUMES,
                    f"escalation CI upper {hi:.2f} < cost break-even {be:.2f}: cheap-first is cheaper",
                    tested=True, null_ts=null_ts)
    return _rec(INCONCLUSIVE, rate, lo, hi, be, n, False, p, "",
                f"CI [{lo:.2f},{hi:.2f}] straddles cost break-even {be:.2f}", tested=True,
                null_ts=null_ts)


def advise(*, log_path=None, min_n: int = _DEFAULT_MIN_N, cost_ratio: float = _DEFAULT_COST_RATIO,
           z: float = _DEFAULT_Z, alpha: float = _DEFAULT_ALPHA, rates: dict | None = None) -> dict:
    """Per-task-type cost-efficiency verdicts over the whole route log. Fail-safe: any error → {}.

    Applies a Benjamini-Hochberg multiplicity correction across task-types: testing many task-types
    inflates the chance of at least one spurious flag, so a cell that clears its per-cell CI test is
    demoted to INCONCLUSIVE unless its two-sided p-value also survives BH at `alpha`. The BH FAMILY is
    every cell that met min_n (i.e. was actually tested), NOT just the cells that crossed the CI —
    selecting the family by significance would itself be anti-conservative (Codex pass-2). `rates` may
    be injected (from `route_log.read_rates`) for testing; else read fresh.
    """
    try:
        if rates is None:
            from . import route_log
            rates = route_log.read_rates(log_path=log_path)
        # Pass 1: per-cell verdicts.
        out: dict = {}
        for tt, cell in (rates or {}).items():
            if not isinstance(tt, str) or not isinstance(cell, dict):
                continue
            n = cell.get("n")
            esc = cell.get("escalated")
            if not isinstance(n, int) or not isinstance(esc, int):
                continue
            null_ts = cell.get("null_ts", 0)
            if isinstance(null_ts, bool) or not isinstance(null_ts, int) or null_ts < 0:
                null_ts = 0
            out[tt] = advise_one(n, esc, min_n=min_n, cost_ratio=cost_ratio, z=z,
                                 null_ts=null_ts)

        # Pass 2: BH multiplicity correction. The FAMILY is every TESTED cell (met min_n), so family
        # membership is independent of the outcome — building it from only the CI-significant cells
        # would filter the family by the same data used to test, inflating the false-action rate
        # (Codex pass-2 P1). BH ranks over the whole family; only cells that BOTH crossed the CI AND
        # survive BH keep an actionable verdict.
        family = [tt for tt, r in out.items() if r["tested"]]
        if len(family) > 1:
            pvals = [out[tt]["p_value"] for tt in family]
            keep = stats.benjamini_hochberg(pvals, alpha=alpha)
            survivors = {tt for tt, s in zip(family, keep) if s}
            for tt in family:
                r = out[tt]
                if r["significant"] and tt not in survivors:
                    out[tt] = _rec(INCONCLUSIVE, r["rate"], r["ci_low"], r["ci_high"], r["break_even"],
                                   r["n"], False, r["p_value"], "",
                                   f"per-cell significant but did not survive BH across {len(family)} "
                                   f"tested task-types (multiplicity) — keep the default", tested=True,
                                   null_ts=r.get("null_ts", 0))
        return out
    except Exception:
        return {}


def _rec(verdict, rate, ci_low, ci_high, break_even, n, significant, p_value, assumes, reason,
         tested=False, null_ts: int = 0) -> dict:
    # `tested` = this cell met min_n and produced a real p-value, so it is a member of the BH family
    # regardless of whether it crossed the CI. `significant` = it crossed the per-cell Wilson gate.
    # Keeping them distinct is the fix for selecting the hypothesis family by significance.
    if null_ts > 0:
        reason = f"{reason} !! {null_ts} null-ts rows (provenance unknown)"
    rec = {
        "verdict": verdict,
        "rate": rate,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "break_even": break_even,
        "n": n,
        "significant": significant,
        "tested": tested,
        "p_value": p_value,
        "assumes": assumes,
        "reason": reason,
    }
    if null_ts > 0:
        rec["null_ts"] = null_ts
    return rec
