"""Zeno frontier — where the last bit of reliability goes, measured on this machine's model runs.

Zeno: to arrive you first cover half the distance, then half of what is left, and so on. Model
progress often looks the same: each generation closes a fraction of the remaining gap,

    gap_{n+1} = alpha * gap_n,   0 < alpha < 1,

but the cost of a step does not shrink with the gap (often C_{n+1} > C_n). The gap series converges;
the cost series need not. This module puts that on numbers, in three lenses:

1. Engineering Zeno — each extra unit of reliability costs more. `frontier()` takes (cost, failure)
   points and gives, per segment, the gap contraction alpha, the local elasticity
   d ln(failure) / d ln(cost), and **decades of cost per nine** (how many 10x of cost one more 10x
   cut in failures takes at the local rate). Rising decades-per-nine = Zeno. `ladder()` is the toy
   series: geometric gap, geometric cost.
2. Horizon Zeno — step reliability compounds: P(n-step task) = p^n (0.99^100 = 36.6%,
   0.999^100 = 90.5%). `horizon()` gives the table; `compounding()` compares that independence
   prediction with the clean-session rate actually observed. A gap means one p with independent
   steps does not describe the sessions (clustering, per-session p, or outcome-dependent length).
3. Epistemic Zeno — the target moves: more capability opens new ways to fail, and you only see the
   ones you measure. `discovery()` is the failure-mode discovery curve with the Good-Turing chance
   that the next failure is a kind never seen yet, Chao1 for how many kinds there probably are,
   and the share of failures with no recorded cause (the blind spot). `coverage()` is
   E_benchmark -> 0 while E_stratum does not: per-stratum Wilson bounds against the aggregate,
   flagging strata that are measurably worse, too thin to say, or never measured at all.

What it measures and what it doesn't: the proxy records whether a call FINISHED (transport and
upstream faults), not whether the answer was RIGHT; xval records whether a review reached a verdict.
Those are the reliability floors under answer quality — they don't stand in for it, and the report
says so. Pure stdlib; read-only over ~/.apex/telemetry.jsonl and ~/.apex-router/xval_runs.jsonl.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import markov as mk
from .core.stats import wilson_ci

LN10 = math.log(10.0)


# ---- 1. engineering Zeno ---------------------------------------------------------------------

def nines(p: float) -> float:
    """Reliability in nines: 0.9 -> 1, 0.99 -> 2, 0.999 -> 3. math.inf at p == 1."""
    if not 0.0 <= p <= 1.0:
        raise ValueError("p must be in [0, 1]")
    return math.inf if p == 1.0 else -math.log10(1.0 - p) + 0.0  # +0.0: no "-0.00" at p=0


def steps_to(gap0: float, alpha: float, target: float) -> int:
    """Generations of gap <- alpha*gap needed to bring gap0 down to <= target (0 if already there)."""
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if gap0 <= 0 or target <= 0:
        raise ValueError("gap0 and target must be > 0")
    if gap0 <= target:
        return 0
    n = math.ceil(math.log(target / gap0) / math.log(alpha))
    # float guard: log ratios that land a hair past an exact integer
    while n > 0 and gap0 * alpha ** (n - 1) <= target:
        n -= 1
    return n


def ladder(alpha: float = 0.5, growth: float = 10.0, gens: int = 8,
           gap0: float = 1.0, c0: float = 1.0) -> list[dict]:
    """The Zeno series: gap_n = gap0*alpha^n converges to 0, cost_n = c0*growth^n does not.

    Each row: generation, gap, reliability (1-gap, for gap0 <= 1), nines, the step's cost, and the
    cumulative cost. sum(gap) -> gap0/(1-alpha) (finite); sum(cost) diverges for growth >= 1.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if growth <= 0 or gens < 0:
        raise ValueError("growth must be > 0 and gens >= 0")
    rows, cum = [], 0.0
    for n in range(gens + 1):
        gap = gap0 * alpha ** n
        cost = c0 * growth ** n
        cum += cost
        rel = 1.0 - gap if gap0 <= 1.0 else None
        rows.append({"gen": n, "gap": gap, "reliability": rel,
                     "nines": nines(rel) if rel is not None else None,
                     "step_cost": cost, "cum_cost": cum})
    return rows


