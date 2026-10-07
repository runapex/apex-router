"""Tests for apex_router.snapshot — the read-only menu-bar snapshot and its SwiftBar formatter."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apex_router import cli, snapshot

NOW = 1_790_000_000.0


def _row(ts, cause=None, is_error=False, model="claude-opus-5-5", **kw):
    r = {"ts": ts, "client": "claude-code", "session_id": "s1", "agent_id": None,
         "model_requested": model, "is_error": is_error, "error_cause": cause,
         "error_detail": None, "ttft_ms": 500.0}
    r.update(kw)
    return r


def _telemetry(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _ok(n):
    return [_row(NOW - 60 + i * 0.01) for i in range(n)]


def _down():
    return {"up": False, "error": "refused"}


def _worker():
    return {"label": "w", "pid": None, "running": False, "inbox": 0, "queue_running": 0}


def _collect(tmp_path, **kw):
    kw.setdefault("home", tmp_path)
    kw.setdefault("telemetry", tmp_path / ".apex" / "telemetry.jsonl")
    kw.setdefault("observe_dir", tmp_path / ".apex-router" / "observe")
    kw.setdefault("adapters", tmp_path / ".apex-router" / "adapters")
    return snapshot.collect(now=NOW, proxy_fn=_down, worker_fn=_worker, **kw)


# ---------------------------------------------------------------- dot colour

@pytest.mark.parametrize("p,colour", [
    ({"level": "GREEN", "insufficient_sample": False}, "green"),
    ({"level": "GREEN", "insufficient_sample": True}, "gray"),
    ({"level": "AMBER", "insufficient_sample": False}, "orange"),
    ({"level": "RED", "insufficient_sample": False}, "red"),
    ({"level": "RED", "insufficient_sample": True}, "red"),       # fresh retry-after forces RED
    ({"level": "UNKNOWN", "insufficient_sample": True}, "gray"),
    ({"error": "boom"}, "gray"),
    ({"level": "GREEN"}, "gray"),                                 # sample unknown: not calm
])
def test_dot(p, colour):
    assert snapshot.dot({"pressure": p}) == colour


def test_dot_missing_pressure_is_gray():
    assert snapshot.dot({}) == "gray"
    assert snapshot.dot(None) == "gray"


def test_bar_colors_end_to_end(tmp_path):
    tel = tmp_path / ".apex" / "telemetry.jsonl"
    _telemetry(tel, _ok(100))
    bar = snapshot.menubar(_collect(tmp_path)).splitlines()[0]
    assert bar.startswith("●") and "color=#34C759" in bar

    _telemetry(tel, _ok(90) + [_row(NOW - 30, cause="ReadError", is_error=True)
                               for _ in range(5)])
    assert "color=#FF9500" in snapshot.menubar(_collect(tmp_path)).splitlines()[0]

    _telemetry(tel, _ok(50) + [_row(NOW - 30, cause="SSLError", is_error=True)
                               for _ in range(20)])
    assert "color=#FF3B30" in snapshot.menubar(_collect(tmp_path)).splitlines()[0]

    _telemetry(tel, _ok(3))
    out = snapshot.menubar(_collect(tmp_path))
    assert "color=#8E8E93" in out.splitlines()[0]
    assert "insufficient sample (n=3)" in out


def test_unknown_when_no_telemetry_is_gray(tmp_path):
    snap = _collect(tmp_path)
    assert snap["pressure"]["level"] == "UNKNOWN"
    assert "error" in snap["pressure"]
    assert "color=#8E8E93" in snapshot.menubar(snap).splitlines()[0]


def test_menubar_error_is_gray():
    out = snapshot.menubar_error("x | y")
    assert out.splitlines()[0] == "● | color=#8E8E93"
    assert "x ¦ y" in out and out.endswith("Refresh | refresh=true")


# ---------------------------------------------------------------- blocks

def test_errors15m_by_cause(tmp_path):
    tel = _telemetry(tmp_path / "t.jsonl", _ok(20) + [
        _row(NOW - 30, cause="ReadError", is_error=True),
        _row(NOW - 40, cause="ReadError", is_error=True),
        _row(NOW - 50, cause=None, is_error=True),
        _row(NOW - 20 * 60, cause="SSLError", is_error=True),     # outside 15 min
    ])
    e = snapshot.errors_block(tel, NOW)
    assert e == {"n": 3, "by_cause": {"ReadError": 2, "unlabeled": 1}, "window_min": 15}


def test_pressure_block_shape(tmp_path):
    tel = _telemetry(tmp_path / "t.jsonl", _ok(30))
    p = snapshot.pressure_block(tel, NOW)
    assert p["level"] == "GREEN" and p["insufficient_sample"] is False
    assert p["n"] == 30 and p["window_min"] == 15
    assert p["families"] == {"opus": "GREEN"}
    assert set(p["causes"]) >= {"rate_limited", "transport_errors", "retried"}


def test_measure_newest_event_ms_ts(tmp_path):
    d = tmp_path / "observe"
    d.mkdir()
    (d / "2026-10-05.jsonl").write_text(json.dumps(
        {"ev": "measure", "ts": (NOW - 9000) * 1000, "limit_kind": "seven_day",
         "limit_pct": 50, "cost_usd": 1.0}) + "\n")
    (d / "2026-10-06.jsonl").write_text("\n".join([
        json.dumps({"ev": "measure", "ts": (NOW - 900) * 1000, "limit_kind": "seven_day",
                    "limit_pct": 71, "resets_at": "2026-10-08T00:00:00Z", "cost_usd": 2.5,
                    "ctx_pct": 16}),
        json.dumps({"ev": "spawn", "ts": (NOW - 10) * 1000}),
        "garbage",
    ]) + "\n")
    m = snapshot.measure_block(d, NOW)
    assert m["limit_kind"] == "seven_day" and m["limit_pct"] == 71
    assert m["cost_usd"] == 2.5 and m["age_s"] == pytest.approx(900, abs=1)
    assert snapshot._measure_line(m) == "7d 71% · $2.50 · age 15m"


def test_measure_camelcase_and_missing(tmp_path):
    d = tmp_path / "observe"
    d.mkdir()
    assert snapshot.measure_block(d, NOW)["missing"] is True
    (d / "a.jsonl").write_text(json.dumps(
        {"ev": "measure", "ts": NOW - 60, "limitKind": "five_hour", "limitPercent": 3,
         "costUsd": 0.004}) + "\n")
    m = snapshot.measure_block(d, NOW)
    assert (m["limit_kind"], m["limit_pct"], m["age_s"]) == ("five_hour", 3, 60.0)
    assert snapshot._measure_line(m) == "5h 3% · <$0.01 · age 1m"


# ---------------------------------------------------------------- adapters

def _adapter(d: Path, name: str, doc) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return p


def test_adapters_caps_and_malformed(tmp_path):
    d = tmp_path / "ad"
    _adapter(d, "0-bad.json", "{not json")
    _adapter(d, "0-list.json", [1, 2])
    _adapter(d, "0-notitle.json", {"rows": ["x"], "ts": NOW})
    _adapter(d, "0-norows.json", {"title": "t", "rows": "x", "ts": NOW})
    for i in range(7):
        _adapter(d, f"{i + 1}.json", {"title": f"T{i}", "rows": [f"row {j} " + "x" * 200
                                                                 for j in range(15)],
                                      "ts": NOW - 60})
    out = snapshot.adapters_block(d, NOW)
    assert [a["title"] for a in out] == ["T0", "T1", "T2", "T3", "T4"]
    assert all(len(a["rows"]) == 10 for a in out)
    assert all(len(r) <= 120 for a in out for r in a["rows"])
    assert out[0]["age_s"] == 60.0 and out[0]["stale"] is False


def test_adapters_stale_and_ms_ts(tmp_path):
    d = tmp_path / "ad"
    _adapter(d, "a.json", {"title": "Old", "rows": ["r"], "ts": NOW - 2 * 86400})
    _adapter(d, "b.json", {"title": "Fresh", "rows": ["r"], "ts": (NOW - 120) * 1000})
    _adapter(d, "c.json", {"title": "NoTs", "rows": ["r"]})
    out = {a["title"]: a for a in snapshot.adapters_block(d, NOW)}
    assert out["Old"]["stale"] is True
    assert out["Fresh"]["stale"] is False and out["Fresh"]["age_s"] == 120.0
    assert out["NoTs"]["stale"] is True and out["NoTs"]["age_s"] is None
    text = snapshot.menubar({"pressure": {}, "adapters": list(out.values())})
    assert "stale · updated 2d ago" in text
    assert "updated 2m ago" in text


def test_adapters_two_homes_dedup_and_shared_cap(tmp_path, monkeypatch):
    dp = tmp_path / "dp"
    ar = tmp_path / "ar"
    monkeypatch.setenv("DATAPCE_HOME", str(dp))
    monkeypatch.setenv("APEX_ROUTER_HOME", str(ar))
    monkeypatch.delenv("APEX_ADAPTERS_DIR", raising=False)
    _adapter(dp / "adapters", "0same.json", {"title": "from datapce", "rows": [], "ts": NOW})
    _adapter(ar / "adapters", "0same.json", {"title": "from apex", "rows": [], "ts": NOW})
    for i in range(3):
        _adapter(dp / "adapters", f"d{i}.json", {"title": f"D{i}", "rows": [], "ts": NOW})
        _adapter(ar / "adapters", f"a{i}.json", {"title": f"A{i}", "rows": [], "ts": NOW})
    _adapter(ar / "adapters", ".tmp.json", {"title": "hidden", "rows": [], "ts": NOW})
    titles = [a["title"] for a in snapshot.adapters_block(None, NOW)]
    assert len(titles) == 5                                     # one cap across both homes
    assert "from datapce" in titles and "from apex" not in titles and "hidden" not in titles
    assert titles == ["from datapce", "A0", "A1", "A2", "D0"]
    # override replaces both
    only = tmp_path / "only"
    _adapter(only, "x.json", {"title": "Only", "rows": [], "ts": NOW})
    monkeypatch.setenv("APEX_ADAPTERS_DIR", str(only))
    assert [a["title"] for a in snapshot.adapters_block(None, NOW)] == ["Only"]


def test_adapters_dirs_default_under_home(monkeypatch):
    monkeypatch.delenv("APEX_ADAPTERS_DIR", raising=False)
    monkeypatch.delenv("DATAPCE_HOME", raising=False)
    monkeypatch.delenv("APEX_ROUTER_HOME", raising=False)
    home = Path.home()
    assert snapshot.adapters_dirs() == [home / ".datapce" / "adapters",
                                        home / ".apex-router" / "adapters"]


# ---------------------------------------------------------------- escaping / format

def test_esc():
    assert snapshot.esc("a | b") == "a ¦ b"
    assert snapshot.esc("line1\nline2") == "line1 line2"
    assert snapshot.esc("---") == "–––"
    assert snapshot.esc("--x") == "––x"
    assert len(snapshot.esc("y" * 500)) == 120


def test_user_text_is_escaped_in_menu(tmp_path):
    snap = {
        "pressure": {"level": "GREEN", "insufficient_sample": False, "n": 50, "window_min": 15},
        "errors15m": {"n": 1, "by_cause": {"Bad|Cause": 1}},
        "agents": [{"kind": "claude", "repo": "evil | bash=/bin/rm", "session": "abcd1234",
                    "state": "active", "age_s": 5}],
        "adapters": [{"title": "--T | x", "rows": ["r | refresh=true", "---"], "age_s": 5,
                      "stale": False}],
    }
    out = snapshot.menubar(snap)
    lines = out.splitlines()
    # only our own lines carry a parameter separator
    with_params = [ln for ln in lines if "|" in ln]
    assert all(ln.startswith(("● ", "Refresh |")) or ln.endswith(f"size=12 color={snapshot.COLORS['gray']}")
               or (ln.endswith(" | emojize=false") and ln.count("|") == 1)
               for ln in with_params), with_params
    assert "evil ¦ bash=/bin/rm" in out
    assert "––T ¦ x | size=12" in out
    assert "––– | emojize=false" in lines and lines[-1] == "Refresh | refresh=true"


def test_menubar_sections_and_idle_submenu(tmp_path):
    snap = {
        "pressure": {"level": "GREEN", "insufficient_sample": True, "n": 4, "window_min": 15},
        "errors15m": {"n": 0, "by_cause": {}},
        "measure": {"limit_kind": "seven_day", "limit_pct": 71, "cost_usd": 3.1, "age_s": 720},
        "agents": [{"kind": "claude", "repo": "r1", "session": "s1", "state": "active",
                    "age_s": 30, "subagents": 2},
                   {"kind": "pi", "repo": "r2", "session": "s2", "state": "idle", "age_s": 900}],
        "worker": {"label": "w", "pid": 42, "inbox": 1, "queue_running": 0},
        "proxy": {"up": True, "port": 8788, "version": "0.4.1"},
        "adapters": [],
    }
    out = snapshot.menubar(snap)
    lines = out.splitlines()
    assert lines[0] == "● 1 | color=#8E8E93" and lines[1] == "---"
    assert "insufficient sample (n=4)" in out
    assert "7d 71% · $3.10 · age 12m" in out
    assert "claude · r1 · active · 30s · s1 · 2 subagents" in out
    assert "idle (1)" in lines and "--pi · r2 · idle · 15m · s2" in lines
    assert "pid 42 · inbox 1 · running 0" in out
    assert "up :8788 v0.4.1" in out
    assert out.endswith("Refresh | refresh=true")


def test_fmt_age():
    assert [snapshot.fmt_age(s) for s in (None, 5, 61, 7200, 90000)] == ["?", "5s", "1m", "2h", "1d"]


# ---------------------------------------------------------------- fail-open + read-only

def test_collect_fails_open(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(snapshot, "measure_block", boom)
    monkeypatch.setattr(snapshot.agents_mod, "discover", boom)
    snap = _collect(tmp_path)
    assert snap["measure"] == {"error": "RuntimeError: kaput"}
    assert snap["agents"] == [{"error": "RuntimeError: kaput"}]
    assert snap["schema"] == 1
    snapshot.menubar(snap)                                      # still renders


def _listing(root: Path) -> dict:
    out = {}
    for dp, dn, fn in os.walk(root):
        for n in dn + fn:
            p = Path(dp) / n
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def test_snapshot_writes_nothing(tmp_path, monkeypatch, capsys):
    home = Path(os.environ["HOME"])                             # per-test sandbox (conftest)
    _telemetry(home / ".apex" / "telemetry.jsonl", _ok(40))
    proj = home / ".claude" / "projects" / "-Users-you-src-r"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text("{}\n")
    (home / ".apex-router" / "observe").mkdir(parents=True)
    _adapter(home / ".apex-router" / "adapters", "a.json", {"title": "A", "rows": ["x"], "ts": 1})
    monkeypatch.setenv("APEX_PORT", "9")                       # discard port: proxy down fast
    monkeypatch.setattr(snapshot.agents_mod, "_launchd_pid", lambda label: (None, "stub"))
    before = _listing(home)
    for flag in ("--json", "--menubar"):
        assert cli.main(["snapshot", flag]) == 0
    assert _listing(home) == before
    assert not (home / ".apex-router" / "pressure.json").exists()
    out = capsys.readouterr().out
    assert '"schema": 1' in out and "Refresh | refresh=true" in out


def test_main_catastrophic_failure_is_gray(monkeypatch, capsys):
    def boom(**k):
        raise RuntimeError("total")
    monkeypatch.setattr(snapshot, "collect", boom)
    assert snapshot.main(["--menubar"]) == 0
    assert capsys.readouterr().out.startswith("● | color=#8E8E93")
    assert snapshot.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["error"] == "RuntimeError: total"


# ---- review fixes: non-finite ts, /healthz injection, formatter failure, family dot ---------

def test_non_finite_adapter_ts_does_not_crash(tmp_path):
    d = tmp_path / "ad"
    _adapter(d, "inf.json", '{"title": "t", "rows": ["a"], "ts": -Infinity}')
    out = snapshot.adapters_block(d, NOW)
    assert out[0]["age_s"] is None and out[0]["stale"] is True
    assert "updated ? ago" in snapshot.menubar({"pressure": {}, "adapters": out})
    assert snapshot.fmt_age(float("inf")) == "?" and snapshot.fmt_age(float("nan")) == "?"


def test_proxy_port_from_healthz_is_never_interpolated_raw():
    line = snapshot._proxy_line({"up": True, "port": "1 | bash=/bin/sh terminal=false"})
    assert line == "up" and "bash=" not in line
    assert snapshot._proxy_line({"up": True, "port": 8788}) == "up :8788"


def test_formatter_failure_falls_back_to_gray(monkeypatch, capsys):
    monkeypatch.setattr(snapshot, "collect", lambda **k: {"pressure": {}})
    def boom(snap):
        raise OverflowError("bad field")
    monkeypatch.setattr(snapshot, "menubar", boom)
    assert snapshot.main(["--menubar"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("● | color=#8E8E93") and "OverflowError: bad field" in out


def test_family_level_lifts_the_dot():
    calm = {"level": "GREEN", "insufficient_sample": True}
    assert snapshot.dot({"pressure": dict(calm, families={"opus": "RED"})}) == "red"
    assert snapshot.dot({"pressure": dict(calm, families={"opus": "AMBER"})}) == "orange"
    assert snapshot.dot({"pressure": dict(calm, families={"opus": "GREEN"})}) == "gray"


def test_worker_line_unknown_queue_is_question_mark():
    assert snapshot._worker_line({"pid": 7, "inbox": None}) == "pid 7 · inbox ? · running ?"


def test_adapter_rows_disable_emoji(tmp_path):
    d = tmp_path / "ad"
    _adapter(d, "a.json", {"title": "T", "rows": [":x: row"], "ts": NOW})
    text = snapshot.menubar({"pressure": {}, "adapters": snapshot.adapters_block(d, NOW)})
    assert ":x: row | emojize=false" in text
