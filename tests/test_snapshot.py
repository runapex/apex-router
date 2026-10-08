"""Tests for apex_router.snapshot — the read-only menu-bar snapshot and its SwiftBar formatter."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from apex_router import agent_resources, cli, snapshot

NOW = 1_790_000_000.0


@pytest.fixture(autouse=True)
def _no_real_resource_sources(monkeypatch):
    """No real ps / lsof / ioreg / libproc / ollama in unit tests."""
    monkeypatch.setattr(snapshot.agent_resources, "run_cmd", lambda argv, timeout=2.0: "")
    monkeypatch.setattr(snapshot.agent_resources, "http_get", lambda url, timeout=1.0: b"{}")
    monkeypatch.setattr(snapshot.agent_resources, "rusage", lambda pid: None)


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


_PARAM_KEYS = {"size", "color", "font", "emojize", "symbolize", "tooltip", "refresh"}


def _param_line_ok(ln: str) -> bool:
    """A SwiftBar line with parameters: exactly one '|' and only our own key=value params."""
    import shlex
    if ln.count("|") != 1:
        return False
    try:
        params = shlex.split(ln.split(" | ", 1)[1])
    except (IndexError, ValueError):
        return False
    return all("=" in p and p.split("=", 1)[0] in _PARAM_KEYS for p in params)


def _user_lines_disable_emoji(lines, needles) -> bool:
    return all("emojize=false symbolize=false" in ln for ln in lines
               if any(n in ln for n in needles))


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
    assert snapshot._measure_line(m) == "7d 71% · age 15m"           # no $ in the widget; cost_usd stays in --json


def test_measure_camelcase_and_missing(tmp_path):
    d = tmp_path / "observe"
    d.mkdir()
    assert snapshot.measure_block(d, NOW)["missing"] is True
    (d / "a.jsonl").write_text(json.dumps(
        {"ev": "measure", "ts": NOW - 60, "limitKind": "five_hour", "limitPercent": 3,
         "costUsd": 0.004}) + "\n")
    m = snapshot.measure_block(d, NOW)
    assert (m["limit_kind"], m["limit_pct"], m["age_s"]) == ("five_hour", 3, 60.0)
    assert snapshot._measure_line(m) == "5h 3% · age 1m"


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
    assert all(_param_line_ok(ln) for ln in with_params), with_params
    assert "evil ¦ ba…/rm abcd1234" in out          # repo cut, never the 8-char session id
    assert "––T ¦ x | size=12" in out
    assert any(ln.startswith("––– | emojize=false symbolize=false") for ln in lines)
    assert "Refresh | refresh=true" not in lines              # SwiftBar's own menu has Refresh
    assert lines[-1] != "---"                                 # no dangling separator
    assert _user_lines_disable_emoji(lines, ["evil", "Bad¦Cause", "––T", "r ¦ refresh"])


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
    assert "7d 71% · age 12m" in out and "$" not in out
    assert any(ln.startswith("r1 s1 ") and " active " in ln and "font=Menlo" in ln for ln in lines)
    assert "idle (1)" in _plain(lines)
    assert "--pi:r2 s2                idle 15m | font=Menlo size=12 emojize=false symbolize=false" \
        f" color={snapshot.INK['text']}" in lines
    assert "worker up · queue 1 |" in out                  # running 0: not shown
    assert "proxy" not in _plain(out.splitlines())[-8:][0]    # a proxy that answers: no line
    assert "proxy up" not in out
    assert "errors 15m" not in out                            # zero errors: no line
    assert "Refresh" not in out


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


def _writes_home(monkeypatch):
    home = Path(os.environ["HOME"])                             # per-test sandbox (conftest)
    _telemetry(home / ".apex" / "telemetry.jsonl", _ok(40))
    proj = home / ".claude" / "projects" / "-Users-you-src-r"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text("{}\n")
    (home / ".apex-router" / "observe").mkdir(parents=True)
    _adapter(home / ".apex-router" / "adapters", "a.json", {"title": "A", "rows": ["x"], "ts": 1})
    monkeypatch.setenv("APEX_PORT", "9")                       # discard port: proxy down fast
    monkeypatch.setattr(snapshot.agents_mod, "_launchd_pid", lambda label: (None, "stub"))
    return home


def test_snapshot_writes_nothing(tmp_path, monkeypatch, capsys):
    # --json / --graph never write; --menubar writes only its history, and not with
    # --no-history or APEX_WIDGET_NO_HISTORY=1 (iteration 4 ruling).
    home = _writes_home(monkeypatch)
    before = _listing(home)
    for args in (["--json"], ["--graph"], ["--menubar", "--no-history"]):
        assert cli.main(["snapshot", *args]) == 0
    monkeypatch.setenv("APEX_WIDGET_NO_HISTORY", "1")
    assert cli.main(["snapshot", "--menubar"]) == 0
    assert _listing(home) == before
    assert not (home / ".apex-router" / "pressure.json").exists()
    out = capsys.readouterr().out
    assert '"schema": 1' in out and "System | size=12" in out


def test_menubar_writes_only_the_history_file(tmp_path, monkeypatch, capsys):
    home = _writes_home(monkeypatch)
    before = _listing(home)
    assert cli.main(["snapshot", "--menubar"]) == 0
    after = _listing(home)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    hist = os.path.join(".apex-router", "widget", "history.jsonl")
    lock = hist + ".lock"
    assert hist in changed
    assert changed <= {os.path.join(".apex-router", "widget"), hist, lock, ".apex-router"}, changed
    rows = [json.loads(x) for x in (home / hist).read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["v"] == 1 and isinstance(rows[0]["agents"], list)
    assert (home / lock).stat().st_size == 0                    # the lock file holds no data
    assert "System | size=12" in capsys.readouterr().out


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
    assert line == "proxy up" and "bash=" not in line
    assert snapshot._proxy_line({"up": True, "port": 8788}) == "proxy up :8788"


def test_formatter_failure_falls_back_to_gray(monkeypatch, capsys):
    monkeypatch.setattr(snapshot, "collect", lambda **k: {"pressure": {}})
    def boom(snap, **kw):                          # menubar(snap, history=, bin_path=)
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
    assert snapshot._worker_line({"pid": 7, "inbox": None}) == "worker up · queue ? · running ?"
    assert snapshot._worker_line({"pid": 7, "inbox": 0, "queue_running": 0}) == "worker up · queue 0"
    assert snapshot._worker_line({"pid": 7, "inbox": 2, "queue_running": 1}) == \
        "worker up · queue 2 · running 1"


def test_worker_skipped_by_deadline_is_not_reported_down():
    # A launchctl lookup the deadline skipped says nothing about the worker (it read
    # "not running" for a live worker on a loaded machine).
    w = {"label": "w", "error": "DeadlineSkip: deadline exceeded, source skipped"}
    assert snapshot._worker_line(w) == "worker not checked (deadline) · w"
    assert snapshot._worker_line({"label": "w", "error": "not loaded", "inbox": 0,
                                  "queue_running": 0}) == "worker down (not loaded) · w · queue 0"


def test_redundant_lines_are_dropped():
    base = {"pressure": {"level": "GREEN", "insufficient_sample": False, "n": 50, "window_min": 15,
                         "families": {"opus": "GREEN"}},
            "errors15m": {"n": 0, "by_cause": {}},
            "measure": {"limit_pct": None, "age_s": 90000.0, "ts": 1.0}}
    out = snapshot.menubar(base)
    assert "families:" not in out                    # same level as overall
    assert "errors 15m" not in out                   # zero
    assert "Limit" not in out and "age 1d" not in out   # a meter age without a meter
    fam = dict(base, pressure=dict(base["pressure"], families={"opus": "GREEN", "haiku": "AMBER"}))
    assert "families: haiku AMBER, opus GREEN" in snapshot.menubar(fam)
    err = dict(base, errors15m={"n": 2, "by_cause": {"429": 2}})
    assert "errors 15m: 2 (429 2)" in snapshot.menubar(err)
    lim = dict(base, measure={"limit_kind": "five_hour", "limit_pct": 3, "age_s": 60})
    assert "5h 3% · age 1m" in snapshot.menubar(lim)


def test_no_subagents_drops_the_duplicate_totals():
    # Without subagents Σ == main == the row: the submenu keeps only what the row lacks.
    a = {"kind": "pi", "repo": "r", "session": "s1", "session_id": "0123abcd-0000", "state": "active",
         "res": {"telemetry": {"requests": 4, "tokens_in": 1, "cache_read": 90, "cache_write": 9,
                               "tokens_out": 50, "p50_ttft_ms": 900, "models": {"m": 4}}}}
    sub = _plain(snapshot._agent_submenu(a, 2))
    assert not any(x.startswith("--Σ") for x in sub)
    assert sub[0] == "--cache 90% · ttft 0.9s"
    assert sub[-1] == "--model  m"
    # the collector's real zero-subagent shape: count 0 + a zero-filled totals dict (xval P2)
    zero = {"list": [], "more": 0, "hidden": None, "count": 0, "running": 0, "flagged": 0,
            "totals": agent_resources.merge_stats()}
    sub0 = _plain(snapshot._agent_submenu(dict(a, res=dict(a["res"], subagents=zero)), 2))
    assert not any(x.startswith("--Σ") for x in sub0) and sub0[0] == "--cache 90% · ttft 0.9s"
    with_subs = dict(a, res=dict(a["res"], subagents={"list": [], "more": 1, "count": 1}))
    assert _plain(snapshot._agent_submenu(with_subs, 2))[0].startswith("--Σ 60m  ")


def test_menubar_collect_has_a_hard_wall_clock_limit(monkeypatch, capsys):
    # The shared Deadline caps when a source starts, not a peer that trickles bytes (xval P1).
    monkeypatch.setattr(snapshot, "HARD_LIMIT_S", 1)
    monkeypatch.setattr(snapshot, "collect", lambda **k: time.sleep(5))
    t = time.monotonic()
    assert snapshot.main(["--menubar", "--no-history"]) == 0
    assert time.monotonic() - t < 3
    out = capsys.readouterr().out
    assert out.startswith("● | color=#8E8E93") and "HardLimit" in out


def test_hard_limit_is_not_swallowed_by_fail_open_sources(monkeypatch, capsys, tmp_path):
    # Every source fails open with `except Exception`; the limit must still end the collect
    # (xval pass 2: an Exception subclass was swallowed by _safe and the refresh ran on, green).
    monkeypatch.setattr(snapshot, "HARD_LIMIT_S", 1)
    monkeypatch.setenv("APEX_WIDGET_NO_HISTORY", "")
    after = []

    def stall(*a, **k):
        time.sleep(5)
    monkeypatch.setattr(snapshot.agent_resources, "collect", stall)
    monkeypatch.setattr(snapshot, "proxy_fn", None, raising=False)
    monkeypatch.setattr(snapshot.widget_history, "append", lambda cur: after.append(cur))
    t = time.monotonic()
    assert snapshot.main(["--menubar"]) == 0
    assert time.monotonic() - t < 3
    assert capsys.readouterr().out.startswith("● | color=#8E8E93")
    assert after == []                                   # no history on the timeout path


def test_gpu_line_does_not_say_unknown_beside_a_known_spark():
    s = {"gpu_util_pct": 7, "gpu_mem_mb": 30830.5, "loadavg": [8.0, 11.2, 9.9], "ollama": []}
    hist = [{"ts": 1.0, "agents": [], "system": {"gpu_util_pct": 5}}]
    plain = _plain(snapshot._system_lines(s, {}, hist))
    gpu = next(x for x in plain if x.startswith("gpu "))
    assert gpu.endswith("7% · mem 30.1GB · load 8.0")      # one line: spark, %, memory, load
    assert sum(x.startswith("gpu ") or "GPU" in x for x in plain) == 1
    skipped = _plain(snapshot._system_lines({"loadavg": [1.0], "ollama": None}, {}, hist))
    assert next(x for x in skipped if x.startswith("gpu ")).endswith("? · load 1.0")


def test_every_menu_line_is_coloured_or_clickable():
    # SwiftBar disables a line with neither an action nor a colour and AppKit greys it out.
    out = snapshot.menubar(_res_snap())
    for ln in out.splitlines()[1:]:
        if ln.lstrip("-") == "" or ln.lstrip("-").startswith("---"):
            continue
        params = ln.partition(" | ")[2]
        assert "color=" in params or "bash=" in params or "href=" in params, ln


def test_ink_reads_parameter_keys_not_tooltip_text():
    # xval: a subagent type "custom color=red" put "color=" in a tooltip; a substring check took it
    # for a colour and left the row grey.
    ln = 'x | font=Menlo tooltip="type custom color=red href=y"'
    assert snapshot._ink(ln).endswith(f" color={snapshot.INK['text']}")
    assert snapshot._ink("a | color=#000") == "a | color=#000"
    assert snapshot._ink("a | bash=/x refresh=false") == "a | bash=/x refresh=false"
    assert snapshot._ink("bare |") == f"bare | color={snapshot.INK['text']}"
    assert snapshot._ink("plain") == f"plain | color={snapshot.INK['text']}"
    assert snapshot._ink("-----") == "-----"
    assert "color=" in snapshot.menubar_error("boom").splitlines()[2]


def test_proxy_line_only_when_not_up():
    up = dict(_res_snap(), proxy={"up": True, "port": 8788})
    assert "proxy" not in " ".join(_plain(snapshot.menubar(up).splitlines()))
    down = dict(_res_snap(), proxy={"up": False})
    assert any(x.startswith("proxy down") for x in _plain(snapshot.menubar(down).splitlines()))


def test_net_line_from_interface_counters():
    t0 = 1_791_390_000.0
    hist = [{"ts": t0 + 60 * i, "agents": [],
             "system": {"net_rx": 1000 + 60_000 * i, "net_tx": 500 + 6_000 * i, "net_if": "en0"}}
            for i in range(4)]
    line = snapshot.net_spark_line(hist)
    plain = line.split(" | ")[0]
    assert plain.endswith("↓1000B/s ↑100B/s · 3m ↓176K ↑18K"), plain
    assert "color=" + snapshot.INK["blue"] in line
    # a counter reset (reboot) or an interface change gives no rate, never a negative one
    reset = hist + [{"ts": t0 + 240, "agents": [], "system": {"net_rx": 5, "net_tx": 5,
                                                              "net_if": "en0"}}]
    assert len(snapshot.widget_history.net_rates(reset)) == 3
    assert "measuring" in snapshot.net_spark_line(hist[:1])
    # the newest sample gave no rate: say so instead of repeating the previous one (xval)
    assert "counters restarted" in snapshot.net_spark_line(reset)
    # the hour total counts only the part of an interval inside the hour (xval)
    long = [{"ts": t0, "agents": [], "system": {"net_rx": 0, "net_tx": 0, "net_if": "en0"}},
            {"ts": t0 + 300, "agents": [], "system": {"net_rx": 300_000, "net_tx": 0, "net_if": "en0"}}]
    long += [{"ts": t0 + 300 + 600 * i, "agents": [],
              "system": {"net_rx": 300_000 + 600_000 * i, "net_tx": 0, "net_if": "en0"}}
             for i in range(1, 7)]                 # 1000 B/s for 3900 s; 10-min gaps allowed
    plain = snapshot.net_spark_line(long).split(" | ")[0]
    assert plain.endswith("1h ↓3.4M ↑0B"), plain        # 3,600,000 B, not 3,900,000


def test_adapter_rows_disable_emoji(tmp_path):
    d = tmp_path / "ad"
    _adapter(d, "a.json", {"title": "T", "rows": [":x: row"], "ts": NOW})
    text = snapshot.menubar({"pressure": {}, "adapters": snapshot.adapters_block(d, NOW)})
    assert ":x: row | emojize=false" in text


# ---- agent resources: submenu, System section, --graph ---------------------------------------

def _res_snap(name="pytest", desc="find x", model="claude-opus-5-5"):
    return {
        "pressure": {"level": "GREEN", "insufficient_sample": False, "n": 50, "window_min": 15},
        "agents": [{"kind": "claude", "repo": "r", "session": "s1", "state": "active", "age_s": 5,
                    "res": {"status": "busy",
                            "tree": {"pid": 9, "alive": True, "footprint_mb": 412.0, "rss_mb": 300.0,
                                     "cpu_pct": 14.0, "read_mb": 745.0, "write_mb": 569.0,
                                     "top": [{"pid": 10, "name": name, "rss_mb": 30.0, "cpu_pct": 20.0}]},
                            "telemetry": {"requests": 4, "tokens_out": 2500, "errors": 1,
                                          "p50_ttft_ms": 1200, "models": {model: 4}},
                            "subagents": {"list": [{"id": "a1", "type": "Explore", "description": desc,
                                                    "state": "active",
                                                    "telemetry": {"requests": 2, "tokens_out": 10,
                                                                  "errors": 0, "models": {model: 2}}}],
                                          "more": 3}}},
                   {"kind": "pi", "repo": "q", "session": "s2", "state": "active", "age_s": 9,
                    "res": {"unattributed": {"ambiguous": 2, "sessions": 1}}}],
        "worker": {"label": "w", "pid": 42, "inbox": 0, "queue_running": 0,
                   "res": {"tree": {"alive": True, "rss_mb": 8.0, "cpu_pct": 0.0},
                           "ollama_tree": {"alive": True, "footprint_mb": 16.0, "cpu_pct": 1.0},
                           "ollama_models": [{"name": "qwen3:8b", "vram_mb": 5120.0}]}},
        "system": {"gpu_util_pct": 37, "gpu_mem_mb": 1510.0, "loadavg": [3.29, 3.11, 2.69],
                   "agents_rss_mb": 1300.0, "agents_procs": 10, "ollama": [],
                   "errors": {"lsof": "TimeoutExpired"}},
    }


def _plain(lines):
    """Menu lines without their SwiftBar parameters."""
    return [ln.split(" | ", 1)[0] for ln in lines]


def test_menubar_agent_metrics_submenu_and_system():
    lines = snapshot.menubar(_res_snap()).splitlines()
    plain = _plain(lines)
    row = next(ln for ln in lines if ln.startswith("r s1 "))
    # footprint (not rss), cpu, Claude's own status; lifetime io only in the tooltip
    assert row.split(" | ")[0] == ("r s1                   busy    412MB   14%  "
                                   "6 req · out 2.5k ⚠")
    assert "font=Menlo size=12 emojize=false symbolize=false" in row
    assert 'tooltip="pid 9 · io life 745/569MB · rss 300MB · cpu = ps average"' in row
    i = plain.index(row.split(" | ")[0])
    sub = plain[i + 1:plain.index("pi:q s2                active      —     —  0 req · out 0")]
    assert sub == ["--Σ 60m  6 req · out 2.5k · 1 err 17%",
                   "--main  4 req · out 2.5k · 1 err 25% · ttft 1.2s",
                   "--Subagents 60m · 4 · 0 running",
                   "--▶ find x                  2 req · out 10",
                   "--… 3 more (0 req, out 0)",
                   "--Processes · 1 · ?", "----pytest                       ?  20%",
                   "--model  claude-opus-5-5"]                   # one model: a line
    assert "--process not attributed (2 candidates share this cwd)" in plain
    assert sum("ollama" in ln for ln in plain) == 1                 # once, in System
    s = plain.index("System")
    assert plain[s + 1:s + 7] == ["proxy down", "worker up · queue 0",
                                  "net  –                               measuring (needs two refreshes)",
                                  "gpu  –                               37% · mem 1.5GB · load 3.3",
                                  "ollama · no model loaded", "not read this refresh: lsof"]
    wl = next(ln for ln in lines if ln.startswith("worker up"))
    assert "pid 42 · ?MB · 0%" in wl                                # memory/cpu: tooltip only
    ol = next(ln for ln in lines if ln.startswith("ollama"))
    assert "server + runners 16MB · 1%" in ol
    assert lines[0] == "● 2 ⚠ | color=#34C759"                     # main-thread errors flag


def test_menubar_escapes_process_names_and_descriptions():
    out = snapshot.menubar(_res_snap(name="--evil | bash=/bin/rm", desc="x | terminal=true\nnl",
                                     model="m|href=http://x"))
    lines = out.splitlines()
    with_params = [ln for ln in lines if "|" in ln]
    assert all(_param_line_ok(ln) for ln in with_params), with_params
    plain = _plain(lines)
    assert "----––evil ¦ bash=/bin/rm        ?  20%" in plain
    assert "--▶ x ¦ terminal=true nl    2 req · out 10" in plain
    assert "--model  m¦href=http://x" in plain
    assert _user_lines_disable_emoji(lines, ["evil", "terminal=true", "href"])


def test_menubar_system_missing_or_error():
    out = snapshot.menubar({"pressure": {}, "system": {"error": "RuntimeError: a|b"}})
    assert "unavailable · RuntimeError: a¦b" in out
    assert "System | size=12" in snapshot.menubar({"pressure": {}})


def test_collect_resources_failure_is_contained(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("res down")
    snap = _collect(tmp_path, resources_fn=boom)
    assert snap["system"] == {"error": "RuntimeError: res down"}
    assert snap["graph"] == {"nodes": [], "edges": []}
    assert "unavailable · RuntimeError: res down" in snapshot.menubar(snap)


def test_collect_attaches_resources(tmp_path):
    seen = {}

    def fake(agents, **kw):
        seen.update(kw)
        return {"agents": [dict(a, res={"status": "busy"}) for a in agents],
                "system": {"gpu_util_pct": 1}, "graph": {"nodes": [1], "edges": []},
                "worker": {"tree": {}}}
    proj = tmp_path / ".claude" / "projects" / "-Users-you-src-r"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text("{}\n")
    os.utime(proj / "s.jsonl", (NOW - 5, NOW - 5))
    snap = _collect(tmp_path, resources_fn=fake)
    assert snap["agents"][0]["res"] == {"status": "busy"}
    assert snap["system"] == {"gpu_util_pct": 1} and snap["graph"]["nodes"] == [1]
    assert snap["worker"]["res"] == {"tree": {}} and seen["worker_pid"] is None
    assert seen["now"] == NOW


def test_main_graph_prints_tree(monkeypatch, capsys):
    monkeypatch.setattr(snapshot, "collect", lambda **k: {"graph": {
        "nodes": [{"id": "s", "kind": "session", "label": "claude · r · s1", "state": "active"}],
        "edges": []}})
    assert snapshot.main(["--graph"]) == 0
    assert capsys.readouterr().out.strip() == "claude · r · s1 · active"
    monkeypatch.setattr(snapshot, "collect", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert snapshot.main(["--graph"]) == 0
    assert "snapshot error: RuntimeError: x" in capsys.readouterr().out


# ---- iteration 2: caps, totals, dedupe, flags, deadline ---------------------------------------

def _sub(i, state="done", req=1, out=100, errors=0, flags=()):
    return {"id": f"a{i}", "type": "Explore", "description": f"task {i}", "depth": 1,
            "age_s": 600.0, "run_s": 300.0, "state": state, "flags": list(flags),
            "telemetry": {"requests": req, "tokens_out": out, "errors": errors, "tokens_in": 1,
                          "cache_read": 99, "cache_write": 0, "models": {"claude-opus-5-5": req}}}


def _session_agent(i, n_subs=25, state="active", status="busy", fp=100.0, flags=False):
    subs = [_sub(j, flags=["errors"] if flags and j == 0 else []) for j in range(n_subs)]
    from apex_router import agent_resources as ar
    shown, rest = subs[:ar.SUBAGENTS_MAX], subs[ar.SUBAGENTS_MAX:]
    return {"kind": "claude", "repo": f"repo{i}", "session": f"s{i:07d}", "session_id": f"sid{i}",
            "state": state, "age_s": 30.0 if state == "active" else 2040.0,
            "res": {"status": status,
                    "tree": {"pid": 1000 + i, "alive": True, "footprint_mb": fp, "rss_mb": fp,
                             "cpu_pct": 1.0, "read_mbs": 0.0, "write_mbs": 0.0, "top": []},
                    "telemetry": {"requests": 2, "tokens_out": 10 * i, "errors": 0,
                                  "tokens_in": 1, "cache_read": 10, "cache_write": 0,
                                  "models": {"claude-opus-5-5": 2}},
                    "subagents": {"list": shown, "more": len(rest),
                                  "hidden": ar.merge_stats(*[s["telemetry"] for s in rest]),
                                  "totals": ar.merge_stats(*[s["telemetry"] for s in subs]),
                                  "count": len(subs), "running": 0, "quiet": 0,
                                  "flagged": sum(1 for s in subs if s["flags"])}}}


def test_menubar_is_bounded_with_more_totals():
    agents = [_session_agent(i) for i in range(200)]
    out = snapshot.menubar({"pressure": {}, "agents": agents})
    lines = out.splitlines()
    assert len(lines) <= snapshot.MENU_LINES_MAX + 5, len(lines)
    rows = [ln for ln in lines if ln.startswith("repo")]
    assert len(rows) == snapshot.ACTIVE_MAX
    assert rows[0].startswith("repo199 s0000199")              # ranked by output tokens
    hidden = agents[:200 - snapshot.ACTIVE_MAX]
    req = sum(2 + 25 for _ in hidden)
    out_tok = sum(10 * int(a["session"][1:]) + 25 * 100 for a in hidden)
    from apex_router import agent_resources as ar
    assert f"… {len(hidden)} more active ({req} req, out {ar._k(out_tok)})" in _plain(lines)


def test_menubar_session_totals_include_every_subagent():
    a = _session_agent(3, n_subs=12)
    lines = _plain(snapshot.menubar({"pressure": {}, "agents": [a]}).splitlines())
    sigma = next(ln for ln in lines if ln.startswith("--Σ 60m"))
    assert sigma.startswith("--Σ 60m  14 req · in 13 · cached 1.2k · out 1.2k")   # main + 12 subs
    assert "--Subagents 60m · 12 · 0 running" in lines
    assert sum(1 for ln in lines if ln.startswith("--✓ task")) == 8
    assert "--… 4 more (4 req, out 400)" in lines
    row = next(ln for ln in lines if ln.startswith("repo3"))
    assert "14 req · out 1.2k" in row


def test_menubar_claude_status_replaces_mtime_state():
    active = _session_agent(1, n_subs=0, status="busy")
    idle = _session_agent(2, n_subs=0, state="idle", status="idle", fp=342.0)
    lines = _plain(snapshot.menubar({"pressure": {}, "agents": [active, idle]}).splitlines())
    row = next(ln for ln in lines if ln.startswith("repo1"))
    assert " busy " in row and " active " not in row and " 0s" not in row
    assert "idle (1) · 342MB held" in lines
    assert "--repo2 s0000002          idle 34m   342MB" in lines
    assert not any("idle · idle" in ln or "0 err" in ln for ln in lines)


def test_bar_warns_on_a_flagged_agent_but_keeps_the_pressure_colour():
    p = {"level": "GREEN", "insufficient_sample": False}
    calm = snapshot.menubar({"pressure": p, "agents": [_session_agent(1)]})
    assert calm.splitlines()[0] == "● 1 | color=#34C759"
    hot = snapshot.menubar({"pressure": p, "agents": [_session_agent(1, flags=True)]})
    assert hot.splitlines()[0] == "● 1 ⚠ | color=#34C759"
    assert any(ln.startswith("--⚠ task 0") for ln in hot.splitlines())


def test_agents_header_tokens_cache_and_no_dollars():
    snap = {"pressure": {}, "agents": [_session_agent(1)],
            "system": {"agents_footprint_mb": 1228.8, "agents_cpu_pct": 18.0,
                       "rate_window_s": 0.25,
                       "traffic_60m": {"requests": 74, "tokens_out": 120_000, "tokens_in": 10,
                                       "cache_read": 980, "cache_write": 10}}}
    out = snapshot.menubar(snap)
    hdr = next(ln for ln in out.splitlines() if ln.startswith("Agents ·"))
    assert hdr.startswith("Agents · 1 active · 0 idle · 1.2GB · cpu 18% · 74 req/h · "
                          "out 120.0k/h · cache 98% | size=12")
    assert "$" not in out


def test_ollama_shown_once_with_unload_time():
    snap = {"pressure": {}, "system": {"ollama": [{"name": "Ornith-9B Q4_K_M", "vram_mb": 6553.6,
                                                   "unloads_in_s": 180}]},
            "worker": {"label": "w", "pid": 7, "inbox": 0, "queue_running": 0,
                       "res": {"ollama_models": [{"name": "Ornith-9B Q4_K_M", "vram_mb": 6553.6}]}}}
    lines = _plain(snapshot.menubar(snap).splitlines())
    # one line: the model's weights are in the runner, so no separate server-footprint line
    assert [ln for ln in lines if "Ornith" in ln] == ["ollama Ornith-9B Q4_K_M 6.4GB 3m"]


def test_snapshot_deadline_skips_proxy(tmp_path):
    from apex_router import agent_resources as ar
    clock = iter([0.0] + [99.0] * 100)
    dl = ar.Deadline(1.5, clock=lambda: next(clock))
    snap = snapshot.collect(home=tmp_path, telemetry=tmp_path / "t.jsonl",
                            observe_dir=tmp_path / "o", adapters=tmp_path / "a", now=NOW,
                            worker_fn=_worker, deadline=dl)
    assert snap["proxy"] == {"up": False, "skipped": True, "error": "skipped: deadline"}
    assert "not checked (deadline)" in snapshot.menubar(snap)


# ---- iteration 3: Claude status, context table, error rule, width, controls, speed ------------

def _status_agent(i, state, status, out=100):
    a = _session_agent(i, n_subs=0, state=state, status=status)
    a["res"]["telemetry"]["tokens_out"] = out
    return a


def test_active_idle_comes_from_claude_status_not_log_mtime():
    ghost = _status_agent(1, "active", "idle")        # log touched by something else, Claude idle
    quiet = _status_agent(2, "idle", "busy")          # busy but its log is older than 5 min
    nostatus = _status_agent(3, "active", None)       # no status: the mtime state decides
    snap = {"pressure": {}, "agents": [ghost, quiet, nostatus]}
    out = snapshot.menubar(snap)
    lines = _plain(out.splitlines())
    assert lines[0] == "● 2"
    assert next(ln for ln in lines if ln.startswith("Agents ·")).startswith(
        "Agents · 2 active · 1 idle")
    assert any(ln.startswith("idle (1)") for ln in lines)
    assert any(ln.startswith("--repo1 s0000001") for ln in lines)          # folded as idle
    assert any(ln.startswith("repo2 s0000002") for ln in lines)            # top level: active
    assert any(ln.startswith("repo3 s0000003") for ln in lines)


def test_error_flag_needs_a_recent_error_or_a_5pct_rate():
    def agent(errors, requests, errors_5m):
        a = _session_agent(1, n_subs=0)
        a["res"]["telemetry"].update(errors=errors, requests=requests, errors_5m=errors_5m)
        return a
    p = {"level": "GREEN", "insufficient_sample": False}
    old_one = snapshot.menubar({"pressure": p, "agents": [agent(1, 100, 0)]})
    assert old_one.splitlines()[0] == "● 1 | color=#34C759"                # 1% and 40 min old
    assert "⚠" not in _plain(old_one.splitlines())[12]
    recent = snapshot.menubar({"pressure": p, "agents": [agent(1, 100, 1)]})
    assert recent.splitlines()[0] == "● 1 ⚠ | color=#34C759"
    rate = snapshot.menubar({"pressure": p, "agents": [agent(5, 100, 0)]})
    assert rate.splitlines()[0] == "● 1 ⚠ | color=#34C759"
    tiny = snapshot.menubar({"pressure": p, "agents": [agent(1, 200, 0)]})
    assert "1 err <1%" in tiny and "1 err 0%" not in tiny


def test_session_row_label_keeps_the_session_id_and_shows_rate_and_last():
    a = _session_agent(1, n_subs=0)
    a["repo"] = "a-very-long-repository-name/worktree"
    a["res"]["telemetry"].update(req_5m=16, last_ts=NOW - 42, ctx_tokens=283_000,
                                 ctx_window=1_000_000, ctx_pct=28)
    lines = _plain(snapshot.menubar({"pressure": {}, "agents": [a], "ts": NOW}).splitlines())
    row = next(ln for ln in lines if "s0000001" in ln and not ln.startswith("--"))
    assert row.startswith("a-v…/worktree s0000001 busy")                  # 22 wide, id whole
    assert "ctx 283k/1M 28% · last 42s · r5 3.2/min" in row


def test_processes_header_adds_up_to_the_listed_rows():
    a = _session_agent(1, n_subs=0, fp=700.0)
    a["res"]["tree"].update(procs=4, top=[
        {"pid": 11, "name": "a", "footprint_mb": 292.0, "cpu_pct": 0.0},
        {"pid": 12, "name": "b", "footprint_mb": 2.0, "cpu_pct": 0.0},
        {"pid": 13, "name": "c", "footprint_mb": 15.0, "cpu_pct": 0.0}])
    lines = snapshot.menubar({"pressure": {}, "agents": [a]}).splitlines()
    hdr = next(ln for ln in lines if ln.startswith("--Processes"))
    assert hdr.split(" | ")[0] == "--Processes · 3 · 309MB"                   # 292 + 2 + 15
    assert "4 procs · 700MB" in hdr                                          # whole tree: tooltip
    assert _param_line_ok(hdr)


def test_subagent_row_format_and_tooltip():
    sa = {"id": "x1", "type": "general-purpose", "description": "RSI iter 3", "depth": 1,
          "age_s": 2.0, "last_s": 2.0, "run_s": 540.0, "state": "running", "flags": [],
          "telemetry": {"requests": 36, "tokens_out": 57_800, "tokens_in": 24_600,
                        "cache_read": 4_700_000, "cache_write": 177_100, "errors": 0,
                        "ctx_tokens": 184_000, "ctx_window": 1_000_000, "ctx_pct": 18}}
    row = snapshot._sub_row(sa, "--")
    text, params = row.split(" | ", 1)
    assert text == ("--▶ RSI iter 3              36 req · out 57.8k · cache 96% · "
                    "ctx 184k/1M 18% · last 2s")
    assert "in 24.6k · cached 4.7M · write 177.1k · out 57.8k" in params
    assert "run 9m" in params


def test_control_characters_never_reach_a_line():
    assert snapshot.esc("a\x1b[31mb\x7fc\x9bd\x00e") == "a[31mbcde"
    assert snapshot.esc("x\ty\r\nz") == "x y z"
    snap = _res_snap(desc="\x1b]0;pwn\x07evil \x1b[2J\x9b31m desc", name="p\x1bq")
    out = snapshot.menubar(snap)
    assert not any(ord(c) < 0x20 and c != "\n" or 0x7f <= ord(c) <= 0x9f for c in out)
    assert "]0;pwnevil [2J31m desc" in out
    from apex_router import agent_resources as ar
    a = snap["agents"][0]
    g = ar.graph_text(ar.build_graph([a], now=NOW))
    assert not any(ord(c) < 0x20 and c != "\n" or 0x7f <= ord(c) <= 0x9f for c in g)


def _big_home(root: Path, n_proj=30, per_proj=10, n_subs=40, rows_per=12):
    """300 sessions x 40 subagents, long names, control characters, large token counts."""
    tel_rows = []
    k = 0
    for pi in range(n_proj):
        proj = root / ".claude" / "projects" / (
            f"-Users-you-src-a-very-long-repository-name-{pi:03d}--claude-worktrees-wt{pi}")
        for _ in range(per_proj):
            sid = f"{k:08x}-0000-4000-8000-000000000000"
            d = proj / sid / "subagents"
            d.mkdir(parents=True)
            f = proj / f"{sid}.jsonl"
            f.write_text("{}\n")
            os.utime(f, (NOW - (30 if k % 10 == 0 else 600 + k),) * 2)
            for j in range(n_subs):
                aid = f"a{k:04d}{j:03d}"
                log = d / f"agent-{aid}.jsonl"
                log.write_text("{}\n")
                os.utime(log, (NOW - 5 - j * 30,) * 2)
                if j < 10:
                    (d / f"agent-{aid}.meta.json").write_text(json.dumps(
                        {"agentType": "general-purpose", "spawnDepth": 1,
                         "description": "\x1b[31mRSI \x1b]0;t\x07 a long task description " * 3}))
            for r in range(rows_per):
                tel_rows.append(_row(NOW - 10 - r * 20, session_id=sid,
                                     agent_id=None if r % 3 == 0 else f"a{k:04d}{r:03d}",
                                     model=("claude-haiku-4-5-20251001" if r % 4 == 2
                                            else "claude-opus-5-5"),
                                     is_error=r == 5, cause="ReadError" if r == 5 else None,
                                     tokens_in=99_999, cache_read_tokens=999_990,
                                     cache_write_tokens=99_900, tokens_out=99_999))
            k += 1
    return _telemetry(root / ".apex" / "telemetry.jsonl", tel_rows)


@pytest.fixture(scope="module")
def big_home(tmp_path_factory):
    root = tmp_path_factory.mktemp("bighome")
    _big_home(root)
    return root


def _big_collect(home, monkeypatch=None):
    return snapshot.collect(home=home, telemetry=home / ".apex" / "telemetry.jsonl",
                            observe_dir=home / "o", adapters=home / "a", now=NOW,
                            proxy_fn=_down, worker_fn=_worker)


def test_large_home_is_fast_and_scans_only_shown_sessions(big_home, monkeypatch):
    import time as _time
    calls = []
    real = snapshot.agents_mod.decode_slug
    monkeypatch.setattr(snapshot.agents_mod, "decode_slug",
                        lambda slug, home=None: calls.append(slug) or real(slug, home))
    t0 = _time.perf_counter()
    snap = _big_collect(big_home)
    out = snapshot.menubar(snap)
    graph = snapshot.agent_resources.graph_text(snap["graph"])
    elapsed = _time.perf_counter() - t0
    assert len([a for a in snap["agents"] if a.get("kind") == "claude"]) == 300
    assert elapsed < 1.0, elapsed                                   # ~0.15 s measured
    assert snap["system"]["subagent_scans"] <= snapshot.ACTIVE_MAX
    assert len(calls) == len(set(calls)) == 30                       # once per project dir
    assert out and graph


def test_large_home_line_widths(big_home):
    snap = _big_collect(big_home)
    out = snapshot.menubar(snap)
    widths = [len(snapshot.visible(ln)) for ln in out.splitlines()]
    assert max(widths) <= snapshot.MENU_WIDTH, max(widths)
    g = snapshot.agent_resources.graph_text(snap["graph"])
    assert max(len(ln) for ln in g.splitlines()) <= 120
    assert not any(ord(c) < 0x20 and c != "\n" or 0x7f <= ord(c) <= 0x9f for c in out + g)
    # every shown session row keeps its full 8-char id
    sids = {a["session"] for a in snap["agents"]}
    rows = [ln for ln in out.splitlines() if "font=Menlo" in ln and not ln.startswith("-")
            and not ln.startswith(("net ", "gpu "))]                   # System spark rows
    assert rows and all(any(s in r for s in sids) for r in rows)


def test_extreme_values_stay_within_the_width():
    a = _session_agent(1, n_subs=12, fp=123_456.0)
    a["repo"] = "x" * 80
    a["res"]["tree"].update(cpu_pct=1234.5, procs=999)
    a["res"]["telemetry"].update(requests=99_999, tokens_out=987_654_321, errors=999,
                                 errors_5m=9, req_5m=9_999, last_ts=NOW - 86_000,
                                 ctx_tokens=199_999, ctx_window=200_000, ctx_pct=100,
                                 tokens_in=987_654_321, cache_read=987_654_321,
                                 cache_write=987_654_321, p50_ttft_ms=123_456)
    for s in a["res"]["subagents"]["list"]:
        s["description"] = "d" * 300
        s["flags"] = ["errors", "ctx", "long"]
        s["telemetry"].update(requests=99_999, tokens_out=987_654_321, errors=99_999,
                              ctx_tokens=999_999, ctx_window=200_000, ctx_pct=500)
    out = snapshot.menubar({"pressure": {}, "agents": [a] * 3, "ts": NOW})
    assert max(len(snapshot.visible(ln)) for ln in out.splitlines()) <= snapshot.MENU_WIDTH
    row = next(ln for ln in out.splitlines() if "s0000001" in ln and not ln.startswith("-"))
    assert snapshot.visible(row).endswith(" ⚠")                         # the flag is never cut
    from apex_router import agent_resources as ar
    g = ar.graph_text(ar.build_graph([a], now=NOW))
    assert max(len(ln) for ln in g.splitlines()) <= 120


def test_idle_row_keeps_the_session_id():
    a = _session_agent(2, n_subs=0, state="idle", status="idle")
    a["repo"] = "a-really-long-repo-name-without-slash"
    lines = _plain(snapshot.menubar({"pressure": {}, "agents": [a]}).splitlines())
    assert any(ln.startswith("--a-really-lon… s0000002  idle") for ln in lines), lines


def test_active_row_shows_io_only_when_disk_is_busy():
    def row(r, w):
        a = {"kind": "claude", "repo": "r", "session": "abcd1234", "state": "active",
             "res": {"status": "busy", "tree": {"alive": True, "footprint_mb": 100.0, "cpu_pct": 1.0,
                                                "read_mbs": r, "write_mbs": w}}}
        return snapshot._agent_row(a, now=NOW).split(" | ")[0]   # visible text, not the tooltip
    assert "io 2.5MB/s" in row(2.0, 0.5)
    assert "io " not in row(0.1, 0.1)


def test_ollama_line_names_its_clients():
    s = {"ollama": [{"name": "qwen3.8:27b-mlx", "vram_mb": 26800.0, "pinned": True}],
         "ollama_clients": [{"pid": 55074, "name": "apex-router labels", "worker": False},
                            {"pid": 12369, "name": "python", "worker": True}]}
    (line,) = snapshot._ollama_lines(s, {})
    assert line.split(" | ")[0] == "ollama qwen3.8:27b-mlx 26.2GB pinned ← apex-router labels, worker"
    assert "pid 55074" in line and snapshot.INK["violet"] in line
    idle = dict(s, ollama_clients=[])
    assert "←" not in snapshot._ollama_lines(idle, {})[0]


# ---------------------------------------------------------------- 0.4.2: waiting? + quiet pi

def _waiting_snap(waiting_s, flags):
    snap = _res_snap()
    a = snap["agents"][0]
    a["res"]["telemetry"]["errors"] = 0
    a["res"]["subagents"] = {"count": 1, "flagged": 1 if flags else 0, "list": [
        {"id": "a1234abcd", "type": "Explore", "description": "find x", "state": "done",
         "age_s": waiting_s, "last_s": waiting_s, "run_s": 60.0, "waiting": True,
         "waiting_s": waiting_s, "flags": flags, "telemetry": {"requests": 2, "tokens_out": 10}}]}
    return snap


def test_menubar_waiting_subagent_has_its_own_symbol_and_flags_after_ten_minutes():
    lines = snapshot.menubar(_waiting_snap(180.0, [])).splitlines()
    sub = next(ln for ln in lines if "find x" in ln)
    assert sub.startswith(f"--{snapshot.WAITING_SYM} find x") and "waiting? its session" in sub
    assert "run 1m · waiting? 3m" in sub
    row = next(ln for ln in lines if ln.startswith("r s1 "))
    assert not row.split(" | ")[0].endswith("⚠")                # < 10 min: not a ⚠ reason
    lines = snapshot.menubar(_waiting_snap(900.0, ["waiting"])).splitlines()
    sub = next(ln for ln in lines if "find x" in ln)
    assert sub.startswith(f"--{snapshot.WAITING_SYM} find x")    # still its own symbol
    assert "flagged: quiet > 10 min while its session is busy" in sub and "heuristic" in sub
    row = next(ln for ln in lines if ln.startswith("r s1 "))
    assert row.split(" | ")[0].endswith("⚠") and lines[0].startswith("● 2 ⚠")
    both = _waiting_snap(900.0, ["errors", "waiting"])
    sub = next(ln for ln in snapshot.menubar(both).splitlines() if "find x" in ln)
    assert sub.startswith("--⚠ find x")                         # another flag: the plain ⚠


def test_menubar_lists_quiet_pi_processes_under_idle_and_counts_them():
    snap = _res_snap()
    snap["system"]["agents_footprint_mb"] = 2048.0
    snap["system"]["quiet_procs"] = [
        {"pid": 300, "kind": "pi", "cwd_name": "graphs|x", "uptime_s": 3 * 86400,
         "footprint_mb": 159.0, "rss_mb": 5.0, "procs": 1},
        {"pid": 301, "kind": "codex", "cwd_name": None, "uptime_s": 120,
         "footprint_mb": None, "rss_mb": 3.0}]
    lines = snapshot.menubar(snap).splitlines()
    plain = _plain(lines)
    hdr = next(ln for ln in plain if ln.startswith("Agents ·"))
    assert "· 2 idle ·" in hdr and "2.0GB" in hdr                 # quiet procs count as idle
    assert "idle (2) · 162MB held" in plain
    assert "--pi · graphs¦x · quiet · up 3d · 159MB" in plain      # esc()'d cwd name
    assert "--codex · ? · quiet · up 2m · 3MB" in plain           # no cwd, rss fallback
    row = next(ln for ln in lines if ln.startswith("--pi · "))
    assert "emojize=false symbolize=false" in row and "bash=" not in row
    assert "pid 300" in row


def test_menubar_quiet_procs_share_the_idle_cap():
    snap = _res_snap()
    snap["system"]["quiet_procs"] = [{"pid": 300 + i, "kind": "pi", "cwd_name": f"p{i}",
                                      "uptime_s": 60, "footprint_mb": 1.0}
                                     for i in range(snapshot.IDLE_SHOWN_MAX + 3)]
    plain = _plain(snapshot.menubar(snap).splitlines())
    assert sum(1 for ln in plain if ln.startswith("--pi · ")) == snapshot.IDLE_SHOWN_MAX
    assert "--… 3 more" in plain


def test_collect_enables_the_quiet_process_lookup(tmp_path):
    seen = {}

    def fake(agents, **kw):
        seen.update(kw)
        return {"agents": agents, "system": {}, "graph": {"nodes": [], "edges": []}}
    _collect(tmp_path, resources_fn=fake)
    assert seen["quiet"] is True
