"""Quality section (24 h): routing classification, tier conformance, reliability, verification."""
from __future__ import annotations

import json

from apex_router import quality, snapshot

NOW = 1_791_396_000.0


def _w(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _tel(i, **kw):
    r = {"ts": NOW - 60 * i, "client": "claude-code", "model_requested": "m", "session_id": "s",
         "is_error": False, "upstream_rejected": False, "error_cause": None,
         "connect_retries": 0, "connect_backoff_ms": 0.0}
    r.update(kw)
    return r


def _fixture(tmp_path):
    home = tmp_path / "ar"
    _w(home / "route_log.jsonl", [
        {"ts": NOW - 10, "task_type": "debug", "escalated": False, "note": "turn"},
        {"ts": NOW - 20, "task_type": "debug", "escalated": True, "note": "turn: provider error"},
        {"ts": NOW - 30, "task_type": "review", "escalated": False},
        {"ts": NOW - 40, "task_type": "review", "escalated": False, "label_pending": True},
        {"ts": NOW - 2 * 86400, "task_type": "old", "escalated": True},          # outside 24 h
    ])
    _w(home / "conformance.jsonl", [
        {"ts": NOW - 5, "surface": "resolve", "task_type": "debug", "requested_tier": "opus",
         "resolved_model": "claude-opus-5-5", "matched": True},
        {"ts": NOW - 6, "surface": "resolve", "task_type": "explore", "requested_tier": "sonnet",
         "resolved_model": "kimi", "matched": False},
        {"ts": NOW - 7, "surface": "agent", "task_type": "x", "requested_tier": "haiku",
         "resolved_model": None, "matched": None},                               # intent only
    ])
    _w(home / "xval_runs.jsonl", [
        {"ts": NOW - 100, "ok": True, "seconds": 100, "truncated_outputs": 1},
        {"ts": NOW - 200, "ok": False, "seconds": 300, "feedback": "bad"},
    ])
    tel = tmp_path / "telemetry.jsonl"
    _w(tel, [_tel(i) for i in range(196)] + [
        _tel(200, upstream_rejected=True, error_cause="http_400"),               # 4xx: not is_error
        _tel(201, is_error=True, error_cause="midstream_ReadError"),
        _tel(202, is_error=True),                                                # no cause
        _tel(203, connect_retries=2, connect_backoff_ms=150.0)])
    return home, tel


def test_collect_counts_each_source(tmp_path, monkeypatch):
    home, tel = _fixture(tmp_path)
    monkeypatch.setattr(quality.Path, "home", lambda: tmp_path)    # codeqa log: absent
    q = quality.collect(NOW, tel, home)
    c = q["classification"]
    assert c["n"] == 3 and c["types"] == {"debug": 2, "review": 1}   # pending + old left out
    assert c["escalated"] == 1 and c["escalation_causes"] == {"provider error": 1}
    m = q["conformance"]
    assert (m["n"], m["observed"], m["mismatched"]) == (3, 2, 1)     # null match: not observed
    assert m["mismatches"] == {"explore: sonnet → kimi": 1}
    r = q["reliability"]
    assert r["requests"] == 200 and r["failed"] == 3
    assert r["retries"] == 2 and r["retried_requests"] == 1 and r["retry_wait_ms"] == 150
    assert r["midstream"] == 1 and r["causes"] == {"http_400": 1, "midstream_ReadError": 1}
    v = q["verification"]
    assert v["xval"] == {"runs": 2, "ok": 1, "bad_feedback": 1, "truncated": 1, "median_s": 200.0}
    assert v["codeqa"] == {"missing": True}


def test_quality_lines_and_colours(tmp_path, monkeypatch):
    home, tel = _fixture(tmp_path)
    monkeypatch.setattr(quality.Path, "home", lambda: tmp_path)
    lines = snapshot._quality_lines(quality.collect(NOW, tel, home))
    plain = [ln.split(" | ")[0] for ln in lines]
    assert plain == ["routing 3 tasks · debug 2 review 1 · escalated 1 (33%)",
                     "tier match 1/2 (50%)",
                     "requests 200 · ok 98% · retries 2 · failed 3",
                     "verify xval 1/2 verdict"]
    assert "escalated because: provider error 1" in lines[0]
    assert "explore: sonnet → kimi ×1" in lines[1]
    assert "no cause recorded 1" in lines[2]
    assert all("color=" in ln for ln in lines)
    assert snapshot.INK["amber"] in lines[1] and snapshot.INK["amber"] in lines[3]


def test_pct_never_rounds_a_shortfall_to_100():
    assert snapshot._pct(1865, 1874) == "99.5%"          # live: 9 failures must not read 100%
    assert snapshot._pct(9999, 10000) == "99.9%"
    assert snapshot._pct(10, 10) == "100%" and snapshot._pct(1, 2) == "50%"
    assert snapshot._pct(1, 10000) == "0.1%" and snapshot._pct(0, 5) == "0%"
    assert snapshot._pct(0, 0) == "–"


def test_missing_sources_say_so(tmp_path, monkeypatch):
    monkeypatch.setattr(quality.Path, "home", lambda: tmp_path)
    q = quality.collect(NOW, tmp_path / "none.jsonl", tmp_path / "empty")
    plain = [ln.split(" | ")[0] for ln in snapshot._quality_lines(q)]
    assert plain == ["routing ?", "tier match ?", "requests ?"]       # no fake zeros


def test_menu_has_quality_section():
    snap = {"pressure": {}, "quality": {"classification": {"n": 0, "types": {}},
                                       "conformance": {"n": 0, "observed": 0},
                                       "reliability": {"requests": 4, "failed": 0, "retries": 0},
                                       "verification": {}}}
    out = snapshot.menubar(snap)
    assert "Quality · 24h |" in out
    assert "requests 4 · ok 100% · retries 0" in out
