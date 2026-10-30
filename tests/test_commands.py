"""Commands backend (contract: docs/design/studio-v2/API.md, section "Commands", owner decision D19):
``commands.mask_short`` / ``mask_secrets`` (rule 2), the pi and Claude Code readers (rule 1), call ids
(rule 3), ``procs`` (rule 4), the bounds (rule 5), ``running_now`` and the menu bar row (Q3).
Every test uses a fixture home in tmp_path; nothing reads the real ~/.pi, ~/.claude or ~/.apex-router."""
from __future__ import annotations

import datetime
import json
import os
import re
import time
from pathlib import Path

import pytest

from apex_router import commands

KEY = b"k" * 32
CALL_RE = re.compile(r"c-[0-9a-f]{16}")
SECRETS = {
    "name_key": ("export OPENAI_API_KEY=abcdef123456 && run", "abcdef123456"),
    "name_token": ("GH_TOKEN=tok987654 gh pr list", "tok987654"),
    "name_secret": ("CLIENT_SECRET='s3cr3t-value' ./x", "s3cr3t-value"),
    "name_password": ("DB_PASSWORD=hunter2hunter2 psql", "hunter2hunter2"),
    "name_auth": ("GIT_AUTH=xyzw9876 git fetch", "xyzw9876"),
    "bearer": ("curl -H 'Authorization: Bearer abc.def.ghi' https://x", "abc.def.ghi"),
    "sk": ("echo sk-proj-AbCdEf0123456789", "sk-proj-AbCdEf0123456789"),
    "ghp": ("echo ghp_AbCdEf0123456789AbCdEf", "ghp_AbCdEf0123456789AbCdEf"),
    "xox": ("echo xoxb-1234-5678-abcdef", "xoxb-1234-5678-abcdef"),
    "akia": ("aws s3 ls # AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    "jwt": ("echo eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOjF9.x", "eyJhbGciOiJIUzI1NiJ9"),
    "hex32": ("echo 0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
    "b64_32": ("echo QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3", "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3"),
}


# ---- rule 2: mask_short ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,args,want", [
    ("read", {"path": "/Users/x/dev/repo/src/apex_router/commands.py"}, "read commands.py"),
    ("edit", {"path": "src/a/b.py", "oldText": "SECRET-OLD", "newText": "x"}, "edit b.py"),
    ("write", {"path": "/tmp/out.txt", "content": "PRIVATE BODY"}, "write out.txt"),
    ("Read", {"file_path": "/Users/x/notes.md"}, "Read notes.md"),
    ("Edit", {"file_path": "/a/b/c.py", "old_string": "x", "new_string": "y"}, "Edit c.py"),
    ("Write", {"file_path": "/a/b/d.py", "content": "z"}, "Write d.py"),
])
def test_file_tools_show_the_tool_and_the_file_name_only(tool, args, want):
    assert commands.mask_short(tool, args) == (want, False)


def test_bash_shows_the_first_command_words():
    assert commands.mask_short("bash", {"command": "pytest tests/test_menubar_problems.py -q"}) == \
        ("pytest tests/test_menubar_problems.py -q", False)
    assert commands.mask_short("Bash", {"command": "git status"}) == ("git status", False)


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_each_secret_form_becomes_hidden(name):
    cmd, secret = SECRETS[name]
    short, masked = commands.mask_short("bash", {"command": cmd})
    # allow-list (round 4): the secret goes as [hidden] or with the cut (<text>) of a non-plain word
    assert secret not in short and ("[hidden]" in short or short.endswith("<text>")) and masked is True
    text = commands.mask_secrets(cmd)[0]
    assert secret not in text and "[hidden]" in text


def test_name_value_keeps_the_name():
    short, _ = commands.mask_short("bash", {"command": "export OPENAI_API_KEY=abc123xyz"})
    assert short == "export OPENAI_API_KEY=[hidden]"


def test_heredoc_body_becomes_script():
    cmd = "cat > /tmp/x.py <<'EOF'\nimport os\nprint(os.environ['X'])\nEOF\npytest -q"
    short, masked = commands.mask_short("bash", {"command": cmd})
    assert "<script>" in short and "import os" not in short and "environ" not in short
    # xval 45zb: only the first line shows (fail closed), so "pytest -q" after the heredoc is hidden too
    assert "pytest -q" not in short and "\n" not in short and masked is True
    short2, _ = commands.mask_short("bash", {"command": "python3 - <<EOF\nprint(1)\nEOF"})
    assert "<script>" in short2 and "print(1)" not in short2
    # no terminator: the rest is the body
    short3, _ = commands.mask_short("bash", {"command": "cat <<END\nline one\nline two"})
    assert "<script>" in short3 and "line" not in short3
    # a here-string is not a heredoc
    assert commands.mask_short("bash", {"command": "cat <<< hi"}) == ("cat <<< hi", False)


@pytest.mark.parametrize("cmd,gone", [
    ("python3 -c 'import sys; print(sys.argv)'", "import sys"),
    ('node -e "console.log(1)"', "console.log"),
    ("bash -lc 'echo hi && ls'", "echo hi"),
    ("perl -e print", "print"),
    ("ruby -e 'puts 1'", "puts"),
])
def test_dash_c_and_dash_e_scripts_become_script(cmd, gone):
    short, masked = commands.mask_short("bash", {"command": cmd})
    assert "<script>" in short and gone not in short and masked is True


