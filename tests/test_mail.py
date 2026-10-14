"""Mail bus: short status lines from agents, owner messages back to them (``apex-router mail``).

All state lives under ``$APEX_MAIL_DIR`` (default ``~/.apex-router/mail``); the conftest sandbox
points HOME at a tmp dir, so nothing here touches the real install.
"""
from __future__ import annotations

import json
import os

import pytest

from apex_router import mail


@pytest.fixture
def mdir(tmp_path, monkeypatch):
    d = tmp_path / "mail"
    monkeypatch.setenv("APEX_MAIL_DIR", str(d))
    monkeypatch.setenv("APEX_ADAPTERS_DIR", str(tmp_path / "adapters"))
    return d


def test_post_appends_one_line_and_returns_id(mdir):
    r = mail.post("run1", "w-dist/reviewer", "cycle 1: 4 rows kept, 2 killed", now=100.0)
    assert r["id"] == 1 and r["kind"] == "status"
    lines = (mdir / "run1" / "out.jsonl").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row == {"id": 1, "ts": 100.0, "agent": "w-dist/reviewer", "kind": "status",
                   "text": "cycle 1: 4 rows kept, 2 killed", "reply_to": None}
    r2 = mail.post("run1", "w-ml/mean", "hello", kind="question", now=101.0)
    assert r2["id"] == 2


def test_post_clips_strips_control_chars_and_rejects_bad_names(mdir):
    r = mail.post("run1", "a", "x\x1b[31m" + "y" * 1000 + "\n", now=1.0)
    assert "\x1b" not in r["text"] and "\n" not in r["text"]
    assert len(r["text"]) == mail.TEXT_MAX
    for bad in ("", "../x", "a/../b", ".hidden", "a b", "x/y"):
        with pytest.raises(ValueError):
            mail.post(bad, "a", "t", now=1.0)
    for bad in ("", "../x", "a/../b", ".hidden", "a b", "_all", "_x", "a__b", "a/_b"):
        with pytest.raises(ValueError):
            mail.post("run1", bad, "t", now=1.0)
        with pytest.raises(ValueError):
            mail.send("run1", bad, "t", now=1.0)
    with pytest.raises(ValueError):
        mail.brief("r", "$(x)")
    with pytest.raises(ValueError):
        mail.post("run1", "a", "   ", now=1.0)
    with pytest.raises(ValueError):
        mail.post("run1", "a", "t", kind="shout", now=1.0)


def test_send_then_read_advances_cursor_and_includes_broadcast(mdir):
    mail.send("run1", "w-dist/reviewer", "why did you kill D-03?", now=10.0)
    mail.send("run1", "*", "everyone: budget is $25k floor", now=11.0)
    mail.send("run1", "w-ml/mean", "not for you", now=12.0)
    got = mail.read("run1", "w-dist/reviewer")
    assert [m["text"] for m in got] == ["why did you kill D-03?", "everyone: budget is $25k floor"]
    assert got[0]["to"] == "w-dist/reviewer" and got[1]["to"] == "*"
    assert mail.read("run1", "w-dist/reviewer") == []
    # peek does not consume
    mail.send("run1", "w-dist/reviewer", "second", now=15.0)
    assert [m["text"] for m in mail.read("run1", "w-dist/reviewer", peek=True)] == ["second"]
    assert [m["text"] for m in mail.read("run1", "w-dist/reviewer")] == ["second"]
    assert mail.read("run1", "w-dist/reviewer") == []
    # an unknown agent in an unknown run reads nothing and creates nothing
    assert mail.read("nope", "x") == []
    assert not (mdir / "nope").exists()
    # a reply references the owner's message id
    r = mail.post("run1", "w-dist/reviewer", "D-03 failed K-PREREG", kind="reply", reply_to=1, now=16.0)
    assert r["reply_to"] == 1


