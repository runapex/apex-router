"""Iteration 4 of the menu-bar widget: history file, sparklines, click actions, detail pages."""
from __future__ import annotations

import html.parser
import json
import math
import multiprocessing
import os
import re
import shlex
import time
from pathlib import Path

import pytest

from apex_router import agent_resources as ar
from apex_router import snapshot, widget_detail, widget_history

NOW = 1_790_000_000.0
SID = "d5443384-0a68-4edf-82e8-1d130c562c8e"
AID = "a1cacfc99af4755f9"
BIN = "/Users/you/.local/bin/apex-router"


@pytest.fixture(autouse=True)
def _no_real_sources(monkeypatch):
    monkeypatch.setattr(ar, "run_cmd", lambda argv, timeout=2.0: "")
    monkeypatch.setattr(ar, "http_get", lambda url, timeout=1.0: b"{}")
    monkeypatch.setattr(ar, "rusage", lambda pid: None)


def _agent(sid=SID, desc="find x", subs=None, status="busy", series=None):
    res = {"status": status,
           "tree": {"pid": 2894, "alive": True, "footprint_mb": 705.0, "cpu_pct": 0.5,
                    "cpu_src": "sampled", "read_mbs": 0.1, "write_mbs": 0.0, "uptime_s": 3600,
                    "top": [{"pid": 18188, "name": "pyright-langserver", "footprint_mb": 292.0,
                             "cpu_pct": 0.0, "rss_mb": 200.0}]},
           "telemetry": {"requests": 4, "tokens_out": 2500, "tokens_in": 10, "cache_read": 1000,
                         "cache_write": 5, "errors": 0, "ctx_tokens": 293_000,
                         "ctx_window": 1_000_000, "ctx_pct": 29, "req_5m": 3,
                         "models": {"claude-opus-5-5": 4}},
           "subagents": {"list": subs if subs is not None else [
               {"id": AID, "type": "Explore", "description": desc, "state": "running",
                "flags": [], "age_s": 3.0, "last_s": 3.0,
                "telemetry": {"requests": 2, "tokens_out": 10, "models": {"claude-opus-5-5": 2}}}],
               "count": 1, "running": 1}}
    if series is not None:
        res["series"] = series
    return {"kind": "claude", "repo": "apex-router", "session": sid[:8], "session_id": sid,
            "state": "active", "age_s": 3.0, "res": res}


def _snap(agents=None):
    return {"ts": NOW, "pressure": {"level": "GREEN", "insufficient_sample": False},
            "agents": agents if agents is not None else [_agent()],
            "system": {"gpu_util_pct": 6, "gpu_mem_mb": 1510.0, "loadavg": [3.29, 3.1, 2.7],
                       "ollama": [{"name": "qwen3:8b", "vram_mb": 5120.0}]}}


# ---------------------------------------------------------------- history

def test_sample_is_numbers_states_and_ids_only():
    s = widget_history.sample(_snap([_agent(desc="SECRET description")]))
    assert s["v"] == 1 and s["ts"] == NOW
    a = s["agents"][0]
    assert a == {"session": SID, "kind": "claude", "status": "busy", "footprint_mb": 705.0,
                 "cpu_pct": 0.5, "io_mbs": 0.1, "ctx": 293_000, "ctx_window": 1_000_000,
                 "req_5m": 3, "subagents": [{"id": AID, "state": "running"}]}
    assert s["system"] == {"gpu_util_pct": 6, "gpu_mem_mb": 1510.0, "load1": 3.29}
    assert "SECRET" not in json.dumps(s) and "apex-router" not in json.dumps(s)


