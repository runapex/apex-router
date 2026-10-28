"""apex studio: the Workflows view (the main view; the Metro map is retired) and the icon sprite.

Contract: docs/design/studio-v2/API.md "Workflows". Icons: docs/design/studio-v2/icons/README.md.
The server puts the sprite (icons.svg) into the page one time, at start (no fetch). The page uses DOM
APIs and textContent only. These checks read ui.html as text and call the live server with a fixture
home; the screenshots (headless Chrome) check the look.
"""
from __future__ import annotations

import http.client
import re
import subprocess
import shutil
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from apex_router.studio import flow, server, traffic, workflows

import studio_fixture as F

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "apex_router" / "studio" / "ui.html"
SPRITE = ROOT / "src" / "apex_router" / "studio" / "icons.svg"
TOKEN = "w" * 32
SVG_NS = "{http://www.w3.org/2000/svg}"


def _html() -> str:
    return UI.read_text(encoding="utf-8")


def _script(html: str) -> str:
    return html[html.index("<script nonce"):html.rindex("</script>")]


def _style(html: str) -> str:
    return html[html.index("<style nonce"):html.index("</style>")]


def _ids() -> list:
    return [s.get("id") for s in ET.fromstring(SPRITE.read_text(encoding="utf-8")).iter(f"{SVG_NS}symbol")]


# ---- the live page (fixture home) ---------------------------------------------------------------

@pytest.fixture
def studio(tmp_path, monkeypatch):
    root = tmp_path / "home"
    import time
    F.make_home(root, time.time(), telemetry=False)
    for k, v in F.env_for(root).items():
        monkeypatch.setenv(k, v)
    traffic._kinds.clear()
    workflows.clear_caches()
    srv = server.make_server(port=0, token=TOKEN, paths=flow.resolve_paths())
    srv.stats_fetch = srv.restart_fetch = lambda url: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _page(srv) -> tuple[int, str, dict]:
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    c.request("GET", "/", headers={"Host": f"127.0.0.1:{srv.port}", "X-Studio-Token": TOKEN})
    r = c.getresponse()
    body = r.read().decode("utf-8")
    hdrs = dict(r.getheaders())
    c.close()
    return r.status, body, hdrs


# ---- 1. the sprite: the server puts it in one time, at start --------------------------------------

def test_the_page_holds_the_sprite_once_with_every_symbol(studio):
    st, html, _ = _page(studio)
    assert st == 200
    assert html.count("<symbol ") == len(_ids()) == 22
    for sid in _ids():
        assert html.count(f'<symbol id="{sid}"') == 1, sid
    # in the body, before the script, and hidden from the accessibility tree
    i = html.index('<svg id="sprite"')
    assert html.index("<body>") < i < html.index("<script nonce")
    assert 'aria-hidden="true"' in html[i:html.index(">", i)]


def test_ui_html_has_a_slot_not_a_copy_of_the_sprite():
    html = _html()
    assert html.count("__ICONS__") == 1
    assert "<symbol" not in html


def test_the_sprite_is_read_one_time_at_start(studio, monkeypatch):
    assert "<symbol" in studio.ui
    calls = []
    real = Path.read_text

    def spy(self, *a, **kw):
        calls.append(str(self))
        return real(self, *a, **kw)
    monkeypatch.setattr(Path, "read_text", spy)
    for _ in range(3):
        assert _page(studio)[0] == 200
    assert not [c for c in calls if c.endswith(("icons.svg", "ui.html"))]


def test_the_page_never_fetches_the_sprite():
    js = _script(_html())
    assert "icons.svg" not in js
    assert not re.search(r"""href\s*[:=]\s*["'][^"'#]""", js), "an icon href must be a local #id"


def test_the_sprite_insert_cannot_add_script_or_style(tmp_path):
    good = SPRITE.read_text(encoding="utf-8")
    assert server.sprite_markup(good).count("<symbol ") == 22
    bad = [
        good.replace("<title>pi</title>", "<title>pi</title><script>alert(1)</script>", 1),
        good.replace("<title>pi</title>", "<title>pi</title><style>*{}</style>", 1),
        good.replace('<circle cx="16"', '<circle onload="x()" cx="16"', 1),
        good.replace('<circle cx="16"', '<circle style="opacity:.5" cx="16"', 1),
        good.replace("<title>pi</title>", '<title>pi</title><use href="/x.svg#a"/>', 1),
        good.replace("<title>pi</title>", "<title>pi</title><foreignObject/>", 1),
        good.replace('<symbol id="agent-pi"', '<?x <img src=/x>?><symbol id="agent-pi"', 1),
        good.replace('<symbol id="agent-pi"', '<!-- <img src=/x> --><symbol id="agent-pi"', 1),
        good.replace("<title>pi</title>", "<title>pi</script><img src=x></title>", 1),
        good.replace("<title>pi</title>", "<title>__NONCE__</title>", 1),
        good.replace("<title>pi</title>", "<title>&lt;b&gt;</title>", 1),
        good.replace('fill="#24283b"', 'fill="url(/x#a)"', 1),
        "not xml <",
        "",
    ]
    for b in bad:
        assert server.sprite_markup(b) == "", b[:80]


