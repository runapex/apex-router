"""Zeno progress detector (RESEARCH-FIT-BACKLOG P6; DESIGN-worldmodel-P6.md §4) + G1 criterion 5.

The question: is the agent closing a shrinking share of the remaining distance, so that it
converges *short* of the goal and steps-to-goal → ∞?

**Progress signal v.** One observation per test run (`tests.ran` with a failing count):
v = 1 − failing / f_ref, f_ref = the first non-zero failing count of the task (a task whose tests
never fail has nothing to converge on). With no test runs at all: v = 1 − open-error share, one
observation per step after the first error, where an error is *open* until a later call of the
same tool succeeds; an observation is recorded only at a step that opens or closes an error.
Observations, not steps: carrying v forward over the steps between test runs (or between error
events) would insert Δ = 0 between every real change, read every quiet stretch as a stall and
break the geometric model.

**Geometric fit.** On the last w ≥ 4 observations, Δ_j = v_{j+1} − v_j, fit Δ_{j+1} ≈ r·Δ_j by
least squares through the origin in ratio (linear) space: r = Σ Δ_jΔ_{j+1} / Σ Δ_j². Not log
space: a plateau (Δ = 0, e.g. 38 → 1 → 1 → 1) and a regression (Δ < 0) are the shapes that matter
and log Δ is undefined for both; the linear fit also weights the large, well-measured early drops
over the integer-rounding noise of the tail.

**Limit** (the corrected formula): v_∞ = v_t + Δ_t / (1 − r), Δ_t = v_{t+1} − v_t the latest step;
computed as v_{t+1} + r·Δ̂_t/(1 − r) (identical when Δ̂_t = Δ_t), with Δ̂_t from the window's fit
(Δ̂_t = c·r^m, c by least squares on all the window's Δ_j) instead of the raw last difference.
With the raw one a single flat pair (failing 2 → 2) makes Δ_t = 0, v_∞ = v_t whatever r is, and a
zero-width CI below 1 — a flag from one rounding plateau. Anchoring at the observed v_{t+1} keeps
the progress already made (the fitted Δ̂_t can be smaller than the observed one). On an exact
geometric series all forms agree (0.5, 0.75, 0.875 → r = 0.5, v_∞ = 1.0).
Valid for |r| < 1 (r < 0 is a damped oscillation, labelled `oscillating`); r ≥ 1 → `not
converging` (no limit, never flagged); all Δ = 0 in the window → `stalled`, v_∞ = v_{t+1}.
**CI**: bootstrap the (Δ_j, Δ_{j+1}) pairs of the window (200 draws, seeded), refit r and Δ̂_t,
recompute v_∞; a draw with r ≥ 1 or r ≤ −1 counts as +∞ (no evidence of a short limit), a draw
of only (0, 0) pairs as stalled. 95% nearest-rank percentile CI. With 3 pairs the CI is coarse by
construction; that is the honest width.
**Flag** `converging_short` when the CI's upper bound < threshold (default 1.0: all tests pass).

**Criterion 5.** Every detector is a per-step score; it flags at the first step whose score > θ.
θ is tuned on train+val successful tasks so that ≤ 10% of them are ever flagged (θ ≥ 0 for the
Zeno score = threshold − CI upper, so the tuned rule is never looser than "CI upper < 1").
Baselines: a step-count cutoff (score = steps so far) and a wall-time cutoff (seconds since the
task's first step), tuned the same way. On the test split, among tasks that end FAIL / ESCALATE /
ABANDON (outcome partial or fail), the share the detector flags strictly before the earlier of the
two baseline cutoffs (a task the baseline never flags counts as "before" if the detector flags it
at all), and the detector's FPR on successful test tasks. Scored on gold labels; weak labels only
give a provisional number. PASS needs FPR ≤ 10% and an earlier share ≥ `min_share` (default 0.5 —
the G1 text names no share; owner sign-off owed).
"""
from __future__ import annotations

import math
import zlib

import numpy as np

from .protocol import label
from ..core.stats import wilson_ci

