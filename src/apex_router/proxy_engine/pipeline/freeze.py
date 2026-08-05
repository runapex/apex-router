"""Prefix guard + freeze semantics — §5.2 three-way diff `[LOCKED]`.

The guard is the last line before emit. It answers one question: are the bytes apex is about
to ship a safe continuation of what it shipped before, so the upstream prompt cache keeps its
prefix? Getting this wrong busts the cache (the single most expensive failure — cache is ~20×
compression's value) or, worse, resurrects content the user deleted (a fidelity bug).

HASHING HERE IS OVER RAW EMITTED BYTES, NEVER canonical_json (cross-validation). Anthropic
caches on the literal byte prefix; the guard must reason in exactly those bytes. `identity.
canonical_json` is for §4 logical-message identity ONLY — see its docstring. This module hashes
the actual output stream via incremental sha256 with a checkpoint at t_len ("hash-at-length"),
because prefixes GROW every turn (round 2 §2: "hash==last" is wrong; "last is a byte-prefix of
current" is right).

Three-way diff (§5.2), reusing the §4 matcher's verdict for the ORIGINAL side (never
re-derived — a second hash function is the silent-catastrophe bug class §3.2 warns of):

  client stable? (matcher event == 'extend')
    yes -> transformed side matches stored prefix hash-at-length?
             yes -> OK, silent
             no  -> APEX-CAUSED divergence: serve stored freeze bytes for the diverged
                    region; guard_action=fallback; alarm. NEVER ship the recompute.
    no  -> CLIENT mutated history (edit/compaction/clear — already classified by §4):
           invalidate affected freeze/prefix rows; pass client bytes through;
           guard_action=invalidate. NEVER serve stored bytes (would resurrect deleted content).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from apex_router.proxy_engine.session.identity import sha256_hex

GuardAction = Literal["none", "fallback", "invalidate"]


def hash_at_length(data: bytes, length: int) -> str:
    """sha256 of exactly the first `length` bytes of `data`, via a memoryview so no copy is
    materialized (§5.2: "incremental sha256 … no materialized copy"). `length` is the byte
    span the stored hash covers; `data` is the current (longer) transformed prefix."""
    if length < 0 or length > len(data):
        raise ValueError(f"length {length} out of range for {len(data)} bytes")
    return hashlib.sha256(memoryview(data)[:length]).hexdigest()


def full_hash(data: bytes) -> str:
    """sha256 hex of the full bytes. Delegates to identity.sha256_hex so there is ONE sha256
    implementation in the codebase (review finding — a second hash function is the
    silent-catastrophe class identity.py warns against). Kept as a named alias because callers
    read as 'hash the whole emitted prefix'."""
    return sha256_hex(data)


@dataclass(frozen=True)
class GuardResult:
    action: GuardAction              # none | fallback | invalidate
    ok: bool                         # True when the emit is cache-safe as-is
    emit: bytes | None               # bytes to ship, or None on fallback (caller MUST
    #                                  reconstruct from freeze — the recompute is NEVER shipped)
    alarm: bool = False              # apex-caused divergence — a real problem, telemetry+alert
    reason: str = ""                 # human-readable, for telemetry/debug


def guard(
    *,
    client_stable: bool,
    transformed_prefix: bytes,
    stored_t_hash: str | None,
    stored_t_len: int | None,
    turn: int,
) -> GuardResult:
    """Run the three-way guard for one turn's emit.

    `client_stable` is the §4 verdict (event == 'extend'). `transformed_prefix` is the FULL
    bytes apex is about to emit as the cacheable prefix this turn. `stored_t_hash/_len` are
    last turn's checkpoint. `turn` is THIS emit's turn index (0-based, the value the §4 matcher
    returns): the guard DERIVES from it whether a checkpoint should exist, so the safe behavior
    is not an opt-in flag a caller can forget (review finding — the old `expect_checkpoint=False`
    default silently reopened the lost-state bust). On a stable client at turn > 0, a missing
    checkpoint is LOST STATE → fail closed; only turn 0 (or a just-invalidated session, which
    the caller signals by passing turn 0) is a legitimate silent baseline.

    On `action='fallback'`, `emit` is None: the recomputed bytes are unsafe and must never be
    shipped. The pipeline substitutes stored freeze renderings (§5.3).
    """
    # CLIENT mutated history: pass their bytes through, invalidate stored state. Never serve
    # stored bytes — the user may have deleted content and we must not resurrect it.
    if not client_stable:
        return GuardResult(
            action="invalidate", ok=True, emit=transformed_prefix,
            reason="client mutated history (edit/compaction/clear)",
        )

    # CLIENT stable, no prior checkpoint.
    if stored_t_hash is None or stored_t_len is None:
        if turn > 0:
            # a stable EXTEND of a prior turn whose checkpoint is missing = lost state. Fail
            # CLOSED: treat as apex-caused divergence so the caller reconstructs, never a
            # silent pass that could ship a changed prefix.
            return GuardResult(
                action="fallback", ok=False, emit=None, alarm=True,
                reason=f"stable extend at turn {turn} but prior checkpoint missing "
                       f"(lost state) — fail closed",
            )
        # genuine turn 0 / first turn after invalidate: this emit establishes the baseline.
        return GuardResult(action="none", ok=True, emit=transformed_prefix,
                           reason="baseline (no prior checkpoint)")

    # CLIENT stable WITH a checkpoint. The stored prefix must be a byte-prefix of what we are
    # about to emit (prefixes grow). A SHORTER emit shrinks the cached prefix → bust.
    if stored_t_len > len(transformed_prefix):
        return GuardResult(
            action="fallback", ok=False, emit=None, alarm=True,
            reason=f"emitted prefix ({len(transformed_prefix)}B) shorter than stored "
                   f"checkpoint ({stored_t_len}B)",
        )

    if hash_at_length(transformed_prefix, stored_t_len) == stored_t_hash:
        return GuardResult(action="none", ok=True, emit=transformed_prefix,
                           reason="stable: transformed prefix extends cleanly")

    # APEX-CAUSED divergence: same client history, but our transformed output changed within
    # the already-shipped span. Serving the recompute would bust the cache — emit=None; the
    # caller substitutes stored freeze renderings for the diverged region (§5.3).
    return GuardResult(
        action="fallback", ok=False, emit=None, alarm=True,
        reason="apex-caused divergence: transformed prefix changed within shipped span",
    )
