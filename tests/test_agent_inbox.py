"""Agent inbox (docs/design/AGENTS-INBOX.md sections 2-6). Every test uses a fixture home in tmp_path; nothing
reads the real ~/.apex-router, ~/.claude or ~/.pi."""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from apex_router import agent_inbox as ai
from apex_router import commands

NOW = 1793300000.0
SECRETS = ("sk-ant-abcdefghijklmnop1234", "ghp_ABCDEFGHIJKLMNOPQRSTUV123456",
           "hunter2pass", "AKIAABCDEFGHIJKLMNOP", "PROMPTBODYSENTINEL")


# ---- fixtures ---------------------------------------------------------------------------------------------

@pytest.fixture
def homes(tmp_path):
    home = tmp_path / "home"
    rh = home / ".apex-router"
    (rh / "pi-jobs").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".claude" / "jobs").mkdir(parents=True)
    commands.clear_caches()
    ai.clear_caches()
    return home, rh


def job_name(i: int, suffix: str = "abcd") -> str:
    t = time.gmtime(NOW - 100000 + i * 10)
    return time.strftime("%Y%m%d%H%M%S", t) + "-" + suffix


def mk_job(rh: Path, name: str, *, state="done", agent="worker", task="TASK: Do the thing.\nmore prompt text",
           result=None, started=None, ended=None, pid=None, cost=0.5, **extra) -> Path:
    d = rh / "pi-jobs" / name
    d.mkdir(parents=True, exist_ok=True)
    started = NOW - 500 if started is None else started
    meta = {"id": name, "state": state, "agent": agent, "kind": "xval" if agent == "xval" else "pi",
            "task": task, "cwd": "/Users/x/dev/apex-router",
            "argv": ["pi", "-p", task], "created": int(started * 1000), "started": int(started * 1000),
            "session_id": f"apexjob-{name}", "usage": {"turns": 3, "cost": cost},
            "resume": f'cd "/Users/x/dev/apex-router" && pi --session-id apexjob-{name}',
            "last_tool": "bash: {\"command\":\"export TOKEN=hunter2pass\"}",
            "final_preview": "PROMPTBODYSENTINEL preview"}
    if pid is not None:
        meta["pid"] = pid
    if ended is not None or state not in ("running", "queued"):
        meta["ended"] = int((ended if ended is not None else NOW - 100) * 1000)
    meta.update(extra)
    (d / "meta.json").write_text(json.dumps(meta))
    (d / "events.jsonl").write_text("")
    if result is not None:
        (d / "result.md").write_text(f"# job {name} — {state}\nagent {agent} · 100 s · 3 turns · $0.5\n\n" + result)
    return d


def mk_sub(home: Path, sid: str, aid: str, *, text="All done.", task="TASK: Review the diff.\nsecret body",
           age=1000.0, slug="-Users-x-dev-apex-router") -> Path:
    d = home / ".claude" / "projects" / slug / sid / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"agent-{aid}.jsonl"
    base = {"agentId": aid, "cwd": "/Users/x/dev/apex-router", "gitBranch": "main", "sessionId": sid,
            "isSidechain": True}
    rows = [dict(base, type="user", timestamp="2026-10-30T10:00:00Z",
                 message={"role": "user", "content": task}),
            dict(base, type="assistant", timestamp="2026-10-30T10:05:00Z",
                 message={"role": "assistant", "content": [{"type": "text", "text": text}]})]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    t = NOW - age
    os.utime(p, (t, t))
    os.utime(d, (t, t))
    return p


def mk_bg(home: Path, jid: str, *, state="blocked", needs="1) draft a request? 2) commit the guide?",
          detail="plan complete; awaiting go", output=None, **extra) -> Path:
    d = home / ".claude" / "jobs" / jid
    d.mkdir(parents=True, exist_ok=True)
    st = {"state": state, "detail": detail, "needs": needs, "output": output, "tokens": 860991,
          "name": "Integrate a framework", "intent": "PROMPTBODYSENTINEL do it",
          "sessionId": f"{jid}-06aa-4e28-8e09-04ddb8a10e35", "resumeSessionId": f"{jid}-5c80-47fe-a708-50b4b4848e10",
          "cwd": "/Users/x/dev/headroom", "createdAt": "2026-10-30T09:00:00.000Z",
          "updatedAt": "2026-10-30T10:00:00.000Z",
          "providerEnv": {"ANTHROPIC_AUTH_TOKEN": "sk-ant-abcdefghijklmnop1234"}}
    st.update(extra)
    (d / "state.json").write_text(json.dumps(st))
    return d


def collect(home, rh, **kw):
    kw.setdefault("now", NOW)
    return ai.collect(home=home, router_home=rh, **kw)


def item(doc, iid):
    return next(i for i in doc["items"] if i["id"] == iid)


def run_cli(argv, home, rh):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ai.main(argv, home=home, router_home=rh, now=NOW)
    return rc, buf.getvalue()


# ---- 2. blocker parse (result footer, sections), verdict --------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ("body\nNO-GO\n", "NO-GO"),
    ("body\nGO\n", "GO"),
    ("body\nBLOCKED\n", "BLOCKED"),
    ("body\n**Verdict: NO-GO.**\n", "NO-GO"),
    ("body\nVERDICT: GO-WITH-CHANGES\n", "GO"),
    ("body\n**Verdict: GO-WITH-CHANGES**, limited to the coverage above.\n", "GO"),
    ("VERDICT: NO-GO\nmore text\nand more\nand more\n", "NO-GO"),
    ("## OPEN\n- none\nVERDICT: DONE\n", "DONE"),
    ("Read-only review; no files changed.\nNo push performed.\nThe tests were not run.\n", None),
    ("body\nVERDICT: AGREE — GO-WITH-CHANGES. Fix M4.\n", None),
    ("We go on with it. Nothing is blocked here.\n", None),
    ("## Verdict\nNO-GO\n", "NO-GO"),
])
def test_verdict_real_forms(text, want):
    assert ai.parse_verdict(text) == want


