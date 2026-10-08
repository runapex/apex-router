"""Retry-action A/B readout — does waiting before a transport retry make it succeed more often?

The burst chain (markov.py, zeno 1b) found that a call right after a failure fails far more often
than the base rate. That suggests waiting (or switching upstream) before retrying instead of retrying
at once — a hypothesis about an ACTION, which a held-out fit of session cleanliness cannot test. The
proxy now labels every transport retry with its arm (telemetry v11: retry_policy / retry_arm /
retry_propensity; upstream.retry_assignment). Under APEX_RETRY_AB=1 the arm is a per-request coin.

This readout, per arm, over RANDOMISED rows only (retry_policy == "ab"):
  - P(retry recovered) = the request got an upstream response after retrying (no raise-path
    error_cause) — Wilson 95% CI;
  - P(clean) = no error_cause at all (stricter: a recovered request can still be a 429);
  - mean apex-slept backoff, the price of waiting;
  - the difference arm − immediate with a Newcombe (Wilson-hybrid) 95% CI. INCONCLUSIVE until every
    compared arm has `min_n` (default 30) rows; "no detectable difference" when the CI spans 0.
Non-randomised labelled rows (a fixed APEX_RETRY_POLICY) and pre-v11 retried rows are shown as
context and power-planning input — never pooled into the comparison.

Pure stdlib; read-only over the proxy telemetry (telemetry_path()).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

from .core.stats import wilson_ci

MIN_N = 30
RATE_DAYS = 7.0  # window for the retried-requests/day planning rate
_ARMS_ORDER = ("immediate", "wait", "switch")


def _rows(path: Path, since_ts: float):
    try:
        f = open(path)
    except FileNotFoundError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("ev") or r.get("is_error") is None:
                continue
            ts = r.get("ts")
            if isinstance(ts, (int, float)) and ts >= since_ts:
                yield r


def _raise_path(r: dict) -> bool:
    """True when send_stream raised (no upstream response ever came back): an error_cause that is an
    exception class, not an `http_<status>` response or a `midstream_*` break after the response."""
    c = r.get("error_cause")
    return bool(c) and not str(c).startswith(("http_", "midstream_"))


def _retries(r: dict) -> int:
    v = r.get("connect_retries")
    return int(v) if isinstance(v, (int, float)) and v > 0 else 0


def _arm_stats(rows: list) -> dict:
    n = len(rows)
    rec = sum(1 for r in rows if not _raise_path(r))
    clean = sum(1 for r in rows if not r.get("error_cause") and not r.get("is_error"))
    waits = [float(r.get("connect_backoff_ms") or 0.0) for r in rows]
    props = sorted({r.get("retry_propensity") for r in rows if r.get("retry_propensity") is not None})
    return {"n": n, "recovered": rec, "p_recovered": rec / n if n else None,
            "recovered_ci": wilson_ci(rec, n) if n else (None, None),
            "clean": clean, "p_clean": clean / n if n else None,
            "clean_ci": wilson_ci(clean, n) if n else (None, None),
            "mean_backoff_ms": sum(waits) / n if n else None, "propensities": props}


def newcombe_diff(k1: int, n1: int, k0: int, n0: int, z: float = 1.96):
    """(p1 - p0, lo, hi): Newcombe's hybrid score interval (method 10) for a difference of two
    independent proportions. None fields when either arm is empty."""
    if n1 <= 0 or n0 <= 0:
        return None, None, None
    p1, p0 = k1 / n1, k0 / n0
    l1, u1 = wilson_ci(k1, n1, z)
    l0, u0 = wilson_ci(k0, n0, z)
    d = p1 - p0
    lo = d - math.sqrt((p1 - l1) ** 2 + (u0 - p0) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p0 - l0) ** 2)
    return d, lo, hi


def _verdict(a: dict, b: dict, lo, hi, min_n: int) -> str:
    if a["n"] < min_n or b["n"] < min_n:
        return "INCONCLUSIVE"
    if lo is not None and lo > 0:
        return "better"
    if hi is not None and hi < 0:
        return "worse"
    return "no detectable difference"


def mde(p0: float, n: int, z_a: float = 1.96, z_b: float = 0.84) -> float | None:
    """Minimum detectable difference (two-sided 5%, 80% power) for two arms of n each around a base
    rate p0, by the normal approximation — a planning number, not a test."""
    if n <= 0 or p0 is None:
        return None
    return (z_a + z_b) * math.sqrt(2 * p0 * (1 - p0) / n)


def n_for_diff(p0: float, diff: float, z_a: float = 1.96, z_b: float = 0.84) -> int | None:
    """Rows per arm to detect `diff` around base rate p0 (two-sided 5%, 80% power), normal approx."""
    if p0 is None or diff <= 0:
        return None
    return math.ceil((z_a + z_b) ** 2 * 2 * p0 * (1 - p0) / diff ** 2)


def report(telemetry: Path | None = None, since_days: float | None = None,
           min_n: int = MIN_N) -> dict:
    from .telemetry_path import telemetry_path
    tel = Path(telemetry) if telemetry else telemetry_path()
    since = time.time() - since_days * 86400 if since_days else 0.0
    rows = list(_rows(tel, since))
    ab = defaultdict(list)
    fixed = defaultdict(list)
    legacy = []
    for r in rows:
        arm = r.get("retry_arm")
        if arm:
            (ab if r.get("retry_policy") == "ab" else fixed)[arm].append(r)
        elif _retries(r):
            legacy.append(r)

    arms = {a: _arm_stats(ab[a]) for a in sorted(ab, key=lambda x: (
        _ARMS_ORDER.index(x) if x in _ARMS_ORDER else 9, x))}
    comparisons = []
    base = arms.get("immediate")
    for name, st in arms.items():
        if name == "immediate" or base is None:
            continue
        out = {"arm": name, "vs": "immediate"}
        for metric, k in (("recovered", "recovered"), ("clean", "clean")):
            d, lo, hi = newcombe_diff(st[k], st["n"], base[k], base["n"])
            out[metric] = {"diff": d, "ci": (lo, hi), "verdict": _verdict(st, base, lo, hi, min_n)}
        comparisons.append(out)

    # Planning context: how often a request retries at all, and the pre-A/B recovery rate.
    # The rate is taken over the last RATE_DAYS of the window (retry coverage widened in v8, so the
    # long tail of older rows would understate today's rate).
    per_day = None
    tss = [r["ts"] for r in rows]
    if len(tss) > 1:
        last = max(tss)
        lo_ts = max(min(tss), last - RATE_DAYS * 86400.0)
        span_days = (last - lo_ts) / 86400.0
        if span_days > 0:
            per_day = sum(1 for r in rows if r["ts"] >= lo_ts and _retries(r)) / span_days
    hist = _arm_stats(legacy) if legacy else None
    k_arms = max(len(arms), 2)
    ns = [s["n"] for s in arms.values()] + [0] * (k_arms - len(arms))  # an unseen arm has 0 rows
    # retries split evenly over the arms, so the thinnest arm sets the remaining traffic needed
    need = max(0, min_n - min(ns)) * k_arms
    return {
        "source": {"telemetry": str(tel), "since_days": since_days, "requests": len(rows)},
        "min_n": min_n,
        "arms": arms,
        "comparisons": comparisons,
        "verdict": ("INCONCLUSIVE" if not comparisons
                    else comparisons[0]["recovered"]["verdict"]),
        "fixed_policy": {a: _arm_stats(v) for a, v in sorted(fixed.items())},
        "pre_v11_retried": hist,
        "retried_per_day": per_day,
        "days_to_min_n": (need / per_day) if per_day else None,
        "mde_at_min_n": mde(hist["p_recovered"], min_n) if hist else None,
        "n_per_arm_for_10pts": n_for_diff(hist["p_recovered"], 0.10) if hist else None,
    }


def _pct(x, d=1):
    return "—" if x is None else f"{100 * x:.{d}f}%"


def _ci(ci, d=1):
    lo, hi = ci
    return "—" if lo is None else f"[{_pct(lo, d)}, {_pct(hi, d)}]"


def _signed(x):
    return "—" if x is None else f"{100 * x:+.1f} pts"


def render(rep: dict) -> str:
    L = ["Retry-action A/B — P(transport retry recovers), per arm (randomised rows only)",
         f"source: {rep['source']['requests']} proxy requests"
         + (f", last {rep['source']['since_days']:g} days" if rep["source"]["since_days"] else "")]
    if not rep["arms"]:
        L.append("  no randomised retry rows yet (set APEX_RETRY_AB=1 on the proxy to start the A/B)")
    for name, s in rep["arms"].items():
        mb = "—" if s["mean_backoff_ms"] is None else f"{s['mean_backoff_ms']:.0f} ms"
        L.append(f"  {name:<10} n={s['n']:<5} recovered {_pct(s['p_recovered']):>6} "
                 f"{_ci(s['recovered_ci']):<16} clean {_pct(s['p_clean']):>6} "
                 f"{_ci(s['clean_ci']):<16} mean backoff {mb}  propensity {s['propensities']}")
    for c in rep["comparisons"]:
        for metric in ("recovered", "clean"):
            m = c[metric]
            lo, hi = m["ci"]
            ci = "—" if lo is None else f"[{_signed(lo)}, {_signed(hi)}]"
            L.append(f"  {c['arm']} − {c['vs']} ({metric}): {_signed(m['diff'])} {ci} → {m['verdict']}")
    L.append(f"verdict: {rep['verdict']}"
             + (f" (needs ≥{rep['min_n']} randomised rows per arm)" if rep["verdict"] == "INCONCLUSIVE"
                else ""))
    for name, s in rep["fixed_policy"].items():
        L.append(f"  not randomised (fixed policy) {name}: n={s['n']} recovered {_pct(s['p_recovered'])} "
                 "— context only, not in the comparison")
    h = rep["pre_v11_retried"]
    if h:
        L.append(f"  pre-v11 retried requests (immediate, unlabelled): n={h['n']} recovered "
                 f"{_pct(h['p_recovered'])} {_ci(h['recovered_ci'])}, clean {_pct(h['p_clean'])}")
    if rep["retried_per_day"] is not None:
        eta = rep["days_to_min_n"]
        L.append(f"  retried requests/day (last {RATE_DAYS:g} days): {rep['retried_per_day']:.1f}"
                 + ("" if eta is None else f"; ≈ {eta:.1f} days of A/B traffic to reach "
                                           f"{rep['min_n']}/arm"))
    if rep["mde_at_min_n"] is not None:
        L.append(f"  at {rep['min_n']}/arm the detectable recovery difference is ≈ "
                 f"{100 * rep['mde_at_min_n']:.1f} pts (80% power, base = pre-v11 rate); a 10-pt "
                 f"difference needs ≈ {rep['n_per_arm_for_10pts']}/arm")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router retry-ab",
                                 description="Retry-action A/B readout (telemetry v11).")
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("report", help="per-arm P(retry recovers) with Wilson CIs (default)")
    rp.add_argument("--telemetry", type=Path)
    rp.add_argument("--since-days", type=float)
    rp.add_argument("--min-n", type=int, default=MIN_N)
    rp.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    a = args if args.cmd == "report" else rp.parse_args([])
    if a.min_n < 1:
        print("apex-router retry-ab: --min-n must be >= 1", file=sys.stderr)
        return 2
    rep = report(a.telemetry, a.since_days, a.min_n)
    print(json.dumps(rep, indent=2, default=str) if a.json else render(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