def test_quoted_text_becomes_text():
    # round 4 allow-list: every quoted word is cut (stricter than the old 40-character rule)
    q40 = "a" * 20 + " " + "b" * 19
    q41 = q40 + "c"
    assert commands.mask_short("bash", {"command": f'git commit -m "{q40}"'}) == ("git commit -m <text>", True)
    short, masked = commands.mask_short("bash", {"command": f"git commit -m '{q41}'"})
    assert short == "git commit -m <text>" and masked is True
    short, masked = commands.mask_short("bash", {"command": 'echo "unterminated ' + "x " * 30})
    assert short == "echo <text>" and masked


def test_the_80_and_40_character_limits():
    cmd = " ".join(f"word{i}" for i in range(60))
    short, masked = commands.mask_short("bash", {"command": cmd})
    assert len(short) <= 80 and short.endswith("…") and short.startswith("word0 word1") and masked is False
    s40, _ = commands.mask_short("bash", {"command": cmd}, limit=40)
    assert len(s40) <= 40 and s40.endswith("…")
    # a cut never splits a marker
    s, _ = commands.mask_short("bash", {"command": "x" * 30 + " GH_TOKEN=abc " + "y" * 60}, limit=40)
    assert "abc" not in s and len(s) <= 40 and not re.search(r"\[hid(?!den\])", s)


def test_short_is_one_line_without_control_characters():
    short, _ = commands.mask_short("bash", {"command": "ls \\\n  -la\necho\x1b[31m red\x07\r\nwc -l"})
    assert "\n" not in short and "\r" not in short and "\x1b" not in short and "\x07" not in short
    assert short == "ls <script>"      # round 4: a line continuation is not plain and more lines follow


def test_masked_is_false_when_nothing_changed():
    assert commands.mask_short("bash", {"command": "ls -la src"})[1] is False
    assert commands.mask_short("read", {"path": "/a/b.py"})[1] is False


def test_other_tools_show_the_tool_name_only():
    short, masked = commands.mask_short("agent_spawn", {"task": "TASK: private brief ghp_AAAAAAAAAAAAAAAA"})
    assert short == "agent_spawn" and masked is False
    # round 8 allow-list: a tool name with a control character is not shown (stricter than before)
    assert commands.mask_short("mcp__x\n__y", {}) == ("other", True)
    assert commands.mask_short("mcp__x__y", {}) == ("mcp__x__y", False)


def test_a_31883_character_command_stays_bounded_and_fast():
    cmd = "python3 - <<'EOF'\n" + ("print('x' * 10)  # GH_TOKEN=abc\n" * 1000)[:31800] + "\nEOF\n"
    cmd = cmd + "x" * (31883 - len(cmd))
    assert len(cmd) == 31883
    t = time.monotonic()
    short, masked = commands.mask_short("bash", {"command": cmd})
    assert time.monotonic() - t < 0.5
    assert len(short) <= 80 and masked and "abc" not in short
    text = commands.mask_secrets(cmd)[0]
    assert "GH_TOKEN=[hidden]" in text and "GH_TOKEN=abc" not in text


# ---- rule 2c on the full text ---------------------------------------------------------------------

# ---- fixtures ---------------------------------------------------------------------------------------

def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _job_name(ts: float, suffix: str) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + suffix