def frontier(points) -> dict:
    """Diminishing-returns test over (cost, failure_rate) points.

    Sorted by cost. Per consecutive segment (c1,f1) -> (c2,f2):
      alpha             = f2/f1                      gap contraction (< 1 = progress)
      elasticity        = ln(f2/f1) / ln(c2/c1)      < 0 = more cost buys fewer failures
      decades_per_nine  = log10(c2/c1) / -log10(f2/f1)
                          how many 10x of cost one more 10x cut in failures takes at this local rate
    verdict: "zeno" when decades_per_nine rises segment over segment (each nine costs more than the
    last), "steady" when it stays within 10%, "accelerating" when it falls, "stalled" when a
    segment spends more and fails no less, "insufficient" with < 2 usable segments.
    A segment touching a zero failure rate has no finite elasticity (a zero observed rate is a
    bound, not a value — use an upper bound such as 3/n before calling this).
    """
    pts = sorted((float(c), float(f)) for c, f in points)
    if any(c <= 0 for c, _ in pts) or any(not 0.0 <= f <= 1.0 for _, f in pts):
        raise ValueError("costs must be > 0 and failure rates in [0, 1]")
    segs = []
    for (c1, f1), (c2, f2) in zip(pts, pts[1:]):
        seg = {"cost_from": c1, "cost_to": c2, "fail_from": f1, "fail_to": f2,
               "cost_ratio": c2 / c1, "alpha": (f2 / f1) if f1 > 0 else None,
               "elasticity": None, "decades_per_nine": None}
        if c2 > c1 and f1 > 0 and f2 > 0:
            seg["elasticity"] = math.log(f2 / f1) / math.log(c2 / c1)
            if f2 < f1:
                seg["decades_per_nine"] = math.log10(c2 / c1) / -math.log10(f2 / f1)
        segs.append(seg)
    usable = [s for s in segs if s["cost_ratio"] > 1 and s["fail_from"] > 0 and s["fail_to"] > 0]
    if any(s["fail_to"] >= s["fail_from"] for s in usable):
        verdict = "stalled"
    else:
        d = [s["decades_per_nine"] for s in usable]
        if len(d) < 2:
            verdict = "insufficient"
        else:
            # one symmetric 10% band per step: up / down / flat — no overlap between verdicts
            moves = {"up" if b > 1.1 * a else "down" if b < 0.9 * a else "flat"
                     for a, b in zip(d, d[1:])}
            verdict = ({"up": "zeno", "down": "accelerating", "flat": "steady"}[moves.pop()]
                       if len(moves) == 1 else "mixed")
    return {"segments": segs, "verdict": verdict}


# ---- 2. horizon Zeno -------------------------------------------------------------------------

def horizon_success(p: float, steps: int) -> float:
    """P(all `steps` independent steps succeed) = p^steps."""
    if not 0.0 <= p <= 1.0 or steps < 0:
        raise ValueError("p in [0, 1], steps >= 0")
    return p ** steps


def per_step_needed(target: float, steps: int) -> float:
    """Per-step reliability for P(success over `steps`) >= target: target^(1/steps)."""
    if not 0.0 < target <= 1.0 or steps < 1:
        raise ValueError("target in (0, 1], steps >= 1")
    return target ** (1.0 / steps)


