"""The menu's Problems section and the problem alert (owner decision D18: the Electron app is parked, the
menu bar tells the owner about problems).

Rules (fixed): proxy down; worker down; pressure RED / AMBER (the dot rule); upstream error % >= 5 in 15 min;
a 502 count > 0; a job quiet > 10 min; a job that ended failed or capped in the last 60 min; open owner
decisions > 0. Rows are fixed-map words, worst first, max 5 plus "+N more"; each opens the studio at a node.
A failed check shows "problems not checked", never a wrong 0. The alert: a new key (not seen in 30 min), max
1 in 10 min, state in ~/.apex-router/widget/alerts.json, APEX_ROUTER_NO_ALERTS=1 turns it off.
These tests never run osascript (an autouse fixture points it at a path that does not exist)."""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
import sys
import time

import pytest

from apex_router import agent_resources as ar
from apex_router import agent_runs, decisions, snapshot, widget_alerts

BIN = "/Users/you/.local/bin/apex-router"
NOW = 1_791_400_000.0
SECRET = "sk-ant-api03-SECRETSECRET"
ROW_RE = re.compile(r"[a-z0-9 .·%+]+")


@pytest.fixture(autouse=True)
def _no_osascript(monkeypatch, tmp_path):
    monkeypatch.setattr(widget_alerts, "OSASCRIPT", str(tmp_path / "no-osascript-in-tests"))
    monkeypatch.setattr(ar, "run_cmd", lambda argv, timeout=2.0: "")
    monkeypatch.setattr(ar, "http_get", lambda url, timeout=1.0: b"{}")
    monkeypatch.delenv("APEX_ROUTER_NO_ALERTS", raising=False)
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path / "rh"))


def _calm(**kw):
    """A snapshot with every source checked and no problem."""
    s = {"ts": NOW, "agents": [], "system": {},
         "pressure": {"level": "GREEN", "insufficient_sample": False, "families": {}},
         "proxy": {"up": True, "port": 8788},
         "worker": {"label": "com.ornith.worker", "pid": 500, "running": True, "inbox": 0, "queue_running": 0},
         "flow": {"error_pct": 0.0, "retries": 0, "unreachable": 0, "proxy": {"up": True}},
         "jobs": {"running": 0, "queued": 0, "items": []},
         "jobs_ended": {"items": []},
         "decisions": {"open": 0}}
    s.update(kw)
    return s


def _kinds(snap):
    return [it["kind"] for it in snapshot.problems(snap)["items"]]


def _menu(snap, bin_path=BIN):
    snap = dict(snap, problems=snapshot.problems(snap))
    return snapshot.menubar(snap, bin_path=bin_path)


def _section(text):
    """Visible text of the Problems section rows (header excluded)."""
    lines = text.splitlines()
    hdr = [i for i, ln in enumerate(lines) if ln.startswith("Problems · ")]
    if not hdr:
        return []
    out = []
    for ln in lines[hdr[0] + 1:]:
        if ln == "---":
            break
        out.append(ln)
    return out


def _params(line):
    return dict(p.split("=", 1) for p in shlex.split(line.split(" | ", 1)[1]))


# ---------------------------------------------------------------- each rule

def test_calm_snapshot_has_no_problem_and_no_section():
    pb = snapshot.problems(_calm())
    assert pb["n"] == 0 and pb["items"] == [] and pb["unchecked"] == []
    text = _menu(_calm())
    assert "Problems" not in text and "not checked" not in text
    assert text.splitlines()[0] == "● 0 | color=" + snapshot.COLORS["green"]


