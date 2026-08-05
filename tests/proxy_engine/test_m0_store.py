"""M0 store — schema creation, WAL mode, 14-day GC, canonicalization golden vector.

The canonicalization golden vector is the M1 seam pinned early (§3.2): the matcher and the
guard MUST hash identically. If this vector ever changes, every frozen block in flight
mismatches — so it is nailed down before either consumer exists.
"""
from __future__ import annotations

from apex_router.proxy_engine.session.identity import canonical_json, hash_obj
from apex_router.proxy_engine.session.store import Store


def test_schema_and_wal(tmp_path):
    with Store(tmp_path / "state.db") as s:
        counts = s.counts()
        assert set(counts) == {"sessions", "chain", "freeze", "prefix_hashes", "epochs", "ccr"}
        assert all(v == 0 for v in counts.values())
        mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_gc_removes_stale_sessions(tmp_path):
    with Store(tmp_path / "state.db", retention_days=14) as s:
        now = 1_000_000.0
        s.upsert_epoch("ep1", "{}", "0.0.1", "default", now=now)
        # fresh session (now) and a stale one (20 days old)
        s.create_session("fresh", "ep1", "claude", now=now)
        s.create_session("stale", "ep1", "claude", now=now - 20 * 86400)
        removed = s.gc(now=now)
        assert removed >= 1
        assert s.get_session("fresh") is not None
        assert s.get_session("stale") is None


def test_canonical_json_golden_vector():
    # LOCKED §3.2: sort_keys, compact separators, ensure_ascii=False.
    obj = {"b": 1, "a": [3, 2], "u": "café"}
    assert canonical_json(obj) == '{"a":[3,2],"b":1,"u":"café"}'
    # key order in the input must not change the hash (matcher==guard invariant)
    assert hash_obj({"a": 1, "b": 2}) == hash_obj({"b": 2, "a": 1})
    # this exact digest is the golden vector — if it ever changes, every frozen block in
    # flight mismatches. Pinned so a canonicalization regression fails loudly here.
    assert canonical_json({"role": "user", "content": "hi"}) == '{"content":"hi","role":"user"}'
    assert hash_obj({"role": "user", "content": "hi"}) == (
        "9017285104d1b249960a30732b8e92f6e2fb3acf8d8e4b2a16c116ad0c1ed211"
    )
