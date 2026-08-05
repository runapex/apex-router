"""M4 — cache simulator calibration + correctness. §8.2.

The spec's calibration gate is "retrodict the reference proxy's hit/bust profile within ±10% hit tokens,
exact bust count." A methodology finding (documented in the M4 benchmark report): NO existing
corpus has both real message bytes AND real per-session request sequences — the ledger logs
only COMPRESSION events (a subsample of requests), has no session id (pid ≠ session), and no
message bytes. So the ledger cannot retrodict the FULL cache profile (~6-7:1 achievable vs
the reference proxy's measured 21:1). Full-profile calibration is deferred to the live-capture loop
(directive #3).

What we CAN and DO gate on here:
  1. EXACT correctness on controlled synthetic sessions — known inputs → known read/write/bust.
     This is the real proof the model is right; a sim that miscounts here retrodicts nothing.
  2. The aggregate ledger retrodiction runs and stays within a DOCUMENTED (loose) tolerance,
     with the corpus caveat recorded — so a gross regression is still caught.
"""
from __future__ import annotations

from apex_router.proxy_engine.tuner.cachesim import CacheSimulator, Pricing

# ---- 1. EXACT correctness on controlled sessions (the real gate) ----

def test_growing_session_reads_cached_prefix():
    """Full-history-resend: turn N reads the whole prior prefix, writes only the new suffix."""
    sim = CacheSimulator()
    content = b""
    tok = 0
    results = []
    for _turn in range(4):
        content += b"X" * 8000  # +8000 bytes
        tok += 2000            # +2000 tokens
        results.append(sim.request("s", content, tok, ts=1000.0 + _turn))
    # turn 0: cold → all write (above the 1024 floor)
    assert results[0].cache_read_tokens == 0
    assert results[0].cache_write_tokens == 2000
    # turn 1: reads the 2000 cached, writes the new 2000
    assert results[1].cache_read_tokens == 2000
    assert results[1].cache_write_tokens == 2000
    # turn 3: reads 6000 cached, writes 2000 new
    assert results[3].cache_read_tokens == 6000
    assert results[3].cache_write_tokens == 2000


def test_ttl_eviction_is_a_bust_not_a_hit():
    """A request after > ttl_s idle finds a cold cache → write, bust_cause=ttl (NOT transform)."""
    sim = CacheSimulator()
    sim.request("s", b"X" * 8000, 2000, ts=1000.0)
    r = sim.request("s", b"X" * 12000, 3000, ts=1000.0 + 400)  # 400s > 300 TTL
    assert r.cache_read_tokens == 0
    assert r.cache_write_tokens == 3000
    assert r.bust is True and r.bust_cause == "ttl"


def test_transform_divergence_is_a_transform_bust():
    """When the emitted bytes changed within the cached span (guard-detected), the whole prefix
    re-writes and the bust is attributed to `transform` — the one the tripwire counts."""
    sim = CacheSimulator()
    sim.request("s", b"X" * 8000, 2000, ts=1000.0)
    r = sim.request("s", b"X" * 12000, 3000, ts=1000.0 + 10,
                    prev_cached_diverged=True, diverge_cause="transform")
    assert r.bust is True and r.bust_cause == "transform"
    assert r.cache_read_tokens == 0  # nothing reusable past the divergence
    assert r.cache_write_tokens == 3000
    assert sim.profile()["transform_bust_count"] == 1


def test_below_min_cacheable_is_base_not_write():
    """A cold request under the 1024-token floor is base cost, not a cache write (too small to
    cache)."""
    sim = CacheSimulator()
    r = sim.request("s", b"X" * 400, 500, ts=1000.0)  # 500 < 1024
    assert r.cache_write_tokens == 0 and r.base_tokens == 500


def test_pricing_blended_cost():
    """Cost = read*P_read + write*P_write + base*P_base, from the config pricing table."""
    p = Pricing(p_write=1.25, p_read=0.10, p_base=1.0)
    sim = CacheSimulator(p)
    sim.request("s", b"X" * 8000, 2000, ts=1000.0)              # write 2000
    r = sim.request("s", b"X" * 16000, 4000, ts=1000.0 + 5)     # read 2000, write 2000
    assert r.cost == 2000 * 0.10 + 2000 * 1.25  # 200 + 2500 = 2700
    # aggregate: turn0 write 2000*1.25=2500 + turn1 2700
    assert sim.profile()["total_cost"] == 2500 + 2700


def test_partial_byte_prefix_match():
    """If the incoming content diverges from the cached bytes partway, only the common byte
    prefix reads; the rest writes."""
    sim = CacheSimulator()
    sim.request("s", b"A" * 8000, 2000, ts=1000.0)
    # next turn: first 4000 bytes match (half the cached prefix), then diverge + grow
    r = sim.request("s", b"A" * 4000 + b"B" * 8000, 3000, ts=1000.0 + 5)
    # half of the 2000 cached tokens are readable (4000/8000 byte match)
    assert r.cache_read_tokens == 1000
    assert r.cache_write_tokens == 2000  # 3000 total - 1000 read


# ---- cross-validation xval regressions ----