N_BOOT = 200
WINDOW = 4


# ---- progress signal --------------------------------------------------------------------------

def progress_signal(steps) -> tuple:
    """(kind, [(step index, v)]) — kind 'tests', 'errors' or None (no signal)."""
    obs, f_ref = [], None
    for i, s in enumerate(steps):
        t = s.get("tests") or {}
        f = t.get("failed")
        if t.get("ran") and isinstance(f, int) and f >= 0:
            if f_ref is None:
                if f == 0:
                    continue
                f_ref = f
            obs.append((i, f, 1.0 - f / f_ref))
    if obs:
        return "tests", [(i, v) for i, _, v in obs]
    if any(s.get("tests", {}).get("ran") for s in steps):
        return None, []
    open_by_tool, total, out = {}, 0, []
    for i, s in enumerate(steps):
        tool = s.get("tool") or s.get("act")
        if s.get("err"):
            open_by_tool[tool] = open_by_tool.get(tool, 0) + 1
            total += 1
        elif tool in open_by_tool:
            open_by_tool.pop(tool)
        else:
            continue                       # nothing opened or closed: not an observation
        out.append((i, 1.0 - sum(open_by_tool.values()) / total))
    return ("errors", out) if out else (None, [])


def failing_counts(steps) -> list:
    return [(i, s["tests"]["failed"]) for i, s in enumerate(steps)
            if (s.get("tests") or {}).get("ran") and isinstance(s["tests"].get("failed"), int)]


# ---- geometric fit + limit --------------------------------------------------------------------

def fit_r(v) -> float | None:
    """Least-squares ratio of consecutive progress steps; None when every Δ_j (j < last) is 0."""
    d = np.diff(np.asarray(v, dtype=float))
    a, b = d[:-1], d[1:]
    den = float(a @ a)
    return None if den == 0 else float(a @ b) / den


def limit(v_prev: float, delta: float, r: float) -> float:
    """v_∞ = v_t + Δ_t/(1 − r) for |r| < 1; +∞ otherwise (no finite limit)."""
    return v_prev + delta / (1.0 - r) if -1.0 < r < 1.0 else math.inf


def tail_limit(v_last: float, delta_hat, r):
    """The same limit anchored at the observed v_{t+1}: v_{t+1} + r·Δ̂_t/(1 − r). Equal to
    `limit(v_t, Δ_t, r)` when Δ̂_t is the observed Δ_t; with a fitted Δ̂_t it keeps the progress
    already observed and forecasts only the tail."""
    return v_last + r * delta_hat / (1.0 - r)


def fitted_last_delta(d, r):
    """Δ̂ at the window's last position under Δ_j = c·r^j, c by least squares on every Δ of the
    window. Works on an array of r (bootstrap) as well as a scalar."""
    d = np.asarray(d, dtype=float)
    j = np.arange(len(d))
    R = np.power.outer(np.atleast_1d(np.asarray(r, dtype=float)), j)      # (k, m+1)
    den = (R * R).sum(axis=1)
    c = (R @ d) / np.where(den > 0, den, 1.0)
    out = c * R[:, -1]
    return out if np.ndim(r) else float(out[0])


