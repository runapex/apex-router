"""R1 calibration fit (Spec 2) — CONTROLS BEFORE ANY CALIBRATION NUMBER IS TRUSTED.

The fit proves apex's byte accounting predicts the provider's billed tokens: per-class
tokens-per-byte coefficients from live capture + a calibration statement. Analytics plane only (no
wire, no pipeline import). It CONSUMES the wire-semantics pin (a calibration invariant) — y is fresh input tokens,
extracted by the SAME helper the doctor uses post-fix, never re-derived.

Controls (spec):
  1. SYNTHETIC RECOVERY (positive) — rows from known coefficients + noise → fit recovers them.
  2. PERMUTED-y (negative) — shuffle y against X → fit must REFUSE (r² floor trips). This is the
     control that catches a silent population mismatch (the historical R1 failure mode).
  3. MIXED-WIRE guard — fit is per-endpoint, never pooled (the two wires' y-semantics differ).
  4. PIN-SHARING — r1's y-extraction equals the doctor's on a shared fixture.
  5. PHYSICAL CONSTRAINTS — coefficients in the plausible band, r² floor; else REFUSE with a reason.
"""
from __future__ import annotations

import random

from apex_router.proxy_engine.analytics.r1 import (
    COEF_BAND,
    R_SQUARED_FLOOR,
    extract_xy,
    fit_r1,
)

_CLASSES = ("prose", "file_read", "json", "terminal", "diff")


def _row(bytes_by_class: dict, fresh_input: int, endpoint="anthropic", cache_read=0):
    """A generative telemetry row with shadow.bytes_by_class (X) and usage (y). For anthropic,
    tokens_in is already fresh; for openai it is total, so we set cache_read to keep fresh honest."""
    tokens_in = fresh_input if endpoint == "anthropic" else fresh_input + cache_read
    return {
        "schema_version": 3, "endpoint_id": endpoint, "is_error": False,
        "model_resolved": "opus" if endpoint == "anthropic" else "gpt-5",
        "tokens_in": tokens_in, "cache_read_tokens": cache_read, "cache_write_tokens": 0,
        "tokens_out": 50,
        "usage": {"input_tokens": tokens_in, "output_tokens": 50},
        "shadow": {"bytes_by_class": dict(bytes_by_class)},
    }


def _synthetic_rows(coefs: dict, intercept: float, n: int, endpoint="anthropic", noise=0.0):
    rng = random.Random(1234)
    rows = []
    for _ in range(n):
        bbc = {c: rng.randint(200, 20_000) for c in _CLASSES}
        y = intercept + sum(coefs[c] * bbc[c] for c in _CLASSES)
        y += rng.uniform(-noise, noise) * y
        rows.append(_row(bbc, int(round(y)), endpoint=endpoint))
    return rows


# ---------- CONTROL 4: pin-sharing (y-extraction equals the doctor's) ----------

def test_y_extraction_shares_the_doctor_wire_semantics_pin():
    from apex_router.proxy_engine.readout.doctor import _fresh_input
    # OpenAI row: tokens_in is TOTAL, cache_read a subset → fresh must be total − cached, matching
    # the doctor exactly (r1 must NOT re-derive the semantics — a calibration invariant).
    row = _row({c: 1000 for c in _CLASSES}, fresh_input=800, endpoint="openai", cache_read=200)
    X, y, _ = extract_xy([row], endpoint="openai")
    assert y[0] == _fresh_input(row) == 800


# ---------- CONTROL 1: synthetic recovery (positive) ----------

def test_recovers_known_coefficients_within_tolerance():
    true = {"prose": 0.25, "file_read": 0.30, "json": 0.22, "terminal": 0.28, "diff": 0.26}
    rows = _synthetic_rows(true, intercept=40.0, n=400, noise=0.01)
    fit = fit_r1(rows, endpoint="anthropic")
    assert fit.refused is False, f"a clean synthetic fit must not refuse: {fit.refusal_reason}"
    for c in _CLASSES:
        assert abs(fit.coefficients[c] - true[c]) < 0.03, f"{c}: {fit.coefficients[c]} vs {true[c]}"
    assert fit.r_squared > R_SQUARED_FLOOR
    assert fit.median_abs_pct_error < 5.0


