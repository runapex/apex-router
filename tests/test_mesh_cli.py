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
    assert "per node" in out and "n=40" in out
    rc, out, _ = _run(capsys, "advise", "--json")
    assert json.loads(out)["per_node"]


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
        def __init__(self, ident, table, card_fn, *, iface_ip, port, targets=None):
            record["beacon_kw"] = {"iface_ip": iface_ip, "port": port, "targets": targets}
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
    assert record["beacon_kw"] == {"iface_ip": "192.168.1.5", "port": 47777,
                                   "targets": [("10.0.0.9", 47778)]}
    assert pid["beacon_port"] == 47777
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
    for argv in (["up", "--peer", "host:notaport"], ["up", "--share", "bogus"], ["up", "--port", "70000"],
                 ["up", "--beacon-port", "0"], ["up", "--beacon-port", "70000"]):
        args = mesh_cli.build_parser().parse_args(argv)
        assert mesh_cli.run_up(args, threading.Event()) == 2


def test_up_beacon_port_for_beacon_and_bare_peers(home, monkeypatch):
    identity.init()
    record: dict = {}
    _fake_modules(monkeypatch, record)
    args = mesh_cli.build_parser().parse_args(
        ["up", "--beacon-port", "47787", "--peer", "10.0.0.9", "--peer", "10.0.0.8:47777"])
    stop = threading.Event()
    stop.set()                                   # one cycle, then stop
    assert mesh_cli.run_up(args, stop, interval_s=0.01) == 0
    assert record["beacon_kw"]["port"] == 47787
    assert record["beacon_kw"]["targets"] == [("10.0.0.9", 47787), ("10.0.0.8", 47777)]


def test_parse_peer_default_port():
    assert mesh_cli._parse_peer("h") == ("h", 47777)
    assert mesh_cli._parse_peer("h", 47787) == ("h", 47787)
    assert mesh_cli._parse_peer("h:1", 47787) == ("h", 1)
    assert mesh_cli._parse_peer("fe80::1", 47787) == ("fe80::1", 47787)
    assert mesh_cli._parse_peer("[fe80::1]:9", 47787) == ("fe80::1", 9)


def test_main_help_lists_mesh(capsys):
    with pytest.raises(SystemExit):
        main_cli.main(["--help"])
    assert "mesh" in capsys.readouterr().out


def test_advise_prints_start_tier_split(capsys, monkeypatch):
    from apex_router.mesh import cli as mesh_cli
    rates = {"code": {"n": 12, "escalated": 3, "rate": 0.25, "by_tier": {
        "opus": {"n": 10, "escalated": 1, "rate": 0.1},
        "sonnet": {"n": 2, "escalated": 2, "rate": 1.0}}},
        "docs": {"n": 3, "escalated": 0, "rate": 0.0}}
    res = {"n_nodes": 1, "n_ok": 1, "pooled": {"route_rates": rates, "route_advice_pooled": {
        "code": {"verdict": "inconclusive", "n": 12, "rate": 0.25, "ci_low": 0.1, "ci_high": 0.5},
        "docs": {"verdict": "inconclusive", "n": 3, "rate": 0.0, "ci_low": 0.0, "ci_high": 0.5}}}}
    monkeypatch.setattr(mesh_cli, "_run_collect", lambda names, timeout: (res, True))
    rc = mesh_cli._cmd_advise(type("A", (), {"json": False, "timeout": 1})())
    out = capsys.readouterr().out
    assert rc == 0
    assert "    start opus" in out and "n=10" in out
    assert "    start sonnet" in out and "rate=1.000" in out
    assert out.count("start ") == 2  # docs has no split -> no extra rows


# ---- xval -----------------------------------------------------------------------------------

def test_xval_verdict_last_line_wins():
    assert mesh_cli.xval_verdict("x\nVERDICT: AGREE\nmore\n**VERDICT: DISAGREE**\nDONE") == "DISAGREE"
    assert mesh_cli.xval_verdict("VERDICT: PARTIAL\nDONE") == "PARTIAL"
    assert mesh_cli.xval_verdict("no verdict here\nDONE") is None
    assert mesh_cli.xval_verdict("I verdict: agree") is None