def max_horizon(p: float, target: float) -> float:
    """Longest task (steps) that still succeeds with probability >= target at step reliability p."""
    if not 0.0 < target <= 1.0 or not 0.0 <= p <= 1.0:
        raise ValueError("p in [0, 1], target in (0, 1]")
    if p == 1.0:
        return math.inf
    if p == 0.0:
        return 0
    n = max(0, math.floor(math.log(target) / math.log(p)))
    # The log ratio is off by at most one rounding step: settle it on the exact inequality
    # p^n >= target with ONE bounded check each way (a loop can stall on an underflow plateau).
    if p ** (n + 1) >= target:
        n += 1
    elif n > 0 and p ** n < target:
        n -= 1
    return n


def horizon(p: float, steps=(1, 10, 100, 1000), target: float = 0.9) -> dict:
    return {"p": p, "nines": nines(p),
            "table": [{"steps": n, "p_success": horizon_success(p, n)} for n in steps],
            "target": target, "max_steps_at_target": max_horizon(p, target),
            "needed_for_100_steps": per_step_needed(target, 100)}


_BUCKETS = ((1, 10), (11, 50), (51, 200), (201, None))


def compounding(sessions, buckets=_BUCKETS) -> dict:
    """Observed clean-session rate vs the p^n independence prediction.

    `sessions`: iterable of (calls, errors). p = pooled per-call success. Per length bucket:
    observed share of sessions with zero errors (Wilson interval) vs mean(p^calls). ratio =
    observed / predicted. A ratio away from 1 says the single-p independence model does not
    describe these sessions; it does NOT say why. Candidates: failures clustering in a few sessions,
    sessions with different p (one bad upstream hour), or session length that depends on outcome
    (a run that stops at its first failure is short AND failed). Read it as a model check.
    """
    sessions = [(int(c), int(e)) for c, e in sessions if c > 0]
    calls = sum(c for c, _ in sessions)
    errs = sum(e for _, e in sessions)
    if not calls:
        return {"sessions": 0, "calls": 0, "p": None, "buckets": []}
    p = 1.0 - errs / calls
    out = []
    for lo, hi in buckets:
        b = [(c, e) for c, e in sessions if c >= lo and (hi is None or c <= hi)]
        if not b:
            continue
        clean = sum(1 for _, e in b if e == 0)
        pred = sum(p ** c for c, _ in b) / len(b)
        obs = clean / len(b)
        out.append({"calls": f"{lo}-{hi}" if hi else f"{lo}+", "sessions": len(b),
                    "observed_clean": obs, "observed_ci": wilson_ci(clean, len(b)),
                    "predicted_clean": pred,
                    "ratio": (obs / pred) if pred > 0 else None})
    return {"sessions": len(sessions), "calls": calls, "errors": errs, "p": p, "buckets": out}


def markov_horizon(seqs, steps=(1, 10, 100, 1000), buckets=_BUCKETS,
                   min_pairs: int = 200, r1_threshold: float = 0.1) -> dict:
    """The bursty-failure estimate beside p^n — an independent signal, not a correction to it.

    `seqs`: per-session 0/1 outcome sequences in time order (1 = failed call), sessions in
    first-appearance order. Fits a 2-state chain (markov.fit_chain) and gives P(clean) at the
    horizon steps, per length bucket (observed vs iid p^n vs chain, both fitted in-sample on these
    sessions), and a held-out comparison (first 70% of sessions fit, the rest scored).
    verdict: "independence violated" when lag-1 autocorrelation r1 >= `r1_threshold` over >=
    `min_pairs` consecutive pairs, "independence holds" below it, "insufficient" under `min_pairs`.
    """
    seqs = [list(s) for s in seqs if s]
    ch = mk.fit_chain(seqs)
    r1, pairs = mk.autocorr(seqs)
    p = mk.iid_p(seqs, ch["prior"])
    out_b = []
    for lo, hi in buckets:
        b = [s for s in seqs if len(s) >= lo and (hi is None or len(s) <= hi)]
        if not b:
            continue
        pcs = [mk.p_clean(ch, len(s)) for s in b]
        out_b.append({"calls": f"{lo}-{hi}" if hi else f"{lo}+", "sessions": len(b),
                      "observed_clean": sum(1 for s in b if not any(s)) / len(b),
                      "iid_clean": sum(p ** len(s) for s in b) / len(b),
                      "markov_clean": (None if any(x is None for x in pcs)
                                       else sum(pcs) / len(pcs))})
    if pairs < min_pairs or r1 is None:
        verdict = "insufficient"
    else:
        verdict = "independence violated" if r1 >= r1_threshold else "independence holds"
    return {"sessions": ch["sessions"], "r1": r1, "pairs": pairs, "counts": ch["counts"],
            "p_first_ok": ch["p_first_ok"], "p_ok_ok": ch["p_ok_ok"],
            "p_fail_fail": ch["p_fail_fail"],
            "mean_fail_run": mk.mean_fail_run(ch["p_fail_fail"]),
            "stationary_fail": mk.stationary_fail(ch["p_ok_ok"], ch["p_fail_fail"]),
            "table": [{"steps": n, "p_success": mk.p_clean(ch, n)} for n in steps],
            "buckets": out_b, "holdout": mk.holdout(seqs, prior=ch["prior"]),
            "verdict": verdict}


