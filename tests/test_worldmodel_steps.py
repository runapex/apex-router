"""P6 E1 step dataset: pi / Claude / subagent extraction, linkage, splits, outcomes, privacy."""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apex_router import labels as L
from apex_router.worldmodel import cli as WCLI
from apex_router.worldmodel import steps as S

SECRET = "sk-SECRET-do-not-store-42"


@pytest.fixture(autouse=True)
def _no_ollama(monkeypatch):
    """Hermetic: the default embedder (local ollama) is never called from tests."""
    monkeypatch.setattr(S, "_auto_embed", lambda: None)
T0 = 1_790_000_000.0


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write(p: Path, rows: list, raw_lines: tuple = ()) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows) + "".join(raw_lines))
    return p


# ---- record builders (shapes copied from real pi / Claude Code logs; text synthetic) ---------

def pi_session(uh: Path, sid: str, t0: float, turns: list, raw_lines=()) -> Path:
    """turns: [(user_text, [(tool, args, is_error, result_text)])]"""
    p = uh / ".pi" / "agent" / "sessions" / "--x--" / f"2026-10-07T00-00-00-000Z_{sid}.jsonl"
    rows = [{"type": "session", "version": 3, "id": sid, "timestamp": _iso(t0), "cwd": "/w"},
            {"type": "model_change", "id": "m0", "timestamp": _iso(t0), "provider": "p",
             "modelId": "pi-model"}]
    t, n = t0, 0
    for user, calls in turns:
        t += 10
        rows.append({"type": "message", "id": f"u{t}", "timestamp": _iso(t), "message": {
            "role": "user", "content": [{"type": "text", "text": user}], "timestamp": int(t * 1000)}})
        for tool, args, err, out in calls:
            n += 1
            t += 5
            rows.append({"type": "message", "id": f"a{n}", "timestamp": _iso(t), "message": {
                "role": "assistant", "model": "pi-model", "provider": "p", "timestamp": int(t * 1000),
                "content": [{"type": "toolCall", "id": f"call_{sid[:4]}_{n}", "name": tool,
                             "arguments": args}]}})
            rows.append({"type": "message", "id": f"r{n}", "timestamp": _iso(t + 1), "message": {
                "role": "toolResult", "toolCallId": f"call_{sid[:4]}_{n}", "toolName": tool,
                "content": [{"type": "text", "text": out}], "isError": err,
                "timestamp": int((t + 1) * 1000)}})
    return _write(p, rows, raw_lines)


def _claude_rows(sid: str, t: float, items: list, agent=None, prefix="toolu") -> tuple[list, float]:
    """items: ("user", text) | ("tool", name, input, is_error, result, id_or_None)."""
    rows, n = [], 0
    for it in items:
        t += 5
        base = {"sessionId": sid, "timestamp": _iso(t), "isSidechain": agent is not None,
                "uuid": f"{prefix}-{t}"}
        if agent:
            base["agentId"] = agent
        if it[0] == "user":
            rows.append({**base, "type": "user", "message": {"role": "user", "content": it[1]}})
            continue
        _, name, inp, err, out, uid = it
        n += 1
        uid = uid or f"{prefix}_{sid[:4]}_{agent or 'm'}_{n}"
        rows.append({**base, "type": "assistant", "message": {
            "model": "claude-test-model", "role": "assistant", "type": "message",
            "content": [{"type": "tool_use", "id": uid, "name": name, "input": inp,
                         "caller": {"type": "direct"}}]}})
        rows.append({**base, "type": "user", "timestamp": _iso(t + 1), "message": {
            "role": "user", "content": [{"tool_use_id": uid, "type": "tool_result",
                                         "content": out, "is_error": err}]},
            "toolUseResult": {"stdout": out, "stderr": "", "interrupted": False}})
    return rows, t


def claude_session(uh: Path, sid: str, t0: float, items: list, raw_lines=()) -> Path:
    rows, _ = _claude_rows(sid, t0, items)
    return _write(uh / ".claude" / "projects" / "-w-proj" / f"{sid}.jsonl", rows, raw_lines)


