"""First-week shadow readout — R1 wire-usage regression + read:write distribution + json/xl watch.

The Stage-A deliverable (roadmap §Step 4): reconcile the compiler's byte-level predictions against
the provider's own token accounting, on OUR traffic. Three products, each computed from the shadow
telemetry jsonl (the append-only sink apex writes in shadow mode — the SAME lines the TUI consumes):

  R1  Wire-usage regression: y = usage.input_tokens ~ Σ_class eff_tokens_per_byte[c] · bytes[c].
      Fit by ordinary least squares (normal equations). The coefficients are the first on-policy
      measurement of tokens/byte/class; the residuals' stationarity is the tokenizer-convention
      alarm. Ships with a +/- CONTROL (roadmap R1 admission bar): inject synthetic drift → the alarm
      must fire, else the instrument is untrusted.

  RW  Read:write distribution vs the [6:1, 30:1] band — shadow deliverable #2 as numbers.
      A week of traffic collapses the band's width (always a variance claim) to a measured
      distribution: mean, quantiles, and the fraction inside the band.

  XL  json/xl predicted-vs-denominator: the compiler's first live prediction (predicted_bytes_saved)
      meeting the real request count it applies to.

This is ANALYTICS-plane code (lives in apex_router.proxy_engine.tuner) — it never runs on the hot path. Realized data
(usage) never feeds the compiler's `expected` (arbiter separation, roadmap standing rule): this
reads telemetry and REPORTS; it does not recompile policy.

HONESTY: until the shadow week runs on live traffic, the only input is the replay/synthetic fixture,
so every number here is DRY-RUN — a validation that the harness computes correctly, not a real
readout. `Readout.provenance` records whether the corpus is live-shadow or a fixture; the CLI prints
it. A real readout requires real shadow telemetry, which needs the wire switch (an operator step).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# PURE-PYTHON by design: the rest of apex_router.proxy_engine.tuner (compiler, cachesim) is numpy-free and the whole
# suite runs without the `tuner` extra installed, so this harness must too — an instrument that
# silently skips when numpy is absent is worse than none. OLS here is a tiny normal-equations solve
# (class count ~7), well within a hand-rolled Gaussian elimination; no numpy/scipy dependency.

# The calibration band the compiler's sign-stability has bracketed since round one (read:write).
BAND_LO = 6.0
BAND_HI = 30.0


@dataclass
class R1Fit:
    """The wire-usage regression result. `coef` maps content class → eff tokens/byte (the slope);
    `intercept` absorbs fixed per-request overhead. `r2`, `residual_std`, and the residual `trend`
    (slope of residual vs request index, ~0 if stationary) are the drift-alarm inputs."""

    classes: list[str]
    coef: dict[str, float]
    intercept: float
    n: int
    r2: float
    residual_std: float
    residual_trend: float  # slope of residuals over request order; |trend| large ⇒ drift
    drift_alarm: bool

    def to_dict(self) -> dict:
        return {
            "classes": self.classes,
            "coef_tokens_per_byte": {k: round(v, 6) for k, v in self.coef.items()},
            "intercept": round(self.intercept, 3),
            "n": self.n,
            "r2": round(self.r2, 4),
            "residual_std": round(self.residual_std, 3),
            "residual_trend": round(self.residual_trend, 6),
            "drift_alarm": self.drift_alarm,
        }


@dataclass
class RWDist:
    n: int
    mean: float
    median: float
    p10: float
    p90: float
    frac_in_band: float

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "mean": round(self.mean, 2),
            "median": round(self.median, 2),
            "p10": round(self.p10, 2),
            "p90": round(self.p90, 2),
            "frac_in_band": round(self.frac_in_band, 3),
            "band": [BAND_LO, BAND_HI],
        }


@dataclass
class XLWatch:
    requests: int  # json/xl blocks seen
    emitted: int  # of those, how many chose emit
    predicted_bytes_saved: int

    def to_dict(self) -> dict:
        return {
            "json_xl_requests": self.requests,
            "json_xl_emitted": self.emitted,
            "predicted_bytes_saved": self.predicted_bytes_saved,
        }


@dataclass
class Readout:
    provenance: str  # "live-shadow" | "fixture:<path>" — a real readout needs live-shadow
    n_requests: int
    r1: R1Fit | None
    rw: RWDist | None
    xl: XLWatch
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "provenance": self.provenance,
            "dry_run": not self.provenance.startswith("live-shadow"),
            "n_requests": self.n_requests,
            "r1": self.r1.to_dict() if self.r1 else None,
            "read_write": self.rw.to_dict() if self.rw else None,
            "json_xl": self.xl.to_dict(),
            "notes": self.notes,
        }


# --- telemetry loading ---------------------------------------------------------------------------


def load_request_lines(path: str) -> list[dict]:
    """Read the shadow telemetry jsonl → request-event dicts (heartbeats and malformed lines
    dropped). Fail-open per line: a bad line is skipped, never fatal."""
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(d, dict) and d.get("ev") != "hb" and "shadow" in d:
                out.append(d)
    return out


# --- pure-python least squares (normal equations + Gaussian elimination) -------------------------


def _solve_normal_equations(X: list[list[float]], y: list[float]) -> list[float]:
    """Solve min‖Xβ − y‖ via the normal equations (XᵀX)β = Xᵀy with Gaussian elimination + partial
    pivoting. X is n×p (p small — the class count + intercept), so the p×p solve is trivial. A
    singular/degenerate system (collinear classes) falls back to a small ridge (+1e-6·I) so the fit
    never throws — the caller reports R² so a poor fit is visible, not hidden."""
    n = len(X)
    p = len(X[0]) if n else 0
    # A = XᵀX (p×p), b = Xᵀy (p)
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
    # ridge cushion against singularity
    for a in range(p):
        A[a][a] += 1e-6
    # Gaussian elimination with partial pivoting on the augmented [A | b]
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


def _polyfit_slope(idx: list[float], resid: list[float]) -> float:
    """Slope of the least-squares line resid ~ a·idx + b (the residual trend). Closed form."""
    n = len(idx)
    if n < 2:
        return 0.0
    mx = sum(idx) / n
    my = sum(resid) / n
    num = sum((idx[i] - mx) * (resid[i] - my) for i in range(n))
    den = sum((idx[i] - mx) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0


def _std(xs: list[float]) -> float:
    n = len(xs)
    if n == 0:
        return 0.0
    m = sum(xs) / n
    return (sum((x - m) ** 2 for x in xs) / n) ** 0.5


def _percentile(sorted_xs: list[float], q: float) -> float:
    """Linear-interpolation percentile on an already-sorted list (q in [0,100])."""
    if not sorted_xs:
        return 0.0
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = (q / 100.0) * (len(sorted_xs) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(sorted_xs):
        return sorted_xs[-1]
    return sorted_xs[lo] * (1 - frac) + sorted_xs[lo + 1] * frac


# --- R1: wire-usage regression -------------------------------------------------------------------


def fit_r1(rows: list[dict], *, drift_trend_threshold: float = 0.0) -> R1Fit | None:
    """OLS fit of provider input_tokens on per-class emitted bytes (normal equations via lstsq).

    y_i = usage.input_tokens for request i (the provider's truth — R1's regressand).
    X_i = [bytes_by_class[c] for c in classes] + [1] (intercept).
    Only requests with a captured usage.input_tokens contribute (else y is unknown).

    The residual TREND (slope of residual vs request order) is the drift signal: a stationary
    tokenizer convention gives ~0 trend; a drifting one trends. `drift_alarm` fires when
    |trend| exceeds `drift_trend_threshold` (0.0 here means "any nonzero-by-fit trend is reported";
    the +/- control test sets a real threshold and injects drift to confirm the alarm fires).
    """
    xs: list[dict[str, float]] = []
    ys: list[float] = []
    for d in rows:
        usage = d.get("usage") or {}
        shadow = d.get("shadow") or {}
        if not usage.get("captured"):
            continue
        y = usage.get("input_tokens")
        bbc = shadow.get("bytes_by_class") or {}
        if not y or not bbc:
            continue
        xs.append({k: float(v) for k, v in bbc.items()})
        ys.append(float(y))
    if len(ys) < 3:
        return None
    classes = sorted({c for x in xs for c in x})
    X = [[x.get(c, 0.0) for c in classes] + [1.0] for x in xs]  # design matrix (+intercept col)
    beta = _solve_normal_equations(X, ys)  # pure-python OLS
    pred = [sum(X[i][j] * beta[j] for j in range(len(beta))) for i in range(len(ys))]
    resid = [ys[i] - pred[i] for i in range(len(ys))]
    ymean = sum(ys) / len(ys)
    ss_res = sum(r * r for r in resid)
    ss_tot = sum((v - ymean) ** 2 for v in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    idx = [float(i) for i in range(len(resid))]
    trend = _polyfit_slope(idx, resid)  # residual trend = drift signal
    resid_std = _std(resid)
    alarm = abs(trend) > drift_trend_threshold if drift_trend_threshold > 0 else False
    return R1Fit(
        classes=classes,
        coef={c: float(beta[i]) for i, c in enumerate(classes)},
        intercept=float(beta[-1]),
        n=len(ys),
        r2=r2,
        residual_std=resid_std,
        residual_trend=trend,
        drift_alarm=alarm,
    )


# --- RW: read:write distribution -----------------------------------------------------------------


def read_write_dist(rows: list[dict]) -> RWDist | None:
    ratios: list[float] = []
    for d in rows:
        w = d.get("cache_write_tokens") or 0
        r = d.get("cache_read_tokens") or 0
        if w > 0:
            ratios.append(r / w)
    if not ratios:
        return None
    s = sorted(ratios)
    in_band = sum(1 for r in ratios if BAND_LO <= r <= BAND_HI) / len(ratios)
    return RWDist(
        n=len(ratios),
        mean=sum(ratios) / len(ratios),
        median=_percentile(s, 50),
        p10=_percentile(s, 10),
        p90=_percentile(s, 90),
        frac_in_band=in_band,
    )


# --- XL: json/xl watch ---------------------------------------------------------------------------


def json_xl_watch(rows: list[dict]) -> XLWatch:
    req = emit = saved = 0
    for d in rows:
        for blk in (d.get("shadow") or {}).get("blocks", []):
            if blk.get("cell") == "json/xl":
                req += 1
                saved += int(blk.get("bytes_saved", 0))
                if blk.get("reason") == "emit":
                    emit += 1
    return XLWatch(requests=req, emitted=emit, predicted_bytes_saved=saved)


# --- top-level -----------------------------------------------------------------------------------


def build_readout(path: str, *, provenance: str, drift_trend_threshold: float = 0.0) -> Readout:
    rows = load_request_lines(path)
    notes: list[str] = []
    if not provenance.startswith("live-shadow"):
        notes.append(
            "DRY-RUN: computed on a fixture, not live shadow traffic. The harness is validated; "
            "the NUMBERS are not a real readout — a real readout needs the wire switch."
        )
    r1 = fit_r1(rows, drift_trend_threshold=drift_trend_threshold)
    if r1 is None:
        notes.append("R1 not fit: fewer than 3 requests with captured usage.input_tokens.")
    rw = read_write_dist(rows)
    if rw is None:
        notes.append("read:write not computed: no requests with a cache write.")
    xl = json_xl_watch(rows)
    return Readout(
        provenance=provenance,
        n_requests=len(rows),
        r1=r1,
        rw=rw,
        xl=xl,
        notes=notes,
    )


def format_readout(r: Readout) -> str:
    """Human-readable readout for the CLI / weekly report."""
    lines = [
        f"apex shadow readout · provenance={r.provenance} · "
        f"{'DRY-RUN (fixture)' if not r.provenance.startswith('live-shadow') else 'LIVE'}",
        f"  requests analyzed: {r.n_requests}",
    ]
    if r.r1:
        lines.append(
            f"  R1 wire-usage: n={r.r1.n} R²={r.r1.r2:.3f} resid_std={r.r1.residual_std:.1f} "
            f"resid_trend={r.r1.residual_trend:.2e} drift_alarm={r.r1.drift_alarm}"
        )
        for c in r.r1.classes:
            lines.append(f"    tokens/byte[{c}] = {r.r1.coef[c]:.4f}")
        lines.append(f"    intercept = {r.r1.intercept:.1f}")
    else:
        lines.append("  R1 wire-usage: not fit (insufficient captured-usage rows)")
    if r.rw:
        lines.append(
            f"  read:write: mean={r.rw.mean:.1f}:1 median={r.rw.median:.1f} "
            f"[p10={r.rw.p10:.1f}, p90={r.rw.p90:.1f}] in-band[{BAND_LO:.0f},{BAND_HI:.0f}]="
            f"{r.rw.frac_in_band * 100:.0f}%"
        )
    else:
        lines.append("  read:write: no cache-write samples")
    lines.append(
        f"  json/xl: {r.xl.emitted}/{r.xl.requests} emit · predicted saved "
        f"{r.xl.predicted_bytes_saved / 1024:.1f}KB"
    )
    for n in r.notes:
        lines.append(f"  ! {n}")
    return "\n".join(lines)


def _inject_drift(rows: list[dict], slope: float) -> list[dict]:
    """+/- CONTROL helper (roadmap R1 admission bar): return a copy of rows with a synthetic
    tokenizer drift injected — scale each request's usage.input_tokens by (1 + slope·i/n), so the
    residual trend grows monotonically. If `fit_r1` with a real threshold does NOT alarm on
    drifted data (and does NOT on clean data), the instrument is untrusted. Used by the test,
    here so the control is part of the module, not hidden in the test."""
    out = []
    n = max(1, len(rows))
    for i, d in enumerate(rows):
        d2 = dict(d)
        usage = dict(d.get("usage") or {})
        if usage.get("captured") and usage.get("input_tokens"):
            usage["input_tokens"] = int(usage["input_tokens"] * (1.0 + slope * i / n))
        d2["usage"] = usage
        out.append(d2)
    return out