# ---- 3. epistemic Zeno -----------------------------------------------------------------------

def discovery(labels) -> dict:
    """Failure-mode discovery curve over time-ordered failure labels (None = no recorded cause).

    good_turing_p_new = f1/n: chance the next labeled failure is a kind not seen yet.
    chao1 = S_obs + f1^2/(2 f2) (bias-corrected f1(f1-1)/2 when f2 = 0): estimated number of kinds.
    unlabeled_share: failures we counted but cannot name — the part of the failure space we are not
    measuring at all. events_since_new: labeled failures since the last new kind appeared.
    """
    labels = list(labels)
    known = [x for x in labels if x]
    counts = Counter(known)
    seen, curve, last_new = set(), [], 0
    for i, x in enumerate(known, 1):
        if x not in seen:
            seen.add(x)
            curve.append({"event": i, "kinds": len(seen), "label": x})
            last_new = i
    n, s_obs = len(known), len(counts)
    f1 = sum(1 for v in counts.values() if v == 1)
    f2 = sum(1 for v in counts.values() if v == 2)
    chao1 = s_obs + (f1 * f1 / (2 * f2) if f2 else f1 * (f1 - 1) / 2)
    return {"failures": len(labels), "labeled": n,
            "unlabeled_share": (len(labels) - n) / len(labels) if labels else 0.0,
            "kinds": s_obs, "singletons": f1, "doubletons": f2,
            "good_turing_p_new": f1 / n if n else None,
            "chao1": chao1 if n else None,
            "events_since_new": n - last_new if n else None,
            "counts": dict(counts.most_common()), "curve": curve}


def coverage(strata: dict, expected=(), thin: int = 30) -> dict:
    """E_aggregate -> 0 while E_stratum does not.

    `strata`: name -> (failures, trials). Each stratum gets its rate and Wilson interval and one of:
      worse     — its Wilson LOWER bound is above the aggregate's UPPER bound (measurably worse)
      thin      — fewer than `thin` trials (a 0/n stratum is still bounded only by ~3/n)
      unmeasured— named in `expected` but no trials at all (the aggregate says nothing about it)
      ok        — otherwise
    masking = worst stratum upper bound / aggregate rate: how much the headline can hide.
    """
    k = sum(f for f, _ in strata.values())
    n = sum(t for _, t in strata.values())
    agg = k / n if n else None
    agg_ci = wilson_ci(k, n) if n else (None, None)
    rows = {}
    for name, (f, t) in sorted(strata.items()):
        if t <= 0:
            rows[name] = {"failures": 0, "trials": 0, "rate": None, "ci": (None, None),
                          "status": "unmeasured"}
            continue
        lo, hi = wilson_ci(f, t)
        status = ("worse" if agg_ci[1] is not None and lo > agg_ci[1]
                  else "thin" if t < thin else "ok")
        rows[name] = {"failures": f, "trials": t, "rate": f / t, "ci": (lo, hi), "status": status}
    for name in expected:
        rows.setdefault(name, {"failures": 0, "trials": 0, "rate": None, "ci": (None, None),
                               "status": "unmeasured"})
    uppers = [r["ci"][1] for r in rows.values() if r["ci"][1] is not None]
    masking = (max(uppers) / agg) if (uppers and agg) else None
    return {"aggregate": agg, "aggregate_ci": agg_ci, "trials": n, "strata": rows,
            "masking": masking}