def test_build_xval_task_fences_artifacts():
    t = mesh_cli.build_xval_task("Is the score right?", [("a.md", "alpha\n"), ("git diff HEAD", "+x")])
    assert t.startswith(mesh_cli.XVAL_BRIEF) and "QUESTION: Is the score right?" in t
    assert "===== BEGIN ARTIFACT: a.md =====\nalpha\n===== END ARTIFACT: a.md =====" in t
    assert "BEGIN ARTIFACT: git diff HEAD" in t and "VERDICT: AGREE" in t
    assert "QUESTION: Is this work correct" in mesh_cli.build_xval_task("", [("s", "x")])


def _xval_env(monkeypatch, home, verdict_report, state="done"):
    identity.init(name="me")
    from apex_router.mesh import sender
    seen: dict = {}
    monkeypatch.setattr(sender, "resolve_to", lambda spec, rows: "b" * 32)

    def fake_submit(ident, **kw):
        seen.update(kw)
        return "c" * 16
    monkeypatch.setattr(sender, "submit", fake_submit)

    def fake_result(args):
        seen["result_args"] = args
        print(json.dumps({"id": args.id, "state": state, "files": {"report": "r.md"},
                          "result": {"status": state, "report": verdict_report}}))
        return 0
    monkeypatch.setattr(mesh_cli, "_cmd_result", fake_result)
    return seen


def test_xval_submits_file_and_maps_verdict_to_exit(home, monkeypatch, capsys, tmp_path):
    f = tmp_path / "report.md"
    f.write_text("the claim: 2 + 2 = 5\n")
    seen = _xval_env(monkeypatch, home, "finding 1\nVERDICT: DISAGREE\nDONE")
    rc, out, _ = _run(capsys, "xval", "--to", "node-b", "--file", str(f), "--question", "Is 2+2=5?",
                      "--out-dir", str(tmp_path / "o"))
    assert rc == 1 and "verdict DISAGREE" in out and "finding 1" in out
    assert seen["repo"] == "xval" and seen["ref"] == "main" and seen["max_effort"] == "high"
    assert "QUESTION: Is 2+2=5?" in seen["task"] and "2 + 2 = 5" in seen["task"]
    assert seen["result_args"].wait is True and seen["result_args"].out_dir == str(tmp_path / "o")


@pytest.mark.parametrize("report,state,rc", [("VERDICT: AGREE\nDONE", "done", 0),
                                             ("VERDICT: PARTIAL\nDONE", "done", 1),
                                             ("no verdict\nDONE", "done", 3),
                                             ("VERDICT: AGREE\nDONE", "failed", 3)])
def test_xval_exit_codes(home, monkeypatch, capsys, tmp_path, report, state, rc):
    f = tmp_path / "w.md"
    f.write_text("work")
    _xval_env(monkeypatch, home, report, state)
    assert _run(capsys, "xval", "--to", "node-b", "--file", str(f), "--json")[0] == rc


def test_xval_usage_errors(home, monkeypatch, capsys, tmp_path):
    identity.init(name="me")
    assert _run(capsys, "xval", "--to", "node-b")[0] == 2                      # nothing to review
    big = tmp_path / "big.md"
    from apex_router.mesh import jobs
    big.write_text("x" * (jobs.MAX_TASK_BYTES + 1))
    rc, _, err = _run(capsys, "xval", "--to", "node-b", "--file", str(big))
    assert rc == 2 and "limit" in err
    assert _run(capsys, "xval", "--to", "node-b", "--file", str(tmp_path / "missing.md"))[0] == 2


def test_up_restarts_when_an_update_lands(home, monkeypatch):
    identity.init()
    record: dict = {}
    _fake_modules(monkeypatch, record)
    ticks = []

    class FakeUpdater:
        def __init__(self, h):
            record["updater_home"] = h

        def card_build(self):
            return {"commit": "c" * 40, "update": "updated", "caps": ["bundle", "update"]}

        def tick(self, peers):
            ticks.append(len(list(peers)))

        def restart_needed(self):
            return len(ticks) >= 2

    su = types.ModuleType("apex_router.mesh.selfupdate")
    su.AutoUpdater = FakeUpdater
    monkeypatch.setitem(sys.modules, su.__name__, su)
    args = mesh_cli.build_parser().parse_args(["up"])
    assert args.auto_update is True
    assert mesh_cli.run_up(args, threading.Event(), interval_s=0.01) == mesh_cli.RESTART
    assert record["card"]["build"]["commit"] == "c" * 40
    assert record["server_stopped"] and record["beacon_stopped"]
    assert not (home / "mesh.pid").exists()
    off = mesh_cli.build_parser().parse_args(["up", "--no-auto-update"])
    stop = threading.Event()
    stop.set()
    assert mesh_cli.run_up(off, stop, interval_s=0.01) == 0


