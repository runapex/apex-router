"""Zeno progress detector (RESEARCH-FIT-BACKLOG P6; DESIGN-worldmodel-P6.md §4) + G1 criterion 5.

The question: is the agent closing a shrinking share of the remaining distance, so that it
converges *short* of the goal and steps-to-goal → ∞?

**Progress signal v.** One observation per test run (`tests.ran` with a failing count):
v = 1 − failing / f_ref, f_ref = the first non-zero failing count of the task (a task whose tests
never fail has nothing to converge on). With no test runs at all: v = 1 − open-error share, where
an error is *open* until a later call of the same tool succeeds; an observation is recorded only at
a step that opens or closes an error. Observations, not steps: carrying v forward over the steps
between test runs would insert Δ = 0 between every real change and read every quiet stretch as a
stall. **Resolution** q: v moves in units of 1/f_ref (one test; 1/errors for the error signal), so
a limit is known no better than ±q/2.

**Geometric fit.** On the last w ≥ 4 observations, Δ_j = v_{j+1} − v_j, fit Δ_{j+1} ≈ r·Δ_j by
least squares through the origin in ratio (linear) space: r = Σ Δ_jΔ_{j+1} / Σ Δ_j². Not log
space: plateaus (Δ = 0) and regressions (Δ < 0) occur and log Δ is undefined for both; the linear
fit also weights the large, well-measured early drops over the integer noise of the tail.

**Limit** (the corrected formula): v_∞ = v_t + Δ_t / (1 − r), Δ_t = v_{t+1} − v_t the latest step;
computed as v_{t+1} + r·Δ̂_t/(1 − r) (identical when Δ̂_t = Δ_t), with Δ̂_t = c·r^m from the
window's fit (c by least squares on all its Δ_j). On an exact geometric series every form agrees
(0.5, 0.75, 0.875 → r = 0.5, v_∞ = 1.0).

**Status of a window** (only the first two can raise the Zeno flag):
  `converging`     0 ≤ r < 1 and the latest Δ > 0;
  `oscillating`    −1 < r < 0 and the latest Δ > 0 (a damped oscillation still has a limit);
  `regressing`     the latest Δ < 0 (getting worse is not converging short — reported, no flag);
  `plateau`        the latest Δ = 0 but the window is not flat (38 → 1 → 1 → 1, 8 → 4 → 2 → 2):
                   a geometric decay never hits an exact 0 step, so a plateau is not Zeno
                   evidence — it waits for the next run (stricter than "≥ 1 non-zero Δ among
                   the last two pairs": with that rule 8,4,2,2 flags and 10,5,2,2 does not);
  `stalled`        every Δ in the window is 0 (1 → 1 → 1 → 1): the *stuck* detector, scored
                   separately from Zeno in criterion 5;
  `not converging` r ≥ 1 (no limit);  `insufficient`  no ratio yet (all but the last Δ are 0).
**CI**: residual bootstrap — residuals of the c·r^j fit resampled onto the fitted Δ's (200 draws,
seeded per task and window), r, c, Δ̂_t refitted per draw, a draw with r outside (−1, 1) counting
as +∞; 95% nearest-rank percentiles, then widened by the resolution ±q/2. A 2–3-pair window that
fits exactly (equal ratios: 10, 6, 8, 7) has zero residuals; the resolution floor is what keeps
its CI from being a point.
**Flag** `converging_short` when the status can flag and the CI's upper bound < threshold
(default 1.0: all tests pass).

**Criterion 5** (rule: coordinator ruling 2026-10-07, pending owner sign-off). Every detector is a
per-step score and flags at the first step whose score > θ; θ is tuned on train+val successful
tasks so that ≤ 10% of them are ever flagged (θ ≥ 0 for Zeno / stalled, so the tuned rule is never
looser than "CI upper < 1"). Baselines: step-count and wall-time cutoffs tuned the same way;
f_b = the earlier of the two (∞ if neither fires). Per bad gold test task (outcome fail / partial,
i.e. FAIL / ESCALATE / ABANDON) with first detector flag f_d (∞ if none) and length L:
win = f_d < f_b and f_d ≤ L − 2;  loss = f_b < f_d;  steps saved = min(f_b, L) − min(f_d, L).
PASS iff the Wilson upper bound of the test FPR on successful tasks ≤ 10%, the Wilson lower bound
of wins/(wins + losses) > 0.5, and the median steps saved over bad tasks > 0. Weak labels only →
provisional numbers, INCONCLUSIVE. Combined detectors (`zeno|rho`, …) use each part's own θ and
are not re-tuned: their tuned FPR can reach ~20%; the test-FPR gate still applies.
"""
from __future__ import annotations

