"""apex studio Workflows backend (contract: docs/design/studio-v2/API.md, section "Workflows"):
GET /api/workflows, POST /api/workflows/join and /split, SSE ``workflow`` events. Every test uses a
fixture home in tmp_path; nothing reads the real ~/.pi, ~/.claude or ~/.apex-router."""
from __future__ import annotations

import datetime
import http.client
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from apex_router.studio import flow, server, traffic, workflows

import studio_fixture as F

TOKEN = "w" * 32
PROMPT = "PLEASE-REFACTOR-MY-PRIVATE-PROMPT-TEXT-7731"
SECRET2 = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab"


# ---- fixture builders --------------------------------------------------------------------------

def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _fname_ts(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-000Z")


def _job_name(ts: float, suffix: str) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + suffix


def make_repo(root: Path, name: str, branch: str) -> Path:
    r = root / "dev" / name
    (r / ".git").mkdir(parents=True, exist_ok=True)
    (r / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    return r


class PiSession:
    """Writes one pi session file in the real layout."""

    def __init__(self, root: Path, sid: str, cwd: Path, start: float):
        self.sid, self.cwd, self.start = sid, str(cwd), start
        d = root / ".pi" / "agent" / "sessions" / ("--" + str(cwd).strip("/").replace("/", "-") + "--")
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"{_fname_ts(start)}_{sid}.jsonl"
        self.rows = [{"type": "session", "version": 3, "id": sid, "timestamp": _iso(start), "cwd": self.cwd}]
        self.n = 0
        self.last = start

    def _msg(self, ts, message):
        self.n += 1
        self.rows.append({"type": "message", "id": f"m{self.n:07x}", "parentId": None, "timestamp": _iso(ts),
                          "message": message})
        self.last = max(self.last, ts)

    def user(self, ts, text=PROMPT):
        self._msg(ts, {"role": "user", "content": [{"type": "text", "text": text}]})

    def assistant(self, ts, text="ok"):
        self._msg(ts, {"role": "assistant", "content": [{"type": "text", "text": text}]})

    def spawn(self, ts, job_id, agent="worker", task="TASK: do it"):
        call = f"toolu_{job_id}"
        self._msg(ts, {"role": "assistant", "content": [
            {"type": "toolCall", "id": call, "name": "agent_spawn", "arguments": {"agent": agent, "task": task}}]})
        self._msg(ts + 0.5, {"role": "toolResult", "toolCallId": call, "toolName": "agent_spawn",
                             "content": [{"type": "text", "text": f"Started {job_id} {agent} running"}],
                             "details": {"id": job_id}, "isError": False, "timestamp": int((ts + 0.5) * 1000)})

    def pad(self, ts, nbytes):
        """One big assistant message (fills the middle of the file)."""
        self._msg(ts, {"role": "assistant", "content": [{"type": "text", "text": "x" * nbytes}]})

    def write(self, mtime=None):
        with open(self.path, "w", encoding="utf-8") as f:
            for r in self.rows:
                f.write(json.dumps(r) + "\n")
        m = self.last if mtime is None else mtime
        os.utime(self.path, (m, m))
        return self


def make_job(root: Path, job_id: str, agent="worker", state="done", task="TASK: build the thing\nCONTEXT: x",
             cwd=None, created=None, ended=None, cost=1.25, parent_pid=100, pid=200, result=None, **extra):
    d = root / ".apex-router" / "pi-jobs" / job_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {"id": job_id, "state": state, "agent": agent, "kind": "pi", "task": task, "cwd": str(cwd or root),
            "argv": [], "created": int(created * 1000), "started": int(created * 1000) + 300,
            "timeout_s": 3600, "idle_s": 1200, "session_id": f"apexjob-{job_id}",
            "usage": {"turns": 3, "input": 10, "output": 20, "cost": cost},
            "parent_pid": parent_pid, "owner": f"{parent_pid}-abcd", "pid": pid}
    if ended is not None:
        meta["ended"] = int(ended * 1000)
    meta.update(extra)
    (d / "meta.json").write_text(json.dumps(meta))
    if result is not None:
        (d / "result.md").write_text(result)
    return d


def _row(ts, sid, **kw):
    r = {"ts": ts, "client": "claude-code", "endpoint_id": "anthropic", "schema_version": 13, "session_id": sid,
         "is_error": False, "error_cause": None, "connect_retries": 0, "ttft_ms": 900.0, "total_ms": 4100.0,
         "model_requested": "it-entra-claude-opus-5-5", "tokens_in": 3, "tokens_out": 100,
         "cache_read_tokens": 1000, "cache_write_tokens": 10, "stop_reason": "end_turn",
         "guard_fired": False}
    r.update(kw)
    return r


def _write_rows(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    now = time.time()
    F.make_home(root, now, telemetry=False)
    for k, v in F.env_for(root).items():
        monkeypatch.setenv(k, v)
    (root / ".apex").mkdir(parents=True, exist_ok=True)
    traffic._kinds.clear()
    workflows.clear_caches()
    return root, now


def collect(window="24h", now=None):
    workflows.clear_caches()
    return workflows.collect(flow.resolve_paths(), window, now=now)


def all_sessions(doc):
    return [s for w in doc["workflows"] for s in w["sessions"]]


def session(doc, sid):
    for s in all_sessions(doc):
        if s["id"] == sid:
            return s
    raise AssertionError(f"session {sid} not in the response")


def workflow_of(doc, sid):
    for w in doc["workflows"]:
        if any(s["id"] == sid for s in w["sessions"]):
            return w
    raise AssertionError(f"session {sid} not in the response")


def walk_agents(nodes):
    for a in nodes:
        yield a
        yield from walk_agents(a.get("agents") or [])


SID_A = "01a17a87-5739-73a0-a287-ffedaecd21d2"
SID_B = "01a17b00-0000-7000-8000-000000000001"
SID_C = "01a17c00-0000-7000-8000-000000000002"


# ---- rule 1: tasks and sub-agent placement -------------------------------------------------------

def test_a_gap_over_30_min_starts_a_new_task(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    s = PiSession(root, SID_A, repo, now - 3 * 3600)
    s.user(now - 3 * 3600 + 5)
    s.assistant(now - 3 * 3600 + 600)              # 10 min later: same task
    s.assistant(now - 3 * 3600 + 600 + 25 * 60)    # 25 min gap: same task
    s.user(now - 3600)                             # gap of more than 30 min: task 2
    s.assistant(now - 3600 + 60)
    s.write()
    doc = collect(now=now)
    tasks = session(doc, SID_A)["tasks"]
    assert len(tasks) == 2
    assert tasks[0]["id"] == f"{SID_A}:t1" and tasks[1]["id"] == f"{SID_A}:t2"
    assert tasks[0]["end"] is not None and tasks[0]["end"] < tasks[1]["start"]
    assert tasks[1]["start"] - tasks[0]["end"] > 30 * 60


def test_sub_agents_sit_in_the_task_of_their_spawn_with_state_cost_verdict(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    t1, t2 = now - 4 * 3600, now - 1800
    j1, j2, j3 = _job_name(t1 + 60, "aaaa"), _job_name(t2 + 60, "bbbb"), _job_name(t2 + 120, "cccc")
    s = PiSession(root, SID_A, repo, t1)
    s.user(t1 + 1)
    s.spawn(t1 + 60, j1, "worker", "TASK: first job brief\nmore")
    s.assistant(t1 + 900)
    s.user(t2)
    s.spawn(t2 + 60, j2, "worker", "TASK: second brief")
    s.spawn(t2 + 120, j3, "xval", "TASK: review it")
    s.assistant(now - 30)
    s.write()
    make_job(root, j1, "worker", "killed(cost)", created=t1 + 60, ended=t1 + 800, cost=25.4,
             task="TASK: first job brief\nmore")
    make_job(root, j2, "worker", "done", created=t2 + 60, ended=t2 + 600, cost=3.5, task="TASK: second brief")
    make_job(root, j3, "xval", "done", created=t2 + 120, ended=t2 + 700, cost=0.75, task="TASK: review it",
             result="Findings: none.\n\n**Verdict: NO-GO.**\n")
    doc = collect(now=now)
    tasks = session(doc, SID_A)["tasks"]
    assert len(tasks) == 2
    assert [a["id"] for a in tasks[0]["agents"]] == [j1]
    assert [a["id"] for a in tasks[1]["agents"]] == [j2, j3]
    a1 = tasks[0]["agents"][0]
    assert a1["state"] == "killed(cost)" and a1["cost"] == pytest.approx(25.4) and a1["role"] == "worker"
    assert a1["session_id"] == f"apexjob-{j1}" and a1["verdict"] is None
    assert tasks[0]["state"] == "problem" and "cost limit" in tasks[0]["cause"]
    x = tasks[1]["agents"][1]
    assert x["role"] == "xval" and x["verdict"] == "NO-GO" and x["state"] == "done"
    assert tasks[1]["agents"][0]["verdict"] is None
    assert tasks[1]["label"] == "second brief" and tasks[1]["label_from"] == "brief"


def test_go_verdict_and_state_running(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    j = _job_name(now - 300, "gogo")
    s = PiSession(root, SID_A, repo, now - 600)
    s.user(now - 590)
    s.spawn(now - 300, j, "xval")
    s.write()
    make_job(root, j, "xval", "done", created=now - 300, ended=now - 100, result="ok\n\nVerdict: GO\n")
    doc = collect(now=now)
    a = session(doc, SID_A)["tasks"][0]["agents"][0]
    assert a["verdict"] == "GO"
    assert session(doc, SID_A)["state"] == "running"     # last activity < 5 min


def test_depth_two_children_from_the_sub_agent_session(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    j1, j2 = _job_name(now - 1200, "par1"), _job_name(now - 900, "chi1")
    s = PiSession(root, SID_A, repo, now - 1500)
    s.user(now - 1490)
    s.spawn(now - 1200, j1, "worker")
    s.write()
    sub = PiSession(root, f"apexjob-{j1}", repo, now - 1199)
    sub.user(now - 1198, "TASK: brief")
    sub.spawn(now - 900, j2, "xval")
    sub.write()
    make_job(root, j1, "worker", "running", created=now - 1200, pid=4242)
    make_job(root, j2, "xval", "done", created=now - 900, ended=now - 600, parent_pid=4242, result="Verdict: GO\n")
    doc = collect(now=now)
    a = session(doc, SID_A)["tasks"][0]["agents"]
    assert [x["id"] for x in a] == [j1]
    assert [x["id"] for x in a[0]["agents"]] == [j2]
    assert a[0]["agents"][0]["verdict"] == "GO"
    # the sub-agent session is not a top-level session
    assert f"apexjob-{j1}" not in [x["id"] for x in all_sessions(doc)]


def test_a_spawn_in_the_middle_of_a_big_file_is_linked_by_the_parent_pid(home):
    """Only 64 KiB head and tail are read: a spawn in the middle is found by the job's parent_pid
    (a job of the same pi process that the head or tail shows) and its created time."""
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    ja, jm, jz = _job_name(now - 3000, "head"), _job_name(now - 2000, "midd"), _job_name(now - 100, "tail")
    s = PiSession(root, SID_A, repo, now - 3100)
    s.user(now - 3090)
    s.spawn(now - 3000, ja)
    s.pad(now - 2500, 80 * 1024)
    s.spawn(now - 2000, jm)
    s.pad(now - 1500, 80 * 1024)
    s.spawn(now - 100, jz)
    s.write()
    for j, c in ((ja, now - 3000), (jm, now - 2000), (jz, now - 100)):
        make_job(root, j, created=c, ended=c + 60, parent_pid=777)
    make_job(root, _job_name(now - 2000, "othr"), created=now - 2000, ended=now - 1900, parent_pid=999)
    doc = collect(now=now)
    ids = [a["id"] for t in session(doc, SID_A)["tasks"] for a in t["agents"]]
    assert sorted(ids) == sorted([ja, jm, jz])
    by = {a["id"]: a for t in session(doc, SID_A)["tasks"] for a in t["agents"]}
    assert by[ja]["linked_by"] == "spawn" and by[jm]["linked_by"] == "pid"
    assert doc["skipped"]["jobs_unlinked"] == 1


def test_at_most_12_agents_per_task_plus_more(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    s = PiSession(root, SID_A, repo, now - 3000)
    s.user(now - 2990)
    for i in range(15):
        j = _job_name(now - 2900 + i * 60, f"n{i:03d}")
        s.spawn(now - 2900 + i * 60, j)
        make_job(root, j, created=now - 2900 + i * 60, ended=now - 2800 + i * 60)
    s.write()
    t = session(collect(now=now), SID_A)["tasks"][0]
    assert len(t["agents"]) == 12 and t["agents_more"] == 3


# ---- rule 2: label privacy ----------------------------------------------------------------------

def test_label_never_holds_prompt_text_and_masks_secrets(home):
    root, now = home
    repo = make_repo(root, "apex-router", "feat-x")
    j = _job_name(now - 500, "sec1")
    s = PiSession(root, SID_A, repo, now - 900)
    s.user(now - 899, f"{PROMPT} key {F.FAKE_SECRET}")
    s.spawn(now - 500, j, task=f"TASK: rotate {F.FAKE_SECRET} and {SECRET2} for the proxy\n{PROMPT}")
    s.write()
    make_job(root, j, created=now - 500, ended=now - 300, task=f"TASK: rotate {F.FAKE_SECRET} and {SECRET2} "
             f"for the proxy\n{PROMPT}")
    # a second session: no brief -> repo · branch; the first user message is the prompt
    s2 = PiSession(root, SID_B, repo, now - 800)
    s2.user(now - 799, f"TASK: {PROMPT} {F.FAKE_SECRET}")
    s2.write()
    _write_rows(root / ".apex" / "telemetry.jsonl", [_row(now - 400, SID_A), _row(now - 790, SID_B)])
    doc = collect(now=now)
    t = session(doc, SID_A)["tasks"][0]
    assert t["label_from"] == "brief"
    assert t["label"].startswith("rotate ") and len(t["label"]) <= 60
    t2 = session(doc, SID_B)["tasks"][0]
    assert t2["label_from"] == "repo" and t2["label"] == "apex-router · feat-x"
    text = json.dumps(doc)
    for bad in (PROMPT, F.FAKE_SECRET, SECRET2, "sk-fixture", "ghp_"):
        assert bad not in text


def test_a_brief_without_task_prefix_is_not_a_label(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    j = _job_name(now - 500, "nopf")
    s = PiSession(root, SID_A, repo, now - 900)
    s.user(now - 899)
    s.spawn(now - 500, j, task=PROMPT)
    s.write()
    make_job(root, j, created=now - 500, ended=now - 300, task=PROMPT)
    doc = collect(now=now)
    t = session(doc, SID_A)["tasks"][0]
    assert t["label_from"] == "repo" and t["label"] == "apex-router · main"
    assert PROMPT not in json.dumps(doc)


# ---- rule 3: auto join --------------------------------------------------------------------------

def test_same_repo_and_overlap_join(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    a = PiSession(root, SID_A, repo, now - 3600)
    a.user(now - 3590); a.assistant(now - 600); a.write()
    b = PiSession(root, SID_B, repo, now - 1800)
    b.user(now - 1790); b.assistant(now - 300); b.write()
    doc = collect(now=now)
    w = workflow_of(doc, SID_A)
    assert {s["id"] for s in w["sessions"]} == {SID_A, SID_B}
    assert w["joined_by"] == ["auto:repo-overlap"]
    assert w["repo"] == "apex-router" and w["branches"] == ["main"]


def test_same_repo_without_overlap_and_main_branch_stay_apart(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    a = PiSession(root, SID_A, repo, now - 5 * 3600)
    a.user(now - 5 * 3600 + 1); a.assistant(now - 4 * 3600); a.write()
    b = PiSession(root, SID_B, repo, now - 1800)
    b.user(now - 1790); b.assistant(now - 300); b.write()
    doc = collect(now=now)
    assert workflow_of(doc, SID_A)["id"] != workflow_of(doc, SID_B)["id"]


def test_same_feature_branch_joins_across_repos_and_time(home):
    root, now = home
    r1 = make_repo(root, "avenger", "juri/EYE-1")
    r2 = make_repo(root, "thor", "juri/EYE-1")
    a = PiSession(root, SID_A, r1, now - 5 * 3600)
    a.user(now - 5 * 3600 + 1); a.assistant(now - 4 * 3600); a.write()
    b = PiSession(root, SID_B, r2, now - 1800)
    b.user(now - 1790); b.write()
    w = workflow_of(collect(now=now), SID_A)
    assert {s["id"] for s in w["sessions"]} == {SID_A, SID_B}
    assert w["joined_by"] == ["auto:branch"]


# ---- rule 4: manual join and split (store) --------------------------------------------------------

def test_manual_join_and_split_in_the_store(home):
    root, now = home
    r1 = make_repo(root, "one", "main")
    r2 = make_repo(root, "two", "main")
    a = PiSession(root, SID_A, r1, now - 3600); a.user(now - 3590); a.assistant(now - 60); a.write()
    b = PiSession(root, SID_B, r2, now - 1800); b.user(now - 1790); b.write()
    c = PiSession(root, SID_C, r1, now - 3000); c.user(now - 2990); c.assistant(now - 100); c.write()
    doc = collect(now=now)
    assert workflow_of(doc, SID_A)["id"] == workflow_of(doc, SID_C)["id"]      # auto
    assert workflow_of(doc, SID_A)["id"] != workflow_of(doc, SID_B)["id"]
    store = workflows.Store(flow.resolve_paths().router_home / "workflows.jsonl")
    store.join([SID_A, SID_B], now=now - 50)
    doc = collect(now=now)
    w = workflow_of(doc, SID_A)
    assert {s["id"] for s in w["sessions"]} == {SID_A, SID_B, SID_C}
    assert "manual" in w["joined_by"]
    assert doc["manual"] == {"joins": 1, "splits": 0}
    store.split(SID_C, now=now - 40)            # out of the auto join too
    doc = collect(now=now)
    assert {s["id"] for s in workflow_of(doc, SID_C)["sessions"]} == {SID_C}
    assert {s["id"] for s in workflow_of(doc, SID_A)["sessions"]} == {SID_A, SID_B}
    assert doc["manual"] == {"joins": 1, "splits": 1}
    store.join([SID_C, SID_A], now=now - 30)    # the latest row per session wins
    doc = collect(now=now)
    assert {s["id"] for s in workflow_of(doc, SID_C)["sessions"]} == {SID_A, SID_B, SID_C}


def test_store_rows_hold_ids_and_timestamps_only_under_a_flock(home, monkeypatch):
    root, now = home
    path = flow.resolve_paths().router_home / "workflows.jsonl"
    calls = []
    real = workflows.fcntl.flock
    monkeypatch.setattr(workflows.fcntl, "flock", lambda fd, op: (calls.append(op), real(fd, op)))
    store = workflows.Store(path)
    store.join([SID_A, SID_B], now=now)
    store.split(SID_A, now=now + 1)
    assert workflows.fcntl.LOCK_EX in calls
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(rows) == 2
    for r in rows:
        assert set(r) <= {"ts", "op", "sessions"}
        assert r["op"] in ("join", "split")
    assert (path.stat().st_mode & 0o777) == 0o600
    for bad in ([SID_A], [SID_A, "x" * 300], [SID_A, "bad id!"], [SID_A, F.FAKE_SECRET + " x"], "notalist",
                [SID_A] * 2, [f"s{i}" for i in range(30)]):
        with pytest.raises(workflows.WorkflowError):
            store.join(bad, now=now)
    with pytest.raises(workflows.WorkflowError):
        store.split("bad id!", now=now)
    assert len(path.read_text().splitlines()) == 2


def test_a_bad_store_line_is_skipped(home):
    root, now = home
    path = flow.resolve_paths().router_home / "workflows.jsonl"
    path.write_text("not json\n" + json.dumps({"ts": now, "op": "join", "sessions": [SID_A, SID_B]}) + "\n"
                    + json.dumps({"ts": now, "op": "join", "sessions": "x"}) + "\n")
    st = workflows.Store(path).state()
    assert st["joins"] == 1


# ---- bounds and bad files -------------------------------------------------------------------------

def test_only_64k_head_and_tail_of_a_big_session_file_is_read(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    s = PiSession(root, SID_A, repo, now - 3000)
    s.user(now - 2990)
    for i in range(40):
        s.pad(now - 2900 + i * 10, 100 * 1024)          # 4 MB
    s.write()
    doc = collect(now=now)
    assert doc["counts"]["sessions"] == 1
    assert doc["counts"]["bytes_read"] <= 2 * workflows.HEAD_TAIL + 64 * 1024


def test_newest_200_sessions_and_2000_job_dirs(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    for i in range(workflows.SESSIONS_MAX + 7):
        sid = f"01a17b00-0000-7000-8000-{i:012d}"
        s = PiSession(root, sid, repo, now - 7200 + i)
        s.user(now - 7200 + i)
        s.write()
    jobs = root / ".apex-router" / "pi-jobs"
    for i in range(workflows.JOBS_MAX + 5):
        (jobs / _job_name(now - 20000 + i, f"z{i:03d}"[-4:])).mkdir(parents=True, exist_ok=True)
    doc = collect(now=now)
    assert len(all_sessions(doc)) == workflows.SESSIONS_MAX
    assert doc["skipped"]["sessions"] == 7
    assert doc["counts"]["job_dirs"] == workflows.JOBS_MAX
    assert doc["skipped"]["jobs"] >= 5
    newest = {f"01a17b00-0000-7000-8000-{i:012d}" for i in range(7, workflows.SESSIONS_MAX + 7)}
    assert {s["id"] for s in all_sessions(doc)} == newest


def test_bad_and_huge_files_are_skipped_and_counted_never_fatal(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    s = PiSession(root, SID_A, repo, now - 900)
    s.user(now - 899)
    jb, jh, jok = _job_name(now - 800, "badm"), _job_name(now - 700, "huge"), _job_name(now - 600, "good")
    s.spawn(now - 800, jb); s.spawn(now - 700, jh); s.spawn(now - 600, jok)
    s.write()
    make_job(root, jok, created=now - 600, ended=now - 500)
    (root / ".apex-router" / "pi-jobs" / jb).mkdir(parents=True)
    (root / ".apex-router" / "pi-jobs" / jb / "meta.json").write_text("{not json")
    (root / ".apex-router" / "pi-jobs" / jh).mkdir(parents=True)
    (root / ".apex-router" / "pi-jobs" / jh / "meta.json").write_text(
        json.dumps({"id": jh, "pad": "x" * (workflows.META_MAX + 10)}))
    sess = root / ".pi" / "agent" / "sessions" / "--bad--"
    sess.mkdir(parents=True)
    (sess / f"{_fname_ts(now - 100)}_01a17d00-0000-7000-8000-000000000009.jsonl").write_bytes(b"\xff\xfe\x00garbage")
    os.mkfifo(sess / f"{_fname_ts(now - 90)}_01a17d00-0000-7000-8000-00000000000a.jsonl")
    (sess / f"{_fname_ts(now - 80)}_01a17d00-0000-7000-8000-00000000000b.jsonl").mkdir()
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("doc", collect(now=now)), daemon=True)
    th.start()
    th.join(10)
    assert not th.is_alive(), "a FIFO or a bad file made the read hang"
    doc = out["doc"]
    ids = [a["id"] for a in session(doc, SID_A)["tasks"][0]["agents"]]
    assert ids == [jok]
    assert doc["skipped"]["jobs_bad"] == 2
    assert doc["skipped"]["files"] >= 2
    assert doc["complete"] is False


def test_an_empty_home_gives_no_workflows(home):
    doc = collect()
    assert doc["workflows"] == [] and doc["window"] == "24h" and doc["manual"] == {"joins": 0, "splits": 0}


def test_window_7d_includes_older_sessions(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    s = PiSession(root, SID_A, repo, now - 3 * 86400)
    s.user(now - 3 * 86400 + 5)
    s.write()
    assert all_sessions(collect("24h", now=now)) == []
    assert [x["id"] for x in all_sessions(collect("7d", now=now))] == [SID_A]
    with pytest.raises(workflows.WorkflowError):
        collect("1h", now=now)


# ---- telemetry: requests, tokens, decisions -----------------------------------------------------

def test_requests_tokens_and_decisions_last_join_by_session_id(home):
    root, now = home
    repo = make_repo(root, "apex-router", "main")
    j = _job_name(now - 600, "tele")
    s = PiSession(root, SID_A, repo, now - 900)
    s.user(now - 899)
    s.spawn(now - 600, j)
    s.write()
    make_job(root, j, "worker", "running", created=now - 600)
    _write_rows(root / ".apex" / "telemetry.jsonl",
                [_row(now - 850, SID_A), _row(now - 800, SID_A),
                 _row(now - 500, f"apexjob-{j}"), _row(now - 400, f"apexjob-{j}", stop_reason="max_tokens"),
                 _row(now - 300, "someone-else")])
    doc = collect(now=now)
    t = session(doc, SID_A)["tasks"][0]
    assert t["requests"] == 2
    assert t["tokens"] == {"in": 6, "out": 200, "cache_read": 2000, "cache_write": 20}
    a = t["agents"][0]
    assert a["requests"] == 2
    assert [d["step"] for d in a["decisions_last"]] == ["task", "model", "guard", "cache", "send", "answer"]
    assert a["decisions_last"][-1]["verdict"].startswith("stopped: length")


# ---- W4 second: Claude Code -----------------------------------------------------------------------

def test_claude_code_session_task_agents(home):
    root, now = home
    sid = "b60396b0-3f54-4128-b7b4-383317d4e6ce"
    proj = root / ".claude" / "projects" / "-Users-x-dev-avenger"
    proj.mkdir(parents=True)
    base = {"cwd": "/nonexistent/dev/avenger", "sessionId": sid, "gitBranch": "juri/EYE-9", "isSidechain": False}
    main = [dict(base, type="user", timestamp=_iso(now - 1000), message={"role": "user", "content": PROMPT}),
            dict(base, type="assistant", timestamp=_iso(now - 200), message={"role": "assistant", "content": "x"})]
    (proj / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in main))
    sub = proj / sid / "subagents"
    sub.mkdir(parents=True)
    rows = [dict(base, isSidechain=True, agentId="a6c66d8c6123475ad", type="user", timestamp=_iso(now - 900),
                 message={"role": "user", "content": f"Review target {PROMPT}"}),
            dict(base, isSidechain=True, agentId="a6c66d8c6123475ad", type="assistant", timestamp=_iso(now - 700),
                 message={"role": "assistant", "content": "done"})]
    (sub / "agent-a6c66d8c6123475ad.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (sub / "agent-a6c66d8c6123475ad.meta.json").write_text(json.dumps({"agentType": "general-purpose",
                                                                        "description": PROMPT}))
    os.utime(sub / "agent-a6c66d8c6123475ad.jsonl", (now - 700, now - 700))
    os.utime(proj / f"{sid}.jsonl", (now - 200, now - 200))
    doc = collect(now=now)
    s = session(doc, sid)
    assert s["agent"] == "claude"
    t = s["tasks"][0]
    assert [a["id"] for a in t["agents"]] == ["a6c66d8c6123475ad"]
    a = t["agents"][0]
    assert a["role"] == "worker" and a["state"] == "done" and a["cost"] is None and a["agents"] == []
    assert t["label"] == "avenger · juri/EYE-9"
    assert PROMPT not in json.dumps(doc)


# ---- HTTP: routes, token, Origin, restart --------------------------------------------------------

def _start(paths):
    srv = server.make_server(port=0, token=TOKEN, paths=paths)
    srv.stats_fetch = lambda url: None
    srv.restart_fetch = lambda url: None
    srv.stream_poll_s, srv.stream_snap_s, srv.stream_hb_s, srv.stream_wf_s = 0.05, 0.3, 0.4, 0.1
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _stop(srv):
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def studio(home):
    root, now = home
    paths = flow.resolve_paths()
    srv = _start(paths)
    yield srv, paths, root, now
    _stop(srv)


def req(srv, method, path, body=None, token=TOKEN, origin=None, ctype="application/json", query_token=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    h = {"Host": f"127.0.0.1:{srv.port}"}
    if token:
        h["X-Studio-Token"] = token
    if origin:
        h["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = ctype
        h["Content-Length"] = str(len(data))
    if query_token:
        path += ("&" if "?" in path else "?") + "token=" + query_token
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    txt = r.read().decode()
    c.close()
    try:
        return r.status, json.loads(txt)
    except ValueError:
        return r.status, txt


def test_get_workflows_needs_the_header_token(studio):
    srv, _, _, _ = studio
    assert req(srv, "GET", "/api/workflows")[0] == 200
    assert req(srv, "GET", "/api/workflows", token=None)[0] == 403
    assert req(srv, "GET", "/api/workflows", token=None, query_token=TOKEN)[0] == 403
    assert req(srv, "GET", "/api/workflows", origin="http://evil.example")[0] == 403
    assert req(srv, "GET", "/api/workflows?window=7d")[0] == 200
    assert req(srv, "GET", "/api/workflows?window=1h")[0] == 400


@pytest.mark.parametrize("path,body", [("/api/workflows/join", {"sessions": [SID_A, SID_B]}),
                                       ("/api/workflows/split", {"session": SID_A})])
def test_post_routes_need_token_origin_and_json(studio, path, body):
    srv, paths, _, _ = studio
    store = paths.router_home / "workflows.jsonl"
    assert req(srv, "POST", path, body, token=None)[0] == 403
    assert req(srv, "POST", path, body, token=None, query_token=TOKEN)[0] == 403
    assert req(srv, "POST", path, body, origin="http://evil.example")[0] == 403
    assert req(srv, "POST", path, body, ctype="text/plain")[0] == 415
    assert req(srv, "GET", path)[0] == 405
    assert not store.exists()
    st, d = req(srv, "POST", path, body, origin=f"http://127.0.0.1:{srv.port}")
    assert st == 200 and d["ok"] is True
    assert len(store.read_text().splitlines()) == 1


def test_post_rejects_bad_ids(studio):
    srv, paths, _, _ = studio
    for body in ({"sessions": [SID_A]}, {"sessions": [SID_A, "bad id"]}, {"sessions": "x"}, [1, 2], {}):
        assert req(srv, "POST", "/api/workflows/join", body)[0] == 400
    assert req(srv, "POST", "/api/workflows/split", {"session": "../../etc"})[0] == 400
    assert not (paths.router_home / "workflows.jsonl").exists()


def test_manual_join_survives_a_studio_restart(studio):
    srv, paths, root, now = studio
    r1, r2 = make_repo(root, "one", "main"), make_repo(root, "two", "main")
    a = PiSession(root, SID_A, r1, now - 600); a.user(now - 590); a.write()
    b = PiSession(root, SID_B, r2, now - 500); b.user(now - 490); b.write()
    st, d = req(srv, "GET", "/api/workflows")
    assert workflow_of(d, SID_A)["id"] != workflow_of(d, SID_B)["id"]
    assert req(srv, "POST", "/api/workflows/join", {"sessions": [SID_A, SID_B]})[0] == 200
    _stop(srv)
    workflows.clear_caches()
    srv2 = _start(paths)
    try:
        st, d = req(srv2, "GET", "/api/workflows")
        assert st == 200
        w = workflow_of(d, SID_A)
        assert {s["id"] for s in w["sessions"]} == {SID_A, SID_B} and "manual" in w["joined_by"]
        assert d["manual"]["joins"] == 1
        assert req(srv2, "POST", "/api/workflows/split", {"session": SID_B})[0] == 200
        st, d = req(srv2, "GET", "/api/workflows")
        assert workflow_of(d, SID_A)["id"] != workflow_of(d, SID_B)["id"]
    finally:
        _stop(srv2)


def test_existing_routes_keep_their_shapes(studio):
    srv, _, _, _ = studio
    st, d = req(srv, "GET", "/api/recent?n=5")
    assert st == 200 and set(d) == {"requests"}
    st, d = req(srv, "GET", "/api/tokens?window=15m")
    assert st == 200 and set(d) == {"window", "total", "by_agent", "by_provider", "by_model", "cache_share"}
    assert req(srv, "GET", "/api/ping")[0] == 200


# ---- SSE: workflow events ------------------------------------------------------------------------

def _read_events(srv, seconds):
    s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
    s.sendall((f"GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:{srv.port}\r\nX-Studio-Token: {TOKEN}\r\n\r\n").encode())
    buf = b""
    end = time.time() + seconds
    s.settimeout(0.2)
    while time.time() < end:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk
    s.close()
    out = []
    for block in buf.decode(errors="replace").split("\n\n"):
        ev = data = None
        for ln in block.splitlines():
            if ln.startswith("event: "):
                ev = ln[7:]
            elif ln.startswith("data: "):
                data = json.loads(ln[6:])
        if ev:
            out.append((ev, data))
    return out


def test_sse_sends_a_workflow_event_for_the_changed_node_only(studio):
    srv, paths, root, now = studio
    repo = make_repo(root, "apex-router", "main")
    j, k = _job_name(now - 600, "sse1"), _job_name(now - 500, "sse2")
    s = PiSession(root, SID_A, repo, now - 900)
    s.user(now - 899)
    s.spawn(now - 600, j)
    s.spawn(now - 500, k)
    s.write()
    make_job(root, j, "worker", "running", created=now - 600)
    make_job(root, k, "worker", "done", created=now - 500, ended=now - 400)
    workflows.clear_caches()
    got = {}

    def reader():
        got["ev"] = _read_events(srv, 2.5)
    th = threading.Thread(target=reader)
    th.start()
    time.sleep(1.0)
    make_job(root, j, "worker", "done", created=now - 600, ended=time.time(), cost=4.5)
    workflows.clear_caches()
    th.join(10)
    wf = [d for e, d in got["ev"] if e == "workflow"]
    agent_ev = [d for d in wf if d["kind"] == "agent" and d["node"]["id"] == j]
    assert agent_ev, got["ev"]
    assert agent_ev[-1]["node"]["state"] == "done" and agent_ev[-1]["node"]["cost"] == 4.5
    assert agent_ev[-1]["session"] == SID_A and agent_ev[-1]["task"] == f"{SID_A}:t1"
    assert not [d for d in wf if d["kind"] == "agent" and d["node"]["id"] == k]
    text = json.dumps(got["ev"])
    assert PROMPT not in text


def test_workflow_events_are_rate_limited_per_node():
    st = workflows.EventState()
    doc = lambda cost: {"workflows": [{"id": "wf-1", "sessions": [{"id": "s", "tasks": [  # noqa: E731
        {"id": "s:t1", "label": "x", "agents": [{"id": "j1", "cost": cost, "agents": []}]}]}]}]}
    assert workflows.node_events(st, doc(1.0), 100.0) == []            # baseline at connect
    ev = workflows.node_events(st, doc(2.0), 100.5)
    assert [(e["kind"], e["node"]["id"]) for e in ev] == [("agent", "j1")]
    assert workflows.node_events(st, doc(3.0), 100.9) == []            # < 1 s since the last event
    ev = workflows.node_events(st, doc(3.0), 101.6)                    # held change goes out
    assert [e["node"]["cost"] for e in ev] == [3.0]
    assert workflows.node_events(st, doc(3.0), 103.0) == []