def test_append_and_load_roundtrip(tmp_path):
    p = tmp_path / "w" / "history.jsonl"
    for i in range(3):
        snap = _snap()
        snap["ts"] = NOW + i * 60
        assert widget_history.append(snap, path=p, now=NOW + i * 60)
    rows = widget_history.load(0, path=p)
    assert [r["ts"] for r in rows] == [NOW, NOW + 60, NOW + 120]
    assert widget_history.load(NOW + 30, path=p)[0]["ts"] == NOW + 60
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert oct(p.parent.stat().st_mode & 0o777) == "0o700"
    assert widget_history.agent_series(rows, SID, "cpu_pct") == [(NOW, 0.5), (NOW + 60, 0.5),
                                                                (NOW + 120, 0.5)]
    assert widget_history.system_series(rows, "gpu_util_pct")[0] == (NOW, 6)


def test_default_path_honours_apex_router_home(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path / "arh"))
    assert widget_history.history_path() == tmp_path / "arh" / "widget" / "history.jsonl"
    assert widget_history.append(_snap(), now=NOW)
    assert (tmp_path / "arh" / "widget" / "history.jsonl").exists()


def test_trim_by_age_and_by_bytes(tmp_path):
    p = tmp_path / "history.jsonl"
    old = widget_history.sample(_snap())
    rows = []
    for i in range(50):
        r = dict(old, ts=NOW - 30 * 3600 + i * 3600)          # 30 h ago .. 19 h ahead
        rows.append(json.dumps(r))
    p.write_text("\n".join(rows) + "\n")
    assert widget_history.append(dict(old, ts=NOW), path=p, now=NOW)
    kept = widget_history.load(0, path=p)
    assert kept and min(r["ts"] for r in kept) >= NOW - 24 * 3600
    # bytes: a 4 KB cap trims to 3 KB, newest lines kept
    q = tmp_path / "b.jsonl"
    for i in range(40):
        widget_history.append(dict(old, ts=NOW + i), path=q, now=NOW + i, max_bytes=4096,
                              trim_to=3072)
        assert q.stat().st_size <= 4096
    got = widget_history.load(0, path=q)
    assert got[-1]["ts"] == NOW + 39 and len(got) < 40


def test_load_skips_garbage_and_reads_only_the_tail(tmp_path):
    p = tmp_path / "history.jsonl"
    good = json.dumps(widget_history.sample(_snap()))
    p.write_text("not json\n" + good + "\n{\"agents\": 5, \"ts\": 1}\n[1]\n" + good[:20])
    assert len(widget_history.load(0, path=p)) == 1
    big = "\n".join([good] * 200) + "\n"
    p.write_text(big)
    assert 0 < len(widget_history.load(0, path=p, max_bytes=len(good) * 3)) <= 3


def test_append_fails_open(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert widget_history.append(_snap(), path=blocker / "history.jsonl") is False
    assert widget_history.append({"agents": "junk", "system": 5}, path=tmp_path / "h") in (True,
                                                                                          False)
    assert widget_history.load(0, path=tmp_path / "missing") == []


def test_append_skips_when_the_lock_is_held(tmp_path):
    import fcntl
    p = tmp_path / "history.jsonl"
    with open(tmp_path / "history.jsonl.lock", "a") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        t0 = time.monotonic()
        assert widget_history.append(_snap(), path=p, now=NOW) is False
        assert time.monotonic() - t0 < 0.5
    assert widget_history.append(_snap(), path=p, now=NOW) is True


def _hammer(path: str, n: int, wid: int) -> None:
    base = widget_history.sample(_snap())
    for i in range(n):
        widget_history.append(dict(base, ts=NOW + wid * 1000 + i), path=Path(path), now=NOW + 999,
                              max_bytes=20_000, trim_to=15_000)


def test_concurrent_writers_never_corrupt_the_file(tmp_path):
    p = tmp_path / "history.jsonl"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_hammer, args=(str(p), 40, w)) for w in range(4)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(30)
    lines = [ln for ln in p.read_text().splitlines() if ln]
    assert lines
    for ln in lines:                                             # every line parses
        assert json.loads(ln)["v"] == 1
    assert p.stat().st_size <= 20_000


# ---------------------------------------------------------------- sparklines

