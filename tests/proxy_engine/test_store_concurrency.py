"""Store thread-safety — the connection is shared across the event loop + offload-pool threads.

The M3 offload pool runs transforms in worker threads; the pipeline reads/writes the store from
them. A same-thread-only sqlite connection crashes there, and a naive per-statement lock still
corrupts reads (a live cursor iterated after the lock releases). These tests pin both: the store
survives heavy concurrent access with correct results.
"""
from __future__ import annotations

import concurrent.futures as cf
from collections import Counter

import pytest

from apex_router.proxy_engine.session.store import Store


def test_concurrent_replace_and_read_no_corruption(tmp_path):
    """8 threads hammering replace_chain + get_chain on a SHARED session must always read a
    consistent 10-row chain — never a torn read (the live-cursor bug returned 11 rows)."""
    s = Store(tmp_path / "s.db")
    s.upsert_epoch("e", "{}", "0", "d", now=1000)
    s.create_session("shared", "e", "cc", now=1000)
    bad_lengths: list[int] = []
    errors: list[str] = []

    def worker(_n):
        try:
            for _ in range(60):
                s.replace_chain("shared", [f"h{j}" for j in range(10)])
                g = s.get_chain("shared")
                if len(g) != 10:
                    bad_lengths.append(len(g))
        except Exception as ex:  # noqa: BLE001 - any raise is a failure to record
            errors.append(f"{type(ex).__name__}: {ex}")

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(worker, range(8)))
    assert not errors, f"store raised under concurrency: {errors[:3]}"
    assert not bad_lengths, f"torn reads: {Counter(bad_lengths)}"


def test_concurrent_unique_sessions_all_persist(tmp_path):
    """8 threads each writing 50 unique sessions → all 400 persist with correct chains."""
    s = Store(tmp_path / "s.db")
    s.upsert_epoch("e", "{}", "0", "d", now=1000)
    errors: list[str] = []

    def worker(n):
        try:
            for i in range(50):
                sid = f"s{n}_{i}"
                s.create_session(sid, "e", "cc", now=1000)
                s.replace_chain(sid, [f"h{j}" for j in range(10)])
                assert s.get_chain(sid) == [f"h{j}" for j in range(10)]
        except Exception as ex:  # noqa: BLE001
            errors.append(f"{type(ex).__name__}: {ex}")

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(worker, range(8)))
    assert not errors, f"{errors[:3]}"
    assert s.counts()["sessions"] == 400


def test_concurrent_prefix_put_idempotent(tmp_path):
    """Identical prefix_put from many threads is idempotent (no IntegrityError from the
    check-then-insert race); a genuine conflict still raises."""
    s = Store(tmp_path / "s.db")
    errors: list[str] = []

    def worker(_n):
        try:
            s.prefix_put("sess", 0, "sameHash", 100)
        except Exception as ex:  # noqa: BLE001
            errors.append(f"{type(ex).__name__}")

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(worker, range(16)))
    assert not errors, f"concurrent identical prefix_put raised: {errors[:3]}"

    with pytest.raises(ValueError, match="immutable"):
        s.prefix_put("sess", 0, "different", 50)