def _write(path: Path, rows, pad_bytes: int = 0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if pad_bytes:
            filler = json.dumps({"type": "message_update", "x": "p" * 4000}) + "\n"
            for _ in range(pad_bytes // len(filler) + 1):
                f.write(filler)
        for r in rows:
            f.write(json.dumps(r) + "\n")


def make_pi_job(router_home: Path, name: str, now: float, state="running", pid=None, pid_start=None,
                calls=None, pad_bytes=0):
    """A job dir with meta.json and events.jsonl in the real pi layout."""
    d = router_home / "pi-jobs" / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"id": name, "state": state, "agent": "worker", "kind": "pi", "task": "TASK: private",
            "cwd": "/tmp/w", "created": int((now - 600) * 1000), "started": int((now - 600) * 1000)}
    if pid is not None:
        meta["pid"] = pid
    if pid_start is not None:
        meta["pid_start"] = pid_start
    (d / "meta.json").write_text(json.dumps(meta))
    rows = [{"type": "session", "id": "apexjob-" + name, "timestamp": _iso(now - 600)}]
    for i, c in enumerate(calls if calls is not None else [
            ("bash", {"command": "pytest -q tests"}, False, now - 300),
            ("read", {"path": "/x/y/z.py"}, True, now - 200),
            ("bash", {"command": "GH_TOKEN=abc123456 gh pr list"}, None, now - 100)]):
        tool, args, err, ts = c
        cid = f"toolu_{name}_{i}"
        rows.append({"type": "message_end", "message": {"role": "assistant", "timestamp": int(ts * 1000)}})
        rows.append({"type": "tool_execution_start", "toolCallId": cid, "toolName": tool, "args": args})
        if err is not None:
            rows.append({"type": "tool_execution_update", "toolCallId": cid, "partialResult": {"x": "y"}})
            rows.append({"type": "tool_execution_end", "toolCallId": cid, "toolName": tool,
                         "result": {"content": [{"type": "text", "text": "OUTPUT-TEXT"}]}, "isError": err})
            rows.append({"type": "message_start", "message": {"role": "toolResult", "timestamp": int((ts + 5) * 1000)}})
    _write(d / "events.jsonl", rows, pad_bytes)
    return d


def make_pi_session(home: Path, sid: str, now: float, cwd="/tmp/pi-cwd"):
    d = home / ".pi" / "agent" / "sessions" / "--tmp-pi-cwd--"
    rows = [{"type": "session", "version": 3, "id": sid, "timestamp": _iso(now - 900), "cwd": cwd},
            {"type": "message", "id": "m1", "timestamp": _iso(now - 800),
             "message": {"role": "assistant", "content": [
                 {"type": "toolCall", "id": "tc1", "name": "bash", "arguments": {"command": "ls -la"}},
                 {"type": "toolCall", "id": "tc2", "name": "edit", "arguments": {"path": "/r/a.py", "oldText": "o", "newText": "n"}}]}},
            {"type": "message", "id": "m2", "timestamp": _iso(now - 790),
             "message": {"role": "toolResult", "toolCallId": "tc1", "toolName": "bash", "isError": False,
                         "timestamp": int((now - 790) * 1000), "content": [{"type": "text", "text": "OUT"}]}},
            {"type": "message", "id": "m3", "timestamp": _iso(now - 780),
             "message": {"role": "toolResult", "toolCallId": "tc2", "toolName": "edit", "isError": True,
                         "timestamp": int((now - 780) * 1000), "content": []}}]
    p = d / f"2026-10-28T10-00-00-000Z_{sid}.jsonl"
    _write(p, rows)
    return p


def make_claude(home: Path, sid: str, now: float, sub_id: str | None = None):
    proj = home / ".claude" / "projects" / "-tmp-repo"
    base = {"sessionId": sid, "cwd": "/tmp/repo", "gitBranch": "main"}
    rows = [dict(base, type="user", timestamp=_iso(now - 500), message={"role": "user", "content": "PROMPT TEXT"}),
            dict(base, type="assistant", timestamp=_iso(now - 400), message={"role": "assistant", "content": [
                {"type": "text", "text": "x"},
                {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "npm test", "description": "d"}}]}),
            dict(base, type="user", timestamp=_iso(now - 390), message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "is_error": False, "content": "OUT"}]}),
            dict(base, type="assistant", timestamp=_iso(now - 60), message={"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu2", "name": "Read", "input": {"file_path": "/tmp/repo/README.md"}},
                {"type": "tool_use", "id": "tu3", "name": "Bash",
                 "input": {"command": "curl -H 'Authorization: Bearer zzz111' https://x"}}]}),
            dict(base, type="user", timestamp=_iso(now - 55), message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu2", "is_error": True, "content": "nope"}]})]
    _write(proj / f"{sid}.jsonl", rows)
    if sub_id:
        srows = [dict(base, isSidechain=True, agentId=sub_id, type="assistant", timestamp=_iso(now - 30),
                      message={"role": "assistant", "content": [
                          {"type": "tool_use", "id": "su1", "name": "Grep", "input": {"pattern": "PRIVATE"}}]})]
        _write(proj / sid / "subagents" / f"agent-{sub_id}.jsonl", srows)
    return proj / f"{sid}.jsonl"


@pytest.fixture
def homes(tmp_path):
    home = tmp_path / "home"
    rh = home / ".apex-router"
    rh.mkdir(parents=True)
    commands.clear_caches()
    return home, rh


def no_ps(argv, timeout=1.0):
    raise AssertionError("no process lookup expected")


# ---- rule 1: the readers ----------------------------------------------------------------------------

def test_pi_job_calls_newest_first(homes):
    home, rh = homes
    now = time.time()
    job = _job_name(now - 600, "ab12")
    make_pi_job(rh, job, now, state="done")
    doc = commands.list_calls(home, rh, job, KEY, now=now, run=no_ps)
    assert doc["agent"] == job and doc["kind"] == "pi-job" and doc["complete"] is True
    assert doc["calls_more"] == 0 and doc["procs"] is None
    c = doc["calls"]
    assert [x["tool"] for x in c] == ["bash", "read", "bash"]
    assert c[0]["short"] == "GH_TOKEN=[hidden] gh pr list" and c[0]["masked"] is True
    assert c[0]["state"] == "error" and c[0]["end"] is None          # no end and the job is done
    assert c[1]["state"] == "error" and c[1]["short"] == "read z.py"
    assert c[2]["state"] == "ok" and c[2]["short"] == "pytest -q tests" and c[2]["masked"] is False
    assert abs(c[2]["start"] - (now - 300)) < 1 and abs(c[2]["end"] - (now - 295)) < 1
    assert all(CALL_RE.fullmatch(x["call"]) for x in c) and len({x["call"] for x in c}) == 3
    assert set(c[0]) == {"call", "tool", "short", "masked", "start", "end", "state"}
    assert "abc123456" not in json.dumps(doc) and "OUTPUT-TEXT" not in json.dumps(doc)


def test_pi_job_running_call(homes):
    home, rh = homes
    now = time.time()
    job = _job_name(now - 600, "ab13")
    make_pi_job(rh, job, now, state="running")
    doc = commands.list_calls(home, rh, job, KEY, now=now, run=lambda a, timeout=1.0: "")
    assert doc["calls"][0]["state"] == "running"


def test_pi_session_calls(homes):
    home, rh = homes
    now = time.time()
    sid = "01a17a87-0000-7000-8000-000000000001"
    make_pi_session(home, sid, now)
    doc = commands.list_calls(home, rh, sid, KEY, now=now, run=lambda a, timeout=1.0: "")
    assert doc["kind"] == "pi" and doc["agent"] == sid
    assert [(x["tool"], x["short"], x["state"]) for x in doc["calls"]] == [
        ("edit", "edit a.py", "error"), ("bash", "ls -la", "ok")]
    assert abs(doc["calls"][1]["end"] - (now - 790)) < 1


def test_claude_session_calls(homes):
    home, rh = homes
    now = time.time()
    sid = "5b0c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c3"
    make_claude(home, sid, now)
    doc = commands.list_calls(home, rh, sid, KEY, now=now, run=lambda a, timeout=1.0: "")
    assert doc["kind"] == "claude"
    got = [(x["tool"], x["short"], x["state"]) for x in doc["calls"]]
    assert got == [("Bash", "curl -H <text>", "running"),          # round 4: a quoted word is cut
                   ("Read", "Read README.md", "error"), ("Bash", "npm test", "ok")]
    assert "zzz111" not in json.dumps(doc) and "PROMPT TEXT" not in json.dumps(doc)


def test_claude_subagent_calls(homes):
    home, rh = homes
    now = time.time()
    sid = "5b0c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c4"
    make_claude(home, sid, now, sub_id="a6c66d8c6123475ad")
    doc = commands.list_calls(home, rh, "a6c66d8c6123475ad", KEY, now=now, run=no_ps)
    assert doc["kind"] == "claude-sub" and doc["procs"] is None
    assert [(x["tool"], x["short"]) for x in doc["calls"]] == [("other", "Grep")]
    assert "PRIVATE" not in json.dumps(doc)


@pytest.mark.parametrize("bad", ["", "../etc", "a/b", "x" * 200, "nope-not-here", "20990101000000-zz99"])
def test_unknown_or_bad_agent_gives_none(homes, bad):
    home, rh = homes
    assert commands.list_calls(home, rh, bad, KEY, run=no_ps) is None


# ---- rule 3: call ids and the full text ----------------------------------------------------------

def test_ids_are_unique_per_block_and_stable(homes):
    home, rh = homes
    now = time.time()
    sid = "5b0c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c6"
    make_claude(home, sid, now)
    a = commands.list_calls(home, rh, sid, KEY, now=now, run=lambda a, timeout=1.0: "")
    b = commands.list_calls(home, rh, sid, KEY, now=now, run=lambda a, timeout=1.0: "")
    assert [x["call"] for x in a["calls"]] == [x["call"] for x in b["calls"]]
    assert len({x["call"] for x in a["calls"]}) == 3


# ---- rule 5: bounds ------------------------------------------------------------------------------

def test_a_huge_events_file_reads_only_the_tail(homes, monkeypatch):
    home, rh = homes
    now = time.time()
    job = _job_name(now - 600, "ab17")
    make_pi_job(rh, job, now, state="done", pad_bytes=6 * 1024 * 1024)
    seen = []
    real = commands._read_tail

    def spy(path, cap):
        data = real(path, cap)
        seen.append((cap, len(data[0]) if data else 0))
        return data
    monkeypatch.setattr(commands, "_read_tail", spy)
    doc = commands.list_calls(home, rh, job, KEY, now=now, run=no_ps)
    assert doc["complete"] is False and len(doc["calls"]) == 3
    # contract rule 5 (changed 2026-10-29 with the read growth): each read <= TAIL_GROW_MAX (4 MiB), x4 steps
    assert seen and all(cap <= commands.TAIL_GROW_MAX and n <= commands.TAIL_GROW_MAX for cap, n in seen)
    assert [c for c, _ in seen] == [256 * 1024, 1024 * 1024, 4 * 1024 * 1024]
    assert commands.TAIL_MAX == 256 * 1024 and commands.TAIL_GROW_MAX == 4 * 1024 * 1024


def test_3000_job_dirs(homes):
    home, rh = homes
    now = time.time()
    root = rh / "pi-jobs"
    names = [_job_name(now - 4000 + i, f"j{i:04d}") for i in range(3000)]
    for n in names:
        (root / n).mkdir(parents=True)
    newest, oldest = names[-1], names[0]
    make_pi_job(rh, newest, now, state="done")
    make_pi_job(rh, oldest, now, state="done")
    assert commands.JOBS_MAX == 2000
    t = time.monotonic()
    assert commands.list_calls(home, rh, newest, KEY, now=now, run=no_ps) is not None
    assert time.monotonic() - t < 2.0
    assert commands.list_calls(home, rh, oldest, KEY, now=now, run=no_ps) is None   # not in the newest 2000


# ---- rule 4: procs -------------------------------------------------------------------------------

FMT = "%a %b %d %H:%M:%S %Y"


def _ps_line(pid, ppid, rss_kb, cpu, comm, start):
    return f"{pid:>6} {ppid:>6} {rss_kb:>8} {cpu:>5} {time.strftime(FMT, time.localtime(start))}     {comm}"


def fake_run(rows, args=None, lsof=""):
    def run(argv, timeout=1.0):
        if argv[:2] == ["ps", "-axo"]:
            return "\n".join(rows) + "\n"
        if argv[:2] == ["ps", "-o"]:
            want = [int(p) for p in argv[-1].split(",")]
            return "".join(f"{p} {(args or {})[p]}\n" for p in want if p in (args or {}))
        if argv[0] == "lsof":
            return lsof
        raise RuntimeError("unexpected")
    return run


def test_procs_of_a_running_pi_job(homes):
    home, rh = homes
    now = time.time()
    start = int(now - 1000)
    job = _job_name(now - 600, "ab18")
    make_pi_job(rh, job, now, state="running", pid=5000, pid_start=time.strftime(FMT, time.localtime(start)))
    rows = [_ps_line(5000, 1, 160 * 1024, 3.1, "pi", start),
            _ps_line(5001, 5000, 4096, 0.0, "/bin/bash", now - 12),
            _ps_line(5002, 5001, 340 * 1024, 80.2, "python3.13", now - 12),
            _ps_line(5003, 5002, 1024, 0.0, "/bin/sh", now - 5),
            _ps_line(5004, 5003, 1024, 0.0, "sleep", now - 5),
            _ps_line(9000, 1, 1024, 0.0, "other", now - 5)]
    args = {5002: "python3.13 -m pytest -q --token=SECRETARG"}
    doc = commands.list_calls(home, rh, job, KEY, now=now, run=fake_run(rows, args))
    p = doc["procs"]
    assert p["pid"] == 5000 and p["name"] == "pi" and p["cpu"] == 3.1 and p["rss_mb"] == 160
    bash = p["children"][0]
    assert bash["pid"] == 5001 and bash["name"] == "bash" and bash["label"] is None and 10 <= bash["age_s"] <= 14
    py = bash["children"][0]
    assert py["label"] == "pytest" and py["name"] == "python3.13"
    assert py["children"][0]["pid"] == 5003 and py["children"][0]["children"] == []   # 3 levels below the root
    assert "SECRETARG" not in json.dumps(doc) and doc["complete"] is False             # level 4 cut


def test_procs_is_null_when_the_pid_is_reused_or_gone(homes):
    home, rh = homes
    now = time.time()
    job = _job_name(now - 600, "ab19")
    make_pi_job(rh, job, now, state="running", pid=5000, pid_start=time.strftime(FMT, time.localtime(now - 99999)))
    rows = [_ps_line(5000, 1, 1024, 0.0, "pi", now - 10)]
    assert commands.list_calls(home, rh, job, KEY, now=now, run=fake_run(rows))["procs"] is None
    assert commands.list_calls(home, rh, job, KEY, now=now, run=fake_run([]))["procs"] is None


def test_procs_cap_40(homes):
    home, rh = homes
    now = time.time()
    job = _job_name(now - 600, "ab20")
    make_pi_job(rh, job, now, state="running", pid=5000)
    rows = [_ps_line(5000, 1, 1024, 0.0, "pi", now - 100)]
    rows += [_ps_line(6000 + i, 5000, 1024, 0.0, "node", now - 50) for i in range(80)]
    doc = commands.list_calls(home, rh, job, KEY, now=now, run=fake_run(rows))

    def count(n):
        return 1 + sum(count(c) for c in n["children"])
    assert count(doc["procs"]) <= 40 and doc["complete"] is False


def test_procs_of_a_claude_session(homes):
    home, rh = homes
    now = time.time()
    sid = "5b0c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c7"
    make_claude(home, sid, now)
    start = int(now - 3000)
    sd = home / ".claude" / "sessions"
    sd.mkdir(parents=True)
    (sd / "700.json").write_text(json.dumps({"pid": 700, "sessionId": sid,
                                             "procStart": time.strftime(FMT, time.gmtime(start))}))
    rows = [_ps_line(700, 1, 2048, 1.0, "claude", start), _ps_line(701, 700, 1024, 0.0, "zsh", now - 5)]
    doc = commands.list_calls(home, rh, sid, KEY, now=now, run=fake_run(rows))
    assert doc["procs"]["pid"] == 700 and doc["procs"]["children"][0]["name"] == "zsh"


def test_procs_of_a_pi_session_by_cwd(homes):
    home, rh = homes
    now = time.time()
    sid = "01a17a87-0000-7000-8000-000000000002"
    cwd = home / "work"
    cwd.mkdir(parents=True)
    make_pi_session(home, sid, now, cwd=str(cwd))
    rows = [_ps_line(800, 1, 2048, 1.0, "pi", now - 900)]
    doc = commands.list_calls(home, rh, sid, KEY, now=now, run=fake_run(rows, lsof=f"p800\nfcwd\nn{cwd}\n"))
    assert doc["procs"]["pid"] == 800


# ---- running_now and the menu bar (Q3) -------------------------------------------------------------

def test_running_now_gives_the_newest_running_call_at_40_characters(homes):
    home, rh = homes
    now = time.time()
    job = _job_name(now - 600, "ab21")
    long_cmd = "GH_TOKEN=abc123456 " + " ".join(f"w{i}" for i in range(40))
    make_pi_job(rh, job, now, state="running", calls=[("bash", {"command": long_cmd}, None, now - 700)])
    r = commands.running_now(job, home=home, router_home=rh, now=now)
    assert r["agent"] == job and r["kind"] == "pi-job" and r["tool"] == "bash"
    assert len(r["short"]) <= 40 and "abc123456" not in r["short"] and r["masked"] is True
    assert 695 <= r["age_s"] <= 705
    assert set(r) == {"agent", "kind", "tool", "short", "masked", "start", "age_s"}
    done = _job_name(now - 600, "ab22")
    make_pi_job(rh, done, now, state="running", calls=[("bash", {"command": "ls"}, False, now - 10)])
    assert commands.running_now(done, home=home, router_home=rh, now=now) is None


def _menu_snap(now, items, jobs=None, agents=None):
    return {"ts": now, "pressure": {"level": "GREEN", "n": 0}, "agents": agents or [],
            "jobs": jobs or {"running": 0, "queued": 0, "items": []},
            "commands_now": {"items": items}}


@pytest.mark.parametrize("secret", ["0123456789abcdef0123456789abcdef", "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3",
                                    "a/QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3/b"])
def test_a_long_token_is_still_hidden(secret):
    short, masked = commands.mask_short("bash", {"command": f"echo {secret}"})
    assert "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3" not in short and "0123456789abcdef0123456789abcdef" not in short
    assert masked is True


def test_the_read_never_passes_the_grow_cap(homes, monkeypatch):
    home, rh = homes
    now = time.time()
    job = _job_name(now - 600, "big2")
    make_pi_job(rh, job, now, state="done", pad_bytes=6 * 1024 * 1024)
    sizes = []
    real = commands._read_tail
    monkeypatch.setattr(commands, "_read_tail", lambda p, cap: (sizes.append(cap), real(p, cap))[1])
    doc = commands.list_calls(home, rh, job, KEY, now=now, run=no_ps)
    assert max(sizes) <= commands.TAIL_GROW_MAX and doc["complete"] is False


# ---- xval dt3i (round 1): masking bypasses and the session listing bound ------------------------------

@pytest.mark.parametrize("cmd,secret", [
    ("TOKEN=$'topsecret' run", "topsecret"),
    ('API_KEY=$"topsecret2" run', "topsecret2"),
])
def test_ansi_c_and_locale_quoted_values_are_hidden(cmd, secret):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert secret not in short and masked is True
    assert secret not in commands.mask_secrets(cmd)[0]


@pytest.mark.parametrize("cmd,gone", [
    ("python -c'print(123)'", "print(123)"),
    ('python3 -c"import os; os.system(1)"', "import os"),
    ("node -e'console.log(42)'", "console.log(42)"),
    ("bash -lc'echo private'", "echo private"),
    ("python -cprint(77)", "print(77)"),
    ("perl -e$'print 5'", "print 5"),
    ("x --eval=privatecode", "privatecode"),
])
def test_attached_scripts_are_hidden(cmd, gone):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert gone not in short and "<script>" in short and masked is True


@pytest.mark.parametrize("cmd", [
    "cat <<'1'\nprivate command body\n1",
    "cat <<1\nprivate command body\n1",
    'cat <<"END-X"\nprivate command body\nEND-X',
    "cat << \\EOF.x\nprivate command body\nEOF.x",
    "cat <<-'a b'\n\tprivate command body\n\ta b",
])
def test_any_heredoc_delimiter_hides_the_body(cmd):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert "private command body" not in short and "<script>" in short and masked is True


def test_plain_flags_are_not_hidden():
    # flags without c or e stay; -c<x> and -e<x> are hidden on purpose (fail closed: tar -czf shows "-c<script>")
    short, masked = commands.mask_short("bash", {"command": "ls -la && git status -s && rg -n foo src"})
    assert short == "ls -la && git status -s && rg -n foo src" and masked is False


def test_claude_session_listing_is_bounded(tmp_path, monkeypatch):
    from apex_router.agent_resources import sessions as sess
    d = tmp_path / ".claude" / "sessions"
    d.mkdir(parents=True)
    for i in range(30):
        (d / f"{i}.json").write_text(json.dumps({"pid": 10_000 + i, "sessionId": f"s{i}"}))
    monkeypatch.setattr(sess, "SESSION_FILES_MAX", 10)
    reads = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (reads.append(self), real(self, *a, **k))[1])
    table = {10_000 + i: {"name": "claude", "start": None} for i in range(30)}
    out = sess.claude_sessions(tmp_path, table)
    assert len(reads) <= 10 and len(out) <= 10


