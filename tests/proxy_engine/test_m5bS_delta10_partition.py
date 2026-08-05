"""Δ10 — namespace partition, SPLIT-ONLY (roadmap §1). Session matching runs ONLY inside a partition
cell keyed on (project_id, client_session_id, agent_id). A mismatch on any partition component makes
merging impossible — a partition can only SPLIT (fragment evidence), never MERGE. Content stays the
merge authority (the conservative-to-new chain matcher, unchanged).

Failure asymmetry (the test every new matcher input must pass): a wrong project inference degrades
to fragmentation (one cache miss), never byte corruption (a cross-context merge). So project /
client-session-id join the HARD partition (split-only); they are NOT given merge authority — an id
that asserts "same session" over diverging content is exactly the false-merge input class.
"""

from __future__ import annotations

from apex_router.proxy_engine.session.matcher import chain_of, identify


class _FakeStore:
    """A minimal CandidateSource: holds candidate rows + their chains, returns all as candidates
    (the matcher applies the partition filter). Rows expose the partition columns (Δ10)."""

    def __init__(self, rows):
        self._rows = rows  # list of dicts
        self._chains = {r["session_id"]: r["chain"] for r in rows}

    def candidate_sessions(self, *, client, sys_prompt_hash, within_s, now):
        return [
            {
                "session_id": r["session_id"],
                "wire_hint": r.get("wire_hint"),
                "turn": r["turn"],
                "agent_id": r.get("agent_id"),
                "project_id": r.get("project_id"),
                "client_session_id": r.get("client_session_id"),
            }
            for r in self._rows
        ]

    def get_chain(self, session_id):
        return self._chains[session_id]


def _msgs(*texts):
    return [{"role": "user", "content": t} for t in texts]


# ── split-only: a different project never merges ──


def test_different_project_never_merges():
    """Same content chain, but the candidate is in project B and the request is project A → the
    matcher must NOT extend it (partition mismatch). It returns NEW instead."""
    base = _msgs("hello", "world")
    g = chain_of(base)
    store = _FakeStore(
        [{"session_id": "s_projB", "chain": g[:1], "turn": 0, "project_id": "projB"}]
    )
    # request is a clean extension of s_projB's chain, but from projA
    m = identify(
        base,
        client="claude-code",
        sys_prompt_hash="h",
        wire_hint=None,
        store=store,
        now=1.0,
        project_id="projA",
    )
    assert m.is_new is True  # partition blocked the merge


def test_same_project_still_merges_on_content():
    """Control: identical setup but SAME project → the content matcher extends as before (partition
    permits, content decides)."""
    base = _msgs("hello", "world")
    g = chain_of(base)
    store = _FakeStore(
        [{"session_id": "s_projA", "chain": g[:1], "turn": 0, "project_id": "projA"}]
    )
    m = identify(
        base,
        client="claude-code",
        sys_prompt_hash="h",
        wire_hint=None,
        store=store,
        now=1.0,
        project_id="projA",
    )
    assert m.is_new is False and m.event == "extend" and m.session_id == "s_projA"


# ── the asymmetry pin: an id cannot FORCE a merge ──


def test_client_session_id_cannot_force_merge_over_divergent_content():
    """Same client_session_id, but the incoming content does NOT extend the candidate's chain (a
    client edit / fresh history). The id must NOT force a merge — content is the merge authority, so
    a genuine divergence still resolves to NEW (or a content event), never a blind id merge."""
    cand_chain = chain_of(_msgs("alpha", "beta", "gamma"))
    incoming = _msgs("totally", "different", "history")  # shares no prefix with the candidate
    store = _FakeStore(
        [
            {
                "session_id": "s1",
                "chain": cand_chain,
                "turn": 2,
                "client_session_id": "cs-42",
                "project_id": "projA",
            }
        ]
    )
    m = identify(
        incoming,
        client="claude-code",
        sys_prompt_hash="h",
        wire_hint=None,
        store=store,
        now=1.0,
        project_id="projA",
        client_session_id="cs-42",
    )
    # the id matches, but content diverges → the matcher must not merge on the id alone
    assert m.is_new is True


def test_client_session_id_partitions_split_only():
    """A DIFFERENT client_session_id blocks the merge even on matching content (split-only)."""
    base = _msgs("hello", "world")
    g = chain_of(base)
    store = _FakeStore(
        [
            {
                "session_id": "s_csB",
                "chain": g[:1],
                "turn": 0,
                "client_session_id": "csB",
                "project_id": "projA",
            }
        ]
    )
    m = identify(
        base,
        client="claude-code",
        sys_prompt_hash="h",
        wire_hint=None,
        store=store,
        now=1.0,
        project_id="projA",
        client_session_id="csA",
    )
    assert m.is_new is True  # different client session id → no merge


# ── back-compat: no partition keys → prior behavior ──


def test_no_partition_keys_preserves_prior_behavior():
    """When project_id / client_session_id are not supplied (None on both sides), matching behaves
    as it did before Δ10 — a clean content extension merges."""
    base = _msgs("hello", "world")
    g = chain_of(base)
    store = _FakeStore([{"session_id": "s", "chain": g[:1], "turn": 0}])
    m = identify(
        base, client="claude-code", sys_prompt_hash="h", wire_hint=None, store=store, now=1.0
    )
    assert m.is_new is False and m.event == "extend"


# ── Codex F3: the REAL sqlite store persists + exposes the partition columns ──


def test_real_store_persists_and_exposes_partition_keys(tmp_path):
    """Codex F3: passing project_id to identify() must not turn every follow-up into a NEW session —
    the real Store schema now carries project_id/client_session_id and create_session persists them,
    so candidate_sessions exposes them and a same-project extension still merges."""
    from apex_router.proxy_engine.session.store import Store

    store = Store(tmp_path / "s.db")
    base = _msgs("hello", "world", "again")
    g = chain_of(base)
    # a session created WITH a project_id, one turn in
    store.create_session("s0", "ep0", "claude-code", sys_prompt_hash="h",
                         project_id="projA", now=1.0)
    store.replace_chain("s0", g[:2])
    store.touch_session("s0", turn=1, now=1.0)
    # a follow-up turn from the SAME project that extends the chain → must EXTEND, not NEW
    m = identify(base, client="claude-code", sys_prompt_hash="h", wire_hint=None,
                 store=store, now=2.0, project_id="projA")
    assert m.is_new is False and m.event == "extend" and m.session_id == "s0"
    # and a request from a DIFFERENT project does NOT merge (split-only)
    m2 = identify(base, client="claude-code", sys_prompt_hash="h", wire_hint=None,
                  store=store, now=3.0, project_id="projB")
    assert m2.is_new is True
