"""agent_runs collector: no text copied, idempotent, turns rule, verdict enum, context-alert proxy."""
from __future__ import annotations

import importlib
import json
import os

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
    # cross-validation scheme (xval briefs ask for AGREE | PARTIAL | DISAGREE)
    ("findings...\n\n**Verdict: AGREE**", "AGREE"),
    ("**VERDICT: PARTIAL**", "PARTIAL"),
    ("table\n\nVERDICT: DISAGREE", "DISAGREE"),
    ("No new findings.\n\nAGREE", "AGREE"),
    ("# job 2026-x — done\nagent xval · 9 s · 0 turns · $0.0000\nPARTIAL\n\nP1 open", "PARTIAL"),
    ("I partially agree with the plan, but verdict: pending.", None),
    ("We do not AGREE yet; I will DISAGREE if tests fail.", None),
    ("VERDICT: PENDING", None),                         # not in either scheme: no label
    ("table\n\nAPPROVED", None),
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


def test_refresh_verdicts_rewrites_old_rows_once(ar, tmp_path):
    # rows written by an older parser have verdict None; refresh re-reads result.md (enum only)
    _job(tmp_path, "x1", agent="xval", kind="xval", result="table\n\n**Verdict: AGREE**")
    _job(tmp_path, "w1", task="You are engineer E1.", result="VERDICT: AGREE")      # not a judge: stays None
    _job(tmp_path, "j1", task="You are judge J1.", result="VERDICT: GO")
    ar.collect_runs()
    rows = [json.loads(l) for l in ar.RUNS_OUT.read_text().splitlines()]
    for r in rows:
        r["verdict"] = None                                                      # as an old collector left it
    ar.RUNS_OUT.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    assert ar.refresh_verdicts() == 2
    got = {r["id"]: r["verdict"] for r in map(json.loads, ar.RUNS_OUT.read_text().splitlines())}
    assert got == {"x1": "AGREE", "w1": None, "j1": "GO"}
    assert ar.refresh_verdicts() == 0                                            # idempotent
    assert oct(ar.RUNS_OUT.stat().st_mode & 0o777) in ("0o600", "0o644")
    assert "SECRET" not in ar.RUNS_OUT.read_text()


def test_refresh_keeps_rows_whose_job_dir_is_gone(ar, tmp_path):
    import shutil
    _job(tmp_path, "x1", agent="xval", kind="xval", result="AGREE")
    ar.collect_runs()
    shutil.rmtree(tmp_path / "pi-jobs" / "x1")
    before = ar.RUNS_OUT.read_text()
    assert ar.refresh_verdicts() == 0 and ar.RUNS_OUT.read_text() == before


def test_refresh_and_append_never_lose_a_row(ar, tmp_path, monkeypatch):
    # P1: an append that arrives while a refresh holds its snapshot waits for the lock, then lands
    import threading
    _job(tmp_path, "x1", agent="xval", kind="xval", result="AGREE")
    ar.collect_runs()
    rows = [json.loads(l) for l in ar.RUNS_OUT.read_text().splitlines()]
    rows[0]["verdict"] = None
    ar.RUNS_OUT.write_text(json.dumps(rows[0], sort_keys=True) + "\n")
    in_refresh, appended = threading.Event(), threading.Event()
    real = ar._verdict

    def slow_verdict(text):
        in_refresh.set()
        appended.wait(0.5)                    # the append below is blocked by the lock: times out
        return real(text)

    monkeypatch.setattr(ar, "_verdict", slow_verdict)
    t = threading.Thread(target=ar.refresh_verdicts)
    t.start()
    assert in_refresh.wait(5)
    w = threading.Thread(target=lambda: (ar._append(ar.RUNS_OUT, [{"id": "late", "role": "worker"}]),
                                         appended.set()))
    w.start()
    t.join(5)
    w.join(5)
    ids = [json.loads(l)["id"] for l in ar.RUNS_OUT.read_text().splitlines()]
    assert ids == ["x1", "late"]
    assert json.loads(ar.RUNS_OUT.read_text().splitlines()[0])["verdict"] == "AGREE"


def test_refresh_temp_file_is_never_world_readable(ar, tmp_path, monkeypatch):
    # P2: the temp copy is created 0600, then gets the log's own mode
    _job(tmp_path, "x1", agent="xval", kind="xval", result="AGREE")
    ar.collect_runs()
    rows = [json.loads(l) for l in ar.RUNS_OUT.read_text().splitlines()]
    rows[0]["verdict"] = None
    ar.RUNS_OUT.write_text(json.dumps(rows[0], sort_keys=True) + "\n")
    os.chmod(ar.RUNS_OUT, 0o600)
    os.chmod(ar.RUNS_OUT, 0o644)              # a public log: the temp copy must still START 0600
    seen = []
    real_fdopen = os.fdopen

    def spy(fd, *a, **kw):                    # mode of the temp file while its content is written
        seen.append(os.fstat(fd).st_mode & 0o777)
        return real_fdopen(fd, *a, **kw)

    monkeypatch.setattr(ar.os, "fdopen", spy)
    old = os.umask(0o022)
    try:
        assert ar.refresh_verdicts() == 1
    finally:
        os.umask(old)
    assert seen == [0o600] and (ar.RUNS_OUT.stat().st_mode & 0o777) == 0o644
    assert not ar.RUNS_OUT.with_name(ar.RUNS_OUT.name + ".tmp").exists()


