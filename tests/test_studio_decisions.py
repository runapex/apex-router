"""apex studio v2: the per-request decision flow (contract: docs/design/studio-v2/API.md, section
"Decision flow"). Every verdict comes from a fixed map; the route_log join is bounded (a capped
tail, same session, nearest earlier ts within 120 s). Fixture homes only (tmp_path)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from apex_router.studio import decisions as D
from apex_router.studio import live, traffic

import studio_fixture as F
from test_studio_v2 import SSE, _row, _write, get, home, studio  # noqa: F401  (fixtures)

UI = Path(__file__).resolve().parents[1] / "src" / "apex_router" / "studio" / "ui.html"
SID = "apexjob-20261027120000-ab12"
STEPS = ["task", "model", "guard", "cache", "send", "answer"]
LABELS = ["Task", "Model choice", "Safety blocks", "Cache", "Send", "Answer"]


def _tel(**kw):
    r = {"ts": 1000.0, "_ts": 1000.0, "client": "claude-code", "endpoint_id": "anthropic", "session_id": SID,
         "is_error": False, "error_cause": None, "connect_retries": 0, "upstream_rejected": False,
         "model_requested": "it-entra-claude-opus-5-5", "model_resolved": "it-entra-claude-opus-5-5",
         "tokens_in": 100, "tokens_out": 50, "cache_read_tokens": 900, "cache_write_tokens": 0,
         "bust": False, "bust_cause": "none", "cache_key_rewrite": None, "stop_reason": "end_turn",
         "refusal": False, "ttft_ms": 900.0, "total_ms": 4100.0, "guard_fired": False, "guard_action": "none"}
    r.update(kw)
    return r


def _idx(rows, tmp_path):
    p = tmp_path / "route_log.jsonl"
    _write(p, rows)
    return D.route_index(p)


def _by(ds):
    return {d["step"]: d for d in ds}


# ---- shape and fixed maps ------------------------------------------------------------------------

def test_six_steps_in_contract_order_with_fixed_labels(tmp_path):
    ds = D.decisions(_tel(), D.RouteIndex.empty())
    assert [d["step"] for d in ds] == STEPS
    assert [d["label"] for d in ds] == LABELS
    for d in ds:
        assert set(d) == {"step", "label", "verdict", "state"}
        assert d["state"] in ("ok", "warn", "error", "none") and isinstance(d["verdict"], str)


def test_a_clean_request_reads_plainly(tmp_path):
    idx = _idx([{"ts": 970.0, "session_id": SID, "task_type": "debug", "family": "deep", "escalated": False,
                 "note": "turn"}], tmp_path)
    b = _by(D.decisions(_tel(), idx))
    assert (b["task"]["verdict"], b["task"]["state"]) == ("debug", "ok")
    assert (b["model"]["verdict"], b["model"]["state"]) == ("deep → Claude Opus", "ok")
    assert (b["guard"]["verdict"], b["guard"]["state"]) == ("passed", "ok")
    assert (b["cache"]["verdict"], b["cache"]["state"]) == ("90 % from cache", "ok")
    assert (b["send"]["verdict"], b["send"]["state"]) == ("sent", "ok")
    assert (b["answer"]["verdict"], b["answer"]["state"]) == ("ended normally · 4.1 s", "ok")


def test_cache_share_counts_each_token_once_on_both_wires():
    # Anthropic: tokens_in is the uncached rest. OpenAI: cache_read is inside tokens_in.
    a = _by(D.decisions(_tel(tokens_in=100, cache_read_tokens=900, cache_write_tokens=0), D.RouteIndex.empty()))
    o = _by(D.decisions(_tel(endpoint_id="openai", tokens_in=1000, cache_read_tokens=900, cache_write_tokens=0),
                        D.RouteIndex.empty()))
    assert a["cache"]["verdict"] == o["cache"]["verdict"] == "90 % from cache"
    w = _by(D.decisions(_tel(tokens_in=100, cache_read_tokens=600, cache_write_tokens=300), D.RouteIndex.empty()))
    assert w["cache"]["verdict"] == "60 % from cache"
    z = _by(D.decisions(_tel(tokens_in=0, cache_read_tokens=0, cache_write_tokens=0), D.RouteIndex.empty()))
    assert (z["cache"]["verdict"], z["cache"]["state"]) == ("—", "none")


def test_cache_bust_is_amber_with_a_plain_cause():
    b = _by(D.decisions(_tel(bust=True, bust_cause="ttl"), D.RouteIndex.empty()))
    assert (b["cache"]["verdict"], b["cache"]["state"]) == ("cache missed: it expired", "warn")
    u = _by(D.decisions(_tel(bust=True, bust_cause="new raw cause text"), D.RouteIndex.empty()))
    assert (u["cache"]["verdict"], u["cache"]["state"]) == ("cache missed: cause unknown", "warn")


@pytest.mark.parametrize("n,want", [(1, "1 retry, then sent"), (2, "2 retries, then sent")])
def test_a_retried_request_says_how_many_retries_in_amber(n, want):
    b = _by(D.decisions(_tel(connect_retries=n, retry_arm="immediate"), D.RouteIndex.empty()))
    assert (b["send"]["verdict"], b["send"]["state"]) == (want, "warn")
    assert b["answer"]["state"] == "ok"


def test_not_reached_is_red_and_the_answer_is_empty():
    b = _by(D.decisions(_tel(is_error=True, error_cause="ConnectError", connect_retries=2, stop_reason=None,
                             model_resolved=None, total_ms=30000), D.RouteIndex.empty()))
    assert (b["send"]["verdict"], b["send"]["state"]) == ("2 retries, not reached", "error")
    assert (b["answer"]["verdict"], b["answer"]["state"]) == ("no answer", "none")
    c = _by(D.decisions(_tel(is_error=True, error_cause="ConnectTimeout", stop_reason=None), D.RouteIndex.empty()))
    assert (c["send"]["verdict"], c["send"]["state"]) == ("not reached", "error")


def test_provider_refusal_is_red_with_a_fixed_cause():
    b = _by(D.decisions(_tel(upstream_rejected=True, error_cause="http_429", stop_reason=None), D.RouteIndex.empty()))
    assert (b["send"]["verdict"], b["send"]["state"]) == ("provider said no: rate limit", "error")
    c = _by(D.decisions(_tel(is_error=True, upstream_rejected=True, error_cause="http_503", stop_reason=None),
                        D.RouteIndex.empty()))
    assert (c["send"]["verdict"], c["send"]["state"]) == ("provider said no: provider error", "error")


def test_a_failure_mid_answer_is_red_on_the_answer_station():
    b = _by(D.decisions(_tel(is_error=True, error_cause="midstream_ReadError", stop_reason=None), D.RouteIndex.empty()))
    assert (b["send"]["verdict"], b["send"]["state"]) == ("sent", "ok")
    assert (b["answer"]["verdict"], b["answer"]["state"]) == ("stopped in the middle · 4.1 s", "error")


@pytest.mark.parametrize("kw,verdict,state", [
    ({"stop_reason": "max_tokens"}, "stopped: length · 4.1 s", "warn"),
    ({"stop_reason": "length"}, "stopped: length · 4.1 s", "warn"),
    ({"refusal": True, "stop_reason": "refusal"}, "refused · 4.1 s", "warn"),
    ({"stop_reason": "tool_use"}, "asked for a tool · 4.1 s", "ok"),
    ({"stop_reason": "completed"}, "ended normally · 4.1 s", "ok"),
    ({"stop_reason": "end_turn", "total_ms": None}, "ended normally", "ok"),
    ({"stop_reason": "end_turn", "total_ms": 45000}, "ended normally · 45 s", "ok"),
    ({"stop_reason": "raw new reason"}, "ended · 4.1 s", "ok"),
])
def test_answer_verdicts(kw, verdict, state):
    b = _by(D.decisions(_tel(**kw), D.RouteIndex.empty()))
    assert (b["answer"]["verdict"], b["answer"]["state"]) == (verdict, state)


def test_guard_block_is_red_and_a_missing_source_is_none():
    b = _by(D.decisions(_tel(guard_fired=True, guard_action="fallback"), D.RouteIndex.empty()))
    assert (b["guard"]["verdict"], b["guard"]["state"]) == ("blocked: sent the safe copy", "error")
    r = _tel()
    del r["guard_fired"], r["guard_action"]
    m = _by(D.decisions(r, D.RouteIndex.empty()))
    assert (m["guard"]["verdict"], m["guard"]["state"]) == ("—", "none")


def test_model_station_warns_on_escalation_and_on_a_swap(tmp_path):
    idx = _idx([{"ts": 990.0, "session_id": SID, "task_type": "review", "family": "deep", "escalated": True}], tmp_path)
    b = _by(D.decisions(_tel(), idx))
    assert (b["model"]["verdict"], b["model"]["state"]) == ("escalated → Claude Opus", "warn")
    s = _by(D.decisions(_tel(model_resolved="it-entra-claude-sonnet-5-5"), D.RouteIndex.empty()))
    assert (s["model"]["verdict"], s["model"]["state"]) == ("Claude Opus → Claude Sonnet", "warn")
    n = _by(D.decisions(_tel(), D.RouteIndex.empty()))
    assert (n["model"]["verdict"], n["model"]["state"]) == ("Claude Opus", "ok")
    assert (n["task"]["verdict"], n["task"]["state"]) == ("—", "none")


# ---- route_log join ------------------------------------------------------------------------------

def test_join_same_session_nearest_earlier_within_120_s(tmp_path):
    idx = _idx([
        {"ts": 800.0, "session_id": SID, "task_type": "explore", "family": "deep", "escalated": False},
        {"ts": 950.0, "session_id": SID, "task_type": "review", "family": "frontier", "escalated": False},
        {"ts": 990.0, "session_id": "other-session", "task_type": "generate", "family": "deep", "escalated": False},
        {"ts": 1005.0, "session_id": SID, "task_type": "generate", "family": "deep", "escalated": False},
        {"ts": 990.0, "task_type": "debug", "family": "deep", "escalated": False},       # no session id
    ], tmp_path)
    assert _by(D.decisions(_tel(), idx))["task"]["verdict"] == "review"           # 950, not 1005 or 800
    assert _by(D.decisions(_tel(), idx))["model"]["verdict"] == "frontier → Claude Opus"
    far = _by(D.decisions(_tel(ts=1071.0, _ts=1071.0), idx))                      # 121 s after 950
    assert (far["task"]["verdict"], far["task"]["state"]) == ("generate", "ok")   # 1005 is in reach
    gone = _by(D.decisions(_tel(ts=1126.0, _ts=1126.0), idx))                     # 121 s after 1005
    assert (gone["task"]["verdict"], gone["task"]["state"]) == ("—", "none")
    other = _by(D.decisions(_tel(session_id="nobody"), idx))
    assert other["task"]["verdict"] == "—"
    none = _by(D.decisions(_tel(session_id=None), idx))
    assert none["task"]["verdict"] == "—"


def test_route_index_reads_only_a_capped_tail(tmp_path, monkeypatch):
    p = tmp_path / "route_log.jsonl"
    rows = [{"ts": 100.0 + i, "session_id": f"s{i}", "task_type": "debug", "family": "deep", "escalated": False,
             "pad": "x" * 200} for i in range(2000)]
    _write(p, rows)
    seen = []
    real = live._lines

    def spy(path, cap):
        seen.append(cap)
        return real(path, cap)
    monkeypatch.setattr(live, "_lines", spy)
    monkeypatch.setattr(D, "ROUTE_TAIL", 16 * 1024)
    idx = D.route_index(p)
    assert seen and max(seen) <= 16 * 1024
    assert idx.lookup("s1999", 1999 + 100.0 + 1) is not None          # the newest row is in the tail
    assert idx.lookup("s0", 101.0) is None                           # the oldest is not
    assert idx.rows <= 16 * 1024 // 200


def test_route_index_caps_rows_and_session_id_length(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "ROUTE_ROWS_MAX", 50)
    long_sid = "s" * 5000
    idx = _idx([{"ts": 1.0 + i, "session_id": f"a{i}", "task_type": "debug"} for i in range(200)]
               + [{"ts": 500.0, "session_id": long_sid, "task_type": "debug"}], tmp_path)
    assert idx.rows <= 50
    assert idx.lookup(long_sid, 501.0) is None


def test_route_index_missing_dir_and_fifo_do_not_hang(tmp_path):
    assert D.route_index(tmp_path / "nope.jsonl").rows == 0
    assert D.route_index(tmp_path).rows == 0
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    out = []
    th = threading.Thread(target=lambda: out.append(D.route_index(fifo)), daemon=True)
    th.start()
    th.join(2.0)
    assert out and out[0].rows == 0


def test_route_index_cached_rebuilds_when_the_file_changes(tmp_path):
    p = tmp_path / "route_log.jsonl"
    _write(p, [{"ts": 900.0, "session_id": SID, "task_type": "debug"}])
    a = D.route_index_cached(p)
    assert D.route_index_cached(p) is a
    _write(p, [{"ts": 950.0, "session_id": SID, "task_type": "review"}], mode="a")
    os.utime(p, (time.time() + 5, time.time() + 5))
    b = D.route_index_cached(p)
    assert b.lookup(SID, 1000.0)["task"] == "review"


# ---- no raw text --------------------------------------------------------------------------------

def test_no_raw_row_text_reaches_a_verdict(tmp_path):
    s = F.FAKE_SECRET
    idx = _idx([{"ts": 990.0, "session_id": SID, "task_type": s, "family": s, "escalated": False, "note": s,
                 "model": s}], tmp_path)
    ds = D.decisions(_tel(model_requested=s, model_resolved=s + "x", error_cause=s, stop_reason=s,
                          bust=True, bust_cause=s, guard_fired=True, guard_action=s, error_detail=s,
                          cache_key_rewrite={"from": "client", "key": "apex:" + s}, prompt=s), idx)
    blob = json.dumps(ds)
    for bad in (s, "sk-fixture", SID, "it-entra", "apex:"):
        assert bad not in blob, bad
    b = _by(ds)
    assert b["task"]["verdict"] == "other work"
    assert b["guard"]["verdict"] == "blocked: stopped"


def test_bad_types_never_raise():
    for bad in ({}, {"connect_retries": "x", "tokens_in": "y", "total_ms": True, "bust": "yes",
                     "stop_reason": 5, "guard_fired": "1", "session_id": 7, "_ts": "z"}):
        ds = D.decisions(bad, D.RouteIndex.empty())
        assert [d["step"] for d in ds] == STEPS


# ---- the field on /api/recent and on SSE request events -------------------------------------------

def test_recent_and_stream_items_carry_the_same_decisions(studio):
    srv, _, root, now = studio
    t = time.time()
    _write(root / ".apex-router" / "route_log.jsonl",
           [{"ts": t - 30, "session_id": F.PI_SID, "task_type": "debug", "family": "deep", "escalated": False}])
    s = SSE(srv)
    try:
        s.events(lambda o: len(o) >= 1)
        _write(root / ".apex" / "telemetry.jsonl", [_row(t, F.PI_SID, connect_retries=2, tokens_out=7,
                                                         stop_reason="end_turn", total_ms=4100)], mode="a")
        ev = s.events(lambda o: any(e[0] == "request" for e in o))
        r = [e[1] for e in ev if e[0] == "request"][0]
    finally:
        s.close()
    b = _by(r["decisions"])
    assert b["task"]["verdict"] == "debug"
    assert (b["send"]["verdict"], b["send"]["state"]) == ("2 retries, then sent", "warn")
    rec = get(srv, "/api/recent?n=1")[1]["requests"][0]
    assert rec["decisions"] == r["decisions"]
    assert F.PI_SID not in json.dumps(rec) and F.PI_SID not in json.dumps(r)


# ---- the UI: no prompt box, a live station line ---------------------------------------------------

def test_ui_has_no_try_prompt_and_no_test_fill():
    html = UI.read_text()
    for bad in ("Try a prompt", "tryBox", "try-in", "focusTry", "runTest", "/api/test", 'q.get("test")',
                "S.tryText", "innerHTML"):
        assert bad not in html, bad


def test_ui_draws_the_decision_line_from_the_decisions_field():
    html = UI.read_text()
    assert "decisions" in html and "STATION_MS = 150" in html
    for name in ("Task", "Model", "Safety", "Cache", "Send", "Answer"):
        assert f'"{name}"' in html, name
    assert "LIT_MAX = 10" in html
    assert "prefers-reduced-motion" in html


def test_flow_starts_only_after_recent_is_sorted():
    # xval: with reduced motion the flow painted before the sort, so a newer error was hidden from "Last 10"
    html = (Path(__file__).parents[1] / "src" / "apex_router" / "studio" / "ui.html").read_text()
    body = html[html.index("function addRequests("):]
    body = body[: body.index("\n}\n")]
    assert body.index("S.recent.sort(") < body.index("flowStart(")
