"""Review fixes F1..F12 for the LAN mesh (one test per finding) and one two-node end-to-end test.

Loopback only. Every node home is under tmp_path (APEX_MESH_HOME).
"""
from __future__ import annotations

import fcntl
import inspect
import json
import os
import select
import socket
import sys
import threading
import time
import types

import pytest

from apex_router import agents
from apex_router import cli as main_cli
from apex_router.mesh import beacon as beacon_mod
from apex_router.mesh import cli as mesh_cli
from apex_router.mesh import collect, datasets, identity, protocol, server
from apex_router.mesh.identity import Identity
from apex_router.mesh.peers import PeerTable
from apex_router.mesh.protocol import MCAST_PORT

LO = "127.0.0.1"
NODE_A, NODE_B = "a" * 32, "b" * 32
SECRET = bytes([7]) * 32
T0 = time.time() - 100


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "meshhome"
    monkeypatch.setenv("APEX_MESH_HOME", str(h))
    return h


def _ident(node=NODE_A):
    return Identity(node_id=node, name="n", secret=SECRET)


def _card(node_id, port=47778, **kw):
    c = {"node_id": node_id, "name": "box", "host": LO, "port": port, "version": "1.0",
         "roles": ["worker"], "load": 0, "started_at": T0}
    c.update(kw)
    return c


def _srv(**kw):
    s = server.MeshServer(_ident(NODE_A), PeerTable(_card(NODE_A)), lambda: _card(NODE_A),
                          host=LO, port=0, **kw)
    s.start()
    return s


def _get(s, path, headers=None):
    import http.client
    host, port = s.address
    c = http.client.HTTPConnection(host, port, timeout=5)
    try:
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def _hold_lock(home):
    """Hold mesh.lock like a live `mesh up` does. Returns the open fd."""
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / "mesh.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _fake_modules(monkeypatch, record):
    netif = types.ModuleType("apex_router.mesh.netif")
    netif.choose_bind = lambda bind, allow_public=False: bind or "192.168.1.5"

    class Server:
        def __init__(self, ident, table, card_fn, *, host, port, shared):
            record["card_fn"] = card_fn

        def start(self):
            pass

        def stop(self):
            pass

        @property
        def address(self):
            return ("192.168.1.5", 50001)

    class Beacon:
        def __init__(self, ident, table, card_fn, *, iface_ip, targets=None):
            record["targets"] = targets

        def start(self):
            pass

        def stop(self):
            pass

    srv_mod = types.ModuleType("apex_router.mesh.server")
    srv_mod.MeshServer = Server
    bcn_mod = types.ModuleType("apex_router.mesh.beacon")
    bcn_mod.Beacon = Beacon
    for mod in (netif, srv_mod, bcn_mod):
        monkeypatch.setitem(sys.modules, mod.__name__, mod)


# ---- F1: absolute per-connection deadline ---------------------------------------------------

def test_f1_slowloris_closed_at_deadline(monkeypatch):
    monkeypatch.setattr(server, "REQUEST_DEADLINE_S", 0.6)
    s = _srv()
    try:
        host, port = s.address
        t0 = time.monotonic()
        closed = False
        with socket.create_connection((host, port), timeout=5) as c:
            c.sendall(b"GET /mesh/v1/card HTTP/1.1\r\n")
            while time.monotonic() - t0 < 4.0:
                try:
                    c.sendall(b"X-A: b\r\n")           # one header line per 0.2 s: never idle
                except OSError:
                    closed = True
                    break
                r, _, _ = select.select([c], [], [], 0.2)
                if r:
                    try:
                        if c.recv(4096) == b"":
                            closed = True
                            break
                    except OSError:
                        closed = True
                        break
        elapsed = time.monotonic() - t0
    finally:
        s.stop()
    assert closed and elapsed < 3.0, elapsed


# ---- F2: auth names the recipient -----------------------------------------------------------

