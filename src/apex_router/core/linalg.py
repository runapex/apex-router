"""pce-core linalg — small dense linear algebra, stdlib only (d ≤ 8: no SVD needed).

Moved from proxy_engine/tuner/readout.py: solve_normal_equations (was _solve_normal_equations,
its fixed 1e-6 ridge cushion is now the `ridge` parameter) and residual_trend (was
_polyfit_slope). New: cyclic Jacobi eigen-decomposition, eigengap test, principal angle.
plugin/hooks/core/linalg.ts mirrors this file.
"""
from __future__ import annotations

import math


def dot(a, b) -> float:
    s = 0.0
    for x, y in zip(a, b):
        s += x * y
    return s


def solve_normal_equations(X: list[list[float]], y: list[float], ridge: float = 1e-6) -> list[float]:
    """Solve min‖Xβ − y‖ via the normal equations (XᵀX + ridge·I)β = Xᵀy with Gaussian elimination
    + partial pivoting. A singular/degenerate system falls back on the ridge cushion so the fit never
    throws — the caller reports R² so a poor fit is visible, not hidden."""
    n = len(X)
    p = len(X[0]) if n else 0
    A = [[0.0] * p for _ in range(p)]
    b = [0.0] * p
    for i in range(n):
        xi = X[i]
        yi = y[i]
        for a in range(p):
            b[a] += xi[a] * yi
            xia = xi[a]
            row = A[a]
            for c in range(p):
                row[c] += xia * xi[c]
    for a in range(p):
        A[a][a] += ridge
    for col in range(p):
        piv = max(range(col, p), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-12:
            continue
        if piv != col:
            A[col], A[piv] = A[piv], A[col]
            b[col], b[piv] = b[piv], b[col]
        pivval = A[col][col]
        for r in range(p):
            if r == col:
                continue
            factor = A[r][col] / pivval
            if factor == 0.0:
                continue
            for c in range(col, p):
                A[r][c] -= factor * A[col][c]
            b[r] -= factor * b[col]
    return [b[i] / A[i][i] if abs(A[i][i]) > 1e-12 else 0.0 for i in range(p)]


def residual_trend(idx: list[float], resid: list[float]) -> float:
    """Slope of the least-squares line resid ~ a·idx + b (the residual trend). Closed form."""
    n = len(idx)
    if n < 2:
        return 0.0
    mx = sum(idx) / n
    my = sum(resid) / n
    num = sum((idx[i] - mx) * (resid[i] - my) for i in range(n))
    den = sum((idx[i] - mx) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0


def jacobi_eigen(A, tol: float = 1e-12, max_sweeps: int = 64):
    """Cyclic Jacobi for a symmetric matrix. Stops when the off-diagonal mass is ≤ tol² of the
    Frobenius mass (relative tolerance). Returns (values descending, vectors as rows), each vector
    sign-normalised so its first nonzero component is positive."""
    n = len(A)
    a = [[float(x) for x in row] for row in A]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(max_sweeps):
        off = 0.0
        for p in range(n):
            for q in range(p + 1, n):
                off += a[p][q] * a[p][q]
        total = 0.0
        for i in range(n):
            for j in range(n):
                total += a[i][j] * a[i][j]
        if total == 0.0 or off <= tol * tol * total:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                apq = a[p][q]
                if apq == 0.0:
                    continue
                theta = (a[q][q] - a[p][p]) / (2.0 * apq)
                t = (1.0 if theta >= 0 else -1.0) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for k in range(n):
                    akp, akq = a[k][p], a[k][q]
                    a[k][p] = c * akp - s * akq
                    a[k][q] = s * akp + c * akq
                for k in range(n):
                    apk, aqk = a[p][k], a[q][k]
                    a[p][k] = c * apk - s * aqk
                    a[q][k] = s * apk + c * aqk
                for k in range(n):
                    vkp, vkq = v[k][p], v[k][q]
                    v[k][p] = c * vkp - s * vkq
                    v[k][q] = s * vkp + c * vkq
    order = sorted(range(n), key=lambda i: -a[i][i])
    values = [a[i][i] for i in order]
    vectors = []
    for i in order:
        col = [v[k][i] for k in range(n)]
        lead = next((x for x in col if abs(x) > 1e-12), 0.0)
        if lead < 0:
            col = [-x for x in col]
        vectors.append(col)
    return values, vectors


def has_gap(values, k: int, rel: float = 1e-6) -> bool:
    """True when λ_k − λ_{k+1} (1-based k) exceeds rel·|λ_1|: the top-k subspace is well defined."""
    if k <= 0 or k >= len(values):
        return False
    scale = max(abs(values[0]), 5e-324)
    return values[k - 1] - values[k] > rel * scale


def principal_angle(u, w) -> float:
    """θ = arccos(|u·w| / (|u||w|)) in radians; a zero vector is orthogonal to everything."""
    nu = math.sqrt(dot(u, u))
    nw = math.sqrt(dot(w, w))
    if nu == 0.0 or nw == 0.0:
        return math.pi / 2
    c = min(1.0, abs(dot(u, w)) / (nu * nw))
    return math.acos(c)