# ---- xval 45zb (round 2): fail closed instead of a regex per shell form --------------------------------

@pytest.mark.parametrize("cmd,secret", [
    ("TOKEN=$''topsecret run", "topsecret"),
    ("X_TOKEN=a'b'$'c\\'d'\"e\"topsecret more", "topsecret"),
    ('API_KEY="abc\\"topsecret" x', "topsecret"),
    ("A_KEY=$'x\\'topsecret' y", "topsecret"),
])
def test_a_joined_or_escaped_value_is_one_word(cmd, secret):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert secret not in short and masked is True
    assert secret not in commands.mask_secrets(cmd)[0]


@pytest.mark.parametrize("cmd", [
    "python -c$'print(\\'hi\\');print(123)'",
    "python -c 'a' ; print(123)",
    "sh -c \"x\" && print(123)",
])
def test_everything_after_a_script_flag_is_hidden(cmd):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert "print(123)" not in short and short.endswith("<script>") and masked is True


@pytest.mark.parametrize("cmd", [
    "cat <<\"a'b\"\nprivate command body\na'b",
    "cat <<''\nprivate command body\n",
    "x <<$'E'\nprivate command body\nE",
    "echo one\nprivate command body",
])
def test_only_the_first_line_shows(cmd):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert "private command body" not in short and short.endswith("<script>") and masked is True


