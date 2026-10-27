"""The menu's Flow section and studio rows: no Dashboard row, "Flow studio ↗" first, Flow rows open the
studio at a node with fixed args only, the numbers come from studio.live (one source), the rows do
not cost agent rows, Tokyo Night dark colours, no studio token in the menu, and the run time.
Fixture homes in tmp_path; no real ~/.apex or ~/.apex-router, no launchctl, no network."""
from __future__ import annotations

import json
import shlex
import time

import pytest

from apex_router import agent_resources as ar
from apex_router import snapshot
from apex_router.studio import traffic, flow as flow_mod
from apex_router.studio import live

import studio_fixture as F

BIN = "/Users/you/.local/bin/apex-router"
NOW = 1_791_400_000.0
TOKEN = "TOKENtokenTOKENtoken0123456789ab"


@pytest.fixture(autouse=True)
def _no_real_sources(monkeypatch):
    monkeypatch.setattr(ar, "run_cmd", lambda argv, timeout=2.0: "")
    monkeypatch.setattr(ar, "http_get", lambda url, timeout=1.0: b"{}")
    monkeypatch.setattr(ar, "rusage", lambda pid: None)


def _flow(**kw):
    f = {"window_min": 15, "sources": {"pi": 7, "claude": 40, "codex": 5},
         "upstreams": {"anthropic": 40, "openai": 5, "local": 3}, "requests": 45,
         "error_pct": 2.2, "ttft_p50_ms": 920, "retries": 2, "unreachable": 1,
         "proxy": {"port": 8788, "up": True, "checked": True, "activity": {"inflight": 3, "idle_s": 0.0}},
         "draft_changes": 4}
    f.update(kw)
    return f


def _snap(flow=None, agents=None):
    s = {"ts": NOW, "pressure": {"level": "GREEN", "insufficient_sample": False},
         "agents": agents or [], "system": {}}
    if flow is not None:
        s["flow"] = flow
    return s


def _params(line):
    return dict(p.split("=", 1) for p in shlex.split(line.split(" | ", 1)[1]))


def _flow_rows(text):
    lines = text.splitlines()
    i = lines.index(next(ln for ln in lines if ln.startswith("Flow · 15m | ")))
    out = []
    for ln in lines[i + 1:]:
        if ln == "---":
            break
        out.append(ln)
    return out


# ---------------------------------------------------------------- rows and actions

def test_flow_rows_text_and_studio_targets():
    text = snapshot.menubar(_snap(_flow()), bin_path=BIN)
    rows = _flow_rows(text)
    vis = [snapshot.visible(r) for r in rows]
    assert vis == ["sources: pi 7 · claude 40 · codex 5",
                   "proxy :8788: up · in flight 3 · idle 0s",
                   "upstreams: anthropic 40 · openai 5 · local 3 · err 2.2% · ttft p50 920ms",
                   "errors: retries 2 · 502 1",
                   "draft: 4 changes"]
    want = ["node:src.pi", "node:detect", "node:upstream", "node:upstream", "review"]
    for r, target in zip(rows, want):
        p = _params(r)
        assert {k: p[k] for k in ("bash", "param1", "param2", "param3", "param4", "param5",
                                  "terminal", "refresh")} == {
            "bash": BIN, "param1": "studio", "param2": "--idle-exit",
            "param3": str(snapshot.STUDIO_IDLE_EXIT_S), "param4": "--open", "param5": target,
            "terminal": "false", "refresh": "false"}, r
        assert set(p) <= {"bash", "param1", "param2", "param3", "param4", "param5", "terminal",
                          "refresh", "color", "tooltip"}, r
    assert _params(rows[3])["color"] == snapshot.INK["red"]           # errors row is red


def test_flow_section_follows_pressure_and_studio_row_is_first():
    lines = snapshot.menubar(_snap(_flow()), bin_path=BIN).splitlines()
    assert lines[1] == "---" and lines[2].startswith("Flow studio ↗ | ") and lines[3] == "---"
    heads = [ln.split(" | ")[0] for ln in lines if " size=12 color=" + snapshot.INK["accent"] in ln
             and not ln.startswith("--")]
    assert heads[:2] == ["Pressure", "Flow · 15m"]
    assert "Open dashboard" not in "\n".join(lines) and "param3=all" not in "\n".join(lines)