def test_inbox_lists_latest_run_by_default_filters_and_tails(mdir):
    mail.post("old", "a", "old status", now=1.0)
    os.utime(mdir / "old" / "out.jsonl", (1.0, 1.0))
    for i in range(5):
        mail.post("new", "w-dist/x" if i % 2 else "w-ml/y", f"s{i}", now=20.0 + i)
    assert mail.latest_run() == "new"
    rows = mail.inbox(None, n=3)
    assert [r["text"] for r in rows] == ["s2", "s3", "s4"]
    rows = mail.inbox("new", agent="w-dist/x")
    assert [r["text"] for r in rows] == ["s1", "s3"]
    rows = mail.inbox("new", agent="w-dist/")          # prefix = a whole world
    assert [r["text"] for r in rows] == ["s1", "s3"]
    assert mail.inbox("old") and mail.inbox("missing") == []
    assert mail.runs()[0]["run"] == "new"
    old = next(r for r in mail.runs() if r["run"] == "old")
    assert old["posts"] == 1 and old["questions"] == 0


def test_agents_summary_counts_open_questions_and_unread_owner_mail(mdir):
    mail.post("r", "a", "started", kind="start", now=1.0)
    mail.post("r", "a", "may I drop D-01?", kind="question", now=2.0)
    mail.post("r", "b", "started", kind="start", now=3.0)
    s = {x["agent"]: x for x in mail.agents("r")}
    assert s["a"]["last"] == "may I drop D-01?" and s["a"]["questions"] == 1
    mail.post("r", "a", "ack", kind="reply", now=3.5)          # the agent's own reply closes nothing
    assert {x["agent"]: x for x in mail.agents("r")}["a"]["questions"] == 1
    mail.send("r", "a", "yes", now=4.0)                        # the owner's line does
    mail.send("r", "*", "all: carry on", now=5.0)
    s = {x["agent"]: x for x in mail.agents("r")}
    assert s["a"]["questions"] == 0
    assert s["a"]["unread"] == 2 and s["b"]["unread"] == 1
    mail.post("r", "b", "and me?", kind="question", now=6.0)   # after the broadcast: open
    assert {x["agent"]: x for x in mail.agents("r")}["b"]["questions"] == 1
    mail.read("r", "a")
    assert {x["agent"]: x for x in mail.agents("r")}["a"]["unread"] == 0
    mail.post("r", "a", "finished", kind="done", now=7.0)
    assert {x["agent"]: x for x in mail.agents("r")}["a"]["state"] == "done"


def test_widget_adapter_is_written_on_post_and_send(mdir, tmp_path):
    mail.post("r", "w-dist/sig", "cycle 1 draft done", kind="status", now=1.0)
    mail.post("r", "w-ml/mean", "need a ruling on C", kind="question", now=2.0)
    p = tmp_path / "adapters" / "mail.json"
    doc = json.loads(p.read_text())
    assert doc["title"].startswith("Mail") and doc["ts"] == 2.0
    assert any("1 question" in r for r in doc["rows"])
    assert any("w-ml/mean" in r and "need a ruling" in r for r in doc["rows"])
    assert all(len(r) <= 120 for r in doc["rows"]) and len(doc["rows"]) <= 10
    mail.post("r", "w-ml/mean", "floor is $25k, cap $ 1.2M, say $3", kind="status", now=2.5)
    doc = json.loads(p.read_text())
    assert "$" not in json.dumps(doc)
    assert any("floor is •, cap •, say •" in r for r in doc["rows"])
    mail.send("r", "w-ml/mean", "C stays inside", now=3.0)
    doc = json.loads(p.read_text())
    assert doc["ts"] == 3.0 and any("unread" in r for r in doc["rows"])


def test_torn_lines_are_skipped_and_never_glue_onto_the_next_row(mdir):
    mail.post("r", "a", "ok", now=1.0)
    with open(mdir / "r" / "out.jsonl", "a") as f:
        f.write('{"id": 2, "ts": 2.0, "agent": "a", "kind": "status", "te')
    assert [r["text"] for r in mail.inbox("r")] == ["ok"]
    r = mail.post("r", "a", "after", now=3.0)
    assert r["id"] == 2                                 # ids count parsed rows only
    assert [r["text"] for r in mail.inbox("r")] == ["ok", "after"]
    assert len((mdir / "r" / "out.jsonl").read_text().splitlines()) == 3