def test_masking_a_64_kib_input_is_fast():
    worst = ["cat <<' '\n" + " " * 65522 + "X\n \n", "TOKEN='" + "a" * 65000, "A_KEY=$'" + "\\" * 30000,
             "x " * 30000, "-c " * 20000, "a=" * 30000]
    t = time.monotonic()
    for w in worst:
        commands.mask_short("bash", {"command": w})
        commands.mask_secrets(w)
    assert time.monotonic() - t < 1.0


def test_session_listing_counts_every_entry(tmp_path, monkeypatch):
    from apex_router.agent_resources import sessions as sess
    d = tmp_path / ".claude" / "sessions"
    d.mkdir(parents=True)
    for i in range(50):
        (d / f"{i}.tmp").write_text("x")
    monkeypatch.setattr(sess, "SESSION_FILES_MAX", 10)
    seen = []
    real = os.scandir

    class Spy:
        def __init__(self, p): self.it = real(p)
        def __enter__(self): return self
        def __exit__(self, *a): self.it.close()
        def __iter__(self):
            for e in self.it:
                seen.append(e.name)
                yield e
    monkeypatch.setattr(sess.os, "scandir", Spy)
    sess.claude_sessions(tmp_path, {1: {"name": "claude", "start": None}})
    assert len(seen) <= 11


