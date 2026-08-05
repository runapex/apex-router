"""run_shadow observation budget — a DECISION, not an apologetic guard.

The metrics re-collection (the reference window) found the active latency tail is single 1.3–2.0 MB blocks:
run_shadow's decompose/classify cost is ~linear in body size (measured `ms = 17.7·MB + 0.36` on this
machine), so a multi-MB single-block frontier blows the G3 25ms gate. The fix is an observation
BUDGET: above a DERIVED byte threshold, skip decompose but COUNT what wasn't inspected — telemetry
records `oversize_skipped=True` + `frontier_bytes`, so it under-counts VISIBLY, not silently.

Two register-shaped requirements pinned here:
  1. THE THRESHOLD IS A BOUND, NOT A POLICY — derived from the measured size-vs-latency curve (the
     point where compute stays within a budget), not a round number (the 3.0-vs-3.2 lesson).
  2. THE SKIPPED POPULATION IS A LABELED COMPOSITION GAP — `bytes_by_class` under-counts EXACTLY the
     oversize blocks, which are the ones R1's regression most wants (biggest bytes). So a skip is a
     LABELED exclusion (`oversize_skipped`, `frontier_bytes`), never a silent zero — an R1 fit that
     ignores it biases its coefficients. `bytes_by_class` is NOT populated from a skipped body.
"""
from __future__ import annotations

import json

from apex_router.proxy_engine.pipeline.shadow import OVERSIZE_FRONTIER_BYTES, run_shadow


def _body(text: str) -> bytes:
    return json.dumps({"model": "m", "messages": [{"role": "user", "content": text}]}).encode()


# 1 — the threshold is a DERIVED BOUND (keeps compute under the latency budget), not a round number.
def test_threshold_is_a_derived_bound_not_a_round_number():
    # Derived from `ms = 17.7·MB + 0.36` at a ~10ms budget → ~544 KB. It must NOT be a round number
    # (10**5, 500_000, 1<<20) — a bound is computed from the measurement, not chosen for tidiness.
    round_numbers = {100_000, 250_000, 500_000, 1_000_000, 1 << 19, 1 << 20, 2_000_000}
    assert OVERSIZE_FRONTIER_BYTES not in round_numbers, (
        f"OVERSIZE_FRONTIER_BYTES={OVERSIZE_FRONTIER_BYTES} is a round number — it must be DERIVED "
        "from the measured size×latency curve (a bound), not chosen for tidiness"
    )
    # sanity: in the region the curve places a ~10ms budget (400–650 KB), not absurd
    assert 400_000 <= OVERSIZE_FRONTIER_BYTES <= 650_000


# 2a — a body OVER the threshold is SKIPPED and LABELED (not silently thinned)
def test_oversize_body_is_skipped_and_labeled():
    huge = _body("x" * (OVERSIZE_FRONTIER_BYTES + 500_000))  # well over
    rep = run_shadow(huge, None)
    d = rep.to_dict()
    assert d["oversize_skipped"] is True, "an oversize frontier must set oversize_skipped=True"
    assert d["frontier_bytes"] >= OVERSIZE_FRONTIER_BYTES, (
        "the skipped body's size must be COUNTED (frontier_bytes) — count what wasn't inspected"
    )


# 2b — the skip does NOT silently populate bytes_by_class from an un-inspected body (comp gap)
def test_oversize_skip_does_not_fabricate_composition():
    huge = _body("x" * (OVERSIZE_FRONTIER_BYTES + 500_000))
    rep = run_shadow(huge, None)
    d = rep.to_dict()
    # bytes_by_class is R1's X; on a skipped body it must be EMPTY (the labeled gap), not a guess
    assert d["bytes_by_class"] == {}, (
        "bytes_by_class was populated from a body that was never decomposed — R1's X would be "
        "fabricated; a skip is a LABELED exclusion, not a silent composition estimate"
    )


# 3 — a body UNDER the threshold still gets the full compute (the budget only skips the tail)
def test_under_threshold_body_still_fully_observed():
    small = _body("x" * 10_000)  # well under
    rep = run_shadow(small, None)
    d = rep.to_dict()
    assert d["oversize_skipped"] is False
    assert d["bytes_by_class"], "an under-threshold body must still be decomposed (X computed)"
    assert d["n_blocks"] >= 1


# 4 — fail-open unchanged: an unparseable body is still an empty report, not an error
def test_unparseable_body_still_empty_report():
    rep = run_shadow(b"not json at all", None)
    d = rep.to_dict()
    assert d["oversize_skipped"] is False and d["bytes_by_class"] == {} and d["n_blocks"] == 0