def test_ids_are_unique_under_concurrent_writers(mdir):
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(8) as ex:
        ids = sorted(f.result()["id"] for f in [ex.submit(mail.post, "r", f"a{i}", "hi") for i in range(24)])
    assert ids == list(range(1, 25))


def test_text_whitespace_becomes_a_space_and_bidi_is_dropped(mdir):
    r = mail.post("r", "a", "line1\nline2\tword \u2028x \u202eREV\u200f end", now=1.0)
    assert r["text"] == "line1 line2 word x REV end"


def test_runs_skip_foreign_directories(mdir):
    mail.post("ok", "a", "hi", now=1.0)
    (mdir / "bad name").mkdir()
    (mdir / "bad name" / "out.jsonl").write_text("{}\n")
    assert [r["run"] for r in mail.runs()] == ["ok"] and mail.latest_run() == "ok"


def test_slash_and_underscore_encodings_cannot_collide(mdir):
    mail.send("r", "a/b", "for a/b", now=1.0)
    assert [m["text"] for m in mail.read("r", "a/b")] == ["for a/b"]
    assert (mdir / "r" / "in" / "a__b.jsonl").exists()
    assert mail.read("r", "a__b") == []                 # invalid name reads nothing


def test_brief_names_the_run_agent_and_commands(mdir):
    b = mail.brief("wf_1", "w-dist/reviewer")
    assert "apex-router mail post wf_1 w-dist/reviewer" in b
    assert "apex-router mail read wf_1 w-dist/reviewer" in b
    assert "question" in b and "done" in b


def test_cli_round_trip(mdir, capsys):
    assert mail.main(["post", "r", "w-dist/sig", "draft in", "--kind", "start"]) == 0
    assert mail.main(["send", "r", "w-dist/sig", "focus on D-06"]) == 0
    assert mail.main(["read", "r", "w-dist/sig"]) == 0
    out = capsys.readouterr().out
    assert "[id 1] focus on D-06" in out
    assert mail.main(["read", "r", "w-dist/sig"]) == 0
    assert capsys.readouterr().out.strip() == ""
    assert mail.main(["inbox", "r"]) == 0
    out = capsys.readouterr().out
    assert "w-dist/sig" in out and "draft in" in out and "focus on D-06" in out
    assert mail.main(["runs"]) == 0 and "r: 1 agents" in capsys.readouterr().out
    assert mail.main(["agents", "r"]) == 0 and "w-dist/sig" in capsys.readouterr().out
    assert mail.main(["brief", "r", "x"]) == 0 and "apex-router mail post r x" in capsys.readouterr().out
    assert mail.main(["post", "bad name", "a", "t"]) == 2
    assert mail.main(["inbox", "r", "--json"]) == 0
    rows = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert rows[0]["agent"] == "w-dist/sig"
    assert mail.main(["widget"]) == 0


def test_cli_through_top_level_entry(mdir, capsys):
    from apex_router.cli import main as top
    assert top(["mail", "post", "r", "a", "hi"]) == 0
    assert top(["mail", "inbox", "r"]) == 0
    assert "hi" in capsys.readouterr().out


def test_inbox_interleaves_owner_lines_and_filters_them_by_recipient(mdir):
    mail.post("r", "a", "s1", now=1.0)
    mail.send("r", "a", "to a", now=2.0)
    mail.send("r", "b", "to b", now=3.0)
    mail.send("r", "*", "to all", now=4.0)
    mail.post("r", "a", "s2", kind="reply", reply_to=1, now=5.0)
    assert [(r["agent"], r["text"]) for r in mail.inbox("r")] == [
        ("a", "s1"), ("owner", "to a"), ("owner", "to b"), ("owner", "to all"), ("a", "s2")]
    assert [r["text"] for r in mail.inbox("r", agent="a")] == ["s1", "to a", "to all", "s2"]
    assert [r["text"] for r in mail.inbox("r", owner=False)] == ["s1", "s2"]


