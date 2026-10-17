import json
import time

import pytest

from apex_router import agents, route_log
from apex_router.mesh import datasets


@pytest.fixture
def env(tmp_path, monkeypatch):
    tel = tmp_path / "telemetry.jsonl"
    log = tmp_path / "route_log.jsonl"
    home = tmp_path / "home"
    queue = tmp_path / "queue"
    home.mkdir()
    monkeypatch.setenv("APEX_TELEMETRY", str(tel))
    monkeypatch.setenv("APEX_ROUTER_LOG", str(log))
    monkeypatch.setenv("APEX_ROUTER_HOME", str(home))
    monkeypatch.setenv("APEX_ORNITH_QUEUE", str(queue))
    monkeypatch.setattr(agents, "_launchd_pid", lambda label, timeout=2.0: (4242, None))
    return {"tel": tel, "log": log, "home": home, "queue": queue}


def _row(ts, cause=None, is_error=False):
    return {"ts": ts, "client": "claude-code", "session_id": "s1", "agent_id": None,
            "model_requested": "claude-opus-5-5", "is_error": is_error, "error_cause": cause,
            "error_detail": None, "ttft_ms": 500.0}


def _ok(names):
    out = datasets.read(names, datasets.ALLOWLIST)
    json.dumps(out)
    return out


def test_pressure(env):
    now = time.time()
    env["tel"].write_text("".join(json.dumps(_row(now - 60 + i * 0.01)) + "\n" for i in range(50)))
    out = _ok(["pressure"])["pressure"]
    assert "level" in out and "causes" in out
    assert "error" not in out and "telemetry" not in out
    assert str(env["tel"]) not in json.dumps(out)


def test_errors15m(env):
    now = time.time()
    rows = [_row(now - 30, "ReadError", True), _row(now - 20, "ReadError", True),
            _row(now - 10, "SSLError", True), _row(now - 5)]
    env["tel"].write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = _ok(["errors15m"])["errors15m"]
    assert out["n"] == 3
    assert out["by_cause"] == {"ReadError": 2, "SSLError": 1}


def test_measure(env):
    d = env["home"] / "observe"
    d.mkdir()
    (d / "a.jsonl").write_text(json.dumps(
        {"ev": "measure", "ts": time.time(), "limit_pct": 42, "cost_usd": 1.5, "ctx_pct": 10}) + "\n")
    out = _ok(["measure"])["measure"]
    assert out["limit_pct"] == 42 and out["cost_usd"] == 1.5 and out["ctx_pct"] == 10


def test_measure_missing(env):
    out = _ok(["measure"])["measure"]
    assert out["missing"] is True


def _fill_log(n_ok, n_esc):
    for _ in range(n_ok):
        assert route_log.log_outcome("explore", "haiku", "ok")
    for _ in range(n_esc):
        assert route_log.log_outcome("explore", "haiku", "escalated")


def test_route_rates(env):
    _fill_log(3, 1)
    out = _ok(["route_rates"])["route_rates"]
    assert out["explore"]["n"] == 4 and out["explore"]["escalated"] == 1
    assert out["explore"]["rate"] == 0.25


def test_route_rates_empty(env):
    assert _ok(["route_rates"])["route_rates"] == {}


def test_route_advice(env):
    _fill_log(30, 30)
    out = _ok(["route_advice"])["route_advice"]
    assert isinstance(out, dict) and "explore" in out
    assert "verdict" in out["explore"]


def test_worker_counts_only(env):
    (env["queue"] / "jobs" / "inbox").mkdir(parents=True)
    (env["queue"] / "jobs" / "running").mkdir(parents=True)
    (env["queue"] / "jobs" / "inbox" / "j1.json").write_text("{}")
    (env["queue"] / "jobs" / "inbox" / "j2.json").write_text("{}")
    out = _ok(["worker"])["worker"]
    assert out == {"running": True, "inbox": 2, "queue_running": 0}
    assert "label" not in out and "error" not in out and "pid" not in out


def test_worker_error_dropped(env, monkeypatch):
    monkeypatch.setattr(agents, "_launchd_pid", lambda label, timeout=2.0: (None, "boom"))
    out = _ok(["worker"])["worker"]
    assert "error" not in out and out["running"] is False


# ---- scrub ----

def test_scrub_drops_keys_recursive():
    v = {"a": 1, "error": "x", "path": "p", "telemetry": "t", "session": "s", "session_id": "s",
         "repo": "r", "cwd": "c", "description": "d", "note": "n", "url": "u",
         "nested": {"repo": "r", "k": [{"cwd": "c", "v": 2}]}}
    assert datasets.scrub(v) == {"a": 1, "nested": {"k": [{"v": 2}]}}


def test_scrub_drops_path_strings():
    v = {"a": "/etc/passwd", "b": "~/x", "c": "C:\\x", "d": "ok", "l": ["/x", "~y", "a\\b", "fine"]}
    assert datasets.scrub(v) == {"d": "ok", "l": ["fine"]}


def test_scrub_keeps_scalars_and_json_safe():
    out = datasets.scrub({"a": (1, 2), "b": None, "c": True, "d": 1.5, "e": {3}, "f": object})
    json.dumps(out)
    assert out["a"] == [1, 2] and out["b"] is None and out["c"] is True and out["e"] == [3]


# ---- read ----

def test_read_not_shared_and_unknown(env):
    out = datasets.read(["worker", "nope", "agents"], frozenset({"route_rates"}))
    assert out == {"worker": {"error": "not_shared"}, "nope": {"error": "not_shared"},
                   "agents": {"error": "not_shared"}}


def test_read_unavailable(monkeypatch):
    def boom():
        raise RuntimeError("secret /path")
    monkeypatch.setitem(datasets.ALLOWLIST, "worker", boom)
    out = datasets.read(["worker"], {"worker"})
    assert out == {"worker": {"error": "unavailable"}}
    json.dumps(out)


def test_read_unserializable_is_scrubbed(monkeypatch):
    monkeypatch.setitem(datasets.ALLOWLIST, "worker", lambda: {"x": object()})
    out = datasets.read(["worker"], {"worker"})
    json.dumps(out)


def test_read_shared_empty(env):
    assert datasets.read(["worker"], frozenset()) == {"worker": {"error": "not_shared"}}


# ---- parse_share ----

def test_parse_share():
    all_names = frozenset(datasets.ALLOWLIST)
    assert datasets.parse_share(None) == all_names
    assert datasets.parse_share([]) == all_names
    assert datasets.parse_share(["all"]) == all_names
    assert datasets.parse_share(["none"]) == frozenset()
    assert datasets.parse_share(["worker", "pressure"]) == frozenset({"worker", "pressure"})
    assert datasets.parse_share(["worker", "none"]) == frozenset({"worker"})


def test_parse_share_unknown():
    with pytest.raises(ValueError):
        datasets.parse_share(["worker", "agents"])


def test_allowlist_names():
    assert set(datasets.ALLOWLIST) == {"pressure", "errors15m", "measure", "route_rates",
                                       "route_advice", "worker"}
    assert "agents" not in datasets.ALLOWLIST
