"""Pure statistics module using only the Python standard library.

Provides confidence intervals, multiple hypothesis testing corrections,
parameter estimation, and resampling methods.
"""

import math
import random
from typing import Tuple, List, Dict


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
    # difference between equivalent float forms of the threshold (Codex xval).
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


def bradley_terry(pairwise: Dict, max_iter: int = 1000, tol: float = 1e-9) -> Dict:
    """Bradley-Terry strengths from pairwise win counts.
    `pairwise` maps (winner, loser) -> number of times `winner` beat `loser`.
    Fit positive strengths p[model] via the standard MM/iterative algorithm:
    for each model i, w_i = total wins by i; p_i <- w_i / sum_j( n_ij / (p_i + p_j) )
    where n_ij = games between i and j (both directions). Iterate to convergence
    (max_iter / tol), then NORMALIZE so sum(p.values()) == 1.0. All strengths > 0.
    Return dict[str, float]. Handle 2+ models. Initialize all p_i = 1/num_models."""
    if len(pairwise) == 0:
        return {}
    
    # Extract all models
    models = set()
    for (winner, loser) in pairwise.keys():
        models.add(winner)
        models.add(loser)
    
    models = sorted(models)  # Sort for deterministic ordering
    n_models = len(models)
    
    if n_models < 2:
        raise ValueError("Need at least 2 models")
    
    # Build win counts and game counts
    wins = {m: 0.0 for m in models}
    games = {}  # (i, j) -> number of games between i and j
    
    for (winner, loser), count in pairwise.items():
        wins[winner] += count
        games[(winner, loser)] = games.get((winner, loser), 0) + count
        games[(loser, winner)] = games.get((loser, winner), 0) + count
    
    # Initialize strengths
    p = {m: 1.0 / n_models for m in models}
    
    # Iterate to convergence (Hunter 2004 MM). NORMALIZE inside every iteration so
    # the convergence test compares stationary quantities — without this the raw
    # p_i drift and the loop stops early on an unnormalized max-change, producing a
    # budget-dependent (wrong) ranking (Codex empirical xval, 2026-07-31).
    for _iteration in range(max_iter):
        p_new = {}

        for i in models:
            denominator = 0.0
            for j in models:
                if i == j:
                    continue
                n_ij = games.get((i, j), 0)
                if n_ij > 0:
                    denominator += n_ij / (p[i] + p[j])

            if denominator > 0:
                p_new[i] = wins[i] / denominator
            else:
                p_new[i] = p[i]  # Keep current value if no games

        # Normalize the new iterate before comparing/looping.
        total_new = sum(p_new.values())
        if total_new > 0:
            p_new = {m: v / total_new for m, v in p_new.items()}

        # Check convergence on the normalized iterate.
        max_change = max(abs(p_new[m] - p[m]) for m in models)
        p = p_new

        if max_change < tol:
            break

    # Final normalization so sum(p.values()) == 1.0 exactly.
    total = sum(p.values())
    if total > 0:
        p = {m: v / total for m, v in p.items()}

    return p


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
    # extreme alpha / small n_boot — Codex xval).
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
    # maximum significance (Codex pass2 #4). Require the aggregate to be finite too.
    if obs_mean != obs_mean or obs_mean in (float("inf"), float("-inf")):
        raise ValueError("deltas mean is non-finite (overflow); cannot compute p-value")

    # Impose the mean-zero null: resample the CENTERED sample (each delta minus the
    # observed mean, so the bootstrap population has mean exactly 0) and ask how often
    # a null-resample mean is at least as extreme as the observed mean. This is the
    # calibrated bootstrap hypothesis test; resampling the RAW sample and taking its
    # tail beyond zero is NOT a p-value and is anti-conservative on skew (Codex F4).
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
