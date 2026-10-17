"""LAN mesh network layer: netif, beacon (UDP), server (HTTP).

Loopback only (127.0.0.1), port 0 or free ephemeral ports, unicast targets. Never the wildcard.
Multicast tests skip when the OS refuses a multicast join or loop on loopback.
"""
from __future__ import annotations

import http.client
import json
import socket
import time

import pytest

from apex_router.mesh import beacon as beacon_mod
from apex_router.mesh import datasets, netif, protocol, server
from apex_router.mesh.beacon import Beacon
from apex_router.mesh.identity import Identity
from apex_router.mesh.peers import PeerTable
from apex_router.mesh.server import MeshServer, auth_header

LO = "127.0.0.1"
NODE_A, NODE_B = "a" * 32, "b" * 32
SECRET = bytes([7]) * 32
T0 = time.time() - 100           # fixed start time: a card must not change between calls


@pytest.fixture(autouse=True)
def _mesh_home(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_MESH_HOME", str(tmp_path / "meshhome"))


def _ident(node=NODE_A, secret=SECRET):
    return Identity(node_id=node, name="n", secret=secret)


def _card(node_id, port=47778, **kw):
    c = {"node_id": node_id, "name": "box", "host": LO, "port": port, "version": "1.0",
         "roles": ["worker"], "load": 0, "started_at": T0}
    c.update(kw)
    return c


def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((LO, 0))
        return s.getsockname()[1]


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ---------------------------------------------------------------------------------- netif ----
@pytest.mark.parametrize("ip", ["10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.1",
                                "169.254.10.10", "127.0.0.1", "::1", "fe80::1", "fe80::1%en0",
                                "fd12:3456::1", "::ffff:10.0.0.1"])
def test_is_private_true(ip):
    assert netif.is_private(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "172.32.0.1", "100.64.0.1", "192.0.2.1", "0.0.0.0",
                                "::", "2001:db8::1", "239.255.77.77", "not-an-ip", "", None, 7])
def test_is_private_false(ip):
    assert not netif.is_private(ip)


@pytest.mark.parametrize("ip", ["0.0.0.0", "::", "::ffff:0.0.0.0", "239.255.77.77", "x"])
def test_choose_bind_refuses(ip):
    with pytest.raises(ValueError):
        netif.choose_bind(ip, allow_public=True)


def test_choose_bind_public_needs_flag():
    with pytest.raises(ValueError, match="allow-public"):
        netif.choose_bind("8.8.8.8")
    assert netif.choose_bind("8.8.8.8", allow_public=True) == "8.8.8.8"
    assert netif.choose_bind(" 192.168.0.5 ") == "192.168.0.5"


def test_choose_bind_default(monkeypatch):
    monkeypatch.setattr(netif, "default_lan_ip", lambda: "10.0.0.9")
    assert netif.choose_bind(None) == "10.0.0.9"
    monkeypatch.setattr(netif, "default_lan_ip", lambda: None)
    with pytest.raises(ValueError, match="--bind"):
        netif.choose_bind(None)
    monkeypatch.setattr(netif, "default_lan_ip", lambda: "8.8.4.4")
    with pytest.raises(ValueError):
        netif.choose_bind(None)


def test_default_lan_ip_is_ip_or_none():
    ip = netif.default_lan_ip()
    assert ip is None or netif._parse(ip)


# --------------------------------------------------------------------------------- server ----
@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setitem(datasets.ALLOWLIST, "worker", lambda: {"running": 2, "inbox": 1,
                                                               "error": "x"})
    ident = _ident(NODE_A)
    table = PeerTable(_card(NODE_A))
    table.update(_card(NODE_B, started_at=T0 - 1000))
    s = MeshServer(ident, table, lambda: _card(NODE_A), host=LO, port=0,
                   shared=frozenset({"worker"}))
    s.start()
    yield s
    s.stop()


def _get(s, path, headers=None, method="GET"):
    host, port = s.address
    c = http.client.HTTPConnection(host, port, timeout=5)
    try:
        c.putrequest(method, path, skip_host=False, skip_accept_encoding=True)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def _auth(path, ident=None, **kw):
    return {server.AUTH_HEADER: auth_header(ident or _ident(NODE_B), path, **kw)}


def test_server_refuses_wildcard():
    t = PeerTable(_card(NODE_A))
    for host in ("0.0.0.0", "::", "239.1.1.1", "localhost"):
        with pytest.raises(ValueError):
            MeshServer(_ident(), t, lambda: _card(NODE_A), host=host, port=0)


