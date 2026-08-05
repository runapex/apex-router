"""Δ9 — message-structural frontier extraction (roadmap §1). `session_frontiers` currently slices
the frontier by raw byte subtraction (`content(t)[len(content(t-1)):]`). That is correct for the
common append-only case (measured corpus: 2934/2934 turn-deltas are clean message-suffix appends)
but can slice MID-MESSAGE when history is edited/reordered. Δ9: when a `Request` carries its message
boundaries, extract the frontier as the whole messages appended since the previous turn — the
longest common leading run of WHOLE messages is the valid frozen prefix; the rest is the frontier.
"""

from __future__ import annotations

from apex_router.proxy_engine.tuner.composition import session_frontiers
from apex_router.proxy_engine.tuner.replay import Request


def _req(sid, messages, ts, tokens=1000):
    """A Request whose content is the concatenation of `messages`, carrying their cumulative byte
    boundaries (Δ9). `messages` is a list of str."""
    parts = [m.encode("utf-8") for m in messages]
    content = b"".join(parts)
    bounds, acc = [], 0
    for p in parts:
        acc += len(p)
        bounds.append(acc)
    return Request(sid, content, tokens, ts=ts, model="opus", message_boundaries=tuple(bounds))


# ── equivalence on the append-only common case ──


def test_append_only_matches_byte_subtraction():
    """When every turn cleanly appends whole messages, message-structural extraction gives the SAME
    frontier bytes as byte subtraction — the corpus's measured reality."""
    a, b, c = "msgA" * 50, "msgB" * 50, "msgC" * 50
    corpus = [
        _req("s", [a], ts=1.0),
        _req("s", [a, b], ts=2.0),
        _req("s", [a, b, c], ts=3.0),
    ]
    frs = session_frontiers(corpus)
    assert frs[0].block == a.encode()
    assert frs[1].block == b.encode() and not frs[1].diverged
    assert frs[2].block == c.encode() and not frs[2].diverged


def test_duplicate_turn_still_empty_frontier():
    a, b = "msgA" * 50, "msgB" * 50
    corpus = [_req("s", [a, b], ts=1.0), _req("s", [a, b], ts=2.0)]
    frs = session_frontiers(corpus)
    assert frs[1].block == b"" and not frs[1].diverged  # nothing new


# ── the case byte subtraction gets wrong: a mid-history insert ──


def test_mid_history_insert_does_not_slice_mid_message():
    """Turn t-1 = [A, B, C]; turn t = [A, X, B, C] (X inserted after A). Byte subtraction shares the
    prefix `A`, then treats `X B C` as one opaque frontier — but the REAL new content is just X;
    B and C were already sent (just at different offsets). Message-structural extraction must NOT
    emit a frontier that starts mid-B or re-elides already-sent messages: the valid frozen prefix is
    the whole-message common run [A], and divergence is flagged (cached prefix after A invalid)."""
    A, B, C, X = "A" * 40, "B" * 40, "C" * 40, "X" * 40
    corpus = [
        _req("s", [A, B, C], ts=1.0),
        _req("s", [A, X, B, C], ts=2.0),
    ]
    frs = session_frontiers(corpus)
    f = frs[1]
    # the frontier must be a whole-message boundary slice, never a partial message
    assert f.block.decode("utf-8").count("B") % 40 == 0  # no split B
    # and it diverged (the cached prefix past A changed) — so the replay resets, not appends
    assert f.diverged is True
    # Codex F2: on divergence the freeze pipeline RESETS, so the frontier is the WHOLE content
    # (matching _byte_frontier) — NOT just the changed suffix, which would drop the shared A and
    # undercount cost. Pin that the diverged block equals the full request content.
    assert f.block == corpus[1].content


def test_reorder_is_flagged_diverged_not_a_clean_append():
    """[A, B] → [B, A]: no whole-message prefix is shared (the first message changed), so this is a
    divergence, not a clean append — byte subtraction would also diverge here (via a full-content
    frontier); message-structural agrees it diverged."""
    A, B = "alpha" * 30, "beta" * 30
    corpus = [_req("s", [A, B], ts=1.0), _req("s", [B, A], ts=2.0)]
    frs = session_frontiers(corpus)
    assert frs[1].diverged is True


# ── back-compat: no boundaries → byte subtraction ──


def test_request_without_boundaries_uses_byte_subtraction():
    """A legacy Request (no message_boundaries) must still decompose via byte subtraction — Δ9 is
    additive, not a hard requirement."""
    prev = Request("s", b"AAAABBBB", 1000, ts=1.0, model="opus")
    cur = Request("s", b"AAAABBBBCCCC", 1000, ts=2.0, model="opus")
    frs = session_frontiers([prev, cur])
    assert frs[1].block == b"CCCC" and not frs[1].diverged


def test_equivalence_on_real_corpus():
    """Δ9 benchmark (the instrument-equivalence check): on the real corpus, message-structural
    extraction must be BYTE-IDENTICAL to byte subtraction (every turn is a clean append). A
    disagreement here would mean Δ9 changed the frontier on real traffic — it must not."""
    from apex_router.proxy_engine.tuner.composition import _byte_frontier, _message_frontier, _split_messages
    from fixtures.build_replay_corpus import build_corpus

    corpus, _ = build_corpus(
        "-Users-juri-kern-dev-the reference proxy", limit_sessions=3, min_turns=3, exclude_contaminated=True
    )
    by: dict = {}
    for r in corpus:
        by.setdefault(r.session_id, []).append(r)
    for reqs in by.values():
        prev_msgs: list = []
        prev = b""
        for r in sorted(reqs, key=lambda r: (r.ts, len(r.content))):
            assert _byte_frontier(r.content, prev) == _message_frontier(r, prev_msgs), (
                "message-structural frontier diverged from byte subtraction on real traffic"
            )
            prev_msgs = _split_messages(r.content, r.message_boundaries)
            prev = r.content