import math
import zlib

import numpy as np

from .protocol import label
from ..core.stats import wilson_ci

N_BOOT = 200
WINDOW = 4
FLAGGING = ("converging", "oscillating")


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
    if any((s.get("tests") or {}).get("ran") for s in steps):
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


def resolution(steps) -> float:
    """One unit of the progress signal: 1/f_ref for tests, 1/(errors seen) for the error signal."""
    kind, _ = progress_signal(steps)
    if kind == "tests":
        f_ref = next(f for _, f in failing_counts(steps) if f > 0)
        return 1.0 / f_ref
    if kind == "errors":
        return 1.0 / max(1, sum(1 for s in steps if s.get("err")))
    return 0.0


def failing_counts(steps) -> list:
    out = []
    for i, s in enumerate(steps):
        t = s.get("tests") or {}
        if t.get("ran") and isinstance(t.get("failed"), int):
            out.append((i, t["failed"]))
    return out


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
    """The same limit anchored at the observed v_{t+1}: v_{t+1} + r·Δ̂_t/(1 − r)."""
    return v_last + r * delta_hat / (1.0 - r)


def _geo_fit(D, r):
    """Row-wise c = argmin Σ (Δ_j − c·r^j)² for Δ rows D (k, m+1) and ratios r (k,)."""
    j = np.arange(D.shape[1])
    R = np.power.outer(r, j)
    den = (R * R).sum(axis=1)
    return (R * D).sum(axis=1) / np.where(den > 0, den, 1.0), R


def fitted_last_delta(d, r):
    """Δ̂ at the window's last position under Δ_j = c·r^j (scalar r or an array of r)."""
    d = np.asarray(d, dtype=float)
    rr = np.atleast_1d(np.asarray(r, dtype=float))
    c, R = _geo_fit(np.broadcast_to(d, (len(rr), len(d))), rr)
    out = c * R[:, -1]
    return out if np.ndim(r) else float(out[0])


def _ratios(D):
    a, b = D[:, :-1], D[:, 1:]
    den = (a * a).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(den > 0, (a * b).sum(axis=1) / np.where(den > 0, den, 1.0), np.nan)


