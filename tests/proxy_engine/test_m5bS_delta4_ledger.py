"""Δ4 — rendering ledger, the transactional core (roadmap §1 / baseline v2 §3.2). The v1 runtime had
no transactional discipline: a retry could re-run a transform, a crash between persist and forward
left ambiguous state, and "has this block shipped?" was the indirect `ship_count >= 2` heuristic.
Δ4 adds a per-session `PrefixLedger` with:
  - CAS on `revision` (single-writer actor per session serializes concurrent turns);
  - idempotent retry: the same `request_id` reuses the persisted rendering BYTE-IDENTICALLY (never
    re-runs the transform);
  - persist-before-egress: original + rendering are stored BEFORE the bytes go out; an ambiguous
    delivery marks the ledger `uncertain` and recovery re-serves the stored bytes;
  - committed-rendering identity: a block ever committed ships its stored bytes forever (replaces
    `ship_count >= 2`);
  - epoch isolation: a bundle swap mid-request completes under the request's reserved epoch.
"""

from __future__ import annotations

import pytest

from apex_router.proxy_engine.session.ledger import PrefixLedger, StaleRevision


def test_retry_same_request_id_reuses_rendering_byte_identical():
    """A retry with the same request_id must return the stored rendering and NOT re-run the
    transform (asserted via a call counter on the render fn)."""
    led = PrefixLedger("s")
    calls = {"n": 0}

    def render():
        calls["n"] += 1
        return b"RENDERED-BYTES"

    a = led.commit_turn(request_id="req-1", block_id="b0", original=b"ORIG", render=render, epoch=1)
    b = led.commit_turn(request_id="req-1", block_id="b0", original=b"ORIG", render=render, epoch=1)
    assert a == b == b"RENDERED-BYTES"
    assert calls["n"] == 1, (
        "the transform must run once for a repeated request_id (idempotent retry)"
    )


def test_committed_block_ships_stored_bytes_forever():
    """Once a block is committed, a later turn for the SAME block_id serves the stored rendering —
    even if a new render fn would produce different bytes. Replaces the `ship_count >= 2` rule."""
    led = PrefixLedger("s")
    led.commit_turn(request_id="r1", block_id="b0", original=b"O", render=lambda: b"FIRST", epoch=1)
    # a later turn (different request_id) for the same block, whose render would differ:
    out = led.emit_committed(block_id="b0", fallback_render=lambda: b"DIFFERENT")
    assert out == b"FIRST", "a committed block must ship its stored identity, never a re-render"


def test_uncommitted_block_uses_fallback():
    led = PrefixLedger("s")
    out = led.emit_committed(block_id="never-seen", fallback_render=lambda: b"RAW")
    assert out == b"RAW"


def test_persist_before_egress_and_uncertain_recovery():
    """An ambiguous delivery (crash between persist and forward) marks the turn `uncertain`; then
    recovery re-serves the STORED bytes and the ledger stays consistent (persist before egress)."""
    led = PrefixLedger("s")
    led.commit_turn(request_id="r1", block_id="b0", original=b"O", render=lambda: b"BYTES", epoch=1)
    led.mark_uncertain(request_id="r1")
    assert led.status(request_id="r1") == "uncertain"
    # recovery serves the persisted rendering (the bytes were stored before the ambiguous send)
    assert led.recover(request_id="r1") == b"BYTES"


def test_concurrent_turns_serialize_on_revision():
    """Two turns reserving the same revision → the second CAS fails (StaleRevision); the holder
    commits by presenting its reserved revision; the ledger advances by exactly one."""
    led = PrefixLedger("s")
    rev = led.revision()
    led.reserve(rev)  # turn A reserves the current revision
    with pytest.raises(StaleRevision):
        led.reserve(rev)  # turn B reserves the SAME stale revision
    # the holder commits by presenting its reserved revision (cross-validation: CAS enforced at commit)
    led.commit_turn(request_id="rA", block_id="b0", original=b"O", render=lambda: b"X", epoch=1,
                    expected_revision=rev)
    assert led.revision() == rev + 1  # exactly one advance


def test_commit_cannot_bypass_a_held_reservation():
    """cross-validation: a DIFFERENT turn cannot commit while another holds the reservation — it is a real
    CAS token, not a decoration. A non-holder (no / wrong expected_revision) is stale."""
    led = PrefixLedger("s")
    rev = led.revision()
    led.reserve(rev)  # turn A holds revision `rev`
    calls = {"n": 0}

    def render():
        calls["n"] += 1
        return b"X"

    # turn B tries to commit WITHOUT holding the reservation → blocked, and render never runs.
    with pytest.raises(StaleRevision):
        led.commit_turn(request_id="B", block_id="b0", original=b"O", render=render, epoch=1)
    assert calls["n"] == 0, "a blocked commit must not run the transform (no duplicate render)"
    assert led.revision() == rev  # unchanged — no lost update


def test_idempotent_retry_bypasses_the_cas():
    """An idempotent retry (same request_id already committed) re-serves its bytes even if a later
    reservation is now held — re-serving a committed turn is always safe."""
    led = PrefixLedger("s")
    led.commit_turn(request_id="r1", block_id="b0", original=b"O", render=lambda: b"FIRST", epoch=1)
    led.reserve(led.revision())  # some other turn now holds a reservation
    # the retry of r1 still returns its stored bytes, reservation notwithstanding
    assert led.commit_turn(request_id="r1", block_id="b0", original=b"O",
                           render=lambda: b"SECOND", epoch=1) == b"FIRST"


def test_revision_strictly_monotonic_across_turns():
    led = PrefixLedger("s")
    revs = []
    for i in range(4):
        led.commit_turn(
            request_id=f"r{i}",
            block_id=f"b{i}",
            original=b"O",
            render=lambda i=i: f"X{i}".encode(),
            epoch=1,
        )
        revs.append(led.revision())
    assert revs == sorted(revs) and len(set(revs)) == len(revs)  # strictly increasing


def test_epoch_reserved_at_turn_start_survives_midflight_swap():
    """A turn commits under the epoch it started with; a later bundle swap (higher epoch) does not
    retroactively change an in-flight/committed turn's recorded epoch."""
    led = PrefixLedger("s")
    led.commit_turn(request_id="r1", block_id="b0", original=b"O", render=lambda: b"X", epoch=7)
    # epoch bumps for the NEXT turn, but the committed turn keeps epoch 7
    led.commit_turn(request_id="r2", block_id="b1", original=b"O", render=lambda: b"Y", epoch=8)
    assert led.epoch_of(request_id="r1") == 7
    assert led.epoch_of(request_id="r2") == 8