def test_sparkline_scaling_gaps_and_constants():
    sp = widget_history.sparkline
    assert sp([0, 1, 2, 3, 4, 5, 6, 7]) == "▁▂▃▄▅▆▇█"
    assert sp([600, 660, 720], zero=False) == "▁▅█"
    assert sp([700, 705, 710], zero=False) == "▆▇█"               # span floor: 5% of the max
    assert sp([700, 705, 710]) == "███"                          # 0-based: all near the top
    assert sp([1, None, float("nan"), float("inf"), 2]) == "▅   █"   # 1/2 of 7 steps rounds up
    assert sp([0, 0, 0]) == "▁▁▁"
    assert sp([5, 5]) == "██" and sp([5, 5], zero=False) == "▄▄"
    assert sp([]) == "" and sp([None, float("nan")]) == "" and sp(None) == ""
    assert len(sp(list(range(100)))) == widget_history.SPARK_POINTS
    assert sp([3]) == "█"
    assert sp([-1, 1]) == "▁█"


def test_telemetry_buckets():
    rows = [{"session_id": "s", "ts": NOW - 10, "tokens_out": 5, "tokens_in": 1,
             "cache_read_tokens": 100, "cache_write_tokens": 2},
            {"session_id": "s", "ts": NOW - 301, "tokens_out": 7, "is_error": True},
            {"session_id": "s", "ts": NOW - 3601},                # outside 60 min
            {"session_id": None, "ts": NOW - 5}, {"ts": NOW}, "junk",
            {"session_id": "t", "ts": float("nan")}]
    b = widget_history.buckets(rows, NOW)
    assert set(b) == {"s"}
    s = b["s"]
    assert len(s["req"]) == 12 and s["req"][-1] == 1 and s["req"][-2] == 1 and sum(s["req"]) == 2
    assert s["out"][-1] == 5 and s["out"][-2] == 7 and s["err"][-2] == 1
    assert s["cached"][-1] == 100 and s["write"][-1] == 2 and s["in"][-1] == 1
    starts = widget_detail.bucket_starts(NOW)
    assert len(starts) == 72 and starts[-1] <= NOW < starts[-1] + 300
    d = widget_detail.bucketize(rows, starts)
    assert sum(d["req"]) == 5 and sum(d["err"]) == 1             # 6 h, every session
    assert next(iter(d["causes"].values())) == {"unlabeled": 1}


# ---------------------------------------------------------------- menu: sparklines + actions

def _hist(n=10, sid=SID):
    out = []
    for i in range(n):
        s = widget_history.sample(_snap())
        s["ts"] = NOW - (n - i) * 60
        s["agents"][0]["cpu_pct"] = float(i)
        s["agents"][0]["footprint_mb"] = 700.0 + i
        s["agents"][0]["session"] = sid
        out.append(s)
    return out


def _actions(text):
    """{visible text: params dict} for every line with a bash= action."""
    out = []
    for ln in text.splitlines():
        assert ln.count("|") <= 1, ln                            # one separator, ever
        if " | " not in ln or "bash=" not in ln.split(" | ", 1)[1]:
            continue                                             # 'bash=' as visible text only
        params = shlex.split(ln.split(" | ", 1)[1])
        out.append((ln, dict(p.split("=", 1) for p in params)))
    return out


def test_menu_sparklines_from_history_and_telemetry():
    series = {"req": [0, 0, 1, 5, 2, 0, 0, 0, 0, 0, 3, 22], "out": [0] * 11 + [9800]}
    text = snapshot.menubar(_snap([_agent(series=series)]), history=_hist(), bin_path=BIN)
    vis = [snapshot.visible(ln) for ln in text.splitlines()]
    cpu = next(v for v in vis if v.startswith("cpu  "))
    assert cpu.startswith("cpu  ▁▂▃▃▄▅▆▆▇█") and cpu.endswith("  9%")
    assert any(v.startswith("mem  ▆") and v.endswith("709MB") for v in vis)
    assert any(v.startswith("ctx  ▄") and v.endswith("29% 293k") for v in vis)
    assert any(v.startswith("req  ") and v.endswith("22 per 5 min · 60 min") for v in vis)
    assert any(v.startswith("tok  ") and v.endswith("out 9.8k per 5 min") for v in vis)
    assert any(v.startswith("gpu  ") and v.endswith("6%") for v in vis)
    assert max(len(v) for v in vis) <= snapshot.MENU_WIDTH