def claude_sub(uh: Path, sid: str, agent: str, t0: float, items: list, tool_use_id=None,
               workflow=None) -> Path:
    d = uh / ".claude" / "projects" / "-w-proj" / sid / "subagents"
    if workflow:
        d = d / "workflows" / workflow
    rows, _ = _claude_rows(sid, t0, [("user", "subagent prompt " + SECRET)] + items, agent=agent,
                           prefix="tsub")
    p = _write(d / f"agent-{agent}.jsonl", rows)
    meta = {"agentType": "general-purpose", "description": "d", "spawnDepth": 1}
    if tool_use_id:
        meta["toolUseId"] = tool_use_id
    (d / f"agent-{agent}.meta.json").write_text(json.dumps(meta))
    return p


SID_PI = "01a0aaaa-0000-7000-8000-000000000001"
SID_CL = "11111111-2222-3333-4444-555555555555"


def _fixture(tmp_path: Path):
    uh = tmp_path / "user"
    pi_session(uh, SID_PI, T0, [
        ("fix the parser " + SECRET, [
            ("read", {"path": "/w/a.py"}, False, "x" * 1500),
            ("edit", {"path": "/w/a.py", "oldText": "a", "newText": SECRET}, False, "ok"),
            ("bash", {"command": f"cd /w && .venv/bin/pytest -q # {SECRET}"}, False,
             "....F\n1 failed, 4 passed in 0.3s"),
            ("bash", {"command": "cd /w && .venv/bin/pytest -q"}, False, "5 passed in 0.3s"),
            ("bash", {"command": "git commit -am x"}, False, "[main abc] x")]),
        ("just chatting", []),                                   # no tool: not a task
        ("Base directory for this skill: /x", [("bash", {"command": "ls"}, False, "")]),
        ("now the docs", [("write", {"path": "/w/d.md", "content": "y" * 20_000}, True, "denied")]),
    ], raw_lines=("{not json\n", "[1, 2]\n"))
    claude_session(uh, SID_CL, T0 + 3600, [
        ("user", "investigate " + SECRET),
        ("tool", "Grep", {"pattern": SECRET}, False, "a.py:1", None),
        ("tool", "Agent", {"prompt": SECRET, "subagent_type": "general-purpose"}, False,
         "done", "toolu_spawn_1"),
        ("tool", "Workflow", {"script": "x"}, False, "started", None),
        ("user", [{"type": "text", "text": "<system-reminder>harness noise</system-reminder>"}]),
        ("tool", "Bash", {"command": "git status"}, False, "clean", None),
        ("user", "second request"),
        ("tool", "Bash", {"command": "echo hi; cargo test 2>&1 | tail"}, True,
         "test result: FAILED. 3 passed; 2 failed; 0 ignored", None),
    ])
    # subagent spawned by toolu_spawn_1 (task 0); a nested one spawned by the first subagent;
    # a workflow agent with no toolUseId (joins by time)
    claude_sub(uh, SID_CL, "aaaa1111", T0 + 3600 + 12, [
        ("tool", "Read", {"file_path": "/w/x"}, False, "z" * 50, None),
        ("tool", "Agent", {"prompt": "nested"}, False, "ok", "tsub_nested_spawn"),
        ("tool", "Edit", {"file_path": "/w/x", "old_string": "a", "new_string": "b"}, False, "ok",
         None),
        ("tool", "Bash", {"command": "uv run pytest -q"}, False, "2 passed in 0.1s", None),
        ("tool", "SubagentHandback", {"message": SECRET}, False, "", None),
    ], tool_use_id="toolu_spawn_1")
    claude_sub(uh, SID_CL, "bbbb2222", T0 + 3600 + 40, [
        ("tool", "Bash", {"command": "rg foo"}, False, "", None),
    ], tool_use_id="tsub_nested_spawn")
    claude_sub(uh, SID_CL, "cccc3333", T0 + 3600 + 16, [
        ("tool", "Bash", {"command": "ls"}, False, "", None),
    ], workflow="wf_1")
    claude_sub(uh, "99999999-0000-0000-0000-000000000000", "dddd4444", T0, [
        ("tool", "Bash", {"command": "ls"}, False, "", None)], tool_use_id="nope")   # orphan
    # telemetry: a model for the main Claude thread and for subagent aaaa1111
    tel = tmp_path / "telemetry.jsonl"
    _write(tel, [
        {"ts": T0 + 3600 + 5, "session_id": SID_CL, "agent_id": None, "model_resolved": "tel-main"},
        {"ts": T0 + 3600 + 12, "session_id": SID_CL, "agent_id": "aaaa1111",
         "model_resolved": "tel-sub"},
        {"ts": T0, "session_id": "other", "model_resolved": "x"},
    ], raw_lines=("garbage\n",))
    return uh, tel


