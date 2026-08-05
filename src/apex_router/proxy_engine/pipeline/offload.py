"""Offload pool + TTFT clock — keep CPU off the event loop. §2.3 / §6 step 5.

Compression is CPU-bound. Running it inline on the asyncio loop STALLS every other in-flight
request (internal review: the asyncio-stall finding — latency is the 3rd invariant wall). So
CPU-bound transforms run in a pool via run_in_executor, off the loop.

The inline/offload split (§6 step 5, decision #7):
  - transforms measured < 25ms at p99 on fixtures (compaction, terminal) run INLINE within the
    TTFT budget — they monetize turn 1.
  - everything else (astgrep, and M5's search/keep) OFFLOADS and monetizes at the frontier (§5.1)
    — it ships raw at turn N, compresses in the inter-turn gap, splices at N+1.

A ThreadPool suffices for v1: the transforms are either pure-Python string work (releases little)
or subprocess calls (ast-grep — releases the GIL while the child runs). ProcessPool is the escape
hatch if a future pure-Python transform becomes CPU-dominant (spec §12).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# Transforms whose measured p99 is under the inline threshold run on the request path.
INLINE_TRANSFORMS = frozenset({"compaction", "terminal"})
INLINE_BUDGET_MS = 25.0  # §6 step 5


class OffloadUnavailable(RuntimeError):
    """The pool could not run the work (shutting down / closed). The pipeline treats this as
    fail-open: ship the original block."""


@dataclass
class Timed:
    result: object
    elapsed_ms: float


class OffloadPool:
    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="apex-xf")
        self._closed = False

    async def run(self, fn: Callable[[], object]) -> Timed:
        """Run `fn` in the pool (off the event loop) and time it.

        If the pool is already shutting down, submit raises RuntimeError; we surface it as
        OffloadUnavailable so the pipeline can fall open (ship the original block) rather than
        leak a raw executor error (Codex M3). We do NOT catch asyncio.CancelledError — real task
        cancellation must propagate (swallowing it breaks structured cancellation); the
        pipeline's fail-open wrapper handles a cancelled offload by shipping the original.
        Exceptions from `fn` ITSELF propagate unchanged — the transform's fail-open signal (§6).
        """
        if self._closed:
            raise OffloadUnavailable("pool is closed")
        loop = asyncio.get_running_loop()
        t0 = time.perf_counter()
        try:
            result = await loop.run_in_executor(self._pool, fn)
        except RuntimeError as e:
            if "shutdown" in str(e).lower():
                raise OffloadUnavailable("pool is shutting down") from e
            raise
        return Timed(result, (time.perf_counter() - t0) * 1000.0)

    def run_sync_timed(self, fn: Callable[[], object]) -> Timed:
        """Run `fn` inline (caller is already off the hot path, or it's a sub-25ms transform),
        timed. Used for the inline split and for offline replay (no event loop)."""
        t0 = time.perf_counter()
        result = fn()
        return Timed(result, (time.perf_counter() - t0) * 1000.0)

    def close(self) -> None:
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)


def is_inline(transform_name: str) -> bool:
    return transform_name in INLINE_TRANSFORMS


def rendering_selectable(*, first_emitted: bool, rendering_ready: bool, elapsed_ms: float) -> bool:
    """Δ5 offload-deadline decision: may a rendering be chosen for this block right now?

    A rendering is selectable iff ALL hold:
      - the block has NOT yet had its first provider emission (`not first_emitted`) — after that
        emission the prefix is committed; applying a rendering would rewrite it and bust the cache
        (there is no "compress it next turn");
      - the rendering IS ready (`rendering_ready`) — an offloaded transform that hasn't produced a
        result by first-emission time can't be used (the block emits raw; a late worker result is
        discarded, never applied);
      - it completed within the inline budget (`elapsed_ms <= INLINE_BUDGET_MS`) — an overrun is a
        deadline MISS, so the block emits raw.

    A block that emits raw here is raw for the session forever via the committed-rendering identity
    (Δ4): `PrefixLedger.emit_committed` serves the stored (raw) bytes on every later turn."""
    return (not first_emitted) and rendering_ready and (elapsed_ms <= INLINE_BUDGET_MS)