@pytest.mark.parametrize("change,kind", [
    ({"proxy": {"up": False, "error": "URLError: refused"}}, "proxy_down"),
    ({"worker": {"label": "w", "pid": None, "running": False, "inbox": 0}}, "worker_down"),
    ({"pressure": {"level": "RED", "insufficient_sample": False, "families": {}}}, "pressure_red"),
    ({"pressure": {"level": "AMBER", "insufficient_sample": False, "families": {}}}, "pressure_amber"),
    ({"pressure": {"level": "GREEN", "insufficient_sample": True, "families": {"opus": "RED"}}}, "pressure_red"),
    ({"pressure": {"level": "GREEN", "insufficient_sample": True, "families": {"opus": "AMBER"}}}, "pressure_amber"),
    ({"flow": {"error_pct": 5.0, "retries": 0, "unreachable": 0}}, "upstream_errors"),
    ({"flow": {"error_pct": 0.0, "retries": 0, "unreachable": 2}}, "upstream_502"),
    ({"jobs": {"running": 1, "queued": 0, "items": [
        {"id": "20261027000001-aaaa", "role": "worker", "state": "running", "age_s": 900, "quiet_s": 601}]}},
     "job_quiet"),
    ({"jobs_ended": {"items": [{"id": "20261027000001-aaaa", "role": "worker", "state": "failed",
                                "capped": False, "ended_ago_s": 59 * 60}]}}, "job_failed"),
    ({"jobs_ended": {"items": [{"id": "20261027000001-aaaa", "role": "worker", "state": "timed_out(idle)",
                                "capped": False, "ended_ago_s": 60}]}}, "job_failed"),
    ({"jobs_ended": {"items": [{"id": "20261027000001-aaaa", "role": "worker", "state": "killed(cost)",
                                "capped": False, "ended_ago_s": 60}]}}, "job_capped"),
    ({"jobs_ended": {"items": [{"id": "20261027000001-aaaa", "role": "worker", "state": "done",
                                "capped": True, "ended_ago_s": 60}]}}, "job_capped"),
    ({"decisions": {"open": 2}}, "decisions_open"),
])
def test_each_rule_gives_its_problem(change, kind):
    assert _kinds(_calm(**change)) == [kind]


@pytest.mark.parametrize("change", [
    {"flow": {"error_pct": 4.9, "retries": 3, "unreachable": 0}},          # below 5 %, retries alone: no
    {"flow": {"error_pct": None, "retries": 0, "unreachable": 0}},          # no telemetry: not a problem
    {"pressure": {"level": "GREEN", "insufficient_sample": True, "families": {}}},   # gray dot
    {"pressure": {"level": "UNKNOWN", "insufficient_sample": True, "families": {}}},
    {"jobs": {"running": 1, "queued": 0, "items": [
        {"id": "x", "role": "worker", "state": "running", "age_s": 900, "quiet_s": 600}]}},
    {"jobs_ended": {"items": [{"id": "x", "role": "worker", "state": "failed", "capped": False,
                               "ended_ago_s": 61 * 60}]}},                  # older than 60 min
    {"jobs_ended": {"items": [{"id": "x", "role": "worker", "state": "done", "capped": False,
                               "ended_ago_s": 60}]}},
    {"jobs_ended": {"items": [{"id": "x", "role": "worker", "state": "killed", "capped": False,
                               "ended_ago_s": 60}]}},                       # the owner stopped it
    {"decisions": {"open": 0}},
])
def test_below_the_threshold_is_not_a_problem(change):
    assert _kinds(_calm(**change)) == []


def test_pressure_problem_follows_the_dot_rule():
    for p in ({"level": "RED", "insufficient_sample": True, "families": {}},
              {"level": "AMBER", "insufficient_sample": False, "families": {"a": "RED"}},
              {"level": "GREEN", "insufficient_sample": False, "families": {"a": "AMBER"}}):
        snap = _calm(pressure=p)
        want = {"red": ["pressure_red"], "orange": ["pressure_amber"]}[snapshot.dot(snap)]
        assert _kinds(snap) == want


# ---------------------------------------------------------------- order, cap, bar

def _many():
    return _calm(
        decisions={"open": 1},
        proxy={"up": False, "error": "refused"},
        flow={"error_pct": 12.0, "retries": 1, "unreachable": 3},
        worker={"label": "w", "pid": None, "running": False},
        pressure={"level": "AMBER", "insufficient_sample": False, "families": {}},
        jobs={"running": 1, "queued": 0, "items": [
            {"id": "20261027000009-qqqq", "role": "judge", "state": "running", "age_s": 2000, "quiet_s": 1200}]},
        jobs_ended={"items": [
            {"id": "20261027000002-ffff", "role": "worker", "state": "failed", "capped": False, "ended_ago_s": 300},
            {"id": "20261027000003-cccc", "role": "reviewer", "state": "killed(cost)", "capped": False,
             "ended_ago_s": 300}]})


