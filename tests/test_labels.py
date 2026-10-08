"""Task outcome labels: extraction, rules, judge combination, gold, precision accounting."""
from __future__ import annotations

import json
import os
from pathlib import Path

from apex_router import labels as L


def _pi_session(home: Path, name: str, turns: list) -> Path:
    """turns: [(user_text, [(tool, command, is_error, result)], final_text)]"""
    p = home / ".pi" / "agent" / "sessions" / "--x--" / f"{name}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines, n = [{"type": "session"}], 0
    for user, calls, final in turns:
        lines.append({"type": "message", "message": {"role": "user",
                                                     "content": [{"type": "text", "text": user}]}})
        for tool, cmd, err, out in calls:
            n += 1
            lines.append({"type": "message", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": f"c{n}", "name": tool, "arguments": {"command": cmd}}]}})
            lines.append({"type": "message", "message": {
                "role": "toolResult", "toolCallId": f"c{n}", "toolName": tool, "isError": err,
                "content": [{"type": "text", "text": out}]}})
        lines.append({"type": "message", "message": {"role": "assistant", "content": [
            {"type": "text", "text": final}]}})
    p.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return p


def _home():
    return Path(os.environ["HOME"])


def test_extract_tasks_and_rules():
    h = _home()
    p = _pi_session(h, "2026-10-07_aaaaaaaa-0000-0000-0000-000000000001", [
        ("fix the parser bug", [("edit", "", False, "ok"),
                                ("bash", "cd r && .venv/bin/pytest -q", False, "12 passed in 1s")],
         "fixed"),
        ("thanks, commit and push", [("bash", "git commit -m x && git push", False, "ok")], "done"),
        ("add retries", [("edit", "", False, ""), ("bash", "timeout 60 pytest", True,
                                                     "1 failed, 3 passed")], "tried"),
        ("no, still broken", [("bash", "ls", False, "")], "hm"),
        ("just chatting", [], "hi"),                                   # no tools: not a task
    ])
    noise = _pi_session(h, "2026-10-07_dddddddd-0000-0000-0000-000000000004", [
        ("review this", [("bash", "ls", False, "")], "on it"),
        ("Base directory for this skill: /x\n# Skill body", [("bash", "rg x", False, "")], "..."),
        ("thanks", [("bash", "echo", False, "")], "bye")])
    nt = L.extract(noise)
    # the skill body is not a request: its tool calls stay with "review this", next = "thanks"
    assert [t["request"] for t in nt] == ["review this", "thanks"] and nt[0]["next"] == "thanks"
    assert len(nt[0]["calls"]) == 2
    ts = L.extract(p)
    assert [t["request"] for t in ts] == ["fix the parser bug", "thanks, commit and push",
                                          "add retries", "no, still broken"]
    v = [L.votes(t) for t in ts]
    assert v[0] == {"tests": "success", "next_positive": "success"}
    assert v[1] == {"commit": "success"}
    assert v[2] == {"tests": "fail", "next_negative": "fail"}
    assert ts[3]["next"] == "just chatting" and v[3] == {}
    assert len({t["id"] for t in ts}) == 4


def test_tests_before_the_last_edit_do_not_count():
    t = {"request": "x", "next": None, "calls": [
        ["bash", "pytest", False, "5 passed"], ["edit", "", False, ""]]}
    assert L.lf_tests(t) is None


def test_repeated_request_is_a_failure_signal():
    t = {"request": "make the widget show network traffic per agent please",
         "next": "please make the widget show network traffic per agent", "calls": [["bash", "", 0, ""]]}
    assert L.lf_repeated(t) == "fail"


def test_posterior_needs_agreement_or_measured_accuracy():
    one = {"votes": {"commit": "success"}}
    lab, p = L.posterior(one, {})
    assert lab == "success" and p < L.EMIT_MIN                      # one unmeasured vote: no label
    two = {"votes": {"commit": "success", "tests": "success"}}
    assert L.posterior(two, {})[1] < L.EMIT_MIN                     # unmeasured voters: not yet
    three = {"votes": {"commit": "success", "tests": "success", "next_positive": "success"}}
    assert L.posterior(three, {})[1] >= L.EMIT_MIN                  # three agreeing votes: label
    # a voter measured accurate on gold is enough alone; a measured-bad one is not
    assert L.posterior(one, {"commit": (48, 50)})[1] >= L.EMIT_MIN
    assert L.posterior(one, {"commit": (20, 50)})[1] < L.EMIT_MIN
    conflict = {"votes": {"commit": "success", "next_negative": "fail"}}
    assert L.posterior(conflict, {})[1] < L.EMIT_MIN


def test_judge_votes_only_when_confident():
    r = {"votes": {}, "judge": {"outcome": "fail", "confidence": 0.5}}
    assert L._all_votes(r) == {}
    r["judge"]["confidence"] = 0.9
    assert L._all_votes(r) == {"judge": "fail"}
    r["judge"]["outcome"] = "unknown"
    assert L._all_votes(r) == {}


def test_wilson_interval():
    lo, hi = L.wilson(90, 100)
    assert 0.82 < lo < 0.84 and 0.94 < hi < 0.95
    assert L.wilson(0, 0) == (0.0, 1.0)


def test_gold_target_grows_with_data():
    assert L.gold_target(100) == L.GOLD_MIN
    assert L.gold_target(5000) == 500


def test_build_stores_no_text_and_gold_overrides(monkeypatch):
    h = _home()
    secret = "SECRET-PROMPT-TEXT"
    _pi_session(h, "2026-10-07_bbbbbbbb-0000-0000-0000-000000000002", [
        (f"{secret} fix it", [("edit", "", False, ""), ("bash", "pytest", False, "3 passed")], "ok"),
        ("thanks", [("bash", "echo", False, "")], "bye")])
    calls = []

    def fake_judge(t):
        calls.append(t["id"])
        return {"outcome": "success", "confidence": 0.95, "evidence": "tests", "model": "fake"}
    res = L.build(judge_limit=10, judge_fn=fake_judge, log=None)
    d = L.home()
    for f in ("tasks.jsonl", "labels.jsonl"):
        assert secret not in (d / f).read_text()
        assert oct((d / f).stat().st_mode)[-3:] == "600"
    assert len(calls) == 2
    lab = {x["id"]: x for x in res["labels"]}
    first = res["labels"][0]
    assert first["label"] == "success" and first["from"] == "weak"
    # judged results are kept: a rebuild does not call the judge again
    L.build(judge_limit=10, judge_fn=fake_judge, log=None)
    assert len(calls) == 2
    # a gold label wins over the weak one and feeds voter accuracy
    (d / "gold.jsonl").write_text(json.dumps({"id": first["id"], "outcome": "fail"}) + "\n")
    res = L.relabel()
    lab = {x["id"]: x for x in res["labels"]}
    assert lab[first["id"]]["label"] == "fail" and lab[first["id"]]["from"] == "gold"
    assert res["acc"]["judge"] == (0, 1)
    assert "precision" in L.report(res)


def test_review_writes_gold_and_sampling_prefers_disagreement(monkeypatch):
    rows = [{"id": f"t{i}", "source": "pi", "votes": {"commit": "success"}} for i in range(30)]
    rows.append({"id": "dis", "source": "pi", "votes": {"commit": "success",
                                                       "next_negative": "fail"}})
    labels = [{"id": r["id"], "label": "unknown", "p": 0.5} for r in rows]
    picked = L.sample_for_review(rows, labels, {}, 6)
    assert picked[0]["id"] == "dis" and len(picked) == 6
    assert len({r["id"] for r in picked}) == 6
    # review(): answers go to gold.jsonl
    h = _home()
    _pi_session(h, "2026-10-07_cccccccc-0000-0000-0000-000000000003", [
        ("do x", [("bash", "ls", False, "")], "did x"), ("next", [("bash", "ls", False, "")], "y")])
    L.build(log=None)
    answers = iter(["s", "f"])
    n = L.review(5, inp=lambda _: next(answers, "q"), out=lambda *_: None)
    gold = [json.loads(x) for x in (L.home() / "gold.jsonl").read_text().splitlines()]
    assert n == 2 and sorted(g["outcome"] for g in gold) == ["fail", "success"]


def test_extract_fails_open(tmp_path):
    """Odd records (non-string text, non-dict blocks/lines, bad JSON) are skipped, an unreadable
    file gives no tasks — extract never raises."""
    p = tmp_path / "s.jsonl"
    rows = [
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": 5}]}},
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
        {"type": "message", "message": {"role": "assistant", "content": [
            7, {"type": "text", "text": None},
            {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}]}},
        {"type": "message", "message": {"role": "toolResult", "toolCallId": "c1",
                                        "content": [{"type": "text", "text": {"x": 1}}]}},
        [1, 2], "str", None,
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows) + "{bad json\n")
    ts = L.extract(p)
    assert [t["request"] for t in ts] == ["go"] and len(ts[0]["calls"]) == 1
    assert L.extract(tmp_path / "missing.jsonl") == []
    d = tmp_path / "dir.jsonl"
    d.mkdir()
    assert L.extract(d) == []