def test_menu_without_history_or_series_has_no_spark_lines():
    text = snapshot.menubar(_snap())
    vis = [snapshot.visible(ln) for ln in text.splitlines()]
    assert not any(v.startswith(("cpu  ", "mem  ", "req  ", "gpu  ")) for v in vis)
    assert "bash=" not in text and "Open dashboard" not in text      # no binary: no actions


def test_click_actions_only_with_valid_ids_and_binary():
    subs = [{"id": AID, "type": "Explore", "description": "ok", "state": "running", "flags": []},
            {"id": "a1; rm -rf ~", "type": "x", "description": "bad id", "state": "done",
             "flags": []},
            {"id": "a12345678 param4=x", "type": "x", "description": "space", "state": "done",
             "flags": []},
            {"id": "abcdef12", "type": "x", "description": "x | bash=/bin/sh param1=-c",
             "state": "done", "flags": []}]
    good = _agent(subs=subs)
    bad = _agent(sid='1234abcd" terminal=true')
    idle = _agent(sid="ffffffff-0000-4000-8000-000000000000", status="idle")
    text = snapshot.menubar(_snap([good, bad, idle]), history=_hist(), bin_path=BIN)
    acts = _actions(text)
    assert acts
    for ln, p in acts:
        assert set(p) - {"font", "size", "emojize", "symbolize", "tooltip"} == {
            "bash", "param1", "param2", "param3", "terminal", "refresh"}, ln
        assert p["bash"] == BIN and p["param1"] == "snapshot" and p["param2"] == "--detail"
        assert p["terminal"] == "false" and p["refresh"] == "false"
        assert snapshot.valid_target(p["param3"]), ln
    targets = {p["param3"] for _, p in acts}
    assert targets == {"all", SID, AID, "ffffffff-0000-4000-8000-000000000000"}
    # the injection-shaped description stays visible text, never a parameter
    inj = next(ln for ln in text.splitlines() if "bash=/bin/sh" in ln.split(" | ")[0]
               or "bash¦" in ln or "¦ bash=/bin/sh" in ln)
    assert "bash=" + BIN not in inj and inj.count(" | ") == 1     # session-shaped id: no action
    assert snapshot.click_action(BIN, "abcdef12", "agent") == ""
    assert snapshot.click_action(BIN, SID, "agent") == "" and snapshot.click_action(BIN, AID)
    assert "Open dashboard ↗ | bash=" + BIN in text
    assert any(ln.startswith("--Open details ↗ | ") and SID in ln for ln in text.splitlines())


@pytest.mark.parametrize("bin_path", ["apex-router", "relative/apex-router", "/a b/apex-router",
                                      '/x"/apex', "/x/../bin/sh", "/x;rm", "", None,
                                      "/x/apex-router param4=1"])
def test_click_action_rejects_unsafe_binaries(bin_path):
    assert snapshot.click_action(bin_path, SID) == ""
    assert "bash=" not in snapshot.menubar(_snap(), bin_path=bin_path)


@pytest.mark.parametrize("target,ok", [
    ("all", True), (SID, True), ("d5443384", True), (AID, True), ("a12345678", True),
    ("D5443384", False), ("d544338", False), ("x" * 40, False), ("a123456", False),
    ("all ", False), ("d5443384 param4=x", False), ('d5443384"', False), ("a1;b", False),
    ("../../etc", False), ("", False), (None, False), ("-" * 8, False), ("d5443384\n", False)])
def test_valid_target(target, ok):
    assert snapshot.valid_target(target) is ok


