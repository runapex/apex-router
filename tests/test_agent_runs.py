"""agent_runs collector: no text copied, idempotent, turns rule, verdict enum, context-alert proxy."""
from __future__ import annotations

import importlib
import json

import pytest

SECRET = "SECRET-BRIEF-TEXT do not copy /srv/private/repo"


@pytest.fixture
def ar(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_HOME", str(tmp_path))
    import apex_router.agent_runs as mod
    return importlib.reload(mod)


def _job(home, jid, *, agent="worker", kind="pi", state="done", task="TASK: x", result="done", turns=3, tools=2,
         model="it-entra-claude-sonnet-5-5", started=1_000_000, ended=1_060_000):
    d = home / "pi-jobs" / jid
    d.mkdir(parents=True)
    argv = ["pi", "--provider", "foundry", "--model", model, "--thinking", "medium", f"Task: {task}"]
    if kind == "xval":
        argv = ["apex-router", "xval", "-m", model, task]
    meta = {"id": jid, "state": state, "agent": agent, "kind": kind, "task": task, "argv": argv, "started": started,
            "ended": ended, "usage": {"turns": turns, "input": 10, "output": 20, "cost": 0.5}, "max_cost": 5}
    (d / "meta.json").write_text(json.dumps(meta))
    ev = [{"type": "turn_end", "message": {"content": SECRET}}] * turns + [{"type": "tool_execution_start"}] * tools
    (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in ev) + "\n")
    (d / "result.md").write_text(result)
    return d


def test_rows_carry_no_text_and_count_turns(ar, tmp_path):
    _job(tmp_path, "j1", task=f"You are engineer E1. {SECRET}", result=f"{SECRET}\nall done", turns=4, tools=7)
    assert ar.collect_runs() == 1
    raw = ar.RUNS_OUT.read_text()
    assert "SECRET" not in raw and "/srv/" not in raw
    row = json.loads(raw)
    assert row["role"] == "engineer" and row["turns"] == 4 and row["tool_calls"] == 7
    assert row["turns_source"] == "pi turn_end"
    assert row["model"] == "it-entra-claude-sonnet-5-5" and row["provider"] == "foundry" and row["thinking"] == "medium"
    assert row["wall_s"] == 60.0 and row["cost_usd"] == 0.5 and row["verdict"] is None


def test_collect_is_idempotent_and_skips_running_jobs(ar, tmp_path):
    _job(tmp_path, "j1")
    _job(tmp_path, "j2", state="running")
    assert ar.collect_runs() == 1
    assert ar.collect_runs() == 0
    assert len(ar.RUNS_OUT.read_text().splitlines()) == 1


@pytest.mark.parametrize("answer,want", [
    ("lots of text, we GO on\n\nVERDICT: NO-GO", "NO-GO"),
    ("**Verdict:** GO-WITH-CHANGES", "GO-WITH-CHANGES"),
    ("Five MUST-FIX remain.\n\n**NO-GO**", "NO-GO"),
    ("no decision here", None),
    ("No verdict yet; need the failing test before giving GO or NO-GO.", None),
    ("No verdict. GO is not justified until tests finish.", None),
    ("**VERDICT: GO**\n\n" + "long report line\n" * 400, "GO"),
    ("## Verdict - NO-GO\nmore", "NO-GO"),
    ("**NO-GO**\n\nMUST-FIX: x.py:3 is wrong.", "NO-GO"),
    ("GO\n\nMUST-FIX: none.", "GO"),
    ("# job 2026-x — done\nagent xval · 379 s · 0 turns · $0.0000\nGO\n\nMUST-FIX: none.", "GO"),
    ("# job 2026-x — done\nagent xval · 9 s · 0 turns · $0.0000\n**NO-GO**\n\nMUST-FIX: a", "NO-GO"),
    ("GO for `ec781c4`.\n\n- MUST-FIX: None.", None),
])
def test_judge_verdict_is_an_enum(ar, tmp_path, answer, want):
    _job(tmp_path, "j1", task="You are judge J2. vote", result=answer)
    ar.collect_runs()
    assert json.loads(ar.RUNS_OUT.read_text())["verdict"] == want


def test_xval_turns_come_from_codex_calls(ar, tmp_path):
    _job(tmp_path, "x1", agent="xval", kind="xval", model="it-entra-gpt-6-astra", turns=0, started=1_000_000, ended=1_100_000)
    (tmp_path / "xval_runs.jsonl").write_text(json.dumps({"ts": 1_095.0, "calls": 6, "input": 100, "cached": 50, "output": 9, "cost": 77.0}) + "\n")
    ar.collect_runs()
    row = json.loads(ar.RUNS_OUT.read_text())
    assert row["role"] == "judge" and row["turns"] == 6 and row["turns_source"] == "codex calls" and row["xval_cost_units"] == 77.0