# ---- xval lm3c (round 3): allow-list short text, fail-closed NAME=value -----------------------------

@pytest.mark.parametrize("cmd,secret", [
    ("TOKEN=$:topsecret run", "topsecret"),
    ("TOKEN=x$(printf topsecret) run", "topsecret"),
    ("cat <<'E' # \\\nprivate_body\nE", "private_body"),
    ("python '-c' 'print(123)'", "print(123)"),
    ("TOKEN=$x\\ topsecret", "topsecret"),
    ("ls `cat topsecret`", "topsecret"),
    ("echo #topsecret", "topsecret"),
])
def test_round3_inputs_are_hidden(cmd, secret):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert secret not in short and masked is True


@pytest.mark.parametrize("cmd", ["TOKEN=$:topsecret run", "TOKEN=x$(printf topsecret) run", "TOKEN=$x\\ topsecret",
                                 "A_KEY='a b topsecret' c", "TOKEN=\"$(cat x) topsecret\""])
def test_a_non_plain_secret_value_hides_to_the_line_end_in_the_full_text(cmd):
    assert "topsecret" not in commands.mask_secrets(cmd)[0]


def test_short_text_shows_only_allowed_characters():
    import random
    rnd = random.Random(7)
    alphabet = "ab$'\"\\`()#{}=- \n;|&<>xyz_TOKEN"
    for _ in range(3000):
        cmd = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 40)))
        short, _ = commands.mask_short("bash", {"command": cmd})
        body = short.replace("<text>", "").replace("<script>", "").replace("[hidden]", "").replace("…", "")
        assert not set(body) & set("'\"\\`()#\n"), (cmd, short)