def test_card_unauthenticated_and_signed(srv):
    assert srv.address[0] == LO and srv.address[1] > 0
    st, body = _get(srv, "/mesh/v1/card")
    assert st == 200
    env = protocol.decode(body, _ident(NODE_B), None, max_bytes=1 << 20)
    assert env["kind"] == "card" and env["body"]["node_id"] == NODE_A


def test_peers_and_leader_with_auth(srv):
    st, body = _get(srv, "/mesh/v1/peers", _auth("/mesh/v1/peers"))
    assert st == 200
    env = protocol.decode(body, _ident(NODE_B), None, max_bytes=1 << 20)
    assert env["kind"] == "peers" and env["body"]["n_alive"] == 2
    st, body = _get(srv, "/mesh/v1/leader", _auth("/mesh/v1/leader"))
    assert st == 200
    env = protocol.decode(body, _ident(NODE_B), None, max_bytes=1 << 20)
    assert env["kind"] == "leader" and env["body"]["node_id"] == NODE_B


@pytest.mark.parametrize("path", ["/mesh/v1/peers", "/mesh/v1/leader",
                                  "/mesh/v1/data?names=worker"])
def test_no_auth_401(srv, path):
    st, body = _get(srv, path)
    assert st == 401 and json.loads(body) == {"error": "unauthorized"}


def test_bad_auth_401(srv):
    p = "/mesh/v1/peers"
    other = Identity(node_id=NODE_B, name="x", secret=bytes([9]) * 32)
    cases = [
        {server.AUTH_HEADER: "garbage"},
        {server.AUTH_HEADER: "{}"},
        _auth(p, ident=other),                                           # other mesh / bad MAC
        _auth(p, now=time.time() - 120),                                 # skew
        {server.AUTH_HEADER: protocol.encode("card", {"path": p}, _ident(NODE_B)).decode()},
        {server.AUTH_HEADER: protocol.encode("auth", {"path": p, "x": 1},
                                             _ident(NODE_B)).decode()},
        {server.AUTH_HEADER: "x" * 5000},
    ]
    good = auth_header(_ident(NODE_B), p)
    env = json.loads(good)
    env["body"]["path"] = "/mesh/v1/leader"                             # tampered, MAC stale
    cases.append({server.AUTH_HEADER: json.dumps(env)})
    for h in cases:
        st, body = _get(srv, p, h)
        assert st == 401, h
        assert b"Traceback" not in body


def test_path_mismatch_401(srv):
    st, _ = _get(srv, "/mesh/v1/peers", _auth("/mesh/v1/leader"))
    assert st == 401
    st, _ = _get(srv, "/mesh/v1/data?names=worker", _auth("/mesh/v1/data?names=pressure"))
    assert st == 401
    st, _ = _get(srv, "/mesh/v1/data?names=worker", _auth("/mesh/v1/data"))
    assert st == 401


def test_replay_401(srv):
    h = _auth("/mesh/v1/peers")
    assert _get(srv, "/mesh/v1/peers", h)[0] == 200
    assert _get(srv, "/mesh/v1/peers", h)[0] == 401


def test_rejected_auth_does_not_burn_nonce(srv):
    h = _auth("/mesh/v1/peers")
    assert _get(srv, "/mesh/v1/leader", h)[0] == 401      # path mismatch
    assert _get(srv, "/mesh/v1/peers", h)[0] == 200       # nonce still unused


def test_data_shared_and_not_shared(srv):
    p = "/mesh/v1/data?names=worker,pressure,bogus"
    st, body = _get(srv, p, _auth(p))
    assert st == 200
    env = protocol.decode(body, _ident(NODE_B), None, max_bytes=1 << 20)
    assert env["kind"] == "data"
    assert env["body"]["node_id"] == NODE_A
    data = env["body"]["data"]
    assert data["worker"] == {"running": 2, "inbox": 1}
    assert data["pressure"] == {"error": "not_shared"}
    assert data["bogus"] == {"error": "not_shared"}


def test_data_name_limits(srv):
    many = ",".join(f"n{i}" for i in range(17))
    for path, err in [(f"/mesh/v1/data?names={many}", "too_many_names"),
                      ("/mesh/v1/data?names=", "no_names"),
                      ("/mesh/v1/data", "bad_query"),
                      ("/mesh/v1/data?names=a&names=b", "bad_query"),
                      ("/mesh/v1/data?names=a&x=1", "bad_query"),
                      ("/mesh/v1/data?names=../etc", "bad_name")]:
        st, body = _get(srv, path, _auth(path))
        assert st == 400, path
        assert json.loads(body) == {"error": err}
    ok = ",".join(f"n{i}" for i in range(16))
    path = f"/mesh/v1/data?names={ok}"
    assert _get(srv, path, _auth(path))[0] == 200