def test_a_bad_sprite_gives_a_page_without_icons_not_an_error(tmp_path, monkeypatch):
    p = tmp_path / "icons.svg"
    p.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>')
    monkeypatch.setattr(server, "ICONS_PATH", p)
    srv = server.make_server(port=0, token=TOKEN)
    try:
        assert "<script>x" not in srv.ui and "__ICONS__" not in srv.ui
        assert '<svg id="sprite"' not in srv.ui
    finally:
        srv.server_close()


# ---- 2. Workflows is the first screen; the Metro map is gone; the panels stay --------------------------

def test_the_metro_map_is_gone():
    html = _html()
    for gone in ('id="map"', "renderMap", "geometry(", "spawnTrain", "g-lines", "g-trains", "drawMarks",
                 "Metro map", "trainpaths"):
        assert gone not in html, gone


def test_workflows_is_the_main_view():
    html = _html()
    assert 'id="wf"' in html and "/api/workflows?window=" in html
    js = _script(html)
    init = js[js.index("(async function init("):]
    assert "loadWorkflows(" in init


def test_the_four_panels_open_from_the_top_buttons():
    html = _html()
    nav = html[html.index("<nav"):html.index("</nav>")]
    panels = re.findall(r'data-panel="([a-z]+)"', nav)
    assert sorted(panels) == ["changes", "experiments", "records", "tokens"]
    js = _script(html)
    for p in panels:
        assert f"VIEWS.{p} =" in js, p


# ---- 3. icons per row type, sizes, accessible names --------------------------------------------------

def test_icons_per_row_type():
    js = _script(_html())
    ids = set(_ids())
    used = set(re.findall(r'"((?:agent|role|model|proc)-[a-z-]+)"', js))
    assert used <= ids, used - ids
    assert {"agent-pi", "agent-claude", "agent-codex"} <= used
    assert {"role-worker", "role-xval", "role-scout", "role-reviewer", "role-coordinator"} <= used
    assert {"model-opus", "model-sonnet", "model-gpt", "model-local"} <= used
    assert len({u for u in used if u.startswith("proc-")}) >= 6       # one per Records pipeline + Records
    for fn in ("agentIcon(", "roleIcon(", "modelIcon(", "procIcon("):
        assert fn in js, fn


def test_icon_sizes_20_in_rows_and_32_in_headers():
    css = _style(_html())
    assert re.search(r"\.ico\s*\{[^}]*width:\s*20px[^}]*height:\s*20px", css)
    assert re.search(r"\.ico\.big\s*\{[^}]*width:\s*32px[^}]*height:\s*32px", css)


def test_every_icon_takes_its_name_from_the_symbol_title():
    js = _script(_html())
    body = js[js.index("function icon("):]
    body = body[:body.index("\n}\n")]
    assert '"role", "img"' in body or "role: \"img\"" in body
    assert "title" in body and "aria-label" in body
    assert 'createElementNS(NS, "use")' in body or 'sv("use"' in body


# ---- 4. running work pulses; the live decision line; problem rings -----------------------------------------

def test_running_pulse_and_problem_rings_in_the_css():
    css = _style(_html())
    assert "@keyframes pulse" in css
    assert re.search(r"\.ring-amber[^{]*\{[^}]*var\(--amber\)", css)
    assert re.search(r"\.ring-red[^{]*\{[^}]*var\(--red\)", css)
    reduce = css[css.index("prefers-reduced-motion"):]
    assert "animation:none" in reduce.replace(" ", "")


def test_the_lowest_station_of_a_running_agent_is_the_decision_line():
    js = _script(_html())
    assert "decisions_last" in js
    body = js[js.index("function agentRow("):]
    body = body[:body.index("\n}\n")]
    assert "liveLine(" in body and "running" in body


def test_a_problem_shows_one_plain_cause():
    js = _script(_html())
    for words in ("stopped at the cost limit", "ran out of time", "stopped: no progress", "was stopped", "failed"):
        assert words in js, words


def test_more_than_12_agents_shows_plus_n_more():
    js = _script(_html())
    assert "agents_more" in js and '" more"' in js


# ---- 5. join and split: POST with the header token, no reload ------------------------------------------

def test_join_and_split_use_the_post_routes_and_reload_the_view_without_a_page_reload():
    js = _script(_html())
    assert '"/api/workflows/join"' in js and '"/api/workflows/split"' in js
    assert "location.reload" not in js
    j = js[js.index("async function joinSelected("):]
    j = j[:j.index("\n}\n")]
    assert 'method: "POST"' in j and "loadWorkflows(" in j
    s = js[js.index("async function splitSession("):]
    s = s[:s.index("\n}\n")]
    assert 'method: "POST"' in s and "loadWorkflows(" in s


def test_the_token_goes_only_in_the_header():
    js = _script(_html())
    assert '"X-Studio-Token": TOKEN' in js
    # no call puts the token in a URL
    assert not re.search(r"[?&]token=\"?\s*\+\s*TOKEN", js)
    assert not re.search(r"`[^`]*token=\$\{TOKEN\}", js)


# ---- 6. fold, window ------------------------------------------------------------------------------------