# ---- live data -------------------------------------------------------------------------------

def _router_home() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME", Path.home() / ".apex-router"))


def _jsonl(path: Path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except FileNotFoundError:
        return


def _request_rows(path: Path, since_ts: float):
    """Proxy request rows only: heartbeats (ev=hb) and rows without an is_error verdict are skipped."""
    for r in _jsonl(path):
        if r.get("ev") or r.get("is_error") is None:
            continue
        ts = r.get("ts")
        if isinstance(ts, (int, float)) and ts >= since_ts:
            yield r


def _xval_frontier(xval_path: Path, min_n: int, since_ts: float = 0.0) -> dict:
    """Cost vs failure per xval arm (output cap).

    Every run with an arm counts toward runs/failed — a failed run often has no cost (the rollout
    died), and dropping it would hide exactly the failures. Mean cost is over costed runs only.
    fail_upper is the Wilson upper bound, so a 0/n arm still reports how bad it could be. Arms with
    zero failures stay out of the frontier: a zero rate is a bound, not a point on the curve.
    """
    by = defaultdict(lambda: [0, 0, 0, 0.0])  # arm -> [runs, failed, costed, cost_sum]
    for r in _jsonl(xval_path):
        ts = r.get("ts")
        if not r.get("arm") or (since_ts and not (isinstance(ts, (int, float)) and ts >= since_ts)):
            continue
        b = by[r["arm"]]
        b[0] += 1
        b[1] += 0 if r.get("ok") else 1
        if isinstance(r.get("cost"), (int, float)):
            b[2] += 1
            b[3] += float(r["cost"])
    arms = []
    for arm, (n, bad, costed, cost) in sorted(by.items()):
        arms.append({"arm": arm, "runs": n, "failed": bad, "costed": costed,
                     "mean_cost": cost / costed if costed else None,
                     "fail_rate": bad / n, "fail_upper": wilson_ci(bad, n)[1],
                     "evidence": "ok" if n >= min_n else "thin"})
    usable = [a for a in arms if a["evidence"] == "ok" and a["failed"] > 0
              and a["mean_cost"] is not None]
    fr = frontier([(a["mean_cost"], a["fail_rate"]) for a in usable]) if len(usable) >= 3 else None
    return {"arms": arms, "min_runs": min_n,
            "frontier": fr, "verdict": fr["verdict"] if fr else "insufficient"}


def report(telemetry: Path | None = None, xval_runs: Path | None = None,
           since_days: float | None = None, min_arm_runs: int = 20) -> dict:
    from .telemetry_path import telemetry_path
    tel = Path(telemetry) if telemetry else telemetry_path()
    xv = Path(xval_runs) if xval_runs else _router_home() / "xval_runs.jsonl"
    since = time.time() - since_days * 86400 if since_days else 0.0

    rows = sorted(_request_rows(tel, since), key=lambda r: r["ts"])
    n = len(rows)
    k = sum(1 for r in rows if r.get("is_error"))
    p = 1.0 - k / n if n else None

    per_session = defaultdict(lambda: [0, 0])
    seqs = defaultdict(list)  # session -> 0/1 outcomes in ts order; dict order = first appearance
    for r in rows:
        sid = r.get("session_id")
        if sid:
            per_session[sid][0] += 1
            per_session[sid][1] += 1 if r.get("is_error") else 0
            seqs[sid].append(1 if r.get("is_error") else 0)

    strata = defaultdict(lambda: [0, 0])
    clients = defaultdict(lambda: [0, 0])
    fields_by_schema = defaultdict(set)
    for r in rows:
        for key, d in (((r.get("stratum") or "unknown"), strata),
                       ((r.get("client") or "unknown"), clients)):
            d[key][0] += 1 if r.get("is_error") else 0
            d[key][1] += 1
        fields_by_schema[r.get("schema_version")].update(
            f for f, v in r.items() if v is not None)

    return {
        "source": {"telemetry": str(tel), "xval_runs": str(xv), "since_days": since_days,
                   "requests": n,
                   "first_ts": rows[0]["ts"] if rows else None,
                   "last_ts": rows[-1]["ts"] if rows else None},
        "measures": "call completion (transport/upstream faults) and xval verdicts — "
                    "NOT answer correctness",
        "reliability": ({"errors": k, "p": p, "p_ci": wilson_ci(n - k, n), **horizon(p)}
                        if n else None),
        "compounding": compounding(per_session.values()),
        "markov": markov_horizon(seqs.values()),
        "engineering": _xval_frontier(xv, min_arm_runs, since),
        "discovery": discovery(r.get("error_cause") for r in rows if r.get("is_error")),
        "coverage_stratum": coverage({s: tuple(v) for s, v in strata.items()},
                                     expected=("xs", "s", "m", "l")),
        "coverage_client": coverage({c: tuple(v) for c, v in clients.items()}),
        "measurement_space": {str(v): len(f) for v, f in sorted(
            fields_by_schema.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))},
    }