def test_verdict_label_beats_the_last_lines():
    assert ai.parse_verdict("VERDICT: NO-GO\n\nx\ny\nGO\n") == "NO-GO"


def test_verdict_last_three_lines_only():
    assert ai.parse_verdict("NO-GO\na\nb\nc\n") is None


def test_open_footer_with_priorities():
    text = "body\n**WARNING:** ignore me\n## OPEN\n- [P1] fix the lock\n- [P2] add a test\nVERDICT: NO-GO\n"
    got = ai.parse_blockers(text)
    assert [(b["pri"], b["text"]) for b in got] == [("P1", "fix the lock"), ("P2", "add a test")]


@pytest.mark.parametrize("text", ["## OPEN\n- none\nVERDICT: DONE\n", "## OPEN\nVERDICT: GO\n",
                                  "x\nOPEN: none.\n", "## OPEN\n\nNone.\n"])
def test_open_footer_none_means_no_blockers(text):
    assert ai.parse_blockers(text) == []


def test_bold_open_numbered_list():                              # 20261029152627-3okb form
    text = ("**TESTED**\n| a | b |\n|---|---|\n| x | y |\n\n**OPEN**\n1. Step 21 shots are old.\n"
            "2. The checklist uses the last 6 h.\n3. Files keep old names.\n")
    got = ai.parse_blockers(text)
    assert [b["text"] for b in got] == ["Step 21 shots are old.", "The checklist uses the last 6 h.",
                                        "Files keep old names."]


def test_open_table_rows():                                      # 20261029171340-f9ys form
    text = ("## NOT DONE\n1. a\n\n## OPEN\n\n| # | Item | Owner |\n|---|---|---|\n"
            "| O1 | Does Q3a work on RHEL 8? | Ivan |\n| O2 | Codename map: confirm. | Owner |\n")
    got = ai.parse_blockers(text)
    assert len(got) == 2 and "Does Q3a work on RHEL 8?" in got[0]["text"] and "O1" in got[0]["text"]


def test_open_items_heading_and_bold_colon():                    # kkip, a6fl forms
    a = ai.parse_blockers("x\n**Open items**\n1. Ops must check it.\n2. Decide the clean up.\n\n"
                          "**Tested:** all ok.\n\nCHANGED (all):\n- y\n")
    assert [b["text"] for b in a] == ["Ops must check it.", "Decide the clean up."]
    b = ai.parse_blockers("**TESTED:**\n| a |\n\n**OPEN:**\n1. **To unblock:** Ops must run a rake task.\n"
                          "2. Option without Ops.\n")
    assert [x["text"] for x in b] == ["To unblock: Ops must run a rake task.", "Option without Ops."]


def test_open_plain_label_list():                                # 0t8z form
    got = ai.parse_blockers("x\nOPEN:\n- O1 is still open.\n- The console bug is still there.\n")
    assert len(got) == 2


def test_bare_open_line_with_a_list():                            # ak9j, 6co5, j1j8 forms
    got = ai.parse_blockers("body\n\nOPEN\n- `DEMO/script.md` still says the old name.\n- Step 11 shot was not retaken.\n")
    assert len(got) == 2
    assert len(ai.parse_blockers("x\nOPEN\n1. The UI still has the row.\n2. Jest count is 872.\n")) == 2
    assert ai.parse_blockers("x\nBLOCKED\n") == [] and ai.parse_verdict("x\nBLOCKED\n") == "BLOCKED"


def test_nested_sub_items_belong_to_the_parent():                # j9fa form
    got = ai.parse_blockers("## OPEN\n\n1. **bench_ objects are still in org 1:**\n   - 60 simulated VENs.\n"
                            "   - Rule sets.\n2. **Bundles are not deleted.**\n6. **Plan:**\n   1. Deploy B1.\n")
    assert [b["text"] for b in got] == ["bench_ objects are still in org 1:", "Bundles are not deleted.", "Plan:"]


def test_open_questions_heading():                               # aveo form
    got = ai.parse_blockers("## Open questions\n1. Which branch merges first?\n")
    assert [b["text"] for b in got] == ["Which branch merges first?"]


def test_warning_inline_and_section_when_no_footer():            # 1fpo, i2xs forms
    text = ("**WARNING:** The PCE on devtest22 is down.\nmore of the warning\n\n"
            "**WARNING:** Another agent added 3 commits.\n\nreport body\n| t | BLOCKED (by the task rules) |\n")
    got = ai.parse_blockers(text)
    assert [b["text"] for b in got] == ["The PCE on devtest22 is down.", "Another agent added 3 commits."]


def test_blocked_and_caution_sections():
    assert [b["text"] for b in ai.parse_blockers("## BLOCKED\n- no key for the box\n")] == ["no key for the box"]
    assert [b["text"] for b in ai.parse_blockers("CAUTION: the disk is 97 % full.\n")] == ["the disk is 97 % full."]


def test_footer_beats_sections():
    text = "**WARNING:** old warning\n\n## OPEN\n- [P3] one thing\nVERDICT: GO\n"
    assert [b["text"] for b in ai.parse_blockers(text)] == ["one thing"]


def test_no_section_no_blockers():
    assert ai.parse_blockers("All done.\nOpening the file worked.\nVERDICT: GO\n") == []


def test_blocker_text_is_masked_one_line_160():
    text = "## OPEN\n- [P1] set TOKEN=hunter2pass and key sk-ant-abcdefghijklmnop1234 " + "x " * 200 + "\n"
    (b,) = ai.parse_blockers(text)
    assert "hunter2pass" not in b["text"] and "sk-ant" not in b["text"] and "[hidden]" in b["text"]
    assert len(b["text"]) <= 160 and "\n" not in b["text"]