# ---- reliability: address drift, bind retry (2026-10-22) ---------------------------------------

def _netif_rows(monkeypatch, rows, lan=None):
    mod = sys.modules["apex_router.mesh.netif"]
    from apex_router.mesh import netif as real
    mod._iface_addrs = lambda text=None: rows() if callable(rows) else rows
    mod._parse = real._parse
    mod.is_private = real.is_private
    mod.lan_ip = lambda default=None, ifaces=None: lan() if callable(lan) else lan


def test_up_restarts_when_its_address_is_gone(home, monkeypatch, capsys):
    identity.init()
    record: dict = {}
    _fake_modules(monkeypatch, record)
    monkeypatch.setattr(mesh_cli, "ADDR_CHECK_EVERY", 1)
    _netif_rows(monkeypatch, [("en0", "10.3.203.90", True)], lan="10.3.203.90")
    args = mesh_cli.build_parser().parse_args(["up", "--bind", "10.0.0.84", "--no-auto-update"])
    stop = threading.Event()
    threading.Timer(2.0, stop.set).start()                         # a regression fails, not hangs
    assert mesh_cli.run_up(args, stop, interval_s=0.01) == mesh_cli.RESTART
    assert "address_gone (10.0.0.84); restarting" in capsys.readouterr().out
    assert record["server_stopped"] and not (home / "mesh.pid").exists()


def test_up_follows_a_new_lan_address_only_without_bind(home, monkeypatch, capsys):
    identity.init()
    record: dict = {}
    _fake_modules(monkeypatch, record)
    monkeypatch.setattr(mesh_cli, "ADDR_CHECK_EVERY", 1)
    rows = [("en0", "192.168.1.5", True), ("en1", "10.9.9.9", True)]
    _netif_rows(monkeypatch, rows, lan="10.9.9.9")              # VPN off: lan_ip moved
    args = mesh_cli.build_parser().parse_args(["up", "--no-auto-update"])
    stop = threading.Event()
    threading.Timer(2.0, stop.set).start()
    assert mesh_cli.run_up(args, stop, interval_s=0.01) == mesh_cli.RESTART
    assert "address_changed" in capsys.readouterr().out
    # --bind given and still present: no restart
    pinned = mesh_cli.build_parser().parse_args(["up", "--bind", "192.168.1.5", "--no-auto-update"])
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    assert mesh_cli.run_up(pinned, stop, interval_s=0.01) == 0


def test_up_does_not_restart_on_one_flap_or_missing_data(home, monkeypatch):
    identity.init()
    record: dict = {}
    _fake_modules(monkeypatch, record)
    monkeypatch.setattr(mesh_cli, "ADDR_CHECK_EVERY", 1)
    seq = iter([[("en0", "10.3.203.90", True)]] + [[("en0", "192.168.1.5", True)]] * 10_000)
    _netif_rows(monkeypatch, lambda: next(seq), lan="192.168.1.5")   # gone once, then back
    args = mesh_cli.build_parser().parse_args(["up", "--no-auto-update"])
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    assert mesh_cli.run_up(args, stop, interval_s=0.01) == 0
    _netif_rows(monkeypatch, [], lan=None)                           # ifconfig failed
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    assert mesh_cli.run_up(args, stop, interval_s=0.01) == 0


def _cmd_up_with(monkeypatch, outcomes):
    """Patch run_up to return or raise each item of outcomes in turn; return the call log."""
    calls = []

    def fake(args, stop, **kw):
        calls.append(1)
        o = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(o, BaseException):
            raise o
        return o

    monkeypatch.setattr(mesh_cli, "run_up", fake)
    waits = []
    real_event = threading.Event

    class Ev(real_event):
        def wait(self, t=None):
            waits.append(t)
            return False

    monkeypatch.setattr(mesh_cli.threading, "Event", Ev)
    return calls, waits


def test_first_start_fails_fast_on_a_bind_error(monkeypatch, capsys):
    monkeypatch.delenv(mesh_cli.RETRY_ENV, raising=False)
    calls, waits = _cmd_up_with(monkeypatch, [OSError(49, "Can't assign requested address")])
    assert mesh_cli._cmd_up(mesh_cli.build_parser().parse_args(["up"])) == 1
    assert calls == [1] and waits == [] and "cannot start" in capsys.readouterr().err


