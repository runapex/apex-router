"""Parity fixtures: pce-core (Python) is the oracle; plugin/hooks/core/*.ts and
plugin/hooks/classify.ts must reproduce these values (tests/core_*.test.ts, classify.test.ts).

    python -m apex_router.core.fixtures --write    # regenerate plugin/tests/fixtures/{core,classify}.ts
    python -m apex_router.core.fixtures --check    # exit 1 when the committed files are stale
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from apex_router.core import anomaly, cost, drift, linalg, routing, stats
from apex_router.route_log import classify_dispatch

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = ROOT / "plugin" / "tests" / "fixtures"

XS = [10, 11, 9, 10, 50, 52, 49, 51, 10, 10]
M4 = [[4.0, 1.0, 2.0, 0.5], [1.0, 3.0, 0.0, 1.0], [2.0, 0.0, 5.0, 1.5], [0.5, 1.0, 1.5, 2.0]]


def _camel_cell(c: dict) -> dict:
    return {"n": c["n"], "pass": c["pass"], "state": c["state"], "winN": c["win_n"],
            "winPass": c["win_pass"], "below": c["below"], "above": c["above"],
            "cusum": c["cusum"], "p0": c["p0"]}


def _cells() -> dict:
    ready = [1] * 40
    scenarios = {
        "floor_29": ([1] * 29, 30, 0.9),
        "all_pass_35": ([1] * 36, 30, 0.9),
        "floor_target_085": ([1] * 31, 30, 0.85),
        "anomaly_not_regime": (ready + [1] * 9 + [0], 30, 0.9),
        "streak_then_restore": (ready + ([1] * 8 + [0] * 2) * 2 + [1] * 30, 30, 0.9),
        "cusum_small_regression": (ready + ([1] * 9 + [0]) * 4, 30, 0.9),
        "rebaseline": (ready + ([1] * 5 + [0] * 5) * 4 + [1] * 3, 30, 0.9),
    }
    out = {}
    for name, (seq, min_n, target) in scenarios.items():
        c = routing.cell_new()
        states = []
        for x in seq:
            c = routing.cell_observe(c, bool(x), min_n=min_n, target=target)
            states.append(c["state"])
        out[name] = {"min_n": min_n, "target": target, "seq": seq, "states": states, "final": _camel_cell(c)}
    return out


def build_core() -> dict:
    w = stats.welford_new()
    for x in XS:
        w = stats.welford_push(w, x)
    cov_rows = [[1.0, 2.0], [2.0, 4.0], [3.0, 7.0], [4.0, 4.0], [5.0, 1.0]]
    cs = stats.welford_cov_new(2)
    for r in cov_rows:
        cs = stats.welford_cov_push(cs, r)
    ew, q16, s, sq = [], [], None, None
    for x in XS:
        s = stats.ewma(s, x, 0.2)
        sq = stats.ewma_q16(sq, x)
        ew.append(s)
        q16.append(sq)
    rank_values = [15, 20, 35, 40, 50]
    jac = []
    for A in ([[2.0, 1.0], [1.0, 2.0]], [[4.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 9.0]], M4):
        values, vectors = linalg.jacobi_eigen(A)
        jac.append({"A": A, "values": values, "vectors": vectors})
    X = [[1.0, float(x)] for x in (1, 2, 3, 4, 5, 6)]
    y = [2.1, 4.9, 8.2, 10.9, 14.1, 17.0]
    V = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    vals = [4.0, 1.0]
    cov3 = [[4.0, 2.0, 0.5], [2.0, 9.0, 1.0], [0.5, 1.0, 1.0]]
    corr, sd = anomaly.correlation_of(cov3)
    pv, pvec = anomaly.pca_fit(corr, 2)
    mad_xs = [1.0, 2.0, 3.0, 4.0, 100.0]
    med, madv = anomaly.median(mad_xs), anomaly.mad(mad_xs)
    explain_cases = [
        [10.0, 1.0, [1, 2, 3, 4, 5, 6, 7, 8, 9, 11], [1.0, 2.0]],
        [0.0, 3.0, [1.0], [1.0, 2.0]],
        [5.0, 5.0, [], []],
    ]
    zs = [-1.5] * 5 + [0.0] * 3 + [-3.0, 2.0]
    trace, sc = [], 0.0
    for z in zs:
        sc, alarm = drift.cusum_lower(sc, z)
        trace.append([sc, alarm])
    scores = [1, 9, 1, 1.5, 3, 5, 7, 1, 1, 1, 1]
    cost_xs = [1000, 2000, 3000, 4000, 5000, 6000]
    cost_ys = [0.051, 0.079, 0.112, 0.139, 0.171, 0.198]
    budget_cases = [[5.0, 360.0, 10.0], [0.0, 10.0, 10.0], [12.0, 60.0, 10.0], [1.0, 0.0, 10.0]]
    budgets = []
    for spent, minutes, budget in budget_cases:
        r = cost.budget_burn(spent, minutes, budget)
        budgets.append([spent, minutes, budget, None if r is None else {"burn": r["burn"], "minutesToExhaust": r["minutes_to_exhaust"]}])
    return {
        "welford": {"xs": XS, "n": w["n"], "mean": w["mean"], "m2": w["m2"], "var": stats.welford_variance(w)},
        "welford_cov": {"rows": cov_rows, "n": cs["n"], "mean": cs["mean"], "c": cs["c"], "cov": stats.covariance(cs)},
        "ewma": {"a": 0.2, "xs": XS, "out": ew, "q16": q16},
        "wilson": [[k, n, *stats.wilson_ci(k, n)] for k, n in
                   [(0, 1), (1, 1), (5, 10), (27, 30), (30, 30), (34, 34), (35, 35), (99, 100)]],
        "nearest_rank": {"values": rank_values,
                         "cases": [[q, stats.nearest_rank(rank_values, q)] for q in (0.05, 0.3, 0.4, 0.5, 0.95, 1.0)]},
        "jacobi": jac,
        "principal_angle": [[u, v, linalg.principal_angle(u, v)] for u, v in
                            ([[1.0, 0.0], [0.0, 1.0]], [[1.0, 0.0], [1.0, 1.0]], [[1.0, 2.0, 3.0], [1.0, 2.0, 3.1]], [[0.0, 0.0], [1.0, 0.0]])],
        "has_gap": [[v, k, linalg.has_gap(v, k)] for v, k in ([[3.0, 1.0], 1], [[2.0, 2.0], 1], [[3.0, 1.0], 0], [[5.0, 4.0, 1.0], 2])],
        "solve": {"X": X, "y": y, "ridge": 1e-6, "beta": linalg.solve_normal_equations(X, y, 1e-6)},
        "residual_trend": {"idx": [0, 1, 2, 3, 4], "resid": [0.1, -0.2, 0.05, 0.3, -0.1],
                           "slope": linalg.residual_trend([0, 1, 2, 3, 4], [0.1, -0.2, 0.05, 0.3, -0.1])},
        "anomaly": {
            "V": V, "values": vals,
            "cases": [[xc, anomaly.q_residual(xc, V), anomaly.t_squared(xc, V, vals)]
                      for xc in ([2.0, 1.0, 3.0], [0.5, -1.0, 0.2], [0.0, 0.0, 0.0])],
            "cov": cov3, "corr": corr, "sd": sd, "pca": {"values": pv, "vectors": pvec},
            "scales": {"rows": [[1.0, -4.0, 0.0], [2.0, 2.0, 0.0]], "scales": anomaly.max_abs_scales([[1.0, -4.0, 0.0], [2.0, 2.0, 0.0]])},
            "mad": {"xs": mad_xs, "median": med, "mad": madv,
                    "robust": [[x, anomaly.robust_z(x, med, madv)] for x in mad_xs]},
            "explain": [[q, t2, qh, th, anomaly.explain(q, t2, qh, th)] for q, t2, qh, th in explain_cases],
        },
        "drift": {
            "cusum": {"zs": zs, "trace": trace},
            "penalty": {"scores": scores, "trace": [list(t) for t in drift.penalty_run(scores)]},
            "variance_l1": [[a, b, drift.variance_l1(a, b)] for a, b in ([[3.0, 1.0], [2.0, 2.0]], [[5.0, 3.0, 2.0], [4.0, 4.0, 2.0]], [[0.0], [1.0]])],
        },
        "cells": _cells(),
        "cost": {"xs": cost_xs, "ys": cost_ys, "fit": cost.fit_cost(cost_xs, cost_ys), "budget": budgets},
    }


CLASSIFY_CASES = [
    ("Explore", "anything at all", None),
    (None, "Review the diff for bugs", None),
    ("general-purpose", "Implement backend Task 3: pressure", None),
    ("general-purpose", "Why does the test fail", None),
    (None, "Rename the helpers", None),
    (None, "Plan the migration", None),
    (None, "audit the auth flow", None),
    (None, "verify claims in the report", None),
    (None, "fix flaky test", None),
    (None, "root cause the crash", None),
    (None, "summarize the report", None),
    ("code-reviewer", "look at this", None),
    (None, "Debug the proxy", "and then refactor it"),
    (None, "x", "please review the change"),
    (None, None, None),
    ("Plan", "anything", None),
    ("debug", "", None),
    (None, "Build the CLI", None),
    (None, "rewrite the parser", None),
    (None, "reviewing PR 12", None),
    ("general-purpose", "Generate fixtures", None),
    (None, "explain the cache", "why is it slow"),
]


def build_classify() -> list:
    return [[st, desc, head, classify_dispatch(st, desc, head)] for st, desc, head in CLASSIFY_CASES]


def render_ts(name: str, obj) -> str:
    body = json.dumps(obj, indent=1, sort_keys=True, allow_nan=False)
    return ("// Generated by `python -m apex_router.core.fixtures --write` from pce-core. Do not edit.\n"
            f"export const {name} = {body}\n")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    files = {"core.ts": render_ts("CORE", build_core()), "classify.ts": render_ts("CLASSIFY", build_classify())}
    if "--write" in argv:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (FIXTURE_DIR / name).write_text(text)
        return 0
    stale = [n for n, t in files.items() if not (FIXTURE_DIR / n).exists() or (FIXTURE_DIR / n).read_text() != t]
    if stale:
        print("stale fixtures: " + ", ".join(stale) + " — run python -m apex_router.core.fixtures --write")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
