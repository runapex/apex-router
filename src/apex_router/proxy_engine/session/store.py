"""Durable sqlite store — §3.1 DDL, WAL mode.

Durable state is NOT optional (round 2 §3): an in-memory freeze store makes every proxy
restart mid-session a cache-bust generator. The schema is the spec's §3.1 verbatim.

M0 only creates the schema and exercises open/GC/round-trip — the freeze/chain/ccr writers
land in M1/M2/M3. Keeping the full DDL here now means those milestones add rows, not tables.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id      TEXT PRIMARY KEY,        -- uuid4, apex-assigned
  created_at      REAL, last_seen_at REAL,
  epoch_id        TEXT NOT NULL,
  client          TEXT,                    -- 'claude-code'|'codex'
  status          TEXT DEFAULT 'active',   -- 'active'|'compacted'|'closed'
  -- §4 candidate filtering (amends §3.1 per P0.2 findings; logged in decision-log):
  sys_prompt_hash TEXT,                    -- system-prompt lineage; different hash => new session
  agent_id        TEXT,                    -- x-claude-code-agent-id: sub-agents don't collide
  -- Δ10 SPLIT-ONLY partition columns (cross-validation): the matcher filters candidates by symmetric
  -- equality on these; they are exposed by `candidate_sessions` (SELECT *) so a matcher passing the
  -- partition args sees them. NULL (default) means "no partition key", which matches only a request
  -- that also has None — preserving pre-Δ10 behavior until callers persist real values.
  project_id        TEXT,                  -- workspace path / git root — split-only auth
  client_session_id TEXT,                  -- x-claude-code-session-id as a partition (split-only)
  wire_hint       TEXT,                    -- x-claude-code-session-id: TIEBREAKER only, never key
  turn            INTEGER DEFAULT 0        -- last turn index observed
);
CREATE TABLE IF NOT EXISTS chain (
  session_id TEXT, pos INTEGER,
  orig_hash  TEXT NOT NULL,
  PRIMARY KEY (session_id, pos)
);
CREATE TABLE IF NOT EXISTS freeze (
  session_id   TEXT, block_hash TEXT,
  rendering    BLOB NOT NULL,
  transform    TEXT, knob_epoch TEXT,
  first_ship_turn INTEGER, ship_count INTEGER DEFAULT 1,
  rerender_count  INTEGER DEFAULT 0,
  PRIMARY KEY (session_id, block_hash)
);
CREATE TABLE IF NOT EXISTS prefix_hashes (
  session_id TEXT, turn INTEGER,
  t_hash TEXT NOT NULL,
  t_len  INTEGER NOT NULL,
  PRIMARY KEY (session_id, turn)
);
CREATE TABLE IF NOT EXISTS epochs (
  epoch_id     TEXT PRIMARY KEY,
  knob_vector  TEXT NOT NULL,
  apex_version TEXT NOT NULL,
  created_at   REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS ccr (
  session_id TEXT, block_hash TEXT,
  original BLOB NOT NULL, rendering BLOB NOT NULL,
  retrieved_count INTEGER DEFAULT 0, last_retrieved_at REAL,
  PRIMARY KEY (session_id, block_hash)
);
CREATE INDEX IF NOT EXISTS idx_sessions_lastseen ON sessions (last_seen_at);
CREATE INDEX IF NOT EXISTS idx_prefix_session ON prefix_hashes (session_id);
"""

# Tables carrying a per-session recency column used for GC.
_GC_BY_SESSION = ("freeze", "prefix_hashes", "chain", "ccr")