def _read(p: Path) -> list:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _build(tmp_path, **kw):
    uh, tel = _fixture(tmp_path)
    home = tmp_path / "ar"
    man = S.build(home=home, user_home=uh, telemetry=tel, **kw)
    d = home / "worldmodel"
    return man, _read(d / "steps.jsonl"), _read(d / "tasks.jsonl"), d, uh


def test_steps_tasks_and_ids(tmp_path):
    man, steps, tasks, d, uh = _build(tmp_path)
    keys = {"sid", "agent", "src", "task", "i", "ts", "act", "tool", "err", "tests", "out_b",
            "in_b", "dt", "phase", "model", "spawn"}
    assert all(set(s) == keys for s in steps)
    # pi: task ordinals are the user-turn ordinals labels.extract uses. The skill body is not a
    # turn, so its tool call lands in the turn before it ("just chatting" becomes task 1).
    pi_tasks = [t for t in tasks if t["src"] == "pi"]
    assert [t["task"] for t in pi_tasks] == [f"{SID_PI}:0", f"{SID_PI}:1", f"{SID_PI}:2"]
    pi_path = next((uh / ".pi").glob("**/*.jsonl"))
    assert [t["index"] for t in L.extract(pi_path)] == [0, 1, 2]
    assert [len(t["calls"]) for t in L.extract(pi_path)] == [t["steps"] for t in pi_tasks]
    pi_steps = [s for s in steps if s["task"] == f"{SID_PI}:0"]
    assert [s["act"] for s in pi_steps] == ["read", "edit", "test", "test", "vcs"]
    assert [s["phase"] for s in pi_steps] == ["explore", "edit", "verify", "verify", "deliver"]
    assert [s["i"] for s in pi_steps] == [0, 1, 2, 3, 4]
    assert pi_steps[0]["dt"] is None and pi_steps[1]["dt"] == 5.0
    assert pi_steps[2]["tests"] == {"ran": 1, "failed": 1, "passed": 4}
    assert pi_steps[3]["tests"] == {"ran": 1, "failed": 0, "passed": 5}
    assert pi_steps[0]["tests"] == {"ran": 0, "failed": None, "passed": None}
    assert pi_steps[0]["out_b"] == 1 and pi_steps[0]["in_b"] == 0
    assert all(s["model"] == "pi-model" for s in pi_steps)            # transcript fallback
    w = [s for s in steps if s["task"] == f"{SID_PI}:2"]
    assert w[0]["act"] == "write" and w[0]["err"] == 1 and w[0]["in_b"] == 2
    # Claude main: the harness reminder is not a task boundary
    cl = [s for s in steps if s["task"] == f"{SID_CL}:0" and s["agent"] is None]
    assert [s["tool"] for s in cl] == ["Grep", "Agent", "Workflow", "Bash"]
    assert [s["spawn"] for s in cl] == [0, 1, 1, 0]
    assert cl[0]["model"] == "tel-main"                                # telemetry join
    c2 = [s for s in steps if s["task"] == f"{SID_CL}:1"]
    assert c2[0]["act"] == "test" and c2[0]["err"] == 1
    assert c2[0]["tests"] == {"ran": 1, "failed": 2, "passed": 3}
    assert man["counts"]["tasks"] == 5 and man["counts"]["sessions"] == 2
    assert man["read_errors"]["malformed_lines"] == 2                  # fail open, counted