def test_stderr_tail_is_not_a_section():
    text = "body\n## OPEN\n- none\nVERDICT: GO\n\n## stderr (tail)\nWarning: No project session found\nNO-GO\n"
    assert ai.parse_blockers(text) == [] and ai.parse_verdict(text) == "GO"


# ---- 1. sources and the contract shape --------------------------------------------------------------------

ITEM_KEYS = {"id", "kind", "role", "group", "state", "verdict", "label", "cwd_short", "branch", "ahead",
             "start", "end", "cost", "open", "resume", "stale"}


def test_contract_shape_for_each_kind(homes):
    home, rh = homes
    mk_job(rh, job_name(1), result="## OPEN\n- [P1] fix it\nVERDICT: NO-GO\n", agent="xval")
    mk_sub(home, "11111111-2222-3333-4444-555555555555", "a1b2c3d4e5f6a7b8c",
           text="Done.\n## OPEN\n- [P2] a test is missing\n")
    mk_bg(home, "7da8ec27")
    doc = collect(home, rh)
    assert set(doc) >= {"now", "complete", "counts", "items"} and doc["complete"] is True
    assert set(doc["counts"]) == {"running", "needs", "done"}
    kinds = {i["kind"] for i in doc["items"]}
    assert kinds == {"pi-job", "claude-sub", "claude-bg"}
    for it in doc["items"]:
        assert ITEM_KEYS <= set(it), it
        assert it["group"] in ("needs", "running", "done")
        assert it["role"] in ("xval", "worker", "reviewer", "scout", "claude", "other")
        assert it["state"] in ("running", "done", "failed", "capped", "blocked", "cancelled", "timed_out")
        assert it["verdict"] in ("GO", "NO-GO", "BLOCKED", "DONE", None)
        assert len(it["label"]) <= 60
        for o in it["open"]:
            assert re.fullmatch(r"[0-9a-f]{16}", o["key"]) and o["pri"] in ("P1", "P2", "P3", None)
            assert o["resolved"] is False and len(o["text"]) <= 160
    job = item(doc, job_name(1))
    assert job["role"] == "xval" and job["verdict"] == "NO-GO" and job["group"] == "needs"
    assert job["label"].startswith("Do the thing") and job["cost"] == 0.5
    assert job["resume"].startswith("cd ") and "pi --session-id" in job["resume"]
    sub = item(doc, "a1b2c3d4e5f6a7b8c")
    assert sub["role"] == "claude" and sub["state"] == "done" and sub["group"] == "needs"
    assert sub["label"].startswith("Review the diff") and sub["open"][0]["pri"] == "P2"
    bg = item(doc, "7da8ec27")
    assert bg["state"] == "blocked" and bg["group"] == "needs"
    assert [o["text"] for o in bg["open"]][:2] == ["draft a request?", "commit the guide?"]


def test_json_cli_output_parses(homes):
    home, rh = homes
    mk_job(rh, job_name(1), result="All good.\nVERDICT: GO\n")
    rc, out = run_cli(["--json"], home, rh)
    doc = json.loads(out)
    assert rc == 0 and doc["items"][0]["group"] == "done" and doc["counts"]["done"] == 1


def test_claude_sub_running_by_mtime(homes):
    home, rh = homes
    mk_sub(home, "s1", "aaaa1111", age=30)
    mk_sub(home, "s1", "bbbb2222", age=500)
    doc = collect(home, rh)
    assert item(doc, "aaaa1111")["state"] == "running" and item(doc, "aaaa1111")["group"] == "running"
    assert item(doc, "bbbb2222")["state"] == "done"


def test_pi_job_states(homes):
    home, rh = homes
    states = {"failed": "failed", "killed(cost)": "capped", "killed": "cancelled",
              "timed_out(idle)": "timed_out", "timed_out(wall)": "timed_out"}
    for i, s in enumerate(states):
        mk_job(rh, job_name(i, f"s{i}"), state=s, result="x\n")
    mk_job(rh, job_name(9, "cap"), state="done", capped=True, result="x\n")
    mk_job(rh, job_name(10, "dead"), state="running", pid=999999)      # pid gone: not running
    mk_job(rh, job_name(11, "live"), state="running", pid=os.getpid())
    doc = collect(home, rh)
    for i, s in enumerate(states):
        it = item(doc, job_name(i, f"s{i}"))
        assert it["state"] == states[s] and it["group"] == "needs"
    assert item(doc, job_name(9, "cap"))["state"] == "capped"
    assert item(doc, job_name(10, "dead"))["group"] == "needs"
    assert item(doc, job_name(11, "live"))["group"] == "running"


def test_now_only_on_running_items(homes, monkeypatch):
    home, rh = homes
    calls = []

    def fake(agent, **kw):
        calls.append(agent)
        return {"tool": "bash", "short": "pytest -q", "masked": False}
    monkeypatch.setattr(ai.commands, "running_now", fake)
    mk_job(rh, job_name(1, "run"), state="running", pid=os.getpid())
    mk_job(rh, job_name(2, "done"), result="x\n")
    doc = collect(home, rh)
    assert item(doc, job_name(1, "run"))["now"] == {"tool": "bash", "short": "pytest -q"}
    assert "now" not in item(doc, job_name(2, "done")) or item(doc, job_name(2, "done"))["now"] is None
    assert calls == [job_name(1, "run")]


