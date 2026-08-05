"""M3 — offload pool + inline/offload split + TTFT budget under concurrent load. §2.3 / §6.

Exit criterion (build-plan §10): "TTFT budget held under concurrent load test." The point of
offloading CPU off the event loop is that a slow transform on one request must not stall the
loop for others. We verify that a CPU-bound transform running in the pool does NOT block
concurrent async tasks beyond the pool's own latency.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from apex_router.proxy_engine.pipeline.offload import OffloadPool, OffloadUnavailable, is_inline


@pytest.mark.asyncio
async def test_close_yields_offload_unavailable_not_raw_error():
    """xval: submitting to a closed pool surfaces OffloadUnavailable (fail-open signal), not a
    raw executor RuntimeError/CancelledError leaked to the awaiter."""
    pool = OffloadPool(max_workers=1)
    pool.close()
    with pytest.raises(OffloadUnavailable):
        await pool.run(lambda: 1)


def test_inline_split_classification():
    # sub-25ms lossless transforms run inline (monetize turn 1)
    assert is_inline("compaction")
    assert is_inline("terminal")
    # everything else offloads and monetizes at the frontier
    assert not is_inline("astgrep")
    assert not is_inline("search")
    assert not is_inline("keep")


@pytest.mark.asyncio
async def test_offload_runs_off_event_loop():
    """A CPU-bound fn in the pool must not block a concurrent async heartbeat. If it ran
    inline on the loop, the heartbeat would be starved for the whole busy period."""
    pool = OffloadPool(max_workers=2)
    try:
        ticks = []

        async def heartbeat():
            for _ in range(20):
                ticks.append(time.perf_counter())
                await asyncio.sleep(0.005)

        def cpu_bound():
            # ~50ms of pure-Python work (would stall the loop if run inline)
            s = 0
            end = time.perf_counter() + 0.05
            while time.perf_counter() < end:
                s += 1
            return s

        hb = asyncio.create_task(heartbeat())
        timed = await pool.run(cpu_bound)
        await hb

        # the heartbeat kept ticking during the CPU work → loop was not starved
        gaps = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
        max_gap_ms = max(gaps) * 1000
        assert max_gap_ms < 25, f"event loop starved for {max_gap_ms:.1f}ms (offload failed)"
        assert timed.elapsed_ms >= 45  # the work really took ~50ms
    finally:
        pool.close()


@pytest.mark.asyncio
async def test_concurrent_offloads_run_in_parallel_not_serialized():
    """Many concurrent offloads complete in ~max-latency, not sum-of-latencies — proof they
    ran off the loop in parallel. Uses time.sleep as the workload: it RELEASES the GIL (as
    ast-grep's subprocess does), so this tests the real offload property, not GIL contention
    from a pure-Python busy-spin (which threads cannot parallelize)."""
    pool = OffloadPool(max_workers=8)
    try:
        def work():
            time.sleep(0.03)  # GIL-releasing, like a subprocess transform
            return 1

        n = 8
        t0 = time.perf_counter()
        results = await asyncio.gather(*[pool.run(work) for _ in range(n)])
        wall_ms = (time.perf_counter() - t0) * 1000

        assert all(t.result == 1 for t in results)
        # serialized would be ~n*30ms = 240ms; parallel across 8 workers ≈ 30ms. Generous
        # bound (100ms) absorbs scheduler noise while still proving non-serialization.
        assert wall_ms < 100, f"offloads serialized: {wall_ms:.0f}ms for {n}×30ms tasks"
    finally:
        pool.close()