def test_masking_64_kib_of_repeated_names_is_fast():
    t = time.monotonic()
    for w in [("TOKEN=$x\\ " * 6554)[:65536], ("A=1 TOKEN=" * 7000)[:65536], ("TOKEN=a " * 8000)[:65536]]:
        commands.mask_short("bash", {"command": w})
        commands.mask_secrets(w)
    assert time.monotonic() - t < 1.0


# ---- xval 7p38 (round 4) ------------------------------------------------------------------------------

def test_a_quoted_secret_value_over_lines_is_hidden_in_the_full_text():
    for cmd in ('TOKEN="first\nprivate_body" run', "A_KEY='x\r\nprivate_body'", 'TOKEN=$(cat\nprivate_body)'):
        assert "private_body" not in commands.mask_secrets(cmd)[0]


@pytest.mark.parametrize("cmd,gone", [("python -cpass", "pass"), ("perl -epayload", "payload"),
                                      ("bash -lcpayload", "payload"), ("x --eval=payload", "payload"),
                                      ("x --command=payload", "payload")])
def test_a_glued_script_body_is_hidden(cmd, gone):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert gone not in short and short.endswith("<script>") and masked is True


@pytest.mark.parametrize("cmd", ["\nprivate_body", "ls\rprivate_body", "ls\x0bprivate_body", "ls\x0cprivate_body",
                                 "ls\u2028private_body", "ls\x85private_body", "ls\x1eprivate_body"])
def test_any_line_break_ends_the_first_line(cmd):
    short, masked = commands.mask_short("bash", {"command": cmd})
    assert "private_body" not in short and masked is True


def test_script_word_check_is_linear():
    t = time.monotonic()
    for w in ["-" + "c" * 65534 + "!", "-" + "x" * 65534 + "!", "-" + "a" * 65535, "--" + "c" * 65534]:
        commands.mask_short("bash", {"command": w})
        commands.mask_secrets(w)
    assert time.monotonic() - t < 0.5


# ---- xval 8ll6 (round 5): a check per word, no NAME regex; script flags with any c/e/C/E -----------------

@pytest.mark.parametrize("cmd,secret", [
    ("TOKEN==private_body", "private_body"),
    ("A" * 124 + "TOKEN=private_body", "private_body"),
    ("TOKEN = private_body x", "private_body"),
    ("TOKEN= private_body x", "private_body"),
    ("TOKEN =private_body x", "private_body"),
    ("X=1 TOKEN+=private_body y", "private_body"),
    ("TOKEN[0]=private_body", "private_body"),
    ("a.b.TOKEN=private_body", "private_body"),
])
def test_any_secret_name_before_an_equals_sign_hides_the_value(cmd, secret):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert secret not in short and masked is True
    assert secret not in commands.mask_secrets(cmd)[0]