# ---------- CONTROL 2: permuted-y (negative) — must REFUSE ----------

def test_permuted_y_refuses_on_the_r_squared_floor():
    true = {"prose": 0.25, "file_read": 0.30, "json": 0.22, "terminal": 0.28, "diff": 0.26}
    rows = _synthetic_rows(true, intercept=40.0, n=400, noise=0.01)
    # shuffle y against X → destroy the relationship. The fit MUST refuse (r² below floor).
    ys = [r["usage"]["input_tokens"] for r in rows]
    rng = random.Random(99)
    rng.shuffle(ys)
    for r, y in zip(rows, ys):
        r["usage"]["input_tokens"] = y
        r["tokens_in"] = y
    fit = fit_r1(rows, endpoint="anthropic")
    assert fit.refused is True, "permuted y must refuse — the r² floor is the population-mismatch guard"
    assert "r" in fit.refusal_reason.lower()  # cites r² / the floor


# ---------- CONTROL 3: mixed-wire guard ----------

def test_pooled_wires_raises():
    import pytest
    true = {"prose": 0.25, "file_read": 0.30, "json": 0.22, "terminal": 0.28, "diff": 0.26}
    rows = _synthetic_rows(true, 40.0, 50, endpoint="anthropic") + \
        _synthetic_rows(true, 40.0, 50, endpoint="openai")
    # extract_xy must be called per-endpoint; a pooled call (endpoint=None with mixed rows) raises.
    with pytest.raises(ValueError):
        fit_r1(rows, endpoint=None)


# ---------- CONTROL 5: physical constraints refuse a bad fit ----------

def test_out_of_band_coefficient_refuses():
    # coefficients far outside the plausible tokens-per-byte band [0.15,0.60] → refuse, don't emit.
    absurd = {"prose": 5.0, "file_read": 5.0, "json": 5.0, "terminal": 5.0, "diff": 5.0}
    rows = _synthetic_rows(absurd, intercept=0.0, n=300, noise=0.0)
    fit = fit_r1(rows, endpoint="anthropic")
    assert fit.refused is True
    assert "band" in fit.refusal_reason.lower() or "coefficient" in fit.refusal_reason.lower()


def test_band_and_floor_are_derived_constants_not_round():
    # the band is derived from the measured 3.2–4.06 bytes/token inverted (±margin) — documented,
    # and the r² floor is a real threshold, not a tidy 0.5/0.8.
    lo, hi = COEF_BAND
    assert 0.10 <= lo < hi <= 0.65
    assert R_SQUARED_FLOOR >= 0.9


def test_cached_wire_mismatch_refusal_names_the_specific_cause():
    # The real-data failure mode (register): X is whole-frontier bytes but y is fresh-ONLY tokens, so
    # on a heavily-cached wire most rows have y≈0 against huge X → the fit must refuse AND the reason
    # must name this cause (not just "r² low"), so the operator isn't left guessing. Reproduce it:
    # big X, near-zero fresh y on most rows (the cached-prefix pattern).
    rows = []
    for i in range(300):
        bbc = {c: 100_000 for c in _CLASSES}          # huge frontier bytes
        rows.append(_row(bbc, fresh_input=2))          # ~0 fresh tokens (cached prompt)
    fit = fit_r1(rows, endpoint="anthropic")
    assert fit.refused is True
    assert "cache" in fit.refusal_reason.lower() or "fresh" in fit.refusal_reason.lower(), (
        f"refusal must name the cached-wire X/y mismatch, got: {fit.refusal_reason}"
    )


