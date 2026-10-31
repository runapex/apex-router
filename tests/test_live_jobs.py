"""agent_runs.live_jobs reads the newest pi-jobs meta.json files; a "running" job with a dead pid does not
count."""
from __future__ import annotations

import json
import os
import subprocess
import time

from apex_router import agent_runs


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


def test_live_jobs_skips_an_oversized_meta_and_a_non_dict(tmp_path):
    now = time.time()
    _job(tmp_path, "20261027000001-aaaa", "running", os.getpid(), int(now * 1000), int(now * 1000))
    big = tmp_path / "20261027000002-bbbb"
    big.mkdir()
    (big / "meta.json").write_text('{"state": "running", "pid": %d, "x": "%s"}' % (os.getpid(), "a" * 300_000))
    lst = tmp_path / "20261027000003-cccc"
    lst.mkdir()
    (lst / "meta.json").write_text("[1, 2]")
    j = agent_runs.live_jobs(tmp_path, now)
    assert [i["id"] for i in j["items"]] == ["20261027000001-aaaa"]
