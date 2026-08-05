"""Deterministic Anthropic-prefix-cache simulator — §8.2 `[NEW, round 2 §4]`.

Without this, "blended cost", "0 busts on replay", and the A/B are all undefined (the replay
optimizer prices reduction exactly but NOT cost — cost lives in the cache). The simulator models
how Anthropic caches the longest byte-prefix of the message history and re-reads it at a discount:

  - per SESSION, a store of previously-"written" byte prefixes (checkpoints).
  - each request: find the LONGEST stored prefix that is a byte-prefix of the incoming content.
    Those bytes are a cache READ (priced P_read); the remaining suffix is a cache WRITE
    (priced P_write) once it crosses the minimum cacheable size; below the minimum it is base.
  - idle TTL: a session's cache evicts after TTL_S of inactivity (wall-clock from the corpus).
  - a BUST is a request that SHOULD have hit (its prefix was cached and live) but did NOT, because
    the emitted bytes changed WITHIN the previously-cached span. Attributed by cause so the tuner
    never auto-reverts an innocent knob (a TTL-expiry miss is not a transform bust).

It emits per-request records with the SAME shape as the live telemetry event (§3.3
cache_read_tokens / cache_write_tokens / bust / bust_cause), so replay output and production
telemetry flow through one analysis path.

Honesty line (round 2): this prices COST under counterfactual invariance. It cannot score
fidelity or behavior — those stay with the tripwires + the §9 gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BustCause = Literal["none", "ttl", "transform", "client_edit", "frontier_rerender", "unknown"]


@dataclass(frozen=True)
class Pricing:
    """Pricing multipliers — config, not code (§8.2). Base input = 1.0×; a cache write costs
    1.25× (Anthropic's write premium), a cache read 0.10× (the 10× discount)."""
    p_write: float = 1.25
    p_read: float = 0.10
    p_base: float = 1.0
    p_output: float = 5.0          # output tokens ~5× input on Anthropic (for retrieval-turn cost)
    ttl_s: float = 300.0            # 5-min idle eviction
    min_cacheable_tokens: int = 1024


@dataclass
class CacheResult:
    """Per-request outcome, telemetry-shaped (§3.3)."""
    cache_read_tokens: int
    cache_write_tokens: int
    base_tokens: int               # fresh input below the min-cacheable floor (neither r nor w)
    bust: bool
    bust_cause: BustCause
    cost: float                    # blended: read*P_read + write*P_write + base*P_base

    def total_tokens(self) -> int:
        return self.cache_read_tokens + self.cache_write_tokens + self.base_tokens


@dataclass
class _SessionCache:
    """One session's cached prefix state. `prefix` is the bytes Anthropic currently holds cached;
    `token_len` is its length in TOKENS (we track both — bytes for matching, tokens for pricing);
    `last_seen` is wall-clock for TTL."""
    prefix: bytes = b""
    token_len: int = 0
    last_seen: float = 0.0


def _byte_prefix_len(cached: bytes, incoming: bytes) -> int:
    """Length (bytes) of the longest common prefix of `cached` and `incoming`."""
    n = min(len(cached), len(incoming))
    # bytes compare fast; find first differing index
    i = 0
    # chunked compare keeps it O(n) with low constant (avoids per-byte Python loop on the hit path)
    step = 4096
    while i < n:
        j = min(i + step, n)
        if cached[i:j] == incoming[i:j]:
            i = j
        else:
            while i < n and cached[i] == incoming[i]:
                i += 1
            break
    return i


class CacheSimulator:
    """Deterministic prefix-cache model. Feed it requests in time order per session; it returns a
    telemetry-shaped CacheResult each time and accumulates a profile."""

    def __init__(self, pricing: Pricing | None = None) -> None:
        self.pricing = pricing or Pricing()
        self._sessions: dict[str, _SessionCache] = {}
        self.total_read = 0
        self.total_write = 0
        self.total_base = 0
        self.total_cost = 0.0
        self.bust_counts: dict[str, int] = {}
        self.retrieval_count = 0
        self.total_retrieval_cost = 0.0

    def request(
        self,
        session_id: str,
        content: bytes,
        token_count: int,
        ts: float,
        *,
        prev_cached_diverged: bool = False,
        diverge_cause: BustCause = "unknown",
    ) -> CacheResult:
        """Simulate one request.

        `content` is the FULL message-prefix bytes the client sends this turn; `token_count` is
        its token length (from the corpus / tokenizer). `ts` is wall-clock seconds.
        `prev_cached_diverged` marks that the emitted bytes changed within the previously-cached
        span (a bust) — the caller (replay/live) knows this from the guard; the sim prices it.
        """
        p = self.pricing
        sess = self._sessions.get(session_id)

        # TTL eviction: the cache expires at last_seen + ttl_s, so idle >= ttl_s is expired
        # (Codex #7: the boundary is inclusive — a cache exactly ttl_s old is gone).
        if sess is not None and (ts - sess.last_seen) >= p.ttl_s:
            evicted_cause: BustCause = "ttl"
            sess = None
        else:
            evicted_cause = "none"

        if sess is None:
            # cold session (new or TTL-evicted): the whole thing is a write (above the floor) or
            # base (below it). No read. A TTL eviction is NOT a transform bust.
            read_tokens = 0
            if token_count >= p.min_cacheable_tokens:
                write_tokens, base_tokens = token_count, 0
                # a cacheable cold request establishes the live cached prefix
                self._sessions[session_id] = _SessionCache(content, token_count, ts)
            else:
                # below the floor: Anthropic does NOT cache it → no live prefix (Codex #1). A
                # later request must not read from a prefix that was never written.
                write_tokens, base_tokens = 0, token_count
                self._sessions.pop(session_id, None)
            bust = evicted_cause == "ttl"
            cause: BustCause = "ttl" if bust else "none"
        else:
            # warm session: match the longest byte-prefix against the cached bytes.
            match_bytes = _byte_prefix_len(sess.prefix, content)
            # convert matched BYTES to matched TOKENS proportionally (the corpus gives us tokens
            # per full request, not per byte; a proportional split is the faithful estimate — an
            # approximation, since BPE token density is not uniform over bytes, Codex #2).
            frac = match_bytes / len(sess.prefix) if sess.prefix else 0.0
            read_tokens = int(round(sess.token_len * frac))
            # a read can never exceed this request's own token count (Codex #2: proportional
            # conversion could otherwise report more read tokens than the request contains).
            read_tokens = min(read_tokens, token_count)
            remainder = token_count - read_tokens

            if prev_cached_diverged:
                # apex (or the client) changed bytes inside the cached span → the cache can't
                # reuse the prefix past the divergence point: it all re-writes. This is the bust.
                read_tokens = 0
                remainder = token_count
                bust = True
                cause = diverge_cause
            else:
                bust = False
                cause = "none"

            if remainder >= p.min_cacheable_tokens or read_tokens > 0:
                write_tokens, base_tokens = remainder, 0
            else:
                write_tokens, base_tokens = 0, remainder
            # the new cached prefix is the full content sent this turn
            self._sessions[session_id] = _SessionCache(content, token_count, ts)

        cost = (read_tokens * p.p_read + write_tokens * p.p_write + base_tokens * p.p_base)
        self.total_read += read_tokens
        self.total_write += write_tokens
        self.total_base += base_tokens
        self.total_cost += cost
        if bust:
            self.bust_counts[cause] = self.bust_counts.get(cause, 0) + 1

        return CacheResult(read_tokens, write_tokens, base_tokens, bust, cause, cost)

    def profile(self) -> dict:
        """Aggregate, comparable to the reference proxy's /stats prefix_cache totals."""
        transform_busts = self.bust_counts.get("transform", 0)
        return {
            "cache_read_tokens": self.total_read,
            "cache_write_tokens": self.total_write,
            "base_tokens": self.total_base,
            "total_cost": self.total_cost,
            "bust_counts": dict(self.bust_counts),
            "transform_bust_count": transform_busts,
            "retrieval_count": self.retrieval_count,
            "retrieval_cost": self.total_retrieval_cost,
            "total_cost_with_retrieval": self.total_cost + self.total_retrieval_cost,
        }

    def retrieval(
        self, session_id: str, retrieved_block_tokens: int, remaining_requests: int,
        output_tokens: int = 500,
    ) -> float:
        """Price ONE CCR retrieval event and accumulate it (internal review round-3: retrieval permanently
        REVERSES compression and is most expensive on xl, so it must be priced, not treated as a
        free safety valve). Three components:
          (a) an extra turn whose input is the ENTIRE current cached context, at read price;
          (b) the retrieved original enters the prefix FOREVER: write once + read every remaining
              turn — so you now pay for the compressed copy AND the original;
          (c) output tokens on the extra turn, at output price.
        The punitive ratio is cost∝L (context length) vs the compression saving∝B (block size),
        and B/L is smallest on xl by definition → the safety valve costs most where it's needed
        most. Break-even retrieval probability lands ~5–15%, so retrieval-rate is a DOLLAR
        invariant (an M7 tripwire), not just a fidelity signal.
        """
        p = self.pricing
        sess = self._sessions.get(session_id)
        context_tokens = sess.token_len if sess else retrieved_block_tokens
        a = context_tokens * p.p_read                                   # (a) full-context read
        b = retrieved_block_tokens * p.p_write + \
            retrieved_block_tokens * p.p_read * remaining_requests         # (b) original rides forever
        c = output_tokens * p.p_output                                  # (c) extra-turn output
        cost = a + b + c
        self.retrieval_count += 1
        self.total_retrieval_cost += cost
        return cost

    @staticmethod
    def retrieval_break_even_prob(
        block_tokens: int, retain_fraction: float, remaining_requests: int,
        context_tokens: int, pricing: Pricing | None = None, output_tokens: int = 500,
    ) -> float:
        """The retrieval probability at which a compressed block's expected retrieval cost equals
        its compression saving. Below this, compression nets positive; above it, net-negative.
        Used to floor the retrieval-rate tripwire as a DOLLAR invariant."""
        p = pricing or Pricing()
        saving = (1 - retain_fraction) * block_tokens * p.p_read * remaining_requests
        a = context_tokens * p.p_read
        b = block_tokens * p.p_write + block_tokens * p.p_read * remaining_requests
        c = output_tokens * p.p_output
        retrieval_cost = a + b + c
        return saving / retrieval_cost if retrieval_cost else 0.0