def zeno(v, threshold: float = 1.0, n_boot: int = N_BOOT, seed: int = 0) -> dict:
    """Zeno test on one window of progress values (≥ 3 values; the detector uses ≥ 4)."""
    v = np.asarray(v, dtype=float)
    if len(v) < 3:
        raise ValueError("need at least 3 progress values (2 consecutive Δ pairs)")
    d = np.diff(v)
    a, b = d[:-1], d[1:]
    v_prev, v_last = float(v[-2]), float(v[-1])
    if not np.any(d):
        return {"status": "stalled", "r": None, "delta": 0.0, "v_inf": v_last,
                "ci": (v_last, v_last), "converging_short": v_last < threshold, "n": len(v)}
    r = fit_r(v)
    if r is None:                                   # only the last step moved: no ratio yet
        return {"status": "insufficient", "r": None, "delta": None, "v_inf": None,
                "ci": (None, None), "converging_short": False, "n": len(v)}
    status = "not converging" if r >= 1 else "oscillating" if r < 0 else "converging"
    delta = fitted_last_delta(d, r) if -1 < r < 1 else float(d[-1])
    v_inf = tail_limit(v_last, delta, r) if -1 < r < 1 else math.inf
    rng = np.random.default_rng(seed)
    m = len(a)
    idx = rng.integers(0, m, size=(n_boot, m))
    A, Bm = a[idx], b[idx]
    den = (A * A).sum(axis=1)
    num = (A * Bm).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        rb = np.where(den > 0, num / np.where(den > 0, den, 1.0), 0.0)
        ok = (rb > -1) & (rb < 1)
        db = fitted_last_delta(d, np.where(ok, rb, 0.0))
        rs = np.where(ok, rb, 0.0)
        lim = np.where(ok, tail_limit(v_last, db, rs), np.inf)
    lim = np.where(den > 0, lim, v_last)            # a draw of only (0, 0) pairs is stalled
    lim = np.sort(lim)                              # nearest rank: +inf draws stay +inf
    lo = float(lim[int(round(0.025 * (n_boot - 1)))])
    hi = float(lim[int(round(0.975 * (n_boot - 1)))])
    short = status != "not converging" and math.isfinite(hi) and hi < threshold
    return {"status": status, "r": r, "delta": delta, "v_inf": v_inf, "ci": (lo, hi),
            "converging_short": bool(short), "n": len(v)}


def _seed(base: int, *parts) -> int:
    return (base * 1_000_003 + zlib.crc32("|".join(map(str, parts)).encode())) % (2 ** 32)


def detect(steps, w: int = WINDOW, threshold: float = 1.0, n_boot: int = N_BOOT,
           seed: int = 0, task_id: str = "") -> dict:
    """Run the sliding-window test after each progress observation (from the w-th on)."""
    if w < 4:
        raise ValueError("window must be >= 4 observations")
    kind, obs = progress_signal(steps)
    rows = []
    for j in range(w - 1, len(obs)):
        win = [x for _, x in obs[j - w + 1:j + 1]]
        z = zeno(win, threshold, n_boot, _seed(seed, task_id, j))
        rows.append({"obs": j, "step": obs[j][0], "v": obs[j][1], **z})
    return {"kind": kind, "obs": obs, "windows": rows}


def zeno_scores(steps, threshold: float = 1.0, w: int = WINDOW, seed: int = 0,
                task_id: str = "") -> np.ndarray:
    """Per-step score: threshold − CI upper at a window's step when its status can flag (not
    'not converging' / 'insufficient'), −∞ elsewhere. Score > 0 ⇔ converging_short."""
    sc = np.full(len(steps), -np.inf)
    for r in detect(steps, w, threshold, seed=seed, task_id=task_id)["windows"]:
        hi = r["ci"][1]
        if r["status"] in ("converging", "oscillating", "stalled") and hi is not None \
                and math.isfinite(hi):
            sc[r["step"]] = max(sc[r["step"]], threshold - hi)
    return sc


# ---- detectors + criterion 5 ------------------------------------------------------------------

def step_scores(steps) -> np.ndarray:
    return np.arange(1, len(steps) + 1, dtype=float)


def wall_scores(steps) -> np.ndarray:
    ts = [s.get("ts") for s in steps]
    t0 = next((t for t in ts if isinstance(t, (int, float))), None)
    if t0 is None:
        return np.full(len(steps), -np.inf)
    out, last = [], 0.0
    for t in ts:
        last = (t - t0) if isinstance(t, (int, float)) else last
        out.append(last)
    return np.array(out, dtype=float)


def first_flag(scores, theta: float):
    hit = np.nonzero(np.asarray(scores) > theta)[0]
    return int(hit[0]) if len(hit) else None


