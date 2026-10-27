"""apex flow studio: API shapes, request checks, secret masking, draft -> apply -> rollback, plan-only
restart settings, empty telemetry and the dry-run test path. Every test uses a fixture home in tmp_path;
nothing reads the real ~/.apex or ~/.apex-router."""
from __future__ import annotations

import http.client
import json
import os
import threading
import time
from pathlib import Path

import pytest

from apex_router.studio import drafts, flow, live, server

import studio_fixture as F

TOKEN = "t" * 32


def _fake_resolver(task, paths):
    return {"family": "opus", "source": "route_table", "confidence": 0.91, "task_type": "debug"}


@pytest.fixture
def studio(tmp_path, monkeypatch):
    root = tmp_path / "home"
    F.make_home(root, time.time())
    for k, v in F.env_for(root).items():
        monkeypatch.setenv(k, v)
    paths = flow.resolve_paths()
    srv = server.make_server(port=0, token=TOKEN, paths=paths, resolver=_fake_resolver)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv, paths, root
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, token=TOKEN, host=None, origin=None, ctype="application/json", raw=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{srv.port}"}
    if token is not None and method != "GET":
        h["X-Studio-Token"] = token
    if token is not None and method == "GET":
        path += ("&" if "?" in path else "?") + "token=" + token
    if origin:
        h["Origin"] = origin
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        h["Content-Type"] = ctype
        h["Content-Length"] = str(len(data))
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    txt = r.read().decode()
    hdrs = dict(r.getheaders())
    c.close()
    try:
        return r.status, json.loads(txt), hdrs
    except ValueError:
        return r.status, txt, hdrs


# ---- request checks ------------------------------------------------------------------------------

def test_page_has_csp_with_nonce_and_no_external_resources(studio):
    srv, _, _ = studio
    st, html, h = call(srv, "GET", "/")
    assert st == 200
    csp = h.get("Content-Security-Policy", "")
    assert "default-src 'none'" in csp and "nonce-" in csp
    # no external resources: no src/href to any URL (the SVG namespace string is not a resource)
    import re
    assert not re.search(r'(?:src|href)\s*=\s*["\']?(?:https?:)?//', html, re.I)
    assert "@import" not in html and "url(http" not in html
    assert h.get("Cache-Control") == "no-store"


@pytest.mark.parametrize("path", ["/", "/api/flow", "/api/live?window=15m", "/api/draft"])
def test_get_without_or_with_wrong_token_is_403(studio, path):
    srv, _, _ = studio
    assert call(srv, "GET", path, token=None)[0] == 403
    assert call(srv, "GET", path, token="wrong")[0] == 403


def test_write_needs_the_header_token_not_the_query(studio):
    srv, _, _ = studio
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    body = json.dumps({"set": [{"id": "family.deep.effort", "value": "low"}]}).encode()
    c.request("PUT", f"/api/draft?token={TOKEN}", body=body,
              headers={"Host": f"127.0.0.1:{srv.port}", "Content-Type": "application/json"})
    assert c.getresponse().status == 403


def test_foreign_host_and_origin_are_rejected(studio):
    # DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends Host: evil.example
    srv, _, _ = studio
    assert call(srv, "GET", "/api/flow", host="evil.example")[0] in (400, 403, 421)
    st, _, _ = call(srv, "PUT", "/api/draft", body={"set": []}, origin="http://evil.example")
    assert st == 403


def test_non_json_and_oversize_bodies_are_rejected(studio):
    srv, _, _ = studio
    assert call(srv, "PUT", "/api/draft", raw=b"x=1", ctype="application/x-www-form-urlencoded")[0] == 415
    assert call(srv, "PUT", "/api/draft", raw=b"{" + b" " * (server.MAX_BODY + 10) + b"}")[0] == 413


def test_server_binds_loopback_only(studio):
    srv, _, _ = studio
    assert srv.server_address[0] == "127.0.0.1"


# ---- flow and live -------------------------------------------------------------------------------

def test_flow_shape_and_secret_masking(studio):
    srv, _, _ = studio
    st, d, _ = call(srv, "GET", "/api/flow")
    assert st == 200
    assert [l["id"] for l in d["lanes"]] == ["sources", "client", "proxy", "upstreams", "sinks"]
    ids = {n["id"] for n in d["nodes"]}
    for e in d["edges"]:
        assert e["from"] in ids and e["to"] in ids
    blob = json.dumps(d)
    assert F.FAKE_SECRET not in blob
    # any secret-like env name shows only set / not set
    for n in d["nodes"]:
        for s in n["settings"]:
            if flow.is_secret(s["name"]):
                assert s["current"]["state"] in ("secret-set", "secret-unset") and s["current"].get("value") is None
    st, rv, _ = call(srv, "GET", "/api/draft")
    assert F.FAKE_SECRET not in json.dumps(rv)