def zeno(v, threshold: float = 1.0, n_boot: int = N_BOOT, seed: int = 0,
         resolution: float | None = None) -> dict:
    """Zeno test on one window of progress values (≥ 3 values; the detector uses ≥ 4).

    `resolution` q: the signal's unit (`detect` passes 1/f_ref); default = the smallest non-zero
    |Δ| in the window, the finest step the data shows. The CI is widened by ±q/2.
    """
    v = np.asarray(v, dtype=float)
    if len(v) < 3:
        raise ValueError("need at least 3 progress values (2 consecutive Δ pairs)")
    d = np.diff(v)
    v_last = float(v[-1])
    nz = np.abs(d[d != 0])
    q = float(nz.min()) if resolution is None and len(nz) else float(resolution or 0.0)
    base = {"r": None, "delta": None, "v_inf": None, "ci": (None, None),
            "converging_short": False, "stalled": False, "n": len(v), "resolution": q}
    if not np.any(d):
        return {**base, "status": "stalled", "delta": 0.0, "v_inf": v_last,
                "ci": (v_last, v_last), "stalled": v_last < threshold}
    if d[-1] < 0:
        return {**base, "status": "regressing", "r": fit_r(v), "delta": float(d[-1])}
    if d[-1] == 0:
        return {**base, "status": "plateau", "r": fit_r(v), "delta": 0.0}
    r = fit_r(v)
    if r is None:                                   # only the last step moved: no ratio yet
        return {**base, "status": "insufficient"}
    if r >= 1:
        return {**base, "status": "not converging", "r": r, "delta": float(d[-1]),
                "v_inf": math.inf, "ci": (math.inf, math.inf)}
    if r <= -1:
        return {**base, "status": "oscillating", "r": r, "delta": float(d[-1]),
                "v_inf": math.inf, "ci": (math.inf, math.inf)}
    status = "oscillating" if r < 0 else "converging"
    c, R = _geo_fit(d[None, :], np.array([r]))
    fitted = c[0] * R[0]
    delta = float(fitted[-1])
    v_inf = float(tail_limit(v_last, delta, r))
    resid = d - fitted
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(n_boot, len(d)))
    D = fitted[None, :] + resid[idx]
    rb = _ratios(D)
    ok = np.isfinite(rb) & (rb > -1) & (rb < 1)
    rs = np.where(ok, rb, 0.0)
    cb, Rb = _geo_fit(D, rs)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        lim = np.where(ok, tail_limit(v_last, cb * Rb[:, -1], rs), np.inf)
    lim = np.sort(lim)                              # nearest rank: +inf draws stay +inf
    lo = float(lim[int(round(0.025 * (n_boot - 1)))]) - q / 2
    hi = float(lim[int(round(0.975 * (n_boot - 1)))]) + q / 2
    short = math.isfinite(hi) and hi < threshold
    return {**base, "status": status, "r": r, "delta": delta, "v_inf": v_inf, "ci": (lo, hi),
            "converging_short": bool(short)}


def _seed(base: int, *parts) -> int:
    return (base * 1_000_003 + zlib.crc32("|".join(map(str, parts)).encode())) % (2 ** 32)


def detect(steps, w: int = WINDOW, threshold: float = 1.0, n_boot: int = N_BOOT,
           seed: int = 0, task_id: str = "") -> dict:
    """Run the sliding-window test after each progress observation (from the w-th on)."""
    if w < 4:
        raise ValueError("window must be >= 4 observations")
    kind, obs = progress_signal(steps)
    q = resolution(steps)
    rows = []
    for j in range(w - 1, len(obs)):
        win = [x for _, x in obs[j - w + 1:j + 1]]
        z = zeno(win, threshold, n_boot, _seed(seed, task_id, j), resolution=q)
        rows.append({"obs": j, "step": obs[j][0], "v": obs[j][1], **z})
    return {"kind": kind, "obs": obs, "windows": rows, "resolution": q}


def window_scores(steps, threshold: float = 1.0, w: int = WINDOW, seed: int = 0,
                  task_id: str = "") -> tuple:
    """(zeno, stalled) per-step scores, −∞ where nothing can flag. Zeno: threshold − CI upper at
    a converging/oscillating window (> 0 ⇔ converging_short). Stalled: threshold − v at a flat
    window (> 0 ⇔ stuck below the goal)."""
    zs = np.full(len(steps), -np.inf)
    ss = np.full(len(steps), -np.inf)
    for r in detect(steps, w, threshold, seed=seed, task_id=task_id)["windows"]:
        if r["status"] in FLAGGING and math.isfinite(r["ci"][1]):
            zs[r["step"]] = max(zs[r["step"]], threshold - r["ci"][1])
        elif r["status"] == "stalled":
            ss[r["step"]] = max(ss[r["step"]], threshold - r["v"])
    return zs, ss