def test_order_is_worst_first_with_a_fixed_rank():
    assert _kinds(_many()) == ["proxy_down", "worker_down", "upstream_502", "job_failed",
                               "upstream_errors", "pressure_amber", "job_quiet", "job_capped",
                               "decisions_open"]
    sev = [it["sev"] for it in snapshot.problems(_many())["items"]]
    assert sev == sorted(sev, key=["red", "amber"].index)


def test_section_is_first_under_the_bar_line_max_5_rows_plus_more():
    text = _menu(_many())
    lines = text.splitlines()
    assert lines[1] == "---" and lines[2].startswith("Problems · 9 | ")
    rows = _section(text)
    vis = [snapshot.visible(r) for r in rows]
    assert len(vis) == 6 and vis[-1] == "+4 more"
    assert vis[0].startswith("proxy down · ") and vis[1].startswith("worker down · ")
    assert lines[2 + len(rows) + 1] == "---"
    assert any(ln.startswith("Flow studio ↗ | ") for ln in lines[2 + len(rows):])


def test_bar_shows_the_count_and_the_dot_rule_stays():
    snap = _many()
    bar = _menu(snap).splitlines()[0]
    assert bar == f"● 0 ⚠ 9 | color={snapshot.COLORS[snapshot.dot(snap)]}"
    calm = _calm(pressure=snap["pressure"])
    assert snapshot.dot(calm) == snapshot.dot(snap)                     # problems do not change the dot


def test_rows_open_the_studio_at_a_node_the_studio_accepts():
    from apex_router.studio import server
    rows = [r for r in _section(_menu(_many())) if not snapshot.visible(r).startswith("+")]
    assert len(rows) == 5
    for r in rows:
        p = _params(r)
        assert p["bash"] == BIN and p["param1"] == "studio" and p["param4"] == "--open", r
        assert server.target_hash(p["param5"]) not in (None, ""), r
        assert set(p) <= {"bash", "param1", "param2", "param3", "param4", "param5", "terminal",
                          "refresh", "color"}, r
    every = snapshot.problems(_many())["items"]
    for it in every:
        assert server.target_hash(it["target"]) not in (None, ""), it


def test_rows_without_a_valid_binary_have_no_action_but_still_show():
    text = _menu(_many(), bin_path="/x;rm")
    assert "bash=" not in text and len(_section(text)) == 6


# ---------------------------------------------------------------- no raw text

def test_rows_carry_no_raw_error_prompt_or_token():
    bad = f"{SECRET} | bash=/bin/sh param1=-c\nYou are the worker. TASK: leak"
    snap = _calm(
        proxy={"up": False, "error": bad, "version": bad},
        worker={"label": bad, "pid": None, "running": False, "error": bad},
        pressure={"level": "RED", "insufficient_sample": False, "families": {bad: "RED"},
                  "recommendation": bad, "causes": {"rate_limited": 3}},
        flow={"error_pct": 50.0, "retries": 0, "unreachable": 1, "error": None},
        jobs={"running": 1, "queued": 0, "items": [
            {"id": "../../" + bad, "role": bad, "state": bad, "age_s": 9000, "quiet_s": 9000}]},
        jobs_ended={"items": [{"id": bad, "role": bad, "state": "failed", "capped": False, "ended_ago_s": 1,
                               "task": bad}]},
        decisions={"open": 1, "rows": [{"text": bad}]})
    pb = snapshot.problems(snap)
    assert pb["n"] == 8
    text = _menu(snap)
    rows = _section(text)
    for r in rows:
        assert ROW_RE.fullmatch(snapshot.visible(r)), r
    blob = "\n".join([text.splitlines()[0]] + rows) + json.dumps(pb)    # the bar, the rows, the block
    for s in (SECRET, "bash=/bin/sh", "You are", "TASK", "../", "leak"):
        assert s not in blob, s
    for it in pb["items"]:
        assert re.fullmatch(r"[a-z0-9_:.-]{1,80}", it["key"]), it["key"]
    msg = widget_alerts.decide({}, pb["items"], NOW)[1]
    assert msg and widget_alerts.MESSAGE_RE.fullmatch(msg) and SECRET not in msg