# ---- rendering + CLI -------------------------------------------------------------------------

def _pct(x, d=2):
    return "—" if x is None else f"{100 * x:.{d}f}%"


def _ci(ci, d=2):
    lo, hi = ci
    return "—" if lo is None else f"[{_pct(lo, d)}, {_pct(hi, d)}]"


def render(rep: dict) -> str:
    L = ["Zeno frontier — " + rep["measures"]]
    src = rep["source"]
    L.append(f"source: {src['requests']} proxy requests"
             + (f", last {src['since_days']:g} days" if src["since_days"] else ""))
    rel = rep["reliability"]
    L += ["", "1. horizon — per-call completion compounds"]
    if not rel:
        L.append("  no proxy request rows — nothing to measure")
    else:
        L += _render_horizon(rep)
        L += _render_markov(rep)
    L += _render_rest(rep)
    return "\n".join(L)


def _render_horizon(rep: dict) -> list:
    rel, L = rep["reliability"], []
    L.append(f"  p = {_pct(rel['p'], 3)} {_ci(rel['p_ci'], 3)}  ({rel['nines']:.2f} nines, "
             f"{rel['errors']} failed calls)")
    for row in rel["table"]:
        L.append(f"  {row['steps']:>5} calls → {_pct(row['p_success'], 1):>7} finish clean")
    L.append(f"  ≥{_pct(rel['target'], 0)} clean: at most {rel['max_steps_at_target']} calls at this p;"
             f" 100 calls need p ≥ {_pct(rel['needed_for_100_steps'], 3)}")
    comp = rep["compounding"]
    if comp["buckets"]:
        # its own p: only rows that carry a session id (claude-code; codex rows mostly have none)
        L.append(f"  observed vs p^n over {comp['sessions']} sessions — p = {_pct(comp['p'], 3)} "
                 f"from their {comp['calls']} calls (rows with a session id only):")
        for b in comp["buckets"]:
            L.append(f"    {b['calls']:>8} calls  n={b['sessions']:<3} clean {_pct(b['observed_clean'], 0):>5}"
                     f" {_ci(b['observed_ci'], 0):<12} predicted {_pct(b['predicted_clean'], 0):>5}")
    return L


