"""Wire-asymmetry of the cache accounting — the Codex-xval finding that the doctor computed the
OpenAI served fraction backwards.

Ground truth (verified against real telemetry + the scanner in apex/proxy/usage.py):
  - ANTHROPIC (anthropic): provider `input_tokens` is the FRESH (uncached) remainder; `cache_read`
    and `cache_write` are DISJOINT siblings. A real row: tokens_in=2, cache_read=17490.
  - OPENAI (openai/Responses): provider `input_tokens` is the TOTAL prompt; `cache_read`
    (input_tokens_details.cached_tokens) is a SUBSET of it. A real row: tokens_in=14986,
    cache_read=14208 → true fresh input is 778, not 14986.

The doctor treated `tokens_in` as fresh on BOTH wires, so for OpenAI it double-counted the cached
prefix: served read/(read+total) instead of read/total. On the snapshot that turned a 44.5%-served
Codex session into a reported 30.8% (matching the decision-log's independently-recorded 44.5%), and
would fire the prefix-instability alarm on a FULLY cached session. These tests pin the fix.
"""
from __future__ import annotations

from apex_router.proxy_engine.readout.doctor import prefix_instability_alarm, session_health
from apex_router.proxy_engine.readout.pricing import rates_for


def _openai(session, tokens_in_total, cache_read, output=50):
    """An OpenAI/Responses generative row: `tokens_in` is the TOTAL prompt, `cache_read` a subset."""
    return {
        "schema_version": 3, "session_id": session, "endpoint_id": "openai",
        "model_resolved": "gpt-5", "is_error": False, "tokens_out": output,
        "tokens_in": tokens_in_total,
        "cache_read_tokens": cache_read, "cache_write_tokens": 0,
        "usage": {"input_tokens": tokens_in_total, "output_tokens": output,
                  "input_tokens_details": {"cached_tokens": cache_read}},
    }


def _anthropic(session, fresh_input, cache_read, cache_write, output=50):
    """An Anthropic/anthropic row: `tokens_in` is the FRESH remainder; read/write are disjoint."""
    return {
        "schema_version": 3, "session_id": session, "endpoint_id": "anthropic",
        "model_resolved": "opus", "is_error": False, "tokens_out": output,
        "tokens_in": fresh_input,
        "cache_read_tokens": cache_read, "cache_write_tokens": cache_write,
        "usage": {"input_tokens": fresh_input, "output_tokens": output},
    }


def test_openai_uncached_excludes_the_cached_subset():
    # tokens_in=1000 TOTAL, cache_read=700 → fresh uncached is 300, not 1000.
    h = session_health([_openai("s", tokens_in_total=1000, cache_read=700)])[("s", None, "openai", "gpt-5")]
    assert h.sum_input_uncached == 300, "OpenAI fresh input = tokens_in − cache_read (subset)"
    # served fraction is read/total = 700/1000 = 0.70, NOT read/(read+total) = 0.4118
    assert abs(h.hit_rate - 0.70) < 1e-9


def test_anthropic_uncached_is_the_fresh_input_unchanged():
    # anthropic: tokens_in is ALREADY the fresh remainder; must be left as-is (disjoint from read).
    h = session_health([_anthropic("s", fresh_input=2, cache_read=17490, cache_write=300)])[("s", None, "anthropic", "opus")]
    assert h.sum_input_uncached == 2, "Anthropic tokens_in is fresh-only — do NOT subtract read"
    assert h.hit_rate == 17490 / (17490 + 2)          # read/(read+fresh)
    assert h.hit_rate_incl_write == 17490 / (17490 + 300 + 2)


def test_fully_cached_openai_session_does_not_alarm():
    # Codex's reproduction: a 100k-token fully-cached OpenAI session is HEALTHY (served 100%).
    # The buggy doctor read served=50% and fired a false instability alarm + fake overpay.
    rows = [_openai("s", tokens_in_total=100_000, cache_read=100_000) for _ in range(4)]
    h = session_health(rows)[("s", None, "openai", "gpt-5")]  # keyed by (session_id, model)
    assert h.hit_rate == 1.0, "fully-cached OpenAI session is 100% served"
    assert prefix_instability_alarm(h, rates_for("gpt-5", "openai")) is None, (
        "a fully-cached session must NOT alarm (the wire-double-count made it fire)"
    )


def test_cold_start_single_turn_does_not_alarm():
    # A single first request has no PRIOR prefix to have read from cache — 0% served is compulsory,
    # not instability. Requires >= 2 generative turns before the alarm can fire (Codex F3).
    h = session_health([_openai("s", tokens_in_total=80_000, cache_read=0)])[("s", None, "openai", "gpt-5")]
    assert h.n_generative == 1
    assert prefix_instability_alarm(h, rates_for("gpt-5", "openai")) is None, (
        "a lone cold-start turn must not be flagged as prefix instability"
    )


def test_healthy_anthropic_session_with_cache_creation_does_not_false_alarm():
    # Codex F4 proposed adding cache_write to the served denominator. REJECTED at ground truth: the
    # 0.939 floor was DERIVED from read/(read+fresh) (excl-write) — real healthy Anthropic sessions
    # have read/(read+WRITE+fresh) as low as 0.332 because cache CREATION is normal, not instability.
    # This pins that a healthy session doing heavy cache creation (big write, tiny fresh, good reads)
    # stays healthy — the served fraction and its floor are a matched excl-write pair by design.
    rows = [_anthropic("s", fresh_input=200, cache_read=400_000, cache_write=350_000) for _ in range(4)]
    h = session_health(rows)[("s", None, "anthropic", "opus")]
    a = prefix_instability_alarm(h, rates_for("opus", "anthropic"))
    assert a is None, "cache-creation writes must not push a well-served session below the floor"


def test_no_session_bucket_does_not_merge_distinct_models():
    # Codex F7: no-session traffic on one endpoint must not collapse two models into one bucket
    # priced by whichever appeared first. Key the fallback bucket by (endpoint, model).
    rows = [
        _openai(None, 1000, 700), _openai(None, 1000, 700),
        {**_openai(None, 1000, 700), "model_resolved": "gpt-5-nano"},
    ]
    health = session_health(rows)
    models = {h.model for h in health.values()}
    assert models == {"gpt-5", "gpt-5-nano"}, "distinct models must not share one no-session bucket"