def test_live_numbers_from_fixture_and_empty_home(tmp_path, monkeypatch, studio):
    srv, _, _ = studio
    for w in ("15m", "1h", "24h"):
        st, d, _ = call(srv, "GET", f"/api/live?window={w}")
        assert st == 200 and d["window"] == w
    empty = tmp_path / "empty"
    empty.mkdir()
    p = flow.resolve_paths({"HOME": str(empty), "APEX_STUDIO_LAUNCHAGENTS": str(empty / "LA"),
                            "APEX_STUDIO_MAIN_HOME": str(empty / ".apex"),
                            "APEX_STUDIO_LOCAL_HOME": str(empty / ".apex-local")})
    out = live.collect("15m", "main", paths=p)
    assert isinstance(out, dict)  # missing files give "no data", never an exception
    assert F.FAKE_SECRET not in json.dumps(out)


def test_bad_window_is_rejected(studio):
    srv, _, _ = studio
    assert call(srv, "GET", "/api/live?window=7y")[0] == 400


# ---- draft, apply, rollback ------------------------------------------------------------------------

def test_locked_and_secret_settings_are_rejected_by_the_server(studio):
    srv, _, _ = studio
    st, d, _ = call(srv, "PUT", "/api/draft", body={"set": [{"id": "APEX_POLICY_PATH", "value": "/tmp/x"}]})
    assert st in (400, 403) and "locked" in d["error"]
    st, d, _ = call(srv, "PUT", "/api/draft", body={"set": [{"id": "APEX_ANTHROPIC_UPSTREAM", "value": "http://evil"}]})
    assert st in (400, 403)
    # a hand-edited draft.json with a locked change is refused at apply time too
    _, paths, _ = studio
    dr = drafts.load_draft(paths)
    dr["changes"]["APEX_POLICY_PATH@main"] = {"id": "APEX_POLICY_PATH", "value": "/tmp/x", "proxy": "main"}
    drafts.save_draft(paths, dr)
    st, d, _ = call(srv, "POST", "/api/apply", body={})
    assert st in (400, 403)


def test_bad_values_are_rejected(studio):
    srv, _, _ = studio
    for item in ({"id": "family.deep.effort", "value": "ultra"},
                 {"id": "APEX_CONNECT_RETRIES", "value": 99, "proxy": "main"},
                 {"id": "APEX_CONNECT_RETRIES", "value": "3; rm -rf /", "proxy": "main"}):
        st, _, _ = call(srv, "PUT", "/api/draft", body={"set": [item]})
        assert st == 400, item


def test_apply_writes_live_file_with_backup_and_rollback_restores_it(studio):
    srv, paths, _ = studio
    before = paths.models.read_bytes()
    st, _, _ = call(srv, "PUT", "/api/draft", body={"set": [{"id": "family.deep.effort", "value": "xhigh"}]})
    assert st == 200
    st, r, _ = call(srv, "POST", "/api/apply", body={})
    assert st == 200 and r["applied"] and not r["plans"]
    after = json.loads(paths.models.read_text())
    assert after["pi_families"]["deep"]["effort"] == "xhigh"
    backups = list((paths.router_home / "studio" / "backups").rglob("*"))
    assert any(b.is_file() for b in backups)
    st, r, _ = call(srv, "POST", "/api/rollback", body={})
    assert st == 200
    assert paths.models.read_bytes() == before


def test_restart_setting_gives_a_plan_and_never_writes_the_plist(studio):
    srv, paths, _ = studio
    plist = paths.launch_agents / "com.apex-router.serve.plist"
    before = plist.read_bytes()
    st, _, _ = call(srv, "PUT", "/api/draft",
                    body={"set": [{"id": "APEX_CONNECT_RETRIES", "value": 3, "proxy": "main"}]})
    assert st == 200
    st, r, _ = call(srv, "POST", "/api/apply", body={})
    assert st == 200 and not r["applied"] and r["plans"]
    plan = json.dumps(r["plans"])
    assert "APEX_CONNECT_RETRIES" in plan and "launchctl" in plan
    assert F.FAKE_SECRET not in plan
    assert plist.read_bytes() == before


def test_apply_refuses_invalid_target_json_and_keeps_the_file(studio):
    srv, paths, _ = studio
    st, _, _ = call(srv, "PUT", "/api/draft", body={"set": [{"id": "family.deep.effort", "value": "low"}]})
    assert st == 200
    paths.models.write_text("{not json")          # the file breaks after the draft was made
    st, r, _ = call(srv, "POST", "/api/apply", body={})
    assert st >= 400 or not r.get("applied"), r
    assert paths.models.read_text() == "{not json"


# ---- dry-run test path ---------------------------------------------------------------------------

def test_test_path_is_a_dry_run(studio):
    srv, paths, root = studio
    conf = paths.router_home / "conformance.jsonl"
    n0 = conf.read_text().count("\n") if conf.exists() else 0
    st, d, _ = call(srv, "POST", "/api/test", body={"prompt": ">>auto fix the failing test"})
    assert st == 200 and d["ok"]
    assert d["path"][0] == "src.pi" and d["edges"]
    n1 = conf.read_text().count("\n") if conf.exists() else 0
    assert n1 == n0  # no conformance row for a test prompt
    st, d, _ = call(srv, "POST", "/api/test", body={"prompt": ""})
    assert not d["ok"]