def test_subagent_linkage(tmp_path):
    man, steps, tasks, d, uh = _build(tmp_path)
    sub = [s for s in steps if s["agent"]]
    assert {s["sid"] for s in sub} == {SID_CL}                          # parent session id
    by_agent = {}
    for s in sub:
        by_agent.setdefault(s["agent"], []).append(s)
    assert set(by_agent) == {"aaaa1111", "bbbb2222", "cccc3333"}
    a = by_agent["aaaa1111"]
    assert all(s["task"] == f"{SID_CL}:0" for s in a)                  # via meta toolUseId
    assert [s["act"] for s in a] == ["read", "delegate", "edit", "test", "ask"]
    assert [s["phase"] for s in a] == ["explore", "other", "edit", "verify", "deliver"]
    assert [s["i"] for s in a] == [0, 1, 2, 3, 4] and a[0]["dt"] is None
    assert a[0]["model"] == "tel-sub"
    assert by_agent["bbbb2222"][0]["task"] == f"{SID_CL}:0"            # nested: through parent
    assert by_agent["cccc3333"][0]["task"] == f"{SID_CL}:0"            # workflow: by time
    t0 = next(t for t in tasks if t["task"] == f"{SID_CL}:0")
    assert t0["steps"] == 4 and t0["sub_steps"] == 7 and t0["agents"] == 3
    assert man["counts"]["subagents"] == 3 and man["counts"]["subagents_orphan"] == 1
    assert man["per_source"]["claude"]["steps_sub"] == 7


def test_outcome_precedence(tmp_path):
    uh, tel = _fixture(tmp_path)
    home = tmp_path / "ar"
    pi_path = next((uh / ".pi").glob("**/*.jsonl"))
    cl_path = next((uh / ".claude" / "projects").glob("*/*.jsonl"))
    lab = home / "labels"
    lab.mkdir(parents=True)
    _write(lab / "labels.jsonl", [
        {"id": L._tid(pi_path, 0), "label": "success", "from": "weak"},
        {"id": L._tid(pi_path, 2), "label": "unknown", "from": "weak"},
        {"id": L._tid(cl_path, 0), "label": "fail", "from": "weak"},
    ], raw_lines=("broken\n",))
    _write(lab / "gold.jsonl", [{"id": L._tid(cl_path, 0), "outcome": "partial"}])
    man = S.build(home=home, user_home=uh, telemetry=tel)
    t = {r["task"]: r for r in _read(home / "worldmodel" / "tasks.jsonl")}
    assert (t[f"{SID_PI}:0"]["outcome"], t[f"{SID_PI}:0"]["outcome_src"]) == ("success", "weak")
    assert (t[f"{SID_PI}:2"]["outcome"], t[f"{SID_PI}:2"]["outcome_src"]) == ("unknown", "none")
    assert (t[f"{SID_CL}:0"]["outcome"], t[f"{SID_CL}:0"]["outcome_src"]) == ("partial", "gold")
    assert (t[f"{SID_CL}:1"]["outcome"], t[f"{SID_CL}:1"]["outcome_src"]) == ("unknown", "none")
    assert man["outcomes"] == {"gold": 1, "weak": 1, "none": 3}