def test_errors_and_draft_rows_only_when_nonzero_and_dash_for_missing():
    f = _flow(retries=0, unreachable=0, draft_changes=0, error_pct=None, ttft_p50_ms=None,
              sources={"pi": None, "claude": None, "codex": None},
              upstreams={"anthropic": None, "openai": None, "local": None},
              proxy={"port": 8788, "up": True, "checked": True, "activity": None})
    vis = [snapshot.visible(r) for r in _flow_rows(snapshot.menubar(_snap(f), bin_path=BIN))]
    assert vis == ["sources: pi — · claude — · codex —",
                   "proxy :8788: up · in flight — · idle —",               # old proxy: no activity
                   "upstreams: anthropic — · openai — · local — · err — · ttft p50 —"]
    f = _flow(retries=0, unreachable=3, draft_changes=1)
    vis = [snapshot.visible(r) for r in _flow_rows(snapshot.menubar(_snap(f), bin_path=BIN))]
    assert "errors: retries 0 · 502 3" in vis and "draft: 1 change" in vis


@pytest.mark.parametrize("px,word,ink", [
    ({"port": 8788, "up": False, "checked": True, "activity": None}, "down", "red"),
    ({"port": 8788, "up": False, "checked": False, "activity": None}, "not checked", "amber"),
    ({"port": 8788, "up": True, "checked": True, "activity": {"inflight": 0, "idle_s": 75.0}}, "up", "green")])
def test_proxy_row_state_and_colour(px, word, ink):
    row = _flow_rows(snapshot.menubar(_snap(_flow(proxy=px)), bin_path=BIN))[1]
    assert snapshot.visible(row).startswith(f"proxy :8788: {word} · ")
    assert _params(row)["color"] == snapshot.INK[ink]


def test_flow_rows_without_a_valid_binary_have_no_action():
    for b in (None, "apex-router", "/x;rm", "/x/../bin/sh"):
        text = snapshot.menubar(_snap(_flow()), bin_path=b)
        assert "bash=" not in text and "Flow studio" not in text
        assert len(_flow_rows(text)) == 5                  # the numbers still show


@pytest.mark.parametrize("target,ok", [
    ("", True), ("node:src.pi", True), ("node:detect", True), ("node:sink.xval", True), ("review", True),
    ("branches", True), ("#node=src.pi", False), ("node:src.pi param6=x", False), ("test:hi", False),
    ("node:Src", False), ("review\n", False), ("node:", False), (None, True)])
def test_studio_action_target_shapes(target, ok):
    act = snapshot.studio_action(BIN, target)
    assert bool(act) is ok
    if ok and target:
        assert act.endswith(f"param5={target} terminal=false refresh=false")


def test_every_menu_target_is_accepted_by_the_studio():
    from apex_router.studio import server
    text = snapshot.menubar(_snap(_flow()), bin_path=BIN)
    for ln in text.splitlines():
        if "param1=studio" in ln:
            p = _params(ln)
            assert server.target_hash(p.get("param5", "")) is not None, ln


def test_flow_error_block_is_one_muted_row():
    rows = _flow_rows(snapshot.menubar(_snap({"error": "OSError: boom"}), bin_path=BIN))
    assert len(rows) == 1 and snapshot.visible(rows[0]) == "flow unavailable · OSError: boom"
    assert "bash=" not in rows[0]


def test_flow_is_skipped_when_the_shared_deadline_has_passed(home):
    root, now = home
    dl = ar.Deadline(budget=-1.0)
    fb = snapshot.flow_block(root / ".apex" / "telemetry.jsonl", now, {"up": True}, dl)
    assert fb == {"skipped": True, "error": "skipped: deadline"}
    rows = _flow_rows(snapshot.menubar(_snap(fb), bin_path=BIN))
    assert [snapshot.visible(r) for r in rows] == ["flow not checked (deadline)"]


# ---------------------------------------------------------------- budget

def _session_agent(i):
    return {"kind": "claude", "repo": f"repo{i}", "session": f"s{i:07d}", "session_id": f"{i:08x}-0000",
            "state": "active", "age_s": 30.0,
            "res": {"status": "busy", "tree": {"pid": 1000 + i, "alive": True, "footprint_mb": 100.0},
                    "telemetry": {"requests": 2, "tokens_out": 10 * i, "errors": 0,
                                  "models": {"claude-opus-5-5": 2}},
                    "subagents": {"list": [], "count": 0, "running": 0, "quiet": 0, "flagged": 0}}}


