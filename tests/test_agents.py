"""Tests for apex_router.agents — read-only, mtime-based agent discovery."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from apex_router import agents

NOW = 1_790_000_000.0


def _touch(p: Path, age_s: float, text: str = "{}\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    os.utime(p, (NOW - age_s, NOW - age_s))
    return p


# ---------------------------------------------------------------- Claude Code

def test_claude_active_idle_and_old(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "-Users-you-src-myrepo"
    _touch(proj / "aaaaaaaa-1111-4111-8111-111111111111.jsonl", 30)
    _touch(proj / "bbbbbbbb-2222-4222-8222-222222222222.jsonl", 20 * 60)
    _touch(proj / "cccccccc-3333-4333-8333-333333333333.jsonl", 2 * 3600)
    found = {a["session"]: a for a in agents.claude_agents(tmp_path, NOW)}
    assert set(found) == {"aaaaaaaa", "bbbbbbbb"}
    assert found["aaaaaaaa"]["state"] == "active"
    assert found["bbbbbbbb"]["state"] == "idle"
    assert found["aaaaaaaa"]["kind"] == "claude"
    assert found["aaaaaaaa"]["repo"] == "myrepo"


def test_claude_subagents_counted_only_when_recent(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "-Users-you-src-myrepo"
    sid = "aaaaaaaa-1111-4111-8111-111111111111"
    _touch(proj / f"{sid}.jsonl", 10)
    _touch(proj / sid / "subagents" / "agent-1.jsonl", 60)
    _touch(proj / sid / "subagents" / "agent-2.jsonl", 100)
    _touch(proj / sid / "subagents" / "agent-3.jsonl", 30 * 60)     # finished long ago
    [a] = agents.claude_agents(tmp_path, NOW)
    assert a["subagents"] == 2


def test_claude_top_level_only(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "-Users-you-src-myrepo"
    _touch(proj / "s1" / "subagents" / "agent-1.jsonl", 10)
    assert agents.claude_agents(tmp_path, NOW) == []


def test_claude_missing_home_is_empty(tmp_path):
    assert agents.claude_agents(tmp_path / "nope", NOW) == []


def test_decode_slug_resolves_on_disk(tmp_path, monkeypatch):
    real = tmp_path / "work" / "my-repo"
    real.mkdir(parents=True)
    (tmp_path / "work" / "my").mkdir()                         # shorter decoy prefix
    slug = agents._slugify(str(real))
    assert agents.decode_slug(slug) == "my-repo"
    assert agents.decode_slug(slug + "--claude-worktrees-agent-x") == "my-repo/agent-x"


def test_decode_slug_fallback_strips_home(tmp_path):
    home = Path("/Users/you")
    assert agents.decode_slug("-Users-you-src-not-on-disk-zz", home) == "not-on-disk-zz"


# ---------------------------------------------------------------- pi

def test_pi_reads_cwd_from_first_line(tmp_path):
    d = tmp_path / ".pi" / "agent" / "sessions" / "--x--"
    uid = "01a1141d-aaaa-7bbb-8ccc-0123456789ab"
    first = json.dumps({"type": "session", "cwd": "/work/proj-a"})
    _touch(d / f"2026-10-06T10-00-00-000Z_{uid}.jsonl", 60,
           first + "\n" + json.dumps({"secret": "prompt"}) + "\n")
    _touch(d / "2026-10-06T09-00-00-000Z_other.jsonl", 10, "not json\n")
    _touch(d / "old.jsonl", 3 * 3600)
    found = sorted(agents.pi_agents(tmp_path, NOW), key=lambda a: a["age_s"])
    assert [a["repo"] for a in found] == [None, "proj-a"]
    assert found[1]["session"] == "456789ab"       # UUIDv7: tail, not the shared time prefix
    assert all(a["kind"] == "pi" for a in found)


def test_short_id():
    assert agents.short_id("d5443384-0a68-4edf-82e8-1d130c562c8e") == "d5443384"
    assert agents.short_id("01a1141d-aaaa-7bbb-8ccc-0123456789ab") == "456789ab"


# ---------------------------------------------------------------- Codex

def test_codex_today_and_yesterday_only(tmp_path):
    import datetime as dt
    root = tmp_path / ".codex" / "sessions"
    today = dt.datetime.fromtimestamp(NOW)
    for days, age in ((0, 30), (1, 40 * 60)):
        d = today - dt.timedelta(days=days)
        _touch(root / f"{d:%Y/%m/%d}" / f"rollout-x-{days}.jsonl", age)
    old = today - dt.timedelta(days=5)
    _touch(root / f"{old:%Y/%m/%d}" / "rollout-old.jsonl", 10)        # fresh mtime, old dir
    found = agents.codex_agents(tmp_path, NOW)
    assert sorted(a["state"] for a in found) == ["active", "idle"]


def test_discover_sorted_and_fail_open(tmp_path, monkeypatch):
    proj = tmp_path / ".claude" / "projects" / "-Users-you-src-r"
    _touch(proj / "s-old.jsonl", 600)
    _touch(proj / "s-new.jsonl", 5)

    def boom(home, now):
        raise RuntimeError("bad")
    monkeypatch.setattr(agents, "pi_agents", boom)
    out = agents.discover(tmp_path, NOW)
    assert [a.get("session") for a in out[:2]] == ["s-new", "s-old"]
    assert any(a.get("error") for a in out)


# ---------------------------------------------------------------- worker / proxy

def test_worker_counts_queue(tmp_path, monkeypatch):
    jobs = tmp_path / "q" / "jobs"
    for name in ("a.json", "b.json", ".hidden"):
        _touch(jobs / "inbox" / name, 1)
    (jobs / "running").mkdir(parents=True)
    w = agents.worker(label="x.test", queue=tmp_path / "q", pid_fn=lambda label: (4242, None))
    assert w == {"label": "x.test", "pid": 4242, "running": True, "inbox": 2, "queue_running": 0}


def test_worker_label_from_env_and_queue_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_WORKER_LABEL", "com.example.worker")
    monkeypatch.setenv("APEX_ORNITH_QUEUE", str(tmp_path / "qq"))
    seen = []
    w = agents.worker(pid_fn=lambda label: (seen.append(label), (None, "not loaded"))[1])
    assert seen == ["com.example.worker"]
    assert w["running"] is False and w["error"] == "not loaded"
    assert w["inbox"] is None                                  # no queue dir: unknown, not 0


def test_launchd_pid_parses_and_fails_open(monkeypatch):
    out = '{\n\t"Label" = "x";\n\t"PID" = 61698;\n};\n'
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    assert agents._launchd_pid("x") == (61698, None)

    def missing(*a, **k):
        raise FileNotFoundError("launchctl")
    monkeypatch.setattr(subprocess, "run", missing)
    pid, err = agents._launchd_pid("x")
    assert pid is None and "FileNotFoundError" in err


def test_proxy_down_is_a_state():
    p = agents.proxy("http://127.0.0.1:9/healthz", timeout=0.2)
    assert p["up"] is False and "error" in p


def test_proxy_port_env(monkeypatch):
    monkeypatch.setenv("APEX_PORT", "9999")
    assert agents.proxy_port() == 9999
    monkeypatch.setenv("APEX_PORT", "junk")
    assert agents.proxy_port() == agents.DEFAULT_PROXY_PORT


def test_codex_cwd_from_session_meta_and_full_session_id(tmp_path):
    import datetime as dt
    d = dt.datetime.fromtimestamp(NOW)
    day = tmp_path / ".codex" / "sessions" / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
    sid = "01a1141d-aaaa-7bbb-8ccc-0123456789ab"
    meta = {"type": "session_meta", "payload": {"base_instructions": "x" * 100_000,
                                                "cwd": "/Users/you/src/proj-c"}}
    _touch(day / f"rollout-2026-10-06T00-00-00-{sid}.jsonl", 10, json.dumps(meta) + "\n{}\n")
    (a,) = agents.codex_agents(tmp_path, NOW)
    assert a["cwd"] == "/Users/you/src/proj-c" and a["repo"] == "proj-c"
    assert a["session_id"] == sid and a["session"] == "456789ab"


def test_pi_agent_carries_cwd(tmp_path):
    _touch(tmp_path / ".pi" / "agent" / "sessions" / "--x--" / "2026_abc.jsonl", 10,
           json.dumps({"cwd": "/Users/you/src/p"}) + "\n")
    (a,) = agents.pi_agents(tmp_path, NOW)
    assert a["cwd"] == "/Users/you/src/p" and a["session_id"] == "abc"


def test_claude_session_waiting_on_subagent_is_active(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "-Users-you-src-myrepo"
    sid = "aaaaaaaa-1111-4111-8111-111111111111"
    _touch(proj / f"{sid}.jsonl", 20 * 60)                     # parent quiet for 20 min
    _touch(proj / sid / "subagents" / "agent-x.jsonl", 40)     # its subagent is working
    (a,) = agents.claude_agents(tmp_path, NOW)
    assert a["state"] == "active" and a["age_s"] == 40.0 and a["subagents"] == 1
