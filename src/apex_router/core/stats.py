"""pce-core stats — the one copy of the numeric primitives (stdlib only).

Moved here unchanged from apex_router.stats (which re-exports them): wilson_ci,
benjamini_hochberg, paired_bootstrap_ci, paired_bootstrap_pvalue.
New: Welford running moments (scalar and covariance), EWMA (float and Q16 fixed point), and the
one nearest-rank percentile. plugin/hooks/core/stats.ts mirrors the in-engine subset.
"""
from __future__ import annotations

import math
import random
from typing import List, Tuple

def wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion k successes in n trials.
    Returns (lo, hi), both clamped to [0,1]. Raises ValueError if n <= 0.
    Formula: center = (p_hat + z*z/(2n)) / (1 + z*z/n) where p_hat = k/n;
    half = (z/(1+z*z/n)) * sqrt( p_hat*(1-p_hat)/n + z*z/(4*n*n) );
    lo = max(0.0, center-half), hi = min(1.0, center+half)."""
    if n <= 0:
        raise ValueError("n must be positive")
    
    p_hat = k / n
    z_sq = z * z
    
    center = (p_hat + z_sq / (2 * n)) / (1 + z_sq / n)
    half = (z / (1 + z_sq / n)) * math.sqrt(
        p_hat * (1 - p_hat) / n + z_sq / (4 * n * n)
    )
    
    lo = max(0.0, center - half)
    hi = min(1.0, center + half)
    
    return (lo, hi)


def benjamini_hochberg(pvalues: List[float], alpha: float = 0.05) -> List[bool]:
    """Benjamini-Hochberg FDR step-up. Return a list of bool aligned to the ORIGINAL
    input order: True = the hypothesis is rejected (survives FDR).
    Procedure: let m=len(pvalues). Sort p ascending keeping original indices.
    For sorted rank i (1-based), threshold = (i/m)*alpha. Find the LARGEST rank k
    whose p(k) <= its threshold. Reject ALL hypotheses with sorted rank <= k
    (even ones whose own p exceeds their threshold). If no rank qualifies, reject none.
    Return the mask in original input order. Empty input -> []."""
    m = len(pvalues)
    if m == 0:
        return []
    
    # Create pairs of (pvalue, original_index) and sort by pvalue
    indexed_pvalues = [(pvalues[i], i) for i in range(m)]
    indexed_pvalues.sort(key=lambda x: x[0])
    
    # Find the largest rank k where p(k) <= (k/m)*alpha. Compare cross-multiplied
    # (p*m <= i*alpha) so an exact mathematical boundary doesn't flip on a 1-ULP
    # difference between equivalent float forms of the threshold (cross-validation).
    k_max = 0
    for i in range(1, m + 1):
        p_sorted = indexed_pvalues[i - 1][0]
        if p_sorted * m <= i * alpha:
            k_max = i
    
    # Create result array
    result = [False] * m
    
    # Mark all hypotheses with sorted rank <= k_max as rejected
    for i in range(k_max):
        original_index = indexed_pvalues[i][1]
        result[original_index] = True
    
    return result


def paired_bootstrap_ci(deltas: List[float], n_boot: int = 2000,
                        alpha: float = 0.05, seed: int = 0) -> Tuple[float, float]:
    """Percentile bootstrap CI for the mean of paired differences `deltas`.
    Use random.Random(seed) for reproducibility. Draw n_boot resamples WITH
    replacement of size len(deltas), take each resample's mean, return the
    (alpha/2, 1-alpha/2) percentiles as (lo, hi). Raise ValueError if deltas is empty.
    Must be deterministic given the same seed."""
    if len(deltas) == 0:
        raise ValueError("deltas cannot be empty")
    if n_boot < 1:
        raise ValueError("n_boot must be >= 1")

    rng = random.Random(seed)
    n = len(deltas)

    # Generate bootstrap means
    bootstrap_means = []
    for _ in range(n_boot):
        # Resample with replacement
        resample = [deltas[rng.randint(0, n - 1)] for _ in range(n)]
        bootstrap_means.append(sum(resample) / n)

    bootstrap_means.sort()

    # Symmetric percentile indices via nearest-rank on (n_boot-1); using the same
    # rounding rule for both ends guarantees lower_index <= upper_index for any
    # alpha (the earlier asymmetric int() truncation could invert the interval at
    # extreme alpha / small n_boot — cross-validation).
    def _percentile_index(q: float) -> int:
        idx = int(round(q * (n_boot - 1)))
        return max(0, min(idx, n_boot - 1))

    lower_index = _percentile_index(alpha / 2)
    upper_index = _percentile_index(1 - alpha / 2)

    return (bootstrap_means[lower_index], bootstrap_means[upper_index])


def paired_bootstrap_pvalue(deltas: List[float], n_boot: int = 2000,
                            seed: int = 0, alternative: str = "greater") -> float:
    """One-sided bootstrap p-value for the mean of paired differences `deltas`.

    Resamples `deltas` with replacement `n_boot` times (random.Random(seed) for
    reproducibility) and estimates the probability, under the bootstrap distribution
    of the sample mean, of the null side of zero:
      - alternative='greater' (H1: mean > 0): p = fraction of bootstrap means <= 0
      - alternative='less'    (H1: mean < 0): p = fraction of bootstrap means >= 0
    The p-value is floored at 1/n_boot so an unambiguous effect never reports 0
    (which would falsely read as infinite significance). Raises ValueError on empty
    input, n_boot < 1, or an unknown `alternative`.
    """
    if len(deltas) == 0:
        raise ValueError("deltas cannot be empty")
    if n_boot < 1:
        raise ValueError("n_boot must be >= 1")
    if alternative not in ("greater", "less"):
        raise ValueError("alternative must be 'greater' or 'less'")
    if any(x != x or x in (float("inf"), float("-inf")) for x in deltas):
        raise ValueError("deltas must all be finite (no NaN/inf)")

    n = len(deltas)
    obs_mean = sum(deltas) / n
    # Elementwise finiteness is not enough: finite-but-huge magnitudes (e.g. 1e308)
    # sum/average to +/-inf, which then centers to +/-inf and floors the p to a false
    # maximum significance (cross-validation#4). Require the aggregate to be finite too.
    if obs_mean != obs_mean or obs_mean in (float("inf"), float("-inf")):
        raise ValueError("deltas mean is non-finite (overflow); cannot compute p-value")

    # Impose the mean-zero null: resample the CENTERED sample (each delta minus the
    # observed mean, so the bootstrap population has mean exactly 0) and ask how often
    # a null-resample mean is at least as extreme as the observed mean. This is the
    # calibrated bootstrap hypothesis test; resampling the RAW sample and taking its
    # tail beyond zero is NOT a p-value and is anti-conservative on skew (cross-validation).
    centered = [x - obs_mean for x in deltas]
    rng = random.Random(seed)

    at_least_as_extreme = 0
    for _ in range(n_boot):
        m = sum(centered[rng.randint(0, n - 1)] for _ in range(n)) / n
        if alternative == "greater":
            if m >= obs_mean:
                at_least_as_extreme += 1
        else:  # 'less'
            if m <= obs_mean:
                at_least_as_extreme += 1

    return max(at_least_as_extreme / n_boot, 1.0 / n_boot)


# --- Welford: persist parameters (n, mean, M2), never rows ---------------------------------------

def welford_new() -> dict:
    return {"n": 0, "mean": 0.0, "m2": 0.0}


def welford_push(w: dict, x: float) -> dict:
    n = w["n"] + 1
    delta = x - w["mean"]
    mean = w["mean"] + delta / n
    return {"n": n, "mean": mean, "m2": w["m2"] + delta * (x - mean)}


def welford_variance(w: dict) -> float:
    """Sample variance M2/(n-1); 0.0 below two samples."""
    return w["m2"] / (w["n"] - 1) if w["n"] > 1 else 0.0


def welford_cov_new(d: int) -> dict:
    return {"n": 0, "mean": [0.0] * d, "c": [[0.0] * d for _ in range(d)]}


def welford_cov_push(s: dict, x) -> dict:
    """Multivariate Welford: C += (x - mean_old)(x - mean_new)ᵀ."""
    n = s["n"] + 1
    delta = [xi - mi for xi, mi in zip(x, s["mean"])]
    mean = [mi + di / n for mi, di in zip(s["mean"], delta)]
    after = [xi - mi for xi, mi in zip(x, mean)]
    c = [[cij + delta[i] * after[j] for j, cij in enumerate(row)] for i, row in enumerate(s["c"])]
    return {"n": n, "mean": mean, "c": c}


def covariance(s: dict) -> list:
    """Sample covariance C/(n-1); zeros below two samples."""
    return [[cij / (s["n"] - 1) if s["n"] > 1 else 0.0 for cij in row] for row in s["c"]]


# --- EWMA: s = a·x + (1-a)·s ----------------------------------------------------------------------

def ewma(prev, x: float, a: float = 0.2) -> float:
    return x if prev is None else a * x + (1 - a) * prev


def half_life(a: float) -> float:
    """Samples until a step's weight halves: ln(0.5)/ln(1-a) (a=0.2 → 3.1)."""
    return math.log(0.5) / math.log(1 - a)


Q16 = 65536


def to_q16(x: float) -> int:
    """Round half up (not Python's half-to-even round()) so the TS mirror matches bit for bit."""
    return math.floor(x * Q16 + 0.5)


def ewma_q16(prev, x: float, a_q16: int = 13107) -> int:
    """Fixed-point EWMA: integer state scaled by 2^16; a_q16 = round(0.2·65536)."""
    xq = to_q16(x)
    return xq if prev is None else prev + int((xq - prev) * a_q16 / Q16)


# --- the one nearest-rank percentile --------------------------------------------------------------

def nearest_rank(sorted_values, q: float):
    """s[ceil(q·n) − 1] with the rank clamped to [1, n]; q in (0, 1]. Empty → None.
    Never average per-family percentiles: merge the samples (or histograms) first."""
    if not sorted_values:
        return None
    rank = math.ceil(q * len(sorted_values))
    rank = max(1, min(rank, len(sorted_values)))
    return sorted_values[rank - 1]
