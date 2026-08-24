"""Tests for scripts/cache_report.py — the cache-cost report + offload ROI gate.

Covers the load-bearing invariants that were verified against live telemetry:
heartbeat exclusion, null-safety, partial-line tolerance, the pricing math, the
no-cache counterfactual, per-session ranking, span-vs-window honesty, and the
current-era offload gate.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import cache_report as cr  # noqa: E402

DAY = 86400
NOW = 1_000_000_000.0  # fixed anchor; module never reads wall clock


def _write_jsonl(tmp_path: Path, name: str, rows) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


# ---------------------------------------------------------------- pure math ----
def test_cost_usd_matches_schedule():
    # 1M read @0.1x, 1M write @1.25x, 1M fresh @1x of $5/1M; 1M out @5x
    got = cr.cost_usd(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert got == pytest.approx((0.1 * 5) + (1.25 * 5) + (1.0 * 5) + (5.0 * 5))


def test_no_cache_counterfactual_rebills_cached_as_fresh():
    # read+write+fresh all at 1x; output unchanged
    got = cr.no_cache_cost_usd(1_000_000, 1_000_000, 1_000_000, 0)
    assert got == pytest.approx(3 * 1.0 * 5)  # $15


def test_no_cache_is_more_expensive_when_reads_dominate():
    read, write, fresh, out = 900_000_000, 13_000_000, 6_000_000, 1_800_000
    assert cr.no_cache_cost_usd(read, write, fresh, out) > cr.cost_usd(read, write, fresh, out)


def test_num_is_null_safe():
    assert cr._num(None) == 0.0
    assert cr._num("nope") == 0.0
    assert cr._num(True) == 0.0  # bool must not count as 1
    assert cr._num(42) == 42


# ------------------------------------------------------ heartbeat exclusion ----
def test_heartbeats_excluded_from_cache_totals():
    recs = [
        {"ev": "hb", "cache_read_tokens": 999, "ts": NOW},          # must be ignored
        {"session_id": "s1", "cache_read_tokens": 100, "ts": NOW},  # real
    ]
    out = cr.summarize_cache(recs, now_ts=NOW, days=7)
    assert out["read_tokens"] == 100
    assert out["requests"] == 1


# --------------------------------------------------------- partial-line I/O ----
def test_iter_records_tolerates_corrupt_trailing_line(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"a": 1}) + "\n" + '{"b": 2, "half')  # truncated final line
    recs = list(cr.iter_records(p))
    assert recs == [{"a": 1}]  # good line kept, bad line skipped, no crash


def test_iter_records_missing_file_is_empty(tmp_path):
    assert list(cr.iter_records(tmp_path / "nope.jsonl")) == []


# --------------------------------------------------------- window filtering ----
def test_out_of_window_rows_dropped():
    recs = [
        {"session_id": "old", "cache_read_tokens": 500, "ts": NOW - 10 * DAY},
        {"session_id": "new", "cache_read_tokens": 200, "ts": NOW - 1 * DAY},
    ]
    out = cr.summarize_cache(recs, now_ts=NOW, days=7)
    assert out["read_tokens"] == 200


# ------------------------------------------------- span-vs-window honesty ----
def test_span_present_reports_actual_data_span():
    recs = [
        {"session_id": "a", "cache_read_tokens": 1, "ts": NOW - 2 * DAY},
        {"session_id": "b", "cache_read_tokens": 1, "ts": NOW},
    ]
    out = cr.summarize_cache(recs, now_ts=NOW, days=7)
    assert out["span_days_present"] == 2.0  # not 7 — never annualize a short window


# --------------------------------------------------- per-session ranking ----
def test_top_sessions_ranked_by_read_desc():
    recs = [
        {"session_id": "small", "cache_read_tokens": 100, "cache_write_tokens": 10, "ts": NOW},
        {"session_id": "big", "cache_read_tokens": 900, "cache_write_tokens": 10, "ts": NOW},
    ]
    out = cr.summarize_cache(recs, now_ts=NOW, days=7, top_n=10)
    assert [s["session_id"] for s in out["top_sessions"]] == ["big", "small"]
    assert out["top_sessions"][0]["read_usd_per_req"] > 0


def test_hit_rate_and_rw_ratio():
    recs = [{"session_id": "s", "cache_read_tokens": 90, "cache_write_tokens": 10,
             "tokens_in": 0, "ts": NOW}]
    out = cr.summarize_cache(recs, now_ts=NOW, days=7)
    assert out["input_hit_rate"] == 0.9  # 90 / (90+10+0)
    assert out["read_write_ratio"] == 9.0


# ------------------------------------------------- offload ROI era gate ----
def test_offload_excludes_pre_cutover_ornith_rows():
    recs = [
        # ornith era — before cutover — must be excluded entirely
        {"lane": "review", "model": "ornith-35b", "escalated": True,
         "completion_tokens": 5000, "ts": cr.OFFLOAD_ERA_CUTOVER_TS - DAY},
        # current era — counted
        {"lane": "review", "model": "ornith-35b", "escalated": True,
         "completion_tokens": 100, "frontier_completion_tokens_saved": 0,
         "ts": cr.OFFLOAD_ERA_CUTOVER_TS + DAY},
    ]
    out = cr.summarize_offload_roi(recs, now_ts=cr.OFFLOAD_ERA_CUTOVER_TS + 2 * DAY, days=30)
    assert out["pre_cutover_rows_excluded"] == 1
    assert out["by_lane"]["review"]["n"] == 1
    assert out["by_lane"]["review"]["escalated_completion_tokens"] == 100


def test_offload_positive_only_when_net_positive():
    recs = [
        {"lane": "codegen", "gated": True, "ok": True,
         "frontier_completion_tokens_saved": 500, "escalated": False,
         "ts": cr.OFFLOAD_ERA_CUTOVER_TS + DAY},
        {"lane": "review", "escalated": True, "completion_tokens": 900,
         "frontier_completion_tokens_saved": 0, "ts": cr.OFFLOAD_ERA_CUTOVER_TS + DAY},
    ]
    out = cr.summarize_offload_roi(recs, now_ts=cr.OFFLOAD_ERA_CUTOVER_TS + 2 * DAY, days=30)
    assert out["by_lane"]["codegen"]["offload_positive"] is True   # net +500
    assert out["by_lane"]["review"]["offload_positive"] is False   # net -900


def test_offload_null_tokens_flagged_not_crashed():
    recs = [{"lane": "adhoc", "prompt_tokens": None, "completion_tokens": None,
             "escalated": True, "ts": cr.OFFLOAD_ERA_CUTOVER_TS + DAY}]
    out = cr.summarize_offload_roi(recs, now_ts=cr.OFFLOAD_ERA_CUTOVER_TS + 2 * DAY, days=30)
    assert out["by_lane"]["adhoc"]["null_token_rows"] == 1
    assert out["by_lane"]["adhoc"]["escalated_completion_tokens"] == 0  # null → 0


# ------------------------------------------------------------ end to end ----
def test_build_report_and_check_exit(tmp_path):
    tel = _write_jsonl(tmp_path, "tel.jsonl", [
        {"ev": "hb", "ts": NOW},
        {"session_id": "s", "cache_read_tokens": 1000, "tokens_out": 10, "ts": NOW},
    ])
    off = _write_jsonl(tmp_path, "off.jsonl", [
        {"lane": "review", "escalated": True, "completion_tokens": 5,
         "ts": cr.OFFLOAD_ERA_CUTOVER_TS + DAY},
    ])
    rep = cr.build_report(telemetry=tel, offload=off, now_ts=NOW, days=7)
    assert rep["cache"]["read_tokens"] == 1000
    assert rep["cache"]["requests"] == 1
    assert "review" in rep["offload_roi"]["by_lane"]

    # --check must FAIL (exit 2): only ~0 days of span for a 7d claim
    rc = cr.main(["--telemetry", str(tel), "--offload", str(off),
                  "--now", str(NOW), "--days", "7", "--check"])
    assert rc == 2


def test_offload_saved_derived_from_flags_when_field_absent():
    # Worker rows carry NO frontier_completion_tokens_saved field — the gate must derive
    # savings from gated+ok+not-escalated (matching offload_telemetry.aggregate_offload),
    # or an earned gated pass can never be credited.
    recs = [{"lane": "codegen", "gated": True, "ok": True, "escalated": False,
             "completion_tokens": 42, "ts": cr.OFFLOAD_ERA_CUTOVER_TS + DAY}]
    out = cr.summarize_offload_roi(recs, now_ts=cr.OFFLOAD_ERA_CUTOVER_TS + 2 * DAY, days=30)
    assert out["by_lane"]["codegen"]["frontier_tokens_saved"] == 42
    assert out["by_lane"]["codegen"]["offload_positive"] is True
