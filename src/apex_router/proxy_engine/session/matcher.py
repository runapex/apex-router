"""§4 session identity — longest-suffix chain matching `[LOCKED]`.

The proxy must DERIVE a stable session_id from a bare HTTP request so freeze/epochs/guard/CCR
can key on it. Merge two sessions → cross-session byte leakage; split one → mid-stream epoch
change → guaranteed bust. So the matcher is the most load-bearing algorithm in v1.

Primary signal is CONTENT (the message-hash chain), not the wire header: Codex has no
`x-claude-code-session-id`, so content-matching is the universal mechanism. The header is a
TIEBREAKER only (§4 line 251, confirmed by P0.2).

Four events:
  1. EXTEND      stored chain C is a prefix of incoming G  → same session, turn++
  2. CLIENT_EDIT C and G share a prefix of length j, diverge after → same session, invalidate ≥j
  3. COMPACTION  G's head is unknown but a contiguous run ≥3 of G matches a run in C → same session
  4. NEW         no match → fresh uuid4, default epoch

Conservative-to-new: on a tie, prefer NEW over a wrong merge (a false split costs one bust;
a false merge corrupts bytes — §4).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

from apex_router.proxy_engine.session.identity import hash_obj

Event = Literal["extend", "client_edit", "compaction", "new"]
# Safety thresholds. The governing rule (§4): a FALSE MERGE corrupts bytes (catastrophic); a
# false split costs one cache miss (tolerable). So every threshold errs toward NEW.
MIN_EDIT_PREFIX = 2  # an edit must share a prefix of >= 2 messages (see _score for the
#   full edit rule: shared prefix must EXCEED total divergence — xval #2).
MIN_COMPACTION_TAIL = 3  # compaction must preserve a contiguous TAIL run of >= 3 of C…
COMPACTION_TAIL_ANCHOR = 6  # …taken from within C's last N messages (a recent segment, not
#                             an ancient boilerplate triple — P0.2 preservedSegment is recent)


@dataclass(frozen=True)
class Candidate:
    session_id: str
    chain: list[str]
    wire_hint: str | None
    turn: int


@dataclass(frozen=True)
class Match:
    session_id: str
    turn: int
    event: Event
    # for client_edit: the position from which stored state must be invalidated
    edit_pos: int | None = None
    # for compaction: the new chain to rebase to (G itself)
    rebase_chain: list[str] | None = None
    is_new: bool = False


class CandidateSource(Protocol):
    def candidate_sessions(
        self, *, client: str, sys_prompt_hash: str | None, within_s: float, now: float
    ) -> list: ...
    def get_chain(self, session_id: str) -> list[str]: ...

    # rows returned by candidate_sessions must expose: session_id, wire_hint, turn, agent_id


def chain_of(messages: list) -> list[str]:
    """G = [hash(canonical(m)) for m in messages]  (§4 line 229)."""
    return [hash_obj(m) for m in messages]


def _is_prefix(short: list[str], full: list[str]) -> bool:
    return len(short) <= len(full) and full[: len(short)] == short


def _shared_prefix_len(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def _compaction_overlap(c: list[str], g: list[str]) -> int:
    """Length of C's preserved TAIL that reappears contiguously in G, else 0.

    Compaction keeps a RECENT tail of the conversation (P0.2 preservedSegment: head/anchor/
    tail UUIDs) and drops the head. So the signal is: some suffix of C — of length >=
    MIN_COMPACTION_TAIL, and STARTING within C's last COMPACTION_TAIL_ANCHOR messages — must
    appear as a contiguous run inside G. This is far stricter than "any 3-run anywhere"
    (cross-validation/#2): an ancient shared boilerplate triple no longer matches, because it is
    not part of C's recent tail. We return the longest such tail overlap as a match score.
    """
    n = len(c)
    if n < MIN_COMPACTION_TAIL or len(g) < MIN_COMPACTION_TAIL:
        return 0
    best = 0
    # candidate tail segments of C: suffixes that START in the last COMPACTION_TAIL_ANCHOR msgs
    start_lo = max(0, n - COMPACTION_TAIL_ANCHOR)
    for start in range(start_lo, n - MIN_COMPACTION_TAIL + 1):
        seg = c[start:]  # a recent tail segment of C
        if _contains_run(g, seg):
            best = max(best, len(seg))
    return best


def _contains_run(hay: list[str], needle: list[str]) -> bool:
    """True if `needle` appears as a contiguous sublist of `hay`."""
    if not needle or len(needle) > len(hay):
        return False
    first = needle[0]
    for i in range(len(hay) - len(needle) + 1):
        if hay[i] == first and hay[i : i + len(needle)] == needle:
            return True
    return False


@dataclass(frozen=True)
class _Scored:
    """A candidate's best match to G, with a comparable strength for cross-phase arbitration."""

    cand: Candidate
    event: Event
    strength: int  # messages of C that provably belong to G (prefix len or tail overlap)
    edit_pos: int | None = None


def _score(c: Candidate, g: list[str]) -> _Scored | None:
    """Best (event, strength) for one candidate against G, or None if it doesn't match.

    strength = how many of C's messages are evidenced in G. Extend of a deep chain scores
    high; a 3-message coincidental overlap scores low. This single scale lets us pick the
    strongest match ACROSS event types and detect genuine ties (cross-validation/#4)."""
    if not c.chain:
        return None
    j = _shared_prefix_len(c.chain, g)
    # EXTEND: C is a proper prefix of G (C fully contained as G's head, G longer or equal).
    if _is_prefix(c.chain, g) and len(c.chain) >= 1:
        # equal single-message chains are NOT an extension (would merge parallel openers,
        # xval #6) — require G to be strictly longer, i.e. real new content past C.
        if len(g) > len(c.chain):
            return _Scored(c, "extend", strength=len(c.chain))
        # len(g)==len(c.chain) and identical → exact resend; only meaningful if chain is deep
        if len(c.chain) >= MIN_EDIT_PREFIX:
            return _Scored(c, "extend", strength=len(c.chain))
        return None
    # EDIT: two versions of ONE conversation — they AGREE (shared prefix j) on MORE than they
    # DISAGREE (divergent tails on both sides). The principled discriminator (xval #2):
    #   j > (len(C) - j) + (len(G) - j)     i.e. shared prefix exceeds total divergence.
    # A real client edit resends ~full history changing a recent turn → huge j, tiny tails.
    # A different session sharing only a boilerplate head → small j, large tails → NEW.
    # Both must actually diverge (j < len(C) and j < len(G)); j >= MIN_EDIT_PREFIX.
    c_tail = len(c.chain) - j
    g_tail = len(g) - j
    if MIN_EDIT_PREFIX <= j and c_tail > 0 and g_tail > 0 and j > c_tail + g_tail:
        return _Scored(c, "client_edit", strength=j, edit_pos=j)
    # COMPACTION: a recent tail segment of C reappears contiguously in G.
    overlap = _compaction_overlap(c.chain, g)
    if overlap >= MIN_COMPACTION_TAIL:
        return _Scored(c, "compaction", strength=overlap)
    return None


def _row_get(row, col: str):
    """Column value or None, tolerant of BOTH dict rows and `sqlite3.Row` (which supports `row[col]`
    and `.keys()` but NOT `.get()`). A column absent from an older store → None."""
    try:
        keys = row.keys()
    except AttributeError:
        keys = row  # plain dict: `in` works directly
    return row[col] if col in keys else None


def _in_partition(
    row, *, agent_id: str | None, project_id: str | None, client_session_id: str | None
) -> bool:
    """Δ10 SPLIT-ONLY partition gate. A candidate may merge ONLY if it shares the request's cell on
    every component: agent_id, project_id, client_session_id. SYMMETRIC equality
    (None==None) — a main-context / no-project / no-id request matches only candidates with the same
    absent key, never a foreign one. This can only SPLIT (a mismatch drops the candidate → fragments
    evidence, one cache miss); it can NEVER merge two cells (byte corruption). A row that predates a
    partition column (→ None) participates only when the request's key is also None, so a store not
    yet emitting the column keeps its prior behavior. Content remains the merge authority — the gate
    only removes candidates; it never promotes one."""
    return (
        _row_get(row, "agent_id") == agent_id
        and _row_get(row, "project_id") == project_id
        and _row_get(row, "client_session_id") == client_session_id
    )


def identify(
    messages: list,
    *,
    client: str,
    sys_prompt_hash: str | None,
    wire_hint: str | None,
    store: CandidateSource,
    now: float,
    agent_id: str | None = None,
    project_id: str | None = None,
    client_session_id: str | None = None,
) -> Match:
    """Return the (session_id, turn, event) for an incoming request. Pure w.r.t. the store's
    read API — it does not write; the caller applies the Match (create/extend/invalidate).

    Single scored pass: every candidate is scored to its best (event, strength); the GLOBAL
    strongest wins, but only if it is UNIQUELY strongest (no other candidate ties its
    strength). Any tie → NEW (conservative-to-new: a false merge corrupts bytes, a false
    split costs one miss — §4). This removes the phase-ordering false merges (xval #4) because
    an unrelated shallow extend (strength 2) cannot outrank the true edit of a deep chain
    (strength = its long shared prefix)."""
    g = chain_of(messages)
    cands_raw = store.candidate_sessions(
        client=client, sys_prompt_hash=sys_prompt_hash, within_s=6 * 3600, now=now
    )
    candidates = [
        Candidate(r["session_id"], store.get_chain(r["session_id"]), r["wire_hint"], r["turn"])
        for r in cands_raw
        if _in_partition(
            r, agent_id=agent_id, project_id=project_id, client_session_id=client_session_id
        )
    ]

    scored = [s for s in (_score(c, g) for c in candidates) if s is not None]
    # Drop DOMINATED extends: if candidate C's chain is a proper prefix of another candidate's
    # chain, C is an ambiguous early snapshot — extending it is a guess between C and the
    # deeper session (xval #4: A=[a,b] vs B=[a,b,old], incoming [a,b,new] → neither, → NEW).
    all_chains = [c.chain for c in candidates]
    scored = [
        s for s in scored if not (s.event == "extend" and _dominated(s.cand.chain, all_chains))
    ]
    if scored:
        best = _resolve(scored, wire_hint)
        if best is not None:
            m = best
            turn = m.cand.turn + 1
            if m.event == "extend":
                return Match(m.cand.session_id, turn, "extend")
            if m.event == "client_edit":
                return Match(m.cand.session_id, turn, "client_edit", edit_pos=m.edit_pos)
            if m.event == "compaction":
                return Match(m.cand.session_id, turn, "compaction", rebase_chain=g)

    return Match(_new_session_id(), 0, "new", is_new=True)


def _resolve(scored: list[_Scored], wire_hint: str | None) -> _Scored | None:
    """Pick the UNIQUELY strongest match. Resolution order on a strength tie:
    1. deeper candidate chain wins — more evidence (an edit of a deep chain beats a shallow
       coincidental extend of the same prefix, xval #4);
    2. then the wire hint, if it singles one out (tiebreaker only, never primary);
    3. else None → NEW (conservative: a false merge corrupts bytes, a false split is cheap).
    """
    max_strength = max(s.strength for s in scored)
    top = [s for s in scored if s.strength == max_strength]
    if len(top) == 1:
        return top[0]
    # (1) prefer the deeper chain — but ONLY if it is uniquely deepest (else still ambiguous)
    max_depth = max(len(s.cand.chain) for s in top)
    deepest = [s for s in top if len(s.cand.chain) == max_depth]
    if len(deepest) == 1:
        return deepest[0]
    # (2) wire hint tiebreaker
    if wire_hint is not None:
        hinted = [s for s in deepest if s.cand.wire_hint == wire_hint]
        if len(hinted) == 1:
            return hinted[0]
    return None  # ambiguous → new session, never a guessed merge


def _dominated(chain: list[str], all_chains: list[list[str]]) -> bool:
    """True if `chain` is a PROPER prefix of some other candidate's chain (i.e. a shorter
    early snapshot of a deeper session). Such a chain is an ambiguous extend target."""
    return any(
        other is not chain and len(other) > len(chain) and other[: len(chain)] == chain
        for other in all_chains
    )


def _new_session_id() -> str:
    # Random uuid4 — NOT derived from content: two truly new sessions with an identical first
    # message must still get distinct ids (else they'd merge on turn 1).
    return str(uuid.uuid4())