HOLDOUT_MIN_TEST = 20  # scored sessions before the held-out line prints a comparison


def _render_markov(rep: dict) -> list:
    m = rep.get("markov")
    L = ["", "1b. horizon — Markov (bursty failures; independent of p^n)"]
    if not m or not m["sessions"]:
        return L + ["  no rows with a session id — nothing to chain"]
    r1 = "—" if m["r1"] is None else f"{m['r1']:.3f}"
    L.append(f"  lag-1 autocorrelation r1 = {r1} over {m['pairs']} consecutive pairs in "
             f"{m['sessions']} sessions → {m['verdict']}")
    run = "—" if m["mean_fail_run"] is None else f"{m['mean_fail_run']:.2f} calls"
    L.append(f"  P(ok|ok) = {_pct(m['p_ok_ok'])}  P(fail|fail) = {_pct(m['p_fail_fail'])}  "
             f"mean failure run {run}  stationary fail {_pct(m['stationary_fail'])}")
    for row in m["table"]:
        L.append(f"  {row['steps']:>5} calls → {_pct(row['p_success'], 1):>7} finish clean (chain)")
    if m["buckets"]:
        L.append("  per session length — observed vs iid p^n vs chain (both fitted on these sessions):")
        for b in m["buckets"]:
            L.append(f"    {b['calls']:>8} calls  n={b['sessions']:<3} "
                     f"observed {_pct(b['observed_clean'], 0):>5}  iid {_pct(b['iid_clean'], 0):>5}"
                     f"  markov {_pct(b['markov_clean'], 0):>5}")
    h = m["holdout"]
    if h["n_test"] < HOLDOUT_MIN_TEST:
        # Too few scored sessions for the comparison to mean anything (the prior can flip it).
        L.append(f"  held out: too few sessions to compare ({h['n_train']} train / {h['n_test']} test, "
                 f"need {HOLDOUT_MIN_TEST} test)")
    elif h["markov_loglik"] is not None and h["iid_loglik"] is not None:
        L.append(f"  held out (fit first {h['n_train']} sessions, score next {h['n_test']}): "
                 f"clean/not log-lik iid {h['iid_loglik']:.1f} vs markov {h['markov_loglik']:.1f}; "
                 f"Brier iid {h['iid_brier']:.3f} vs markov {h['markov_brier']:.3f}")
    else:
        L.append(f"  held out: not enough sessions ({h['n_train']} train / {h['n_test']} test)")
    L.append("  caveat: one chain for every session — between-session and over-time heterogeneity "
             "is not modeled, so long sessions can still be under-predicted")
    return L