# ---------------------------------------------------------------- not checked, never a wrong 0

def test_a_failed_problem_check_shows_not_checked_never_zero():
    snap = _calm()
    snap["problems"] = {"error": "RuntimeError: boom"}
    text = snapshot.menubar(snap, bin_path=BIN)
    lines = text.splitlines()
    assert snapshot.visible(lines[2]) == "problems not checked"
    assert "Problems · 0" not in text and "boom" not in text
    assert lines[0] == "● 0 | color=" + snapshot.COLORS["green"]


@pytest.mark.parametrize("change,src", [
    ({"proxy": {"up": False, "skipped": True, "error": "skipped: deadline"}}, "proxy"),
    ({"worker": {"label": "w", "error": "DeadlineSkip: deadline exceeded, source skipped"}}, "worker"),
    ({"pressure": {"error": "OSError: x"}}, "pressure"),
    ({"flow": {"skipped": True, "error": "skipped: deadline"}}, "upstream"),
    ({"jobs": {"error": "OSError: x"}}, "jobs"),
    ({"jobs_ended": {"skipped": True, "error": "skipped: deadline"}}, "jobs"),
    ({"decisions": {"skipped": True, "error": "skipped: deadline"}}, "decisions"),
])
def test_a_source_not_read_is_named_not_counted_as_zero(change, src):
    snap = _calm(**change)
    pb = snapshot.problems(snap)
    assert pb["n"] == 0 and pb["unchecked"] == [src]
    lines = _menu(snap).splitlines()
    assert snapshot.visible(lines[2]) == f"problems not checked · {src}"
    assert "Problems ·" not in "\n".join(lines)


def test_unchecked_sources_are_named_under_a_non_empty_section():
    snap = _calm(proxy={"up": False, "error": "refused"},
                 decisions={"skipped": True, "error": "skipped: deadline"})
    rows = [snapshot.visible(r) for r in _section(_menu(snap))]
    assert rows[0].startswith("proxy down · ") and rows[-1] == "not checked · decisions"


def test_missing_sources_count_as_not_checked():
    snap = {"ts": NOW, "agents": [], "system": {}}
    pb = snapshot.problems(snap)
    assert pb["n"] == 0
    assert pb["unchecked"] == ["proxy", "worker", "pressure", "upstream", "jobs", "decisions"]


# ---------------------------------------------------------------- slow source: the deadline

def _collect(tmp_path, monkeypatch, deadline, flow_fn):
    (tmp_path / "tel.jsonl").write_text("")                  # telemetry present: pressure is checked
    called = []
    monkeypatch.setattr(decisions, "read_rows", lambda: called.append("decisions") or ([], 0))
    real = agent_runs.ended_jobs
    monkeypatch.setattr(agent_runs, "ended_jobs", lambda *a, **k: called.append("ended") or real(*a, **k))
    snap = snapshot.collect(
        home=tmp_path / "home", telemetry=tmp_path / "tel.jsonl", observe_dir=tmp_path / "obs",
        adapters=tmp_path / "ad", now=NOW, deadline=deadline, flow_fn=flow_fn,
        proxy_fn=lambda: {"up": True, "port": 8788},
        worker_fn=lambda: {"label": "w", "pid": 1, "running": True, "inbox": 0, "queue_running": 0},
        resources_fn=lambda *a, **k: {"agents": [], "system": {}}, jobs_dir=tmp_path / "jobs")
    return snap, called


def test_a_passed_deadline_skips_the_problem_reads_and_says_not_checked(tmp_path, monkeypatch):
    snap, called = _collect(tmp_path, monkeypatch, ar.Deadline(budget=-1.0),
                            lambda: {"error_pct": 0.0, "retries": 0, "unreachable": 0})
    assert called == []
    assert set(snap["problems"]["unchecked"]) == {"jobs", "decisions"}
    assert snapshot.visible(snapshot.menubar(snap).splitlines()[2]) == "problems not checked · jobs, decisions"