def test_not_found_and_methods(srv):
    for path in ["/", "/mesh/v1/card/", "/mesh/v1/cards", "/mesh/v1/card?x=1", "/etc/passwd"]:
        assert _get(srv, path)[0] == 404, path
    for m in ("POST", "PUT", "DELETE", "HEAD"):
        st, _ = _get(srv, "/mesh/v1/card", method=m)
        assert st == 501, m


def test_duplicate_auth_header_401(srv):
    host, port = srv.address
    c = http.client.HTTPConnection(host, port, timeout=5)
    c.putrequest("GET", "/mesh/v1/peers")
    c.putheader(server.AUTH_HEADER, auth_header(_ident(NODE_B), "/mesh/v1/peers"))
    c.putheader(server.AUTH_HEADER, auth_header(_ident(NODE_B), "/mesh/v1/peers"))
    c.endheaders()
    assert c.getresponse().status == 401
    c.close()


def test_no_traceback_on_error(capsys):
    def boom():
        raise RuntimeError("secret detail /home/x")
    s = MeshServer(_ident(), PeerTable(_card(NODE_A)), boom, host=LO, port=0)
    s.start()
    try:
        st, body = _get(s, "/mesh/v1/card")
    finally:
        s.stop()
    assert st == 500 and json.loads(body) == {"error": "internal"}
    out = capsys.readouterr()
    assert "Traceback" not in out.err and "secret detail" not in out.err + out.out


def test_garbage_request_line_and_silent_log(srv, capsys):
    host, port = srv.address
    with socket.create_connection((host, port), timeout=5) as c:
        c.sendall(b"\x00\xffGARBAGE\r\n\r\n")
        resp = c.recv(4096)
    assert b"Traceback" not in resp
    assert capsys.readouterr().err == ""
    assert _get(srv, "/mesh/v1/card")[0] == 200       # still serving


def test_slow_client_does_not_block(srv):
    host, port = srv.address
    with socket.create_connection((host, port), timeout=5) as idle:
        idle.sendall(b"GET /mesh/v1/card HTTP/1.1\r\n")      # never finish the request
        assert _get(srv, "/mesh/v1/card")[0] == 200


def test_server_stop_twice():
    s = MeshServer(_ident(), PeerTable(_card(NODE_A)), lambda: _card(NODE_A), host=LO, port=0)
    s.start()
    s.stop()
    s.stop()


# --------------------------------------------------------------------------------- beacon ----
def _beacon(node, port, targets, **kw):
    ident = _ident(node)
    table = PeerTable(_card(node))
    b = Beacon(ident, table, lambda: _card(node), iface_ip=LO, port=port, interval_s=0.1,
               targets=targets, **kw)
    return b, table


def _ids(table):
    return {p.node_id for p in table.alive()}


def test_beacon_rejects_bad_args():
    t = PeerTable(_card(NODE_A))
    for kw in ({"iface_ip": "0.0.0.0"}, {"iface_ip": "::"}, {"iface_ip": "nope"},
               {"iface_ip": LO, "group": "10.0.0.1"}, {"iface_ip": LO, "interval_s": 0},
               {"iface_ip": LO, "targets": [("h", 0)]}, {"iface_ip": LO, "port": 70000}):
        with pytest.raises(ValueError):
            Beacon(_ident(), t, lambda: _card(NODE_A), **kw)


def test_two_beacons_find_each_other_unicast():
    pa, pb = _free_udp_port(), _free_udp_port()
    a, ta = _beacon(NODE_A, pa, [(LO, pb)])
    b, tb = _beacon(NODE_B, pb, [(LO, pa)])
    a.start()
    b.start()
    try:
        assert _wait(lambda: _ids(ta) == {NODE_A, NODE_B} and _ids(tb) == {NODE_A, NODE_B})
        assert ta.leader().node_id == tb.leader().node_id
    finally:
        a.stop()
    # A's bye removes A from B's table at once (no TTL wait).
    assert _wait(lambda: _ids(tb) == {NODE_B}, timeout=3)
    b.stop()
    assert b.stats()["drops"] == {} or set(b.stats()["drops"]) <= {"replay"}


