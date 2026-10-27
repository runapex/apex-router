"""Studio restart-class apply: plist write + restart only when the proxy is idle. A fake `run`
records launchctl/plutil calls (the real launchctl is never called); a fake `fetch` plays the proxy."""
from __future__ import annotations

import json
import plistlib
import time

import pytest

from apex_router.studio import drafts, flow, restart as R

import studio_fixture as F


class FakeProxy:
    """/stats + /healthz of one proxy. `restart()` (called by fake bootstrap) makes a new process."""

    def __init__(self, inflight=0, idle_s=600.0, activity=True, comes_back=True):
        self.inflight, self.idle_s, self.activity, self.comes_back = inflight, idle_s, activity, comes_back
        self.started = time.time() - 3600
        self.up = True
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if not self.up:
            return None
        if url.endswith("/healthz"):
            return {"ok": True}
        st = {"version": "x"}
        if self.activity:
            st["activity"] = {"inflight": self.inflight, "idle_s": self.idle_s,
                              "started_s_ago": round(time.time() - self.started, 1)}
        return st


class FakeRun:
    def __init__(self, proxy: FakeProxy, lint_ok=True, bootstrap_rc=0):
        self.proxy, self.lint_ok, self.bootstrap_rc = proxy, lint_ok, bootstrap_rc
        self.argv = []

    def __call__(self, argv):
        self.argv.append(list(argv))
        if argv[0].endswith("plutil"):
            return (0, "OK") if self.lint_ok else (1, "bad plist")
        if argv[1] == "bootout":
            self.proxy.up = False
            return 0, ""
        if argv[1] == "bootstrap":
            if self.bootstrap_rc == 0:
                self.proxy.up = self.proxy.comes_back
                self.proxy.started = time.time()
            return self.bootstrap_rc, ""
        raise AssertionError(argv)


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    F.make_home(root, time.time())
    for k, v in F.env_for(root).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(R, "_pause", lambda s: None)       # no real waits in bootstrap retries
    return flow.resolve_paths()


def _env(paths):
    return plistlib.loads(paths.plist("main").read_bytes())["EnvironmentVariables"]


def test_gate_fails_closed(home):
    for p, why in [(FakeProxy(inflight=1), "in progress"), (FakeProxy(idle_s=30), "idle 30"),
                   (FakeProxy(activity=False), "older code")]:
        g = R.gate(home, "main", 120, p.fetch)
        assert not g["ready"] and why in g["why"], g
    down = FakeProxy()
    down.up = False
    assert not R.gate(home, "main", 120, down.fetch)["ready"]
    assert R.gate(home, "main", 1, FakeProxy(idle_s=59).fetch)["need_idle_s"] == R.MIN_IDLE_S  # floor


def test_busy_proxy_is_not_restarted_and_plist_untouched(home):
    p = FakeProxy(inflight=2)
    run = FakeRun(p)
    before = home.plist("main").read_bytes()
    with pytest.raises(R.RestartError, match="in progress"):
        R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, run, p.fetch)
    assert home.plist("main").read_bytes() == before
    assert not any(a[0].endswith("launchctl") for a in run.argv)


def test_idle_proxy_restart_writes_plist_keeps_secrets_and_reloads(home):
    p = FakeProxy()
    run = FakeRun(p)
    r = R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, run, p.fetch, health_timeout=2)
    assert r["ok"] and r["changed"] == ["APEX_CONNECT_RETRIES"]
    env = _env(home)
    assert env["APEX_CONNECT_RETRIES"] == "3"
    assert F.FAKE_SECRET in env.values()          # the real value stays in the file (only display masks)
    verbs = [a[1] for a in run.argv if a[0].endswith("launchctl")]
    assert verbs[:2] == ["bootout", "bootstrap"]
    m = json.loads(next(home.backups.glob("*-r/manifest.json")).read_text())
    assert m["complete"] and m["kind"] == "plist"


def test_gate_closing_between_write_and_restart_restores_plist(home):
    p = FakeProxy()
    run = FakeRun(p)
    before = home.plist("main").read_bytes()
    n = {"k": 0}

    def fetch(url):
        if url.endswith("/stats"):
            n["k"] += 1
            if n["k"] == 2:                       # a request starts right after the plist write
                p.inflight = 1
        return p.fetch(url)
    with pytest.raises(R.RestartError, match="plist restored"):
        R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, run, fetch)
    assert home.plist("main").read_bytes() == before
    assert not any(a[0].endswith("launchctl") for a in run.argv)