def tune_threshold(max_scores, fpr: float = 0.10, floor: float | None = None) -> float:
    """Smallest θ (from the data) with share(max score > θ) ≤ fpr; never below `floor`."""
    m = np.sort(np.asarray([x for x in max_scores], dtype=float))[::-1]
    if not len(m):
        return floor if floor is not None else math.inf
    k = int(math.floor(fpr * len(m)))
    theta = float(m[k]) if k < len(m) else -math.inf
    if floor is not None:
        theta = max(theta, floor)
    return theta


def detector_scores(ds, ids, chain=None, threshold: float = 1.0, seed: int = 0,
                    rho_window: int = 20) -> dict:
    """{detector: {task id: per-step scores}} for zeno, rho (needs a chain), steps, wall."""
    from .chains import window_rho
    out = {"zeno": {}, "rho": {}, "steps": {}, "wall": {}}
    for tid in ids:
        st = ds.steps[tid]
        out["zeno"][tid] = zeno_scores(st, threshold, seed=seed, task_id=tid)
        if chain is not None:
            out["rho"][tid] = np.nan_to_num(window_rho(chain, st, w=rho_window), nan=-np.inf)
        out["steps"][tid] = step_scores(st)
        out["wall"][tid] = wall_scores(st)
    if chain is None:
        out.pop("rho")
    return out


FLOORS = {"zeno": 0.0}


def _rate(k, n):
    return {"k": k, "n": n, "rate": k / n if n else None,
            "ci": wilson_ci(k, n) if n else (None, None)}


def criterion5(ds, scores: dict, tune_ids, test_ids, fpr: float = 0.10, min_share: float = 0.5,
               detectors=("zeno", "rho", "zeno|rho")) -> dict:
    """G1 criterion 5 from precomputed per-step scores (see `detector_scores`)."""
    def mx(name, tid):
        s = scores[name][tid]
        return float(np.max(s)) if len(s) else -math.inf

    tune_succ = [t for t in tune_ids if label(ds.tasks[t]) == 1]
    thetas = {name: tune_threshold([mx(name, t) for t in tune_succ], fpr, FLOORS.get(name))
              for name in scores}

    def flag(name, tid):
        if "|" in name:
            fs = [flag(n, tid) for n in name.split("|") if n in scores]
            fs = [f for f in fs if f is not None]
            return min(fs) if fs else None
        return first_flag(scores[name][tid], thetas[name]) if name in scores else None

    gold = [t for t in test_ids if label(ds.tasks[t]) is not None
            and ds.tasks[t].get("outcome_src") == "gold"]
    weak = [t for t in test_ids if label(ds.tasks[t]) is not None]
    scored, quality = (gold, "gold") if gold else (weak, "provisional" if weak else "inconclusive")
    bad = [t for t in scored if label(ds.tasks[t]) == 0]
    good = [t for t in scored if label(ds.tasks[t]) == 1]

    def base_flag(tid):
        fs = [f for f in (flag("steps", tid), flag("wall", tid)) if f is not None]
        return min(fs) if fs else None

    out = {"thresholds": thetas, "quality": quality, "n_bad": len(bad), "n_good": len(good),
           "fpr_target": fpr, "min_share": min_share,
           "baseline": {"recall": _rate(sum(base_flag(t) is not None for t in bad), len(bad)),
                        "fpr": _rate(sum(base_flag(t) is not None for t in good), len(good))},
           "detectors": {}}
    for name in detectors:
        if any(n not in scores for n in name.split("|")):
            continue
        earlier = 0
        for t in bad:
            f, b = flag(name, t), base_flag(t)
            earlier += 1 if (f is not None and (b is None or f < b)) else 0
        fp = sum(flag(name, t) is not None for t in good)
        e, fr = _rate(earlier, len(bad)), _rate(fp, len(good))
        if quality != "gold" or not bad or not good:
            verdict = "INCONCLUSIVE"
        else:
            verdict = "PASS" if (fr["rate"] <= fpr and e["rate"] >= min_share) else "FAIL"
        out["detectors"][name] = {
            "earlier": e, "recall": _rate(sum(flag(name, t) is not None for t in bad), len(bad)),
            "fpr": fr, "verdict": verdict}
    return out
