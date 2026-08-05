"""Δ4 — the rendering ledger: the transactional core the v1 runtime lacked (baseline v2 §3.2).

Per session, an atomic ledger that makes emission a committed, idempotent, crash-recoverable step
instead of a best-effort recompute. It is the state that lets the runtime answer, safely:
  - "did I already render this block?"  → committed-rendering identity (replaces `ship_count >= 2`);
  - "is this a retry?"                  → idempotent by request_id (never re-runs the transform);
  - "did my last send land?"            → per-turn status {committed, uncertain}, from the store.

Single-writer per session: every mutation runs under one re-entrant lock, so concurrent turns of a
session serialize (the "single-writer actor" the spec assumes). Turns are ordered by a monotonic
`revision`; a turn reserves the current revision by CAS (`reserve`), and a second reservation of the
same revision fails (`StaleRevision`) — the loser retries against the advanced revision.

This module is deliberately storage-agnostic and in-memory: it models the CONTRACT (idempotency,
persist-before-egress ordering, committed identity, epoch isolation) so it can be unit-tested in
isolation and later backed by the durable sqlite store without changing the contract. It imports no
pipeline/economics code (plane-clean).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

TurnStatus = Literal["committed", "uncertain"]


class StaleRevision(Exception):
    """Raised when a turn reserves a revision that has already advanced — the CAS lost, so the
    caller must re-read `revision()` and retry (another turn of this session committed first)."""


def _h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


@dataclass
class RenderingRecord:
    """One committed block's identity (baseline v2 §3.2). `emitted` is the stored wire bytes served
    forever once committed; the hashes + versions pin what produced them (fold into provenance)."""

    block_id: str
    original_hash: str
    emitted: bytes
    emitted_hash: str
    request_id: str
    epoch: int
    status: TurnStatus = "committed"


@dataclass
class PrefixLedger:
    """A session's atomic rendering ledger. All public methods are serialized by `_lock` (single-
    writer actor). `revision` advances by one per committed turn; `_by_request` gives idempotent
    retry; `_by_block` gives committed-rendering identity."""

    session_id: str
    _revision: int = 0
    _reserved: int | None = None
    _by_request: dict[str, RenderingRecord] = field(default_factory=dict)
    _by_block: dict[str, RenderingRecord] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # --- revision / CAS ---------------------------------------------------------------------------

    def revision(self) -> int:
        with self._lock:
            return self._revision

    def reserve(self, revision: int) -> None:
        """Reserve `revision` for a turn about to commit. Fails if it's not the live revision or is
        already reserved (CAS) — the single-writer discipline that serializes concurrent turns."""
        with self._lock:
            if revision != self._revision or self._reserved is not None:
                raise StaleRevision(
                    f"revision {revision} is stale (live={self._revision}, "
                    f"reserved={self._reserved}) — retry against the advanced revision"
                )
            self._reserved = revision

    # --- commit / idempotent retry ----------------------------------------------------------------

    def commit_turn(
        self,
        *,
        request_id: str,
        block_id: str,
        original: bytes,
        render: Callable[[], bytes],
        epoch: int,
        expected_revision: int | None = None,
    ) -> bytes:
        """Commit one turn's block and return its emitted bytes. IDEMPOTENT: a repeated `request_id`
        returns the stored bytes and does NOT call `render` again. PERSIST-BEFORE-EGRESS: the record
        is written to the ledger before this returns (the caller sends only after). Advances the
        revision by one on a genuinely new commit.

        CAS ENFORCEMENT (cross-validation): if a turn holds a reservation (`reserve()` set `_reserved`), a
        DIFFERENT turn cannot commit through it — the committer must present `expected_revision`
        matching the live reservation, else `StaleRevision`. This makes the reservation a real CAS
        token, not a decoration: two concurrent turns can't both land. The uncontended path (nobody
        reserved) is unaffected. An idempotent retry short-circuits BEFORE the CAS check: re-serving
        an already-committed turn's bytes is always safe, reservation or not."""
        with self._lock:
            prior = self._by_request.get(request_id)
            if prior is not None:
                return prior.emitted  # idempotent retry — no re-render, no CAS (already committed)

            # CAS: a held reservation gates every new commit. The committer must present its
            # reserved revision); anyone else is stale and must retry against the advanced revision.
            if self._reserved is not None and expected_revision != self._reserved:
                raise StaleRevision(
                    f"revision {self._reserved} is reserved by another turn; this commit presented "
                    f"{expected_revision!r} — retry against the advanced revision (cross-validation CAS)")

            emitted = render()  # the ONE render for this request_id
            rec = RenderingRecord(
                block_id=block_id,
                original_hash=_h(original),
                emitted=emitted,
                emitted_hash=_h(emitted),
                request_id=request_id,
                epoch=epoch,
                status="committed",
            )
            self._by_request[request_id] = rec
            # committed-rendering identity: first commit of a block_id wins and is kept forever.
            self._by_block.setdefault(block_id, rec)
            self._revision += 1
            self._reserved = None  # release the reservation this commit consumed
            return emitted

    # --- committed identity (replaces ship_count >= 2) --------------------------------------------

    def emit_committed(self, *, block_id: str, fallback_render: Callable[[], bytes]) -> bytes:
        """If `block_id` has EVER been committed, return its stored bytes (identity is permanent);
        otherwise produce fresh bytes via `fallback_render`. This is the safe replacement for the
        indirect `ship_count >= 2` check: a block that shipped once ships identically forever."""
        with self._lock:
            rec = self._by_block.get(block_id)
            return rec.emitted if rec is not None else fallback_render()

    # --- crash recovery ---------------------------------------------------------------------------

    def mark_uncertain(self, *, request_id: str) -> None:
        """Mark a turn's delivery ambiguous (crash between persist and forward). The stored bytes
        are intact (persist-before-egress), so recovery re-serves them; status records the doubt."""
        with self._lock:
            rec = self._by_request.get(request_id)
            if rec is not None:
                rec.status = "uncertain"

    def recover(self, *, request_id: str) -> bytes | None:
        """Re-serve the persisted bytes for an uncertain (or committed) turn — the bytes were stored
        before egress, so recovery is exact. None if the request was never persisted."""
        with self._lock:
            rec = self._by_request.get(request_id)
            return rec.emitted if rec is not None else None

    def status(self, *, request_id: str) -> TurnStatus | None:
        with self._lock:
            rec = self._by_request.get(request_id)
            return rec.status if rec is not None else None

    def epoch_of(self, *, request_id: str) -> int | None:
        """The epoch a turn committed under — reserved at turn start, unchanged by a later bundle
        swap (epoch isolation: an in-flight turn completes under its own epoch)."""
        with self._lock:
            rec = self._by_request.get(request_id)
            return rec.epoch if rec is not None else None