def test_beacon_other_mesh_is_dropped():
    pa, pb = _free_udp_port(), _free_udp_port()
    a, ta = _beacon(NODE_A, pa, [(LO, pb)])
    other = Identity(node_id=NODE_B, name="x", secret=bytes([9]) * 32)
    tb = PeerTable(_card(NODE_B))
    b = Beacon(other, tb, lambda: _card(NODE_B), iface_ip=LO, port=pb, interval_s=0.1,
               targets=[(LO, pa)])
    a.start()
    b.start()
    try:
        assert _wait(lambda: a.stats()["drops"].get("other_mesh", 0) >= 2)
        assert _ids(ta) == {NODE_A} and _ids(tb) == {NODE_B}
    finally:
        a.stop()
        b.stop()


def test_beacon_handle_drops_by_reason():
    b, t = _beacon(NODE_A, 0, [])
    ib = _ident(NODE_B)
    b.handle(b"x" * 2000)
    b.handle(b"not json")
    b.handle(b"{}")
    b.handle(protocol.encode("announce", _card(NODE_B), _ident(secret=bytes([3]) * 32)))
    env = json.loads(protocol.encode("announce", _card(NODE_B), ib))
    env["body"]["load"] = 5
    b.handle(json.dumps(env).encode())                                  # bad MAC
    b.handle(protocol.encode("announce", _card(NODE_B), ib, now=time.time() - 300))
    b.handle(protocol.encode("announce", {"node_id": NODE_B}, ib))      # bad card
    b.handle(protocol.encode("hello", {"node_id": NODE_B}, ib))         # bad kind
    b.handle(protocol.encode("announce", {"x": 1}, ib))                 # bad body
    good = protocol.encode("announce", _card(NODE_B), ib)
    b.handle(good)
    b.handle(good)                                                      # replay
    b.handle(protocol.encode("announce", _card(NODE_A), _ident(NODE_A)))  # own
    st = b.stats()
    assert st["drops"] == {"too_large": 1, "malformed": 2, "other_mesh": 1, "bad_mac": 1,
                           "skew": 1, "bad_card": 1, "bad_kind": 1, "bad_body": 1, "replay": 1}
    assert st["own"] == 1 and st["announces"] == 1 and st["rx"] == 12
    assert _ids(t) == {NODE_A, NODE_B}
    b.handle(protocol.encode("bye", {"node_id": NODE_A}, ib))           # cannot remove self
    b.handle(protocol.encode("bye", {"node_id": NODE_B}, ib))
    assert _ids(t) == {NODE_A}


def test_beacon_survives_raising_card_fn():
    pa = _free_udp_port()
    b = Beacon(_ident(), PeerTable(_card(NODE_A)), lambda: 1 / 0, iface_ip=LO, port=pa,
               interval_s=0.05)
    b.start()
    try:
        assert _wait(lambda: b.stats()["send_errors"] >= 2)
        assert b._thread.is_alive()
    finally:
        b.stop()
    assert not b._thread.is_alive()
    b.stop()


def _multicast_works(group, port):
    """Probe: join + send + receive one datagram on loopback. False if the OS refuses."""
    try:
        r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return False
    try:
        beacon_mod._set_reuse(r)
        r.bind((group, port))
        r.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(group) + socket.inet_aton(LO))
        r.settimeout(1.0)
        s.bind((LO, 0))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(LO))
        s.sendto(b"probe", (group, port))
        return r.recv(64) == b"probe"
    except OSError:
        return False
    finally:
        r.close()
        s.close()


def test_two_beacons_find_each_other_multicast():
    group, port = "239.255.77.78", _free_udp_port()
    if not _multicast_works(group, port):
        pytest.skip("OS refuses a multicast join/loop on 127.0.0.1")
    a, ta = _beacon(NODE_A, port, [], group=group)
    b, tb = _beacon(NODE_B, port, [], group=group)
    try:
        a.start()
        b.start()
    except OSError as e:
        a.stop()
        pytest.skip(f"OS refuses a shared multicast port: {e}")
    try:
        if not (a.multicast and b.multicast):
            pytest.skip(f"OS refuses a multicast join: {a.multicast_error or b.multicast_error}")
        assert _wait(lambda: _ids(ta) == {NODE_A, NODE_B} and _ids(tb) == {NODE_A, NODE_B})
        assert a.stats()["own"] >= 1                       # loopback copy of own announce
    finally:
        a.stop()
        b.stop()
