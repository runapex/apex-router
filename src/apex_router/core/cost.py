"""pce-core cost — per-cell OLS `cost ≈ a + b·input_tokens` with R² and the residual-trend alarm
(the readout's tokens-on-bytes fit, generalised), and the budget burn of spec §7.
plugin/hooks/core/cost.ts mirrors this file."""
from __future__ import annotations

from apex_router.core.linalg import residual_trend, solve_normal_equations


def fit_cost(xs, ys):
    n = len(xs)
    if n < 3 or len(ys) != n:
        return None
    X = [[1.0, float(x)] for x in xs]
    a, b = solve_normal_equations(X, [float(y) for y in ys])
    resid = [ys[i] - (a + b * xs[i]) for i in range(n)]
    mean_y = 0.0
    for y in ys:
        mean_y += y
    mean_y /= n
    ss_res = 0.0
    ss_tot = 0.0
    for i in range(n):
        ss_res += resid[i] ** 2
        ss_tot += (ys[i] - mean_y) ** 2
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"a": a, "b": b, "r2": r2, "trend": residual_trend(list(range(n)), resid), "n": n}


def budget_burn(spent_usd: float, minutes: float, budget_usd: float):
    """burn = spend_rate / (budget/1440); minutes_to_exhaust = remaining / spend_rate."""
    if budget_usd <= 0 or minutes <= 0:
        return None
    rate = spent_usd / minutes
    burn = rate / (budget_usd / 1440)
    mte = max(0.0, budget_usd - spent_usd) / rate if rate > 0 else None
    return {"burn": burn, "minutes_to_exhaust": mte}