def test_bg_states_and_needs_split(homes):
    home, rh = homes
    mk_bg(home, "aaaa0001", state="done", needs=None, detail="all finished")
    mk_bg(home, "aaaa0002", state="running", needs=None, detail="working")
    mk_bg(home, "aaaa0003", state="blocked", needs="- Something else about the plan?",
          detail="- Something else about the plan?")
    doc = collect(home, rh)
    assert item(doc, "aaaa0001")["group"] == "done" and item(doc, "aaaa0001")["open"] == []
    assert item(doc, "aaaa0002")["group"] == "running"
    assert [o["text"] for o in item(doc, "aaaa0003")["open"]] == ["Something else about the plan?"]


# ---- 4. group rules, order, bounds ------------------------------------------------------------------------

def test_group_rules(homes):
    home, rh = homes
    mk_job(rh, job_name(1, "go"), result="ok\nVERDICT: GO\n")
    mk_job(rh, job_name(2, "nogo"), result="bad\nNO-GO\n")
    mk_job(rh, job_name(3, "blk"), result="x\nBLOCKED\n")
    mk_job(rh, job_name(4, "open"), result="## OPEN\n- [P2] a\nVERDICT: GO\n")
    mk_job(rh, job_name(5, "none"), result="## OPEN\n- none\nVERDICT: DONE\n")
    doc = collect(home, rh)
    g = {i["id"].split("-")[1]: i["group"] for i in doc["items"]}
    assert g == {"go": "done", "nogo": "needs", "blk": "needs", "open": "needs", "none": "done"}
    assert doc["counts"] == {"running": 0, "needs": 3, "done": 2}


def test_order_needs_running_done(homes):
    home, rh = homes
    mk_job(rh, job_name(1, "n1"), state="failed", ended=NOW - 300, result="x\n")
    mk_job(rh, job_name(2, "n2"), state="failed", ended=NOW - 100, result="x\n")
    mk_job(rh, job_name(3, "r1"), state="running", pid=os.getpid(), started=NOW - 900)
    mk_job(rh, job_name(4, "r2"), state="running", pid=os.getpid(), started=NOW - 50)
    mk_job(rh, job_name(5, "d1"), ended=NOW - 400, result="ok\n")
    mk_job(rh, job_name(6, "d2"), ended=NOW - 10, result="ok\n")
    doc = collect(home, rh)
    assert [i["id"].split("-")[1] for i in doc["items"]] == ["n2", "n1", "r1", "r2", "d2", "d1"]


def test_3000_job_dirs_stay_in_bounds(homes):
    home, rh = homes
    for i in range(3000):
        mk_job(rh, job_name(i, f"j{i}"), result="ok\nVERDICT: GO\n", task="TASK: t")
    t0 = time.monotonic()
    doc = collect(home, rh, deadline_s=30)
    took = time.monotonic() - t0
    assert len(doc["items"]) == 200
    assert sum(doc["counts"].values()) == 2000          # the 2000 newest dirs only
    assert doc["items"][0]["id"] == job_name(2999, "j2999")
    assert took < 15, took


def test_running_items_stay_when_needs_fill_the_200(homes):
    home, rh = homes
    for i in range(250):
        mk_job(rh, job_name(i, f"f{i}"), state="failed", result="x\n")
    mk_job(rh, job_name(300, "run"), state="running", pid=os.getpid())
    doc = collect(home, rh)
    assert len(doc["items"]) == 200 and doc["counts"] == {"running": 1, "needs": 250, "done": 0}
    assert [i["group"] for i in doc["items"]][-1] == "running"
    assert doc["items"][-1]["id"] == job_name(300, "run")


def test_50mb_result_reads_only_the_tail(homes):
    home, rh = homes
    d = mk_job(rh, job_name(1, "big"))
    with open(d / "result.md", "wb") as f:
        f.write(b"# job x \xe2\x80\x94 done\n")
        f.truncate(50 * 1024 * 1024)
        f.seek(0, 2)
        f.write(b"\n**WARNING:** early text " + b"y" * 9000 + b"\n## OPEN\n- [P1] tail item\nVERDICT: NO-GO\n")
    t0 = time.monotonic()
    doc = collect(home, rh)
    assert time.monotonic() - t0 < 2
    it = item(doc, job_name(1, "big"))
    assert [o["text"] for o in it["open"]] == ["tail item"] and it["verdict"] == "NO-GO"


def test_oversized_meta_is_skipped(homes):
    home, rh = homes
    d = rh / "pi-jobs" / job_name(1, "huge")
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"state": "done", "task": "x" * (300 * 1024)}))
    mk_job(rh, job_name(2, "ok"), result="ok\n")
    doc = collect(home, rh)
    assert [i["id"] for i in doc["items"]] == [job_name(2, "ok")]


def test_deadline_gives_complete_false_and_returns(homes, monkeypatch):
    home, rh = homes
    for i in range(50):
        mk_job(rh, job_name(i, f"j{i}"), result="ok\n")
    real = ai._read_tail

    def slow(*a, **kw):
        time.sleep(0.05)
        return real(*a, **kw)
    monkeypatch.setattr(ai, "_read_tail", slow)
    t0 = time.monotonic()
    doc = collect(home, rh, deadline_s=0.3)
    assert time.monotonic() - t0 < 1.5
    assert doc["complete"] is False and len(doc["items"]) < 50


def test_a_stuck_read_never_hangs(homes, monkeypatch):
    home, rh = homes
    mk_job(rh, job_name(1), result="ok\n")
    ev = threading.Event()
    monkeypatch.setattr(ai, "_read_tail", lambda *a, **kw: ev.wait(5) and None)
    t0 = time.monotonic()
    doc = collect(home, rh, deadline_s=0.3)
    ev.set()
    assert time.monotonic() - t0 < 1.5 and doc["complete"] is False


def test_a_fifo_result_is_not_read(homes):
    home, rh = homes
    d = mk_job(rh, job_name(1))
    os.mkfifo(d / "result.md")
    t0 = time.monotonic()
    doc = collect(home, rh)
    assert time.monotonic() - t0 < 1.5 and item(doc, job_name(1))["open"] == []


