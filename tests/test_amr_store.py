"""Tests for amr.store — the append-only, per-venue performance store (§3).

Mirrors codeqa's persistence discipline (freshness.metrics_record): append-only
JSONL, flock-guarded, corrupt-line-tolerant on read. Two separate streams that
must NOT be conflated (design finding #16):
  - bench reward rows  (only the bench writes these)
  - consumer outcome log (choice + observed cost/latency; NO reward)

Evidence is partitioned by venue ('skill' vs 'proxy') and must never be pooled
(design finding #13).
"""
import json

import pytest

from apex_router import store


def test_append_and_read_roundtrip(tmp_path):
    p = tmp_path / "outcomes.jsonl"
    row = {"venue": "skill", "model": "opus", "cell_id": "c1", "pass": True}
    store.append_row(p, row)
    rows = store.read_rows(p)
    assert len(rows) == 1
    assert rows[0]["model"] == "opus"
    assert rows[0]["venue"] == "skill"


def test_append_is_additive_not_overwrite(tmp_path):
    p = tmp_path / "outcomes.jsonl"
    store.append_row(p, {"venue": "skill", "model": "opus"})
    store.append_row(p, {"venue": "skill", "model": "sonnet"})
    rows = store.read_rows(p)
    assert [r["model"] for r in rows] == ["opus", "sonnet"]


def test_read_skips_corrupt_lines(tmp_path):
    p = tmp_path / "outcomes.jsonl"
    store.append_row(p, {"venue": "proxy", "model": "opus"})
    # inject a corrupt line between two valid ones
    with open(p, "a") as f:
        f.write("this is not json\n")
    store.append_row(p, {"venue": "proxy", "model": "haiku"})
    rows = store.read_rows(p)
    assert [r["model"] for r in rows] == ["opus", "haiku"]  # corrupt line dropped


def test_read_missing_file_returns_empty(tmp_path):
    assert store.read_rows(tmp_path / "nope.jsonl") == []


def test_append_creates_parent_dirs(tmp_path):
    p = tmp_path / "nested" / "deep" / "outcomes.jsonl"
    store.append_row(p, {"venue": "skill", "model": "opus"})
    assert p.exists()
    assert store.read_rows(p)[0]["model"] == "opus"


def test_read_rows_filters_by_venue(tmp_path):
    p = tmp_path / "outcomes.jsonl"
    store.append_row(p, {"venue": "skill", "model": "opus"})
    store.append_row(p, {"venue": "proxy", "model": "sonnet"})
    store.append_row(p, {"venue": "skill", "model": "haiku"})
    skill_rows = store.read_rows(p, venue="skill")
    assert [r["model"] for r in skill_rows] == ["opus", "haiku"]
    assert all(r["venue"] == "skill" for r in skill_rows)


def test_bench_reward_and_outcome_log_are_separate_streams(tmp_path):
    # The two streams live in distinct files; a reward write never lands in the
    # outcome log and vice versa (design finding #16 — consumers never write reward).
    bench = tmp_path / "outcomes.jsonl"
    log = tmp_path / "outcome_log.jsonl"
    store.append_reward(bench, {"venue": "skill", "model": "opus", "cell_id": "c1",
                                "outcome": {"pass": True}})
    store.append_outcome(log, {"venue": "skill", "model": "opus", "chosen": True,
                               "cost_usd": 0.01, "latency": 1.2})
    bench_rows = store.read_rows(bench)
    log_rows = store.read_rows(log)
    assert len(bench_rows) == 1 and "outcome" in bench_rows[0]
    assert len(log_rows) == 1 and "cost_usd" in log_rows[0]
    # the outcome-log row carries NO reward field
    assert "outcome" not in log_rows[0] and "reward" not in log_rows[0]


def test_append_reward_rejects_row_without_outcome(tmp_path):
    # A bench reward row must carry an 'outcome'; guard against a mis-shaped write.
    with pytest.raises(ValueError):
        store.append_reward(tmp_path / "outcomes.jsonl",
                            {"venue": "skill", "model": "opus"})


def test_append_outcome_rejects_reward_bearing_row(tmp_path):
    # The consumer outcome log must never carry reward/outcome (finding #16).
    with pytest.raises(ValueError):
        store.append_outcome(tmp_path / "outcome_log.jsonl",
                             {"venue": "skill", "model": "opus", "outcome": {"pass": True}})


def test_rows_are_valid_jsonl_on_disk(tmp_path):
    # Every persisted line is independently json-parseable (one object per line).
    p = tmp_path / "outcomes.jsonl"
    store.append_row(p, {"venue": "skill", "a": 1})
    store.append_row(p, {"venue": "proxy", "b": 2})
    with open(p) as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    assert len(lines) == 2
    for ln in lines:
        json.loads(ln)  # must not raise