def test_flow_rows_do_not_cost_agent_rows():
    agents = [_session_agent(i) for i in range(200)]
    base = snapshot.menubar(_snap(None, agents), bin_path=BIN).splitlines()
    with_flow = snapshot.menubar(_snap(_flow(), agents), bin_path=BIN).splitlines()
    rows = lambda ls: [ln for ln in ls if not ln.startswith(("Flow · 15m", "sources:", "proxy :",  # noqa: E731
                                                              "upstreams:", "errors:", "draft:"))]
    assert rows(with_flow)[:3] == base[:3]
    assert [ln for ln in rows(with_flow) if ln != "---"] == [ln for ln in base if ln != "---"]
    assert len(with_flow) == len(base) + 7                  # header + 5 rows + separator
    assert len(base) <= snapshot.MENU_LINES_MAX + 1          # + the studio row, as before


# ---------------------------------------------------------------- colours

def test_ink_dark_side_is_tokyo_night_and_light_side_kept():
    dark = {k: v.split(",")[1] for k, v in snapshot.INK.items()}
    assert dark == {"text": "#c0caf5", "muted": "#737aa2", "accent": "#7aa2f7", "blue": "#7dcfff",
                    "green": "#9ece6a", "amber": "#e0af68", "red": "#f7768e", "violet": "#bb9af7"}
    light = {k: v.split(",")[0] for k, v in snapshot.INK.items()}
    assert light == {"text": "#141413", "muted": "#5E5D59", "accent": "#C15F3C", "blue": "#3A6A9E",
                     "green": "#5A7A3C", "amber": "#A86A1E", "red": "#B5372B", "violet": "#7A5C9E"}
    text = snapshot.menubar(_snap(_flow()), bin_path=BIN)
    for ln in text.splitlines()[1:]:
        if ln.lstrip("-") and not ln.lstrip("-").startswith("---") and "color=" in ln:
            assert _params(ln)["color"] in snapshot.INK.values(), ln


# ---------------------------------------------------------------- flow_block: one source, real files

@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    now = time.time()
    F.make_home(root, now)
    for k, v in F.env_for(root).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("APEX_STUDIO_MAIN_HOME", raising=False)       # flow_block sets its own
    rh = root / ".apex-router"
    (rh / "studio").mkdir(parents=True, exist_ok=True)
    (rh / "studio" / "draft.json").write_text(json.dumps({"schema": 1, "changes": {
        "a": {"id": "x.a", "value": 1}, "b": {"id": "x.b", "value": 2}}}))
    (rh / "studio" / "instance.json").write_text(json.dumps({"pid": 1, "port": 1, "token": TOKEN}))
    return root, now


def test_flow_block_numbers_equal_the_studio_live_numbers(home, monkeypatch):
    root, now = home
    tel = root / ".apex" / "telemetry.jsonl"
    urls = []
    monkeypatch.setattr(ar, "http_get", lambda url, timeout=1.0: urls.append(url) or json.dumps(
        {"activity": {"inflight": 2, "idle_s": 0.0, "started_s_ago": 99.0}}).encode())
    fb = snapshot.flow_block(tel, now, {"up": True, "port": 8788})
    env = dict(F.env_for(root), APEX_STUDIO_MAIN_HOME=str(tel.parent))
    d = live.collect("15m", "main", flow_mod.resolve_paths(env), now)
    m = d["metrics"]
    # sources by session id (the studio v2 rule), not by the client header (pi sends Claude headers)
    by = traffic.requests_by_agent(flow_mod.resolve_paths(env), 900, now)
    assert fb["sources"] == {"pi": by["pi"], "claude": by["claude"], "codex": by["codex"]}
    assert fb["upstreams"] == {"anthropic": m["tel.endpoint.anthropic"], "openai": m["tel.endpoint.openai"],
                               "local": m["local.requests"]}
    assert fb["error_pct"] == d["telemetry"]["error_pct"] and fb["ttft_p50_ms"] == d["telemetry"]["ttft_p50"]
    assert fb["retries"] == m["tel.retries"] and fb["unreachable"] == m["tel.unreachable"]
    assert fb["upstreams"]["local"] and sum(v or 0 for v in fb["sources"].values()) <= m["tel.requests"]
    assert fb["draft_changes"] == 2
    assert fb["proxy"] == {"port": 8788, "up": True, "checked": True,
                           "activity": {"inflight": 2, "idle_s": 0.0}}
    assert urls == ["http://127.0.0.1:8788/stats"]


