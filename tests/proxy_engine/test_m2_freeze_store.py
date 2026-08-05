"""M2 store — freeze + prefix_hashes durable primitives (§3.1 / §5.1).

freeze.get is the ONLY source of already-shipped bytes (§5.3), so its immutability and
ship_count/rerender semantics are load-bearing: a behind-frontier block must never re-render,
and a frontier block may re-render at most once.
"""
from __future__ import annotations

from apex_router.proxy_engine.session.store import Store


def test_freeze_roundtrip_and_immutable_rendering(tmp_path):
    with Store(tmp_path / "s.db") as s:
        s.freeze_put("sess", "blockA", b"rendered-bytes-v1", transform="terminal", turn=0)
        row = s.freeze_get("sess", "blockA")
        assert row["rendering"] == b"rendered-bytes-v1"
        assert row["ship_count"] == 1 and row["rerender_count"] == 0
        # ship it again (behind the frontier now): ship_count bumps, rendering is IMMUTABLE
        s.freeze_put("sess", "blockA", b"DIFFERENT-bytes-v2", transform="terminal", turn=1)
        row2 = s.freeze_get("sess", "blockA")
        assert row2["rendering"] == b"rendered-bytes-v1"  # never overwritten (§5.1)
        assert row2["ship_count"] == 2


def test_freeze_rerender_at_most_once(tmp_path):
    with Store(tmp_path / "s.db") as s:
        s.freeze_put("sess", "blk", b"raw", turn=0)
        s.freeze_bump_rerender("sess", "blk")
        assert s.freeze_get("sess", "blk")["rerender_count"] == 1
        # a second bump is a no-op (SQL WHERE rerender_count=0) — the frontier rule (§5.1)
        s.freeze_bump_rerender("sess", "blk")
        assert s.freeze_get("sess", "blk")["rerender_count"] == 1


def test_freeze_get_absent_returns_none(tmp_path):
    with Store(tmp_path / "s.db") as s:
        assert s.freeze_get("sess", "nope") is None


def test_prefix_hash_roundtrip(tmp_path):
    with Store(tmp_path / "s.db") as s:
        s.prefix_put("sess", 0, "hashA", 100)
        row = s.prefix_get("sess", 0)
        assert row["t_hash"] == "hashA" and row["t_len"] == 100
        # different turns coexist
        s.prefix_put("sess", 1, "hashB", 150)
        assert s.prefix_get("sess", 1)["t_len"] == 150
        # (immutability of a given turn is covered by test_prefix_checkpoint_is_immutable)


def test_invalidate_from_clears_prefix_hashes(tmp_path):
    with Store(tmp_path / "s.db") as s:
        # a session with chain + prefix state
        s.upsert_epoch("ep0", "{}", "0.0.1", "default", now=1000)
        s.create_session("sess", "ep0", "claude-code", now=1000)
        s.replace_chain("sess", ["h0", "h1", "h2", "h3"])
        s.prefix_put("sess", 0, "hA", 10)
        s.prefix_put("sess", 1, "hB", 20)
        # client edit at pos 2 → chain trimmed to [h0,h1], prefix_hashes reset wholesale
        s.invalidate_from("sess", 2)
        assert s.get_chain("sess") == ["h0", "h1"]
        assert s.prefix_get("sess", 0) is None
        assert s.prefix_get("sess", 1) is None


def test_invalidate_clears_freeze_no_resurrection(tmp_path):
    """xval #1: invalidate must clear freeze rows, else a later block whose content-hash
    collides would serve deleted bytes (a fidelity resurrection surface)."""
    with Store(tmp_path / "s.db") as s:
        s.upsert_epoch("ep0", "{}", "0.0.1", "default", now=1000)
        s.create_session("sess", "ep0", "claude-code", now=1000)
        s.replace_chain("sess", ["h0", "h1", "h2"])
        s.freeze_put("sess", "blkDeleted", b"content-user-deleted", turn=1)
        s.invalidate_from("sess", 1)
        assert s.freeze_get("sess", "blkDeleted") is None  # gone, no resurrection


def test_prefix_checkpoint_is_immutable(tmp_path):
    """xval #3: overwriting a turn's checkpoint with a different value is a false-silent
    vector — it must raise, not silently corrupt. Identical re-write is an idempotent no-op."""
    import pytest
    with Store(tmp_path / "s.db") as s:
        s.prefix_put("sess", 0, "hashX", 100)
        s.prefix_put("sess", 0, "hashX", 100)  # identical retry → no-op, no raise
        with pytest.raises(ValueError, match="immutable"):
            s.prefix_put("sess", 0, "hashDIFFERENT", 40)  # shorter/different → reject
        assert s.prefix_get("sess", 0)["t_len"] == 100  # unchanged