def test_explicit_cue_skips_resolve(studio):
    srv, _, _ = studio
    st, d, _ = call(srv, "POST", "/api/test", body={"prompt": ">>local summarize this"})
    assert st == 200
    assert "resolve" not in d.get("path", [])


# ---- xval pass 1 regressions -----------------------------------------------------------------------

def test_xval1_f3_review_diff_masks_secret_values_in_live_files(studio):
    srv, paths, _ = studio
    m = json.loads(paths.models.read_text())
    m["pi_families"]["deep"]["API_TOKEN"] = F.FAKE_SECRET
    m["pi_families"]["deep"]["note"] = "sk-live-" + "a" * 30
    paths.models.write_text(json.dumps(m, indent=2))
    call(srv, "PUT", "/api/draft", body={"set": [{"id": "family.deep.effort", "value": "low"}]})
    st, d, _ = call(srv, "GET", "/api/draft")
    blob = json.dumps(d)
    assert st == 200 and '"effort": "low"' in blob.replace('\\"', '"')
    assert F.FAKE_SECRET not in blob and "sk-live-" not in blob
    st, d, _ = call(srv, "GET", "/api/flow")
    assert F.FAKE_SECRET not in json.dumps(d)


def _two_file_draft(srv):
    st, _, _ = call(srv, "PUT", "/api/draft", body={"set": [
        {"id": "family.deep.effort", "value": "low"},
        {"id": "route.review.provider", "value": "openai"}]})
    assert st == 200


def test_xval1_f2_failed_apply_restores_every_touched_file(studio, monkeypatch):
    srv, paths, _ = studio
    _two_file_draft(srv)
    before = {p: p.read_bytes() for p in (paths.models, paths.pi_routes)}
    real = drafts._atomic_write
    n = {"target": 0}

    def flaky(path, text, mode=None):
        if Path(path) in before:
            n["target"] += 1
            if n["target"] == 2:          # first file written, second write fails
                raise OSError("disk full")
        return real(path, text, mode)
    monkeypatch.setattr(drafts, "_atomic_write", flaky)
    st, d, _ = call(srv, "POST", "/api/apply", body={})
    assert st == 500 and "nothing changed" in d["error"]
    for p, b in before.items():
        assert p.read_bytes() == b, p


def test_xval1_f2_readback_failure_restores_that_file(studio, monkeypatch):
    srv, paths, _ = studio
    call(srv, "PUT", "/api/draft", body={"set": [{"id": "family.deep.effort", "value": "low"}]})
    before = paths.models.read_bytes()
    real = drafts._atomic_write

    def corrupt(path, text, mode=None):
        if Path(path) == paths.models and '"low"' in text:
            return real(path, text[:-5], mode)    # a short write: read-back differs
        return real(path, text, mode)
    monkeypatch.setattr(drafts, "_atomic_write", corrupt)
    st, _, _ = call(srv, "POST", "/api/apply", body={})
    assert st == 500
    assert paths.models.read_bytes() == before


def test_xval1_f2_backup_set_is_findable_before_the_first_write(studio, monkeypatch):
    srv, paths, _ = studio
    call(srv, "PUT", "/api/draft", body={"set": [{"id": "family.deep.effort", "value": "low"}]})
    seen = {}
    real = drafts._atomic_write

    def spy(path, text, mode=None):
        if Path(path) == paths.models and "sets" not in seen:
            seen["sets"] = drafts._backup_sets(paths)
        return real(path, text, mode)
    monkeypatch.setattr(drafts, "_atomic_write", spy)
    st, _, _ = call(srv, "POST", "/api/apply", body={})
    assert st == 200
    assert seen["sets"], "the manifest must exist before the target file is written"


def test_xval1_f1_rollback_retry_keeps_every_hand_edit(studio, monkeypatch):
    srv, paths, _ = studio
    _two_file_draft(srv)
    assert call(srv, "POST", "/api/apply", body={})[0] == 200
    edit_a = paths.models.read_text().replace('"low"', '"medium"')
    paths.models.write_text(edit_a)
    edit_b = paths.pi_routes.read_text() + "\n"
    paths.pi_routes.write_text(edit_b)
    real = drafts._atomic_write
    n = {"k": 0}

    def flaky(path, text, mode=None):
        if Path(path) in (paths.models, paths.pi_routes):
            n["k"] += 1
            if n["k"] == 2:
                raise OSError("interrupted")
        return real(path, text, mode)
    monkeypatch.setattr(drafts, "_atomic_write", flaky)
    with pytest.raises(OSError):
        drafts.rollback(paths)
    monkeypatch.setattr(drafts, "_atomic_write", real)
    drafts.rollback(paths)                          # the retry
    kept = sorted(paths.backups.rglob("*.pre-rollback.*"))
    texts = {k.read_text() for k in kept}
    assert edit_a in texts and edit_b in texts, [k.name for k in kept]
