"""Cachegate — fail-CLOSED safety gate. §6 step 4.

Before any transform touches a block, the cachegate asks: is it SAFE to compress this? If there
is ANY doubt, it SKIPs (ships the original) with a reason code. This is fail-closed: the default
answer to "should I compress?" is NO unless the block is provably safe. Cache safety is a wall,
not a weight (spec decision #4) — a skipped compression costs a few tokens; a wrong compression
busts the cache (~20× more valuable) or corrupts bytes.

Skip reasons (each is a telemetry code):
  - "cache_control_protected": the block carries an explicit cache_control marker — the client
    pinned it as a cache breakpoint; touching it moves the breakpoint and busts the cache.
  - "behind_frontier": the block has already shipped (ship_count >= 2) — it is frozen (§5.1);
    re-rendering it is an invariant violation.
  - "frozen_prefix": the block sits inside the cached prefix the client is re-sending; only
    at/after the frontier is compression allowed (the reference proxy prefix_tracker.py:206 semantics).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SkipReason = Literal[
    "cache_control_protected", "behind_frontier", "frozen_prefix", "none",
]


@dataclass(frozen=True)
class GateDecision:
    compress: bool          # True → transforms may run; False → ship original
    reason: SkipReason      # why skipped (or "none" if compress=True)


def check(
    *,
    block_meta: dict[str, Any],
    ship_count: int,
    in_frozen_prefix: bool,
) -> GateDecision:
    """Decide whether `block` may be compressed. Fail-closed: any protective signal → SKIP.

    `block_meta` may carry `cache_control` (the client's explicit breakpoint marker).
    `ship_count` is from the freeze store (>=2 means behind the frontier).
    `in_frozen_prefix` is True when the block is within the cached prefix (not at/after frontier).
    """
    # 1. Explicit client cache_control marker → never touch (protected).
    if block_meta.get("cache_control") is not None:
        return GateDecision(False, "cache_control_protected")

    # 2. Behind the frontier: already shipped twice+ → frozen, re-render forbidden (§5.1).
    if ship_count >= 2:
        return GateDecision(False, "behind_frontier")

    # 3. Inside the cached prefix (not at/after the frontier) → skip; compressing here would
    #    change bytes the provider has already cached.
    if in_frozen_prefix:
        return GateDecision(False, "frozen_prefix")

    return GateDecision(True, "none")