def test_read_tokens_never_exceed_request_tokens():
    """xval #2: a byte-dense cached prefix vs a token-sparse incoming request must not report
    more read tokens than the request contains (impossible total)."""
    sim = CacheSimulator()
    sim.request("s", b"X" * 8000, 2000, ts=1000.0)         # cache 8000B / 2000 tok
    r = sim.request("s", b"X" * 4000, 300, ts=1000.0 + 5)  # incoming 4000B but only 300 tok
    assert r.cache_read_tokens <= 300
    assert r.total_tokens() == 300  # conserved, no impossible surplus


def test_below_floor_cold_request_not_cached():
    """xval #1: a sub-floor cold request is base-priced AND leaves no live cache — a later
    request must not read a prefix Anthropic never wrote."""
    sim = CacheSimulator()
    sim.request("s", b"Y" * 400, 500, ts=1000.0)                  # 500 < 1024 floor
    r = sim.request("s", b"Y" * 400 + b"Z" * 8000, 2500, ts=1000.0 + 5)
    assert r.cache_read_tokens == 0  # the sub-floor prefix was not cached, so nothing to read


def test_ttl_boundary_is_inclusive():
    """xval #7: a cache exactly ttl_s old is EXPIRED (>= boundary)."""
    sim = CacheSimulator()
    sim.request("s", b"X" * 8000, 2000, ts=1000.0)
    r = sim.request("s", b"X" * 8000, 2000, ts=1000.0 + 300.0)  # exactly 300s = ttl_s
    assert r.bust is True and r.bust_cause == "ttl"


# ---- 2. Aggregate ledger retrodiction (documented tolerance, corpus caveat) ----

def test_ledger_aggregate_retrodiction_in_documented_tolerance():
    """Runs the ledger through the sim and checks the aggregate read:write ratio is in a
    DOCUMENTED range. NOT the spec's ±10% (the ledger is a compression-event subsample with no
    session id — see module docstring); this catches a GROSS regression, not fine calibration.
    Full-profile calibration is deferred to the live-capture loop."""
    import calendar
    import glob
    import json
    import os
    import time as _t

    ledger = os.path.expanduser("~/.the reference proxy/savings_events.jsonl")
    if not glob.glob(ledger):
        import pytest
        pytest.skip("the reference proxy ledger not present")

    def pts(s):
        try:
            return calendar.timegm(_t.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            return 0

    rows = [json.loads(x) for x in open(ledger) if x.strip()]
    rows = [r for r in rows if r.get("before", 0) > 0]
    rows.sort(key=lambda r: r.get("ts", ""))
    sim = CacheSimulator()
    # group by pid + 1h idle window (best available session proxy — pid ≠ session, documented)
    last: dict = {}
    counter = 0
    sid_by_pid: dict = {}
    for r in rows:
        pid = r.get("pid")
        ts = pts(r.get("ts"))
        prev = last.get(pid)
        if prev is None or (ts - prev) > 3600:
            counter += 1
            sid_by_pid[pid] = f"led{counter}"
        sid = sid_by_pid[pid]
        before = r["before"]
        sim.request(sid, b"X" * (before * 4), before, ts)
        last[pid] = ts
    prof = sim.profile()
    r_, w_ = prof["cache_read_tokens"], prof["cache_write_tokens"]
    assert r_ > 0 and w_ > 0
    ratio = r_ / w_
    # documented achievable band for this subsample corpus (the reference proxy's true profile is ~21:1;
    # the ledger yields ~6-7:1 — see module docstring). Guard against gross breakage only.
    assert 3.0 < ratio < 15.0, f"ledger ratio {ratio:.1f} outside documented band"
    # no transform busts should appear from a pure token-growth replay (no divergence injected)
    assert prof["transform_bust_count"] == 0


# ---- Fable round-3: CCR retrieval re-inflation pricing ----

def test_retrieval_priced_and_expensive_on_xl():
    """A CCR retrieval permanently reverses compression and costs ∝ context length L, while the
    compression saving is ∝ block size B — so on xl (B/L tiny) retrieval is punishingly expensive.
    Verifies Fable round-3's worked example reproduces."""
    sim = CacheSimulator()
    # establish a big xl context (L=200k tokens)
    sim.request("s", b"X" * (200_000 * 4), 200_000, ts=1000.0)
    # retrieve a 1k compressed block with 40 remaining turns
    cost = sim.retrieval("s", retrieved_block_tokens=1_000, remaining_requests=40, output_tokens=500)
    # Fable: ~23-25k token-equiv (full-context read dominates)
    assert 20_000 < cost < 30_000
    prof = sim.profile()
    assert prof["retrieval_count"] == 1
    assert prof["total_cost_with_retrieval"] >= prof["total_cost"]


def test_retrieval_break_even_probability():
    """Break-even retrieval prob for an xl block lands ~5-15% (Fable) — so retrieval-rate is a
    DOLLAR invariant: above it, compressing the block is net-negative."""
    p_be = CacheSimulator.retrieval_break_even_prob(
        block_tokens=1_000, retain_fraction=0.2, remaining_requests=40, context_tokens=200_000)
    assert 0.05 < p_be < 0.15
    # and it SHRINKS as context grows (retrieval more expensive on bigger xl)
    p_be_bigger = CacheSimulator.retrieval_break_even_prob(
        block_tokens=1_000, retain_fraction=0.2, remaining_requests=40, context_tokens=400_000)
    assert p_be_bigger < p_be
