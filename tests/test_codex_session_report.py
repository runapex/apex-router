"""Tests for scripts/codex_session_report.py — Codex rollout cache-cost report."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import codex_session_report as csr  # noqa: E402


def _rollout(tmp_path, name, records) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _token_count(total_input, cached, cache_write, out):
    return {"type": "response_item", "payload": {
        "type": "token_count",
        "info": {"total_token_usage": {
            "input_tokens": total_input, "cached_input_tokens": cached,
            "cache_write_input_tokens": cache_write, "output_tokens": out,
            "total_tokens": total_input + out}}}}


def test_last_token_count_is_cumulative(tmp_path):
    # two token_count records; the LAST is the cumulative total
    p = _rollout(tmp_path, "rollout-x.jsonl", [
        {"type": "session_meta", "payload": {"session_id": "sid1", "cwd": "/tmp", "timestamp": "t"}},
        _token_count(100, 0, 100, 10),          # early
        {"type": "response_item", "payload": {"type": "user_message"}},
        _token_count(1000, 900, 100, 50),       # cumulative — this one wins
    ])
    s = csr.summarize_session(p)
    assert s["read_tokens"] == 900          # cached_input_tokens from last record
    assert s["write_tokens"] == 100
    # input_tokens is inclusive: fresh = input - cached - cache_write = 1000-900-100
    assert s["fresh_tokens"] == 0
    assert s["out_tokens"] == 50
    assert s["turns"] == 1
    assert s["session_id"] == "sid1"


def test_fresh_excludes_both_cached_and_cache_write(tmp_path):
    # Ground-truth invariant (verified against a real rollout): input_tokens is
    # the full inclusive count, so fresh must subtract BOTH cached and cache_write.
    # Numbers mirror the real session: input=2,140,515 cached=1,855,582 cw=262,514.
    p = _rollout(tmp_path, "rollout-real.jsonl", [
        _token_count(2_140_515, 1_855_582, 262_514, 13_137),
    ])
    s = csr.summarize_session(p)
    assert s["fresh_tokens"] == 2_140_515 - 1_855_582 - 262_514  # 22,419
    # and the mix must reconcile: read + write + fresh == input_tokens
    assert s["read_tokens"] + s["write_tokens"] + s["fresh_tokens"] == 2_140_515


def test_read_cost_uses_c1_schedule(tmp_path):
    p = _rollout(tmp_path, "rollout-y.jsonl", [
        _token_count(1_000_000, 1_000_000, 0, 0),  # 1M cached read, all else 0
    ])
    s = csr.summarize_session(p)
    # 1M read @ 0.1x of $5/1M = $0.50
    assert abs(s["read_cost_usd"] - 0.5) < 1e-9


def test_fresh_never_negative(tmp_path):
    # cached > input (shouldn't happen, but must not go negative)
    p = _rollout(tmp_path, "rollout-z.jsonl", [_token_count(100, 200, 0, 0)])
    s = csr.summarize_session(p)
    assert s["fresh_tokens"] == 0


def test_no_token_count_returns_none(tmp_path):
    p = _rollout(tmp_path, "rollout-empty.jsonl", [
        {"type": "session_meta", "payload": {"session_id": "s"}},
        {"type": "response_item", "payload": {"type": "message"}},
    ])
    assert csr.summarize_session(p) is None


def test_corrupt_line_tolerated(tmp_path):
    p = tmp_path / "rollout-c.jsonl"
    p.write_text(json.dumps(_token_count(10, 5, 0, 1)) + "\n" + '{"half')
    s = csr.summarize_session(p)
    assert s is not None and s["read_tokens"] == 5


def test_build_report_ranks_and_totals(tmp_path):
    d = tmp_path / "sessions" / "2026" / "08" / "20"
    d.mkdir(parents=True)
    _rollout(d, "rollout-small.jsonl", [_token_count(100, 50, 0, 0)])
    _rollout(d, "rollout-big.jsonl", [_token_count(1000, 900, 0, 0)])
    rep = csr.build_report(sessions_dir=tmp_path / "sessions", now_ts=0.0, days=0, top_n=15)
    # days=0/now=0 → mtime_after None → all counted
    assert rep["sessions_counted"] == 2
    assert rep["top_sessions"][0]["read_tokens"] == 900   # big ranked first
    assert rep["total_read_tokens"] == 950


def test_window_filters_by_mtime(tmp_path):
    import os
    d = tmp_path / "sessions"
    d.mkdir()
    old = _rollout(d, "rollout-old.jsonl", [_token_count(100, 50, 0, 0)])
    new = _rollout(d, "rollout-new.jsonl", [_token_count(100, 70, 0, 0)])
    now = 1_000_000.0
    os.utime(old, (now - 10 * 86400, now - 10 * 86400))  # 10d old
    os.utime(new, (now - 1 * 86400, now - 1 * 86400))    # 1d old
    rep = csr.build_report(sessions_dir=d, now_ts=now, days=7, top_n=15)
    assert rep["sessions_counted"] == 1
    assert rep["top_sessions"][0]["read_tokens"] == 70
