"""Δ5 — offload deadline (roadmap §1 / baseline v2 §3.6). A rendering may be selected for a block
ONLY before that block's FIRST provider emission. If the (possibly offloaded) transform is not ready
in time, the block emits RAW — and, because a committed rendering is permanent (Δ4), it stays raw
for the session forever. There is no "compress it next turn": that would rewrite a committed prefix
and bust the cache. Spec + pin (V2: no live path applies a rendering post-first-emission yet).
"""

from __future__ import annotations

from apex_router.proxy_engine.pipeline.offload import INLINE_BUDGET_MS, rendering_selectable
from apex_router.proxy_engine.session.ledger import PrefixLedger

# ── the selectability decision ──


def test_inline_result_within_deadline_is_selectable():
    assert (
        rendering_selectable(
            first_emitted=False, rendering_ready=True, elapsed_ms=INLINE_BUDGET_MS - 1
        )
        is True
    )


def test_deadline_miss_is_not_selectable():
    """A transform that ran but overran the inline budget cannot be selected for first emission."""
    assert (
        rendering_selectable(
            first_emitted=False, rendering_ready=True, elapsed_ms=INLINE_BUDGET_MS + 1
        )
        is False
    )


def test_rendering_not_ready_is_not_selectable():
    """An offloaded transform that hasn't produced a result by first-emission time is not selectable
    (the block emits raw; the worker result, if it lands later, is discarded — never applied)."""
    assert rendering_selectable(first_emitted=False, rendering_ready=False, elapsed_ms=0.0) is False


def test_after_first_emission_never_selectable():
    """Once the block has had its first provider emission, NO rendering may be selected — even a
    ready, in-budget one. Applying it would rewrite a committed prefix (cache bust)."""
    assert rendering_selectable(first_emitted=True, rendering_ready=True, elapsed_ms=0.0) is False


# ── composition with the ledger: a deadline miss is permanent ──


def test_deadline_miss_raw_forever_via_ledger():
    """A block that misses the deadline ships raw and — via the committed-rendering identity (Δ4) —
    ships raw on every later turn, even if a transform later becomes available."""
    led = PrefixLedger("s")

    # turn 1: the transform wasn't ready in time → the block is committed RAW.
    raw = b"RAW-ORIGINAL-BYTES"
    selectable = rendering_selectable(first_emitted=False, rendering_ready=False, elapsed_ms=999.0)
    assert selectable is False
    led.commit_turn(
        request_id="r1", block_id="b0", original=raw, render=lambda: raw, epoch=1
    )  # committed raw (render == original)

    # a later turn for the same block, now WITH a ready transform, still ships the committed raw.
    out = led.emit_committed(block_id="b0", fallback_render=lambda: b"TRANSFORMED-LATE")
    assert out == raw, "a block that shipped raw once must ship raw forever (no late compression)"


def test_precomputed_worker_result_used_at_first_emission():
    """The happy path: an offloaded worker result that IS ready before first emission is selectable
    and gets committed as the block's rendering."""
    led = PrefixLedger("s")
    transformed = b"TRANSFORMED"
    assert rendering_selectable(first_emitted=False, rendering_ready=True, elapsed_ms=5.0) is True
    out = led.commit_turn(
        request_id="r1", block_id="b0", original=b"ORIG", render=lambda: transformed, epoch=1
    )
    assert out == transformed
