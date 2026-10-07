"""Tests for apex_router.agent_resources — every OS source is a fake (no real ps/lsof/ioreg/libproc)."""
from __future__ import annotations

import calendar
import json
import os
import time
from pathlib import Path

import pytest

from apex_router import agent_resources as ar

NOW = 1_790_000_000.0
FMT = "%a %b %d %H:%M:%S %Y"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The two-sample rate window must not slow the suite."""
    monkeypatch.setattr(ar, "_sleep", lambda s: None)


def _lstart(epoch: float) -> str:
    return time.strftime(FMT, time.localtime(epoch))


def _ps_line(pid, ppid, rss_kb, cpu, comm, start=NOW - 3600):
    return f"{pid:>6} {ppid:>6} {rss_kb:>8} {cpu:>5} {_lstart(start)}     {comm}"


def _table(*rows):
    return ar.parse_ps("\n".join(_ps_line(*r) for r in rows))


# ---------------------------------------------------------------- primitives

def test_mach_ticks_to_s_apple_silicon_timebase():
    assert ar.mach_ticks_to_s(3_000_000_000, 125, 3) == pytest.approx(125.0)
    assert ar.mach_ticks_to_s(1_000_000_000, 1, 1) == pytest.approx(1.0)  # Intel: ticks == ns
    assert ar.mach_ticks_to_s(5, 1, 0) == 0.0


def test_parse_ps_spaces_in_comm_and_bad_lines():
    text = "\n".join([
        _ps_line(10, 1, 2048, 1.5, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "garbage line",
        "  x  y  z a b c d e f g",
        _ps_line(11, 10, 1024, 0.0, "claude"),
    ])
    t = ar.parse_ps(text)
    assert set(t) == {10, 11}
    assert t[10]["name"] == "Google Chrome" and t[10]["rss_kb"] == 2048 and t[10]["cpu"] == 1.5
    assert t[11]["start"] == pytest.approx(NOW - 3600, abs=1)


def test_descendants_and_tree_metrics():
    t = _table((10, 1, 102400, 2.0, "claude"), (11, 10, 51200, 10.0, "/bin/zsh"),
               (12, 11, 20480, 30.0, "pytest"), (13, 10, 409600, 0.0, "node"),
               (14, 10, 1024, 0.0, "caffeinate"), (20, 1, 999999, 50.0, "other"))
    assert sorted(ar.descendants(t, 10)) == [11, 12, 13, 14]
    assert ar.descendants(t, 10, cap=2) == [11, 13]

    def ru(pid):
        if pid == 13:
            raise OSError("denied")
        return {"footprint_mb": 100.0, "read_mb": 1.0, "write_mb": 2.0, "cpu_s": 3.0}
    m = ar.tree_metrics(t, 10, ru)
    assert m["alive"] and m["procs"] == 5
    assert m["rss_mb"] == pytest.approx((102400 + 51200 + 20480 + 409600 + 1024) / 1024, abs=0.1)
    assert m["cpu_pct"] == 42.0                                     # 2 + 10 + 30; pid 20 excluded
    assert m["footprint_mb"] == 400.0 and m["read_mb"] == 4.0       # pid 13 failed: skipped
    assert [p["name"] for p in m["top"][:3]] == ["pytest", "zsh", "node"]  # cpu, then memory
    assert ar.tree_metrics(t, 99, ru) == {"pid": 99, "alive": False}


def test_tree_metrics_no_rusage_is_none_not_zero():
    t = _table((10, 1, 1024, 0.0, "claude"))
    m = ar.tree_metrics(t, 10, lambda pid: None)
    assert m["footprint_mb"] is None and m["read_mb"] is None and m["rss_mb"] == 1.0


def test_descendants_cycle_safe():
    t = {1: {"pid": 1, "ppid": 2}, 2: {"pid": 2, "ppid": 1}}
    assert ar.descendants(t, 1) == [2]


# ---------------------------------------------------------------- Claude sessions

def _session(home: Path, pid, sid, start, **kw):
    d = home / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    doc = {"pid": pid, "sessionId": sid, "procStart": time.strftime(FMT, time.gmtime(start)),
           "status": "busy", "name": "n", "cwd": "/x"}
    doc.update(kw)
    (d / f"{pid}.json").write_text(json.dumps(doc))


def test_claude_sessions_dead_and_reused_pids_ignored(tmp_path):
    start = NOW - 7200
    t = _table((100, 1, 1024, 0.0, "claude", start), (200, 1, 1024, 0.0, "claude", start),
               (300, 1, 1024, 0.0, "bash", start))
    _session(tmp_path, 100, "live", start)
    _session(tmp_path, 200, "reused", start - 86400)                  # pid reused since
    _session(tmp_path, 400, "dead", start)                            # no such pid
    _session(tmp_path, 300, "nostart", start, procStart="??")         # unparseable + not claude
    d = tmp_path / ".claude" / "sessions"
    (d / "500.json").write_text("{not json")
    (d / "100.abc.key").write_text("secret")                          # never read
    out = ar.claude_sessions(tmp_path, t)
    assert set(out) == {"live"}
    assert out["live"] == {"pid": 100, "status": "busy", "name": "n", "verified": "procStart"}


def test_claude_sessions_unparseable_start_falls_back_to_comm(tmp_path):
    t = _table((100, 1, 1024, 0.0, "/opt/claude-code/claude"))
    _session(tmp_path, 100, "s", NOW, procStart=None)
    assert ar.claude_sessions(tmp_path, t)["s"]["verified"] == "comm"


def test_claude_sessions_missing_dir(tmp_path):
    assert ar.claude_sessions(tmp_path / "nope", {}) == {}


# ---------------------------------------------------------------- pi / codex via lsof

def test_parse_lsof_cwd():
    text = "p101\nfcwd\nn/Users/you/src/a\np202\nfcwd\nn/Users/you/src/b c\npbad\nn/zz\n"
    assert ar.parse_lsof_cwd(text) == {101: "/Users/you/src/a", 202: "/Users/you/src/b c"}
    assert ar.parse_lsof_cwd("") == {}


def test_match_by_cwd_unambiguous_only(tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    t = _table((1, 0, 10, 0, "pi"), (2, 0, 10, 0, "pi"), (3, 0, 10, 0, "pi"), (4, 0, 10, 0, "codex"))
    cwds = {1: a, 2: b, 3: b, 4: a}
    agents = [{"kind": "pi", "cwd": a}, {"kind": "pi", "cwd": b}, {"kind": "codex", "cwd": a},
              {"kind": "claude", "cwd": a}, {"kind": "pi", "cwd": None}]
    m = ar.match_by_cwd(agents, t, cwds)
    assert m[0] == 1 and m[2] == 4
    assert m[1] == {"ambiguous": 2, "sessions": 1}
    assert 3 not in m and 4 not in m


def test_lsof_cwds_single_call_and_empty():
    calls = []

    def run(argv, timeout=2.0):
        calls.append(argv)
        return "p7\nn/tmp\n"
    assert ar.lsof_cwds([7, 7, 9], run) == {7: "/tmp"}
    assert calls == [["lsof", "-a", "-d", "cwd", "-Fpn", "-p", "7,9"]]
    assert ar.lsof_cwds([], run) == {}


# ---------------------------------------------------------------- GPU / ollama

def test_parse_ioreg():
    text = ('+-o AGXAcceleratorG15X  <class AGXAccelerator>\n'
            '    "PerformanceStatistics" = {"Device Utilization %"=37,"In use system memory"=1583284224,'
            '"Renderer Utilization %"=12}\n')
    out = ar.parse_ioreg(text)
    assert out == {"gpu_util_pct": 37, "gpu_mem_mb": 1509.9}
    assert ar.parse_ioreg("") == {"gpu_util_pct": None, "gpu_mem_mb": None}


def test_parse_ollama():
    body = json.dumps({"models": [{"name": "qwen3:8b", "size": 6 * 2**30, "size_vram": 5 * 2**30},
                                  {"bad": 1}, "x"]})
    assert ar.parse_ollama(body) == [{"name": "qwen3:8b", "size_mb": 6144.0, "vram_mb": 5120.0}]
    assert ar.parse_ollama(b'{"models": []}') == []
    with pytest.raises(ValueError):
        ar.parse_ollama(b"not json")


# ---------------------------------------------------------------- telemetry

def _row(sid, aid=None, model="claude-opus-5-5", **kw):
    r = {"ts": NOW - 60, "client": "claude-code", "session_id": sid, "agent_id": aid,
         "model_requested": model,
         "tokens_out": 100, "cache_read_tokens": 1000, "is_error": False, "ttft_ms": 1000}
    r.update(kw)
    return r


def test_telemetry_split_main_vs_subagents():
    rows = [_row("s1"), _row("s1", ttft_ms=3000), _row("s1", is_error=True, ttft_ms=None),
            _row("s1", "a1", model="claude-sonnet-4-5"), _row("s1", "a1"), _row("s1", "a2"),
            _row("s2"), _row(None), _row("")]
    out = ar.telemetry_split(rows)
    assert set(out) == {"s1", "s2"}
    main = out["s1"]["main"]
    assert main["requests"] == 3 and main["errors"] == 1 and main["tokens_out"] == 300
    assert main["cache_read"] == 3000 and main["p50_ttft_ms"] == 2000
    assert set(out["s1"]["subagents"]) == {"a1", "a2"}
    assert out["s1"]["subagents"]["a1"]["models"] == {"claude-sonnet-4-5": 1, "claude-opus-5-5": 1}
    assert out["s2"]["subagents"] == {}


# ---------------------------------------------------------------- subagents + graph

def _subagent(home: Path, sid: str, aid: str, age: float, meta=None, slug="-Users-you-src-r"):
    d = home / ".claude" / "projects" / slug / sid / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"agent-{aid}.jsonl"
    f.write_text("{}\n")
    os.utime(f, (NOW - age, NOW - age))
    if meta is not None:
        (d / f"agent-{aid}.meta.json").write_text(json.dumps(meta) if isinstance(meta, dict) else meta)


def test_subagents_meta_states_and_cap(tmp_path):
    _subagent(tmp_path, "s1", "aa1", 30, {"agentType": "Explore", "description": "find x",
                                          "spawnDepth": "2"})
    _subagent(tmp_path, "s1", "aa2", 20 * 60, "{broken")
    _subagent(tmp_path, "s1", "old", 2 * 3600, {"agentType": "Plan"})       # too old, no traffic
    _subagent(tmp_path, "s1", "oldbusy", 2 * 3600, {"agentType": "Plan"})   # old but has traffic
    tel = {"oldbusy": {"requests": 2}, "ghost": {"requests": 1}}
    out = ar.subagents(tmp_path, "s1", NOW, tel)
    by = {s["id"]: s for s in out["list"]}
    assert set(by) == {"aa1", "aa2", "oldbusy", "ghost"}
    assert by["aa1"] == {"id": "aa1", "type": "Explore", "description": "find x", "depth": 2,
                         "age_s": 30.0, "run_s": 0.0, "state": "running", "flags": [],
                         "telemetry": None, "last_s": 30.0}
    assert by["aa2"]["type"] == "?" and by["aa2"]["state"] == "done"
    assert by["ghost"]["state"] == "?" and out["list"][-1]["id"] == "ghost"
    for i in range(25):
        _subagent(tmp_path, "s2", f"x{i:02d}", i, {"agentType": "t"})
    out = ar.subagents(tmp_path, "s2", NOW)
    assert len(out["list"]) == ar.SUBAGENTS_MAX and out["more"] == 25 - ar.SUBAGENTS_MAX
    assert out["count"] == 25
    assert out["list"][0]["id"] == "x00"                                    # newest first


def _enriched():
    tree = {"pid": 100, "alive": True, "rss_mb": 400.0, "footprint_mb": 412.0, "cpu_pct": 14.0,
            "read_mb": 745.0, "write_mb": 569.0,
            "top": [{"pid": 200 + i, "name": f"p{i}", "rss_mb": 1.0, "cpu_pct": 0.0}
                    for i in range(9)]}
    subs = [{"id": f"a{i}", "type": "Explore", "description": "d", "depth": 1, "state": "active",
             "telemetry": {"requests": 2, "tokens_out": 1500, "errors": 0,
                           "models": {"claude-haiku-4-5": 2}}} for i in range(30)]
    return [{"kind": "claude", "repo": "r", "session": "s1", "session_id": "s1-full",
             "state": "active", "res": {"tree": tree, "status": "busy",
                                        "telemetry": {"requests": 5, "models": {"claude-opus-5-5": 5}},
                                        "subagents": {"list": subs, "more": 0}}},
            {"kind": "pi", "repo": "q", "session": "s2", "state": "idle"},
            {"kind": "codex", "error": "boom"}]


def test_graph_shape_and_caps():
    g = ar.build_graph(_enriched())
    kinds = [n["kind"] for n in g["nodes"]]
    assert kinds.count("session") == 2                                # error row skipped
    assert kinds.count("subagent") == ar.SUBAGENTS_MAX
    assert kinds.count("process") == ar.GRAPH_PROCS_MAX
    models = {n["label"]: n for n in g["nodes"] if n["kind"] == "model"}
    assert models["claude-haiku-4-5"]["requests"] == 2 * 30          # hidden ones still counted
    assert kinds.count("more") == 1
    assert models["claude-opus-5-5"]["requests"] == 5
    ek = {e["kind"] for e in g["edges"]}
    assert ek == {"spawned", "runs", "calls"}
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in g["edges"])
    assert len(ids) == len(g["nodes"])
    many = [{"kind": "claude", "session": f"s{i}", "state": "idle"} for i in range(50)]
    assert len(ar.build_graph(many)["nodes"]) == ar.GRAPH_SESSIONS_MAX


def test_graph_text():
    text = ar.graph_text(ar.build_graph(_enriched()))
    lines = text.splitlines()
    assert lines[0] == "claude · r · s1 · busy · pid 100 · 412MB · 14% · 65 req/h · out 45.0k/h"
    assert "  spawned Explore · d · 2 req · out 1.5k · active · no log · depth 1" in lines
    assert "  … 22 more subagents (44 req, out 33.0k)" in lines
    assert "0 err" not in text and "active · busy" not in text
    assert "    calls claude-haiku-4-5 ×2" in lines
    assert "  runs p0 (pid 200) · ?MB · 0%" in lines                 # footprint only, never rss
    assert "  calls claude-opus-5-5 ×5" in lines
    assert "pi · q · s2 · idle" in lines
    assert ar.graph_text({}) == "no agents in the last hour"


def test_metrics_text():
    assert ar.metrics_text({"alive": True, "footprint_mb": 412.3, "cpu_pct": 14.0,
                            "read_mb": 745.2, "write_mb": 569.0}) == "412MB · 14%"
    assert ar.metrics_text({"alive": True, "footprint_mb": 412.3, "cpu_pct": 14.0,
                            "read_mbs": 0.3, "write_mbs": 0.1}) == "412MB · 14% · 0.4MB/s"
    assert ar.metrics_text({"alive": True, "rss_mb": 10.0, "cpu_pct": 0.5,
                            "read_mb": None}) == "?MB · 0.5%"
    assert ar.metrics_text({"alive": False}) == "" and ar.metrics_text({}) == ""


# ---------------------------------------------------------------- collect

def _fake_run(ps_text, lsof_text="", ioreg_text='"Device Utilization %"=5 "In use system memory"=1048576'):
    calls = []

    def run(argv, timeout=2.0):
        calls.append(argv[0])
        return {"ps": ps_text, "lsof": lsof_text, "ioreg": ioreg_text}[argv[0]]
    run.calls = calls
    return run


def test_collect_end_to_end_with_fakes(tmp_path):
    start = NOW - 7200
    ps_text = "\n".join([_ps_line(100, 1, 102400, 4.0, "claude", start),
                         _ps_line(101, 100, 20480, 20.0, "pytest", start),
                         _ps_line(300, 1, 10240, 1.0, "pi", start),
                         _ps_line(400, 1, 4096, 0.0, "/opt/homebrew/bin/ollama", start),
                         _ps_line(401, 400, 8192, 0.0, "ollama", start),
                         _ps_line(500, 1, 2048, 0.0, "python", start)])
    _session(tmp_path, 100, "sess-1", start)
    _subagent(tmp_path, "sess-1", "sub1", 10, {"agentType": "Explore", "description": "look"})
    tel = tmp_path / "telemetry.jsonl"
    tel.write_text("".join(json.dumps(r) + "\n" for r in
                           [_row("sess-1"), _row("sess-1", "sub1"), _row("other")]))
    run = _fake_run(ps_text, lsof_text=f"p300\nn{tmp_path}\n")
    agents = [{"kind": "claude", "repo": "r", "session": "sess-1", "session_id": "sess-1",
               "state": "active", "age_s": 5},
              {"kind": "pi", "repo": "x", "session": "p", "session_id": "p-full",
               "cwd": str(tmp_path), "state": "idle", "age_s": 600}]
    out = ar.collect(agents, home=tmp_path, telemetry=tel, now=NOW, run=run,
                     rusage_fn=lambda pid: {"footprint_mb": 50.0, "read_mb": 1.0,
                                            "write_mb": 1.0, "cpu_s": 1.0},
                     fetch=lambda url: b'{"models":[{"name":"m","size":1048576,"size_vram":1048576}]}',
                     loadavg=lambda: (1.234, 1.0, 0.5), worker_pid=500)
    assert "res" not in agents[0]                                   # input not mutated
    c, p = out["agents"]
    assert c["res"]["status"] == "busy" and c["res"]["pid_source"] == "procStart"
    assert c["res"]["tree"]["procs"] == 2 and c["res"]["tree"]["cpu_pct_ps"] == 24.0
    assert c["res"]["tree"]["cpu_pct"] == 0.0 and c["res"]["tree"]["cpu_src"] == "sampled"
    assert c["res"]["tree"]["top"][0]["name"] == "pytest"
    assert c["res"]["telemetry"]["requests"] == 1
    assert c["res"]["subagents"]["list"][0]["telemetry"]["requests"] == 1
    assert p["res"]["pid_source"] == "lsof-cwd" and p["res"]["tree"]["pid"] == 300
    s = out["system"]
    assert s["gpu_util_pct"] == 5 and s["gpu_mem_mb"] == 1.0 and s["gpu_scope"] == "system-wide"
    assert s["loadavg"] == [1.23, 1.0, 0.5] and s["agents_procs"] == 3
    assert s["ollama"] == [{"name": "m", "size_mb": 1.0, "vram_mb": 1.0}] and "errors" not in s
    assert out["worker"]["tree"]["pid"] == 500 and out["worker"]["ollama_tree"]["pid"] == 400
    assert out["worker"]["ollama_tree"]["procs"] == 2
    assert run.calls.count("ps") == 1 and run.calls.count("lsof") == 1
    assert {e["kind"] for e in out["graph"]["edges"]} == {"spawned", "runs", "calls"}


def test_collect_skips_lsof_without_pi_or_codex(tmp_path):
    run = _fake_run(_ps_line(300, 1, 10, 0.0, "pi"))
    ar.collect([{"kind": "claude", "session": "s", "session_id": "s", "state": "active"}],
               home=tmp_path, telemetry=tmp_path / "none", now=NOW, run=run,
               rusage_fn=lambda p: None, fetch=lambda u: b"{}", loadavg=lambda: (0, 0, 0))
    assert "lsof" not in run.calls


@pytest.mark.parametrize("broken", ["run", "rusage", "fetch", "loadavg", "all"])
def test_collect_fails_open_per_source(tmp_path, broken):
    def boom(*a, **k):
        raise RuntimeError("kaput")
    ps_text = _ps_line(100, 1, 1024, 0.0, "claude", NOW - 60) + "\n" + _ps_line(300, 1, 10, 0, "pi")
    _session(tmp_path, 100, "s", NOW - 60)
    kw = dict(run=_fake_run(ps_text, "p300\nn/x\n"), rusage_fn=lambda p: None,
              fetch=lambda u: b"{}", loadavg=lambda: (0.1, 0.2, 0.3))
    if broken == "all":
        kw = {k: boom for k in kw}
    else:
        kw[{"run": "run", "rusage": "rusage_fn", "fetch": "fetch", "loadavg": "loadavg"}[broken]] = boom
    agents = [{"kind": "claude", "session": "s", "session_id": "s", "state": "active"},
              {"kind": "pi", "session": "p", "session_id": "p", "cwd": "/x", "state": "idle"},
              {"kind": "codex", "error": "x"}, "not-a-dict"]
    out = ar.collect(agents, home=tmp_path, telemetry=tmp_path / "missing.jsonl", now=NOW,
                     worker_pid=100, **kw)
    errs = out["system"].get("errors", {})
    assert "telemetry" in errs                                       # missing file -> recorded
    if broken in ("run", "all"):
        assert {"ps", "ioreg"} <= set(errs)
    if broken in ("fetch", "all"):
        assert "ollama" in errs and out["system"]["ollama"] is None
    if broken in ("loadavg", "all"):
        assert out["system"]["loadavg"] is None
    if broken == "rusage":
        assert out["agents"][0]["res"]["tree"]["footprint_mb"] is None
    assert isinstance(out["graph"]["nodes"], list)


def test_rusage_never_raises():
    assert ar.rusage(-1) is None                                     # invalid pid -> None


# ---------------------------------------------------------------- iteration 2: rates

def test_rates_from_two_samples():
    s1 = {1: {"cpu_s": 10.0, "read_mb": 100.0, "write_mb": 50.0},
          2: {"cpu_s": 5.0, "read_mb": 1.0, "write_mb": 1.0},
          3: {"cpu_s": 1.0, "read_mb": 1.0, "write_mb": 1.0}}
    s2 = {1: {"cpu_s": 10.125, "read_mb": 100.5, "write_mb": 50.25},
          2: {"cpu_s": 4.0, "read_mb": 1.0, "write_mb": 1.0},       # went backwards: pid reused
          4: {"cpu_s": 9.0, "read_mb": 0.0, "write_mb": 0.0}}       # not in the first sample
    r = ar.rates_from_samples(s1, s2, 0.25)
    assert set(r) == {1}
    assert r[1]["cpu_pct"] == pytest.approx(50.0)
    assert r[1]["read_mbs"] == pytest.approx(2.0) and r[1]["write_mbs"] == pytest.approx(1.0)
    assert ar.rates_from_samples(s1, s2, 0) == {}


def test_collect_cpu_and_io_now_from_two_samples(tmp_path, monkeypatch):
    start = NOW - 7200
    rows = [_ps_line(100, 1, 102400, 4.0, "claude", start)]
    rows += [_ps_line(1000 + i, 100, 1024, float(i % 7), "zsh", start) for i in range(60)]
    _session(tmp_path, 100, "sess-1", start)
    calls: dict = {}

    def ru(pid):                                       # +0.05 cpu s and +1 MB read per sample
        n = calls[pid] = calls.get(pid, 0) + 1
        return {"footprint_mb": 10.0, "read_mb": float(n), "write_mb": 0.0, "cpu_s": 0.05 * n}
    ticks = iter([0.0, 0.0, 0.25, 0.25, 0.25])         # t1, gap check, dt, …
    monkeypatch.setattr(ar, "_clock", lambda: next(ticks, 0.25))
    out = ar.collect([{"kind": "claude", "session": "s", "session_id": "sess-1",
                       "state": "active"}], home=tmp_path, telemetry=tmp_path / "none",
                     now=NOW, run=_fake_run("\n".join(rows)), rusage_fn=ru,
                     fetch=lambda u: b"{}", loadavg=lambda: (0, 0, 0),
                     deadline=ar.Deadline(5, clock=time.monotonic), self_pid=999999)
    tree = out["agents"][0]["res"]["tree"]
    sampled = sum(1 for n in calls.values() if n == 2)
    assert sampled == ar.RATE_PIDS_MAX                 # bounded second sample
    assert calls[100] == 2                             # the session root is always sampled
    assert tree["cpu_src"] == "sampled"
    assert tree["cpu_pct"] == pytest.approx(ar.RATE_PIDS_MAX * 20.0, abs=0.5)   # 0.05/0.25 s
    assert tree["read_mbs"] == pytest.approx(ar.RATE_PIDS_MAX * 4.0, abs=0.1)
    assert tree["read_mb"] == 61.0                     # lifetime (first sample) kept for tooltip
    assert tree["footprint_mb"] == 610.0
    assert out["system"]["rate_pids"] == ar.RATE_PIDS_MAX


# ---------------------------------------------------------------- iteration 2: self-exclusion

def test_widget_does_not_count_itself():
    t = _table((100, 1, 1024, 1.0, "claude"), (101, 100, 2048, 1.0, "/bin/zsh"),
               (102, 101, 40960, 90.0, "python3.14"), (103, 102, 4096, 50.0, "ps"),
               (104, 100, 8192, 5.0, "node"), (200, 1, 1024, 0.0, "launchd-child"))
    ex = ar.self_exclusion(t, {100}, self_pid=102)
    assert ex == {101, 102, 103}                       # self, its child, its shell below the root
    m = ar.tree_metrics(t, 100, lambda p: None, exclude=ex)
    assert m["procs"] == 2 and [p["pid"] for p in m["top"]] == [104]
    assert m["cpu_pct"] == 6.0
    # not inside any agent tree: only itself and its descendants
    assert ar.self_exclusion(t, {100}, self_pid=200) == {200}


def test_collect_excludes_own_process(tmp_path):
    start = NOW - 7200
    ps_text = "\n".join([_ps_line(100, 1, 1024, 1.0, "claude", start),
                         _ps_line(101, 100, 1024, 0.0, "/bin/zsh", start),
                         _ps_line(102, 101, 99999, 80.0, "python3", start),
                         _ps_line(104, 100, 2048, 2.0, "node", start)])
    _session(tmp_path, 100, "sess-1", start)
    out = ar.collect([{"kind": "claude", "session": "s", "session_id": "sess-1",
                       "state": "active"}], home=tmp_path, telemetry=tmp_path / "none", now=NOW,
                     run=_fake_run(ps_text), rusage_fn=lambda p: None, fetch=lambda u: b"{}",
                     loadavg=lambda: (0, 0, 0), self_pid=102)
    tree = out["agents"][0]["res"]["tree"]
    assert tree["procs"] == 2 and {p["pid"] for p in tree["top"]} == {104}
    assert out["system"]["agents_procs"] == 2


# ---------------------------------------------------------------- iteration 2: script names

SECRET = "sk-live-DEADBEEF0123456789"


@pytest.mark.parametrize("args,label", [
    ("node /Users/x/node_modules/.bin/pyright-langserver --stdio", "pyright-langserver"),
    (f"node /opt/app/server.mjs --token={SECRET}", "server"),
    ("python3 -m pytest -q", "pytest"),
    ("/usr/bin/python3 -X importtime ./serve.py --port 1", "serve"),
    (f"python3 -c print('{SECRET}')", None),               # inline code: never a name
    (f"node -e {SECRET}", None),
    (f"node --token {SECRET} script.js", None),            # first non-flag arg is not a script
    ("node abc/def+ghi==", None),                           # base64-ish, not a path
    ("ruby", None),
    ("bun run /x/y/dev-server.ts", "dev-server"),
    ("python3 /x/" + "a" * 60 + ".py", None),               # over-long basename
])
def test_script_label(args, label):
    assert ar.script_label(args) == label


def test_script_labels_one_bounded_call_and_no_argv_kept():
    t = _table(*[(10 + i, 1, 1024, float(i), "node") for i in range(50)],
               (5, 1, 1024, 0.0, "zsh"))
    seen = []

    def run(argv, timeout=2.0):
        seen.append(argv)
        pids = argv[-1].split(",")
        return "\n".join(f"{p} node /srv/{SECRET}/tool-{p}.js --key {SECRET}" for p in pids)
    out = ar.script_labels(t, sorted(t), run)
    assert len(seen) == 1 and seen[0][:3] == ["ps", "-o", "pid=,args="]
    assert len(seen[0][-1].split(",")) == ar.ARGS_PIDS_MAX and "5" not in seen[0][-1].split(",")
    assert out[10] == "tool-10" and SECRET not in json.dumps(out)


def test_collect_process_names_from_args_without_secrets(tmp_path):
    start = NOW - 7200
    ps_text = "\n".join([_ps_line(100, 1, 1024, 1.0, "claude", start),
                         _ps_line(104, 100, 2048, 2.0, "node", start)])
    _session(tmp_path, 100, "sess-1", start)

    def run(argv, timeout=2.0):
        if argv[:2] == ["ps", "-o"]:
            return f"104 node /x/pyright-langserver.js --stdio --api-key={SECRET}\n"
        return {"ps": ps_text, "ioreg": ""}[argv[0]]
    out = ar.collect([{"kind": "claude", "session": "s", "session_id": "sess-1",
                       "state": "active"}], home=tmp_path, telemetry=tmp_path / "none", now=NOW,
                     run=run, rusage_fn=lambda p: None, fetch=lambda u: b"{}",
                     loadavg=lambda: (0, 0, 0), self_pid=999999)
    assert out["agents"][0]["res"]["tree"]["top"][0]["name"] == "pyright-langserver"
    blob = json.dumps(out) + ar.graph_text(out["graph"])
    assert SECRET not in blob and "--stdio" not in blob


# ---------------------------------------------------------------- iteration 2: lifecycle

def _sub_at(home, sid, aid, spawn_age, last_age, desc="d"):
    _subagent(home, sid, aid, last_age, {"agentType": "Explore", "description": desc})
    mp = home / ".claude" / "projects" / "-Users-you-src-r" / sid / "subagents" / \
        f"agent-{aid}.meta.json"
    os.utime(mp, (NOW - spawn_age, NOW - spawn_age))


def test_subagent_lifecycle_states_and_flags(tmp_path):
    _sub_at(tmp_path, "s", "run1", 14 * 60, 2)                 # running 14m
    _sub_at(tmp_path, "s", "long", 31 * 60, 5)                 # running > 20 min -> flagged
    _sub_at(tmp_path, "s", "quiet", 12 * 60, 3 * 60)
    _sub_at(tmp_path, "s", "done", 40 * 60, 20 * 60)
    _sub_at(tmp_path, "s", "errs", 31 * 60, 9 * 60)
    tel = {"errs": {"requests": 12, "errors": 5, "tokens_out": 10},
           "run1": {"requests": 3, "errors": 0, "tokens_out": 5}}
    out = ar.subagents(tmp_path, "s", NOW, tel)
    by = {s["id"]: s for s in out["list"]}
    assert by["run1"]["state"] == "running" and by["run1"]["run_s"] == pytest.approx(14 * 60 - 2)
    assert by["run1"]["flags"] == []
    assert by["long"]["flags"] == ["long"]
    assert by["quiet"]["state"] == "quiet" and by["done"]["state"] == "done"
    assert by["errs"]["state"] == "done" and by["errs"]["flags"] == ["errors"]
    assert [s["id"] for s in out["list"]][:3] == ["run1", "long", "errs"]   # running, then erroring
    assert (out["count"], out["running"], out["quiet"], out["flagged"]) == (5, 2, 1, 2)
    assert ar.lifecycle_text(by["run1"]) == "run 13m · last 2s"
    assert ar.lifecycle_text(by["quiet"]) == "run 9m · quiet 3m"
    assert ar.lifecycle_text(by["done"]) == "run 20m · done 20m"
    assert ar.lifecycle_text(by["errs"]) == "run 22m · last 9m"
    assert ar.err_text(tel["errs"]) == "5 err 42%" and ar.err_text(tel["run1"]) == ""


def test_subagent_totals_are_summed_before_the_cut(tmp_path):
    tel = {}
    for i in range(12):
        _sub_at(tmp_path, "s", f"a{i:02d}", 600, 120 + i)
        tel[f"a{i:02d}"] = {"requests": i + 1, "tokens_out": 100 * (i + 1), "errors": 0,
                            "tokens_in": 1, "cache_read": 10, "cache_write": 0,
                            "models": {"claude-opus-5-5": i + 1}}
    out = ar.subagents(tmp_path, "s", NOW, tel)
    assert len(out["list"]) == ar.SUBAGENTS_MAX and out["more"] == 12 - ar.SUBAGENTS_MAX
    assert out["totals"]["requests"] == sum(range(1, 13))
    assert out["hidden"]["requests"] == sum(range(1, 13 - ar.SUBAGENTS_MAX))  # least output
    assert out["list"][0]["id"] == "a11"                       # most output first
    total = ar.session_totals({"telemetry": {"requests": 4, "tokens_out": 1, "models": {"x": 4}},
                               "subagents": out})
    assert total["requests"] == 4 + sum(range(1, 13))
    assert total["models"] == {"claude-opus-5-5": sum(range(1, 13)), "x": 4}


# ---------------------------------------------------------------- iteration 2: tokens, context

def test_token_split_cache_share_and_wire_aware_input():
    rows = [_row("s", tokens_in=2, cache_read_tokens=1000, cache_write_tokens=100,
                 tokens_out=50, endpoint_id="anthropic"),
            _row("s", tokens_in=1500, cache_read_tokens=1200, cache_write_tokens=0,
                 tokens_out=10, endpoint_id="openai", model="kimi-k2.6")]   # cached ⊂ input
    st = ar._stats(rows)
    assert (st["tokens_in"], st["cache_read"], st["cache_write"], st["tokens_out"]) == \
        (2 + 300, 2200, 100, 60)
    assert ar.cache_share(st) == pytest.approx(2200 / (302 + 2200 + 100))
    assert ar.tokens_text(st) == "in 302 · cached 2.2k · write 100 · out 60"
    assert ar.tel_text(st).startswith("2 req · in 302 · cached 2.2k · write 100 · out 60 · cache 85%")
    assert ar.cache_share({"requests": 1}) is None
    assert "$" not in ar.tel_text(st)


def test_context_is_the_latest_request_and_window_from_the_family_table():
    rows = [_row("s", ts=NOW - 30, tokens_in=1, cache_read_tokens=240_000, cache_write_tokens=5000),
            _row("s", ts=NOW - 10, tokens_in=3, cache_read_tokens=90_000, cache_write_tokens=0),
            _row("s", ts=NOW - 5, is_error=True, tokens_in=0, cache_read_tokens=0,
                 cache_write_tokens=0, tokens_out=0)]                     # no prompt: ignored
    st = ar._stats(rows)
    assert st["ctx_tokens"] == 90_003 and st["ctx_ts"] == NOW - 10       # latest, not largest
    assert st["ctx_window"] == 1_000_000 and st["ctx_pct"] == 9          # opus 5.x: 1M family
    assert ar.ctx_text(st) == "ctx 90k/1M 9%" and not ar.ctx_flag(st)
    big = ar._stats([_row("s", model="claude-opus-5-5[1m]", tokens_in=1,
                          cache_read_tokens=899_999, cache_write_tokens=0)])
    assert big["ctx_window"] == 1_000_000 and big["ctx_pct"] == 90
    assert ar.ctx_text(big) == "ctx 900k/1M 90%" and ar.ctx_flag(big)
    assert ar.context_window("claude-opus-5-5") == 1_000_000
    # an unknown id: no window is guessed, the size is shown absolute
    unk = ar._stats([_row("s", model="kimi-k2.6", tokens_in=1, cache_read_tokens=136_000,
                          cache_write_tokens=0)])
    assert unk["ctx_window"] is None and unk["ctx_pct"] is None
    assert ar.ctx_text(unk) == "ctx 136k" and not ar.ctx_flag(unk)
    merged = ar.merge_stats(st, big)
    assert "ctx_tokens" not in merged                                    # a latest value: no sum


def test_context_flag_marks_subagent_and_agent():
    _ = ar._lifecycle(30, 60, {"ctx_pct": 86, "errors": 0})
    assert _ == ("running", ["ctx"])
    assert ar.agent_flagged({"res": {"telemetry": {"ctx_pct": 90}}})
    assert not ar.agent_flagged({"res": {"telemetry": {"ctx_pct": 50, "errors": 0}}})


# ---------------------------------------------------------------- iteration 2: dedupe, ollama

def test_graph_text_shows_claude_status_once():
    agents = [{"kind": "claude", "repo": "r", "session": "s1", "session_id": "x", "state": "idle",
               "res": {"status": "idle", "tree": {"pid": 5, "alive": True, "footprint_mb": 10.0,
                                                  "cpu_pct": 0.0}}},
              {"kind": "claude", "repo": "r", "session": "s2", "state": "active",
               "res": {"status": "busy"}}]
    text = ar.graph_text(ar.build_graph(agents))
    assert "idle · idle" not in text and "active · busy" not in text
    assert text.splitlines()[0] == "claude · r · s1 · idle · pid 5 · 10MB · 0%"
    assert text.splitlines()[1] == "claude · r · s2 · busy"


def test_parse_ollama_unload_time():
    body = json.dumps({"models": [{"name": "m", "size": 1, "size_vram": 1048576,
                                   "expires_at": "2026-10-06T12:03:00.123456789-07:00"}]})
    now = calendar.timegm((2026, 10, 6, 19, 0, 0, 0, 0, 0))   # 19:00Z = 12:00-07:00
    out = ar.parse_ollama(body, now=now)
    assert out[0]["unloads_in_s"] == 180
    assert "unloads_in_s" not in ar.parse_ollama(
        json.dumps({"models": [{"name": "m", "expires_at": "garbage"}]}), now=now)[0]


# ---------------------------------------------------------------- iteration 2: deadline

def test_deadline_skips_remaining_sources(tmp_path):
    calls = []

    def run(argv, timeout=2.0):
        calls.append((argv[0], timeout))
        return ""
    clock = iter([0.0] + [10.0] * 50)                   # budget gone after construction
    dl = ar.Deadline(1.5, clock=lambda: next(clock))
    out = ar.collect([{"kind": "pi", "session": "p", "session_id": "p", "cwd": "/x",
                       "state": "active"}], home=tmp_path, telemetry=tmp_path / "none",
                     now=NOW, run=run, rusage_fn=lambda p: None, loadavg=lambda: (0, 0, 0),
                     deadline=dl)
    errs = out["system"]["errors"]
    assert calls == []                                  # nothing started after the deadline
    assert errs["ps"].startswith("DeadlineSkip") and errs["ioreg"].startswith("DeadlineSkip")
    assert errs["ollama"].startswith("DeadlineSkip")


def test_deadline_caps_each_timeout_to_what_is_left():
    t = [0.0]
    dl = ar.Deadline(1.5, clock=lambda: t[0])
    assert dl.timeout(1.0) == 1.0
    t[0] = 1.2
    assert dl.timeout(1.0) == pytest.approx(0.3)
    t[0] = 1.47
    with pytest.raises(ar.DeadlineSkip):
        dl.timeout(1.0)


def test_graph_subagent_state_word_not_repeated():
    a = {"kind": "claude", "session": "s", "state": "active",
         "res": {"subagents": {"list": [
             {"id": "x", "type": "T", "description": "d", "state": "done", "age_s": 1200.0,
              "run_s": 600.0, "flags": [], "telemetry": {"requests": 1}},
             {"id": "y", "type": "T", "description": "e", "state": "running", "age_s": 2.0,
              "run_s": 60.0, "flags": [], "telemetry": {"requests": 1}}]}}}
    lines = ar.graph_text(ar.build_graph([a])).splitlines()
    assert "  spawned T · d · 1 req · out 0 · run 10m · done 20m" in lines
    assert "  spawned T · e · 1 req · out 0 · running · run 1m · last 2s" in lines


# ---------------------------------------------------------------- iteration 3

@pytest.mark.parametrize("model,window", [
    ("claude-opus-5-5", 1_000_000), ("claude-opus-5", 1_000_000), ("claude-opus-4-6", 1_000_000),
    ("claude-opus-4-7-20261001", 1_000_000), ("claude-sonnet-4-6", 1_000_000),
    ("claude-sonnet-5-1", 1_000_000), ("claude-fable-1", 1_000_000),
    ("us.anthropic.claude-opus-5-5-v1:0", 1_000_000), ("claude-sonnet-4-5[1m]", 1_000_000),
    ("claude-haiku-4-5", 200_000), ("claude-haiku-4-5-20251001", 200_000),
    ("claude-opus-4-5", None), ("claude-sonnet-4-5", None), ("claude-opus-4-20250514", None),
    ("claude-3-5-sonnet-20241022", None), ("kimi-k2.6", None), ("gpt-5", None), (None, None),
    ("", None)])
def test_context_window_family_table(model, window):
    assert ar.context_window(model) == window


def test_unknown_window_proven_1m_only_by_a_successful_request_over_200k():
    ok = ar._stats([_row("s", model="kimi-k2.6", ts=NOW - 50, tokens_in=1,
                         cache_read_tokens=210_000),
                    _row("s", model="kimi-k2.6", ts=NOW - 10, tokens_in=1,
                         cache_read_tokens=136_000)], now=NOW)
    assert ok["ctx_window"] == 1_000_000 and ok["ctx_window_src"] == "observed"
    assert ar.ctx_text(ok) == "ctx 136k/1M 14%"
    failed = ar._stats([_row("s", model="kimi-k2.6", ts=NOW - 50, tokens_in=1,
                             cache_read_tokens=210_000, is_error=True),
                        _row("s", model="kimi-k2.6", ts=NOW - 10, tokens_in=1,
                             cache_read_tokens=136_000)], now=NOW)
    assert failed["ctx_window"] is None and ar.ctx_text(failed) == "ctx 136k"
    other_model = ar._stats([_row("s", model="gpt-5", ts=NOW - 50, tokens_in=1,
                                  cache_read_tokens=210_000),
                             _row("s", model="kimi-k2.6", ts=NOW - 10, tokens_in=1,
                                  cache_read_tokens=136_000)], now=NOW)
    assert other_model["ctx_window"] is None


def test_context_flag_for_a_known_200k_model():
    st = ar._stats([_row("s", model="claude-haiku-4-5", tokens_in=1, cache_read_tokens=170_000)])
    assert st["ctx_window"] == 200_000 and ar.ctx_flag(st)
    assert ar.ctx_text(st) == "ctx 170k/200k 85%"
    assert ar.ctx_flag({"ctx_tokens": 171_000, "ctx_window": 200_000})           # no pct stored
    assert not ar.ctx_flag({"ctx_tokens": 169_000, "ctx_window": 200_000, "ctx_pct": 84})
    assert not ar.ctx_flag({"ctx_tokens": 900_000, "ctx_window": None})          # unknown: never


def test_error_flag_recent_or_rate():
    old = [_row("s", ts=NOW - 2400, is_error=True)] + [_row("s", ts=NOW - 60) for _ in range(99)]
    st = ar._stats(old, now=NOW)
    assert st["errors"] == 1 and st["errors_5m"] == 0 and not ar.err_flag(st)
    assert ar.err_text(st) == "1 err 1%"
    recent = ar._stats([_row("s", ts=NOW - 60, is_error=True)]
                       + [_row("s", ts=NOW - 60) for _ in range(99)], now=NOW)
    assert recent["errors_5m"] == 1 and ar.err_flag(recent)
    rate = ar._stats([_row("s", ts=NOW - 2400, is_error=True) for _ in range(5)]
                     + [_row("s", ts=NOW - 2400) for _ in range(95)], now=NOW)
    assert ar.err_flag(rate)
    assert ar.err_text({"errors": 1, "requests": 200}) == "1 err <1%"
    assert ar.err_text({"errors": 1, "requests": 101}) == "1 err <1%"
    assert ar._lifecycle(400, 60, st) == ("done", [])                 # old 1% error: no flag
    assert ar._lifecycle(400, 60, recent) == ("done", ["errors"])
    assert not ar.agent_flagged({"res": {"telemetry": st}})
    assert ar.agent_flagged({"res": {"telemetry": recent}})


def test_stats_rate_and_last_merge():
    st = ar._stats([_row("s", ts=NOW - 30), _row("s", ts=NOW - 200), _row("s", ts=NOW - 900)],
                   now=NOW)
    assert st["req_5m"] == 2 and st["last_ts"] == NOW - 30
    m = ar.merge_stats(st, {"req_5m": 3, "last_ts": NOW - 5}, {"last_ts": float("nan")})
    assert m["req_5m"] == 5 and m["last_ts"] == NOW - 5
    assert ar.fmt_rate_min(16) == "r5 3.2/min" and ar.fmt_rate_min(100) == "r5 20/min"
    assert ar.fmt_rate_min(0) == "r5 0/min"


def test_claude_sessions_unparseable_procstart_is_never_a_match(tmp_path):
    t = _table((100, 1, 1024, 0.0, "/opt/claude-code/claude"))          # start time known
    _session(tmp_path, 100, "s", NOW, procStart="not a date")
    assert ar.claude_sessions(tmp_path, t) == {}
    _session(tmp_path, 100, "s", NOW, procStart=12345)                  # wrong type: same
    assert ar.claude_sessions(tmp_path, t) == {}


def test_clean_text_strips_c0_del_c1():
    assert ar.clean_text("a\x1b[1mb\x07\x7f\x85c\x9bd\n\te") == "a[1mb cd e"   # NEL = space


def test_collect_scans_subagent_logs_only_for_busy_shown_sessions(tmp_path):
    start = NOW - 7200
    ps_text = "\n".join([_ps_line(100, 1, 1024, 1.0, "claude", start),
                         _ps_line(200, 1, 1024, 1.0, "claude", start)])
    _session(tmp_path, 100, "busy-1", start, status="busy")
    _session(tmp_path, 200, "idle-1", start, status="idle")
    for sid in ("busy-1", "idle-1"):
        _subagent(tmp_path, sid, f"{sid}-sub", 10, {"agentType": "Explore", "description": "d"})
    agents = [{"kind": "claude", "repo": "r", "session": s, "session_id": s,
               "state": "active", "age_s": 5} for s in ("busy-1", "idle-1")]  # both mtime-active
    out = ar.collect(agents, home=tmp_path, telemetry=tmp_path / "none", now=NOW,
                     run=_fake_run(ps_text), rusage_fn=lambda p: None, fetch=lambda u: b"{}",
                     loadavg=lambda: (0, 0, 0))
    by = {a["session_id"]: a for a in out["agents"]}
    assert ar.agent_active(by["busy-1"]) and not ar.agent_active(by["idle-1"])
    assert by["busy-1"]["res"]["subagents"]["count"] == 1
    assert by["idle-1"]["res"]["subagents"]["count"] == 0                # not scanned
    assert out["system"]["subagent_scans"] == 1


def test_collect_scan_is_capped_and_deadline_bounded(tmp_path):
    agents = []
    for i in range(20):
        sid = f"s{i:02d}"
        _subagent(tmp_path, sid, f"x{i}", 10, {"agentType": "t"})
        agents.append({"kind": "claude", "session": sid, "session_id": sid, "state": "active"})
    out = ar.collect(agents, home=tmp_path, telemetry=tmp_path / "none", now=NOW,
                     run=_fake_run(""), rusage_fn=lambda p: None, fetch=lambda u: b"{}",
                     loadavg=lambda: (0, 0, 0))
    assert out["system"]["subagent_scans"] == ar.SCAN_SESSIONS_MAX
    t = [0.0]
    dl = ar.Deadline(1.0, clock=lambda: t[0])
    t[0] = 5.0                                                           # budget spent
    sub = ar.subagents(tmp_path, "s00", NOW, {"ghost": {"requests": 1}}, deadline=dl)
    assert sub["partial"] and [s["id"] for s in sub["list"]] == ["ghost"]  # traffic still shown


def test_collect_sample_sleep_never_passes_the_deadline(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(ar, "_sleep", lambda s: slept.append(s))
    t = [0.0]
    monkeypatch.setattr(ar, "_clock", lambda: t[0])
    dl = ar.Deadline(0.1, clock=lambda: t[0])
    ar.collect([{"kind": "claude", "session": "s", "session_id": "s", "state": "active"}],
               home=tmp_path, telemetry=tmp_path / "none", now=NOW,
               run=_fake_run(_ps_line(100, 1, 1024, 1.0, "claude")),
               rusage_fn=lambda p: {"footprint_mb": 1.0, "read_mb": 0.0, "write_mb": 0.0,
                                    "cpu_s": 0.0},
               fetch=lambda u: b"{}", loadavg=lambda: (0, 0, 0), worker_pid=100, deadline=dl,
               sample_s=0.25)
    assert slept and max(slept) <= 0.1 - ar.DEADLINE_MIN_S + 1e-9


def test_graph_lines_wrap_at_120_and_keep_every_part():
    a = {"kind": "claude", "repo": "r" * 50, "session": "abcd1234", "state": "active",
         "res": {"status": "busy",
                 "tree": {"pid": 1, "alive": True, "footprint_mb": 700.0, "cpu_pct": 0.4,
                          "read_mbs": 0.0, "write_mbs": 0.0},
                 "telemetry": {"requests": 28, "tokens_out": 22_500, "ctx_tokens": 293_000,
                               "ctx_window": 1_000_000, "ctx_pct": 29, "req_5m": 22,
                               "last_ts": NOW - 13, "models": {"m": 28}},
                 "subagents": {"list": [{"id": "x", "type": "general-purpose",
                                         "description": "RSI iter 3: fix round-2 findings",
                                         "state": "running", "age_s": 2.0, "run_s": 540.0,
                                         "depth": 1, "flags": [],
                                         "telemetry": {"requests": 36, "tokens_out": 57_800,
                                                       "tokens_in": 24_600,
                                                       "cache_read": 4_700_000,
                                                       "cache_write": 177_100}}]}}}
    lines = ar.graph_text(ar.build_graph([a], now=NOW)).splitlines()
    assert max(len(ln) for ln in lines) <= 120
    text = " · ".join(ln.strip() for ln in lines)
    assert "abcd1234" in lines[0] and "r" * 31 + "…" in lines[0]        # repo cut, id kept
    for part in ("ctx 293k/1M 29%", "r5 4.4/min", "last 13s", "in 24.6k", "cached 4.7M",
                 "write 177.1k", "run 9m", "depth 1"):
        assert part in text, part
    assert lines[1].startswith("    ")                                    # continuation indent