def test_flow_block_reads_no_stats_when_proxy_down_or_old(home, monkeypatch):
    root, now = home
    tel = root / ".apex" / "telemetry.jsonl"
    urls = []
    monkeypatch.setattr(ar, "http_get", lambda url, timeout=1.0: urls.append(url) or b'{"version":"x"}')
    assert snapshot.flow_block(tel, now, {"up": False})["proxy"]["activity"] is None
    assert urls == []
    assert snapshot.flow_block(tel, now, {"up": True, "port": 8790})["proxy"]["activity"] is None  # older proxy
    assert urls == ["http://127.0.0.1:8790/stats"]


def test_menu_never_shows_the_studio_token(home):
    root, now = home
    snap = snapshot.collect(home=root, telemetry=root / ".apex" / "telemetry.jsonl",
                            observe_dir=root / "obs", adapters=root / "ad", now=now,
                            proxy_fn=lambda: {"up": False}, worker_fn=lambda: {"label": "w"})
    text = snapshot.menubar(snap, bin_path=BIN)
    assert "Flow · 15m" in text and "draft: 2 changes" in text
    assert TOKEN not in text and TOKEN not in json.dumps(snap) and "token" not in text


# ---------------------------------------------------------------- run time

def test_collect_and_menu_with_flow_stay_fast_on_a_large_telemetry_file(home, tmp_path):
    """A 20 MB telemetry tail: the Flow read must keep one --menubar run well inside its 1.2 s
    shared deadline + HARD_LIMIT_S (every OS source is faked here; the live run is measured by hand)."""
    root, now = home
    tel = root / ".apex" / "telemetry.jsonl"
    row = json.dumps({"ts": now - 3600 * 5, "client": "claude-code", "endpoint_id": "anthropic",
                      "tokens_in": 1, "pad": "x" * 400}) + "\n"
    tel.write_text(row * 50_000 + tel.read_text())   # old rows first: the file is in ts order
    assert tel.stat().st_size > 20_000_000
    t0, c0 = time.perf_counter(), time.process_time()
    snap = snapshot.collect(home=root, telemetry=tel, observe_dir=root / "obs", adapters=root / "ad",
                            now=now, proxy_fn=lambda: {"up": False}, worker_fn=lambda: {"label": "w"})
    text = snapshot.menubar(snap, bin_path=BIN)
    cpu, wall = time.process_time() - c0, time.perf_counter() - t0
    assert "Flow · 15m" in text and not snap["flow"].get("error")
    assert cpu < 1.0, cpu                          # ~0.15 s on an idle Mac; CPU time ignores load
    assert wall < snapshot.HARD_LIMIT_S, wall



def test_flow_sources_count_a_pi_session_as_pi_not_claude(home):
    # owner: the menu showed "pi 1 · claude 12" while all 13 were pi (pi sends Claude-style headers)
    root, now = home
    tel = root / ".apex" / "telemetry.jsonl"
    traffic._kinds.clear()
    before = snapshot.flow_block(tel, now, {"up": False})["sources"]
    sid = "apexjob-test-0001"
    sess = root / ".pi" / "agent" / "sessions" / "--tmp-x--"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / f"2026-10-27T00-00-00-000Z_{sid}.jsonl").write_text(json.dumps({"type": "session", "id": sid}) + "\n")
    with open(tel, "a") as f:
        for i in range(5):
            f.write(json.dumps({"ts": now - 10 - i, "session_id": sid, "client": "claude-code",
                                "endpoint_id": "anthropic", "model_requested": "claude-opus"}) + "\n")
    traffic._kinds.clear()
    after = snapshot.flow_block(tel, now, {"up": False})["sources"]
    assert after["pi"] == (before["pi"] or 0) + 5
    assert after["claude"] == before["claude"]
