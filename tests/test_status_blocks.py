"""Tests for apex_router.status_blocks — pressure, errors and measure blocks (moved from test_snapshot.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apex_router import status_blocks

NOW = 1_790_000_000.0


def _row(ts, cause=None, is_error=False, model="claude-opus-5-5", **kw):
    r = {"ts": ts, "client": "claude-code", "session_id": "s1", "agent_id": None,
         "model_requested": model, "is_error": is_error, "error_cause": cause,
         "error_detail": None, "ttft_ms": 500.0}
    r.update(kw)
    return r


def _telemetry(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _ok(n):
    return [_row(NOW - 60 + i * 0.01) for i in range(n)]


def test_errors15m_by_cause(tmp_path):
    tel = _telemetry(tmp_path / "t.jsonl", _ok(20) + [
        _row(NOW - 30, cause="ReadError", is_error=True),
        _row(NOW - 40, cause="ReadError", is_error=True),
        _row(NOW - 50, cause=None, is_error=True),
        _row(NOW - 20 * 60, cause="SSLError", is_error=True),     # outside 15 min
    ])
    e = status_blocks.errors_block(tel, NOW)
    assert e == {"n": 3, "by_cause": {"ReadError": 2, "unlabeled": 1}, "window_min": 15}


def test_pressure_block_shape(tmp_path):
    tel = _telemetry(tmp_path / "t.jsonl", _ok(30))
    p = status_blocks.pressure_block(tel, NOW)
    assert p["level"] == "GREEN" and p["insufficient_sample"] is False
    assert p["n"] == 30 and p["window_min"] == 15
    assert p["families"] == {"opus": "GREEN"}
    assert set(p["causes"]) >= {"rate_limited", "transport_errors", "retried"}


def test_measure_newest_event_ms_ts(tmp_path):
    d = tmp_path / "observe"
    d.mkdir()
    (d / "2026-10-05.jsonl").write_text(json.dumps(
        {"ev": "measure", "ts": (NOW - 9000) * 1000, "limit_kind": "seven_day",
         "limit_pct": 50, "cost_usd": 1.0}) + "\n")
    (d / "2026-10-06.jsonl").write_text("\n".join([
        json.dumps({"ev": "measure", "ts": (NOW - 900) * 1000, "limit_kind": "seven_day",
                    "limit_pct": 71, "resets_at": "2026-10-08T00:00:00Z", "cost_usd": 2.5,
                    "ctx_pct": 16}),
        json.dumps({"ev": "spawn", "ts": (NOW - 10) * 1000}),
        "garbage",
    ]) + "\n")
    m = status_blocks.measure_block(d, NOW)
    assert m["limit_kind"] == "seven_day" and m["limit_pct"] == 71
    assert m["cost_usd"] == 2.5 and m["age_s"] == pytest.approx(900, abs=1)


def test_measure_camelcase_and_missing(tmp_path):
    d = tmp_path / "observe"
    d.mkdir()
    assert status_blocks.measure_block(d, NOW)["missing"] is True
    (d / "a.jsonl").write_text(json.dumps(
        {"ev": "measure", "ts": NOW - 60, "limitKind": "five_hour", "limitPercent": 3,
         "costUsd": 0.004}) + "\n")
    m = status_blocks.measure_block(d, NOW)
    assert (m["limit_kind"], m["limit_pct"], m["age_s"]) == ("five_hour", 3, 60.0)


def test_epoch_s_reads_seconds_and_ms_and_rejects_non_finite():
    assert status_blocks._epoch_s(NOW) == NOW and status_blocks._epoch_s(NOW * 1000) == NOW
    assert status_blocks._epoch_s(float("inf")) is None and status_blocks._epoch_s("x") is None
