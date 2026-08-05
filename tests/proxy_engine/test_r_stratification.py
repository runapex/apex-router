"""Per-entry-position R — the stock-vs-flow fix (Fable, the reference window).

The compiler prices `saving(R)` and `retrieval_cost(R)` with R = `_requests_for_regime(band)`, a
session-AGGREGATE (e.g. 13 at the band-min). But a frontier block entering at turn 95 of a 100-request
session is re-read only ~5 times, not 13 — its real amortization horizon is (session_len - turn_index),
not the aggregate. Pricing every block at the aggregate R is the terminal/xl failure family in the TIME
dimension: a phantom amortization horizon the late-entering block never occupies.

`session_frontiers` iterates each session in turn order, so the remaining-requests count is available
at extraction. This pins that `Frontier` carries it, correctly, per block.
"""
from __future__ import annotations

from apex_router.proxy_engine.tuner.composition import session_frontiers
from apex_router.proxy_engine.tuner.replay import Request


def _session(sid: str, n_turns: int) -> list[Request]:
    """A clean growing-prefix session of n_turns requests."""
    reqs = []
    prev = ""
    for t in range(n_turns):
        content = (prev + f"turn-{t} content line\n").encode("utf-8")
        reqs.append(Request(sid, content, max(1, len(content) // 4), ts=float(t), model="opus"))
        prev = content.decode()
    return reqs


def test_frontier_carries_remaining_requests():
    """Each frontier block knows how many requests remain in its session after it — the real R."""
    frs = session_frontiers(_session("s0", 5))
    # 5 turns, indices 0..4; remaining_requests = (5-1) - index = re-reads after this turn
    remaining = [fr.remaining_requests for fr in frs]
    assert remaining == [4, 3, 2, 1, 0]


def test_early_block_has_larger_R_than_late_block():
    """A block entering early amortizes over more remaining requests than a late one — the whole
    point: R must vary by entry position, not be a session-constant."""
    frs = session_frontiers(_session("s0", 100))
    assert frs[0].remaining_requests == 99      # first turn: re-read by all 99 later requests
    assert frs[95].remaining_requests == 4      # turn 95 of 100: only 4 re-reads left
    assert frs[95].remaining_requests < frs[0].remaining_requests


def test_remaining_requests_is_per_session():
    """Two sessions of different lengths → each block's R is relative to its own session."""
    corpus = _session("short", 3) + _session("long", 10)
    frs = session_frontiers(corpus)
    short = [fr.remaining_requests for fr in frs if fr.req.session_id == "short"]
    long = [fr.remaining_requests for fr in frs if fr.req.session_id == "long"]
    assert short == [2, 1, 0]
    assert long == [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]