def test_split_by_session_start_time(tmp_path):
    uh = tmp_path / "user"
    sids = [f"0000000{k}-0000-0000-0000-000000000000" for k in range(10)]
    order = [3, 7, 1, 9, 0, 5, 2, 8, 6, 4]                              # file order != time order
    for rank, k in enumerate(order):
        pi_session(uh, sids[k], T0 + 1000 * k, [("q", [("bash", {"command": "ls"}, False, "")])])
    home = tmp_path / "ar"
    man = S.build(home=home, user_home=uh, telemetry=tmp_path / "none.jsonl")
    tasks = {t["sid"]: t["split"] for t in _read(home / "worldmodel" / "tasks.jsonl")}
    assert [tasks[sids[k]] for k in range(10)] == ["train"] * 7 + ["val"] + ["test"] * 2
    assert man["splits"]["train"]["sessions"] == 7 and man["splits"]["test"]["sessions"] == 2
    steps = _read(home / "worldmodel" / "steps.jsonl")
    assert [s["sid"] for s in steps] == sids                            # emitted in time order
    S.build(home=home, user_home=uh, telemetry=tmp_path / "none.jsonl")
    assert {t["sid"]: t["split"] for t in _read(home / "worldmodel" / "tasks.jsonl")} == tasks


def test_subagent_follows_parent_split(tmp_path):
    man, steps, tasks, d, uh = _build(tmp_path)
    split = {t["sid"]: t["split"] for t in tasks}
    # two sessions: rank 0 < round(0.7*2) -> train, rank 1 < round(0.8*2) -> val
    assert split[SID_PI] == "train" and split[SID_CL] == "val"
    assert all(t["split"] == split[t["sid"]] for t in tasks)


def test_privacy_no_text(tmp_path):
    man, steps, tasks, d, uh = _build(tmp_path)
    for f in ("steps.jsonl", "tasks.jsonl", "manifest.json"):
        blob = (d / f).read_text()
        for needle in (SECRET, "pytest -q", "fix the parser", "investigate", "/w/a.py",
                       "cargo test", "subagent prompt", "harness noise"):
            assert needle not in blob, (f, needle)


def test_idempotent_and_permissions(tmp_path):
    uh, tel = _fixture(tmp_path)
    home = tmp_path / "ar"
    S.build(home=home, user_home=uh, telemetry=tel)
    d = home / "worldmodel"
    first = {f: (d / f).read_bytes() for f in ("steps.jsonl", "tasks.jsonl")}
    m1 = json.loads((d / "manifest.json").read_text())
    S.build(home=home, user_home=uh, telemetry=tel)
    assert {f: (d / f).read_bytes() for f in first} == first
    m2 = json.loads((d / "manifest.json").read_text())
    for k in ("counts", "classes", "splits", "tests", "outcomes", "model_source", "time_range"):
        assert m1[k] == m2[k]
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    for f in ("steps.jsonl", "tasks.jsonl", "manifest.json"):
        assert stat.S_IMODE(os.stat(d / f).st_mode) == 0o600
    assert not list(d.glob("*.tmp"))


def test_fail_open(tmp_path):
    uh = tmp_path / "user"
    p = pi_session(uh, SID_PI, T0, [("q", [("bash", {"command": "ls"}, False, "")])],
                   raw_lines=("\x00\x01garbage\n", '{"message": "not a dict"}\n', "null\n",
                              '{"type":"message","message":{"role":"assistant","content":[7]}}\n'))
    (uh / ".claude" / "projects" / "-w").mkdir(parents=True)
    (uh / ".claude" / "projects" / "-w" / "x.jsonl").write_text("")   # empty file
    home = tmp_path / "ar"
    man = S.build(home=home, user_home=uh, telemetry=tmp_path / "missing" / "t.jsonl")
    assert man["counts"]["steps"] == 1 and man["read_errors"]["malformed_lines"] >= 2
    assert p.exists()


