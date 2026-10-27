"""Sub-agent jobs in the menu bar: agent_runs.live_jobs reads the newest pi-jobs meta.json files;
a "running" job with a dead pid does not count; the menu shows a "Jobs · N running" section."""
from __future__ import annotations

import json
import os
import subprocess
import time

from apex_router import agent_runs, snapshot


def _job(d, jid, state, pid=None, started_ms=None, last_ms=None, agent="worker"):
    j = d / jid
    j.mkdir(parents=True)
    m = {"id": jid, "state": state, "agent": agent, "session_id": "apexjob-" + jid,
         "task": "TASK: do a thing", "started": started_ms, "last_event": last_ms}
    if pid is not None:
        m["pid"] = pid
    (j / "meta.json").write_text(json.dumps(m))


def _dead_pid():
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    return p.pid


def test_live_jobs_counts_only_running_with_a_live_pid_and_queued(tmp_path):
    now = time.time()
    ms = int((now - 120) * 1000)
    _job(tmp_path, "20261027000001-aaaa", "running", os.getpid(), ms, int((now - 3) * 1000))
    _job(tmp_path, "20261027000002-bbbb", "running", _dead_pid(), ms, ms)       # crashed: not live
    _job(tmp_path, "20261027000003-cccc", "done", os.getpid(), ms, ms)
    _job(tmp_path, "20261027000004-dddd", "queued", None, ms, None, agent="xval")
    (tmp_path / "20261027000005-eeee").mkdir()                                  # no meta: skipped
    (tmp_path / "20261027000006-ffff").mkdir()
    (tmp_path / "20261027000006-ffff" / "meta.json").write_text("{bad")        # broken: skipped
    j = agent_runs.live_jobs(tmp_path, now)
    assert j["running"] == 1 and j["queued"] == 1
    ids = [i["id"] for i in j["items"]]
    assert ids == ["20261027000004-dddd", "20261027000001-aaaa"]                # newest first
    run = j["items"][1]
    assert run["age_s"] == 120 and run["quiet_s"] == 3 and run["role"] == "worker"
    assert j["items"][0]["role"] == "judge"


def test_live_jobs_missing_dir_is_empty():
    assert agent_runs.live_jobs("/nonexistent/pi-jobs") == {"running": 0, "queued": 0, "items": []}


def _menu(jobs):
    snap = {"ts": time.time(), "pressure": {"level": "GREEN", "insufficient_sample": False},
            "agents": [], "system": {}, "jobs": jobs}
    return [ln.split(" | ")[0] for ln in snapshot.menubar(snap).splitlines()]


def test_menu_shows_a_jobs_section_only_while_jobs_run():
    assert not any(ln.startswith("Jobs") for ln in _menu({"running": 0, "queued": 0, "items": []}))
    items = [{"id": f"x-{i:04d}", "role": "worker", "state": "running", "age_s": 60 * (i + 1),
              "quiet_s": 400 if i == 0 else 2} for i in range(7)]
    m = _menu({"running": 7, "queued": 0, "items": items})
    i = m.index("Jobs · 7 running")
    assert m[i + 1] == "worker 0000 · running 1m · quiet 6m"                     # quiet > 5 min: shown
    assert m[i + 2] == "worker 0001 · running 2m"
    assert m[i + 6] == "+2 more"
