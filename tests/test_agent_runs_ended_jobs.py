"""agent_runs.ended_jobs: final job states with their end time; a failed or slow read raises or says skipped
(moved from test_menubar_problems.py when the menu bar retired, D25)."""
from __future__ import annotations

import json
import os
import time

import pytest

from apex_router import agent_runs

NOW = 1_791_400_000.0


def _meta(d, jid, **m):
    j = d / jid
    j.mkdir(parents=True)
    (j / "meta.json").write_text(json.dumps(dict({"id": jid, "agent": "worker", "task": "TASK: x"}, **m)))


def test_ended_jobs_reads_final_states_with_their_end_time(tmp_path):
    now = time.time()
    ms = lambda ago: int((now - ago) * 1000)  # noqa: E731
    _meta(tmp_path, "20261027000001-aaaa", state="failed", started=ms(900), ended=ms(300))
    _meta(tmp_path, "20261027000002-bbbb", state="done", started=ms(900), ended=ms(200), capped=True)
    _meta(tmp_path, "20261027000003-cccc", state="running", pid=os.getpid(), started=ms(100))
    big = tmp_path / "20261027000004-dddd"
    big.mkdir()
    (big / "meta.json").write_text('{"state": "failed", "x": "%s"}' % ("a" * 300_000))
    out = agent_runs.ended_jobs(tmp_path, now)
    by = {i["id"]: i for i in out["items"]}
    assert set(by) == {"20261027000001-aaaa", "20261027000002-bbbb"}
    assert by["20261027000001-aaaa"]["state"] == "failed" and by["20261027000001-aaaa"]["ended_ago_s"] == 300
    assert by["20261027000002-bbbb"]["capped"] is True
    assert "task" not in by["20261027000001-aaaa"]
    assert agent_runs.ended_jobs(tmp_path / "missing", now) == {"items": []}


# ---------------------------------------------------------------- alert: dedupe, rate, state


def _job(d, jid, meta):
    (d / jid).mkdir(parents=True)
    (d / jid / "meta.json").write_text(json.dumps(meta) if isinstance(meta, dict) else meta)


def test_ended_jobs_unreadable_meta_raises_so_the_menu_says_not_checked(tmp_path):
    from apex_router import agent_runs
    _job(tmp_path, "20261028100000-aaaa", {"id": "20261028100000-aaaa", "state": "failed", "ended": NOW - 60})
    os.chmod(tmp_path / "20261028100000-aaaa" / "meta.json", 0)
    try:
        with pytest.raises(PermissionError):
            agent_runs.ended_jobs(tmp_path, NOW)
    finally:
        os.chmod(tmp_path / "20261028100000-aaaa" / "meta.json", 0o600)


def test_ended_jobs_damaged_meta_raises(tmp_path):
    from apex_router import agent_runs
    _job(tmp_path, "20261028100000-aaaa", "{not json")
    with pytest.raises(ValueError):
        agent_runs.ended_jobs(tmp_path, NOW)


def test_ended_jobs_folder_without_meta_is_skipped(tmp_path):
    from apex_router import agent_runs
    (tmp_path / "20261028100000-bbbb").mkdir()
    assert agent_runs.ended_jobs(tmp_path, NOW) == {"items": []}


def test_ended_jobs_stops_at_the_deadline_and_says_skipped(tmp_path):
    from apex_router import agent_runs
    for i in range(5):
        _job(tmp_path, f"2026102810000{i}-aaaa", {"state": "failed", "ended": NOW - 60})
    left = iter([1.0, 1.0, 0.0, 0.0, 0.0, 0.0])
    r = agent_runs.ended_jobs(tmp_path, NOW, remaining=lambda: next(left))
    assert r.get("skipped") is True and "items" not in r

