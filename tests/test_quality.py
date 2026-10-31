"""Quality section (24 h): routing classification, tier conformance, reliability, verification."""
from __future__ import annotations

import json

from apex_router import quality

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
    assert c["n"] == 2 and c["types"] == {"debug": 1, "review": 1}
    assert c["infra"] == 1 and c["escalated"] == 0 and c["escalation_causes"] == {}
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


def test_missing_sources_say_so(tmp_path, monkeypatch):
    monkeypatch.setattr(quality.Path, "home", lambda: tmp_path)
    q = quality.collect(NOW, tmp_path / "none.jsonl", tmp_path / "empty")
    for k in ("classification", "conformance", "reliability"):         # no fake zeros (the menu read "?")
        assert q[k] == {"missing": True}
