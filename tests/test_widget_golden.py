"""Golden output of the menu-bar widget on a FIXED fake HOME — a refactor guard.

Builds the same fake machine every run (Claude Code / pi / Codex sessions, subagents, telemetry,
ps / lsof / ioreg / netstat / ollama / libproc fakes, a fixed clock) and compares
``snapshot --json``, ``--menubar`` (with a fixed history) and ``--graph`` byte for byte with the
files in ``tests/fixtures/widget_golden/``. The temp HOME path is replaced by ``<HOME>``; the
``ts`` fields are dropped from the JSON before the comparison.

An intended output change: rerun with ``APEX_WIDGET_GOLDEN_UPDATE=1`` and review the diff.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
from pathlib import Path

import pytest

from apex_router import agent_resources as ar
from apex_router import snapshot, widget_history

NOW = 1_791_400_000.0
GOLDEN = Path(__file__).parent / "fixtures" / "widget_golden"
UPDATE_ENV = "APEX_WIDGET_GOLDEN_UPDATE"
FMT = "%a %b %d %H:%M:%S %Y"
SLUG = "-Users-you-src-demo"
SID_A = "aaaa1111-0000-4000-8000-000000000001"     # busy, running subagents, errors
SID_B = "bbbb2222-0000-4000-8000-000000000002"     # busy, waiting on a quiet subagent
SID_C = "cccc3333-0000-4000-8000-000000000003"     # idle
PI_SID = "0199aaaa-0000-7000-8000-00000000abcd"
CODEX_SID = "0199bbbb-0000-7000-8000-00000000cdef"
BIN = "/opt/apex/bin/apex-router"


def _lstart(epoch):
    return time.strftime(FMT, time.localtime(epoch))


def _ps(pid, ppid, rss_kb, cpu, comm, start):
    return f"{pid:>6} {ppid:>6} {rss_kb:>8} {cpu:>5} {_lstart(start)}     {comm}"


def _touch(path: Path, age: float, text: str = "{}\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (NOW - age, NOW - age))


def _sub(home, sid, aid, spawn_age, last_age, typ, desc, depth=1):
    d = home / ".claude" / "projects" / SLUG / sid / "subagents"
    _touch(d / f"agent-{aid}.jsonl", last_age)
    _touch(d / f"agent-{aid}.meta.json", spawn_age,
           json.dumps({"agentType": typ, "description": desc, "spawnDepth": depth}))


def _session_file(home, pid, sid, start, status):
    _touch(home / ".claude" / "sessions" / f"{pid}.json", 10, json.dumps(
        {"pid": pid, "sessionId": sid, "procStart": time.strftime(FMT, time.gmtime(start)),
         "status": status, "name": "n", "cwd": "/x"}))


def _row(sid, age, aid=None, model="claude-opus-5-5", **kw):
    r = {"ts": NOW - age, "client": "claude-code", "session_id": sid, "agent_id": aid,
         "model_requested": model, "endpoint_id": "anthropic", "tokens_in": 120,
         "tokens_out": 900, "cache_read_tokens": 150_000, "cache_write_tokens": 2_000,
         "is_error": False, "error_cause": None, "ttft_ms": 1500, "bytes_up": 600_000,
         "bytes_down": 9_000}
    r.update(kw)
    return r


def build_home(home: Path) -> dict:
    """The fixed fake machine. Returns the fake OS sources."""
    start = NOW - 2 * 3600
    old = NOW - 3 * 86400
    work = home / "work"
    # Claude Code: three sessions in one project
    proj = home / ".claude" / "projects" / SLUG
    _touch(proj / f"{SID_A}.jsonl", 8)
    _touch(proj / f"{SID_B}.jsonl", 15 * 60)
    _touch(proj / f"{SID_C}.jsonl", 30 * 60)
    _session_file(home, 100, SID_A, start, "busy")
    _session_file(home, 200, SID_B, start, "busy")
    _session_file(home, 250, SID_C, start, "idle")
    (home / ".claude" / "sessions" / "100.abc.key").write_text("never read")
    _sub(home, SID_A, "a0000000001", 14 * 60, 2, "general-purpose", "fix the parser")
    _sub(home, SID_A, "a0000000002", 31 * 60, 5, "Explore", "long search \x1b[31mred\x1b[0m")
    _sub(home, SID_A, "a0000000003", 12 * 60, 3 * 60, "Explore", "quiet | one")
    _sub(home, SID_A, "a0000000004", 40 * 60, 20 * 60, "Plan", "-- done plan")
    _sub(home, SID_A, "a0000000005", 30 * 60, 4 * 60, "Explore", "errors here", depth=2)
    _sub(home, SID_B, "b0000000001", 40 * 60, 25 * 60, "Explore", "finished early")
    _sub(home, SID_B, "b0000000002", 30 * 60, 12 * 60, "general-purpose", "hung review")
    # pi: one live session matched by cwd; codex: one session, no process
    pi_dir = home / ".pi" / "agent" / "sessions" / "--work-pilot--"
    _touch(pi_dir / f"2026-10-07T10-00-00-000Z_{PI_SID}.jsonl", 120,
           json.dumps({"type": "session", "cwd": str(work / "pilot")}) + "\n{}\n")
    d = _dt.datetime.fromtimestamp(NOW)
    cx = home / ".codex" / "sessions" / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
    _touch(cx / f"rollout-2026-10-07T10-00-00-{CODEX_SID}.jsonl", 40 * 60,
           json.dumps({"type": "session_meta", "payload": {"cwd": str(work / "cx")}}) + "\n")
    # telemetry
    rows = []
    for i in range(12):
        rows.append(_row(SID_A, 30 + i * 240))
    rows[0].update(tokens_in=200, cache_read_tokens=280_000)            # the latest: ctx 282k
    rows.append(_row(SID_A, 100, is_error=True, error_cause="http_529"))
    for i in range(6):
        rows.append(_row(SID_A, 4 + i * 60, "a0000000001", model="claude-fable-5-1"))
    for i in range(3):
        rows.append(_row(SID_A, 10 + i * 300, "a0000000002"))
    rows.append(_row(SID_A, 200, "a0000000005", is_error=True, error_cause="midstream_ReadError"))
    rows.append(_row(SID_A, 250, "a0000000005", cache_read_tokens=870_000))
    rows.append(_row(SID_A, 90, "aghost00001"))                         # traffic, no log
    for i in range(4):
        rows.append(_row(SID_B, 13 * 60 + i * 60, "b0000000002", model="claude-sonnet-5-5"))
    rows.append(_row(SID_B, 16 * 60))
    rows.append(_row(PI_SID, 130, model="it-entra-claude-opus-5-5", endpoint_id="openai",
                     tokens_in=50_000, cache_read_tokens=40_000))
    rows.append(_row(None, 50))                                         # unattributed
    tel = home / ".apex" / "telemetry.jsonl"
    _touch(tel, 1, "".join(json.dumps(r) + "\n" for r in sorted(rows, key=lambda r: r["ts"])))
    # observe (limit meter) + adapters
    _touch(home / ".apex-router" / "observe" / "2026-10-07.jsonl", 60, json.dumps(
        {"ev": "measure", "ts": (NOW - 120) * 1000, "limit_kind": "five_hour", "limit_pct": 42,
         "cost_usd": 1.23, "ctx_pct": 31}) + "\n")
    _touch(home / "adapters" / "demo.json", 60, json.dumps(
        {"title": "Demo | adapter", "rows": ["queue 3", "-- last run ok", 7], "ts": NOW - 300}))

    ps_rows = [
        _ps(100, 1, 409600, 4.0, "/opt/claude-code/claude", start),
        _ps(101, 100, 20480, 1.0, "/bin/zsh", start),
        _ps(102, 101, 102400, 12.0, "node", start),
        _ps(103, 101, 51200, 30.0, "python3.13", start),
        _ps(200, 1, 204800, 0.5, "claude", start),
        _ps(250, 1, 153600, 0.0, "claude", start),
        _ps(300, 1, 81920, 0.2, "pi", NOW - 86400),
        _ps(301, 300, 4096, 0.0, "node", NOW - 86400),
        _ps(310, 1, 61440, 0.0, "pi", old),
        _ps(320, 1, 30720, 0.0, "codex", NOW - 7 * 86400),
        _ps(400, 1, 40960, 0.0, "/opt/homebrew/bin/ollama", old),
        _ps(401, 400, 5_000_000, 50.0, "ollama", NOW - 600),
        _ps(500, 1, 30720, 0.1, "Python", old),
        _ps(600, 1, 999999, 80.0, "/Applications/Other.app/Contents/MacOS/Other", old),
    ]
    args = {102: "node /usr/lib/node_modules/pyright/langserver.index.js --stdio",
            103: "python3.13 -m pytest -q --token=SECRET",
            500: "Python -m apex_router.cli labels --model x"}
    cwds = {300: work / "pilot", 301: work / "pilot", 310: work / "old-proj",
            320: work / "cx2"}
    ioreg = ('+-o AGXAcceleratorG15X  <class AGXAcceleratorG15X>\n'
             '  "PerformanceStatistics" = {"Device Utilization %"=37,"In use system memory"='
             '1468006400,"Alloc system memory"=31999999999}\n')
    netstat = ("Name  Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts "
               "Oerrs     Obytes  Coll\n"
               "en0   1500  <Link#11>   aa:bb:cc:dd:ee:ff  100 0  987654321  90 0  123456789 0\n"
               "lo0   16384 <Link#1>                       5 0  5000  5 0  5000 0\n")

    def run(argv, timeout=2.0):
        if argv[:2] == ["ps", "-axo"]:
            return "\n".join(ps_rows) + "\n"
        if argv[:2] == ["ps", "-o"]:
            want = [int(p) for p in argv[-1].split(",")]
            return "".join(f"{p} {args[p]}\n" for p in want if p in args)
        if argv[0] == "lsof" and "cwd" in argv:
            want = [int(p) for p in argv[-1].split(",")]
            return "".join(f"p{p}\nfcwd\nn{cwds[p]}\n" for p in want if p in cwds)
        if argv[0] == "lsof":
            return "p500\ncPython\np401\ncollama\n"
        if argv[0] == "ioreg":
            return ioreg
        if argv[0] == "netstat":
            return netstat
        raise RuntimeError(f"{argv[0]} exit 1")

    seen: dict = {}

    def rusage(pid):
        n = seen[pid] = seen.get(pid, 0) + 1
        base = {"footprint_mb": 10.0 * (pid % 97 + 1), "read_mb": 100.0 + pid,
                "write_mb": 50.0 + pid, "cpu_s": 1000.0 + pid}
        if n > 1:                                # the second sample, 0.25 s later
            base = dict(base, cpu_s=base["cpu_s"] + (pid % 7) * 0.025,
                        read_mb=base["read_mb"] + (0.5 if pid == 103 else 0.0),
                        write_mb=base["write_mb"] + (0.25 if pid == 103 else 0.0))
        return base

    def http_get(url, timeout=1.0):
        exp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW + 240)) + ".123456789Z"
        return json.dumps({"models": [
            {"name": "qwen3:8b", "size": 6 << 30, "size_vram": 6 << 30, "expires_at": exp},
            {"name": "nomic-embed-text:latest", "size": 1 << 29, "size_vram": 1 << 29,
             "expires_at": "2318-01-17T10:00:00.000000000+01:00"}]}).encode()

    return {"run": run, "rusage": rusage, "http_get": http_get, "telemetry": tel}


def _history(snap):
    past = []
    for k in range(5, 0, -1):
        past.append({"v": 1, "ts": NOW - 60 * k, "agents": [
            {"session": SID_A, "kind": "claude", "status": "busy", "footprint_mb": 900.0 + k,
             "cpu_pct": 10.0 * k, "io_mbs": 0.6 if k == 2 else 0.0, "ctx": 250_000 + k * 1000,
             "req_5m": k}], "system": {"gpu_util_pct": 5 * k, "gpu_mem_mb": 30000.0,
                                       "load1": 2.0, "net_rx": 987_000_000 - k * 600_000,
                                       "net_tx": 123_000_000 - k * 60_000, "net_if": "en0"}})
    return past + [widget_history.sample(snap)]


def _strip_ts(x):
    if isinstance(x, dict):
        return {k: _strip_ts(v) for k, v in x.items() if k != "ts"}
    if isinstance(x, list):
        return [_strip_ts(v) for v in x]
    return x


def render(home: Path, monkeypatch) -> dict:
    src = build_home(home)
    t = [0.0]
    monkeypatch.setattr(ar, "run_cmd", src["run"])
    monkeypatch.setattr(ar, "rusage", src["rusage"])
    monkeypatch.setattr(ar, "http_get", src["http_get"])
    monkeypatch.setattr(ar, "_clock", lambda: t[0])
    monkeypatch.setattr(ar, "_sleep", lambda s: t.__setitem__(0, t[0] + s))
    monkeypatch.setattr(os, "getloadavg", lambda: (2.5, 2.25, 2.0))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APEX_ROUTER_HOME", str(home / ".apex-router"))
    monkeypatch.setenv("DATAPCE_HOME", str(home / ".datapce"))
    snap = snapshot.collect(
        home=home, telemetry=src["telemetry"], observe_dir=home / ".apex-router" / "observe",
        adapters=home / "adapters", now=NOW,
        proxy_fn=lambda: {"up": False, "error": "refused"},
        worker_fn=lambda: {"label": "com.ornith.worker", "pid": 500, "running": True,
                           "inbox": 2, "queue_running": 1})
    hist = _history(snap)
    out = {"snapshot.json": json.dumps(_strip_ts(snap), indent=2, sort_keys=True) + "\n",
           "menubar.txt": snapshot.menubar(snap, history=hist, bin_path=BIN) + "\n",
           "graph.txt": ar.graph_text(snap.get("graph") or {}) + "\n"}
    reals = {str(home), os.path.realpath(home)}
    for k, v in out.items():
        for r in sorted(reals, key=len, reverse=True):
            v = v.replace(r, "<HOME>")
        out[k] = v
    return out


@pytest.mark.parametrize("name", ["snapshot.json", "menubar.txt", "graph.txt"])
def test_widget_output_is_byte_identical_to_the_golden(tmp_path, monkeypatch, name):
    got = render(tmp_path / "home", monkeypatch)[name]
    path = GOLDEN / name
    if os.environ.get(UPDATE_ENV):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(got, encoding="utf-8")
    assert got == path.read_text(encoding="utf-8")


def test_golden_fixture_exercises_every_part(tmp_path, monkeypatch):
    """The fixture still covers what the golden is meant to guard."""
    out = render(tmp_path / "home", monkeypatch)
    menu, snap = out["menubar.txt"], json.loads(out["snapshot.json"])
    assert snapshot.WAITING_SYM + " hung review" in menu          # waiting? heuristic
    assert "--pi · old-proj · quiet · up 3d" in menu              # quiet pi process
    assert "--codex · cx2 · quiet · up 7d" in menu
    assert "SECRET" not in out["snapshot.json"] + menu + out["graph.txt"]
    assert "\x1b" not in menu and "never read" not in out["snapshot.json"]
    by = {a.get("session_id"): a for a in snap["agents"]}
    assert by[PI_SID]["res"]["pid_source"] == "lsof-cwd"
    assert snap["system"]["rate_window_s"] == 0.25 and snap["system"]["ollama_clients"]
    assert {n["kind"] for n in snap["graph"]["nodes"]} >= {"session", "subagent", "process",
                                                            "model"}
