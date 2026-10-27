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

    def __init__(self, inflight=0, idle_s=600.0, activity=True, comes_back=True, drain_ok=True):
        self.inflight, self.idle_s, self.activity, self.comes_back = inflight, idle_s, activity, comes_back
        self.drain_ok = drain_ok                  # False = an older proxy without pid/draining
        self.started = time.time() - 3600
        self.up = True
        self.pid, self.draining = 4242, False
        self.calls = []
        self.on_stats = None                      # hook: called before each /stats answer

    def fetch(self, url):
        self.calls.append(url)
        if not self.up:
            return None
        if url.endswith("/healthz"):
            return {"ok": True}
        if self.on_stats:
            self.on_stats(self)
        st = {"version": "x"}
        if self.activity:
            st["activity"] = {"inflight": self.inflight, "idle_s": self.idle_s,
                              "started_s_ago": round(time.time() - self.started, 1)}
            if self.drain_ok:
                st["activity"].update(pid=self.pid, draining=self.draining)
        return st


class FakeRun:
    def __init__(self, proxy: FakeProxy, lint_ok=True, bootstrap_rc=0):
        self.proxy, self.lint_ok, self.bootstrap_rc = proxy, lint_ok, bootstrap_rc
        self.argv = []

    def __call__(self, argv):
        self.argv.append(list(argv))
        if argv[0].endswith("plutil"):
            return (0, "OK") if self.lint_ok else (1, "bad plist")
        if argv[0] == "/bin/kill":
            assert int(argv[2]) == self.proxy.pid
            self.proxy.draining = argv[1] == "-USR1"
            return 0, ""
        if argv[1] == "print":
            return (0, f"state = running\n\tpid = {self.proxy.pid}\n") if self.proxy.up else (113, "")
        if argv[1] == "bootout":
            self.proxy.up = False
            return 0, ""
        if argv[1] == "bootstrap":
            if self.bootstrap_rc == 0:
                self.proxy.up = self.proxy.comes_back
                self.proxy.started = time.time()
                self.proxy.pid += 1
                self.proxy.draining = False
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
    v = _verbs(run)
    assert v.index("print") < v.index("kill-USR1") < v.index("bootout") < v.index("bootstrap")
    m = json.loads(next(home.backups.glob("*-r/manifest.json")).read_text())
    assert m["complete"] and m["kind"] == "plist"


def _verbs(run):
    return [a[1] if a[0].endswith("launchctl") else a[0].rsplit("/", 1)[-1] + a[1] for a in run.argv
            if not a[0].endswith("plutil")]


def test_drain_comes_before_bootout_and_waits_for_a_late_request(home):
    # a request starts after the idle gate (the race codex found): the drain waits for it to end
    p = FakeProxy()
    run = FakeRun(p)
    seen = {"n": 0}

    def late(px):
        seen["n"] += 1
        if px.draining and seen["n"] == 2:
            px.inflight = 1                       # it started just before the drain
        elif px.draining and seen["n"] > 3:
            px.inflight = 0
        assert not (px.inflight and not px.up)
    p.on_stats = late
    r = R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, run, p.fetch, health_timeout=2)
    assert r["ok"]
    v = _verbs(run)
    assert v.index("kill-USR1") < v.index("bootout") < v.index("bootstrap")
    assert seen["n"] >= 4                         # it polled until inflight was 0


def test_drain_that_does_not_finish_ends_the_drain_and_restores_plist(home, monkeypatch):
    monkeypatch.setattr(R, "DRAIN_WAIT_S", 0.05)
    p = FakeProxy()
    run = FakeRun(p)
    p.on_stats = lambda px: setattr(px, "inflight", 1 if px.draining else 0)
    before = home.plist("main").read_bytes()
    monkeypatch.setattr(R, "_drain", lambda *a, **k: R.__dict__["_drain_orig"](*a, wait_s=0.05, **k))
    monkeypatch.setitem(R.__dict__, "_drain_orig", _drain_orig)
    with pytest.raises(R.RestartError, match="did not finish.*plist restored"):
        R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, run, p.fetch)
    assert home.plist("main").read_bytes() == before
    assert "bootout" not in _verbs(run) and not p.draining          # USR2 ended the drain


_drain_orig = R._drain