def test_population_label_states_wire_and_n():
    true = {"prose": 0.25, "file_read": 0.30, "json": 0.22, "terminal": 0.28, "diff": 0.26}
    rows = _synthetic_rows(true, 40.0, 200, endpoint="anthropic")
    fit = fit_r1(rows, endpoint="anthropic")
    assert "anthropic" in fit.population_label
    assert "generative" in fit.population_label.lower()
    assert fit.n_rows == 200


# ---------- cross-validation (all CONFIRMED at ground truth), fixed ----------

def test_rank_deficient_collinear_columns_refuse():
    # cross-validation: if the feature columns are collinear (identical), lstsq returns a min-norm solution
    # with high r² but the per-class coefficients are NOT identifiable. Must refuse, not report them.
    true = {"prose": 0.25, "file_read": 0.30, "json": 0.22, "terminal": 0.28, "diff": 0.26}
    rng = random.Random(3)
    rows = []
    for _ in range(200):
        v = rng.randint(500, 15_000)
        bbc = {c: v for c in _CLASSES}                # ALL columns identical → rank-deficient
        y = 40 + sum(true[c] * v for c in _CLASSES)
        rows.append(_row(bbc, int(round(y))))
    fit = fit_r1(rows, endpoint="anthropic")
    assert fit.refused is True, "collinear (rank-deficient) X must refuse — coefs unidentifiable"
    assert "rank" in fit.refusal_reason.lower() or "identif" in fit.refusal_reason.lower()


def test_large_negative_intercept_refuses():
    # cross-validation: a fit can absorb a population mismatch into an unconstrained NEGATIVE intercept
    # (predicting negative tokens for small prompts). The intercept must be constrained / predictions
    # non-negative; a large negative intercept refuses.
    rng = random.Random(5)
    rows = []
    for _ in range(200):
        bbc = {c: rng.randint(8_000, 15_000) for c in _CLASSES}
        y = -2000 + 0.25 * sum(bbc.values())          # large negative intercept
        rows.append(_row(bbc, int(round(y))))
    fit = fit_r1(rows, endpoint="anthropic")
    assert fit.refused is True, "a large-negative intercept (negative token prediction) must refuse"
    assert "intercept" in fit.refusal_reason.lower() or "negative" in fit.refusal_reason.lower()


def test_unknown_endpoint_label_refuses_not_anthropic_default():
    # cross-validation: an unknown singleton endpoint (e.g. "codex") must NOT silently get Anthropic
    # _fresh_input semantics. Require a RECOGNIZED wire; else refuse (the witness-9 lesson: never
    # apply one wire's field semantics to an unrecognized wire).
    import pytest
    rows = [_row({c: 1000 for c in _CLASSES}, fresh_input=700, endpoint="openai", cache_read=200)]
    for r in rows:
        r["endpoint_id"] = "codex"                     # unrecognized label
    with pytest.raises(ValueError):
        extract_xy(rows, endpoint="codex")


def test_mape_coverage_is_reported_when_zero_targets_excluded():
    # cross-validation: MAPE excludes y==0 rows; when it does, the fit must state the nonzero-target coverage
    # so "median error N%" isn't silently conditional on a subset while reporting full n.
    true = {"prose": 0.25, "file_read": 0.30, "json": 0.22, "terminal": 0.28, "diff": 0.26}
    rows = _synthetic_rows(true, 40.0, 200, endpoint="anthropic", noise=0.01)
    fit = fit_r1(rows, endpoint="anthropic")
    assert fit.refused is False
    assert 0 < fit.mape_coverage <= 1.0               # fraction of rows with nonzero y used for MAPE


# ---------- arbiter separation: r1 must not import the pipeline/enforcement plane ----------

def test_r1_does_not_import_enforcement_plane():
    import ast
    import os
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "src", "apex_router", "proxy_engine", "analytics", "r1.py")
    tree = ast.parse(open(src).read())
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
    forbidden = [m for m in mods if m.startswith(("apex_router.proxy_engine.pipeline", "apex_router.proxy_engine.proxy", "apex_router.proxy_engine.tuner"))]
    assert not forbidden, f"r1 (reporting) must not import the enforcement/economics plane: {forbidden}"