def test_claude_bounds(homes):
    home, rh = homes
    for i in range(230):
        mk_bg(home, f"{i:08x}", state="done", needs=None)
    doc = collect(home, rh)
    assert sum(1 for i in doc["items"] if i["kind"] == "claude-bg") == min(200, 200)
    assert doc["counts"]["done"] == 200


# ---- 5. resolve -------------------------------------------------------------------------------------------

def acks(rh):
    return json.loads((rh / "agent-acks.json").read_text())


def test_resolve_key_then_all_then_new_key_shows_again(homes):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, result="## OPEN\n- [P1] a\n- [P2] b\nVERDICT: NO-GO\n")
    it = item(collect(home, rh), name)
    k1 = it["open"][0]["key"]
    assert run_cli(["resolve", name, k1], home, rh)[0] == 0
    it = item(collect(home, rh), name)
    assert [o["resolved"] for o in it["open"]] == [True, False] and it["group"] == "needs"
    assert run_cli(["resolve", name, "--all"], home, rh)[0] == 0
    it = item(collect(home, rh), name)
    assert all(o["resolved"] for o in it["open"]) and it["group"] == "done"
    e = acks(rh)[name]
    assert e["all"] is True and k1 in e["keys"] and isinstance(e["at"], (int, float))
    (rh / "pi-jobs" / name / "result.md").write_text("## OPEN\n- [P1] a\n- [P2] b\n- [P1] c new\nVERDICT: NO-GO\n")
    it = item(collect(home, rh), name)
    assert it["group"] == "needs" and [o["resolved"] for o in it["open"]] == [True, True, False]


def test_new_verdict_after_resolve_shows_again(homes):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, result="x\nNO-GO\n")
    run_cli(["resolve", name, "--all"], home, rh)
    assert item(collect(home, rh), name)["group"] == "done"
    (rh / "pi-jobs" / name / "result.md").write_text("x\nBLOCKED\n")
    assert item(collect(home, rh), name)["group"] == "needs"


def test_resolve_failed_state(homes):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, state="failed", result="x\n")
    run_cli(["resolve", name, "--all"], home, rh)
    it = item(collect(home, rh), name)
    assert it["state"] == "failed" and it["group"] == "done"


def test_resolve_undo(homes):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, result="x\nNO-GO\n")
    run_cli(["resolve", name, "--all"], home, rh)
    assert run_cli(["resolve", name, "--undo"], home, rh)[0] == 0
    assert name not in acks(rh) and item(collect(home, rh), name)["group"] == "needs"


def test_resolve_unknown_id_or_key_fails_and_writes_nothing(homes):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, result="## OPEN\n- a\n")
    assert run_cli(["resolve", "20991231000000-zzzz", "--all"], home, rh)[0] != 0
    assert run_cli(["resolve", name, "0123456789abcdef"], home, rh)[0] != 0
    assert run_cli(["resolve", "../../etc", "--all"], home, rh)[0] != 0
    assert not (rh / "agent-acks.json").exists()


def test_acks_hold_at_most_2000_ids_oldest_dropped(homes):
    home, rh = homes
    big = {f"20260101000000-{i:04d}": {"keys": [], "all": True, "at": 1000 + i} for i in range(2000)}
    (rh / "agent-acks.json").write_text(json.dumps(big))
    name = job_name(1)
    mk_job(rh, name, result="x\nNO-GO\n")
    assert run_cli(["resolve", name, "--all"], home, rh)[0] == 0
    a = acks(rh)
    assert len(a) == 2000 and name in a and "20260101000000-0000" not in a


def test_corrupt_acks_is_not_overwritten(homes):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, result="x\nNO-GO\n")
    (rh / "agent-acks.json").write_text("{not json")
    assert run_cli(["resolve", name, "--all"], home, rh)[0] != 0
    assert (rh / "agent-acks.json").read_text() == "{not json"
    assert item(collect(home, rh), name)["group"] == "needs"        # read side: fail open to "unresolved"


def test_failed_replace_keeps_the_old_file(homes, monkeypatch):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, result="x\nNO-GO\n")
    (rh / "agent-acks.json").write_text("{}")

    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(ai.os, "replace", boom)
    assert run_cli(["resolve", name, "--all"], home, rh)[0] != 0
    assert (rh / "agent-acks.json").read_text() == "{}"
    assert [p.name for p in rh.iterdir() if p.name.startswith("agent-acks") and p.name != "agent-acks.json"
            and not p.name.endswith(".lock")] == []


def test_parallel_resolves_lose_nothing(homes):
    home, rh = homes
    names = [job_name(i, f"p{i}") for i in range(12)]
    for n in names:
        mk_job(rh, n, result="x\nNO-GO\n")
    errs = []

    def go(n):
        try:
            assert ai.ack_write(rh, n, keys=["0123456789abcdef"], all_=True, verdict="NO-GO", state="done",
                                now=NOW) is True
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go, args=(n,)) for n in names]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert not errs and set(acks(rh)) == set(names)


# ---- 6. surfaces -------------------------------------------------------------------------------------------

def test_table_needs_first(homes):
    home, rh = homes
    mk_job(rh, job_name(1, "done"), result="ok\nGO\n")
    mk_job(rh, job_name(2, "fail"), state="failed", result="x\n")
    rc, out = run_cli([], home, rh)
    assert rc == 0
    assert out.index(job_name(2, "fail")) < out.index(job_name(1, "done"))


def test_show(homes):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, result="## OPEN\n- [P1] fix the lock\nVERDICT: NO-GO\n", branch="apexjob/abc")
    rc, out = run_cli(["show", name], home, rh)
    assert rc == 0
    for want in ("fix the lock", "NO-GO", "Do the thing", "apexjob/abc", "0.5", "pi --session-id"):
        assert want in out, want
    assert run_cli(["show", "20991231000000-zzzz"], home, rh)[0] != 0