def test_f2_auth_for_other_node_is_401():
    s = _srv()
    try:
        p = "/mesh/v1/peers"
        to_b = protocol.auth_header(_ident(NODE_B), p, NODE_B)       # meant for node B
        assert _get(s, p, {server.AUTH_HEADER: to_b})[0] == 401
        no_to = protocol.encode("auth", {"path": p}, _ident(NODE_B)).decode()
        assert _get(s, p, {server.AUTH_HEADER: no_to})[0] == 401
        to_a = protocol.auth_header(_ident(NODE_B), p, NODE_A)
        assert json.loads(to_a)["body"] == {"path": p, "to": NODE_A}
        assert _get(s, p, {server.AUTH_HEADER: to_a})[0] == 200
    finally:
        s.stop()


# ---- F3: dict keys are scrubbed -------------------------------------------------------------

def test_f3_scrub_drops_bad_keys():
    v = {"ok": 1, "/etc/passwd": 1, "~home": 2, "C:\\x": 3, "a b": 4, "k" * 65: 5,
         "task:code-v1.2": 6, 7: 8, "nested": {"/x": 1, "fine": [{"~y": 1, "z": 2}]}}
    assert datasets.scrub(v) == {"ok": 1, "task:code-v1.2": 6, "7": 8,
                                 "nested": {"fine": [{"z": 2}]}}


# ---- F4: one `up` per home; a refused `up` keeps the live pid file -------------------------

def test_f4_second_up_refused_and_keeps_pid(home, monkeypatch, capsys):
    identity.init(name="me")
    _fake_modules(monkeypatch, {})
    fd = _hold_lock(home)
    live = {"pid": 999_999, "host": "10.0.0.2", "port": 47778}
    (home / "mesh.pid").write_text(json.dumps(live))
    try:
        args = mesh_cli.build_parser().parse_args(["up"])
        stop = threading.Event()
        stop.set()
        assert mesh_cli.run_up(args, stop, interval_s=0.01) == 1
    finally:
        os.close(fd)
    assert "already running" in capsys.readouterr().err
    assert json.loads((home / "mesh.pid").read_text()) == live


def test_f4_up_removes_only_own_pid(home, monkeypatch):
    identity.init(name="me")
    _fake_modules(monkeypatch, {})
    other = {"pid": 999_999}
    real_write = mesh_cli._write_json

    def write(path, obj):
        real_write(path, obj)
        if path.name == "peers.json":                 # someone replaced the pid file
            real_write(home / "mesh.pid", other)

    monkeypatch.setattr(mesh_cli, "_write_json", write)
    stop = threading.Event()
    stop.set()
    assert mesh_cli.run_up(mesh_cli.build_parser().parse_args(["up"]), stop) == 0
    assert json.loads((home / "mesh.pid").read_text()) == other


# ---- F5: status tests the lock, not the pid -------------------------------------------------