def test_follow_prints_each_row_once_even_when_a_late_row_sorts_earlier(mdir, monkeypatch, capsys):
    for i in range(6):
        mail.post("f", "a", f"s{i}", now=10.0 + i)
    calls = {"n": 0}

    def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] == 1:
            mail.send("f", "a", "late-but-earlier", now=14.5)   # appended last, sorts before s5
            mail.post("f", "a", "s6", now=20.0)
        else:
            raise KeyboardInterrupt
    monkeypatch.setattr(mail.time, "sleep", fake_sleep)
    assert mail.main(["inbox", "f", "-n", "2", "--follow"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [l.split(": ", 1)[1] for l in out] == ["s4", "s5", "late-but-earlier", "s6"]


def test_digest_lists_questions_agents_and_only_new_lines_then_advances(mdir):
    mail.post("r", "a", "started", kind="start", now=1.0)
    mail.post("r", "a", "ruling on D-01?", kind="question", now=2.0)
    d = mail.digest("r", advance=True, raw=True)
    assert d["new"] == 2 and d["questions"] == 1 and "WAITING FOR YOUR ANSWER" in d["body"]
    assert "• a: ruling on D-01?" in d["body"] and "• a (working): ruling on D-01?" in d["body"]
    assert d["subject"] == "[apex-router mail] r: 0/1 finished, 1 question for you"
    assert "• 02:00 a asks: ruling on D-01?" in d["body"].replace(
        __import__("time").strftime("%H:%M", __import__("time").localtime(2.0)), "02:00")
    assert "started" not in d["body"].split("SINCE LAST TIME")[1]      # start lines are noise
    d = mail.digest("r", raw=True)
    assert d["new"] == 0 and "Since last time: nothing new." in d["body"]
    mail.send("r", "a", "no, keep it", now=3.0)
    d = mail.digest("r", raw=True)
    assert d["new"] == 1 and d["questions"] == 0 and "you → a: no, keep it" in d["body"]
    assert "Nothing waiting on you." in d["body"]
    assert mail.digest("r", since_cursor=False, raw=True)["new"] == 3
    assert mail.digest("missing")["run"] is None


def test_send_email_uses_mail_app_then_sendmail_and_validates_address(mdir):
    calls = []

    class R:
        def __init__(self, rc):
            self.returncode = rc

    def ok(argv, **kw):
        calls.append((argv, kw.get("input")))
        return R(0)
    assert mail.send_email("x@example.com", 'sub "q"', "body\\line", runner=ok)
    argv, script = calls[0]
    assert argv[0] == "/usr/bin/osascript" and 'subject:"sub \\"q\\""' in script
    assert 'content:"body\\\\line"' in script and 'address:"x@example.com"' in script

    def osa_fails(argv, **kw):
        calls.append((argv, kw.get("input")))
        return R(1 if argv[0].endswith("osascript") else 0)
    calls.clear()
    assert mail.send_email("x@example.com", "s", "b", runner=osa_fails)
    assert [c[0][0] for c in calls] == ["/usr/bin/osascript", "/usr/sbin/sendmail"]
    assert "To: x@example.com\nSubject: s\n" in calls[1][1]
    with pytest.raises(ValueError):
        mail.send_email("not an address", "s", "b", runner=ok)
    with pytest.raises(ValueError):
        mail.send_email('a@b.com" & do shell script "x', "s", "b", runner=ok)


def test_cli_digest_prints_and_sends(mdir, capsys, monkeypatch):
    mail.post("r", "a", "hi", now=1.0)
    assert mail.main(["digest", "r", "--raw"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[apex-router mail] r: 0/1 finished") and "• a (working): hi" in out
    sent = []
    monkeypatch.setattr(mail, "send_email", lambda to, s, b: sent.append((to, s)) or True)
    assert mail.main(["digest", "r", "--to", "x@example.com", "--raw"]) == 0
    assert sent == [("x@example.com", "[apex-router mail] r: 0/1 finished, 0 questions for you")]
    assert mail.main(["digest", "r", "--to", "x@example.com"]) == 0
    assert "nothing new" in capsys.readouterr().out and len(sent) == 1
    assert mail.main(["digest", "nope", "--to", "x@example.com"]) == 1


def test_plain_rewrites_with_the_model_and_falls_back_when_it_fails(mdir):
    seen = []

    def model(prompt):
        seen.append(prompt)
        return ' "Sentence: The landscape analyst finished round one and kept six ideas." '
    out = mail.plain("done: cycle 1 table 49 vendors / 17 segments, 6 shortlisted", kind="done", rewriter=model)
    assert out == "The landscape analyst finished round one and kept six ideas."
    assert "cycle 1 table 49 vendors" in seen[0] and "not an engineer" in seen[0]
    # unusable answers and failures fall back to the squeezed original
    assert mail.plain("status: hello world here", rewriter=lambda p: "") == "hello world here"
    assert mail.plain("status: hello world here", rewriter=lambda p: "x" * 500) == "hello world here"
    assert mail.plain("status: hello world here", rewriter=lambda p: (_ for _ in ()).throw(OSError())) == "hello world here"
    assert mail.plain("hi", rewriter=lambda p: "<think>long</think> Short plain sentence here.") == "Short plain sentence here."
    # unfaithful rewrites (a number or a question the source does not have) are rejected
    src = "shortlist B-09 (Cribl $4.8k-84k/yr), B-15 credit caps"
    assert mail.plain(src, rewriter=lambda p: "Are we ready to spend $4.8k on Cribl?") == src
    assert mail.plain(src, rewriter=lambda p: "Kept 3 ideas including Cribl at $4.8k-84k a year.") == src
    assert mail.plain(src, rewriter=lambda p: "Kept two ideas, Cribl at $4.8k-84k a year and credit caps.") == \
        "Kept two ideas, Cribl at $4.8k-84k a year and credit caps."
    assert mail.faithful("ruling on D-11?", "Do you want to rule on D-11?")


def test_squeeze_drops_prefixes_and_long_parentheses_and_clips():
    assert mail._squeeze("dist-mean status: ack search-blind (0 searches used, fetch-only). Next.") == \
        "ack search-blind (0 searches used, fetch-only). Next."
    assert mail._squeeze("done: kept 6 (" + "x" * 80 + ") rows") == "kept 6 rows"
    long = "decision: " + "word " * 100
    out = mail._squeeze(long)
    assert len(out) <= mail.LINE_MAX and out.endswith("…")


def test_pretty_names():
    assert mail.pretty("w-dist/dist-mean") == "infra researcher (quick)"
    assert mail.pretty("w-bi-landscape/bi-landscape") == "landscape analyst"
    assert mail.pretty("w-ml/reviewer") == "ML reviewer"
    assert mail.pretty("w-market/extra-c2") == "market extra"
    assert mail.pretty("monitor-c2") == "monitor"
    assert mail.pretty("reconciler") == "reconciler"
    assert mail.pretty("w-dist/data-availability") == "infra data availability"


def test_digest_uses_the_rewriter_for_agent_lines_but_not_owner_lines(mdir):
    mail.post("r", "w-dist/dist-mean", "status: decision: dropped D-03 (no MEASURED number)", now=1.0)
    mail.send("r", "*", "keep going", now=2.0)
    d = mail.digest("r", rewriter=lambda p: "The quick infra researcher dropped one idea for lack of a measured number.")
    assert "infra researcher (quick) (working): The quick infra researcher dropped one idea" in d["body"]
    assert "you → everyone: keep going" in d["body"]


def test_ollama_call_disables_thinking_and_returns_response(monkeypatch):
    import io
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["body"] = json.loads(req.data.decode())
        seen["url"] = req.full_url
        return Resp(b'{"response": "Plain sentence.", "done_reason": "stop"}')
    monkeypatch.setattr(mail.urllib.request, "urlopen", fake_urlopen)
    assert mail._ollama("p", url="http://127.0.0.1:1/api/generate", model="m") == "Plain sentence."
    assert seen["body"]["think"] is False and seen["body"]["stream"] is False and seen["body"]["model"] == "m"