def test_line_and_cache(homes, monkeypatch):
    home, rh = homes
    mk_job(rh, job_name(1, "r"), state="running", pid=os.getpid())
    mk_job(rh, job_name(2, "f"), state="failed", result="x\n")
    seen = []
    real = ai.collect

    def spy(**kw):
        seen.append(kw.get("deadline_s"))
        return real(**kw)
    monkeypatch.setattr(ai, "collect", spy)
    assert ai.status_line(home=home, router_home=rh, now=NOW) == "● 1 ⚠ 1"
    assert ai.status_line(home=home, router_home=rh, now=NOW + 3) == "● 1 ⚠ 1"
    assert seen == [1.0]
    ai.status_line(home=home, router_home=rh, now=NOW + 6)
    assert len(seen) == 2


def test_line_empty_when_nothing(homes):
    home, rh = homes
    mk_job(rh, job_name(1), result="ok\nGO\n")
    assert ai.status_line(home=home, router_home=rh, now=NOW) == ""


def test_new_blockers_state_file_and_max_5(homes):
    home, rh = homes
    for i in range(8):
        mk_job(rh, job_name(i, f"b{i}"), result=f"## OPEN\n- [P1] item {i}\n")
    lines = ai.new_blockers(home=home, router_home=rh, now=NOW)
    assert len(lines) == 5 and "4 more agents" in lines[-1] and (rh / "agent-inbox-seen.json").exists()
    assert ai.new_blockers(home=home, router_home=rh, now=NOW + 1) == []
    (rh / "pi-jobs" / job_name(3, "b3") / "result.md").write_text("## OPEN\n- [P1] item 3\n- [P2] brand new\n")
    lines = ai.new_blockers(home=home, router_home=rh, now=NOW + 2)
    assert len(lines) == 1 and "brand new" in lines[0]


def test_new_blockers_one_line_per_item(homes):
    home, rh = homes
    mk_job(rh, job_name(1), result="## OPEN\n- [P1] a\n- [P2] b\n- [P2] c\nVERDICT: NO-GO\n")
    lines = ai.new_blockers(home=home, router_home=rh, now=NOW)
    assert len(lines) == 1 and lines[0].endswith("(+3)")


def test_new_blockers_skips_resolved(homes):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, result="## OPEN\n- [P1] a\n")
    run_cli(["resolve", name, "--all"], home, rh)
    assert ai.new_blockers(home=home, router_home=rh, now=NOW) == []


def test_cli_subcommand_is_registered(homes, monkeypatch):
    home, rh = homes
    from apex_router import cli
    got = []
    monkeypatch.setattr(ai, "main", lambda argv, **kw: got.append(argv) or 0)
    assert cli.main(["agents", "--json"]) == 0
    assert got == [["--json"]]


# ---- 6/9. no prompt text, full command or token in any output -------------------------------------------

def test_no_secret_or_prompt_text_in_any_output(homes):
    home, rh = homes
    task = ("TASK: deploy with TOKEN=hunter2pass and sk-ant-abcdefghijklmnop1234\n"
            "PROMPTBODYSENTINEL ghp_ABCDEFGHIJKLMNOPQRSTUV123456\n")
    res = ("PROMPTBODYSENTINEL body\n**WARNING:** key AKIAABCDEFGHIJKLMNOP leaked\n\n"
           "## OPEN\n- [P1] rotate PASSWORD=hunter2pass now\n- [P2] Bearer sk-ant-abcdefghijklmnop1234\n"
           "VERDICT: NO-GO\n")
    name = job_name(1)
    mk_job(rh, name, task=task, result=res, branch="feat/x")
    mk_job(rh, job_name(2, "run"), state="running", pid=os.getpid(), task=task)
    mk_sub(home, "s1", "aaaa1111", task=task, text=res)
    mk_bg(home, "bbbb2222", needs="1) set API_KEY=hunter2pass 2) PROMPTBODYSENTINEL? no: ghp_ABCDEFGHIJKLMNOPQRSTUV123456",
          detail="token sk-ant-abcdefghijklmnop1234", output={"result": "PROMPTBODYSENTINEL"})
    outs = [run_cli(a, home, rh)[1] for a in (["--json"], [], ["show", name], ["show", "aaaa1111"],
                                               ["show", "bbbb2222"])]
    outs.append(ai.status_line(home=home, router_home=rh, now=NOW))
    outs.append("\n".join(ai.new_blockers(home=home, router_home=rh, now=NOW)))
    blob = "\n".join(outs)
    for s in SECRETS[:4]:
        assert s not in blob, s
    assert "deploy with TOKEN=[hidden]" in blob                # the TASK line, masked
    assert "PROMPTBODYSENTINEL" not in json.dumps(json.loads(outs[0])["items"][0]["label"])
    for it in json.loads(outs[0])["items"]:
        assert "PROMPTBODYSENTINEL" not in it["label"]
        assert "PROMPTBODYSENTINEL" not in (it["resume"] or "")
    for f in rh.iterdir():                                       # state and cache files hold no secret
        if f.is_file():
            body = f.read_text(errors="replace")
            for sec in SECRETS[:4]:
                assert sec not in body, (f.name, sec)


# ---- parse cache (stat is cheap, open is not) ------------------------------------------------------------

def test_cache_hit_skips_reads_and_a_change_invalidates(homes, monkeypatch):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, result="## OPEN\n- [P1] a\n")
    assert item(collect(home, rh), name)["open"][0]["text"] == "a"
    assert (rh / "agent-inbox-cache.json").exists()
    reads = []
    real = ai._read_tail
    monkeypatch.setattr(ai, "_read_tail", lambda *a, **kw: reads.append(a[0]) or real(*a, **kw))
    assert item(collect(home, rh), name)["open"][0]["text"] == "a" and reads == []
    (rh / "pi-jobs" / name / "result.md").write_text("## OPEN\n- [P1] a changed\n")
    assert item(collect(home, rh), name)["open"][0]["text"] == "a changed" and len(reads) == 1