def test_a_slow_source_uses_up_the_deadline_and_later_reads_are_skipped(tmp_path, monkeypatch):
    t = [0.0]
    dl = ar.Deadline(budget=1.2, clock=lambda: t[0])

    def slow_flow():
        t[0] += 5.0                                          # the flow read took 5 s
        return {"error_pct": 0.0, "retries": 0, "unreachable": 0}
    snap, called = _collect(tmp_path, monkeypatch, dl, slow_flow)
    assert called == [] and set(snap["problems"]["unchecked"]) == {"jobs", "decisions"}


def test_with_time_left_the_reads_run_and_the_result_is_checked(tmp_path, monkeypatch):
    t = [0.0]
    snap, called = _collect(tmp_path, monkeypatch, ar.Deadline(budget=1.2, clock=lambda: t[0]),
                            lambda: {"error_pct": 0.0, "retries": 0, "unreachable": 0})
    assert sorted(called) == ["decisions", "ended"]
    assert snap["problems"]["unchecked"] == [] and snap["problems"]["n"] == 0


def test_a_failed_read_is_not_checked(tmp_path, monkeypatch):
    def boom():
        raise PermissionError("denied")

    def boom2(*a, **k):
        raise PermissionError("denied")
    (tmp_path / "tel.jsonl").write_text("")
    monkeypatch.setattr(decisions, "read_rows", boom)
    monkeypatch.setattr(agent_runs, "ended_jobs", boom2)
    snap2 = snapshot.collect(
        home=tmp_path / "home", telemetry=tmp_path / "tel.jsonl", observe_dir=tmp_path / "obs",
        adapters=tmp_path / "ad", now=NOW, flow_fn=lambda: {"error_pct": 0.0, "retries": 0, "unreachable": 0},
        proxy_fn=lambda: {"up": True}, worker_fn=lambda: {"label": "w", "pid": 1},
        resources_fn=lambda *a, **k: {"agents": [], "system": {}}, jobs_dir=tmp_path / "jobs")
    assert snap2["problems"]["unchecked"] == ["jobs", "decisions"]
    assert "denied" not in snapshot.menubar(snap2)


# ---------------------------------------------------------------- ended jobs scan

def _meta(d, jid, **m):
    j = d / jid
    j.mkdir(parents=True)
    (j / "meta.json").write_text(json.dumps(dict({"id": jid, "agent": "worker", "task": "TASK: x"}, **m)))


def test_ended_jobs_reads_final_states_with_their_end_time(tmp_path):
    now = time.time()
    ms = lambda ago: int((now - ago) * 1000)  # noqa: E731
    _meta(tmp_path, "20261027000001-aaaa", state="failed", started=ms(900), ended=ms(300))
    _meta(tmp_path, "20261027000002-bbbb", state="done", started=ms(900), ended=ms(200), capped=True)
    _meta(tmp_path, "20261027000003-cccc", state="running", pid=os.getpid(), started=ms(100))
    big = tmp_path / "20261027000004-dddd"
    big.mkdir()
    (big / "meta.json").write_text('{"state": "failed", "x": "%s"}' % ("a" * 300_000))
    out = agent_runs.ended_jobs(tmp_path, now)
    by = {i["id"]: i for i in out["items"]}
    assert set(by) == {"20261027000001-aaaa", "20261027000002-bbbb"}
    assert by["20261027000001-aaaa"]["state"] == "failed" and by["20261027000001-aaaa"]["ended_ago_s"] == 300
    assert by["20261027000002-bbbb"]["capped"] is True
    assert "task" not in by["20261027000001-aaaa"]
    assert agent_runs.ended_jobs(tmp_path / "missing", now) == {"items": []}


# ---------------------------------------------------------------- alert: dedupe, rate, state

def _items(*keys):
    return [{"key": k, "kind": k.split(":")[0], "sev": "red"} for k in keys]


def test_a_new_key_alerts_once_and_a_seen_key_does_not():
    st, msg = widget_alerts.decide({}, _items("proxy_down"), NOW)
    assert msg == "proxy down"
    st, msg = widget_alerts.decide(st, _items("proxy_down"), NOW + 60)
    assert msg is None
    st, msg = widget_alerts.decide(st, _items("proxy_down"), NOW + 25 * 60)
    assert msg is None                                         # still present: never again