def test_older_proxy_without_drain_is_not_restarted(home):
    p = FakeProxy(drain_ok=False)
    run = FakeRun(p)
    before = home.plist("main").read_bytes()
    with pytest.raises(R.RestartError, match="cannot drain"):
        R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, run, p.fetch)
    assert home.plist("main").read_bytes() == before and "bootout" not in _verbs(run)


def test_port_owned_by_another_process_is_not_signalled(home):
    p = FakeProxy()
    run = FakeRun(p)
    real = run.__call__

    def other(argv):
        if argv[1:2] == ["print"]:
            return 0, "pid = 999\n"
        return real(argv)
    with pytest.raises(R.RestartError, match="not com.apex-router.serve"):
        R.restart(home, "main", {}, 120, other, p.fetch)
    assert not any(a[0] == "/bin/kill" for a in run.argv)


def test_exception_after_bootout_restores_plist(home):
    p = FakeProxy()
    run = FakeRun(p)
    before = home.plist("main").read_bytes()
    real = run.__call__

    def boom(argv):
        if argv[1:2] == ["bootstrap"]:
            raise TimeoutError("launchctl hung")
        return real(argv)
    with pytest.raises(R.RestartError, match="TimeoutError.*old plist is restored"):
        R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, boom, p.fetch)
    assert home.plist("main").read_bytes() == before
    m = json.loads(next(home.backups.glob("*-r/manifest.json")).read_text())
    assert m["rolled_back"] and "TimeoutError" in m["aborted"]


def test_rollback_drains_a_new_process_that_already_answers(home):
    # new process answers /healthz but never proves it is new: the rollback must drain it first
    p = FakeProxy()
    run = FakeRun(p)
    real = run.__call__

    def keep_old_start(argv):
        rc, out = real(argv)
        if argv[1:2] == ["bootstrap"]:
            p.started = time.time() - 3600          # looks like an old process: health fails
        return rc, out
    with pytest.raises(R.RestartError):
        R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, keep_old_start, p.fetch, health_timeout=0.2)
    v = _verbs(run)
    second_bootout = [i for i, x in enumerate(v) if x == "bootout"][1]
    assert "kill-USR1" in v[v.index("bootstrap"):second_bootout]


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


def test_pid_line_far_down_in_launchctl_print_is_found():
    out = "x = y\n" * 2000 + "\tpid = 4242\n"
    assert R._launchd_pid("l", lambda argv: (0, out)) == 4242


def test_rollback_with_unreachable_new_process_does_not_reload_without_drain(home):
    # the new process has a pid but its /stats does not answer: no bootout of it (fail closed)
    p = FakeProxy()
    run = FakeRun(p)
    real = run.__call__
    state = {"booted": False}

    def boot(argv):
        rc, out = real(argv)
        if argv[1:2] == ["bootstrap"]:
            state["booted"] = True
            p.up = False                                # answers nothing, but launchd has a pid
        if argv[1:2] == ["print"] and state["booted"]:
            return 0, f"pid = {p.pid}\n"
        return rc, out
    with pytest.raises(R.RestartError, match="NOT confirmed healthy"):
        R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, boot, p.fetch, health_timeout=0.2)
    v = _verbs(run)
    assert v.count("bootout") == 1                      # only the first, drained one


@pytest.mark.parametrize("fail", ["rc", "raise"])
def test_rollback_with_unknown_launchctl_state_does_not_reload(home, fail):
    p = FakeProxy()
    run = FakeRun(p)
    real = run.__call__
    state = {"booted": False}

    def boot(argv):
        if argv[1:2] == ["print"] and state["booted"]:
            if fail == "raise":
                raise TimeoutError("launchctl hung")
            return 5, "Input/output error"
        rc, out = real(argv)
        if argv[1:2] == ["bootstrap"]:
            state["booted"] = True
            p.started = time.time() - 3600          # never proves it is new: health fails
        return rc, out
    with pytest.raises(R.RestartError, match="NOT confirmed healthy"):
        R.restart(home, "main", {"APEX_CONNECT_RETRIES": "3"}, 120, boot, p.fetch, health_timeout=0.2)
    assert _verbs(run).count("bootout") == 1


def test_launchd_pid_states():
    assert R._launchd_pid("l", lambda a: (113, "Could not find service")) is None
    assert R._launchd_pid("l", lambda a: (0, "state = waiting\n")) is None
    assert R._launchd_pid("l", lambda a: (5, "")) == R.UNKNOWN