def test_manifest_and_cli(tmp_path, monkeypatch, capsys):
    uh, tel = _fixture(tmp_path)
    monkeypatch.setenv("HOME", str(uh))
    monkeypatch.setenv("APEX_TELEMETRY", str(tel))
    home = tmp_path / "ar"
    assert WCLI.main(["build", "--home", str(home), "--json"]) == 0
    m = json.loads(capsys.readouterr().out)
    assert m["counts"]["steps"] == 7 + 5 + 7
    assert sum(m["classes"].values()) == m["counts"]["steps"]
    assert m["tests"]["test_steps"] == 4 and m["tests"]["parsed"] == 4
    assert m["tests"]["coverage"] == 1.0
    assert m["model_source"]["telemetry"] > 0 and m["model_source"]["transcript"] > 0
    assert m["time_range"]["first"].startswith("2026-")
    assert set(m["splits"]) == {"train", "val", "test"}
    assert WCLI.main(["stats", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "steps 19" in out and "classes:" in out
    assert WCLI.main(["stats", "--home", str(tmp_path / "empty")]) == 1


def test_top_level_cli_dispatch(tmp_path, monkeypatch, capsys):
    from apex_router import cli
    uh, tel = _fixture(tmp_path)
    monkeypatch.setenv("HOME", str(uh))
    monkeypatch.setenv("APEX_TELEMETRY", str(tel))
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path / "ar2"))
    assert cli.main(["worldmodel", "build"]) == 0
    assert (tmp_path / "ar2" / "worldmodel" / "steps.jsonl").exists()
    capsys.readouterr()


def test_task_type_and_workflow(tmp_path):
    from apex_router.route_resolve import EXEMPLARS
    axes = list(EXEMPLARS)
    vec = {ex: [1.0 if k == j else 0.0 for j in range(len(axes))]
           for k, cls in enumerate(axes) for ex in EXEMPLARS[cls]}
    seen = []

    def fake_embed(text):
        seen.append(text)
        if text in vec:
            return vec[text]
        if "parser" in text:                                    # looks like a debug request
            return [1.0 if c == "debug" else 0.0 for c in axes]
        if "second request" in text:
            raise RuntimeError("embedder down")
        return [1.0] * len(axes)                                # matches every class equally

    uh, tel = _fixture(tmp_path)
    home = tmp_path / "ar"
    man = S.build(home=home, user_home=uh, telemetry=tel, embed_fn=fake_embed)
    t = {r["task"]: r for r in _read(home / "worldmodel" / "tasks.jsonl")}
    assert t[f"{SID_PI}:0"]["task_type"] == "debug"
    assert t[f"{SID_PI}:1"]["task_type"] is None                # no clear lead -> null
    assert t[f"{SID_CL}:1"]["task_type"] is None                # embedder error -> null
    assert {r["workflow"] for r in t.values() if r["sid"] == SID_PI} == {"W0"}
    assert t[f"{SID_CL}:0"]["workflow"] == "W2" and t[f"{SID_CL}:1"]["workflow"] == "W0"
    assert man["task_types"]["debug"] == 1 and man["task_types"]["null"] == 4
    assert sum(man["task_types"].values()) == man["counts"]["tasks"]
    assert man["workflows"] == {"W0": 4, "W2": 1}
    assert sum(len(x) for x in seen) > 0
    for f in ("steps.jsonl", "tasks.jsonl", "manifest.json"):     # the text is never stored
        assert "fix the parser" not in (home / "worldmodel" / f).read_text()
    # no embedder: every task untyped, nothing else changes
    man2 = S.build(home=home, user_home=uh, telemetry=tel, embed_fn=None)
    assert man2["task_types"]["null"] == man2["counts"]["tasks"]
    assert man2["workflows"] == man["workflows"]


def test_phase_causal_in_dataset(tmp_path):
    """Cutting a transcript after any record leaves the earlier steps' phases unchanged."""
    man, steps, tasks, d, uh = _build(tmp_path)
    full = [(s["task"], s["i"], s["phase"]) for s in steps if s["src"] == "pi"]
    p = next((uh / ".pi").glob("**/*.jsonl"))
    lines = p.read_text().splitlines(keepends=True)
    for k in range(4, len(lines)):
        p.write_text("".join(lines[:k]))
        S.build(home=tmp_path / "cut", user_home=uh, telemetry=tmp_path / "none.jsonl",
                embed_fn=None)
        cut = [(s["task"], s["i"], s["phase"]) for s in
               _read(tmp_path / "cut" / "worldmodel" / "steps.jsonl") if s["src"] == "pi"]
        assert cut == full[:len(cut)]