def test_f5_status_uses_lock_not_pid(home, capsys):
    identity.init(name="me")
    (home / "mesh.pid").write_text(json.dumps({"pid": os.getpid(), "host": "10.0.0.2",
                                               "port": 47778}))
    assert main_cli.main(["mesh", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["running"] is False   # reused pid, no lock
    fd = _hold_lock(home)
    try:
        assert main_cli.main(["mesh", "status", "--json"]) == 0
        st = json.loads(capsys.readouterr().out)
    finally:
        os.close(fd)
    assert st["running"] is True and st["pid"] == os.getpid() and st["bind"] == "10.0.0.2"


# ---- F6: load = queue_running ---------------------------------------------------------------

def test_f6_load_is_queue_running(monkeypatch):
    monkeypatch.setattr(agents, "worker", lambda: {"running": True, "queue_running": 3})
    assert mesh_cli._local_load() == 3
    monkeypatch.setattr(agents, "worker", lambda: {"running": True})
    assert mesh_cli._local_load() == 0


# ---- F7: /card does not compute load --------------------------------------------------------

def test_f7_card_fn_uses_cached_load(home, monkeypatch):
    identity.init(name="me")
    record: dict = {}
    _fake_modules(monkeypatch, record)
    calls = []
    monkeypatch.setattr(mesh_cli, "_local_load", lambda: calls.append(1) or 4)
    stop = threading.Event()
    stop.set()
    assert mesh_cli.run_up(mesh_cli.build_parser().parse_args(["up"]), stop) == 0
    n = len(calls)
    cards = [record["card_fn"]() for _ in range(50)]
    assert len(calls) == n and n <= 2
    assert all(c["load"] == 4 for c in cards)


# ---- F8: --peer HOST or HOST:PORT, default UDP beacon port ----------------------------------

def test_f8_peer_host_default_beacon_port(home, monkeypatch, capsys):
    identity.init(name="me")
    record: dict = {}
    _fake_modules(monkeypatch, record)
    argv = ["up", "--peer", "10.0.0.9", "--peer", "10.0.0.8:5000", "--peer", "[fe80::1]",
            "--peer", "[fe80::2]:6000"]
    stop = threading.Event()
    stop.set()
    assert mesh_cli.run_up(mesh_cli.build_parser().parse_args(argv), stop) == 0
    assert record["targets"] == [("10.0.0.9", MCAST_PORT), ("10.0.0.8", 5000),
                                 ("fe80::1", MCAST_PORT), ("fe80::2", 6000)]
    for bad in ("10.0.0.9:0", "10.0.0.9:x", ":5000", "[fe80::1]:"):
        args = mesh_cli.build_parser().parse_args(["up", "--peer", bad])
        assert mesh_cli.run_up(args, threading.Event()) == 2, bad
    with pytest.raises(SystemExit):
        mesh_cli.build_parser().parse_args(["up", "--help"])
    assert "UDP beacon port" in " ".join(capsys.readouterr().out.split())


# ---- F9: total collect deadline -------------------------------------------------------------

def test_f9_collect_total_deadline(home):
    stop = threading.Event()
    lsock = socket.socket()
    lsock.bind((LO, 0))
    lsock.listen(4)
    port = lsock.getsockname()[1]

    def trickle():
        try:
            conn, _ = lsock.accept()
        except OSError:
            return
        with conn:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            end = time.monotonic() + 6.0              # bound: the test never hangs
            while not stop.wait(0.2) and time.monotonic() < end:   # 1 byte / 0.2 s
                try:
                    conn.sendall(b"X")
                except OSError:
                    return

    th = threading.Thread(target=trickle, daemon=True)
    th.start()
    table = PeerTable(_card(NODE_A))
    table.update(_card(NODE_B, port=port))
    try:
        t0 = time.monotonic()
        res = collect.collect(table, _ident(NODE_A), ["pressure"], include_self=False,
                              timeout=1.0)
        elapsed = time.monotonic() - t0
    finally:
        stop.set()
        lsock.close()
        th.join(5)
    assert elapsed < 2.5, elapsed
    assert res["nodes"][0]["ok"] is False and res["nodes"][0]["error"] == "timeout"


# ---- F10: self read follows --share ---------------------------------------------------------

def test_f10_self_read_uses_pid_shared(home, monkeypatch):
    monkeypatch.setattr(datasets, "ALLOWLIST", {"pressure": lambda: {"level": "GREEN"},
                                                "worker": lambda: {"inbox": 1}})
    table = PeerTable(_card(NODE_A))
    res = collect.collect(table, _ident(NODE_A), ["pressure", "worker"])
    assert res["nodes"][0]["data"] == {"pressure": {"level": "GREEN"}, "worker": {"inbox": 1}}
    home.mkdir(parents=True, exist_ok=True)
    (home / mesh_cli.PID_FILE).write_text(json.dumps({"pid": 1, "shared": ["pressure"]}))
    res = collect.collect(table, _ident(NODE_A), ["pressure", "worker"])
    assert res["nodes"][0]["data"] == {"pressure": {"level": "GREEN"},
                                       "worker": {"error": "not_shared"}}


# ---- F11: clean start errors; loop survives a peers.json write error ------------------------

def test_f11_start_error_one_line(home, monkeypatch, capsys):
    def boom(args, stop):
        raise RuntimeError("bad thing")
    monkeypatch.setattr(mesh_cli, "run_up", boom)
    assert main_cli.main(["mesh", "up"]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "cannot start" in err and "Traceback" not in err


def test_f11_peers_write_error_logged_loop_continues(home, monkeypatch, capsys):
    identity.init(name="me")
    _fake_modules(monkeypatch, {})
    real_write = mesh_cli._write_json
    fails = {"n": 0}

    def write(path, obj):
        if path.name == "peers.json" and fails["n"] < 2:
            fails["n"] += 1
            raise OSError("disk full")
        real_write(path, obj)

    monkeypatch.setattr(mesh_cli, "_write_json", write)
    stop = threading.Event()
    rc_box = []
    th = threading.Thread(target=lambda: rc_box.append(
        mesh_cli.run_up(mesh_cli.build_parser().parse_args(["up"]), stop, interval_s=0.02)))
    th.start()
    deadline = time.time() + 5
    while time.time() < deadline and not (home / "peers.json").exists():
        time.sleep(0.02)
    stop.set()
    th.join(5)
    assert rc_box == [0] and (home / "peers.json").exists()
    assert "peers.json" in capsys.readouterr().err


# ---- F12: one auth_header -------------------------------------------------------------------

def test_f12_one_auth_header_in_protocol():
    assert "def auth_header" not in inspect.getsource(collect)
    assert "def auth_header" not in inspect.getsource(server)
    assert server.auth_header is protocol.auth_header
    env = json.loads(protocol.auth_header(_ident(NODE_A), "/p", NODE_B))
    assert env["kind"] == "auth" and env["body"] == {"path": "/p", "to": NODE_B}


# ---- end to end: two real `run_up` nodes on 127.0.0.1 ---------------------------------------

def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((LO, 0))
        return s.getsockname()[1]


def test_e2e_two_nodes_peer_collect_pick(tmp_path, monkeypatch, capsys):
    ha, hb = tmp_path / "a", tmp_path / "b"
    ia = identity.init(ha, name="node-a")
    ib = identity.init(hb, secret=ia.secret, name="node-b")
    pa, pb = _free_udp_port(), _free_udp_port()
    ports = {"node-a": pa, "node-b": pb}
    loads = {"node-a": 5, "node-b": 1}

    class TestBeacon(beacon_mod.Beacon):      # one host: each node needs its own UDP port
        def __init__(self, *a, **kw):
            kw.setdefault("port", ports[threading.current_thread().name])
            kw.setdefault("interval_s", 0.1)
            super().__init__(*a, **kw)

    monkeypatch.setattr(beacon_mod, "Beacon", TestBeacon)
    monkeypatch.setattr(mesh_cli, "_local_load",
                        lambda: loads.get(threading.current_thread().name, 0))
    monkeypatch.setattr(datasets, "ALLOWLIST", {
        "pressure": lambda: {"level": "AMBER"},
        "route_rates": lambda: {"code": {"n": 10, "escalated": 1}},
    })
    stops, threads, rcs = {}, [], {}

    def start(name, h, peer_port):
        monkeypatch.setenv("APEX_MESH_HOME", str(h))
        args = mesh_cli.build_parser().parse_args(
            ["up", "--bind", LO, "--port", "0", "--role", "worker",
             "--peer", f"{LO}:{peer_port}"])
        stops[name] = threading.Event()
        th = threading.Thread(name=name, daemon=True, target=lambda: rcs.__setitem__(
            name, mesh_cli.run_up(args, stops[name], interval_s=0.1)))
        th.start()
        threads.append(th)
        end = time.time() + 5
        while time.time() < end and not (h / "peers.json").exists():
            time.sleep(0.02)

    start("node-a", ha, pb)
    start("node-b", hb, pa)
    try:
        def both(h, ident):
            v = mesh_cli.read_peers(ident, h)
            return v is not None and v["n_alive"] == 2
        end = time.time() + 10
        while time.time() < end and not (both(ha, ia) and both(hb, ib)):
            time.sleep(0.05)
        assert both(ha, ia) and both(hb, ib)
        monkeypatch.setenv("APEX_MESH_HOME", str(ha))
        capsys.readouterr()
        assert main_cli.main(["mesh", "collect", "--json", "--timeout", "3"]) == 0
        res = json.loads(capsys.readouterr().out)
        assert res["n_nodes"] == 2 and res["n_ok"] == 2, res
        assert res["pooled"]["route_rates"]["code"]["n"] == 20
        assert res["pooled"]["pressure"]["level"] == "AMBER"
        assert main_cli.main(["mesh", "pick", "worker", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["peer"]["node_id"] == ib.node_id
    finally:
        for ev in stops.values():
            ev.set()
        for th in threads:
            th.join(10)
    assert rcs == {"node-a": 0, "node-b": 0}
    assert not (ha / "mesh.pid").exists() and not (hb / "mesh.pid").exists()