def test_resolve_bin_prefers_env_then_local_then_path(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "apex-router"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("APEX_ROUTER_BIN", str(exe))
    assert snapshot.resolve_bin() == str(exe)
    monkeypatch.setenv("APEX_ROUTER_BIN", "/no/such/apex-router")
    monkeypatch.setattr(snapshot.shutil, "which", lambda name: None)
    assert snapshot.resolve_bin() is None                       # sandbox HOME has no ~/.local/bin
    local = Path(os.environ["HOME"]) / ".local" / "bin" / "apex-router"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    local.chmod(0o755)
    assert snapshot.resolve_bin() == str(local)


def test_menu_width_with_actions_and_sparks_on_a_large_snapshot():
    agents = []
    for i in range(12):
        sid = f"{i:08x}-0000-4000-8000-000000000000"
        a = _agent(sid=sid, desc="d" * 300, series={"req": list(range(12)), "out": [10 ** 9] * 12})
        a["repo"] = "r" * 90
        a["res"]["tree"]["footprint_mb"] = 123_456.0
        agents.append(a)
    hist = []
    for k in range(40):
        s = widget_history.sample(_snap(agents))
        s["ts"] = NOW - (40 - k) * 60
        hist.append(s)
    text = snapshot.menubar(_snap(agents), history=hist, bin_path=BIN)
    assert max(len(snapshot.visible(ln)) for ln in text.splitlines()) <= snapshot.MENU_WIDTH
    assert len(text.splitlines()) <= snapshot.MENU_LINES_MAX + 25


# ---------------------------------------------------------------- detail page

class _Checker(html.parser.HTMLParser):
    VOID = frozenset({"meta", "br", "img", "input", "link", "hr", "col", "area", "base", "wbr", "source"})

    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.ids, self.tags = [], [], [], set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        d = dict(attrs)
        if "id" in d:
            self.ids.append(d["id"])
        for k, v in attrs:
            if k in ("src", "href", "action", "xlink:href") or k.startswith("on"):
                self.errors.append(f"attr {k}={v}")
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.tags.add(tag)
        for k, v in attrs:
            if k in ("src", "href") or k.startswith("on"):
                self.errors.append(f"attr {k}={v}")

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}> (open: {self.stack[-3:]})")
            return
        self.stack.pop()


def _check_html(text: str) -> _Checker:
    c = _Checker()
    c.feed(text)
    c.close()
    assert not c.errors, c.errors[:5]
    assert not c.stack, c.stack
    assert len(c.ids) == len(set(c.ids)), "duplicate ids"
    assert not re.search(r"https?://", text), "external reference"
    assert "<script" not in text.lower() and "$" not in text
    return c


EVIL = '<script>alert(1)</script> "q" & <img src=x onerror=y> | bash=/bin/sh'


def _detail_home(tmp_path):
    home = tmp_path / "home"
    d = home / ".claude" / "projects" / "-Users-you-src-r" / SID / "subagents"
    d.mkdir(parents=True)
    rows = []
    for k, aid in enumerate([AID, "ab0000000000000001", "ac0000000000000002"]):
        meta = d / f"agent-{aid}.meta.json"
        meta.write_text(json.dumps({"agentType": "Explore", "description": f"{EVIL} {k}",
                                    "spawnDepth": 1}))
        os.utime(meta, (NOW - 3600 * (k + 1),) * 2)
        log = d / f"agent-{aid}.jsonl"
        log.write_text("{}\n")
        os.utime(log, (NOW - 300 * k,) * 2)
        for i in range(20):
            rows.append({"client": "claude-code", "ts": NOW - 3600 * (k + 1) + i * 150,
                         "session_id": SID, "agent_id": aid, "model_requested": "claude-opus-5-5",
                         "tokens_in": 10, "tokens_out": 300, "cache_read_tokens": 50_000 + i * 900,
                         "cache_write_tokens": 100, "is_error": i == 3,
                         "error_cause": "<b>ReadError</b>" if i == 3 else None})
    for i in range(60):
        rows.append({"client": "claude-code", "ts": NOW - 5 * 3600 + i * 300, "session_id": SID,
                     "agent_id": None, "model_requested": "claude-opus-5-5", "tokens_in": 5,
                     "tokens_out": 500, "cache_read_tokens": 100_000 + i * 3000,
                     "cache_write_tokens": 2000})
    tel = home / ".apex" / "telemetry.jsonl"
    tel.parent.mkdir(parents=True)
    tel.write_text("".join(json.dumps(r) + "\n" for r in sorted(rows, key=lambda r: r["ts"])))
    a = _agent(desc=EVIL)
    a["repo"] = "<i>repo</i>"
    a["res"]["tree"]["top"][0]["name"] = "<svg onload=x>proc"
    hp = tmp_path / "history.jsonl"
    for s in _hist(8):
        widget_history.append(s, path=hp, now=NOW)
    return home, tel, hp, _snap([a])