def _measures(home, pts, sid="0000abcd"):
    (home / "observe").mkdir(parents=True, exist_ok=True)
    rows = [{"ev": "measure", "ts": t, "ctx_pct": c, "limit_pct": None, "sid": sid} for t, c in pts]
    (home / "observe" / "2026-10-20.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_context_alert_rows_and_acted_proxy(ar, tmp_path):
    m = 60_000
    # alert 1: 85% at t=0, compacted to 20% at 5 min -> acted; alert 2: 90% at 60 min, stays high -> not acted
    _measures(tmp_path, [(0, 70), (1 * m, 85), (6 * m, 20), (60 * m, 90), (70 * m, 92), (100 * m, 91)])
    assert ar.collect_alerts(now_ms=200 * m) == 2
    rows = [json.loads(l) for l in ar.ALERTS_OUT.read_text().splitlines()]
    assert [(r["ctx_pct"], r["acted"], r["lag_s"]) for r in rows] == [(85, True, 300), (90, False, None)]
    assert ar.collect_alerts(now_ms=200 * m) == 0


def test_open_alert_waits_for_its_window(ar, tmp_path):
    m = 60_000
    _measures(tmp_path, [(0, 85)])
    assert ar.collect_alerts(now_ms=10 * m) == 0  # outcome unknown yet: no row
    assert ar.collect_alerts(now_ms=40 * m) == 1


def test_report_groups_by_role_and_model(ar, tmp_path):
    _job(tmp_path, "j1", task="You are judge J1.", result="VERDICT: GO")
    _job(tmp_path, "j2", task="You are judge J1.", result="VERDICT: NO-GO", state="killed(cost)")
    ar.collect_runs()
    rep = ar.report()
    (cell,) = rep["by_role_model"]
    assert cell["role"] == "judge" and cell["n"] == 2 and cell["cost_capped"] == 1 and cell["verdicts"] == {"GO": 1, "NO-GO": 1}


def test_alerts_keep_sessions_apart_and_skip_rows_without_sid(ar, tmp_path):
    m = 60_000
    (tmp_path / "observe").mkdir(parents=True)
    rows = [
        {"ev": "measure", "ts": 0, "ctx_pct": 85, "sid": "aaaaaaaa"},
        {"ev": "measure", "ts": 1 * m, "ctx_pct": 20, "sid": "bbbbbbbb"},  # another session: not an "acted"
        {"ev": "measure", "ts": 2 * m, "ctx_pct": 90},                     # no sid: skipped
    ]
    (tmp_path / "observe" / "d.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert ar.collect_alerts(now_ms=100 * m) == 1
    (row,) = [json.loads(l) for l in ar.ALERTS_OUT.read_text().splitlines()]
    assert row["sid"] == "aaaaaaaa" and row["acted"] is False


def test_ambiguous_xval_match_is_left_empty(ar, tmp_path):
    _job(tmp_path, "x1", agent="xval", kind="xval", model="it-entra-gpt-6-astra", turns=0, started=1_000_000, ended=1_100_000)
    (tmp_path / "xval_runs.jsonl").write_text("\n".join(json.dumps({"ts": t, "calls": c}) for t, c in ((1_000.0, 6), (1_050.0, 2))) + "\n")
    ar.collect_runs()
    row = json.loads(ar.RUNS_OUT.read_text())
    assert "xval_calls" not in row and row["turns_source"] == "pi usage"


def test_run_daily_survives_a_collector_crash(monkeypatch, tmp_path):
    import types
    monkeypatch.setenv("APEX_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    import apex_router.agent_runs as mod
    from apex_router import nightly, route_log, watch
    from apex_router.ornith import offload_report

    def boom(*a, **k):
        raise RuntimeError("collector broke")

    monkeypatch.setattr(mod, "collect_runs", boom)
    monkeypatch.setattr(mod, "collect_alerts", boom)
    monkeypatch.setattr(offload_report, "aggregate_offload", lambda *a, **k: {})
    monkeypatch.setattr(offload_report, "format_report", lambda *a, **k: "report")
    monkeypatch.setattr(route_log, "read_rates", lambda *a, **k: {})
    monkeypatch.setattr(nightly, "run", lambda *a, **k: "\n## nightly\n")
    assert watch.run_daily() == 0
    assert (tmp_path / ".apex-router" / "offload_daily.md").exists()


def test_soft_capped_done_job_keeps_its_flag(ar, tmp_path):
    d = _job(tmp_path, "j1")
    meta = json.loads((d / "meta.json").read_text())
    meta.update({"capped": True, "soft_cap_at": 1_050_000})
    (d / "meta.json").write_text(json.dumps(meta))
    _job(tmp_path, "j2")
    ar.collect_runs()
    rows = {json.loads(l)["id"]: json.loads(l) for l in ar.RUNS_OUT.read_text().splitlines()}
    assert rows["j1"]["capped"] is True and rows["j1"]["state"] == "done"
    assert rows["j2"]["capped"] is False
    (cell,) = ar.report()["by_role_model"]
    assert cell["soft_capped"] == 1 and cell["cost_capped"] == 0
