"""Δ2 (revised — instrument lesson #7) — CANONICAL byte binning as the plane-neutral cell key.

History: the first Δ2 bridged compiler token-bins to runtime byte-bins with a byte/token RATIO, and
its invariant test asserted the ratio was "conservative". Codex found the hole: the test only checks
the min-bytes/token class, but routing applies to ALL classes and bytes/token varies by class, so
any single ratio misroutes some class in some direction (overshoot for low-density prose/code). The
fix is NOT a better ratio (the error's direction flips by class, no safe single ratio exists); it is
to DELETE the ratio and make byte strata the canonical cell key in BOTH planes. Compiler and runtime
call the SAME `size_stratum_bytes()` on the SAME observable (context bytes), so a routed block's
key == the compiled cell key by construction. Misrouting is undefined (no second binning to disagree
with). The old ratio-invariant test is gone; this pins the identity + blast-radius.
"""

from __future__ import annotations

from apex_router.proxy_engine.pipeline.decide import size_stratum_bytes as runtime_bin
from apex_router.proxy_engine.policy import BYTE_STRATA_BOUNDS, size_stratum_bytes

# ── the identity: both planes use the SAME binning fn on the SAME observable ──


def test_runtime_and_policy_use_the_same_binning_function():
    """decide's `size_stratum_bytes` IS apex_router.proxy_engine.policy.size_stratum_bytes — not a copy that can drift.
    (The old design had two boundary tables bridged by a ratio; the whole class of drift bug is gone
    because there is exactly one function.)"""
    assert runtime_bin is size_stratum_bytes


def test_compiler_and_runtime_agree_on_every_corpus_block():
    """THE identity (replaces the ratio inequality): for every real frontier block, the stratum the
    COMPILER binned it into equals the stratum the RUNTIME routes it to — same fn, same observable
    (context bytes), so they cannot disagree. An identity test has no density-class hole."""
    from apex_router.proxy_engine.tuner.composition import session_frontiers
    from fixtures.build_replay_corpus import build_corpus

    corpus, _ = build_corpus(
        "-Users-juri-kern-dev-the reference proxy", limit_sessions=3, min_turns=3, exclude_contaminated=True
    )
    for fr in session_frontiers(corpus):
        compiler_key = size_stratum_bytes(len(fr.req.content))  # what the compiler bins by
        runtime_key = runtime_bin(len(fr.req.content))  # what the runtime routes by
        assert compiler_key == runtime_key


# ── blast radius: a routing quirk is dollar-class, never fidelity-class ──


def test_size_stratum_bytes_is_total_and_valid():
    """Blast-radius pin (kept from the original): size_stratum_bytes always returns a valid stratum
    name for any input, so rule_for always finds a total cell — a routing quirk changes WHICH
    admitted rule applies (a dollar-class outcome), never whether a fidelity floor holds (floors
    are transform-level, not stratum-level)."""
    for b in (-5, 0, 7999, 8000, 8001, 511_999, 512_000, 10**9):
        assert size_stratum_bytes(b) in ("xs", "s", "m", "l", "xl")


def test_byte_strata_boundaries_monotone_and_total():
    names = [n for _, n in BYTE_STRATA_BOUNDS]
    bounds = [b for b, _ in BYTE_STRATA_BOUNDS]
    assert names == ["xs", "s", "m", "l"]  # xl is the implicit tail
    assert bounds == sorted(bounds) and len(set(bounds)) == len(bounds)  # strictly increasing
    assert size_stratum_bytes(0) == "xs"
    assert size_stratum_bytes(1 << 40) == "xl"


def test_no_ratio_conversion_survives():
    """Regression: the byte←token ratio (`_MIN_BYTES_PER_TOKEN`) that caused the class-dependent
    misroute must be GONE from the size-binning contract — its presence would mean the two-binning
    design crept back."""
    import apex_router.proxy_engine.policy as policy

    assert not hasattr(policy, "_MIN_BYTES_PER_TOKEN"), (
        "the byte/token ratio is back — canonical byte binning must not reintroduce a conversion"
    )
