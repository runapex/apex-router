"""Composition diagnostic — the compiler's first input (§2.1).

Measures *this deployment's own* traffic: for each content class × size stratum, how many
addressable bytes actually appear on the frontier. This is the round-2 denominator (the fix for
the applicability-vs-efficacy error) and the round-3 n=1 answer — the compiler sees the real
traffic mix, not a design-time corpus fitted to one user.

"Addressable" = frontier bytes a transform *could* touch. Under prefix-freeze (§4 T3) the policy
can only ever act on the NEWEST block of each turn; bytes already behind the frontier are frozen
and off-limits. So the unit here is the per-turn **frontier block** — the suffix a session grew
since its previous turn — not the whole (growing) request content. Counting whole-request bytes
would inflate the denominator with frozen history the policy can never compress, re-introducing
the applicability illusion this diagnostic exists to kill, and would over-weight deep sessions'
classes in the mix.

`session_frontiers` is the canonical freeze decomposition; the policy compiler reuses it to
build its freeze-aware replay pipeline, so the diagnostic and the enforcement model agree on
exactly which bytes are addressable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace

from apex_router.proxy_engine.policy import size_stratum_bytes  # canonical byte binning — same fn the runtime routes on
from apex_router.proxy_engine.tuner.replay import Request, request_context_bytes
from apex_router.proxy_engine.tuner.stratify import Stratum
from apex_router.proxy_engine.tuner.tokens import classify, estimate_tokens


@dataclass(frozen=True)
class Frontier:
    """One turn's addressable slice under prefix-freeze, with the context it sits on.

    `block` is the newest bytes the policy may act on. `diverged` is True when the turn did NOT
    extend the previous content (client edit / compaction) — the frozen prefix is invalid and the
    runtime guard would pass client bytes; the replay must NOT append `block` to a stale prefix.
    `prefix_tokens` is the cached-context token length this block is appended onto — the real
    retrieval-cost driver (a small block on a huge context is expensive to retrieve, §6), so
    per-block admission prices against it rather than a synthetic block·R context.
    """

    req: Request
    block: bytes
    diverged: bool
    prefix_tokens: int
    # remaining requests in this block's session AFTER its own turn — the REAL amortization horizon R
    # (how many later requests re-read this block). Per-entry-position, NOT a session-aggregate: a
    # block entering at turn 95 of a 100-request session has R=4, not the band-aggregate 13. Pricing
    # `saving(R)`/`retrieval_cost(R)` at this per-block R avoids the phantom-horizon mispricing (the
    # terminal/xl failure family in the time dimension — internal review 2026-07-15).
    remaining_requests: int = 0


def session_frontiers(corpus: list[Request]) -> list[Frontier]:
    """Decompose each request into its frontier block under prefix-freeze.

    A `Request.content` is the FULL message-prefix the client sent that turn (growing across a
    session). Within a session (grouped by `session_id`, ordered by `ts` then length), the
    frontier of a turn is the byte-suffix it added over the previous turn's content:

        frontier(t) = content(t)[len(content(t-1)):]   when content(t) grows content(t-1)

    Two honest fallbacks, both flagged `diverged=True` so the replay pipeline resets rather than
    inventing bytes (cross-validation):
      - identical content (a replayed/duplicate turn) → empty frontier (nothing new to compress);
      - a turn whose content does NOT extend the previous one (compaction / client edit) → the
        whole content is the frontier and the frozen prefix is discarded. Mirrors the 3-way
        guard's "diverged → pass client bytes" rule.

    Δ9 message-structural extraction: when a `Request` carries `message_boundaries`, the frontier is
    computed over WHOLE MESSAGES — the longest common leading run of whole messages is the valid
    frozen prefix, and everything after is the frontier. This never slices mid-message on an edited
    or reordered history (byte subtraction can), and on the append-only common case produces exactly
    the same bytes (the measured corpus: 2934/2934 clean appends). Falls back to byte subtraction
    when boundaries are absent.

    Returns `Frontier`s in per-session turn order. Deterministic; independent of object identity
    (keyed on session_id + ts + content value, not id() — cross-validation).
    """
    by_session: dict[str, list[Request]] = {}
    for req in corpus:
        by_session.setdefault(req.session_id, []).append(req)

    out: list[Frontier] = []
    for _sid, reqs in by_session.items():
        ordered = sorted(reqs, key=lambda r: (r.ts, len(r.content)))
        session_frontiers_local: list[Frontier] = []  # this session's blocks, to back-fill R
        prev_msgs: list[bytes] = []
        prev = b""
        prev_tokens = 0
        for r in ordered:
            if r.frontier_block is not None:
                block = r.frontier_block
                diverged = bool(r.diverged_hint)
                prefix_tokens = (
                    int(r.prefix_tokens_hint)
                    if r.prefix_tokens_hint is not None
                    else (prev_tokens if not diverged else 0)
                )
                session_frontiers_local.append(
                    Frontier(req=r, block=block, diverged=diverged, prefix_tokens=prefix_tokens)
                )
                prev = r.content
                prev_tokens = r.tokens
                prev_msgs = []
                continue
            if r.message_boundaries is not None:
                block, diverged = _message_frontier(r, prev_msgs)
                prev_msgs = _split_messages(r.content, r.message_boundaries)
            else:
                block, diverged = _byte_frontier(r.content, prev)
                prev_msgs = []
            prefix_tokens = prev_tokens if not diverged else 0
            session_frontiers_local.append(
                Frontier(req=r, block=block, diverged=diverged, prefix_tokens=prefix_tokens)
            )
            prev = r.content
            prev_tokens = r.tokens
        # back-fill the per-entry-position R: a block at index i of an n-request session is re-read by
        # the (n-1-i) requests that follow it. This is the block's real amortization horizon.
        n = len(session_frontiers_local)
        for i, fr in enumerate(session_frontiers_local):
            out.append(replace(fr, remaining_requests=(n - 1 - i)))
    return out


def _byte_frontier(content: bytes, prev: bytes) -> tuple[bytes, bool]:
    """The v1 byte-subtraction frontier: (block, diverged). Duplicate → empty; clean byte-extend →
    the added suffix; otherwise → the whole content (diverged)."""
    if content == prev:
        return b"", False
    if content.startswith(prev) and len(content) > len(prev):
        return content[len(prev) :], False
    return content, True


def _split_messages(content: bytes, bounds: tuple[int, ...]) -> list[bytes]:
    """Split `content` into its wire messages at the cumulative byte boundaries."""
    msgs, lo = [], 0
    for hi in bounds:
        msgs.append(content[lo:hi])
        lo = hi
    return msgs


def _message_frontier(r: Request, prev_msgs: list[bytes]) -> tuple[bytes, bool]:
    """Δ9 frontier over WHOLE messages. The longest common LEADING run of identical messages is the
    valid frozen prefix; the concatenation of the remaining (new) messages is the frontier.
      - all messages shared and none new (duplicate turn) → empty, not diverged;
      - a clean append (every prev message is a leading message of this turn) → the appended tail,
        not diverged;
      - the leading run diverges before all prev messages matched (an edit/insert/reorder within
        history) → the frozen prefix past the common run is invalid → the frontier is everything
        after the common run, flagged diverged so the replay resets.
    """
    cur_msgs = _split_messages(r.content, r.message_boundaries)
    k = 0
    while k < len(prev_msgs) and k < len(cur_msgs) and prev_msgs[k] == cur_msgs[k]:
        k += 1
    if k == len(prev_msgs):
        # every previous message is a leading message of this turn → a clean append (or duplicate):
        # the frozen prefix is still valid, so the frontier is only the appended tail.
        return b"".join(cur_msgs[k:]), False
    # DIVERGED (cross-validation): the common run ended before consuming all prev messages → history changed
    # mid-stream, so the frozen prefix past the common run is INVALID. The freeze pipeline RESETS on
    # a diverged turn, so the frontier must be the WHOLE current content (not just the changed
    # suffix) — exactly what `_byte_frontier` returns on divergence. Returning the suffix would drop
    # the shared leading messages from the reset prefix and undercount cost (the F2 bug).
    return r.content, True


@dataclass
class ClassStratumCell:
    """One (class × stratum) cell of the composition."""

    n: int = 0
    bytes_total: int = 0
    tokens_total: int = 0


@dataclass
class Composition:
    """The deployment's traffic decomposition. `cells[(class, stratum)]` and marginal views."""

    cells: dict[tuple[str, Stratum], ClassStratumCell] = field(default_factory=dict)
    total_bytes: int = 0
    total_frontier_blocks: int = 0

    def addressable_bytes(self, content_class: str) -> int:
        """Total frontier bytes of a class across all strata — the admission denominator."""
        return sum(c.bytes_total for (cls, _st), c in self.cells.items() if cls == content_class)

    def class_share(self, content_class: str) -> float:
        """Fraction of total frontier bytes this class carries. 0 when there is no traffic."""
        return self.addressable_bytes(content_class) / self.total_bytes if self.total_bytes else 0.0

    def stratum_bytes(self, stratum: Stratum) -> int:
        """Total frontier bytes in a stratum across all classes (for the xl-leverage check)."""
        return sum(c.bytes_total for (_cls, st), c in self.cells.items() if st == stratum)

    def classes_present(self) -> list[str]:
        return sorted({cls for (cls, _st) in self.cells})

    def snapshot(self) -> dict:
        """Serializable evidence-pack view: per-cell counts, keyed 'class/stratum'."""
        return {
            "total_bytes": self.total_bytes,
            "total_frontier_blocks": self.total_frontier_blocks,
            "cells": {
                f"{cls}/{st}": {"n": c.n, "bytes": c.bytes_total, "tokens": c.tokens_total}
                for (cls, st), c in sorted(self.cells.items())
            },
        }


def diagnose(corpus: list[Request]) -> Composition:
    """Compute the composition of a replay corpus over its frontier blocks. Deterministic:
    same corpus → same snapshot. Empty frontiers (duplicate turns) contribute nothing."""
    comp = Composition()
    for fr in session_frontiers(corpus):
        if not fr.block:
            continue
        text = fr.block.decode("utf-8", "replace")
        cls = classify(text)
        st = size_stratum_bytes(request_context_bytes(fr.req))  # canonical byte binning (Δ2)
        cell = comp.cells.get((cls, st))
        if cell is None:
            cell = comp.cells[(cls, st)] = ClassStratumCell()
        cell.n += 1
        cell.bytes_total += len(fr.block)
        # token weight of the frontier, class-conditional (not the whole growing request)
        cell.tokens_total += estimate_tokens(text)
        comp.total_bytes += len(fr.block)
        comp.total_frontier_blocks += 1
    return comp


def composition_hash(comp: Composition) -> str:
    """Stable hash of the composition snapshot — folds into `PolicyVersion.corpus_hash` so a
    policy is tied to the traffic mix that justified it (§2.2, evidence pack)."""
    import json

    blob = json.dumps(comp.snapshot(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]
