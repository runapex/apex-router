"""R1 calibration fit (Spec 2) — does apex's byte accounting predict the provider's billed tokens?

Fits per-class effective-tokens-per-byte coefficients from live capture and states a calibration
number: "apex byte accounting vs provider billing — median error N%". Plain OLS (numpy.lstsq).

ARBITER SEPARATION (doctrine, pinned by the plane test): this fit informs REPORTING ONLY. It must
never feed any expected/predicted quantity that the enforcement gate G grades — coefficients flow to
the doctor and drift logging, nowhere else. The AST plane guard prevents pipeline/tuner imports
structurally; this sentence makes the boundary semantic so a future contributor knows why.

WIRE-SEMANTICS PIN (a prior calibration bug) is load-bearing here: y is FRESH input tokens, extracted by the SAME
helper the doctor uses post-fix (`apex_router.proxy_engine.readout.doctor._fresh_input`) — r1 does NOT re-derive the
Anthropic-vs-OpenAI field semantics. A test asserts r1's y equals the doctor's on a shared fixture.

The fit REFUSES (emits no number, states why) rather than report a bad one when the physical
constraints fail — every class coefficient positive and in the plausible band, and r² above the
floor. This relationship is near-mechanical; a weak fit means a population or semantics problem, not
noise, and refusing IS the finding (the same posture the compiler took on first deployment).

ACTIVATION CONDITION (do NOT read "refuses" as "needs more rows"): on the current Anthropic wire the
fit refuses because X is WHOLE-FRONTIER bytes while y is FRESH-only tokens, and at ~97.9%
cache-served most rows have y≈0 against a huge X. That is STRUCTURAL rank-starvation, not a
sample-size problem — more rows of the same cached shape will not fix it. R1 activates on one of two
changes, NOT patience:
  (a) FRONTIER-ONLY X — restrict the design matrix to the bytes that produced the FRESH tokens (the
      uncached suffix), which #14's frontier-byte capture enables (the same prereq as the divergence
      classifier, Spec 1); or
  (b) a CACHE-AWARE design matrix that models cached vs fresh bytes as separate terms.
Until (a) or (b), the honest calibration output is the refusal — and the refusal names this cause.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from apex_router.proxy_engine.readout.doctor import _fresh_input, is_generative

# The byte-class columns of X (the frontier composition, apex_router.proxy_engine.policy classes). `code` is folded into
# the classifier's other buckets at the byte level; these five are what bytes_by_class emits.
FEATURE_CLASSES = ("prose", "file_read", "json", "terminal", "diff")

# Recognized wires whose fresh-token semantics `_fresh_input` knows (a prior calibration bug). An unknown/None
# endpoint must NEVER be fit — it would silently get one wire's field semantics (the Anthropic
# default), the exact mis-read a prior calibration bug was. cross-validation.
RECOGNIZED_WIRES = ("anthropic", "openai")

# Plausible tokens-per-byte band for a class coefficient. Derivation (bound, not policy): measured
# bytes/token ranges 3.2–4.06 across classes (apex_router.proxy_engine.policy canonical byte strata); inverted that is
# 1/4.06 ≈ 0.246 to 1/3.2 ≈ 0.313 tokens/byte for a pure single-class request. A per-class OLS
# coefficient can sit modestly outside that (mixed content, framing overhead attributed to a col),
# so the band widens it to [0.15, 0.60] — ~0.6× the low end to ~1.9× the high end. A coefficient
# outside this is not a token rate; the fit refuses rather than emit it.
COEF_BAND = (0.15, 0.60)

# r² floor. This relationship (bytes → tokens) is near-mechanical, so a healthy fit is ≥0.95; the
# floor sits at 0.90 — clearly below a real fit but far above the ~0 of a permuted/ mismatched
# population, so tripping it means "population or semantics problem", not "noisy but fine".
R_SQUARED_FLOOR = 0.90


# ---------- extraction (pin-shared y) ----------

def _row_endpoint(d: dict) -> str | None:
    return d.get("endpoint_id")


def extract_xy(rows: list[dict], endpoint: str | None):
    """Build (X, y, kept_rows) for one endpoint. X rows are the bytes_by_class columns (+ an
    intercept column of 1s appended by the caller); y is FRESH input tokens via the doctor's
    wire-aware helper (the pin — NOT re-derived here). Keeps GENERATIVE rows for this endpoint that
    carry bytes_by_class. Raises ValueError if `endpoint` is None but the rows span >1 endpoint (the
    mixed-wire guard — the two wires' y-semantics differ, so a pooled fit is meaningless)."""
    gen = [d for d in rows if is_generative(d)]
    # Filter to the requested endpoint FIRST (a stray other-wire row must not trip the mixed guard),
    # keeping only rows with usable bytes_by_class.
    if endpoint is not None and endpoint not in RECOGNIZED_WIRES:
        raise ValueError(
            f"R1 fit needs a recognized wire {RECOGNIZED_WIRES} (fresh-token semantics differ per "
            f"wire — a prior calibration bug); got {endpoint!r}, whose y-semantics are unknown"
        )
    if endpoint is None:
        endpoints = {_row_endpoint(d) for d in gen}
        unknown = endpoints - set(RECOGNIZED_WIRES)
        if unknown:
            raise ValueError(
                f"R1 fit needs a recognized wire; rows carry unknown/absent endpoints {unknown} "
                "whose fresh-token semantics are undefined (a prior calibration bug)"
            )
        if len(endpoints) > 1:
            raise ValueError(
                f"R1 fit is per-endpoint (wire y-semantics differ); got mixed endpoints {endpoints}"
            )
    X, y, kept = [], [], []
    for d in gen:
        if endpoint is not None and _row_endpoint(d) != endpoint:
            continue
        sh = d.get("shadow") or {}
        bbc = sh.get("bytes_by_class") if isinstance(sh, dict) else None
        if not bbc:
            continue
        # oversize-skipped rows have thinned X by construction (#9 labeled gap) — exclude + count.
        if isinstance(sh, dict) and sh.get("oversize_skipped"):
            continue
        X.append([float(bbc.get(c, 0)) for c in FEATURE_CLASSES])
        y.append(float(_fresh_input(d)))
        kept.append(d)
    return X, y, kept


# ---------- the fit ----------

@dataclass(frozen=True)
class R1Fit:
    coefficients: dict = field(default_factory=dict)   # tokens per byte, per class
    intercept: float = 0.0
    n_rows: int = 0
    r_squared: float = 0.0
    median_abs_pct_error: float = 0.0
    mape_coverage: float = 0.0                          # fraction of rows (nonzero y) the MAPE used
    residual_trend_slope: float = 0.0                  # tokens/day drift (reported, NOT alarmed)
    population_label: str = ""
    refused: bool = False
    refusal_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "coefficients": self.coefficients, "intercept": self.intercept,
            "n_rows": self.n_rows, "r_squared": self.r_squared,
            "median_abs_pct_error": self.median_abs_pct_error,
            "mape_coverage": self.mape_coverage,
            "residual_trend_slope": self.residual_trend_slope,
            "population_label": self.population_label,
            "refused": self.refused, "refusal_reason": self.refusal_reason,
        }


