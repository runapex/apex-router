"""pce-core anomaly — session anomaly score (spec §7): standardize with stored (μ, σ), PCA k=2 from
stored (V_k, Λ_k), Q (residual) and T² (in-model), each normalized by its stored empirical CDF
before combining; the explain card names the term. Parameters only, never rows (A9).
plugin/hooks/core/anomaly.ts mirrors this file."""
from __future__ import annotations

import math

from apex_router.core.linalg import dot, jacobi_eigen

# χ²_k(0.99) quantiles: the T² control limit for k retained components.
CHI2_99 = {1: 6.634896601021214, 2: 9.210340371976184, 3: 11.344866730144373,
           4: 13.276704135987622, 5: 15.08627246938899}
WARMUP_N = 50     # PCA disabled until n ≥ 50 turns (rank(Σ) ≤ min(d, n−1))
HIST_MAX = 200    # stored score history for the empirical CDF


def max_abs_scales(rows) -> list:
    d = len(rows[0]) if rows else 0
    scales = []
    for j in range(d):
        m = 0.0
        for row in rows:
            m = max(m, abs(row[j]))
        scales.append(m if m > 0 else 1)
    return scales


def prescale(x, scales) -> list:
    return [xi / s for xi, s in zip(x, scales)]


def zscore(x, mean, sd) -> list:
    return [(xi - m) / (s if s > 0 else 1.0) for xi, m, s in zip(x, mean, sd)]


def median(xs) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def mad(xs) -> float:
    med = median(xs)
    return median([abs(x - med) for x in xs])


def robust_z(x: float, med: float, mad_value: float) -> float:
    """Modified z = 0.6745·(x − med)/MAD; an outlier when |z| > 3.5 (descriptive, benches only)."""
    return 0.6745 * (x - med) / mad_value if mad_value > 0 else 0.0


def correlation_of(cov):
    sd = [math.sqrt(cov[i][i]) if cov[i][i] > 0 else 0.0 for i in range(len(cov))]
    corr = [[cov[i][j] / ((sd[i] or 1.0) * (sd[j] or 1.0)) for j in range(len(cov))] for i in range(len(cov))]
    return corr, sd


def pca_fit(matrix, k: int):
    values, vectors = jacobi_eigen(matrix)
    return values[:k], vectors[:k]


def q_residual(xc, V) -> float:
    proj = [dot(v, xc) for v in V]
    total = 0.0
    for j in range(len(xc)):
        recon = 0.0
        for i, v in enumerate(V):
            recon += proj[i] * v[j]
        total += (xc[j] - recon) ** 2
    return total


def t_squared(xc, V, values) -> float:
    t = 0.0
    for v, lam in zip(V, values):
        if lam > 0:
            s = dot(v, xc)
            t += s * s / lam
    return t


def t2_limit(k: int):
    return CHI2_99.get(k)


def ecdf(value: float, history) -> float:
    if not history:
        return 0.0
    count = 0
    for h in history:
        if h <= value:
            count += 1
    return count / len(history)


def explain(q: float, t2: float, q_hist, t2_hist) -> dict:
    rq = ecdf(q, q_hist)
    rt = ecdf(t2, t2_hist)
    if rq >= rt:
        return {"score": rq, "term": "Q", "q": rq, "t2": rt}
    return {"score": rt, "term": "T2", "q": rq, "t2": rt}