SECTIONS = ["CPU %", "Memory (footprint)", "Disk read+write", "Context tokens (main thread)",
            "Requests per 5 min", "Tokens per 5 min (stacked)", "Output tokens per 5 min",
            "Context size per request", "Subagent timeline", "Call graph", "Subagents table",
            "Processes", "Models (6 h)"]


def test_detail_session_page(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path / "arh"))
    home, tel, hp, snap = _detail_home(tmp_path)
    path, anchor = widget_detail.build(SID, now=NOW, home=home, telemetry=tel, history_path=hp,
                                       snap=snap)
    assert path == tmp_path / "arh" / "widget" / "detail-d5443384.html" and anchor is None
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    text = path.read_text()
    c = _check_html(text)
    for s in SECTIONS:
        assert s in text, s
    assert "<script>alert" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<i>repo</i>" not in text and "&lt;i&gt;repo&lt;/i&gt;" in text
    assert "<svg onload" not in text and "&lt;svg onload=x&gt;proc" in text
    assert "<b>ReadError</b>" not in text and "&lt;b&gt;ReadError&lt;/b&gt;" in text
    assert "window 1M" in text and "errmark" in text
    assert {"svg", "polyline", "rect", "table", "title"} <= c.tags
    assert AID in c.ids                                          # the #a… anchor target
    assert "Content-Security-Policy" in text and "prefers-color-scheme: dark" in text


def test_detail_subagent_target_anchors_its_session_page(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path / "arh"))
    home, tel, hp, snap = _detail_home(tmp_path)
    snap["agents"][0]["res"]["subagents"]["list"] = []          # found via telemetry / logs
    path, anchor = widget_detail.build("ac0000000000000002", now=NOW, home=home, telemetry=tel,
                                       history_path=hp, snap=snap)
    assert path.name == "detail-d5443384.html" and anchor == "ac0000000000000002"
    text = path.read_text()
    _check_html(text)
    assert text.index('class="focus"') < text.index("Process load")   # focus card first
    snap["agents"] = []
    path2, _ = widget_detail.build(AID, now=NOW, home=home, telemetry=tel, history_path=hp,
                                   snap=snap)                  # session gone: logs/telemetry
    assert path2.name == "detail-d5443384.html"


def test_detail_overview_page(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path / "arh"))
    home, tel, hp, snap = _detail_home(tmp_path)
    path, anchor = widget_detail.build("all", now=NOW, home=home, telemetry=tel, history_path=hp,
                                       snap=snap)
    assert path.name == "detail-all.html" and anchor is None
    text = path.read_text()
    _check_html(text)
    for s in ("Sessions (small multiples", "GPU utilisation % (system-wide)", "Load average",
              "GPU memory in use", "All proxy requests per 5 min", "ollama", "qwen3:8b"):
        assert s in text, s
    assert "&lt;i&gt;repo&lt;/i&gt;" in text


def test_detail_empty_sources_still_render(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path / "arh"))
    for target in ("all", SID):
        path, _ = widget_detail.build(target, now=NOW, home=tmp_path, telemetry=tmp_path / "none",
                                      history_path=tmp_path / "none.jsonl",
                                      snap={"agents": [], "system": {}})
        text = path.read_text()
        _check_html(text)
        assert "no samples yet" in text or "no sessions" in text