def test_restarted_up_retries_a_bind_error_with_backoff(monkeypatch, capsys):
    monkeypatch.setenv(mesh_cli.RETRY_ENV, "1")
    err49 = OSError(49, "Can't assign requested address")
    calls, waits = _cmd_up_with(monkeypatch, [err49, mesh_cli.NO_ADDR, err49, OSError(48, "busy"),
                                              err49, err49, err49, 0])
    assert mesh_cli._cmd_up(mesh_cli.build_parser().parse_args(["up"])) == 0
    assert len(calls) == 8 and waits == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    out = capsys.readouterr().out
    assert "retry 1 in 2 s" in out and "no LAN address" in out


def test_restarted_up_does_not_retry_other_errors(monkeypatch, capsys):
    monkeypatch.setenv(mesh_cli.RETRY_ENV, "1")
    calls, waits = _cmd_up_with(monkeypatch, [OSError(13, "Permission denied")])
    assert mesh_cli._cmd_up(mesh_cli.build_parser().parse_args(["up"])) == 1
    calls2, _ = _cmd_up_with(monkeypatch, [2])                       # a config error
    assert mesh_cli._cmd_up(mesh_cli.build_parser().parse_args(["up"])) == 2
    assert calls == [1] and calls2 == [1] and waits == []


def test_restart_sets_the_retry_flag_for_the_new_process(monkeypatch):
    monkeypatch.delenv(mesh_cli.RETRY_ENV, raising=False)
    _cmd_up_with(monkeypatch, [mesh_cli.RESTART])
    su = types.ModuleType("apex_router.mesh.selfupdate")
    seen = []
    su.reexec = lambda: seen.append(os.environ.get(mesh_cli.RETRY_ENV))
    monkeypatch.setitem(sys.modules, su.__name__, su)
    mesh_cli._cmd_up(mesh_cli.build_parser().parse_args(["up"]))
    assert seen == ["1"]


def test_drift_needs_the_same_new_address_twice_and_an_eligible_one(home, monkeypatch, capsys):
    identity.init()
    record: dict = {}
    _fake_modules(monkeypatch, record)
    monkeypatch.setattr(mesh_cli, "ADDR_CHECK_EVERY", 1)
    rows = [("en0", "192.168.1.5", True)]
    cyc = iter(["10.0.0.1", "10.0.0.2", "10.0.0.3"] * 10_000)   # never the same address twice
    _netif_rows(monkeypatch, rows, lan=lambda: next(cyc))
    args = mesh_cli.build_parser().parse_args(["up", "--no-auto-update"])
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    assert mesh_cli.run_up(args, stop, interval_s=0.01) == 0
    _netif_rows(monkeypatch, rows, lan="8.8.4.4")               # public: a restart could not bind
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    assert mesh_cli.run_up(args, stop, interval_s=0.01) == 0
    _netif_rows(monkeypatch, rows, lan="10.0.0.7")              # stable private: restart
    stop = threading.Event()
    threading.Timer(2.0, stop.set).start()
    assert mesh_cli.run_up(args, stop, interval_s=0.01) == mesh_cli.RESTART
    assert "address_changed to 10.0.0.7" in capsys.readouterr().out


def test_retry_delay_never_overflows_and_stop_exits_clean(monkeypatch):
    monkeypatch.setenv(mesh_cli.RETRY_ENV, "1")
    err49 = OSError(49, "gone")
    _, waits = _cmd_up_with(monkeypatch, [err49] * 1100 + [0])
    assert mesh_cli._cmd_up(mesh_cli.build_parser().parse_args(["up"])) == 0
    assert len(waits) == 1100 and max(waits) == 60.0
    stop_waits = []

    class StopEv(threading.Event):
        def wait(self, t=None):
            stop_waits.append(t)
            return True                                    # a signal arrives during the wait

    calls2 = []
    monkeypatch.setattr(mesh_cli, "run_up", lambda a, s, **k: calls2.append(1) or mesh_cli.NO_ADDR)
    monkeypatch.setattr(mesh_cli.threading, "Event", StopEv)
    assert mesh_cli._cmd_up(mesh_cli.build_parser().parse_args(["up"])) == 0
    assert calls2 == [1] and stop_waits == [2.0]
