"""`apex-router mesh ...` CLI: init/export/import, status, peers/leader/pick from a fixture
peers.json, collect/advise without a network, and `up` with fake netif/beacon/server modules.

All state goes to APEX_MESH_HOME under tmp_path.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import types

import pytest

from apex_router import cli as main_cli
from apex_router.mesh import cli as mesh_cli
from apex_router.mesh import datasets, identity

OTHER = "f" * 32


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "meshhome"
    monkeypatch.setenv("APEX_MESH_HOME", str(h))
    return h


def _run(capsys, *argv):
    rc = main_cli.main(["mesh", *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


def _row(node_id, *, last_seen, started_at=1_700_000_000.0, load=0, roles=(), name="box"):
    return {"node_id": node_id, "name": name, "host": "192.168.1.20", "port": 47778,
            "version": "1.0", "roles": list(roles), "load": load, "started_at": started_at,
            "last_seen": last_seen}


def _write_peers(home, ident, rows, **extra):
    doc = {"self": ident.node_id, "leader": None, "mesh_id": ident.mesh_id, "ts": time.time(),
           "peers": rows}
    doc.update(extra)
    (home / "peers.json").write_text(json.dumps(doc))


@pytest.fixture()
def fixture_mesh(home):
    """Self (young, worker, load 5), an older live peer (worker, load 1), a stale gpu peer."""
    ident = identity.init(name="me")
    now = time.time()
    rows = [_row(ident.node_id, last_seen=now, started_at=now - 10, roles=["worker"], load=5,
                 name="me"),
            _row("1" * 32, last_seen=now - 2, started_at=now - 1000, roles=["worker"], load=1,
                 name="old"),
            _row(OTHER, last_seen=now - 100, started_at=now - 5000, roles=["gpu"], name="stale")]
    _write_peers(home, ident, rows)
    return ident


# ---- init / status --------------------------------------------------------------------------

def test_init_creates_identity_and_never_prints_secret(home, capsys):
    rc, out, _ = _run(capsys, "init", "--name", "alpha")
    assert rc == 0
    ident = identity.load(home)
    assert ident.name == "alpha" and ident.node_id in out and ident.mesh_id in out
    tok = identity.export_secret(ident)
    assert tok not in out and ident.secret.hex() not in out
    rc, out2, _ = _run(capsys, "init")
    assert rc == 0 and identity.load(home) == ident and tok not in out2


def test_init_export_then_import_on_other_home(home, tmp_path, capsys, monkeypatch):
    rc, out, _ = _run(capsys, "init", "--export")
    assert rc == 0
    tok = out.strip()
    assert tok.startswith("apexmesh1:") and identity.parse_secret(tok) == identity.load().secret
    monkeypatch.setenv("APEX_MESH_HOME", str(tmp_path / "other"))
    rc, out, _ = _run(capsys, "init", "--import", tok, "--name", "beta")
    assert rc == 0 and tok not in out
    other = identity.load()
    assert other.secret == identity.load(home).secret and other.node_id != identity.load(home).node_id


def test_init_import_bad_token_and_conflict(home, capsys):
    rc, _, err = _run(capsys, "init", "--import", "apexmesh1:nope")
    assert rc == 2 and "bad token" in err and not (home / "mesh.key").exists()
    _run(capsys, "init")
    tok = identity.export_secret(identity.Identity("0" * 32, "x", b"\x07" * 32))
    rc, _, err = _run(capsys, "init", "--import", tok)
    assert rc == 1 and "force" in err
    rc, _, _ = _run(capsys, "init", "--import", tok, "--force")
    assert rc == 0 and identity.load(home).secret == b"\x07" * 32


def test_status_uninitialized_and_initialized(home, capsys):
    rc, out, _ = _run(capsys, "status", "--json")
    assert rc == 1 and json.loads(out)["initialized"] is False
    ident = identity.init(name="me")
    rc, out, _ = _run(capsys, "status", "--json")
    st = json.loads(out)
    assert rc == 0 and st["initialized"] is True and st["running"] is False
    assert st["node_id"] == ident.node_id and st["mesh_id"] == ident.mesh_id
    assert identity.export_secret(ident) not in out and "secret" not in out
    (home / "mesh.pid").write_text(json.dumps({"pid": os.getpid(), "host": "10.0.0.2",
                                               "port": 47778}))
    fd = mesh_cli._take_lock(home)               # a live `up` holds mesh.lock
    try:
        rc, out, _ = _run(capsys, "status", "--json")
        st = json.loads(out)
        assert st["running"] is True and st["pid"] == os.getpid() and st["bind"] == "10.0.0.2"
        rc, out, _ = _run(capsys, "status")
    finally:
        os.close(fd)
    assert rc == 0 and "running" in out


# ---- peers / leader / pick ------------------------------------------------------------------

def test_peers_drops_stale_entries(fixture_mesh, capsys):
    rc, out, _ = _run(capsys, "peers", "--json")
    view = json.loads(out)
    assert rc == 0 and view["n_alive"] == 2
    assert {p["node_id"] for p in view["peers"]} == {fixture_mesh.node_id, "1" * 32}
    assert view["leader"] == "1" * 32
    rc, out, _ = _run(capsys, "peers")
    assert rc == 0 and "old" in out and "stale" not in out


def test_leader(fixture_mesh, capsys):
    rc, out, _ = _run(capsys, "leader", "--json")
    assert rc == 0 and json.loads(out)["leader"]["node_id"] == "1" * 32


def test_pick_exit_codes(fixture_mesh, capsys):
    rc, out, _ = _run(capsys, "pick", "worker", "--json")
    assert rc == 0 and json.loads(out)["peer"]["node_id"] == "1" * 32   # min load
    rc, out, _ = _run(capsys, "pick", "gpu", "--json")                    # only on the stale peer
    assert rc == 1 and json.loads(out)["peer"] is None
    rc, out, _ = _run(capsys, "pick", "none-such")
    assert rc == 1 and "no alive node" in out


def test_stale_self_or_other_mesh_means_no_view(home, capsys):
    ident = identity.init()
    now = time.time()
    _write_peers(home, ident, [_row(ident.node_id, last_seen=now - 100, roles=["worker"])])
    assert _run(capsys, "pick", "worker")[0] == 1
    assert _run(capsys, "leader")[0] == 1
    rc, out, _ = _run(capsys, "peers", "--json")
    assert rc == 0 and json.loads(out)["peers"] == []
    _write_peers(home, ident, [_row(ident.node_id, last_seen=now, roles=["worker"])],
                 mesh_id="0" * 16)
    assert _run(capsys, "pick", "worker")[0] == 1


def test_commands_need_init(home, capsys):
    for argv in (["peers"], ["leader"], ["pick", "x"], ["collect"], ["advise"]):
        rc, _, err = _run(capsys, *argv)
        assert rc == 2 and "mesh init" in err


# ---- collect / advise (no network: local node only) ------------------------------------------

def test_collect_and_advise_local_only(home, capsys, monkeypatch):
    identity.init(name="me")
    monkeypatch.setattr(datasets, "ALLOWLIST", {
        "pressure": lambda: {"level": "AMBER"},
        "route_rates": lambda: {"code": {"n": 40, "escalated": 4, "rate": 0.1}},
    })
    rc, out, _ = _run(capsys, "collect", "--json")
    res = json.loads(out)
    assert rc == 0 and res["n_nodes"] == 1 and res["n_ok"] == 1
    assert res["pooled"]["pressure"]["level"] == "AMBER"
    assert res["pooled"]["route_rates"]["code"]["n"] == 40
    rc, out, _ = _run(capsys, "collect", "bogus")
    assert rc == 2
    rc, out, _ = _run(capsys, "advise", "--json")
    adv = json.loads(out)
    assert rc == 0 and adv["route_advice_pooled"]["code"]["n"] == 40
    rc, out, _ = _run(capsys, "advise")
    assert rc == 0 and "code" in out and "local node only" in out


# ---- up (fake netif / beacon / server) -------------------------------------------------------

def _fake_modules(monkeypatch, record):
    netif = types.ModuleType("apex_router.mesh.netif")
    netif.choose_bind = lambda bind, allow_public=False: bind or "192.168.1.5"

    class Server:
        def __init__(self, ident, table, card_fn, *, host, port, shared):
            record["server_kw"] = {"host": host, "port": port, "shared": shared}
            self.card_fn = card_fn

        def start(self):
            record["server_started"] = True

        def stop(self):
            record["server_stopped"] = True

        @property
        def address(self):
            return ("192.168.1.5", 50001)

    class Beacon:
        def __init__(self, ident, table, card_fn, *, iface_ip, targets=None):
            record["beacon_kw"] = {"iface_ip": iface_ip, "targets": targets}
            record["card"] = card_fn()

        def start(self):
            record["beacon_started"] = True

        def stop(self):
            record["beacon_stopped"] = True

    server = types.ModuleType("apex_router.mesh.server")
    server.MeshServer = Server
    beacon = types.ModuleType("apex_router.mesh.beacon")
    beacon.Beacon = Beacon
    for mod in (netif, server, beacon):
        monkeypatch.setitem(sys.modules, mod.__name__, mod)


def _mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_up_writes_peers_and_pid_then_stops(home, monkeypatch, capsys):
    ident = identity.init(name="me")
    record: dict = {}
    _fake_modules(monkeypatch, record)
    args = mesh_cli.build_parser().parse_args(
        ["up", "--role", "worker", "--peer", "10.0.0.9:47778", "--share", "pressure"])
    stop = threading.Event()
    rc_box = []
    th = threading.Thread(target=lambda: rc_box.append(mesh_cli.run_up(args, stop, interval_s=0.05)))
    th.start()
    deadline = time.time() + 5
    while time.time() < deadline and not (home / "peers.json").exists():
        time.sleep(0.02)
    time.sleep(0.1)
    try:
        assert _mode(home / "peers.json") == 0o600 and _mode(home / "mesh.pid") == 0o600
        pid = json.loads((home / "mesh.pid").read_text())
        assert pid["pid"] == os.getpid() and pid["port"] == 50001 and pid["shared"] == ["pressure"]
        view = mesh_cli.read_peers(ident, home)
        assert view and view["peers"][0]["port"] == 50001 and view["peers"][0]["roles"] == ["worker"]
        assert _run(capsys, "pick", "worker")[0] == 0
        assert json.loads(_run(capsys, "status", "--json")[1])["running"] is True
    finally:
        stop.set()
        th.join(5)
    assert rc_box == [0]
    assert record["server_kw"] == {"host": "192.168.1.5", "port": 47778,
                                   "shared": frozenset({"pressure"})}
    assert record["beacon_kw"] == {"iface_ip": "192.168.1.5", "targets": [("10.0.0.9", 47778)]}
    assert record["server_stopped"] and record["beacon_stopped"]
    assert not (home / "mesh.pid").exists()
    assert identity.export_secret(ident) not in (home / "peers.json").read_text()


def test_up_port_zero_uses_bound_port(home, monkeypatch):
    identity.init()
    record: dict = {}
    _fake_modules(monkeypatch, record)
    args = mesh_cli.build_parser().parse_args(["up", "--port", "0"])
    stop = threading.Event()
    stop.set()                                   # one cycle, then stop
    assert mesh_cli.run_up(args, stop, interval_s=0.01) == 0
    assert record["server_kw"]["port"] == 0
    doc = json.loads((home / "peers.json").read_text())
    assert doc["peers"][0]["port"] == 50001


def test_up_rejects_bad_peer_and_share(home, monkeypatch, capsys):
    identity.init()
    _fake_modules(monkeypatch, {})
    for argv in (["up", "--peer", "host:notaport"], ["up", "--share", "bogus"], ["up", "--port", "70000"]):
        args = mesh_cli.build_parser().parse_args(argv)
        assert mesh_cli.run_up(args, threading.Event()) == 2


def test_main_help_lists_mesh(capsys):
    with pytest.raises(SystemExit):
        main_cli.main(["--help"])
    assert "mesh" in capsys.readouterr().out