@pytest.mark.parametrize("bad", ["../x", "a1;rm", "ALL", "x" * 40, "d5443384 x"])
def test_detail_rejects_invalid_targets(bad, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path / "arh"))
    calls = []
    monkeypatch.setattr(widget_detail, "_run", lambda *a, **k: calls.append(a))
    assert snapshot.main(["--detail", bad]) == 2
    assert "detail:" in capsys.readouterr().out
    assert not calls and not (tmp_path / "arh").exists()


def test_detail_no_open_does_not_spawn_open(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path / "arh"))
    calls = []
    monkeypatch.setattr(widget_detail, "_run", lambda argv, **k: calls.append((argv, k)))
    monkeypatch.setattr(snapshot, "collect", lambda **k: _snap())
    assert snapshot.main(["--detail", "all", "--no-open"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith("detail-all.html") and Path(out).exists() and calls == []
    assert snapshot.main(["--detail", "all"]) == 0
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == ["open", out] and kw.get("timeout") == widget_detail.OPEN_TIMEOUT_S


def test_open_file_uses_a_file_url_for_an_anchor_and_fails_open(tmp_path, monkeypatch):
    calls = []

    class R:
        returncode = 0
    monkeypatch.setattr(widget_detail, "_run", lambda argv, **k: calls.append(argv) or R())
    p = tmp_path / "detail-d5443384.html"
    assert widget_detail.open_file(p, AID) is True
    assert calls[-1] == ["open", p.as_uri() + "#" + AID]

    def boom(*a, **k):
        raise OSError("no open")
    monkeypatch.setattr(widget_detail, "_run", boom)
    assert widget_detail.open_file(p) is False


def test_detail_build_failure_is_contained(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(widget_detail, "build", boom)
    assert widget_detail.main("all", open_page=False, emit=print) == 0
    assert "detail error: RuntimeError: kaput" in capsys.readouterr().out


def test_charts_handle_nan_and_single_points():
    svg = widget_detail.line_chart("x", [("a", "red", [(NOW, float("nan")), (NOW + 1, 2.0)])],
                                   NOW - 10, NOW + 10, lambda v: f"{v:g}")
    assert svg.count("<circle") == 1                           # the NaN point is a gap
    assert "NaN" not in svg and "inf" not in svg
    assert "no samples yet" in widget_detail.line_chart("x", [("a", "r", [])], 0, 1, str)
    assert widget_detail.mini_spark([None, 3]).count("<circle") == 1
    assert not math.isnan(widget_detail._nice(0))


# ---- review fixes: trailing newline ids, tooltip backslash, time gaps, mini spark floor ---------

def test_ids_with_a_trailing_newline_or_dash_start_are_rejected():
    for bad in ("abcdef12\n", "aabcdef0123\n", "--------", "/usr/bin/x\n"):
        assert not snapshot.valid_target(bad), repr(bad)
    assert not snapshot.valid_bin("/usr/bin/x\n")
    assert snapshot.valid_target("d5443384-0a68-4edf-82e8-1d130c562c8e")
    assert snapshot.valid_target("a1cacfc99af4755f9")


def test_tooltip_never_ends_in_a_backslash_escape():
    assert "\\" not in snapshot._tip("path C:\\dir\\")


def test_line_chart_breaks_across_time_gaps():
    from apex_router import widget_detail as wd
    svg = wd.line_chart("cpu", [("cpu", "red", [(0, 1.0), (60, 2.0), (60 + 10 * 60, 3.0), (60 + 11 * 60, 4.0)])],
                        t0=0, t1=1000, fmt=str)
    assert svg.count("<polyline") == 2


def test_mini_spark_small_wobble_is_not_full_height():
    from apex_router import widget_detail as wd
    svg = wd.mini_spark([342.3, 342.4, 342.3], zero=False)
    ys = [float(p.split(",")[1]) for p in svg.split('points="')[1].split('"')[0].split()]
    assert max(ys) - min(ys) < 5