def test_dedupe_window_is_30_minutes():
    st, _ = widget_alerts.decide({}, _items("proxy_down"), NOW)
    st, msg = widget_alerts.decide(st, [], NOW + 60)            # gone
    st2, msg = widget_alerts.decide(st, _items("proxy_down"), NOW + 29 * 60)
    assert msg is None                                         # back within 30 min: seen
    st3, msg = widget_alerts.decide(st, _items("proxy_down"), NOW + 31 * 60)
    assert msg == "proxy down"                                 # back after 30 min: new


def test_rate_limit_is_one_alert_in_10_minutes_and_a_held_key_alerts_later():
    st, msg = widget_alerts.decide({}, _items("proxy_down"), NOW)
    assert msg
    st, msg = widget_alerts.decide(st, _items("proxy_down", "worker_down"), NOW + 5 * 60)
    assert msg is None                                         # new key, but within 10 min
    st, msg = widget_alerts.decide(st, _items("proxy_down", "worker_down"), NOW + 9 * 60)
    assert msg is None
    st, msg = widget_alerts.decide(st, _items("proxy_down", "worker_down"), NOW + 10 * 60)
    assert msg == "worker down"                                # held, not dropped


def test_several_new_keys_make_one_alert():
    _, msg = widget_alerts.decide({}, _items("proxy_down", "worker_down", "decisions_open"), NOW)
    assert msg == "proxy down and 2 more"


def test_state_is_bounded():
    many = _items(*[f"job_failed:j{i:05d}" for i in range(1000)])
    st, _ = widget_alerts.decide({}, many, NOW)
    assert len(st["seen"]) <= widget_alerts.KEYS_MAX
    assert len(json.dumps(st)) < widget_alerts.STATE_BYTES_MAX


def test_state_file_write_is_atomic_and_private(tmp_path):
    p = tmp_path / "widget" / "alerts.json"
    assert widget_alerts.save({"v": 1, "last_alert": NOW, "seen": {"proxy_down": NOW}}, p)
    assert widget_alerts.load(p)["seen"] == {"proxy_down": NOW}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert sorted(x.name for x in p.parent.iterdir()) == ["alerts.json"]     # no temp file left


@pytest.mark.parametrize("body", [b"{bad", b"[1,2]", b'{"seen": {"a": "x"}, "last_alert": "y"}',
                                  b'{"seen": {"proxy_down": ' + b"1" * 70_000 + b"}}"])
def test_a_bad_or_oversized_state_file_is_read_as_empty(tmp_path, body):
    p = tmp_path / "alerts.json"
    p.write_bytes(body)
    st = widget_alerts.load(p)
    assert st.get("seen", {}) == {} or all(isinstance(v, float) for v in st["seen"].values())
    assert widget_alerts.decide(st, _items("proxy_down"), NOW)[1] == "proxy down"


def test_load_reads_at_most_the_cap(tmp_path, monkeypatch):
    p = tmp_path / "alerts.json"
    p.write_bytes(b" " * (10 << 20))
    import builtins
    sizes = []
    real_open = builtins.open

    class Spy:
        def __init__(self, f):
            self.f = f

        def read(self, n=-1):
            sizes.append(n)
            return self.f.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()
    monkeypatch.setattr(builtins, "open", lambda *a, **k: Spy(real_open(*a, **k)))
    assert widget_alerts.load(p) == {}
    assert sizes and all(0 < n <= widget_alerts.STATE_BYTES_MAX + 1 for n in sizes)


def test_run_writes_state_then_notifies_once(tmp_path):
    p = tmp_path / "alerts.json"
    sent = []
    pb = {"n": 1, "items": _items("proxy_down"), "unchecked": []}
    assert widget_alerts.run(pb, now=NOW, path=p, notifier=sent.append) == "proxy down"
    assert widget_alerts.run(pb, now=NOW + 60, path=p, notifier=sent.append) is None
    assert sent == ["proxy down"] and json.loads(p.read_text())["last_alert"] == NOW


def test_run_does_not_notify_when_the_state_cannot_be_saved(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "alerts.json").mkdir()                              # a directory: the replace fails
    sent = []
    pb = {"n": 1, "items": _items("proxy_down"), "unchecked": []}
    assert widget_alerts.run(pb, now=NOW, path=ro / "alerts.json", notifier=sent.append) is None
    assert sent == []