def zeno_scores(steps, threshold: float = 1.0, w: int = WINDOW, seed: int = 0,
                task_id: str = "") -> np.ndarray:
    return window_scores(steps, threshold, w, seed, task_id)[0]


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
    """{detector: {task id: per-step scores}} for zeno, stalled, rho (needs a chain), steps, wall."""
    from .chains import window_rho
    out = {"zeno": {}, "stalled": {}, "rho": {}, "steps": {}, "wall": {}}
    for tid in ids:
        st = ds.steps[tid]
        out["zeno"][tid], out["stalled"][tid] = window_scores(st, threshold, seed=seed,
                                                              task_id=tid)
        if chain is not None:
            out["rho"][tid] = np.nan_to_num(window_rho(chain, st, w=rho_window), nan=-np.inf)
        out["steps"][tid] = step_scores(st)
        out["wall"][tid] = wall_scores(st)
    if chain is None:
        out.pop("rho")
    return out


FLOORS = {"zeno": 0.0, "stalled": 0.0}
DETECTORS = ("zeno", "stalled", "rho", "zeno|stalled", "zeno|rho")


def _rate(k, n):
    return {"k": k, "n": n, "rate": k / n if n else None,
            "ci": wilson_ci(k, n) if n else (None, None)}


def criterion5(ds, scores: dict, tune_ids, test_ids, fpr: float = 0.10,
               detectors=DETECTORS) -> dict:
    """G1 criterion 5 from precomputed per-step scores (see `detector_scores` and the module
    docstring for the win / loss / steps-saved rule)."""
    def mx(name, tid):
        s = scores[name][tid]
        return float(np.max(s)) if len(s) else -math.inf

    tune_succ = [t for t in tune_ids if label(ds.tasks[t]) == 1]
    thetas = {name: tune_threshold([mx(name, t) for t in tune_succ], fpr, FLOORS.get(name))
              for name in scores}

    def flag(name, tid):
        if "|" in name:
            fs = [flag(n, tid) for n in name.split("|")]
            fs = [f for f in fs if f is not None]
            return min(fs) if fs else None
        return first_flag(scores[name][tid], thetas[name])

    gold = [t for t in test_ids if label(ds.tasks[t]) is not None
            and ds.tasks[t].get("outcome_src") == "gold"]
    labeled = [t for t in test_ids if label(ds.tasks[t]) is not None]
    scored, quality = (gold, "gold") if gold else (
        labeled, "provisional" if labeled else "inconclusive")
    bad = [t for t in scored if label(ds.tasks[t]) == 0]
    good = [t for t in scored if label(ds.tasks[t]) == 1]

    def base_flag(tid):
        fs = [f for f in (flag("steps", tid), flag("wall", tid)) if f is not None]
        return min(fs) if fs else None

    out = {"thresholds": thetas, "quality": quality, "n_bad": len(bad), "n_good": len(good),
           "fpr_target": fpr, "rule": "pending owner sign-off",
           "baseline": {"recall": _rate(sum(base_flag(t) is not None for t in bad), len(bad)),
                        "fpr": _rate(sum(base_flag(t) is not None for t in good), len(good))},
           "detectors": {}}
    inf = math.inf
    for name in detectors:
        if any(n not in scores for n in name.split("|")):
            continue
        wins = losses = 0
        saved = []
        for t in bad:
            L = len(ds.steps[t])
            fd, fb = flag(name, t), base_flag(t)
            fd_, fb_ = (inf if fd is None else fd), (inf if fb is None else fb)
            if fd_ < fb_ and fd_ <= L - 2:
                wins += 1
            elif fb_ < fd_:
                losses += 1
            saved.append(min(fb_, L) - min(fd_, L))
        fp = sum(flag(name, t) is not None for t in good)
        w, fr = _rate(wins, wins + losses), _rate(fp, len(good))
        med = float(np.median(saved)) if saved else None
        if quality != "gold" or not bad or not good:
            verdict = "INCONCLUSIVE"
        else:
            ok = (fr["ci"][1] <= fpr and w["n"] > 0 and w["ci"][0] > 0.5
                  and med is not None and med > 0)
            verdict = "PASS" if ok else "FAIL"
        out["detectors"][name] = {
            "wins": wins, "losses": losses, "win_share": w, "median_saved": med,
            "recall": _rate(sum(flag(name, t) is not None for t in bad), len(bad)),
            "fpr": fr, "retuned": "|" not in name, "verdict": verdict}
    return out
