"""Tests for apex_router.pressure — the pre-dispatch rate-limit pressure readout."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apex_router import pressure

NOW = 1_790_000_000.0


def _row(ts, model="claude-opus-5-5", cause=None, agent=None, ttft=500.0, headers=None, **kw):
    r = {"ts": ts, "client": "claude-code", "session_id": "s1", "agent_id": agent,
         "model_requested": model, "is_error": False, "error_cause": cause,
         "error_detail": None if headers is None else {"body": "", "headers": headers},
         "ttft_ms": ttft, "usage": {"output_tokens": 10}}
    r.update(kw)
    return r


def _write(path: Path, rows, trailing: str = "") -> Path:
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.write(trailing)
    return path


def _ok(n, start=NOW - 60, model="claude-opus-5-5", **kw):
    return [_row(start + i * 0.01, model=model, **kw) for i in range(n)]


# ---------------------------------------------------------------- family

@pytest.mark.parametrize("model,fam", [
    ("claude-opus-5-5", "opus"), ("claude-sonnet-4-6", "sonnet"),
    ("claude-haiku-4-5-20251001", "haiku"), ("claude-fable-1", "fable"),
    ("gpt-5-codex", "other"), (None, "other"), ("", "other"),
])
def test_family(model, fam):
    assert pressure.family(model) == fam


# ---------------------------------------------------------------- levels

def test_green(tmp_path):
    p = _write(tmp_path / "t.jsonl", _ok(100))
    r = pressure.compute(p, now=NOW)
    assert r["level"] == "GREEN"
    assert r["overall"]["requests"] == 100
    assert r["recommendation"] == "dispatch as planned"


def test_empty_window_is_green(tmp_path):
    p = _write(tmp_path / "t.jsonl", [])
    r = pressure.compute(p, now=NOW)
    assert r["level"] == "GREEN" and r["overall"]["requests"] == 0


def test_missing_file_is_unknown(tmp_path):
    r = pressure.compute(tmp_path / "nope.jsonl", now=NOW)
    assert r["level"] == "UNKNOWN" and r["overall"]["requests"] == 0
    assert "error" in r


def test_unreadable_file_is_unknown(tmp_path):
    d = tmp_path / "is_a_dir.jsonl"
    d.mkdir()
    r = pressure.compute(d, now=NOW)
    assert r["level"] == "UNKNOWN"


def test_amber_on_429_rate(tmp_path):
    rows = _ok(95) + [_row(NOW - 30, cause="http_429") for _ in range(5)]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["overall"]["rate_limited"] == 5
    assert r["overall"]["rate_limited_rate"] == pytest.approx(0.05)
    assert r["level"] == "AMBER"
    assert r["recommendation"].startswith("shed mechanical and exploration subagents")


def test_amber_on_transport_rate(tmp_path):
    rows = _ok(95) + [_row(NOW - 30, cause=c) for c in ("SSLError", "ReadError") * 2] \
        + [_row(NOW - 30, cause="SSLError")]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["overall"]["transport_errors"] == 5
    assert r["level"] == "AMBER"


def test_transport_below_3pct_is_green(tmp_path):
    rows = _ok(98) + [_row(NOW - 30, cause="ReadError") for _ in range(2)]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["level"] == "GREEN"


def test_client_errors_are_not_pressure(tmp_path):
    rows = _ok(80) + [_row(NOW - 30, cause=c) for c in ("http_400", "http_413", "http_404") * 7]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["level"] == "GREEN"
    assert r["overall"]["transport_errors"] == 0 and r["overall"]["rate_limited"] == 0


def test_red_on_429_rate(tmp_path):
    rows = _ok(80) + [_row(NOW - 30, cause="http_429") for _ in range(20)]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["level"] == "RED"
    assert r["recommendation"] == (
        "no new heavy fan-out; serialize; mechanical work to haiku or the local tier; "
        "wait 60 s before retrying heavy")


def test_exactly_10pct_is_amber_not_red(tmp_path):
    rows = _ok(90) + [_row(NOW - 30, cause="http_429") for _ in range(10)]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["level"] == "AMBER"


def test_red_on_recent_retry_after_even_at_low_rate(tmp_path):
    rows = _ok(200) + [_row(NOW - 45, cause="http_429", headers={"retry-after": "37"})]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["level"] == "RED"
    assert r["retry_after_s"] == 37
    assert "wait 37 s before retrying heavy" in r["recommendation"]


def test_old_retry_after_does_not_force_red(tmp_path):
    rows = _ok(200) + [_row(NOW - 600, cause="http_429", headers={"retry-after": "37"})]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["level"] == "GREEN"
    assert r["headers"]["retry-after"] == "37"  # still reported as newest seen


def test_per_family_breakdown(tmp_path):
    rows = (_ok(10, model="claude-haiku-4-5") + _ok(10, model="claude-opus-5-5")
            + [_row(NOW - 20, model="claude-opus-5-5", cause="http_429") for _ in range(10)])
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["families"]["opus"]["requests"] == 20
    assert r["families"]["opus"]["rate_limited_rate"] == pytest.approx(0.5)
    assert r["families"]["haiku"]["rate_limited"] == 0
    assert r["families"]["haiku"]["level"] == "GREEN"
    assert r["families"]["opus"]["level"] == "RED"


def test_p50_ttft_ignores_errors_and_nulls(tmp_path):
    rows = [_row(NOW - 10, ttft=t) for t in (100, 200, 300)] + [_row(NOW - 10, ttft=None)] \
        + [_row(NOW - 10, ttft=9999, cause="SSLError")]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["overall"]["p50_ttft_ms"] == pytest.approx(200)


def test_inflight_distinct_agents_last_2_minutes(tmp_path):
    rows = [_row(NOW - 30, agent="a1"), _row(NOW - 20, agent="a1"), _row(NOW - 10, agent="a2"),
            _row(NOW - 5, agent=None), _row(NOW - 300, agent="a3")]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["overall"]["inflight_agents"] == 2


def test_skips_heartbeats_and_garbage(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, _ok(10))
    with p.open("a") as f:
        f.write(json.dumps({"ev": "hb", "ts": NOW - 5}) + "\n")
        f.write("not json\n")
        f.write(json.dumps({"ts": "bad"}) + "\n")
    r = pressure.compute(p, now=NOW)
    assert r["overall"]["requests"] == 10


# ---------------------------------------------------------------- window / tail-read

def test_window_cutoff(tmp_path):
    rows = ([_row(NOW - 16 * 60) for _ in range(50)]          # just outside 15 min
            + [_row(NOW - 16 * 60, cause="http_429") for _ in range(50)]
            + [_row(NOW - 14 * 60) for _ in range(7)])         # inside
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW, window_min=15)
    assert r["overall"]["requests"] == 7 and r["level"] == "GREEN"
    r = pressure.compute(tmp_path / "t.jsonl", now=NOW, window_min=20)
    assert r["overall"]["requests"] == 107


def test_tail_read_drops_partial_trailing_line(tmp_path):
    partial = json.dumps(_row(NOW - 1, cause="http_429"))[:40]  # writer mid-append
    p = _write(tmp_path / "t.jsonl", _ok(5), trailing=partial)
    rows = list(pressure.tail_rows(p, since_ts=NOW - 900))
    assert len(rows) == 5
    assert all(r["error_cause"] is None for r in rows)


def test_tail_read_does_not_parse_whole_file(tmp_path, monkeypatch):
    # 20k old rows then 3 recent ones; a small block size must stop early.
    old = [_row(NOW - 86400 + i) for i in range(20000)]
    p = _write(tmp_path / "t.jsonl", old + _ok(3))
    parsed = []
    real = pressure._parse
    monkeypatch.setattr(pressure, "_parse", lambda line: parsed.append(1) or real(line))
    rows = list(pressure.tail_rows(p, since_ts=NOW - 900, block=8192))
    assert len(rows) == 3
    assert len(parsed) < 500, len(parsed)


def test_tail_read_spans_block_boundaries(tmp_path):
    rows = _ok(400)
    p = _write(tmp_path / "t.jsonl", rows)
    got = list(pressure.tail_rows(p, since_ts=NOW - 900, block=777))
    assert sorted(r["ts"] for r in got) == sorted(r["ts"] for r in rows)


# ---------------------------------------------------------------- headers

def test_header_parsing_newest_wins_and_ratelimit_fields(tmp_path):
    rows = [
        _row(NOW - 100, cause="http_429", headers={"retry-after": "5",
                                                   "anthropic-ratelimit-requests-remaining": "9"}),
        _row(NOW - 50, cause="http_429", headers={
            "Retry-After": "12", "anthropic-ratelimit-tokens-remaining": "0",
            "anthropic-ratelimit-tokens-reset": "2026-10-02T18:00:00Z", "x-should-retry": "true",
            "request-id": "req_x"}),
        _row(NOW - 10),
    ]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    h = r["headers"]
    assert h["retry-after"] == "12"
    assert h["anthropic-ratelimit-tokens-remaining"] == "0"
    assert h["anthropic-ratelimit-tokens-reset"] == "2026-10-02T18:00:00Z"
    assert h["anthropic-ratelimit-requests-remaining"] == "9"  # newest per key
    assert "request-id" not in h
    assert r["headers_age_s"] == pytest.approx(50)
    assert r["retry_after_s"] == 12


def test_retry_after_http_date():
    assert pressure.parse_retry_after("120") == 120
    assert pressure.parse_retry_after("1.5") == 2
    assert pressure.parse_retry_after("garbage") is None
    from email.utils import formatdate
    assert pressure.parse_retry_after(formatdate(NOW + 30, usegmt=True), now=NOW) == 30


def test_upstream_rejected_flag_counted_when_present(tmp_path):
    rows = _ok(90) + [_row(NOW - 30, cause=None, upstream_rejected=True) for _ in range(10)]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["overall"]["upstream_rejected"] == 10


# ---------------------------------------------------------------- thresholds

def test_env_threshold_overrides(tmp_path, monkeypatch):
    rows = _ok(95) + [_row(NOW - 30, cause="http_429") for _ in range(5)]
    p = _write(tmp_path / "t.jsonl", rows)
    monkeypatch.setenv("APEX_PRESSURE_AMBER", "0.10")
    assert pressure.compute(p, now=NOW)["level"] == "GREEN"
    monkeypatch.setenv("APEX_PRESSURE_AMBER", "0.01")
    monkeypatch.setenv("APEX_PRESSURE_RED", "0.04")
    assert pressure.compute(p, now=NOW)["level"] == "RED"
    monkeypatch.setenv("APEX_PRESSURE_RED", "nonsense")
    assert pressure.compute(p, now=NOW)["level"] == "AMBER"  # bad value -> default


# ---------------------------------------------------------------- state file + CLI

def test_state_file_written_atomically(tmp_path):
    p = _write(tmp_path / "t.jsonl", _ok(3))
    out = tmp_path / "sub" / "pressure.json"
    r = pressure.compute(p, now=NOW)
    pressure.write_state(r, out)
    assert json.loads(out.read_text())["level"] == "GREEN"
    assert not list(out.parent.glob("*.tmp"))


@pytest.mark.parametrize("n429,code,level", [(0, 0, "GREEN"), (5, 1, "AMBER"), (30, 2, "RED")])
def test_cli_check_exit_codes(tmp_path, n429, code, level, capsys):
    rows = _ok(100 - n429, start=NOW - 60) + [_row(NOW - 30, cause="http_429") for _ in range(n429)]
    p = _write(tmp_path / "t.jsonl", rows)
    out = tmp_path / "pressure.json"
    rc = pressure.main(["--telemetry", str(p), "--state", str(out), "--check", "--json"],
                       now=NOW)
    assert rc == code
    assert json.loads(capsys.readouterr().out)["level"] == level
    assert json.loads(out.read_text())["level"] == level


def test_cli_without_check_exits_zero_and_prints_text(tmp_path, capsys):
    rows = _ok(70) + [_row(NOW - 30, cause="http_429") for _ in range(30)]
    p = _write(tmp_path / "t.jsonl", rows)
    rc = pressure.main(["--telemetry", str(p), "--state", str(tmp_path / "s.json")], now=NOW)
    assert rc == 0
    out = capsys.readouterr().out
    assert "RED" in out and "no new heavy fan-out" in out and "opus" in out


def test_cli_dispatch_through_apex_router_cli(tmp_path, capsys, monkeypatch):
    from apex_router import cli
    p = _write(tmp_path / "t.jsonl", [])
    rc = cli.main(["pressure", "--telemetry", str(p), "--state", str(tmp_path / "s.json"),
                   "--json", "--check"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["level"] == "GREEN"


def test_state_write_failure_does_not_break_readout(tmp_path, capsys):
    p = _write(tmp_path / "t.jsonl", _ok(3))
    blocker = tmp_path / "file"
    blocker.write_text("x")
    rc = pressure.main(["--telemetry", str(p), "--state", str(blocker / "pressure.json"),
                        "--check"], now=NOW)
    assert rc == 0


# ---------------------------------------------------------------- P1: out-of-order tail stop

def test_one_old_row_appended_last_does_not_stop_scan(tmp_path):
    # A 30-minute opus stream finished just now: its row (ts = start) is the newest line in the
    # file but older than the window edge. The 50 in-window 429s before it must still be counted.
    rows = [_row(NOW - 60 + i * 0.1, cause="http_429") for i in range(50)] \
        + [_row(NOW - 30 * 60, model="claude-opus-5-5")]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["overall"]["requests"] == 50
    assert r["level"] == "RED"


def test_several_hours_old_row_appended_last_does_not_stop_scan(tmp_path):
    rows = _ok(50) + [_row(NOW - 3 * 3600)]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["overall"]["requests"] == 50


def test_tail_stops_after_k_consecutive_old_rows(tmp_path, monkeypatch):
    # 1000 rows 1h old (inside the 6h hard bound) then 5 in-window rows: the scan must stop
    # after ~K=200 consecutive old rows, not read all 1000.
    old = [_row(NOW - 3600 + i * 0.001) for i in range(1000)]
    p = _write(tmp_path / "t.jsonl", old + _ok(5))
    parsed = []
    real = pressure._parse
    monkeypatch.setattr(pressure, "_parse", lambda line: parsed.append(1) or real(line))
    rows = list(pressure.tail_rows(p, since_ts=NOW - 900, block=8192))
    assert len(rows) == 5
    assert pressure.STOP_AFTER_OLD_ROWS == 200
    assert 205 <= len(parsed) < 300, len(parsed)


def test_tail_does_not_stop_before_k_old_rows(tmp_path):
    # 150 old rows (< K) sandwiched between in-window rows: everything in-window is counted.
    rows = _ok(20, start=NOW - 120) + [_row(NOW - 3600) for _ in range(150)] + _ok(20)
    got = list(pressure.tail_rows(_write(tmp_path / "t.jsonl", rows), since_ts=NOW - 900))
    assert len(got) == 40


def test_tail_hard_bound_stops_on_row_6h_past_edge(tmp_path, monkeypatch):
    rows = _ok(20, start=NOW - 120) + [_row(NOW - 900 - 600 - 6 * 3600 - 60)] + _ok(5)
    p = _write(tmp_path / "t.jsonl", rows)
    parsed = []
    real = pressure._parse
    monkeypatch.setattr(pressure, "_parse", lambda line: parsed.append(1) or real(line))
    got = list(pressure.tail_rows(p, since_ts=NOW - 900))
    assert len(got) == 5 and len(parsed) == 6


# ---------------------------------------------------------------- P2: exit codes, floor, parsing

def test_cli_check_missing_telemetry_exits_3(tmp_path, capsys):
    rc = pressure.main(["--telemetry", str(tmp_path / "nope.jsonl"), "--no-write", "--check"],
                       now=NOW)
    assert rc == 3
    assert "UNKNOWN" in capsys.readouterr().out


def test_cli_usage_error_exits_4(tmp_path, capsys):
    assert pressure.main(["--bogus-flag"], now=NOW) == 4
    assert pressure.main(["--window", "notanumber"], now=NOW) == 4
    assert "usage" in capsys.readouterr().err


def test_cli_unexpected_exception_is_unknown_exit_3(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(pressure, "compute", boom)
    rc = pressure.main(["--no-write", "--check"], now=NOW)
    assert rc == 3
    assert capsys.readouterr().out.strip() == "pressure: UNKNOWN (RuntimeError: kaboom)"


def test_sample_floor_reports_green_with_note(tmp_path, capsys):
    # 3 of 5 requests 429'd (60%) but n<10: GREEN with an insufficient-sample note; rates shown.
    rows = _ok(2) + [_row(NOW - 300, cause="http_429") for _ in range(3)]
    p = _write(tmp_path / "t.jsonl", rows)
    r = pressure.compute(p, now=NOW)
    assert r["level"] == "GREEN"
    assert r["insufficient_sample"] is True
    assert r["overall"]["rate_limited_rate"] == pytest.approx(0.6)
    rc = pressure.main(["--telemetry", str(p), "--no-write", "--check"], now=NOW)
    assert rc == 0
    out = capsys.readouterr().out
    assert "GREEN (insufficient sample: n<10)" in out
    assert "60.0%" in out


def test_sample_floor_not_applied_at_10(tmp_path):
    rows = _ok(5) + [_row(NOW - 300, cause="http_429") for _ in range(5)]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["level"] == "RED" and r["insufficient_sample"] is False


@pytest.mark.parametrize("v", ["inf", "-inf", "nan", "1e400", "9" * 400])
def test_parse_retry_after_absurd_values(v):
    assert pressure.parse_retry_after(v) is None


def test_pool_timeout_is_local_not_transport(tmp_path, capsys):
    rows = _ok(80) + [_row(NOW - 30, cause="PoolTimeout") for _ in range(20)]
    p = _write(tmp_path / "t.jsonl", rows)
    r = pressure.compute(p, now=NOW)
    assert r["overall"]["local"] == 20
    assert r["overall"]["transport_errors"] == 0
    assert r["level"] == "GREEN"
    pressure.main(["--telemetry", str(p), "--no-write"], now=NOW)
    assert "local" in capsys.readouterr().out


# ---------------------------------------------------------------- P3: retried-but-succeeded

def test_retried_and_failed_row_counted_once(tmp_path):
    rows = _ok(97) + [_row(NOW - 30, cause="SSLError", connect_retries=2) for _ in range(3)]
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["overall"]["transport_errors"] == 3
    assert r["overall"]["retried"] == 3
    assert r["overall"]["transport_rate"] == pytest.approx(0.03)


# ---------------------------------------------------------------- P4: telemetry path

def test_default_path_honours_apex_telemetry(tmp_path, monkeypatch):
    p = _write(tmp_path / "explicit.jsonl", _ok(12))
    monkeypatch.setenv("APEX_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("APEX_TELEMETRY", str(p))
    r = pressure.compute(None, now=NOW)
    assert r["telemetry"] == str(p) and r["overall"]["requests"] == 12


def test_default_path_honours_apex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _write(home / "telemetry.jsonl", _ok(11))
    monkeypatch.delenv("APEX_TELEMETRY", raising=False)
    monkeypatch.setenv("APEX_HOME", str(home))
    r = pressure.compute(None, now=NOW)
    assert r["telemetry"] == str(home / "telemetry.jsonl") and r["overall"]["requests"] == 11


# ---------------------------------------------------------------- retried ≠ transport fault

def test_retried_then_succeeded_is_not_transport(tmp_path):
    rows = _ok(100, connect_retries=1)
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["level"] == "GREEN"
    assert r["overall"]["transport_errors"] == 0
    assert r["overall"]["retried"] == 100


def test_real_transport_fault_still_counts(tmp_path):
    rows = _ok(90) + _ok(10, start=NOW - 30, cause="SSLError", connect_retries=2)
    r = pressure.compute(_write(tmp_path / "t.jsonl", rows), now=NOW)
    assert r["overall"]["transport_errors"] == 10
    assert r["overall"]["retried"] == 10
    assert r["level"] == "AMBER"   # 10% transport → >= 3% amber, not > 10% red