def test_opt_out_env_sends_nothing_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_ROUTER_NO_ALERTS", "1")
    p = tmp_path / "alerts.json"
    sent = []
    pb = {"n": 1, "items": _items("proxy_down"), "unchecked": []}
    assert widget_alerts.run(pb, now=NOW, path=p, notifier=sent.append) is None
    assert sent == [] and not p.exists()


def test_a_failed_problem_check_sends_no_alert(tmp_path):
    sent = []
    for pb in ({"error": "x"}, None, {"items": "x"}):
        assert widget_alerts.run(pb, now=NOW, path=tmp_path / "a.json", notifier=sent.append) is None
    assert sent == []


def test_default_path_is_under_the_widget_dir(tmp_path):
    assert widget_alerts.path() == tmp_path / "rh" / "widget" / "alerts.json"


# ---------------------------------------------------------------- notify: osascript

def test_notify_passes_the_text_as_an_argument_with_a_3s_timeout(monkeypatch):
    from apex_router import safe_proc
    calls = []
    monkeypatch.setattr(safe_proc, "run_capped", lambda argv, timeout, out_max, **k: calls.append(
        (argv, timeout)) or (0, b""))
    assert widget_alerts.NOTIFY_TIMEOUT_S == 3
    assert widget_alerts.notify("proxy down and 2 more") is True
    (argv, timeout), = calls
    assert timeout == 3 and argv[0] == widget_alerts.OSASCRIPT
    assert argv[-1] == "proxy down and 2 more"
    script = " ".join(argv[1:-1])
    assert "proxy down" not in script and "item 1 of argv" in script


@pytest.mark.parametrize("msg", ['x" & do shell script "id', "a\nb", "-e", "", "x" * 200, SECRET, None])
def test_notify_refuses_text_outside_the_fixed_words(monkeypatch, msg):
    from apex_router import safe_proc
    calls = []
    monkeypatch.setattr(safe_proc, "run_capped", lambda *a, **k: calls.append(a) or (0, b""))
    assert widget_alerts.notify(msg) is False and calls == []


def test_notify_stops_a_hung_osascript_at_the_timeout(tmp_path, monkeypatch):
    fake = tmp_path / "slow-osascript"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o700)
    monkeypatch.setattr(widget_alerts, "OSASCRIPT", str(fake))
    monkeypatch.setattr(widget_alerts, "NOTIFY_TIMEOUT_S", 0.5)
    t0 = time.monotonic()
    assert widget_alerts.notify("proxy down") is False
    assert time.monotonic() - t0 < 2.5


def test_notify_with_a_missing_binary_fails_closed():
    assert widget_alerts.notify("proxy down") is False          # the autouse path does not exist


# ---------------------------------------------------------------- CLI: --alert

def test_main_alerts_only_with_the_flag(monkeypatch, capsys):
    snap = _many()
    snap["problems"] = snapshot.problems(snap)
    monkeypatch.setattr(snapshot, "collect", lambda **k: snap)
    got = []
    monkeypatch.setattr(widget_alerts, "run", lambda pb, **k: got.append(pb) or None)
    assert snapshot.main(["--menubar", "--no-history"]) == 0
    assert got == []
    assert snapshot.main(["--menubar", "--no-history", "--alert"]) == 0
    assert got == [snap["problems"]]
    assert "Problems · 9" in capsys.readouterr().out


def test_main_alert_failure_never_blanks_the_menu(monkeypatch, capsys):
    snap = _many()
    snap["problems"] = snapshot.problems(snap)
    monkeypatch.setattr(snapshot, "collect", lambda **k: snap)

    def boom(pb, **k):
        raise OSError("disk")
    monkeypatch.setattr(widget_alerts, "run", boom)
    assert snapshot.main(["--menubar", "--no-history", "--alert"]) == 0
    assert "Problems · 9" in capsys.readouterr().out


def test_swiftbar_script_asks_for_alerts():
    sh = (snapshot.Path(__file__).parent.parent / "integrations" / "swiftbar" / "apex.1m.sh").read_text()
    assert "snapshot --menubar --alert" in sh and "APEX_ROUTER_NO_ALERTS" in sh
