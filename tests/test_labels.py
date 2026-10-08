"""Task outcome labels: extraction, rules, judge combination, gold, precision accounting."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

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


def test_judge_that_saw_signals_and_agreeing_rules_are_one_voter():
    acc = {"judge": (40, 50), "commit": (45, 50), "tests": (30, 50), "next_negative": (9, 10)}
    sees = {"outcome": "success", "confidence": 0.95, "context": "export"}
    blind = {**sees, "context": "clip"}
    r = {"votes": {"commit": "success", "tests": "success"}, "judge": sees}
    g = L.voter_groups(r, acc)
    assert g == [("success", L._smoothed(acc, "commit"))]           # max of the group, once
    # the old judge context (no signals) and pre-context votes stay independent voters
    assert len(L.voter_groups({**r, "judge": blind}, acc)) == 3
    legacy = {"outcome": "success", "confidence": 0.95}                    # stored before context
    assert len(L.voter_groups({**r, "judge": legacy}, acc)) == 3
    # the grouped posterior is lower than the product of three "independent" agreeing voters
    assert L.posterior(r, acc)[1] < L.posterior({**r, "judge": blind}, acc)[1]
    assert L.posterior(r, acc)[0] == "success"
    # a rule that disagrees with the judge still counts separately
    r2 = {"votes": {"commit": "success", "next_negative": "fail"}, "judge": sees}
    g2 = L.voter_groups(r2, acc)
    assert sorted(v for v, _ in g2) == ["fail", "success"] and len(g2) == 2
    # no judge vote (low confidence): rules count as before
    r3 = {"votes": {"commit": "success", "tests": "success"},
          "judge": {**sees, "confidence": 0.5}}
    assert len(L.voter_groups(r3, acc)) == 2


def test_class_prior_line():
    labels = [{"label": "success", "from": "weak"}] * 3 + [{"label": "fail", "from": "weak"},
                                                          {"label": "fail", "from": "gold"},
                                                          {"label": "unknown", "from": "weak"}]
    gold = {"a": "success", "b": "fail", "c": "unknown", "d": "partial"}
    rows = [{"votes": {"tail_errors": "fail"}, "n_errors": 3}, {"votes": {}, "n_errors": 1},
            {"votes": {"commit": "success"}, "n_errors": 0}, {"votes": {}}]
    s = L.class_prior_line(labels, gold, rows)
    assert "weak-emitted 0.75 / 0.00 / 0.25 (n 4)" in s
    assert "gold 0.33 / 0.33 / 0.33 (n 3)" in s
    assert "rule-fail vote 0.25 of tasks, n_errors>0 0.50" in s


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


def _export_fixture(h: Path):
    _pi_session(h, "2026-10-07_eeeeeeee-0000-0000-0000-000000000005", [
        ("SECRET-REQ fix the parser", [("edit", "", False, ""),
                                       ("bash", "pytest -q", False, "4 passed")], "SECRET-FINAL"),
        ("thanks, commit and push", [("bash", "git commit -m x", False, "ok")], "done"),
        ("add retries", [("bash", "ls", True, "")], "tried")])
    L.build(log=None)


def test_export_review_writes_private_file_with_context(tmp_path):
    _export_fixture(_home())
    out = tmp_path / "review.jsonl"
    out.write_text("old")
    os.chmod(out, 0o644)                                    # an existing file is tightened
    assert L.export_review(10, out) == 3
    assert oct(out.stat().st_mode)[-3:] == "600"
    rows = {r["request"][:10]: r for r in map(json.loads, out.read_text().splitlines())}
    r = rows["SECRET-REQ"]
    assert r["last"] == "SECRET-FINAL" and r["next"] == "thanks, commit and push"
    assert r["votes"] == {"tests": "success", "next_positive": "success"}
    assert r["signals"]["tests_after_last_edit"] == "passed" and not r["signals"]["committed"]
    assert {"id", "source", "n_calls", "tools", "judge"} <= set(r)
    assert rows["add retrie"]["next"] is None and rows["add retrie"]["signals"]["session_ended"]
    # nothing new stored: no text under the labels home, no gold
    for f in L.home().iterdir():
        assert "SECRET" not in f.read_text()
    assert not (L.home() / "gold.jsonl").exists()
    assert L.main(["export-review", "--k", "2", "--out", str(tmp_path / "b.jsonl")]) == 0
    assert len((tmp_path / "b.jsonl").read_text().splitlines()) == 2


def test_import_gold_validates_marks_author_and_relabels(tmp_path, capsys):
    _export_fixture(_home())
    ids = [json.loads(x)["id"] for x in (L.home() / "tasks.jsonl").read_text().splitlines()]
    f = tmp_path / "g.jsonl"

    def bad(lines, msg):
        f.write_text("".join(json.dumps(x) + "\n" for x in lines))
        assert L.main(["import-gold", str(f), "--by", "model:x"]) == 2
        assert msg in capsys.readouterr().err
        assert not (L.home() / "gold.jsonl").exists()          # all-or-nothing

    bad([{"id": "nope", "outcome": "success"}], "not a known task")
    bad([{"id": ids[0], "outcome": "great"}], "not in success|partial|fail|unknown")
    bad([{"id": ids[0], "outcome": "fail"}, {"id": ids[0], "outcome": "fail"}], "appears twice")
    bad([{"id": ids[0], "outcome": "fail", "reason": "x" * 121}], "<= 120 chars")
    bad([{"id": ids[0], "outcome": "fail"}, {"id": "nope", "outcome": "fail"}], "not a known")
    with pytest.raises(L.GoldImportError):
        L.import_gold(f, "has space")

    f.write_text(json.dumps({"id": ids[0], "outcome": "fail", "reason": "tests passed but no"})
                 + "\n" + json.dumps({"id": ids[2], "outcome": "unknown"}) + "\n")
    res = L.import_gold(f, "model:claude-test")
    assert res["imported"] == 2
    gold = [json.loads(x) for x in (L.home() / "gold.jsonl").read_text().splitlines()]
    assert {g["by"] for g in gold} == {"model:claude-test"}
    assert gold[0]["reason"] == "tests passed but no" and "reason" not in gold[1]
    assert oct((L.home() / "gold.jsonl").stat().st_mode)[-3:] == "600"
    lab = {x["id"]: x for x in L._read(L.home() / "labels.jsonl")}
    assert lab[ids[0]]["label"] == "fail" and lab[ids[0]]["from"] == "gold"   # relabel ran
    assert res["acc"]["tests"] == (0, 1)
    f.write_text(json.dumps({"id": ids[0], "outcome": "success"}) + "\n")
    with pytest.raises(L.GoldImportError, match="already has a gold"):
        L.import_gold(f, "model:claude-test")
    # report splits gold by author: user (review) vs model
    gold.append({"id": ids[1], "outcome": "success", "ts": 0, "by": "user"})
    L._write(L.home() / "gold.jsonl", gold)
    assert "gold by: user 1 · model/other 2 (model:claude-test 2, user 1)" in L.report()


def test_export_by_ids_and_supersede_corrections(tmp_path, capsys):
    _export_fixture(_home())
    ids = [json.loads(x)["id"] for x in (L.home() / "tasks.jsonl").read_text().splitlines()]
    f = tmp_path / "g.jsonl"
    f.write_text(json.dumps({"id": ids[0], "outcome": "fail", "reason": "first look"}) + "\n")
    L.import_gold(f, "model:x")
    # export exactly the named tasks (gold or not), with the gold row in force
    out = tmp_path / "e.jsonl"
    assert L.main(["export-review", "--k", "0", "--out", str(out),
                   "--ids", f"{ids[0]},{ids[2]}"]) == 0
    ex = [json.loads(x) for x in out.read_text().splitlines()]
    assert [x["id"] for x in ex] == [ids[0], ids[2]] and oct(out.stat().st_mode)[-3:] == "600"
    assert ex[0]["gold"]["outcome"] == "fail" and ex[1]["gold"] is None
    assert L.main(["export-review", "--out", str(out), "--ids", "nope"]) == 2
    ts0 = ex[0]["gold"]["ts"]

    def rejected(line, msg):
        f.write_text(json.dumps(line) + "\n")
        with pytest.raises(L.GoldImportError, match=msg):
            L.import_gold(f, "model:x")
    rejected({"id": ids[0], "outcome": "success"}, "pass supersedes")
    rejected({"id": ids[0], "outcome": "success", "supersedes": ts0 - 5}, "not the ts")
    rejected({"id": ids[2], "outcome": "success", "supersedes": ts0}, "not the ts")
    # a correction appends; the newest row is in force everywhere
    f.write_text(json.dumps({"id": ids[0], "outcome": "success", "supersedes": ts0,
                             "reason": "scope grew; continuation finished it"}) + "\n")
    res = L.import_gold(f, "model:x")
    rows = L._read(L.home() / "gold.jsonl")
    assert len(rows) == 2 and rows[0]["outcome"] == "fail"                 # nothing edited
    assert rows[1]["supersedes"] == ts0 and rows[1]["ts"] > ts0
    assert res["gold"] == {ids[0]: "success"}
    lab = {x["id"]: x for x in L._read(L.home() / "labels.jsonl")}
    assert lab[ids[0]]["label"] == "success"
    assert L.gold_by() == {"model:x": 1}                                    # counted once
    # the superseded ts no longer works; the new one does
    rejected({"id": ids[0], "outcome": "fail", "supersedes": ts0}, "not the ts")
    # newest wins even if rows are out of file order; a rebuild keeps both rows on the task
    assert L.gold_latest([rows[1], rows[0]])[ids[0]]["outcome"] == "success"
    L.build(log=None)
    assert [g["id"] for g in L._read(L.home() / "gold.jsonl")] == [ids[0], ids[0]]


def _claude_session(home: Path, name: str, records: list) -> Path:
    """records: ("user", text, extra) | ("tool", name, cmd, result) | ("say", text).
    ``tool`` writes an assistant tool_use plus the user tool_result, like Claude Code."""
    p = home / ".claude" / "projects" / "-x" / f"{name}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    out, n = [], 0
    for r in records:
        if r[0] == "user":
            out.append({"type": "user", **r[2],
                        "message": {"role": "user", "content": r[1]}})
        elif r[0] == "tool":
            n += 1
            out.append({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"u{n}", "name": r[1], "input": {"command": r[2]}}]}})
            out.append({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"u{n}", "content": r[3]}]}})
        else:
            out.append({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": r[1]}]}})
    p.write_text("".join(json.dumps(x) + "\n" for x in out))
    return p


def test_injected_skill_bodies_and_image_metadata_are_not_requests():
    h = _home()
    chrome = "# Claude in Chrome browser automation\n\nYou have access to browser tools."
    cfg = "# Update Config Skill\n\nModify Claude Code configuration."
    p = _claude_session(h, "ffffffff-0000-0000-0000-000000000006", [
        ("user", "check the landing page in the browser", {}),
        ("tool", "Bash", "ls", "ok"),
        ("user", chrome, {"isMeta": True}),                       # flagged skill body
        ("tool", "Bash", "open x", "ok"),
        ("user", "[Image: original 3550x1990, displayed at 2000x1121.]", {"isMeta": True}),
        ("tool", "Bash", "echo", "ok"),                           # same task after a screenshot
        ("say", "page looks right"),
        ("user", cfg, {}),                                        # unflagged: caught by prefix
        ("tool", "Bash", "cat settings", "ok"),
        ("user", "This session is being continued from a previous conversation...",
         {"isCompactSummary": True}),
        ("user", [{"type": "text", "text": "[Image #24]"}, {"type": "image"}], {}),
        ("user", [{"type": "text", "text": "[Image: source: /tmp/a.png]"}], {"isMeta": True}),
        ("tool", "Bash", "ls", "ok"),
        ("say", "done"),
    ])
    ts = L.extract(p)
    # one browser task (skill body + screenshot metadata did not split it), then the pasted image
    assert [t["request"] for t in ts] == ["check the landing page in the browser", "[Image #24]"]
    assert len(ts[0]["calls"]) == 4 and ts[0]["next"] == "[Image #24]"
    # worldmodel.steps cuts the same boundaries
    from apex_router.worldmodel import steps as S
    assert L.request_text(chrome) is None and L.request_text(cfg) is None
    assert L.request_text("hello", {"isMeta": True}) is None
    assert L.request_text("hello", {"isMeta": False}) == "hello"
    turns = S.walk_main(p, S._Stats())["turns"]
    assert [x["text"] for x in turns] == [t["request"] for t in ts]
    assert [len(x["calls"]) for x in turns] == [4, 1]


def test_rebuild_after_boundary_change_moves_gold_and_judge_with_the_request(monkeypatch):
    """Task ids are positional: when an injected record stops counting as a request, later ids
    shift. Gold follows its request; gold on the vanished pseudo-task is orphaned; no stale id
    ends up labelling a different task; a judge vote survives only if its evidence is unchanged."""
    h = _home()
    recs = [("user", "first request", {"timestamp": "2026-10-07T10:00:00Z"}),
            ("tool", "Bash", "ls", "ok"), ("say", "one"),
            ("user", "# Some Skill\nbody", {"isMeta": True, "timestamp": "2026-10-07T10:01:00Z"}),
            ("tool", "Bash", "ls", "ok"), ("say", "skill ran"),
            ("user", "second request", {"timestamp": "2026-10-07T10:02:00Z"}),
            ("tool", "Bash", "ls", "ok"), ("say", "two"),
            ("user", "third request", {"timestamp": "2026-10-07T10:03:00Z"}),
            ("tool", "Bash", "ls", "ok"), ("say", "three")]
    _claude_session(h, "abababab-0000-0000-0000-000000000007", recs)
    real = L.request_text
    monkeypatch.setattr(L, "request_text", lambda c, r=None: real(c))      # old: flag ignored
    fake = lambda t: {"outcome": "success", "confidence": 0.9, "evidence": "x", "model": "f"}  # noqa: E731
    old = {x["request"]: x["id"] for x in L.all_tasks()}
    L.build(judge_limit=10, judge_fn=fake, log=None)
    gold = [{"id": old["# Some Skill\nbody"], "outcome": "fail", "by": "m"},
            {"id": old["second request"], "outcome": "partial", "by": "m"},
            {"id": old["third request"], "outcome": "success", "by": "m"}]
    L._write(L.home() / "gold.jsonl", gold)
    monkeypatch.setattr(L, "request_text", real)
    new = {x["request"]: x["id"] for x in L.all_tasks()}
    assert new["second request"] == old["# Some Skill\nbody"]               # ids shifted
    res = L.build(log=None)
    g = {x["outcome"]: x for x in L._read(L.home() / "gold.jsonl")}
    assert g["partial"]["id"] == new["second request"] and g["partial"]["id_was"]
    assert g["success"]["id"] == new["third request"]
    assert g["fail"]["id"].startswith("orphan:")
    assert res["gold"] == {new["second request"]: "partial", new["third request"]: "success"}
    rows = {r["id"]: r for r in L._read(L.home() / "tasks.jsonl")}
    assert "judge" not in rows[new["first request"]]        # it absorbed the skill's call
    assert rows[new["third request"]]["judge"]["model"] == "f"   # unchanged: carried by request
    assert "orphaned 1" in L.report()


def test_repeated_ignores_image_placeholders():
    img = "[Image: original 3550x1990, displayed at 2000x1121. Multiply coordinates by 1.77 to map]"
    t = {"request": img, "next": img, "calls": [["bash", "", 0, ""]]}
    assert L.lf_repeated(t) is None                         # identical screenshots: no repeat
    t = {"request": "[Image #3]", "next": "[Image #4]", "calls": [["bash", "", 0, ""]]}
    assert L.lf_repeated(t) is None                         # image-only requests
    t = {"request": "[Image #1] make the widget show network traffic per agent please",
         "next": "please make the widget show network traffic per agent [Image #2]",
         "calls": [["bash", "", 0, ""]]}
    assert L.lf_repeated(t) == "fail"                       # text around images still counts


def test_declining_an_offer_is_not_negative():
    base = {"request": "update memories", "calls": [["bash", "git commit -m x", 0, ""]]}
    offered = dict(base, last="Memory updated. Say \"push\" when you want them on GitHub.")
    for nxt in ("no more pushes", "no, don't push", "no thanks", "no, just leave it local"):
        t = dict(offered, next=nxt)
        assert L.lf_next_negative(t) is None, nxt
        assert L.lf_commit(t) == "success", nxt
        assert L.signals(t)["next_declines_offer"]
    # a real complaint after an offer still counts
    for nxt in ("no, still broken", "no, that's wrong", "nope, it fails"):
        assert L.lf_next_negative(dict(offered, next=nxt)) == "fail", nxt
    # a decline-looking "no" with no offer in the final message still counts
    assert L.lf_next_negative(dict(base, last="Done.", next="no more pushes")) == "fail"
    assert L.lf_next_negative(dict(base, last="Want me to push?", next="no, push")) is None


def _row(i, votes=None, judge=None, session="s"):
    return {"id": f"t{i}", "session": session, "index": i, "votes": votes or {}, "judge": judge}


def test_suspected_fail_signals():
    assert L.suspected_fail(_row(0, votes={"tail_errors": "fail"}))
    assert L.suspected_fail(_row(0, votes={"tests": "fail"}))
    assert not L.suspected_fail(_row(0, votes={"tests": "success", "commit": "success"}))
    assert L.suspected_fail(_row(0, judge={"outcome": "partial", "confidence": 0.95}))
    assert L.suspected_fail(_row(0, judge={"outcome": "fail", "confidence": 0.3}))
    assert L.suspected_fail(_row(0, judge={"outcome": "success", "confidence": 0.8}))
    assert not L.suspected_fail(_row(0, judge={"outcome": "success", "confidence": 0.95}))
    assert not L.suspected_fail(_row(0, judge={"outcome": "unknown", "confidence": 0.95}))
    assert not L.suspected_fail(_row(0))


def test_sample_strata_in_order_disjoint_and_without_gold():
    hi = {"outcome": "success", "confidence": 0.95}
    rows = ([_row(i, votes={"interrupted": "fail"}) for i in range(4)]          # t0-t3 suspect
            + [_row(i, judge=hi) for i in range(4, 12)])                         # t4-t11 clean
    labels = [{"id": f"t{i}", "label": "unknown" if i in (6, 7) else "success"}
              for i in range(12)]
    test_keys = {"s:2", "s:5", "s:6", "s:7", "s:8"}
    gold = {"t0": "fail", "t5": "success"}
    got = L.sample_strata(rows, labels, gold, [("suspected-fail", 10), ("test-split", 2),
                                               ("random", 3)], test_keys, seed=1)
    ids = [r["id"] for r, _ in got]
    assert len(ids) == len(set(ids)) and not set(ids) & set(gold)
    assert sorted(r["id"] for r, s in got if s == "suspected-fail") == ["t1", "t2", "t3"]
    # t2 already taken by suspected-fail; t5 has gold; unlabelled test tasks come first
    assert sorted(r["id"] for r, s in got if s == "test-split") == ["t6", "t7"]
    assert [s for _, s in got].count("random") == 3
    assert all(r["id"] not in ("t0", "t1", "t2", "t3", "t5", "t6", "t7")
               for r, s in got if s == "random")
    # test-split order: no emitted label, then a rule fail vote, then a tool error, then the rest
    lab = {"a": "unknown", "b": "success", "c": "success", "d": "success"}
    pri = [{"id": "d", "votes": {}, "n_errors": 0}, {"id": "c", "votes": {}, "n_errors": 2},
           {"id": "b", "votes": {"repeated": "fail"}}, {"id": "a", "votes": {}}]
    assert [r["id"] for r in sorted(pri, key=lambda r: L._test_priority(r, lab))] == \
        ["a", "b", "c", "d"]
    assert L.sample_strata(rows, labels, gold, [("random", 3)], test_keys, seed=1) == \
        L.sample_strata(rows, labels, gold, [("random", 3)], test_keys, seed=1)
    assert L.parse_strata("suspected-fail:25, test-split:12,random:23") == [
        ("suspected-fail", 25), ("test-split", 12), ("random", 23)]
    for bad in ("nope:3", "random", "random:x", ""):
        with pytest.raises(L.GoldImportError):
            L.parse_strata(bad)


def test_split_test_keys_join_and_strata_export(tmp_path):
    _export_fixture(_home())
    rows = L._read(L.home() / "tasks.jsonl")
    wm = L.home().parent / "worldmodel" / "tasks.jsonl"
    wm.parent.mkdir(parents=True, exist_ok=True)
    # the P6 task id is sid:i; the labels row joins by (session, index)
    wm.write_text("".join(json.dumps({"task": L.wm_task_key(r),
                                      "split": "test" if r["index"] == 1 else "train"}) + "\n"
                          for r in rows) + "not json\n")
    keys = L.split_test_keys()
    assert keys == {L.wm_task_key(r) for r in rows if r["index"] == 1}
    out = tmp_path / "s.jsonl"
    assert L.main(["export-review", "--out", str(out), "--strata", "test-split:5,random:1"]) == 0
    ex = [json.loads(x) for x in out.read_text().splitlines()]
    assert [x["stratum"] for x in ex] == ["test-split", "random"]
    assert ex[0]["id"] == next(r["id"] for r in rows if r["index"] == 1)
    assert oct(out.stat().st_mode)[-3:] == "600"
    assert L.main(["export-review", "--out", str(out), "--strata", "bogus:1"]) == 2


def test_import_gold_tag_and_report_split(tmp_path, capsys):
    _export_fixture(_home())
    ids = [r["id"] for r in L._read(L.home() / "tasks.jsonl")]
    f = tmp_path / "g.jsonl"
    f.write_text(json.dumps({"id": ids[0], "outcome": "success"}) + "\n")
    L.import_gold(f, "model:x")
    f.write_text(json.dumps({"id": ids[1], "outcome": "fail"}) + "\n"
                 + json.dumps({"id": ids[2], "outcome": "unknown"}) + "\n")
    assert L.main(["import-gold", str(f), "--by", "model:x", "--tag", "has space"]) == 2
    assert "--tag" in capsys.readouterr().err
    assert L.main(["import-gold", str(f), "--by", "model:x", "--tag", "holdout-1"]) == 0
    gold = L._read(L.home() / "gold.jsonl")
    assert "tag" not in gold[0] and {g["tag"] for g in gold[1:]} == {"holdout-1"}
    assert L.gold_by_tag() == {"untagged": 1, "holdout-1": 2}
    assert "gold by tag: holdout-1 2, untagged 1" in L.report()