# --- C2: one canonical id per model version (report-time only) ---

@pytest.mark.parametrize("raw", ["it-entra-claude-sonnet-5-5", "sonnet", "sonnet-5-5", "claude-sonnet-5-5",
                                 "anthropic/claude-sonnet-5-5"])
def test_sonnet_strings_give_one_canonical_id(ar, raw):
    from apex_router import model_registry
    assert ar.canonical_model(raw, model_registry.DEFAULTS) == "sonnet-5-5"


def test_canonical_id_keeps_versions_apart_and_unknown_unchanged(ar):
    from apex_router import model_registry
    reg = model_registry.DEFAULTS
    ids = {ar.canonical_model(m, reg) for m in ("it-entra-claude-sonnet-5-5", "it-entra-claude-sonnet-4-6",
                                                "claude-opus-4-5", "it-entra-claude-opus-5-5")}
    assert ids == {"sonnet-5-5", "sonnet-4-6", "opus-4-5", "opus-5-5"}
    assert ar.canonical_model("it-entra-gpt-6.1-sol", reg) == "gpt-6.1-sol"
    assert ar.canonical_model("openai/gpt-5.5", reg) == "gpt-5.5"
    assert ar.canonical_model("it-entra-gpt-6-astra", reg) != ar.canonical_model("it-entra-gpt-6.1-sol", reg)
    for raw in ("deepseek-v4-pro", "kimi-k2.6", "MyModel", None, ""):
        assert ar.canonical_model(raw, reg) == raw


def test_report_groups_sonnet_strings_and_keeps_raw(ar, tmp_path):
    for i, m in enumerate(("it-entra-claude-sonnet-5-5", "sonnet-5-5", "claude-sonnet-5-5", "it-entra-claude-sonnet-4-6")):
        _job(tmp_path, f"j{i}", model=m)
    ar.collect_runs()
    stored = {json.loads(l)["model"] for l in ar.RUNS_OUT.read_text().splitlines()}
    assert "sonnet-5-5" in stored and "claude-sonnet-5-5" in stored      # stored rows keep the raw string
    cells = {c["model"]: c for c in ar.report()["by_role_model"]}
    assert set(cells) == {"sonnet-5-5", "sonnet-4-6"}
    assert cells["sonnet-5-5"]["n"] == 3 and len(cells["sonnet-5-5"]["models_raw"]) == 3


# --- C3: xval USD only from an audited rate ---

def test_xval_usd_from_a_known_list_price(ar):
    usd, regime = ar.xval_usd("it-entra-gpt-5.5", 1_000_000, 400_000, 100_000)
    # gpt-5 list: input 15, cache_read 1.5, output 60 $/M; fresh = 600k
    assert regime.startswith("list:gpt-5/openai")
    assert usd == pytest.approx(0.6 * 15 + 0.4 * 1.5 + 0.1 * 60)


def test_xval_usd_unknown_price_gives_null(ar):
    assert ar.xval_usd("it-entra-gpt-6-astra", 1000, 0, 10)[0] is None      # provisional placeholder rate
    assert ar.xval_usd("some-unpriced-model", 1000, 0, 10)[0] is None
    assert ar.xval_usd("it-entra-gpt-5-mini", 1000, 0, 10)[0] is None       # variant: no own rate
    assert ar.xval_usd("it-entra-gpt-5.5", None, None, None)[0] is None     # no token counts


def _xval_job(tmp_path, jid, model, t0):
    _job(tmp_path, jid, agent="xval", kind="xval", model=model, turns=0, started=t0 * 1000, ended=t0 * 1000 + 100_000)
    meta = json.loads((tmp_path / "pi-jobs" / jid / "meta.json").read_text())
    meta["usage"]["cost"] = 0
    (tmp_path / "pi-jobs" / jid / "meta.json").write_text(json.dumps(meta))
    return {"ts": t0 + 50.0, "calls": 3, "input": 1_000_000, "cached": 400_000, "output": 100_000, "cost": 218919.8, "model": model}


