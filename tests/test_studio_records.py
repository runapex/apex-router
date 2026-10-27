"""apex studio v2 (Metro): ``GET /api/records`` and ``ms_total`` from telemetry v13 ``total_ms``.

Contract: docs/design/studio-v2/API.md. Every test uses a fixture home in tmp_path
(tests/studio_fixture.py); nothing reads the real ~/.apex or ~/.apex-router, and no test calls the
real launchctl (the runner is a fake that records its calls)."""
from __future__ import annotations

import http.client
import json
import os
import plistlib
import threading
import time
from pathlib import Path

import pytest

from apex_router.studio import flow, records, server, traffic

import studio_fixture as F

TOKEN = "r" * 32


def _row(ts, **kw):
    r = {"ts": ts, "client": "claude-code", "endpoint_id": "anthropic", "schema_version": 13,
         "session_id": F.CLAUDE_SID, "is_error": False, "error_cause": None, "connect_retries": 0,
         "ttft_ms": 1000.0, "total_ms": 5000.0, "bytes_up": 6000, "bytes_down": 1200,
         "tokens_in": 2, "tokens_out": 10, "model_requested": "it-entra-claude-opus-5-5"}
    r.update(kw)
    return r


def _write(path: Path, rows, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class FakeRun:
    """Fake `launchctl print`: canned text per label; records every argv."""

    def __init__(self, by_label=None, rc=0):
        self.calls = []
        self.by_label = by_label or {}
        self.rc = rc

    def __call__(self, argv):
        self.calls.append(list(argv))
        label = argv[-1].rsplit("/", 1)[-1]
        txt = self.by_label.get(label, "")
        return (self.rc if txt else 113), txt


LD_OK = ("gui/501/com.apex-router.daily = {\n\tactive count = 0\n\tstate = not running\n\truns = 22\n"
         "\tlast exit code = 0\n\tendpoints = {\n\t\tstate = active\n\t}\n}\n")
LD_FAIL = "gui/501/com.apex.ornith-daily = {\n\tstate = not running\n\truns = 3\n\tlast exit code = 1\n}\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    now = time.time()
    F.make_home(root, now, telemetry=False)
    F.make_sessions(root)
    for k, v in F.env_for(root).items():
        monkeypatch.setenv(k, v)
    traffic._kinds.clear()
    records._cache.clear()
    records._ld_cache.clear()
    return root, now


def _plist(root: Path, label: str, extra: dict):
    la = root / "Library" / "LaunchAgents"
    with open(la / f"{label}.plist", "wb") as f:
        plistlib.dump({"Label": label, "ProgramArguments": ["/usr/bin/true"], **extra}, f)


def _setmtime(p: Path, ts: float):
    os.utime(p, (ts, ts))


# ---- ms_total from total_ms (step 2) --------------------------------------------------------------

def test_item_ms_total_comes_from_total_ms_and_is_null_on_old_rows(home):
    kinds = traffic.kinds_for(flow.resolve_paths())
    new = dict(_row(time.time()), _ts=time.time())
    assert traffic.item(new, b"x", kinds)["ms_total"] == 5000
    old = {k: v for k, v in new.items() if k != "total_ms"}            # a v12 row: no end time
    old["schema_version"] = 12
    assert traffic.item(old, b"x", kinds)["ms_total"] is None
    for bad in (None, 0, -5, True, "5000"):
        assert traffic.item(dict(new, total_ms=bad), b"x", kinds)["ms_total"] is None


def test_recent_and_stream_items_carry_ms_total(home):
    root, now = home
    old = _row(now - 20, schema_version=12)
    del old["total_ms"]                                                  # a v12 row has no total_ms key
    _write(root / ".apex" / "telemetry.jsonl", [_row(now - 30, total_ms=5432.4), old])
    got = traffic.recent(flow.resolve_paths(), 10)["requests"]
    assert [g["ms_total"] for g in got] == [None, 5432]                 # newest first; the v12 row has none


# ---- throughput / latency ----------------------------------------------------------------------

def test_throughput_counts_bytes_by_end_time_in_each_window(home):
    root, now = home
    rows = [_row(now - 30, bytes_up=6000, bytes_down=1200),             # ends in 1 min and 15 min
            _row(now - 600, bytes_up=60000, bytes_down=12000),          # 15 min only
            _row(now - 100, total_ms=60_000.0, bytes_up=300, bytes_down=600),   # started 100 s ago, ended 40 s ago
            _row(now - 3000, bytes_up=10**9)]                           # outside both windows
    _write(root / ".apex" / "telemetry.jsonl", rows)
    _write(root / ".apex-local" / "telemetry.jsonl", [_row(now - 10, client="codex", bytes_up=60, bytes_down=120)])
    d = records.collect(flow.resolve_paths(), now, FakeRun())
    t1, t15 = d["throughput"]["1m"], d["throughput"]["15m"]
    assert (t1["up_bytes"], t1["down_bytes"], t1["requests"]) == (6000 + 300 + 60, 1200 + 600 + 120, 3)
    assert t1["up_bps"] == round((6000 + 300 + 60) / 60, 1)
    assert (t15["up_bytes"], t15["requests"]) == (6000 + 60000 + 300 + 60, 4)
    assert t15["down_bps"] == round((1200 + 12000 + 600 + 120) / 900, 1)


def test_latency_p50_p95_first_word_and_whole_answer(home):
    root, now = home
    rows = [_row(now - 60 - i, ttft_ms=1000.0 + 100 * i, total_ms=5000.0 + 1000 * i) for i in range(20)]
    rows.append(_row(now - 50, is_error=True, ttft_ms=99999.0, total_ms=99999.0))      # errors are left out
    rows.append(_row(now - 40, schema_version=12, total_ms=None))                       # old row: first word only
    _write(root / ".apex" / "telemetry.jsonl", rows)
    lat = records.collect(flow.resolve_paths(), now, FakeRun())["latency"]["15m"]
    fw, wa = lat["first_word"], lat["whole_answer"]
    assert fw["n"] == 21 and wa["n"] == 20
    assert wa["p50"] == 14500                                  # median of 5000..24000 step 1000
    assert wa["p95"] == 23000                                  # nearest rank: index round(0.95 * 19) = 18
    assert fw["p50"] == 1900 and fw["p95"] == 2800             # 1000..2900 + the old row's 1000; errors left out


def test_latency_is_null_when_no_v13_rows(home):
    root, now = home
    _write(root / ".apex" / "telemetry.jsonl", [_row(now - 30, schema_version=12, total_ms=None)])
    lat = records.collect(flow.resolve_paths(), now, FakeRun())["latency"]["15m"]
    assert lat["whole_answer"] == {"p50": None, "p95": None, "n": 0} and lat["first_word"]["n"] == 1


def test_no_telemetry_gives_zeros_not_an_error(home):
    _, now = home
    d = records.collect(flow.resolve_paths(), now, FakeRun())
    assert d["telemetry_present"] is False and d["throughput"]["1m"]["requests"] == 0
    assert d["latency"]["1h"]["first_word"]["p50"] is None


def test_read_is_capped_and_marked_incomplete(home, monkeypatch):
    root, now = home
    rows = [_row(now - 7000 + i * 10) for i in range(700)]               # 2 h of rows
    _write(root / ".apex" / "telemetry.jsonl", rows)
    size = (root / ".apex" / "telemetry.jsonl").stat().st_size
    seen = []
    real = records.live._lines
    monkeypatch.setattr(records.live, "_lines", lambda p, cap: seen.append(cap) or real(p, cap))
    monkeypatch.setattr(records, "READ_CAP", size // 4)
    got = records.rows(root / ".apex" / "telemetry.jsonl", now - records.SPAN_S, cap=records.READ_CAP)
    assert seen and max(seen) <= size // 4
    assert got["complete"] is False and 0 < len(got["rows"]) < 700


def test_rotated_generation_is_read_when_the_live_file_is_young(home):
    root, now = home
    tel = root / ".apex" / "telemetry.jsonl"
    _write(tel.with_name("telemetry.jsonl.3"), [_row(now - 500, bytes_up=7)])
    _write(tel, [_row(now - 20, bytes_up=5)])
    d = records.collect(flow.resolve_paths(), now, FakeRun())
    assert d["throughput"]["15m"]["up_bytes"] == 12


# ---- disk --------------------------------------------------------------------------------------

def test_disk_active_archived_and_other_stores(home):
    root, now = home
    tel = root / ".apex" / "telemetry.jsonl"
    _write(tel, [_row(now - 5)])
    for n, size in ((1, 1000), (2, 3000)):
        tel.with_name(f"telemetry.jsonl.{n}").write_bytes(b"x" * size)
    tel.with_name("telemetry.jsonl.pre-v2-bak").write_bytes(b"x" * 50)      # not a generation
    tel.with_name("telemetry.jsonl.1.tmp").write_bytes(b"x" * 70)           # not a generation
    (root / ".apex" / "state.db").write_bytes(b"x" * 123)
    _write(root / ".apex-local" / "telemetry.jsonl", [_row(now - 5)])
    dk = records.collect(flow.resolve_paths(), now, FakeRun())["disk"]
    assert dk["active_bytes"] == tel.stat().st_size + (root / ".apex-local" / "telemetry.jsonl").stat().st_size
    assert (dk["archived_files"], dk["archived_bytes"], dk["keep_files"]) == (2, 4000, 20)
    other = {o["label"]: o for o in dk["other"]}
    assert other["Request index"] == {"label": "Request index", "bytes": 123, "present": True}
    assert other["Archive sample"]["present"] is False


# ---- pipelines ---------------------------------------------------------------------------------

def _pipelines(home_, run=None, now=None):
    _, n = home_
    d = records.collect(flow.resolve_paths(), now or n, run or FakeRun())
    return {p["id"]: p for p in d["pipelines"]}, d


def test_pipeline_map_is_fixed_and_every_entry_has_the_contract_fields(home):
    got, _ = _pipelines(home)
    assert list(got) == ["menubar", "daily", "archive", "pressure", "offload", "worldmodel"]
    for p in got.values():
        assert set(p) == {"id", "name", "source", "makes", "schedule", "launchd", "last_run_ts", "read_to_ts",
                          "behind_s", "late", "reads", "state", "error"}
        assert set(p["reads"]) == {"kind", "window_s", "rows", "bytes", "files", "files_waiting"}
        assert p["state"] in ("ok", "late", "failed", "never ran", "not installed", "unknown")


def test_menubar_reads_its_window_and_counts_rows_up_to_its_last_run(home):
    root, now = home
    plug = root / "Library" / "Application Support" / "SwiftBar" / "Plugins"
    plug.mkdir(parents=True)
    (plug / "apex.1m.sh").write_text("#!/bin/sh\n")
    _write(root / ".apex" / "telemetry.jsonl", [_row(now - 4000), _row(now - 3000), _row(now - 200), _row(now - 100), _row(now - 10)])
    _write(root / ".apex-local" / "telemetry.jsonl", [_row(now - 150)])     # local rows: not the menu bar's
    hist = root / ".apex-router" / "widget" / "history.jsonl"
    _write(hist, [{"v": 1, "ts": now - 60}])
    _setmtime(hist, now - 60)
    p = _pipelines(home)[0]["menubar"]
    assert p["state"] == "ok" and p["last_run_ts"] == pytest.approx(now - 60)
    assert p["reads"]["kind"] == "window" and p["reads"]["window_s"] == 3600
    assert p["reads"]["rows"] == 3                      # 3000, 200, 100 s ago; not 10 s ago (after the run)
    assert p["behind_s"] == pytest.approx(60, abs=1)


def test_menubar_late_and_not_installed(home):
    root, now = home
    hist = root / ".apex-router" / "widget" / "history.jsonl"
    _write(hist, [{"v": 1}])
    _setmtime(hist, now - 3 * 3600)
    assert _pipelines(home)[0]["menubar"]["state"] == "not installed"       # no SwiftBar plugin
    plug = root / "Library" / "Application Support" / "SwiftBar" / "Plugins"
    plug.mkdir(parents=True)
    (plug / "apex.1m.sh").write_text("#!/bin/sh\n")
    p = _pipelines(home)[0]["menubar"]
    assert p["state"] == "late" and p["late"] is True and p["reads"]["rows"] is None   # run outside the read span


def test_daily_uses_launchd_runner_plist_schedule_and_file_sizes(home):
    root, now = home
    _plist(root, "com.apex-router.daily", {"StartCalendarInterval": {"Hour": 9, "Minute": 0}})
    tel = root / ".apex" / "telemetry.jsonl"
    _write(tel, [_row(now - 5)])
    tel.with_name("telemetry.jsonl.1").write_bytes(b"x" * 100)
    tel.with_name("telemetry.jsonl.2").write_bytes(b"x" * 200)
    log = root / ".apex-router" / "logs" / "com.apex-router.daily.log"
    log.parent.mkdir(parents=True)
    log.write_text("report\n")
    _setmtime(log, now - 7200)
    run = FakeRun({"com.apex-router.daily": LD_OK})
    p = _pipelines(home, run)[0]["daily"]
    assert run.calls == [["/bin/launchctl", "print", f"gui/{os.getuid()}/com.apex-router.daily"]]
    assert p["launchd"] == {"installed": True, "loaded": True, "state": "not running", "runs": 22, "last_exit": 0}
    assert p["schedule"] == {"kind": "launchd", "text": "daily at 09:00", "every_s": 86400}
    assert p["reads"]["bytes"] == tel.stat().st_size + 200 and p["reads"]["files"] == 2   # live + newest only
    assert p["last_run_ts"] == pytest.approx(now - 7200) and p["state"] == "ok"


def test_launchd_failed_exit_and_not_installed_and_cache(home):
    root, now = home
    _plist(root, "com.apex.ornith-daily", {"StartCalendarInterval": {"Hour": 9, "Minute": 0}})
    out = root / ".apex" / "offload_daily.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("x")
    run = FakeRun({"com.apex.ornith-daily": LD_FAIL})
    got, _ = _pipelines(home, run)
    assert got["offload"]["state"] == "failed" and got["offload"]["launchd"]["last_exit"] == 1
    assert got["daily"]["state"] == "not installed" and got["daily"]["launchd"]["installed"] is False
    n = len(run.calls)
    _pipelines(home, run)
    assert len(run.calls) == n                                     # cached: no second launchctl call


def test_launchd_runner_error_fails_open(home):
    root, now = home
    _plist(root, "com.apex-router.daily", {"StartCalendarInterval": {"Hour": 9, "Minute": 0}})

    def boom(argv):
        raise OSError("no launchctl")
    p = _pipelines(home, boom)[0]["daily"]
    assert p["error"] is None and p["launchd"]["installed"] is True and p["launchd"]["loaded"] is None


def test_archive_keeper_reports_its_cursor(home):
    root, now = home
    tel = root / ".apex" / "telemetry.jsonl"
    _write(tel, [_row(now - 5)])
    for n in (1, 2, 3):
        tel.with_name(f"telemetry.jsonl.{n}").write_bytes(b"x")
    (root / ".apex" / "telemetry_reservoir.json").write_text(json.dumps({"v": 1, "folded": [
        {"gen": "sha256:a", "name": "telemetry.jsonl.1", "at": now - 900, "rows": 11766, "skipped": 1, "bad": 0},
        {"gen": "sha256:b", "name": "telemetry.jsonl.2", "at": now - 800, "rows": 29961, "skipped": 2, "bad": 0}],
        "by_cause": {}, "reservoir": None}))
    p = _pipelines(home)[0]["archive"]
    assert p["reads"] == {"kind": "cursor", "window_s": None, "rows": 41727, "bytes": None, "files": 2,
                          "files_waiting": 1}
    assert p["last_run_ts"] == pytest.approx(now - 800) and p["state"] == "ok" and p["behind_s"] is None


def test_bad_store_fails_open_for_that_entry_only(home):
    root, now = home
    (root / ".apex").mkdir(exist_ok=True)
    (root / ".apex" / "telemetry_reservoir.json").write_text("{not json")
    got, d = _pipelines(home)
    assert got["archive"]["error"] == "cannot read: JSONDecodeError" and got["archive"]["state"] == "unknown"
    assert got["menubar"]["error"] is None and d["throughput"]["1m"]["requests"] == 0


def test_pressure_and_worldmodel_on_demand(home):
    root, now = home
    _write(root / ".apex" / "telemetry.jsonl", [_row(now - 400), _row(now - 300), _row(now - 50)])
    pj = root / ".apex-router" / "pressure.json"
    pj.write_text("{}")
    _setmtime(pj, now - 100)
    got, _ = _pipelines(home)
    assert got["pressure"]["reads"]["rows"] == 2 and got["pressure"]["late"] is False
    assert got["pressure"]["schedule"]["kind"] == "demand" and got["pressure"]["state"] == "ok"
    assert got["worldmodel"]["state"] == "never ran" and got["worldmodel"]["reads"]["files"] == 1


# ---- the route ---------------------------------------------------------------------------------

@pytest.fixture
def studio(home):
    root, now = home
    _write(root / ".apex" / "telemetry.jsonl", [_row(now - 30)])
    _plist(root, "com.apex-router.daily", {"StartCalendarInterval": {"Hour": 9, "Minute": 0},
                                           "EnvironmentVariables": {"APEX_FAKE_API_TOKEN": F.FAKE_SECRET}})
    srv = server.make_server(port=0, token=TOKEN, paths=flow.resolve_paths())
    srv.records_run = FakeRun({"com.apex-router.daily": LD_OK})
    srv.stats_fetch = srv.restart_fetch = lambda url: None
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv, root, now
    srv.shutdown()
    srv.server_close()


def get(srv, path, token=TOKEN, query_token=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    h = {"Host": f"127.0.0.1:{srv.port}"}
    if token:
        h["X-Studio-Token"] = token
    if query_token:
        path += "?token=" + query_token
    c.request("GET", path, headers=h)
    r = c.getresponse()
    txt = r.read().decode()
    c.close()
    try:
        return r.status, json.loads(txt)
    except ValueError:
        return r.status, txt


def test_route_shape_and_runner_injected(studio):
    srv, root, now = studio
    st, d = get(srv, "/api/records")
    assert st == 200
    assert set(d) == {"generated_at", "telemetry_present", "complete", "throughput", "latency", "disk", "pipelines"}
    assert set(d["throughput"]) == {"1m", "15m"} and set(d["latency"]) == {"15m", "1h"}
    assert d["latency"]["15m"]["whole_answer"]["p50"] == 5000
    assert srv.records_run.calls and all(c[0] == "/bin/launchctl" for c in srv.records_run.calls)


def test_route_needs_the_header_token(studio):
    srv, _, _ = studio
    assert get(srv, "/api/records", token=None)[0] == 403
    assert get(srv, "/api/records", token=None, query_token=TOKEN)[0] == 403     # header only
    assert get(srv, "/api/records", token="wrong")[0] == 403


def test_route_leaks_no_path_and_no_secret(studio):
    srv, root, _ = studio
    _, d = get(srv, "/api/records")
    txt = json.dumps(d)
    assert F.FAKE_SECRET not in txt and str(root) not in txt and "/Users/" not in txt
    assert "telemetry.jsonl" not in txt and F.CLAUDE_SID not in txt


def test_page_has_the_records_panel_reader(studio):
    srv, _, _ = studio
    st, html = get(srv, "/")
    assert st == 200 and "/api/records" in html and "innerHTML" not in html


# ---- bounds (xval P1: whole-file reads, unbounded listing, unbounded subprocess output) ----------

def test_read_capped_refuses_a_file_over_the_cap_in_one_read(tmp_path):
    from apex_router.studio import records as R
    p = tmp_path / "s.json"
    p.write_bytes(b"x" * 101)
    import pytest
    with pytest.raises(ValueError):
        R.read_capped(p, 100)
    p.write_bytes(b"x" * 100)
    assert R.read_capped(p, 100) == b"x" * 100


def test_generations_are_bounded_and_skip_symlinks(tmp_path, monkeypatch):
    from apex_router.studio import records as R
    monkeypatch.setattr(R, "GENS_MAX", 3)
    base = tmp_path / "telemetry.jsonl"
    for i in range(1, 7):
        (tmp_path / f"telemetry.jsonl.{i}").write_text("{}\n")
    (tmp_path / "telemetry.jsonl.99").symlink_to(tmp_path / "telemetry.jsonl.1")
    assert [g.name for g in R.generations(base)] == ["telemetry.jsonl.4", "telemetry.jsonl.5", "telemetry.jsonl.6"]
    monkeypatch.setattr(R, "LIST_MAX", 2)
    assert len(R.generations(base)) <= 2


def test_run_keeps_at_most_the_cap_and_is_killed_on_timeout(monkeypatch):
    import subprocess, sys, time as _t
    import pytest
    from apex_router.studio import records as R
    monkeypatch.setattr(R, "RUN_OUT_MAX", 1000)
    rc, txt = R._run([sys.executable, "-c", "import sys; sys.stdout.write('a' * 5_000_000)"])
    assert rc == 0 and len(txt) == 1000
    t = _t.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        R._run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    assert _t.monotonic() - t < 5


def test_an_oversized_plist_gives_no_schedule(tmp_path):
    from apex_router.studio import records as R
    from apex_router.studio.flow import Paths
    import types
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "x.plist").write_bytes(b"<?xml version='1.0'?>" + b" " * (R.PLIST_MAX + 10))
    paths = types.SimpleNamespace(launch_agents=la)
    assert R._plist_schedule(paths, "x") is None


def test_run_kills_a_grandchild_that_holds_the_pipe():
    import subprocess, time as _t
    from apex_router.studio import records as R
    t = _t.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        R._run(["/bin/sh", "-c", "sleep 60 & wait"], timeout=0.5)
    assert _t.monotonic() - t < 4


def test_run_cleans_up_when_the_timer_cannot_start(monkeypatch):
    import subprocess
    from apex_router.studio import records as R
    seen = []
    real = subprocess.Popen

    def popen(*a, **k):
        seen.append(real(*a, **k))
        return seen[-1]
    import apex_router.safe_proc as S
    monkeypatch.setattr(S.subprocess, "Popen", popen)

    class NoThread:
        def __init__(self, *a, **k):
            raise RuntimeError("can't start new thread")
    monkeypatch.setattr(S.threading, "Timer", NoThread)
    with pytest.raises(RuntimeError):
        R._run(["/bin/sleep", "30"])
    assert seen[0].poll() is not None and seen[0].stdout.closed