def test_a_secret_name_without_equals_hides_nothing():
    assert commands.mask_secrets("grep KEY file && echo TOKEN done")[0] == \
        "grep KEY file && echo TOKEN done"


@pytest.mark.parametrize("cmd,gone", [("perl -0eprint+42", "print+42"), ("perl -Eprint", "print"),
                                      ("x -1cpayload", "payload"), ("x -9Cpayload", "payload")])
def test_a_script_flag_with_any_letters_before_it_is_hidden(cmd, gone):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert gone not in short and short.endswith("<script>") and masked is True


def test_the_word_check_is_linear_on_64_kib():
    t = time.monotonic()
    for w in ["a." * 32768, ("A" * 100 + "=") * 600, "TOKEN " * 10000, "TOKEN = " * 8000, "=" * 65536,
              "x=TOKEN= " * 7000, "-" + "0" * 65535]:
        commands.mask_short("bash", {"command": w})
        commands.mask_secrets(w)
    assert time.monotonic() - t < 1.0


# ---- xval o41s (round 6) ------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["X=1;TOKEN=private_body", "X=1&&TOKEN=private_body", "a|TOKEN=private_body",
                                 "(TOKEN=private_body)", "X=1`TOKEN=private_body`", "a>TOKEN=private_body"])
def test_a_control_character_ends_the_word_before_a_secret_name(cmd):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert "private_body" not in short and masked is True
    assert "private_body" not in commands.mask_secrets(cmd)[0]


@pytest.mark.parametrize("cmd", ["perl${IFS}-e${IFS}print+42", "perl$IFS-e$IFS'print+42'", "IFS=x; perlx-exprint+42"])
def test_ifs_ends_the_short_text(cmd):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("bash", {"command": cmd}, limit)
        assert "print+42" not in short and masked is True


# ---- xval 7khq (round 7): a control character never joins two words -----------------------------------

@pytest.mark.parametrize("cc", ["\r", "\x00", "\x07", "\x1b", "\x7f", "\x85", "\x9b"])
def test_a_control_character_between_words_keeps_them_apart(cc):
    cmd = f'printf "X=1{cc}TOKEN=private_body"'
    assert "private_body" not in commands.mask_secrets(cmd)[0]
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        assert "private_body" not in commands.mask_short("bash", {"command": cmd}, limit)[0]


def test_a_pi_job_with_many_update_lines_still_shows_20_calls(homes):
    home, rh = homes
    now = time.time()
    job = _job_name(now - 600, "big1")
    d = make_pi_job(rh, job, now, state="done", calls=[])
    # 30 calls, each followed by 40 KiB of message_update lines (the real pi pattern)
    rows = []
    for i in range(30):
        ts = now - 500 + i
        cid = f"tc{i}"
        rows.append({"type": "message_end", "message": {"role": "assistant", "timestamp": int(ts * 1000)}})
        rows.append({"type": "tool_execution_start", "toolCallId": cid, "toolName": "bash",
                     "args": {"command": f"echo step{i}"}})
        rows.append({"type": "tool_execution_end", "toolCallId": cid, "toolName": "bash", "isError": False})
        for _ in range(10):
            rows.append({"type": "message_update", "x": "u" * 4000})
    with open(d / "events.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    doc = commands.list_calls(home, rh, job, KEY, now=now, run=no_ps)
    assert len(doc["calls"]) == 20 and doc["calls_more"] >= 1
    assert doc["calls"][0]["short"] == "echo step29"
    assert doc["calls"][-1]["short"] == "echo step10"


def test_a_long_path_is_not_hidden():
    p = "/home/u/.apex-router/pi-jobs-wt/mvtuzw5r/src/apex_router/studio/commands_reader_long_name.py"
    short, masked = commands.mask_short("bash", {"command": f"python3 {p}"})
    assert "[hidden]" not in short and ("python3 " + p).startswith(short.rstrip("…"))
    assert p in commands.mask_secrets(f"cat {p}")[0]
    assert commands.mask_short("read", {"path": p}) == ("read commands_reader_long_name.py", False)


# ---- xval 066s (round 8): file paths and tool names as an allow-list; no full-text output ----------------

@pytest.mark.parametrize("path", ["/tmp/TOKEN=prefix/private_body", "/tmp/a b/private_body", "/tmp/$(x)/private_body",
                                  "/tmp/x\nprivate_body", "/tmp/sk-AbCdEf0123456789/private_body",
                                  "/tmp/x\u2028/private_body", "/tmp/'q'/private_body"])
def test_a_file_path_with_a_secret_or_non_plain_part_shows_no_name(path):
    for limit in (commands.SHORT_MAX, commands.MENU_MAX):
        short, masked = commands.mask_short("read", {"path": path}, limit)
        assert short == "read <path>" and masked is True


@pytest.mark.parametrize("tool", ["X=1 TOKEN=private_body", "a b", "my_TOKEN_tool", "x" * 41, "\u2028private_body"])
def test_a_tool_name_outside_the_allow_list_shows_other(tool):
    short, masked = commands.mask_short(tool, {})
    assert short == "other" and masked is True and "private_body" not in short


def test_there_is_no_full_text_output():
    assert not hasattr(commands, "mask_full") and not hasattr(commands, "full_call")
