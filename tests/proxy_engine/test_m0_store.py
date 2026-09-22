"""M0 store — schema creation, WAL mode, 14-day GC, canonicalization golden vector.

The canonicalization golden vector is the M1 seam pinned early (§3.2): the matcher and the
guard MUST hash identically. If this vector ever changes, every frozen block in flight
mismatches — so it is nailed down before either consumer exists.
"""
from __future__ import annotations

import json
import sqlite3

from apex_router.proxy_engine.session.identity import canonical_json, hash_obj
from apex_router.proxy_engine.session.store import Store
from apex_router.proxy_engine.session.wire import identify_into_store


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


def _legacy_pre_matcher_db(path):
    """Recreate a `sessions` table as it existed BEFORE the §4 matcher columns landed — the exact
    drift that silently disabled the matcher in production (`no such column: sys_prompt_hash` →
    fail-open → 100% matcher_event='unwired')."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, created_at REAL, last_seen_at REAL,"
        " epoch_id TEXT NOT NULL, client TEXT, status TEXT DEFAULT 'active');"
    )
    conn.commit()
    conn.close()


def test_migration_heals_pre_matcher_sessions_schema(tmp_path):
    # A DB created before the matcher columns must self-heal on open (additive ALTER), not stay
    # broken forever behind CREATE TABLE IF NOT EXISTS.
    db = tmp_path / "legacy.db"
    _legacy_pre_matcher_db(db)
    before = {r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(sessions)")}
    assert "sys_prompt_hash" not in before  # confirm we reproduced the drift

    with Store(db) as s:
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(sessions)")}
        for required in (
            "sys_prompt_hash", "agent_id", "project_id",
            "client_session_id", "wire_hint", "turn",
        ):
            assert required in cols, f"migration did not add {required}"


def test_migration_unblocks_matcher_end_to_end(tmp_path):
    # The regression this fixes: on a drifted DB the matcher threw and every row was 'unwired'.
    # After migration a header-less (codex) request must get a real 'new'→'extend' identity.
    db = tmp_path / "legacy.db"
    _legacy_pre_matcher_db(db)
    with Store(db) as s:
        b1 = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]}).encode()
        r1 = identify_into_store(body=b1, client="codex", wire_hint=None,
                                 agent_id=None, store=s, epoch_id="m0")
        assert r1 is not None and r1[2] == "new"
        b2 = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"},
            {"role": "user", "content": "more"}]}).encode()
        r2 = identify_into_store(body=b2, client="codex", wire_hint=None,
                                 agent_id=None, store=s, epoch_id="m0")
        assert r2 is not None and r2[2] == "extend" and r2[0] == r1[0]


def test_migration_tolerates_case_differing_existing_column(tmp_path):
    # SQLite identifiers are case-insensitive: a legacy UPPERCASE column already satisfies the
    # requirement. Comparing case-sensitively would try to ADD a duplicate and abort startup
    # forever (xval F2). Opening must succeed and not attempt a duplicate ALTER.
    db = tmp_path / "cased.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, created_at REAL, last_seen_at REAL,"
        " epoch_id TEXT NOT NULL, client TEXT, status TEXT, SYS_PROMPT_HASH TEXT);"
    )
    conn.commit()
    conn.close()
    with Store(db) as s:  # must not raise
        cols = {r[1].lower() for r in s._conn.execute("PRAGMA table_info(sessions)")}
        assert "sys_prompt_hash" in cols
        # the other missing columns were still added
        assert "wire_hint" in cols and "turn" in cols


def test_migration_duplicate_column_race_is_swallowed(tmp_path, monkeypatch):
    # Simulate the F1 race: another opener adds the column between our PRAGMA snapshot and our
    # ALTER, so the ALTER raises "duplicate column name". That means the column now exists — the
    # goal — so it must be treated as success, not a startup abort. Any OTHER OperationalError
    # must still propagate.
    db = tmp_path / "race.db"
    _legacy_pre_matcher_db(db)

    import apex_router.proxy_engine.session.store as store_mod

    real_execute = store_mod._LockedConn.execute
    state = {"fired": False}

    def flaky_execute(self, sql, params=()):
        if sql.startswith("ALTER TABLE sessions ADD COLUMN sys_prompt_hash") and not state["fired"]:
            state["fired"] = True
            raise sqlite3.OperationalError("duplicate column name: sys_prompt_hash")
        return real_execute(self, sql, params)

    monkeypatch.setattr(store_mod._LockedConn, "execute", flaky_execute)
    with Store(db) as s:  # must not raise despite the injected duplicate error
        assert state["fired"]
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(sessions)")}
        # the remaining columns were still reconciled after the swallowed duplicate
        assert "wire_hint" in cols and "turn" in cols


def test_migration_reraises_non_duplicate_operational_error(tmp_path, monkeypatch):
    db = tmp_path / "boom.db"
    _legacy_pre_matcher_db(db)
    import apex_router.proxy_engine.session.store as store_mod
    real_execute = store_mod._LockedConn.execute

    def boom_execute(self, sql, params=()):
        if sql.startswith("ALTER TABLE sessions ADD COLUMN"):
            raise sqlite3.OperationalError("database is locked")
        return real_execute(self, sql, params)

    monkeypatch.setattr(store_mod._LockedConn, "execute", boom_execute)
    try:
        Store(db)
        raised = False
    except sqlite3.OperationalError as e:
        raised = "database is locked" in str(e)
    assert raised, "a non-duplicate OperationalError must propagate, not be swallowed"


def test_migration_concurrent_opens_all_succeed(tmp_path):
    # xval improvement #1: many threads opening the SAME drifted DB concurrently must ALL succeed
    # (BEGIN IMMEDIATE serializes their migrations; the duplicate-swallow covers any residual race).
    import threading
    db = tmp_path / "concurrent.db"
    _legacy_pre_matcher_db(db)

    errors = []
    barrier = threading.Barrier(12)

    def opener():
        try:
            barrier.wait()  # maximize overlap on the migration window
            Store(db).close()
        except Exception as e:  # noqa: BLE001 — collect, assert none
            errors.append(repr(e))

    threads = [threading.Thread(target=opener) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], f"concurrent opens raised: {errors}"
    # and the schema is healed
    cols = {r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(sessions)")}
    assert "sys_prompt_hash" in cols and "turn" in cols


def test_store_closes_connection_on_init_failure(tmp_path, monkeypatch):
    # xval improvement #1: a failure during __init__ (after connect) must not leak the connection.
    db = tmp_path / "fail.db"
    _legacy_pre_matcher_db(db)
    import apex_router.proxy_engine.session.store as store_mod
    real_execute = store_mod._LockedConn.execute

    def boom_execute(self, sql, params=()):
        if sql.startswith("ALTER TABLE sessions ADD COLUMN"):
            raise sqlite3.OperationalError("disk I/O error")  # not a duplicate → propagates
        return real_execute(self, sql, params)

    closed = {"n": 0}
    real_connect = store_mod.sqlite3.connect

    class _SpyConn(sqlite3.Connection):
        def close(self):
            closed["n"] += 1
            return super().close()

    def spy_connect(*a, **k):
        k["factory"] = _SpyConn
        return real_connect(*a, **k)

    monkeypatch.setattr(store_mod._LockedConn, "execute", boom_execute)
    monkeypatch.setattr(store_mod.sqlite3, "connect", spy_connect)
    try:
        Store(db)
        raised = False
    except sqlite3.OperationalError:
        raised = True
    assert raised
    assert closed["n"] >= 1, "the raw connection must be closed on init failure (no leak)"


def test_store_cleanup_on_interrupt_mid_migration(tmp_path, monkeypatch):
    # xval #1: a KeyboardInterrupt (BaseException, not Exception) mid-migration must still close the
    # connection AND roll back the BEGIN IMMEDIATE — else the writer lock leaks and blocks every
    # other opener. After the failed open, a fresh writer-lock acquisition must succeed.
    db = tmp_path / "interrupt.db"
    _legacy_pre_matcher_db(db)
    import apex_router.proxy_engine.session.store as store_mod
    real_execute = store_mod._LockedConn.execute

    def interrupt_execute(self, sql, params=()):
        if sql.startswith("ALTER TABLE sessions ADD COLUMN sys_prompt_hash"):
            raise KeyboardInterrupt("sigint mid-migration")
        return real_execute(self, sql, params)

    monkeypatch.setattr(store_mod._LockedConn, "execute", interrupt_execute)
    try:
        Store(db)
        interrupted = False
    except KeyboardInterrupt:
        interrupted = True
    assert interrupted
    monkeypatch.undo()
    # the writer lock must be free (no dangling BEGIN IMMEDIATE, connection closed)
    probe = sqlite3.connect(str(db), timeout=1.0)
    try:
        probe.execute("BEGIN IMMEDIATE")  # would raise "database is locked" if the lock leaked
        probe.execute("ROLLBACK")
    finally:
        probe.close()


def test_migration_is_idempotent_on_current_schema(tmp_path):
    # Opening a fresh (already-current) DB twice must not error or duplicate columns.
    db = tmp_path / "fresh.db"
    with Store(db) as s:
        first = [r[1] for r in s._conn.execute("PRAGMA table_info(sessions)")]
    with Store(db) as s:
        second = [r[1] for r in s._conn.execute("PRAGMA table_info(sessions)")]
    assert first == second
    assert len(second) == len(set(second))  # no dup columns


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