def test_running_items_are_not_cached(homes):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, state="running", pid=os.getpid())
    collect(home, rh)
    c = rh / "agent-inbox-cache.json"
    assert not c.exists() or ("p:" + name) not in json.loads(c.read_text())["e"]


def test_corrupt_cache_is_ignored(homes):
    home, rh = homes
    name = job_name(1)
    mk_job(rh, name, result="x\nNO-GO\n")
    (rh / "agent-inbox-cache.json").write_text("{bad")
    assert item(collect(home, rh), name)["verdict"] == "NO-GO"


# ---- D27 (open decision): an ended item with blockers older than 48 h is "done" with stale=true ------------

def test_an_old_item_with_blockers_is_done_and_stale(homes):
    home, rh = homes
    old = mk_job(rh, job_name(1), result="## OPEN\n- [P1] fix it\nVERDICT: NO-GO\n", agent="xval",
                 started=NOW - 50 * 3600, ended=NOW - 49 * 3600)
    new = mk_job(rh, job_name(2), result="## OPEN\n- [P1] fix it\nVERDICT: NO-GO\n", agent="xval",
                 started=NOW - 3 * 3600, ended=NOW - 2 * 3600)
    failed_old = mk_job(rh, job_name(3), state="failed", started=NOW - 80 * 3600, ended=NOW - 79 * 3600)
    doc = ai.collect(home=home, router_home=rh, now=NOW)
    by = {i["id"]: i for i in doc["items"]}
    assert by[old.name]["group"] == "done" and by[old.name]["stale"] is True
    assert by[old.name]["open"] and by[old.name]["verdict"] == "NO-GO"        # blockers stay visible
    assert by[failed_old.name]["group"] == "done" and by[failed_old.name]["stale"] is True
    assert by[new.name]["group"] == "needs" and by[new.name]["stale"] is False
    assert doc["counts"]["needs"] == 1



# ---- xval x2e6 ----------------------------------------------------------------------------------------------

def test_result_lines_are_masked_and_hide_briefs_and_code(homes):
    home, rh = homes
    d = mk_job(rh, job_name(1), result="Prompt: PRIVATE_PROMPT_SENTINEL\nTASK: secret brief\nAPI_TOKEN=abc123\n"
               "```\ncurl -H 'Authorization: Bearer abc' https://x\n```\nplain line\n")
    out = ai.result_lines(rh, d.name)
    text = "\n".join(out)
    assert "PRIVATE_PROMPT_SENTINEL" not in text and "secret brief" not in text and "abc123" not in text
    assert "Bearer abc" not in text and "plain line" not in text


@pytest.mark.parametrize("body,secret", [
    ("PROMPT:\nPRIVATE_PROMPT_SENTINEL\n", "PRIVATE_PROMPT_SENTINEL"),            # xval e2tw: multi-line prompt
    ("~~~sh\nprintf REPRO_COMMAND\n~~~\n", "REPRO_COMMAND"),                     # xval e2tw: tilde fence
    ("x" * 70000 + "\n```\n- REPRO_COMMAND in a list line of a code block\n```\n", None),
    ("- run `curl -H x https://h/REPRO_COMMAND`\n", "REPRO_COMMAND"),              # inline code with blanks
    ("    - indented REPRO_COMMAND\n", "REPRO_COMMAND"),
])
def test_result_lines_show_only_report_structure(homes, body, secret):
    home, rh = homes
    d = mk_job(rh, job_name(1), result=body + "## OPEN\n- [P1] fix `commands.py:113`\n| a | b |\nVERDICT: NO-GO\n")
    out = ai.result_lines(rh, d.name)
    text = "\n".join(out)
    if secret:
        assert secret not in text
    assert "- [P1] fix `commands.py:113`" in out and "| a | b |" in out and "VERDICT: NO-GO" in out
    assert ai.result_lines(rh, "../../etc") is None and ai.result_lines(rh, job_name(9)) is None


def test_result_lines_refuse_a_symlinked_job_dir_and_a_fifo(homes, tmp_path):
    home, rh = homes
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.md").write_text("OUTSIDE_SENTINEL\n")
    (rh / "pi-jobs").mkdir(parents=True, exist_ok=True)
    os.symlink(outside, rh / "pi-jobs" / job_name(2))
    assert ai.result_lines(rh, job_name(2)) is None
    d = rh / "pi-jobs" / job_name(3)
    d.mkdir()
    os.mkfifo(d / "result.md")
    t = time.monotonic()
    assert ai.result_lines(rh, job_name(3)) is None and time.monotonic() - t < 1


def test_a_symlinked_pi_job_dir_is_not_listed(homes, tmp_path):
    home, rh = homes
    src = mk_job(tmp_path / "elsewhere", job_name(4), result="## OPEN\n- [P1] OUTSIDE\nVERDICT: NO-GO\n")
    (rh / "pi-jobs").mkdir(parents=True, exist_ok=True)
    os.symlink(src, rh / "pi-jobs" / job_name(4))
    doc = ai.collect(home=home, router_home=rh, now=NOW)
    assert all(i["id"] != job_name(4) for i in doc["items"])


def test_a_symlinked_subagents_dir_is_not_read(homes, tmp_path):
    home, rh = homes
    real = tmp_path / "real-subagents"
    real.mkdir()
    (real / "agent-a1b2c3d4e5f6a7b8c.jsonl").write_text("{}\n")
    sess = home / ".claude" / "projects" / "-x" / "11111111-2222-3333-4444-555555555555"
    sess.mkdir(parents=True)
    os.symlink(real, sess / "subagents")
    doc = ai.collect(home=home, router_home=rh, now=NOW)
    assert not [i for i in doc["items"] if i["kind"] == "claude-sub"]


