"""Tests for apex_router.agent_resources — every OS source is a fake (no real ps/lsof/ioreg/libproc)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from apex_router import agent_resources as ar

NOW = 1_790_000_000.0
FMT = "%a %b %d %H:%M:%S %Y"


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
                         "age_s": 30.0, "state": "active", "telemetry": None}
    assert by["aa2"]["type"] == "?" and by["aa2"]["state"] == "idle"
    assert by["ghost"]["state"] == "?" and out["list"][-1]["id"] == "ghost"
    for i in range(25):
        _subagent(tmp_path, "s2", f"x{i:02d}", i, {"agentType": "t"})
    out = ar.subagents(tmp_path, "s2", NOW)
    assert len(out["list"]) == ar.SUBAGENTS_MAX and out["more"] == 5
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
    assert models["claude-haiku-4-5"]["requests"] == 2 * ar.SUBAGENTS_MAX
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
    assert lines[0] == "claude · r · s1 · active · busy · pid 100 · 412MB · 14% · io 745/569MB"
    assert "  spawned Explore · d · 2 req · 1.5k out · 0 err · active · depth 1" in lines
    assert "    calls claude-haiku-4-5 ×2" in lines
    assert "  runs p0 (pid 200) · 1MB · 0%" in lines
    assert "  calls claude-opus-5-5 ×5" in lines
    assert "pi · q · s2 · idle" in lines
    assert ar.graph_text({}) == "no agents in the last hour"


def test_metrics_text():
    assert ar.metrics_text({"alive": True, "footprint_mb": 412.3, "cpu_pct": 14.0,
                            "read_mb": 745.2, "write_mb": 569.0}) == "412MB · 14% · io 745/569MB"
    assert ar.metrics_text({"alive": True, "rss_mb": 10.0, "cpu_pct": 0.5,
                            "read_mb": None}) == "10MB · 0.5%"
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
    assert c["res"]["tree"]["procs"] == 2 and c["res"]["tree"]["cpu_pct"] == 24.0
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