def test_rows_fold_and_only_running_work_is_open_at_start():
    js = _script(_html())
    assert "aria-expanded" in js
    body = js[js.index("function isOpen("):]
    body = body[:body.index("\n}\n")]
    assert "running" in body


def test_24h_by_default_and_a_7d_choice():
    js = _script(_html())
    assert re.search(r'wfWin:\s*"24h"', js)
    assert '"7d"' in js


# ---- 7. screen-1 budget ------------------------------------------------------------------------------------

def test_screen1_fixed_text_is_25_words_or_fewer():
    js = _script(_html())
    m = re.search(r"const SCREEN1 = \{(.*?)\};", js, re.S)
    assert m, "SCREEN1 holds every fixed text of screen 1"
    words = sum(len(s.split()) for s in re.findall(r'"([^"]*)"\s*(?:,|$)', m.group(1)))
    assert 0 < words <= 25, words


def test_screen1_has_4_controls_or_fewer_at_the_top():
    html = _html()
    header = html[html.index("<header>"):html.index("</header>")]
    assert len(re.findall(r"<button\b", header)) <= 4
    assert len(re.findall(r"<(?:input|select|a)\b", header)) == 0


def _lum(hexc: str) -> float:
    rgb = [int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_text_colours_keep_aa_contrast():
    css = _style(_html())
    var = dict(re.findall(r"--([a-z0-9-]+):(#[0-9a-fA-F]{6})", css))
    for fg in ("fg", "fg2", "soft", "amber", "red", "l-pi", "l-claude", "l-codex"):
        for bg in ("bg", "card", "card2"):
            assert _ratio(var[fg], var[bg]) >= 4.5, (fg, bg, round(_ratio(var[fg], var[bg]), 2))


def test_reduced_motion_is_respected():
    html = _html()
    assert "prefers-reduced-motion" in _style(html)
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in _script(html)


# ---- 8. text only; bounded work per SSE burst; a slow /api/workflows does not freeze the page --------------

def test_no_html_sinks():
    js = _script(_html())
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write", "new Function",
                "setTimeout(\"", "setInterval(\""):
        assert bad not in js, bad


def test_sse_workflow_events_are_bounded():
    js = _script(_html())
    assert 'ev === "workflow"' in js
    assert re.search(r"WF_PENDING_MAX\s*=\s*\d+", js)
    on = js[js.index("function onWorkflowEvent("):]
    on = on[:on.index("\n}\n")]
    assert "WF_PENDING_MAX" in on and "wfRenderSoon(" in on


def test_a_slow_workflows_call_has_a_deadline_and_an_abort():
    js = _script(_html())
    body = js[js.index("async function loadWorkflows("):]
    body = body[:body.index("\n}\n")]
    assert "AbortController" in body and "abort" in body and "WF_TIMEOUT_MS" in body


def test_timers_stop_when_a_line_closes():
    js = _script(_html())
    assert "function stopLines(" in js
    body = js[js.index("function renderWorkflows("):]
    body = body[:body.index("\n}\n")]
    assert "stopLines(" in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_script_parses(tmp_path):
    p = tmp_path / "ui.js"
    p.write_text(_script(_html()).split(">", 1)[1])
    r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# ---- review round 1 (xval): deep agent trees and a huge status sentence --------------------------------------

def _fn_src(name: str) -> str:
    """The source of one top-level function of the page script, by brace count."""
    SCRIPT = _script(_html())
    i = SCRIPT.index(f"function {name}(")
    j = SCRIPT.index("{", i)
    depth, k = 0, j
    while True:
        c = SCRIPT[k]
        depth += c == "{"
        depth -= c == "}"
        k += 1
        if depth == 0:
            return SCRIPT[i:k]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_tree_walks_stop_at_the_depth_limit():
    SCRIPT = _script(_html())
    m = re.search(r"const WF_DEPTH = (\d+)", SCRIPT)
    assert m, "WF_DEPTH is missing"
    js = "\n".join([
        f"const WF_DEPTH = {m.group(1)};",
        "const RED = new Set(['failed']), AMBER = new Set(['timed_out(idle)']);",
        "const arr = (v, n) => Array.isArray(v) ? v.slice(0, n) : [];",
        "const isRun = s => s === 'running';",
        _fn_src("worstOf"), _fn_src("treeRuns"),
        "let deep = {state: 'done', agents: []}; const top = deep;",
        "for (let i = 0; i < 12000; i++) { const c = {state: i === 11999 ? 'failed' : 'done', agents: []}; deep.agents = [c]; deep = c; }",
        "worstOf([top]); treeRuns([top]); console.log('ok');",
    ])
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-400:]


def test_status_sentence_is_capped_and_agent_rows_stop_at_the_depth_limit():
    SCRIPT = _script(_html())
    assert re.search(r"String\(st\.sentence \|\| \"\"\)\.slice\(0, STATUS_MAX\)", SCRIPT)
    assert re.search(r"kids = depth < WF_DEPTH \? arr\(a\.agents, 12\) : \[\]", SCRIPT)
    assert "if (d > WF_DEPTH) return;" in _fn_src("wfIndex")