def _render_rest(rep: dict) -> list:
    L = []
    eng = rep["engineering"]
    L += ["", "2. engineering — xval cost vs failed reviews, per output-cap arm"]
    for a in eng["arms"]:
        mc = "—" if a["mean_cost"] is None else f"{a['mean_cost']:,.0f}"
        L.append(f"  {a['arm']:<14} runs {a['runs']:>3}  failed {a['failed']:>2}  "
                 f"fail ≤ {_pct(a['fail_upper'], 0):>4}  mean cost {mc:>10}  [{a['evidence']}]")
    if eng["frontier"]:
        for s in eng["frontier"]["segments"]:
            dpn = s["decades_per_nine"]
            L.append(f"    {s['cost_from']:,.0f}→{s['cost_to']:,.0f}: alpha {s['alpha'] or 0:.2f}, "
                     f"{'—' if dpn is None else f'{dpn:.1f}'} decades of cost per nine")
    L.append(f"  verdict: {eng['verdict']}"
             + ("" if eng["frontier"] else
                f" (needs ≥3 arms with ≥{eng['min_runs']} runs and ≥1 failure each)"))

    d = rep["discovery"]
    L += ["", "3. epistemic — failure kinds seen vs. likely still unseen"]
    L.append(f"  {d['failures']} failures; {_pct(d['unlabeled_share'], 0)} carry no cause "
             f"(counted but not named)")
    if d["labeled"]:
        L.append(f"  {d['kinds']} named kinds in {d['labeled']}; singletons {d['singletons']}, "
                 f"doubletons {d['doubletons']} → Chao1 ≈ {d['chao1']:.1f} kinds; "
                 f"P(next is new) ≈ {_pct(d['good_turing_p_new'], 1)}; "
                 f"{d['events_since_new']} failures since the last new kind")
        L.append("  " + ", ".join(f"{c}={v}" for c, v in d["counts"].items()))
    ms = rep["measurement_space"]
    if ms:
        L.append("  fields recorded per telemetry schema: "
                 + ", ".join(f"v{v}={n}" for v, n in ms.items()))

    for title, cov in (("4. coverage — stratum (context size)", rep["coverage_stratum"]),
                       ("   coverage — client", rep["coverage_client"])):
        L += ["", title]
        L.append(f"  aggregate {_pct(cov['aggregate'])} {_ci(cov['aggregate_ci'])} over {cov['trials']}")
        for name, s in cov["strata"].items():
            L.append(f"  {name:<12} {s['failures']:>4}/{s['trials']:<6} {_pct(s['rate']):>7} "
                     f"{_ci(s['ci']):<18} {s['status']}")
        if cov["masking"]:
            L.append(f"  worst stratum upper bound = {cov['masking']:.1f}× the aggregate")
    return L


def _floats(s: str):
    return [float(x) for x in s.split(",") if x.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router zeno",
                                 description="Zeno frontier: diminishing reliability returns, "
                                             "horizon compounding, failure-mode discovery.")
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("report", help="measure the live proxy telemetry + xval runs (default)")
    rp.add_argument("--telemetry", type=Path)
    rp.add_argument("--xval-runs", type=Path)
    rp.add_argument("--since-days", type=float)
    rp.add_argument("--min-arm-runs", type=int, default=20)
    rp.add_argument("--json", action="store_true")
    hp = sub.add_parser("horizon", help="p^n table for a per-step reliability")
    hp.add_argument("--p", type=float, required=True)
    hp.add_argument("--steps", default="1,10,100,1000")
    hp.add_argument("--target", type=float, default=0.9)
    lp = sub.add_parser("ladder", help="geometric gap vs geometric cost (the Zeno series)")
    lp.add_argument("--alpha", type=float, default=0.5)
    lp.add_argument("--growth", type=float, default=10.0)
    lp.add_argument("--gens", type=int, default=8)
    fp = sub.add_parser("frontier", help="diminishing-returns test over cost:failure points")
    fp.add_argument("points", nargs="+", help="cost:failure_rate, e.g. 1:1 10:0.7 100:0.595")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "horizon":
            h = horizon(args.p, [int(x) for x in _floats(args.steps)], args.target)
            print(json.dumps(h, indent=2))
            return 0
        if args.cmd == "ladder":
            for r in ladder(args.alpha, args.growth, args.gens):
                print(f"gen {r['gen']:>2}  gap {r['gap']:.6f}  nines {r['nines']:.2f}  "
                      f"step cost {r['step_cost']:.3g}  cumulative {r['cum_cost']:.3g}")
            return 0
        if args.cmd == "frontier":
            pts = [tuple(float(x) for x in p.split(":")) for p in args.points]
            if any(len(p) != 2 for p in pts):
                raise ValueError("points are cost:failure_rate")
            print(json.dumps(frontier(pts), indent=2))
            return 0
    except ValueError as e:
        print(f"apex-router zeno: {e}", file=sys.stderr)
        return 2
    a = args if args.cmd == "report" else rp.parse_args([])
    rep = report(a.telemetry, a.xval_runs, a.since_days, a.min_arm_runs)
    print(json.dumps(rep, indent=2, default=str) if a.json else render(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