def test_the_acks_file_stays_under_the_read_cap(homes):
    home, rh = homes
    keys = [f"{i:016x}" for i in range(ai.ACK_KEYS_MAX)]
    for n in range(1100):
        assert ai.ack_write(rh, job_name(n, f"{n:04d}"), keys=keys, now=NOW + n)
    assert os.path.getsize(rh / "agent-acks.json") <= ai.ACKS_FILE_MAX
    acks = ai.ack_read(rh)
    assert isinstance(acks, dict) and job_name(1099, "1099") in acks and job_name(0, "0000") not in acks
    assert ai.ack_write(rh, job_name(5000, "zzzz"), keys=keys[:1], now=NOW + 5000)
    assert job_name(5000, "zzzz") in ai.ack_read(rh)


def test_the_cache_save_waits_at_most_the_cache_lock_wait(homes, monkeypatch):
    home, rh = homes
    mk_job(rh, job_name(1), result="VERDICT: GO\n")
    import fcntl
    rh.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(rh / "agent-inbox-cache.json") + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t = time.monotonic()
        ai.collect(home=home, router_home=rh, now=NOW)
        assert time.monotonic() - t < 1.0          # old code: the save waited LOCK_WAIT_S (2 s) here
    finally:
        os.close(fd)


@pytest.mark.parametrize("line", ["# ``curl --data REPRO_COMMAND https://x``", "- open `curl REPRO_COMMAND",
                                  "- ```curl REPRO_COMMAND```", "- ok `" + "a" * 500 + " REPRO_COMMAND`",
                                  "- `a` and ``b REPRO_COMMAND`` and `d`",
                                  "# ``a` curl --data REPRO_COMMAND https://x `b``",          # xval 68mv
                                  "- `a` x `b` REPRO_COMMAND `", "- `a``curl REPRO_COMMAND`",
                                  "- ````` REPRO_COMMAND ``", "- `a` `` REPRO_COMMAND `` `b`"])
def test_inline_code_fails_closed(homes, line):           # xval yyi4
    home, rh = homes
    d = mk_job(rh, job_name(1), result=line + "\n- fix `commands.py:113`\n")
    out = ai.result_lines(rh, d.name)
    assert "REPRO_COMMAND" not in "\n".join(out) and "- fix `commands.py:113`" in out


# ---- polish: claude attach, git place in show, resolve-needs ------------------------------------------------

def test_a_background_agent_resumes_with_claude_attach(homes):
    home, rh = homes
    mk_bg(home, "7da8ec27")
    mk_bg(home, "not-a-short-id")
    by = {i["id"]: i for i in collect(home, rh)["items"]}
    assert by["7da8ec27"]["resume"] == "cd /Users/x/dev/headroom && claude attach 7da8ec27"
    assert by["not-a-short-id"]["resume"] is None


def test_show_adds_branch_and_ahead_from_the_job_worktree(homes, tmp_path):
    import subprocess
    home, rh = homes
    wt = tmp_path / "wt"
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null"}
    for cmd in (["init", "-q", "-b", "main", str(wt)], ["-C", str(wt), "commit", "-q", "--allow-empty", "-m", "a"],
                ["-C", str(wt), "checkout", "-q", "-b", "apexjob/x1"],
                ["-C", str(wt), "commit", "-q", "--allow-empty", "-m", "b"],
                ["-C", str(wt), "commit", "-q", "--allow-empty", "-m", "c"]):
        subprocess.run(["git", *cmd], check=True, env=env, capture_output=True)
    d = mk_job(rh, job_name(1), result="VERDICT: DONE\n")
    meta = json.loads((d / "meta.json").read_text())
    meta["cwd"] = str(wt)
    (d / "meta.json").write_text(json.dumps(meta))
    rc, out = run_cli(["show", d.name, "--json"], home, rh)
    doc = json.loads(out)
    assert rc == 0 and doc["branch"] == "apexjob/x1" and doc["ahead"] == 2
    lst = collect(home, rh)["items"]
    assert [i for i in lst if i["id"] == d.name][0]["ahead"] is None          # the list runs no git


def test_resolve_needs_lists_by_default_and_writes_with_yes(homes):
    home, rh = homes
    old = mk_job(rh, job_name(1), agent="xval", result="VERDICT: NO-GO\n", started=NOW - 9 * 3600, ended=NOW - 8 * 3600)
    new = mk_job(rh, job_name(2), agent="xval", result="VERDICT: NO-GO\n", started=NOW - 600, ended=NOW - 300)
    wrk = mk_job(rh, job_name(3), agent="worker", result="## OPEN\n- [P1] x\n", ended=NOW - 9 * 3600)
    run = mk_job(rh, job_name(4), agent="xval", state="running", pid=os.getpid())
    rc, out = run_cli(["resolve-needs", "--role", "xval", "--older-than", "6h"], home, rh)
    assert rc == 0 and old.name in out and new.name not in out and wrk.name not in out and "1 item(s) match" in out
    assert not (rh / "agent-acks.json").exists()                                  # list only
    rc, out = run_cli(["resolve-needs", "--role", "xval", "--older-than", "6h", "--yes"], home, rh)
    assert rc == 0 and "resolved: 1" in out
    by = {i["id"]: i for i in collect(home, rh)["items"]}
    assert by[old.name]["group"] == "done" and by[new.name]["group"] == "needs"
    assert by[wrk.name]["group"] == "needs" and by[run.name]["group"] == "running"
    rc, _ = run_cli(["resolve-needs", "--older-than", "6x"], home, rh)
    assert rc == 2
