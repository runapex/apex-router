"""apex studio v2 (Metro) backend: agent attribution, /api/recent, /api/tokens, /api/experiments,
/api/stream (SSE) and the no-secret rule. Contract: docs/design/studio-v2/API.md. Every test uses a
fixture home in tmp_path (tests/studio_fixture.py); nothing reads the real ~/.apex, ~/.apex-router,
~/.pi, ~/.claude, ~/.codex or ~/dev/ml, and no test calls a real proxy."""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from apex_router import agents
from apex_router.studio import experiments, flow, server, traffic

import studio_fixture as F

TOKEN = "v" * 32


def _row(ts, sid, client="claude-code", ep="anthropic", **kw):
    r = {"ts": ts, "client": client, "endpoint_id": ep, "schema_version": 12, "session_id": sid,
         "is_error": False, "error_cause": None, "connect_retries": 0, "ttft_ms": 1200.0,
         "model_requested": "it-entra-claude-opus-5-5", "tokens_in": 2, "tokens_out": 689,
         "cache_read_tokens": 340993, "cache_write_tokens": 242,
         "usage": {"captured": True, "input_tokens": 2, "cache_read_tokens": 340993,
                   "cache_creation_tokens": 242, "output_tokens": 689}}
    r.update(kw)
    return r


def _write(path: Path, rows, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    now = time.time()
    F.make_home(root, now, telemetry=False)
    F.make_sessions(root)
    F.make_experiments(root, now)
    for k, v in F.env_for(root).items():
        monkeypatch.setenv(k, v)
    traffic._kinds.clear()
    traffic._tok_cache.clear()
    return root, now


def _rows_one_per_agent(now):
    return [
        _row(now - 50, F.PI_SID),                                   # pi (job id), Claude headers
        _row(now - 40, F.PI_UUID_SID, model_requested="it-entra-claude-sonnet-5-5"),   # pi (uuid)
        _row(now - 30, F.CLAUDE_SID),                               # Claude Code
        _row(now - 20, F.CODEX_SID, client="codex", ep="openai", model_requested="it-entra-gpt-6-astra",
             tokens_in=16205, tokens_out=104, cache_read_tokens=16137, cache_write_tokens=0,
             usage={"captured": True, "input_tokens": 16205, "cache_read_tokens": 16137,
                    "cache_creation_tokens": 0, "output_tokens": 104}),
        _row(now - 10, F.UNKNOWN_SID),                              # no session file
        _row(now - 5, None),                                        # no session id
    ]


@pytest.fixture
def studio(home):
    root, now = home
    _write(root / ".apex" / "telemetry.jsonl", _rows_one_per_agent(now))
    paths = flow.resolve_paths()
    srv = server.make_server(port=0, token=TOKEN, paths=paths)
    srv.stats_fetch = lambda url: {"activity": {"inflight": 0, "idle_s": 12.0, "draining": False}}
    srv.restart_fetch = lambda url: None
    srv.stream_poll_s, srv.stream_snap_s, srv.stream_hb_s = 0.05, 0.3, 0.4
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv, paths, root, now
    srv.shutdown()
    srv.server_close()


def get(srv, path, token=TOKEN, query_token=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{srv.port}"}
    if token:
        h["X-Studio-Token"] = token
    if query_token:
        path += ("&" if "?" in path else "?") + "token=" + query_token
    c.request("GET", path, headers=h)
    r = c.getresponse()
    txt = r.read().decode()
    c.close()
    try:
        return r.status, json.loads(txt)
    except ValueError:
        return r.status, txt


# ---- attribution -----------------------------------------------------------------------------

def test_session_kinds_maps_pi_claude_codex_by_file_name(home):
    root, _ = home
    k = agents.session_kinds(root)
    assert k[F.PI_SID] == "pi" and k[F.PI_UUID_SID] == "pi"
    assert k[F.CLAUDE_SID] == "claude" and k[F.CODEX_SID] == "codex"
    assert F.UNKNOWN_SID not in k


def test_session_kinds_uses_the_menu_id_rules(home):
    # the same rule the menu snapshot (pi_agents / codex_agents) uses for session_id
    root, now = home
    menu = {a["session_id"]: a["kind"] for a in agents.discover(root, now + 1) if not a.get("error")}
    idx = agents.session_kinds(root)
    for sid, kind in menu.items():
        assert idx[sid] == kind


def test_session_kinds_missing_home_is_empty(tmp_path):
    assert agents.session_kinds(tmp_path / "nope") == {}


def test_pi_traffic_with_claude_client_is_attributed_to_pi(studio):
    srv, _, _, _ = studio
    st, d = get(srv, "/api/recent?n=10")
    assert st == 200
    by_age = sorted(d["requests"], key=lambda r: r["ts"])
    assert [r["agent"] for r in by_age] == ["pi", "pi", "claude", "codex", "other", "other"]


def test_a_new_session_is_found_without_waiting_for_the_ttl(studio):
    srv, _, root, now = studio
    get(srv, "/api/recent?n=1")                               # builds the index
    sid = "apexjob-20261027130000-zz99"
    d = root / ".pi" / "agent" / "sessions" / "--x--"
    d.mkdir(parents=True)
    (d / f"2026-10-27T13-00-00-000Z_{sid}.jsonl").write_text("{}\n")
    _write(root / ".apex" / "telemetry.jsonl", [_row(now - 1, sid)], mode="a")
    k = traffic.kinds_for(srv.get_paths())
    k.built -= traffic.KINDS_MISS_TTL_S + 1                   # the miss window has passed
    st, out = get(srv, "/api/recent?n=1")
    assert out["requests"][0]["agent"] == "pi"


# ---- /api/recent -----------------------------------------------------------------------------

def test_recent_shape_newest_first_and_n(studio):
    srv, _, _, now = studio
    st, d = get(srv, "/api/recent?n=3")
    assert st == 200 and len(d["requests"]) == 3
    ts = [r["ts"] for r in d["requests"]]
    assert ts == sorted(ts, reverse=True)
    r = d["requests"][0]
    assert set(r) == {"id", "ts", "agent", "provider", "model_family", "status", "cause", "ms_total",
                      "ttft_ms", "tokens"}
    assert set(r["tokens"]) == {"in", "out", "cache_read", "cache_write"}
    claude = next(x for x in get(srv, "/api/recent?n=6")[1]["requests"] if x["agent"] == "claude")
    assert claude["provider"] == "anthropic" and claude["model_family"] == "Claude Opus"
    assert claude["status"] == "ok" and claude["cause"] is None and claude["ttft_ms"] == 1200
    assert claude["tokens"] == {"in": 2, "out": 689, "cache_read": 340993, "cache_write": 242}


def test_recent_default_and_bad_n(studio):
    srv, *_ = studio
    st, d = get(srv, "/api/recent")
    assert st == 200 and len(d["requests"]) == 6
    for bad in ("0", "201", "-1", "x", "1e2", "%20"):
        assert get(srv, f"/api/recent?n={bad}")[0] == 400


def test_recent_status_and_cause_are_plain_sentences(studio):
    srv, _, root, now = studio
    _write(root / ".apex" / "telemetry.jsonl", [
        _row(now - 3, F.PI_SID, is_error=True, error_cause="ConnectError", connect_retries=2,
             error_detail="Traceback secret-ish detail"),
        _row(now - 2, F.PI_SID, connect_retries=1),
        _row(now - 1, F.PI_SID, is_error=True, error_cause="weird_new_cause: raw text")], mode="a")
    st, d = get(srv, "/api/recent?n=3")
    a, b, c = d["requests"]
    assert (a["status"], a["cause"]) == ("error", traffic._CAUSE_OTHER)
    assert (b["status"], b["cause"]) == ("retried", traffic.CAUSE_RETRIED)
    assert (c["status"], c["cause"]) == ("error", "Provider not reached.")
    assert "raw text" not in json.dumps(d) and "Traceback" not in json.dumps(d)


def test_recent_ids_are_stable_and_unique(studio):
    srv, *_ = studio
    a = get(srv, "/api/recent?n=6")[1]["requests"]
    b = get(srv, "/api/recent?n=6")[1]["requests"]
    assert [r["id"] for r in a] == [r["id"] for r in b]
    assert len({r["id"] for r in a}) == 6


def test_recent_merges_the_local_proxy_and_the_rotated_file(studio):
    srv, _, root, now = studio
    tel = root / ".apex" / "telemetry.jsonl"
    os.rename(tel, root / ".apex" / "telemetry.jsonl.1")               # rotation: live file is new
    _write(tel, [_row(now - 1, F.PI_SID)])
    _write(root / ".apex-local" / "telemetry.jsonl",
           [{"ev": "hb", "ts": now - 2}, _row(now - 0.5, F.UNKNOWN_SID, client="codex", ep="openai",
                                              model_requested="qwen3.8:27b-mlx", tokens_in=3462,
                                              cache_read_tokens=0, cache_write_tokens=0, usage=None)])
    st, d = get(srv, "/api/recent?n=8")
    assert len(d["requests"]) == 8
    top = d["requests"][0]
    assert top["provider"] == "ollama" and top["model_family"] == "Qwen" and top["agent"] == "other"
    assert d["requests"][1]["agent"] == "pi"


def test_recent_empty_home(home):
    paths = flow.resolve_paths()
    assert traffic.recent(paths, 10) == {"requests": []}


def test_model_family_never_returns_the_id():
    assert traffic.model_family("it-entra-claude-opus-5-5") == "Claude Opus"
    assert traffic.model_family("claude-sonnet-4-6[1m]") == "Claude Sonnet"
    assert traffic.model_family("it-entra-gpt-6-astra") == "GPT 6"
    assert traffic.model_family("it-entra-gpt-5.6-luna") == "GPT 5.6"
    assert traffic.model_family("qwen3.8:27b-mlx") == "Qwen"
    assert traffic.model_family("sk-" + "x" * 30) == "Other"
    assert traffic.model_family(None) == "Other"


# ---- /api/tokens -----------------------------------------------------------------------------

def test_tokens_normalise_per_provider_without_double_count(studio):
    srv, *_ = studio
    st, d = get(srv, "/api/tokens?window=15m")
    assert st == 200 and d["window"] == "15m"
    # OpenAI: tokens_in 16205 includes cache_read 16137 -> in = 68 (not 16205)
    assert d["by_provider"]["openai"] == {"in": 68, "out": 104, "cache_read": 16137, "cache_write": 0}
    # Anthropic: tokens_in is already uncached -> in = 2 per row, 5 rows
    assert d["by_provider"]["anthropic"] == {"in": 10, "out": 5 * 689, "cache_read": 5 * 340993,
                                             "cache_write": 5 * 242}
    t = d["total"]
    assert t["in"] == 78 and t["cache_read"] == 5 * 340993 + 16137
    # every split sums to the total (one row is counted once in each split)
    for split in ("by_agent", "by_provider", "by_model"):
        for k in t:
            assert sum(v[k] for v in d[split].values()) == t[k]
    assert d["by_agent"]["pi"]["in"] == 4 and d["by_agent"]["codex"]["in"] == 68
    assert set(d["by_model"]) == {"Claude Opus", "Claude Sonnet", "GPT 6"}
    den = t["in"] + t["cache_read"] + t["cache_write"]
    assert d["cache_share"] == round(t["cache_read"] / den, 4)


def test_tokens_usage_fallback_and_windows(studio):
    srv, _, root, now = studio
    # an old-format row: the top-level token fields are 0, only the provider usage block is set
    old = _row(now - 2 * 3600, F.PI_SID, tokens_in=0, tokens_out=0, cache_read_tokens=0, cache_write_tokens=0,
               usage={"input_tokens": 7, "cache_read_tokens": 100, "cache_creation_tokens": 5,
                      "output_tokens": 9})
    _write(root / ".apex" / "telemetry.jsonl", [old] + _rows_one_per_agent(now))
    traffic._tok_cache.clear()
    d24 = get(srv, "/api/tokens?window=24h")[1]
    d1 = get(srv, "/api/tokens?window=1h")[1]
    diff = {k: d24["total"][k] - d1["total"][k] for k in d1["total"]}
    assert diff == {"in": 7, "out": 9, "cache_read": 100, "cache_write": 5}   # the 2 h row is in 24h only
    assert d24["by_agent"]["pi"]["in"] - d1["by_agent"]["pi"]["in"] == 7
    assert get(srv, "/api/tokens?window=7d")[0] == 400


def test_tokens_empty_home_has_zero_total_and_null_share(home):
    d = traffic.tokens(flow.resolve_paths(), "1h")
    assert d["total"] == {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    assert d["by_agent"] == {} and d["cache_share"] is None


# ---- /api/experiments ------------------------------------------------------------------------

_KEYS = {"id", "name", "state", "headline", "metric", "gate", "n", "updated_ts", "stale", "source", "error"}
_STATES = {"running", "paused", "done", "failed", "provisional", "unknown"}


def _exp(paths):
    return {i["id"]: i for i in experiments.collect(paths)["items"]}


def test_experiments_good_files(studio):
    srv, *_ = studio
    st, d = get(srv, "/api/experiments")
    assert st == 200
    items = {i["id"]: i for i in d["items"]}
    assert list(items) == [r[0] for r in experiments.READERS]
    for i in items.values():
        assert set(i) == _KEYS and i["state"] in _STATES and i["error"] is None, i
    j = items["jepa"]
    assert j["state"] == "paused" and j["metric"] == {"name": "C1 rel", "value": -11.1, "unit": "%"}
    assert j["gate"] == {"name": "G1", "passed": False} and j["n"] == 7100 and j["stale"] is True
    assert j["headline"] == "Gate G1 failed. Paused by its stop rule."
    g = items["gold"]
    assert g["state"] == "provisional" and g["metric"]["value"] == 91.0 and g["n"] == 100
    assert "1 of 2" in g["headline"]
    assert items["label_judge"]["metric"]["value"] == 0.92 and items["label_judge"]["n"] == 222
    e = items["explore"]
    assert e["state"] == "done" and e["gate"]["passed"] is False and e["n"] == 12
    x = items["xval_bandit"]
    assert x["n"] == 170 and x["metric"]["value"] == 80.0 and x["state"] == "running"
    p = items["panel"]
    assert p["gate"] == {"name": "debate/musique/vs-sc6", "passed": False} and p["n"] == 356
    r = items["rsi_outside"]
    assert r["metric"]["value"] == 80.0 and r["n"] == 3
    assert items["logmine"]["metric"]["value"] == 91.0
    s = items["sec_swarm"]
    assert s["state"] == "done" and s["gate"] == {"name": "evaluate", "passed": False}
    c = items["compaction"]
    assert c["n"] == 1 and c["metric"]["value"] == 105478 and "39 compactions" in c["headline"]


_SRC = {"jepa": "repo/docs/research/2026-10-08-p6-g1-attempt-3.md",
        "gold": ".apex-router/labels/gold/results_for_mac_home.txt",
        "label_judge": "repo/docs/research/2026-10-08-gold-xval.md",
        "explore": ".apex-router/explore/pilot-smoke-20261026-224006/results.jsonl",
        "xval_bandit": ".apex-router/xval_bandit.json",
        "panel": ".apex-router/panel/card-debate-musique-356.json",
        "rsi_outside": "dev/ml/rsi/outside/judge_runs.jsonl",
        "logmine": "dev/ml/logmine/data/oracle_coverage.json",
        "sec_swarm": "dev/ml/sec/swarm/data/STATUS.json",
        "compaction": None}


def _main_file(root: Path, id_: str) -> list:
    if id_ == "compaction":
        return [root / ".apex-router" / "pi-compact", root / "repo" / "docs" / "research" /
                "2026-10-26-pi-compaction-precision-cost.md"]
    return [root / _SRC[id_]]


def _rm(p: Path):
    if p.is_dir():
        import shutil
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()


@pytest.mark.parametrize("id_", list(_SRC))
def test_experiment_missing_file_is_unknown(home, id_):
    root, _ = home
    for p in _main_file(root, id_):
        _rm(p)
    it = _exp(flow.resolve_paths())[id_]
    assert it["state"] == "unknown" and it["error"] == "missing", it
    assert set(it) == _KEYS and it["metric"] is None


def _break(p: Path):
    if p.is_dir():
        for f in p.rglob("*"):
            if f.is_file():
                f.write_bytes(b"\x00\xff{not json")
    else:
        p.write_bytes(b"\x00\xff{not json\n")


@pytest.mark.parametrize("id_", list(_SRC))
def test_experiment_broken_file_is_unknown(home, id_):
    root, _ = home
    for p in _main_file(root, id_):
        _break(p)
    if id_ == "explore":                         # newest dir = the broken one
        _break(root / ".apex-router" / "explore" / "pilot-smoke-20261026-224006" / "results.jsonl")
    it = _exp(flow.resolve_paths())[id_]
    assert it["state"] == "unknown" and it["error"] == "bad format", it


def test_experiment_reader_caps_bytes_and_never_raises(home, monkeypatch):
    root, _ = home
    big = root / "dev" / "ml" / "logmine" / "data" / "oracle_coverage.json"
    big.write_bytes(b"{" + b" " * (experiments.CAP + 10) + b"}")
    it = _exp(flow.resolve_paths())["logmine"]
    assert it["error"] == "too large" and it["state"] == "unknown"

    def boom(paths, now):
        raise RuntimeError("secret /Users/x/path detail")
    monkeypatch.setattr(experiments, "READERS", (("jepa", "JEPA world model", boom),))
    it = experiments.collect(flow.resolve_paths())["items"][0]
    assert it["error"] == "bad format" and "secret" not in json.dumps(it)


def test_experiment_staleness(home):
    root, now = home
    items = _exp(flow.resolve_paths())
    assert items["sec_swarm"]["stale"] is False          # 1 h old
    assert items["logmine"]["stale"] is True             # 2026-10-20 vs now (>= 2026-10-27)


# ---- /api/stream -----------------------------------------------------------------------------

class SSE:
    def __init__(self, srv, token=TOKEN):
        self.sock = socket.create_connection(("127.0.0.1", srv.port), timeout=20)
        req = (f"GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:{srv.port}\r\n"
               + (f"X-Studio-Token: {token}\r\n" if token else "") + "\r\n")
        self.sock.sendall(req.encode())
        self.f = self.sock.makefile("rb")
        line = self.f.readline().decode()
        self.status = int(line.split()[1])
        self.headers = {}
        while True:
            h = self.f.readline().decode().strip()
            if not h:
                break
            k, _, v = h.partition(":")
            self.headers[k.strip().lower()] = v.strip()

    def events(self, until, timeout=20.0):
        """Read (event, data|comment) until ``until(list)`` is true."""
        out, ev, end = [], None, time.time() + timeout
        while time.time() < end:
            ln = self.f.readline()
            if not ln:
                break
            s = ln.decode().rstrip("\n")
            if s.startswith(":"):
                out.append(("comment", s))
            elif s.startswith("event: "):
                ev = s[7:]
            elif s.startswith("data: "):
                out.append((ev, json.loads(s[6:])))
                ev = None
            if until(out):
                return out
        raise AssertionError(f"timeout; got {out}")

    def close(self):
        try:
            self.f.close()
            self.sock.close()
        except OSError:
            pass


def _wait(cond, t=15.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_stream_snapshot_at_connect_and_periodic(studio):
    srv, _, root, _ = studio
    # rows relative to NOW (not fixture time): `live` = a request in the last 60 s, so a slow run
    # must not age the fixture rows out of it
    _write(root / ".apex" / "telemetry.jsonl", _rows_one_per_agent(time.time()))
    s = SSE(srv)
    try:
        assert s.status == 200 and s.headers["content-type"].startswith("text/event-stream")
        ev = s.events(lambda o: sum(1 for e in o if e[0] == "snapshot") >= 2)
        snap = ev[0]
        assert snap[0] == "snapshot"
        d = snap[1]
        assert set(d) == {"status", "lines", "proxy"}
        assert d["status"]["level"] in ("ok", "slow", "problem") and d["status"]["sentence"]
        assert d["proxy"] == {"up": True, "inflight": 0, "idle_s": 12.0, "draining": False}
        lines = {(x["agent"], x["provider"]): x for x in d["lines"]}
        assert lines[("pi", "anthropic")]["rpm"] == 0.4 and lines[("pi", "anthropic")]["live"] is True
        assert ("codex", "openai") in lines and ("claude", "anthropic") in lines
        assert set(next(iter(lines.values()))) == {"agent", "provider", "rpm", "err_pct", "live"}
    finally:
        s.close()


def test_stream_request_event_on_a_new_row_and_heartbeat(studio):
    srv, _, root, now = studio
    s = SSE(srv)
    try:
        s.events(lambda o: len(o) >= 1)
        _write(root / ".apex" / "telemetry.jsonl", [{"ev": "hb", "ts": time.time()},
                                                    _row(time.time(), F.PI_SID, tokens_out=5)], mode="a")
        ev = s.events(lambda o: any(e[0] == "request" for e in o) and any(e[0] == "comment" for e in o))
        reqs = [e[1] for e in ev if e[0] == "request"]
        assert len(reqs) == 1                          # the heartbeat row is no request
        r = reqs[0]
        assert r["agent"] == "pi" and r["tokens"]["out"] == 5
        rec = get(srv, "/api/recent?n=1")[1]["requests"][0]
        assert rec == r                                # same shape and id as /api/recent
        assert ("comment", ": hb") in ev
    finally:
        s.close()


def test_stream_follows_rotation(studio):
    srv, _, root, now = studio
    tel = root / ".apex" / "telemetry.jsonl"
    s = SSE(srv)
    try:
        s.events(lambda o: len(o) >= 1)
        _write(tel, [_row(time.time(), F.PI_SID, tokens_out=11)], mode="a")    # before the rename
        os.rename(tel, tel.with_name("telemetry.jsonl.1"))
        _write(tel, [_row(time.time(), F.CODEX_SID, client="codex", ep="openai", tokens_out=22)])
        ev = s.events(lambda o: len([e for e in o if e[0] == "request"]) >= 2)
        outs = [e[1]["tokens"]["out"] for e in ev if e[0] == "request"]
        assert outs == [11, 22]
        _write(tel, [_row(time.time(), F.PI_SID, tokens_out=33)], mode="a")    # the new file keeps going
        ev = s.events(lambda o: any(e[0] == "request" for e in o))
        assert [e[1]["tokens"]["out"] for e in ev if e[0] == "request"] == [33]
    finally:
        s.close()


def test_stream_partial_line_waits_for_its_end(studio):
    srv, _, root, _ = studio
    tel = root / ".apex" / "telemetry.jsonl"
    s = SSE(srv)
    try:
        s.events(lambda o: len(o) >= 1)
        line = json.dumps(_row(time.time(), F.PI_SID, tokens_out=44))
        with open(tel, "a") as f:
            f.write(line[:30])
        time.sleep(0.3)
        with open(tel, "a") as f:
            f.write(line[30:] + "\n")
        ev = s.events(lambda o: any(e[0] == "request" for e in o))
        assert [e[1]["tokens"]["out"] for e in ev if e[0] == "request"] == [44]
    finally:
        s.close()


def test_fifth_stream_gets_429_and_a_slot_frees_when_a_client_goes(studio):
    srv, *_ = studio
    open_ = [SSE(srv) for _ in range(4)]
    try:
        assert all(s.status == 200 for s in open_)
        assert _wait(lambda: srv.streams_open == 4)
        fifth = SSE(srv)
        assert fifth.status == 429
        fifth.close()
        open_[0].close()                               # client goes away -> BrokenPipe -> slot freed
        assert _wait(lambda: srv.streams_open == 3)
        again = SSE(srv)
        assert again.status == 200
        open_[0] = again
    finally:
        for s in open_:
            s.close()
    assert _wait(lambda: srv.streams_open == 0)


def test_stream_needs_the_header_token_not_the_query(studio):
    srv, *_ = studio
    assert SSE(srv, token=None).status == 403
    assert SSE(srv, token="wrong" * 8).status == 403
    for p in ("/api/recent", "/api/tokens", "/api/experiments", "/api/stream"):
        assert get(srv, p, token=None, query_token=TOKEN)[0] == 403
        assert get(srv, p, host="evil.example")[0] == 421
    assert get(srv, "/api/flow", token=None, query_token=TOKEN)[0] == 200    # old routes keep it


def test_shutdown_ends_open_streams(studio):
    srv, *_ = studio
    s = SSE(srv)
    try:
        assert _wait(lambda: srv.streams_open == 1)
        srv.closing.set()
        assert _wait(lambda: srv.streams_open == 0)
    finally:
        s.close()


def test_open_stream_does_not_keep_the_studio_alive(home, monkeypatch):
    root, _ = home
    paths = flow.resolve_paths()
    box = {}

    def on_start(srv):
        srv.stats_fetch = lambda url: None
        srv.stream_poll_s, srv.stream_snap_s, srv.stream_hb_s = 0.05, 0.2, 0.2
        box["srv"] = srv
        box["sse"] = SSE(srv)

    th = threading.Thread(target=server.serve, kwargs=dict(
        port=0, open_browser=False, idle_exit_s=1.0, emit=lambda *_: None, paths=paths,
        on_start=on_start), daemon=True)
    t0 = time.time()
    th.start()
    th.join(30)
    try:
        assert not th.is_alive(), "the studio kept running with an open stream"
        assert time.time() - t0 < 30
        assert box["srv"].streams_open == 0
    finally:
        box.get("sse") and box["sse"].close()


def test_tail_starts_at_end_and_handles_truncation_and_late_file(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"a":1}\n')
    t = traffic.Tail(p)
    assert t.read() == []
    with open(p, "a") as f:
        f.write('{"a":2}\n')
    assert t.read() == [b'{"a":2}']
    p.write_text("")                          # truncated in place
    t.read()
    with open(p, "a") as f:
        f.write('{"a":3}\n')
    assert t.read() == [b'{"a":3}']
    t.close()
    q = tmp_path / "late.jsonl"
    t2 = traffic.Tail(q)
    assert t2.read() == []
    q.write_text('{"b":1}\n')
    assert t2.read() == [b'{"b":1}']          # a file that appears later is read from 0
    t2.close()


def test_snapshot_levels(home):
    root, now = home
    paths = flow.resolve_paths()
    tel = root / ".apex" / "telemetry.jsonl"
    assert traffic.snapshot(paths, lambda u: None, 1, now)["status"]["level"] == "problem"   # no proxy
    up = lambda u: {"activity": {"inflight": 2, "idle_s": 0.0, "draining": False}}  # noqa: E731
    d = traffic.snapshot(paths, up, 1, now)
    assert d["status"] == {"level": "ok", "sentence": "All good. No requests in the last 5 min."}
    _write(tel, [_row(now - 4, F.PI_SID)])
    assert traffic.snapshot(paths, up, 1, now)["status"]["sentence"] == "All good. Last request 4 s ago."
    _write(tel, [_row(now - i, F.PI_SID, is_error=i % 2 == 0, error_cause="http_429") for i in range(1, 11)])
    assert traffic.snapshot(paths, up, 1, now)["status"]["level"] == "problem"
    _write(tel, [{"ev": "hb", "ts": now - 10}])                       # no /stats, but a fresh heartbeat
    assert traffic.snapshot(paths, lambda u: None, 1, now)["proxy"]["up"] is True


# ---- no secret in any response ---------------------------------------------------------------

def test_no_response_contains_a_planted_secret_or_the_token(studio):
    srv, _, root, now = studio
    secret = F.FAKE_SECRET
    _write(root / ".apex" / "telemetry.jsonl", [
        _row(now - 1, F.PI_SID, request_id=secret, error_detail=secret, is_error=True,
             error_cause=secret, model_requested=secret, endpoint_id=secret, prompt=secret,
             headers={"authorization": f"Bearer {secret}", "x-api-key": secret},
             agent_id=secret, matcher_error=secret, stop_reason=secret)], mode="a")
    _write(root / ".apex-local" / "telemetry.jsonl", [_row(now - 0.5, secret, model_requested=secret)])
    bodies = []
    for p in ("/api/recent?n=200", "/api/tokens?window=15m", "/api/tokens?window=24h", "/api/experiments"):
        traffic._tok_cache.clear()
        st, d = get(srv, p)
        assert st == 200
        bodies.append(json.dumps(d))
    s = SSE(srv)
    try:
        s.events(lambda o: len(o) >= 1)
        _write(root / ".apex" / "telemetry.jsonl", [
            _row(time.time(), F.PI_SID, model_requested=secret, error_detail=secret, prompt=secret)], mode="a")
        ev = s.events(lambda o: any(e[0] == "request" for e in o))
        bodies.append(json.dumps(ev))
    finally:
        s.close()
    blob = "\n".join(bodies)
    for bad in (secret, TOKEN, "sk-fixture", "Bearer", F.PI_SID, F.CODEX_SID, F.CLAUDE_SID,
                str(root), "please fix"):
        assert bad not in blob, bad
    assert "it-entra" not in blob                            # model ids become families


def test_openai_usage_fallback_cache_is_not_counted_twice():
    # xval P2: fresh_input took the cache count only from the top level, _cache_read also from usage
    from apex_router.studio import traffic
    row = {"endpoint_id": "openai", "tokens_in": 100, "cache_read_tokens": 0,
           "usage": {"cache_read_tokens": 80}}
    t = traffic.tokens_of(row)
    assert t["in"] + t["cache_read"] == 100 and t["cache_read"] == 80


def test_tail_rotation_keeps_every_old_row_beyond_the_read_cap(tmp_path):
    # xval P2: after a rotation the old generation got one more capped read, then was closed
    from apex_router.studio import traffic
    p = tmp_path / "t.jsonl"
    p.write_text("")
    t = traffic.Tail(p)
    with open(p, "a") as f:
        for i in range(50):
            f.write(f'{{"n": {i}}}\n')
    p.rename(tmp_path / "t.jsonl.1")
    p.write_text('{"n": "new"}\n')
    got = []
    for _ in range(100):
        got += t.read(cap=64)              # tiny cap: many polls needed
    nums = [ln for ln in got]
    assert len(nums) == 51 and nums[-1] == b'{"n": "new"}'


def test_tail_truncate_and_regrow_past_the_offset_reads_from_zero(tmp_path):
    # xval P1: truncate + regrow to >= the old offset between polls skipped the new head
    from apex_router.studio import traffic
    p = tmp_path / "t.jsonl"
    p.write_text('{"old": 1}\n' * 10)
    t = traffic.Tail(p)                          # starts at the end (offset 110)
    with open(p, "w") as f:                      # same inode: truncate, then write more
        for i in range(20):
            f.write(f'{{"new": {i:02d}}}\n')
    got = []
    for _ in range(5):
        got += t.read()
    assert got[0] == b'{"new": 00}' and len(got) == 20


@pytest.mark.parametrize("path", ["/api/recent", "/api/tokens", "/api/experiments", "/api/live", "/api/flow"])
def test_get_routes_refuse_a_foreign_origin(path, studio):
    # xval P1: Origin was checked only for POST/PUT/DELETE
    srv = studio[0]
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    c.request("GET", path, headers={"X-Studio-Token": srv.token, "Host": f"127.0.0.1:{srv.port}",
                                    "Origin": "http://evil.example"})
    assert c.getresponse().status == 403


def test_experiment_items_are_short_masked_and_never_follow_links(tmp_path):
    # xval P1 x3: unbounded headline captures, secret-shaped values, symlinks out of the root
    from apex_router.studio import experiments as E
    it = E._item("x", "X", tmp_path, "done", "signals " + "A" * 100_000 + " key sk-abcdefghijklmnop end",
                 {"name": "m", "value": "not a number"}, {"name": "g", "passed": "yes"}, n="9")
    assert len(it["headline"]) <= E.HEADLINE_MAX and "sk-abc" not in it["headline"]
    assert "[hidden]" in it["headline"]
    assert it["metric"]["value"] is None and it["gate"]["passed"] is None and it["n"] is None
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": 1}')
    link = tmp_path / "link.json"
    link.symlink_to(outside)
    with pytest.raises(E.ReadError):
        E._read(link)