def test_report_xval_cost_in_usd_or_unknown(ar, tmp_path):
    x = [_xval_job(tmp_path, "x1", "it-entra-gpt-5.5", 1_000), _xval_job(tmp_path, "x2", "it-entra-gpt-6-astra", 5_000)]
    (tmp_path / "xval_runs.jsonl").write_text("\n".join(json.dumps(r) for r in x) + "\n")
    ar.collect_runs()
    rows = [json.loads(l) for l in ar.RUNS_OUT.read_text().splitlines()]
    assert all(r["cost_usd"] is None for r in rows)                          # stored schema unchanged
    cells = {c["model"]: c for c in ar.report()["by_role_model"]}
    assert cells["gpt-5.5"]["cost_sum"] == pytest.approx(15.6) and cells["gpt-5.5"]["cost_unknown"] == 0
    astra = cells["gpt-6-astra"]
    assert astra["cost_sum"] is None and astra["cost_note"] == "usd unknown" and astra["cost_unknown"] == 1
    assert astra["pricing"][0].startswith("provisional:")


# ---- archive: finished jobs leave pi-jobs (every pi session lists pi-jobs on each 2 s tick) --------------

DAY_MS = 86_400_000
NOW = 100 * DAY_MS / 1000


def _set(home, jid, **kv):
    p = home / "pi-jobs" / jid / "meta.json"
    m = json.loads(p.read_text())
    m.update(kv)
    p.write_text(json.dumps(m))


def test_archive_moves_only_old_notified_finished_collected_jobs(ar, tmp_path):
    old, new = 90 * DAY_MS, 95 * DAY_MS                                   # NOW - 10 days, NOW - 5 days
    for jid, state in (("a-old-done", "done"), ("b-old-failed", "failed"), ("c-old-unnotified", "done"),
                       ("d-new-done", "done"), ("e-old-running", "running")):
        _job(tmp_path, jid, state=state, started=old - 1000, ended=new if jid.startswith("d-") else old)
        _set(tmp_path, jid, notified=not jid.startswith("c-"))
    ar.collect_runs()
    _job(tmp_path, "f-old-uncollected", started=old - 1000, ended=old)   # finished, but not in agent_runs.jsonl yet
    _set(tmp_path, "f-old-uncollected", notified=True)
    assert ar.archive_jobs(now=NOW) == 2
    assert sorted(p.name for p in (tmp_path / "pi-jobs-archive").iterdir()) == ["a-old-done", "b-old-failed"]
    assert sorted(p.name for p in (tmp_path / "pi-jobs").iterdir()) == [
        "c-old-unnotified", "d-new-done", "e-old-running", "f-old-uncollected"]
    assert (tmp_path / "pi-jobs-archive" / "a-old-done" / "result.md").read_text() == "done"   # whole job dir moved
    assert ar.archive_jobs(now=NOW) == 0                                                      # idempotent


def test_archive_never_overwrites_an_archived_job(ar, tmp_path):
    _job(tmp_path, "a", ended=1)
    _set(tmp_path, "a", notified=True)
    ar.collect_runs()
    (tmp_path / "pi-jobs-archive" / "a").mkdir(parents=True)
    (tmp_path / "pi-jobs-archive" / "a" / "keep").write_text("x")
    assert ar.archive_jobs(now=NOW) == 0
    assert (tmp_path / "pi-jobs" / "a" / "meta.json").is_file()
    assert (tmp_path / "pi-jobs-archive" / "a" / "keep").read_text() == "x"


def test_archive_skips_unreadable_meta_and_missing_dirs(ar, tmp_path):
    assert ar.archive_jobs(now=NOW) == 0                                  # no pi-jobs, no agent_runs.jsonl
    _job(tmp_path, "a", ended=1)
    _set(tmp_path, "a", notified=True)
    ar.collect_runs()
    (tmp_path / "pi-jobs" / "a" / "meta.json").write_text("{broken")
    assert ar.archive_jobs(now=NOW) == 0


def test_collect_cli_archives_and_refresh_reads_the_archive(ar, tmp_path, capsys):
    _job(tmp_path, "j1", task="You are judge J1.", result="VERDICT: GO", ended=1)
    _set(tmp_path, "j1", notified=True)
    assert ar.main(["collect", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["runs_new"] == 1 and res["archived"] == 1
    assert (tmp_path / "pi-jobs-archive" / "j1").is_dir()
    rows = [json.loads(l) for l in ar.RUNS_OUT.read_text().splitlines()]
    rows[0]["verdict"] = None
    ar.RUNS_OUT.write_text(json.dumps(rows[0], sort_keys=True) + "\n")
    assert ar.refresh_verdicts() == 1                                     # result.md found in the archive


@pytest.mark.parametrize("ended", ["missing", True, float("nan"), float("-inf"), "7d-exact", "text"])
def test_archive_needs_a_real_end_time_older_than_the_cut(ar, tmp_path, ended):
    _job(tmp_path, "a", started=1, ended=1)
    meta = tmp_path / "pi-jobs" / "a" / "meta.json"
    m = json.loads(meta.read_text())
    m.update(notified=True, created=1)
    if ended == "missing":
        del m["ended"]
    else:
        m["ended"] = (NOW - 7 * 86400) * 1000 if ended == "7d-exact" else ended
    ar.collect_runs()
    meta.write_text(json.dumps(m))
    assert ar.archive_jobs(now=NOW) == 0
    assert meta.is_file()
