"""apex flow studio single instance: instance.json (0600, atomic), --open reuses a live studio, a
stale file (dead pid / no answer) is replaced, the file goes on a clean exit, and --open targets
are fixed shapes only. Fixture home in tmp_path; no real browser, no real ~/.apex-router."""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
import time

import pytest

from apex_router import cli
from apex_router.studio import flow, server

import studio_fixture as F


@pytest.fixture
def paths(tmp_path, monkeypatch):
    root = tmp_path / "home"
    F.make_home(root, time.time(), telemetry=False)
    for k, v in F.env_for(root).items():
        monkeypatch.setenv(k, v)
    return flow.resolve_paths()


def _wait(cond, t=5.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _running(paths):
    """A studio served in a thread, with its instance file written (as serve() does)."""
    srv = server.make_server(paths=paths)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    server.write_instance(server.instance_path(paths), srv)
    return srv


def _stop(srv):
    srv.shutdown()
    srv.server_close()


def _dead_pid():
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    return p.pid


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Bg:
    """serve() in a thread (it blocks). ``stop()`` shuts it down cleanly (the finally path that
    deletes the instance file), so no test depends on idle-exit timing under load."""

    def __init__(self, paths, **kw):
        self.out, self.opened, self.srv = [], [], []
        kw.setdefault("opener", self.opened.append)
        self.th = threading.Thread(target=server.serve, kwargs=dict(
            paths=paths, emit=self.out.append, on_start=self.srv.append, **kw), daemon=True)
        self.th.start()

    def stop(self):
        if _wait(lambda: self.srv):
            self.srv[0].shutdown()
        self.th.join(10)
        assert not self.th.is_alive()


def test_serve_writes_instance_file_0600_and_deletes_it_on_clean_exit(paths):
    ipath = server.instance_path(paths)
    bg = _Bg(paths, open_target="node:detect")
    out, opened = bg.out, bg.opened
    assert _wait(lambda: out)
    assert stat.S_IMODE(ipath.stat().st_mode) == 0o600
    inst = json.loads(ipath.read_text())
    assert set(inst) == {"pid", "port", "token"} and inst["pid"] == os.getpid()
    assert server.ping(inst["port"], inst["token"])["pid"] == os.getpid()
    assert server.ping(inst["port"], "x" * 32) is None                 # wrong token: no answer
    assert _wait(lambda: out) and out[0].endswith("#node=detect")
    assert opened == [out[0]]
    assert not list(ipath.parent.glob(".instance.json.*.tmp"))          # atomic: no temp left
    bg.stop()
    assert not ipath.exists()                                           # clean exit: file gone


def test_idle_exit_is_a_clean_exit(paths):
    ipath = server.instance_path(paths)
    bg = _Bg(paths, idle_exit_s=0.2)
    assert _wait(lambda: bg.out)
    bg.th.join(10)
    assert not bg.th.is_alive() and not ipath.exists()


def test_open_reuses_the_live_studio(paths, monkeypatch):
    srv = _running(paths)
    made = []
    monkeypatch.setattr(server, "make_server", lambda *a, **k: made.append(1))
    try:
        out, opened = [], []
        rc = server.serve(paths=paths, emit=out.append, opener=opened.append, open_target="node:src.pi")
        assert rc == 0 and made == []                                   # no new server
        want = f"http://127.0.0.1:{srv.port}/?token={srv.token}#node=src.pi"
        assert out == [want] and opened == [want]
        rc = server.serve(paths=paths, emit=out.append, opener=opened.append, open_target="review")
        assert rc == 0 and opened[-1].endswith(f":{srv.port}/?token={srv.token}#review") and made == []
    finally:
        _stop(srv)


@pytest.mark.parametrize("stale", ["dead_pid", "no_answer", "wrong_token", "garbage"])
def test_stale_instance_file_is_replaced(paths, stale):
    ipath = server.instance_path(paths)
    ipath.parent.mkdir(parents=True, exist_ok=True)
    old = {"pid": os.getpid(), "port": _free_port(), "token": "s" * 32}
    other = None
    if stale == "dead_pid":
        old["pid"] = _dead_pid()
    elif stale == "wrong_token":          # a live studio, but not the one the file names
        other = server.make_server(paths=paths)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        old["port"] = other.port
    ipath.write_text("{not json" if stale == "garbage" else json.dumps(old))
    assert server.live_instance(ipath) is None
    bg = _Bg(paths, open_target="branches")
    out = bg.out
    try:
        assert _wait(lambda: out)
        new = json.loads(ipath.read_text())
        assert new["token"] != old["token"] and new["pid"] == os.getpid()
        assert stat.S_IMODE(ipath.stat().st_mode) == 0o600
        assert out[0] == f"http://127.0.0.1:{new['port']}/?token={new['token']}#branches"
        assert server.live_instance(ipath) == new
    finally:
        bg.stop()
        if other:
            _stop(other)
    assert not ipath.exists()


def test_clear_instance_keeps_a_newer_studios_file(paths):
    a, b = _running(paths), _running(paths)          # b wrote last: the file names b
    try:
        ipath = server.instance_path(paths)
        server.clear_instance(ipath, a)
        assert server.read_instance(ipath)["token"] == b.token
        server.clear_instance(ipath, b)
        assert not ipath.exists()
    finally:
        _stop(a)
        _stop(b)


def test_plain_studio_without_open_does_not_reuse(paths, monkeypatch):
    srv = _running(paths)
    try:
        bg = _Bg(paths)
        assert _wait(lambda: bg.out)
        assert f":{srv.port}/" not in bg.out[0] and "#" not in bg.out[0]
        bg.stop()
    finally:
        _stop(srv)


@pytest.mark.parametrize("target,want", [
    ("", ""), ("review", "#review"), ("#review", "#review"), ("branches", "#branches"),
    ("node:src.pi", "#node=src.pi"), ("#node=detect", "#node=detect"), ("node:upstream", "#node=upstream"),
    ("node:sink.xval", "#node=sink.xval"),
    ("node:nope", None), ("node:src.pi&x=1", None), ("#test=hello", None), ("test:x", None),
    ("node:../x", None), ("review ", None), ("javascript:alert(1)", None), ("node:", None)])
def test_target_hash_fixed_shapes_only(target, want):
    assert server.target_hash(target) == want


def test_every_flow_node_is_an_open_target():
    for n in flow.NODES:
        assert server.target_hash(f"node:{n['id']}") == f"#node={n['id']}"


def test_cli_open_passes_target_and_rejects_bad_one(monkeypatch, capsys):
    got = []
    monkeypatch.setattr(server, "serve", lambda **kw: got.append(kw) or 0)
    assert cli.main(["studio", "--idle-exit", "1800", "--open", "node:upstream"]) == 0
    assert got[-1]["open_target"] == "node:upstream" and got[-1]["idle_exit_s"] == 1800
    assert cli.main(["studio", "--open"]) == 0 and got[-1]["open_target"] == ""
    assert cli.main(["studio"]) == 0 and got[-1]["open_target"] is None
    with pytest.raises(SystemExit) as e:
        cli.main(["studio", "--open", "#test=rm -rf"])
    assert e.value.code == 2 and len(got) == 3


def test_second_open_during_a_slow_browser_start_reuses_the_first(paths, monkeypatch):
    # xval P1: the first studio held the lock, opened a slow browser, and only then served; a
    # second --open in that gap saw no ping answer and started a second studio.
    gate = threading.Event()

    def slow_opener(url):
        gate.wait(5)                                   # the browser takes its time
    first = []
    th = threading.Thread(target=server.serve, kwargs=dict(
        paths=paths, emit=lambda s: None, opener=slow_opener, on_start=first.append,
        open_target="node:detect"), daemon=True)
    th.start()
    assert _wait(lambda: first)
    made = []
    real = server.make_server
    monkeypatch.setattr(server, "make_server", lambda *a, **k: made.append(1) or real(*a, **k))
    out, opened = [], []
    rc = server.serve(paths=paths, emit=out.append, opener=opened.append, open_target="review")
    try:
        assert rc == 0 and made == [], "a second studio was started"
        assert opened and f":{first[0].port}/" in opened[0]
    finally:
        gate.set()
        first[0].shutdown()
        th.join(10)


def test_stop_during_a_slow_browser_start_clears_the_file(paths):
    # xval P1 (pass 2): the browser opener blocked outside the cleanup try; a stop in that gap
    # left instance.json behind. Now the opener runs in a thread and every exit clears the file.
    ipath = server.instance_path(paths)
    hold = threading.Event()
    bg = _Bg(paths, opener=lambda url: hold.wait(30))
    assert _wait(lambda: bg.srv) and _wait(ipath.exists)
    bg.stop()                                  # the opener is still blocked
    assert not ipath.exists()
    hold.set()


def test_sigterm_while_the_url_print_blocks_clears_the_file(paths, tmp_path):
    # xval P1 (pass 3): SIGTERM before the handler was set (during emit / flush) killed the
    # process with the file left behind. The handler is now set before anything is written.
    import signal as _signal
    import sys as _sys
    script = tmp_path / "run.py"
    script.write_text(
        "import sys, time\n"
        "from apex_router.studio import server\n"
        "def emit(s):\n"
        "    time.sleep(60)\n"                       # the URL print hangs (a full pipe)
        "server.serve(open_browser=False, emit=emit)\n")
    import apex_router
    from pathlib import Path as _P
    env = dict(os.environ)                     # the fixture home env + this checkout's src
    env["PYTHONPATH"] = str(_P(apex_router.__file__).parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen([_sys.executable, str(script)], env=env)
    ipath = server.instance_path(paths)
    try:
        assert _wait(ipath.exists, 15), "studio did not start"
        proc.send_signal(_signal.SIGTERM)
        proc.wait(15)
        assert not ipath.exists()
    finally:
        if proc.poll() is None:
            proc.kill()



def test_cleanup_ignores_a_second_sigterm_and_clears_the_file_first(paths, monkeypatch):
    # xval P1 (pass 4): a 2nd SIGTERM inside finally escaped before clear_instance. Cleanup now
    # sets SIGTERM to SIG_IGN and deletes the file before the (slow) shutdown wait.
    import signal as _signal
    seen = []
    real_clear = server.clear_instance

    def clear(path, srv):
        seen.append(("clear", _signal.getsignal(_signal.SIGTERM)))
        real_clear(path, srv)
    monkeypatch.setattr(server, "clear_instance", clear)

    def start(srv):
        raise KeyboardInterrupt                  # as a SIGTERM right after the start
    before = _signal.getsignal(_signal.SIGTERM)
    rc = server.serve(paths=paths, open_browser=False, emit=lambda s: None, on_start=start)
    assert rc == 0
    assert seen and seen[0][0] == "clear" and seen[0][1] == _signal.SIG_IGN
    assert not server.instance_path(paths).exists()
    assert _signal.getsignal(_signal.SIGTERM) == before       # the old handler is back