def test_unhealthy_restart_restores_old_plist_and_reloads_again(home):
    p = FakeProxy(comes_back=False)
    run = FakeRun(p)
    before = home.plist("main").read_bytes()
    with pytest.raises(R.RestartError, match="old plist is restored"):
        R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, run, p.fetch, health_timeout=0.3)
    assert home.plist("main").read_bytes() == before
    assert [a[1] for a in run.argv if a[0].endswith("launchctl")].count("bootstrap") >= 2


def test_lint_failure_writes_nothing(home):
    p = FakeProxy()
    run = FakeRun(p, lint_ok=False)
    before = home.plist("main").read_bytes()
    with pytest.raises(R.RestartError, match="plutil"):
        R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, run, p.fetch)
    assert home.plist("main").read_bytes() == before


def test_old_process_still_answering_is_not_healthy(home):
    # bootstrap "succeeds" but the old process (started an hour ago) still answers: no new process
    p = FakeProxy()
    run = FakeRun(p)
    real_call = run.__call__

    def keep_old(argv):
        rc, out = real_call(argv)
        p.up, p.started = True, time.time() - 3600
        return rc, out
    with pytest.raises(R.RestartError):
        R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, keep_old, p.fetch, health_timeout=0.3)


def test_second_concurrent_restart_is_refused(home):
    import fcntl
    home.studio.mkdir(parents=True, exist_ok=True)
    with open(home.studio / "restart-main.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        with pytest.raises(R.RestartError, match="already running"):
            R.restart(home, "main", {}, 120, FakeRun(FakeProxy()), FakeProxy().fetch)


def test_waiter_waits_for_idle_then_restarts_and_clears_the_draft(home):
    drafts.update_draft(home, {"set": [{"id": "APEX_CONNECT_RETRIES", "value": 3, "proxy": "main"}]})
    ch = drafts.restart_changes(home, "main")
    assert [c["name"] for c in ch] == ["APEX_CONNECT_RETRIES"]
    p = FakeProxy(inflight=1)
    run = FakeRun(p)
    w = R.Waiter()
    done = []
    w.start(home, "main", {c["name"]: c["value"] for c in ch}, 120, 60, run, p.fetch, poll_s=0.01,
            health_timeout=2, on_done=lambda: (done.append(1), drafts.clear_restart_changes(home, "main", [c["key"] for c in ch])))
    time.sleep(0.1)
    assert w.status("main")["state"] == "waiting" and w.busy()
    p.inflight = 0
    for _ in range(200):
        if w.status("main")["state"] != "waiting":
            break
        time.sleep(0.01)
    assert w.status("main")["state"] == "done", w.status("main")
    assert done and not drafts.restart_changes(home, "main")
    assert _env(home)["APEX_CONNECT_RETRIES"] == "3"


def test_waiter_times_out_and_cancel_stops(home):
    p = FakeProxy(inflight=1)
    w = R.Waiter()
    w.start(home, "main", {}, 120, 0.05, FakeRun(p), p.fetch, poll_s=0.01)
    time.sleep(0.2)
    assert w.status("main")["state"] == "timeout"
    w.start(home, "main", {}, 120, 60, FakeRun(p), p.fetch, poll_s=0.01)
    assert w.cancel("main")["state"] in ("cancelled", "waiting")
    time.sleep(0.05)
    assert w.status("main")["state"] == "cancelled"


# ---- server endpoint --------------------------------------------------------------------------------

def test_restart_endpoint_needs_confirm_token_and_never_shows_secrets(home):
    import threading
    from apex_router.studio import server
    from test_studio import call, TOKEN
    srv = server.make_server(port=0, token=TOKEN, paths=home)
    p = FakeProxy()
    srv.restart_run, srv.restart_fetch = FakeRun(p), p.fetch
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        drafts.update_draft(home, {"set": [{"id": "APEX_CONNECT_RETRIES", "value": 3, "proxy": "main"}]})
        st, d, _ = call(srv, "GET", "/api/restart?proxy=main")
        assert st == 200 and d["gate"]["ready"] and F.FAKE_SECRET not in json.dumps(d)
        assert call(srv, "POST", "/api/restart", body={"proxy": "main"})[0] == 400          # no confirm
        assert call(srv, "POST", "/api/restart", body={"proxy": "main", "confirm": "restart com.apex-router.serve"},
                    token="bad")[0] == 403
        st, d, _ = call(srv, "POST", "/api/restart", body={"proxy": "main", "confirm": "restart com.apex-router.serve",
                                                            "idle_s": 1})
        assert st == 200 and d["result"]["ok"], d
        assert _env(home)["APEX_CONNECT_RETRIES"] == "3"
        assert not drafts.restart_changes(home, "main")
        assert F.FAKE_SECRET not in json.dumps(d)
    finally:
        srv.shutdown()
        srv.server_close()