class _Result:
    """A MATERIALIZED statement result. The rows are fetched inside the lock and stored, so
    iterating / fetchone / fetchall after the lock releases is safe. This is the crux of the
    thread-safety fix: sqlite's execute() returns a LIVE cursor whose state belongs to the
    shared connection; iterating it after the lock releases lets another thread's execute()
    reset that cursor mid-read → IndexError / wrong rows (verified). rowcount/lastrowid are
    snapshotted at execute time too."""

    __slots__ = ("_rows", "rowcount", "lastrowid")

    def __init__(self, rows: list, rowcount: int, lastrowid) -> None:
        self._rows = rows
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _LockedConn:
    """A sqlite connection wrapper that serializes every statement under one re-entrant lock
    AND materializes results before releasing the lock.

    Two invariants make one shared connection safe across the loop thread and pool threads:
      1. every execute/executemany/executescript acquires the lock (no store method can forget);
      2. results are fetched INSIDE the lock and returned as _Result (a live cursor iterated
         after the lock releases is not thread-safe — another thread's statement resets it).
    `transaction()` holds the lock across BEGIN..COMMIT so a multi-statement transaction is
    atomic w.r.t. other threads. RLock so a method inside transaction() can still call execute()."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def execute(self, sql: str, params=()) -> _Result:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return _Result(cur.fetchall(), cur.rowcount, cur.lastrowid)

    def executemany(self, sql: str, seq) -> _Result:
        with self._lock:
            cur = self._conn.executemany(sql, seq)
            return _Result(cur.fetchall(), cur.rowcount, cur.lastrowid)

    def executescript(self, script: str) -> None:
        with self._lock:
            self._conn.executescript(script)

    @contextmanager
    def transaction(self):
        """Hold the lock across BEGIN..COMMIT/ROLLBACK so the whole transaction is atomic."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Store:
    """Thin sqlite wrapper, safe to use from the event-loop thread AND offload-pool threads.

    The proxy creates one connection; the transform pipeline reads/writes the store both on the
    loop and from offloaded work (the ThreadPoolExecutor), so the connection is opened with
    check_same_thread=False and EVERY operation is serialized by a re-entrant lock (review
    finding: a same-thread-only connection raises ProgrammingError the moment a pool thread
    touches it; naively dropping the check without a lock risks WAL corruption under concurrent
    access). The lock is held only for the duration of each (fast) statement, so it does not
    stall the loop the way a 5s busy-wait would. Use as a context manager or call .close()."""

    def __init__(self, db_path: Path, *, retention_days: int = 14) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._lock = threading.RLock()
        raw = sqlite3.connect(
            str(self.db_path), isolation_level=None, check_same_thread=False
        )
        raw.row_factory = sqlite3.Row
        self._conn = _LockedConn(raw, self._lock)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # busy_timeout so a concurrent writer/checkpoint yields "wait" not "database is
        # locked". WAL alone does not prevent lock errors.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    # ---- lifecycle ----
    def gc(self, *, now: float | None = None) -> int:
        """Delete state older than retention_days. Returns rows removed. §3.1."""
        now = time.time() if now is None else now
        cutoff = now - self.retention_days * 86400
        removed = 0
        # Single transaction so GC is all-or-nothing, not a half-deleted session on crash.
        # NULL last_seen_at is treated as stale (an un-timestamped orphan).
        with self._conn.transaction() as tx:
            cur = tx.execute(
                "SELECT session_id FROM sessions WHERE last_seen_at < ? OR last_seen_at IS NULL",
                (cutoff,),
            )
            stale = [r["session_id"] for r in cur.fetchall()]
            for sid in stale:
                for tbl in _GC_BY_SESSION:
                    removed += tx.execute(
                        f"DELETE FROM {tbl} WHERE session_id = ?", (sid,)
                    ).rowcount
                removed += tx.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (sid,)
                ).rowcount
        return removed

    def size_bytes(self) -> int:
        # Include the -wal / -shm sidecars: under WAL, freshly written rows live in <db>-wal
        # until a checkpoint, so the main file alone under-reports on-disk usage (review finding).
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path.with_name(self.db_path.name + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def counts(self) -> dict[str, int]:
        tables = ("sessions", "chain", "freeze", "prefix_hashes", "epochs", "ccr")
        out: dict[str, int] = {}
        for t in tables:
            row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
            out[t] = row["n"] if row else 0
        return out

    # ---- epochs / sessions (used from M1; present now so the schema is exercised) ----
    def upsert_epoch(
        self, epoch_id: str, knob_vector: str, apex_version: str, source: str,
        *, now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        self._conn.execute(
            "INSERT OR IGNORE INTO epochs(epoch_id,knob_vector,apex_version,created_at,source)"
            " VALUES(?,?,?,?,?)",
            (epoch_id, knob_vector, apex_version, now, source),
        )

    def create_session(
        self, session_id: str, epoch_id: str, client: str, *,
        sys_prompt_hash: str | None = None, agent_id: str | None = None,
        project_id: str | None = None, client_session_id: str | None = None,
        wire_hint: str | None = None, now: float | None = None,
    ) -> None:
        # Δ10 (cross-validation): `project_id`/`client_session_id` persist the split-only partition keys the
        # matcher filters on, so a session created with them is only a merge candidate within its
        # partition cell. Default None keeps pre-Δ10 behavior (None matches only a None request).
        now = time.time() if now is None else now
        self._conn.execute(
            "INSERT INTO sessions(session_id,created_at,last_seen_at,epoch_id,client,"
            "sys_prompt_hash,agent_id,project_id,client_session_id,wire_hint,turn) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,0)",
            (session_id, now, now, epoch_id, client, sys_prompt_hash, agent_id,
             project_id, client_session_id, wire_hint),
        )

    def touch_session(
        self, session_id: str, *, turn: int | None = None, status: str | None = None,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        sets = ["last_seen_at=?"]
        vals: list = [now]
        if turn is not None:
            sets.append("turn=?")
            vals.append(turn)
        if status is not None:
            sets.append("status=?")
            vals.append(status)
        vals.append(session_id)
        self._conn.execute(f"UPDATE sessions SET {','.join(sets)} WHERE session_id=?", vals)

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()

    def candidate_sessions(
        self, *, client: str, sys_prompt_hash: str | None, within_s: float = 6 * 3600,
        now: float | None = None,
    ) -> list[sqlite3.Row]:
        """§4: sessions with last_seen within `within_s`, same client, same system-prompt
        hash. A changed system prompt is a different cache lineage → not a candidate."""
        now = time.time() if now is None else now
        return self._conn.execute(
            "SELECT * FROM sessions WHERE client=? AND last_seen_at>=? "
            "AND (sys_prompt_hash IS ? OR sys_prompt_hash=?) AND status!='closed' "
            "ORDER BY last_seen_at DESC",
            (client, now - within_s, sys_prompt_hash, sys_prompt_hash),
        ).fetchall()

    # ---- chain (the per-session original-message hash chain) ----
    def get_chain(self, session_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT orig_hash FROM chain WHERE session_id=? ORDER BY pos", (session_id,)
        ).fetchall()
        return [r["orig_hash"] for r in rows]

    def replace_chain(self, session_id: str, hashes: list[str]) -> None:
        """Set the session's chain to `hashes` (0-based positions). One transaction."""
        with self._conn.transaction() as tx:
            tx.execute("DELETE FROM chain WHERE session_id=?", (session_id,))
            tx.executemany(
                "INSERT INTO chain(session_id,pos,orig_hash) VALUES(?,?,?)",
                [(session_id, i, h) for i, h in enumerate(hashes)],
            )

    def invalidate_from(self, session_id: str, pos: int) -> None:
        """§4 edit case: drop chain rows at/after `pos` and reset ALL derived shipped state.

        freeze, ccr, and prefix_hashes are keyed by content-hash / turn (no position column), so
        on a client edit they cannot be trimmed positionally — they are cleared WHOLESALE for the
        session (M2 xval #1 + Codex review: leaving freeze OR ccr rows is a stale-byte
        resurrection surface — a later block whose content-hash collides would serve deleted
        bytes; ccr is per-session shipped state exactly like freeze and must be cleared too).
        They re-establish safely as blocks re-ship post-edit. Correctness over a few re-renders
        (the edit is rare)."""
        with self._conn.transaction() as tx:
            tx.execute("DELETE FROM chain WHERE session_id=? AND pos>=?", (session_id, pos))
            tx.execute("DELETE FROM prefix_hashes WHERE session_id=?", (session_id,))
            tx.execute("DELETE FROM freeze WHERE session_id=?", (session_id,))
            tx.execute("DELETE FROM ccr WHERE session_id=?", (session_id,))

    # ---- freeze (shipped renderings, keyed by ORIGINAL block content hash) ----
    def freeze_get(self, session_id: str, block_hash: str) -> sqlite3.Row | None:
        """The ONLY source of already-shipped bytes (§5.3). Returns the row or None."""
        return self._conn.execute(
            "SELECT * FROM freeze WHERE session_id=? AND block_hash=?",
            (session_id, block_hash),
        ).fetchone()

    def freeze_put(
        self, session_id: str, block_hash: str, rendering: bytes, *,
        transform: str | None = None, knob_epoch: str | None = None, turn: int = 0,
    ) -> None:
        """Record a shipped rendering. If the block was already shipped, bump ship_count and
        keep the ORIGINAL rendering immutable (a behind-frontier block never re-renders, §5.1)
        — we never overwrite `rendering` on conflict."""
        self._conn.execute(
            "INSERT INTO freeze(session_id,block_hash,rendering,transform,knob_epoch,"
            "first_ship_turn,ship_count,rerender_count) VALUES(?,?,?,?,?,?,1,0) "
            "ON CONFLICT(session_id,block_hash) DO UPDATE SET ship_count=ship_count+1",
            (session_id, block_hash, rendering, transform, knob_epoch, turn),
        )

    def freeze_bump_rerender(self, session_id: str, block_hash: str) -> None:
        """Frontier re-render (§5.1): rerender_count 0→1, at most once. Enforced in SQL so a
        second attempt is a no-op the caller can detect via the guard/telemetry."""
        self._conn.execute(
            "UPDATE freeze SET rerender_count=rerender_count+1 "
            "WHERE session_id=? AND block_hash=? AND rerender_count=0",
            (session_id, block_hash),
        )

    # ---- prefix_hashes (the guard's transformed-prefix hash per turn) ----
    def prefix_put(self, session_id: str, turn: int, t_hash: str, t_len: int) -> None:
        """Checkpoint turn N's shipped-prefix hash+length. APPEND-ONLY per turn: a checkpoint
        is the immutable fingerprint of bytes already shipped, and the guard's soundness
        depends on it never changing (Codex M2 xval #3: an overwrite to a shorter/different
        prefix makes hash_at_length pass falsely = a false-silent cache bust). A retry writing
        the IDENTICAL (t_hash, t_len) is a no-op; a DIFFERENT value for an existing turn is a
        bug/race and raises rather than silently corrupting the checkpoint.

        Check-and-insert run in ONE transaction (lock held across both) so two threads can't both
        see 'no row' and race to insert (Codex review: that produced IntegrityError, not the
        promised idempotent no-op). A concurrent insert that still wins is caught and re-checked
        for idempotence."""
        with self._conn.transaction() as tx:
            existing = tx.execute(
                "SELECT t_hash, t_len FROM prefix_hashes WHERE session_id=? AND turn=?",
                (session_id, turn),
            ).fetchone()
            if existing is not None:
                if existing["t_hash"] == t_hash and existing["t_len"] == t_len:
                    return  # idempotent retry
                raise ValueError(
                    f"prefix checkpoint conflict for ({session_id}, turn {turn}): "
                    f"stored ({existing['t_hash'][:8]},{existing['t_len']}) != "
                    f"({t_hash[:8]},{t_len}) — checkpoints are immutable"
                )
            try:
                tx.execute(
                    "INSERT INTO prefix_hashes(session_id,turn,t_hash,t_len) VALUES(?,?,?,?)",
                    (session_id, turn, t_hash, t_len),
                )
            except sqlite3.IntegrityError:
                # a concurrent identical insert beat us — verify it matches, else re-raise
                row = tx.execute(
                    "SELECT t_hash, t_len FROM prefix_hashes WHERE session_id=? AND turn=?",
                    (session_id, turn),
                ).fetchone()
                if not (row and row["t_hash"] == t_hash and row["t_len"] == t_len):
                    raise

    def prefix_get(self, session_id: str, turn: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT t_hash, t_len FROM prefix_hashes WHERE session_id=? AND turn=?",
            (session_id, turn),
        ).fetchone()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