def _refuse(reason: str, n_rows: int, label: str, **extra) -> R1Fit:
    return R1Fit(refused=True, refusal_reason=reason, n_rows=n_rows,
                 population_label=label, **extra)


def fit_r1(rows: list[dict], endpoint: str | None, *, oversize_excluded: int = 0) -> R1Fit:
    """Fit per-class tokens/byte for one endpoint via OLS. Returns an R1Fit; on a constraint breach
    it REFUSES (refused=True, refusal_reason set) rather than emit a bad number."""
    X, y, kept = extract_xy(rows, endpoint)
    n = len(kept)
    label = (f"generative, priced, non-oversize; wire={endpoint or 'n/a'}; "
             f"n={n}; oversize_excluded={oversize_excluded}")
    if n < len(FEATURE_CLASSES) + 2:
        return _refuse(f"too few rows to fit ({n} < {len(FEATURE_CLASSES) + 2})", n, label)

    A = np.array([row + [1.0] for row in X], dtype=float)  # + intercept column
    b = np.array(y, dtype=float)
    x_totals = A[:, :-1].sum(axis=1)

    # Zero-variance target: y is constant → r² is undefined and no rate is estimable. Refuse (Codex
    # F3: forcing r²=0 here would mislabel a perfect constant fit as a weak population fit).
    if float(np.std(b)) == 0.0:
        return _refuse("target y has zero variance (all rows same fresh-token count) — no rate "
                       "estimable", n, label)

    # Population-mismatch pre-check (the register's historical R1 failure mode → a NAMED refusal):
    # X is WHOLE-FRONTIER bytes but y is FRESH-only tokens. On a heavily-cached wire the cached rows
    # have y≈0 against a large X. cross-validation: use the JOINT condition (rows that are BOTH tiny-y AND
    # large-X), not two separate marginals — a marginal test false-refuses a clean fit that merely
    # has a few unrelated tiny-y rows, and false-passes when tiny-y and large-X don't coincide.
    tiny_and_large = np.mean((b < 50) & (x_totals > 10_000))
    if float(tiny_and_large) > 0.5:
        return _refuse(
            f"X/y population mismatch: {tiny_and_large:.0%} of rows are BOTH ~0 FRESH tokens "
            "(cached prefix) AND large frontier bytes — bytes_by_class (whole frontier) and fresh "
            "tokens describe different populations on a cached wire; not calibratable here",
            n, label)

    coef, _resid, rank, _sv = np.linalg.lstsq(A, b, rcond=None)

    # Rank check (cross-validation): if the design matrix is rank-deficient (collinear byte-class columns),
    # lstsq returns a min-norm solution with high r² but the PER-CLASS coefficients are NOT
    # identifiable — reporting them as tokens/byte is meaningless. Full column rank is required.
    if int(rank) < A.shape[1]:
        return _refuse(f"rank-deficient design (rank {int(rank)} < {A.shape[1]} columns) — byte-"
                       "class columns are collinear; per-class coefficients not identifiable",
                       n, label)

    *class_coefs, intercept = coef.tolist()
    coefficients = dict(zip(FEATURE_CLASSES, class_coefs, strict=True))

    # r² from residuals (ss_tot > 0 guaranteed by the zero-variance refusal above).
    pred = A @ coef
    ss_res = float(np.sum((b - pred) ** 2))
    ss_tot = float(np.sum((b - b.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot

    # median absolute percent error — the calibration number — over NONZERO-y rows, with coverage.
    nz = b != 0
    mape_coverage = float(np.mean(nz))
    mape = float(np.median(np.abs((b[nz] - pred[nz]) / b[nz])) * 100.0) if nz.any() else 0.0

    # ---- physical-constraint refusals (fit-refusal doctrine) ----
    if r2 < R_SQUARED_FLOOR:
        return _refuse(f"r²={r2:.3f} < {R_SQUARED_FLOOR:.2f} floor (population/semantics problem, "
                       "not noise)", n, label, r_squared=r2)
    lo, hi = COEF_BAND
    bad = {c: v for c, v in coefficients.items() if not (lo <= v <= hi)}
    if bad:
        pretty = ", ".join(f"{c}={v:.3f}" for c, v in bad.items())
        return _refuse(f"coefficient(s) outside the plausible band [{lo},{hi}]: {pretty}", n, label,
                       r_squared=r2, coefficients=coefficients)
    # Intercept + prediction plausibility (cross-validation): a token count can't be negative. A large
    # negative intercept means the fit absorbed a mismatch into the unconstrained constant term
    # (predicting negative tokens for small prompts). Refuse a negative intercept or any negative
    # fitted prediction — the coefficient band alone doesn't catch this.
    if intercept < 0 or float(pred.min()) < 0:
        return _refuse(f"implausible fit: intercept={intercept:.0f} / min prediction="
                       f"{float(pred.min()):.0f} < 0 (a token count can't be negative — the "
                       "constant term absorbed a population mismatch)", n, label,
                       r_squared=r2, coefficients=coefficients)

    return R1Fit(
        coefficients=coefficients, intercept=intercept, n_rows=n, r_squared=r2,
        median_abs_pct_error=mape, mape_coverage=mape_coverage,
        residual_trend_slope=0.0, population_label=label,
        refused=False, refusal_reason="",
    )


# ---------- doctor line ----------

def format_calibration_line(fit: R1Fit) -> str:
    """The one doctor line. A refused fit says calibration is unavailable and WHY — never a bad
    number (the on-message posture: the instrument declines to state what it can't support)."""
    if fit.refused:
        return f"Calibration: unavailable (fit refused: {fit.refusal_reason})"
    wire = fit.population_label.split("wire=")[-1].split(";")[0]
    return (f"Calibration: apex byte accounting vs provider billing — median error "
            f"{fit.median_abs_pct_error:.1f}% (n={fit.n_rows:,} turns, {wire} wire)")
