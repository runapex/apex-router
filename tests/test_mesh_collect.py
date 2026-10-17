"""LAN mesh data plane: pool() merge rules, fetch() against a stub HTTP server, collect() fan-out.

The stub server is a local http.server on 127.0.0.1 that speaks the contract (signed `data`
envelope, auth header check). It does not need mesh/server.py. One extra test runs against the
real MeshServer and skips while server.py is absent.
"""
from __future__ import annotations

import http.server
import importlib.util
import threading
import time
import urllib.parse

import pytest

from apex_router import route_advise
from apex_router.mesh import collect, datasets, protocol
from apex_router.mesh.identity import Identity
from apex_router.mesh.peers import Peer, PeerTable

SELF_ID = "a" * 32
PEER_ID = "b" * 32


@pytest.fixture(autouse=True)
def _mesh_home(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_MESH_HOME", str(tmp_path / "meshhome"))


def _ident(byte=1, node=SELF_ID):
    return Identity(node_id=node, name="me", secret=bytes([byte]) * 32)


def _ok(node_id, data, name="n"):
    return {"node_id": node_id, "name": name, "ok": True, "data": data}


def _card(node_id, host="127.0.0.1", port=47778, **kw):
    c = {"node_id": node_id, "name": f"n{node_id[0]}", "host": host, "port": port,
         "version": "1.0", "roles": [], "load": 0, "started_at": 1_700_000_000.0}
    c.update(kw)
    return c


def _peer(node_id, port):
    return Peer(node_id=node_id, name="peer", host="127.0.0.1", port=port, version="1.0",
                roles=(), load=0, started_at=1.0, last_seen=time.time())


# ---- pool -----------------------------------------------------------------------------------

def test_pool_sums_route_rates_of_two_nodes():
    a = _ok("1" * 32, {"route_rates": {"code": {"n": 10, "escalated": 2, "rate": 0.2},
                                       "docs": {"n": 4, "escalated": 0, "rate": 0.0}}})
    b = _ok("2" * 32, {"route_rates": {"code": {"n": 30, "escalated": 6, "rate": 0.2,
                                                "null_ts": 3}}})
    rates = collect.pool([a, b])["route_rates"]
    assert rates["code"] == {"n": 40, "escalated": 8, "rate": 0.2, "null_ts": 3}
    assert rates["docs"] == {"n": 4, "escalated": 0, "rate": 0.0}


def test_pool_skips_bad_cells_and_failed_nodes():
    good = _ok("1" * 32, {"route_rates": {
        "code": {"n": 10, "escalated": 1},
        "float_n": {"n": 10.0, "escalated": 1},       # not int
        "bool_n": {"n": True, "escalated": 0},        # bool is not an int count
        "neg": {"n": -1, "escalated": 0},
        "over": {"n": 3, "escalated": 4},             # escalated > n
        "str_esc": {"n": 5, "escalated": "2"},
        "not_dict": 7,
    }})
    junk = _ok("2" * 32, {"route_rates": ["not", "a", "dict"]})
    stub = _ok("3" * 32, {"route_rates": {"error": "not_shared"}})
    failed = {"node_id": "4" * 32, "name": "x", "ok": False, "data": {
        "route_rates": {"code": {"n": 1000, "escalated": 1000}}}, "error": "http 401"}
    rates = collect.pool([good, junk, stub, failed, "garbage", None])["route_rates"]
    assert rates == {"code": {"n": 10, "escalated": 1, "rate": 0.1}}


def test_pool_bad_null_ts_counts_as_zero():
    r = _ok("1" * 32, {"route_rates": {"code": {"n": 2, "escalated": 1, "null_ts": -5}}})
    assert collect.pool([r])["route_rates"]["code"] == {"n": 2, "escalated": 1, "rate": 0.5}


def test_pool_pressure_worst_level_and_per_node():
    rs = [_ok("1" * 32, {"pressure": {"level": "GREEN"}}),
          _ok("2" * 32, {"pressure": {"level": "RED"}}),
          _ok("3" * 32, {"pressure": {"level": "AMBER"}}),
          _ok("4" * 32, {"pressure": {"level": "BOGUS"}}),
          _ok("5" * 32, {"pressure": {"error": "unavailable"}})]
    p = collect.pool(rs)["pressure"]
    assert p["level"] == "RED"
    assert [n["level"] for n in p["nodes"]] == ["GREEN", "RED", "AMBER", "UNKNOWN", "UNKNOWN"]
    assert collect.pool([rs[0], rs[2]])["pressure"]["level"] == "AMBER"
    assert collect.pool([])["pressure"] == {"level": "UNKNOWN", "nodes": []}


def test_pool_errors_summed_measure_per_node():
    rs = [_ok("1" * 32, {"errors15m": {"n": 3, "by_cause": {"rate_limited": 2, "x": 1}},
                         "measure": {"limit_pct": 40}}),
          _ok("2" * 32, {"errors15m": {"n": 2, "by_cause": {"rate_limited": 2, "bad": "1"}},
                         "measure": {"limit_pct": 90}})]
    pooled = collect.pool(rs)
    assert pooled["errors15m"] == {"n": 5, "by_cause": {"rate_limited": 4, "x": 1}}
    assert pooled["measure"] == {"1" * 32: {"limit_pct": 40}, "2" * 32: {"limit_pct": 90}}


def test_pooled_advice_n_is_the_sum():
    a = _ok("1" * 32, {"route_rates": {"code": {"n": 25, "escalated": 5}}})
    b = _ok("2" * 32, {"route_rates": {"code": {"n": 35, "escalated": 7}}})
    pooled = collect.pool([a, b])
    adv = pooled["route_advice_pooled"]
    assert adv["code"]["n"] == 60
    assert adv == route_advise.advise(rates=pooled["route_rates"])


# ---- fetch against a stub HTTP server -------------------------------------------------------

class _Stub:
    """Contract stub of GET /mesh/v1/data. ``mode`` changes the reply for negative tests."""

    def __init__(self, ident: Identity, node_id: str):
        self.ident, self.node_id, self.mode = ident, node_id, "ok"
        self.guard = protocol.ReplayGuard()
        self.paths: list[str] = []
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                stub.paths.append(self.path)
                try:
                    env = protocol.decode(self.headers.get("X-Apex-Mesh", "").encode(),
                                          stub.ident, stub.guard)
                    assert env["kind"] == "auth" and env["body"]["path"] == self.path
                except Exception:
                    self.send_response(401)
                    self.end_headers()
                    return
                q = urllib.parse.urlparse(self.path)
                names = urllib.parse.parse_qs(q.query)["names"][0].split(",")
                data = {n: {"level": "GREEN"} if n == "pressure" else datasets.NOT_SHARED
                        for n in names}
                kind, nid = "data", stub.node_id
                if stub.mode == "kind":
                    kind = "card"
                elif stub.mode == "node":
                    nid = "c" * 32
                if stub.mode == "garbage":
                    raw = b"not json"
                elif stub.mode == "big":
                    raw = b" " * (collect.MAX_RESPONSE_BYTES + 10)
                else:
                    raw = protocol.encode(kind, {"node_id": nid, "data": data}, stub.ident)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_fetch_ok_signed_auth_and_response():
    ident = _ident()
    with _Stub(ident, PEER_ID) as s:
        r = collect.fetch(_peer(PEER_ID, s.port), ident, ["pressure", "worker"], timeout=3)
    assert r == {"node_id": PEER_ID, "name": "peer", "ok": True,
                 "data": {"pressure": {"level": "GREEN"}, "worker": {"error": "not_shared"}}}
    assert s.paths == ["/mesh/v1/data?names=pressure,worker"]


@pytest.mark.parametrize("mode,frag", [("kind", "kind"), ("node", "node_id"),
                                       ("garbage", "bad response"), ("big", "too large")])
def test_fetch_rejects_bad_responses(mode, frag):
    ident = _ident()
    with _Stub(ident, PEER_ID) as s:
        s.mode = mode
        r = collect.fetch(_peer(PEER_ID, s.port), ident, ["pressure"], timeout=3)
    assert r["ok"] is False and frag in r["error"] and r["data"] == {}


def test_fetch_other_mesh_gets_401_and_unreachable_never_raises():
    with _Stub(_ident(byte=2), PEER_ID) as s:
        r = collect.fetch(_peer(PEER_ID, s.port), _ident(byte=1), ["pressure"], timeout=3)
        port = s.port
    assert r["ok"] is False and r["error"] == "http 401"
    r = collect.fetch(_peer(PEER_ID, port), _ident(), ["pressure"], timeout=1)
    assert r["ok"] is False and r["error"].startswith("unreachable")


def test_fetch_ignores_proxy_env(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    ident = _ident()
    with _Stub(ident, PEER_ID) as s:
        r = collect.fetch(_peer(PEER_ID, s.port), ident, ["pressure"], timeout=3)
    assert r["ok"] is True


def test_collect_self_local_and_peer_over_http(monkeypatch):
    ident = _ident()
    calls = []

    def fake_read(names, shared):
        calls.append(list(names))
        return {n: {"level": "AMBER"} for n in names}

    monkeypatch.setattr(datasets, "read", fake_read)
    with _Stub(ident, PEER_ID) as s:
        table = PeerTable(_card(SELF_ID, port=1))     # port 1: self must not be fetched
        table.update(_card(PEER_ID, port=s.port))
        res = collect.collect(table, ident, ["pressure"], timeout=3)
        assert len(s.paths) == 1
        no_self = collect.collect(table, ident, ["pressure"], include_self=False, timeout=3)
    assert calls == [["pressure"]]
    assert res["n_nodes"] == 2 and res["n_ok"] == 2
    assert [n["node_id"] for n in res["nodes"]] == [SELF_ID, PEER_ID]
    assert res["pooled"]["pressure"]["level"] == "AMBER"
    assert no_self["n_nodes"] == 1 and no_self["nodes"][0]["node_id"] == PEER_ID


def test_collect_counts_failed_peer():
    ident = _ident()
    table = PeerTable(_card(SELF_ID))
    table.update(_card(PEER_ID, port=9))   # discard port: nothing listens
    res = collect.collect(table, ident, ["worker"], include_self=False, timeout=1)
    assert res["n_nodes"] == 1 and res["n_ok"] == 0 and res["nodes"][0]["ok"] is False


@pytest.mark.skipif(importlib.util.find_spec("apex_router.mesh.server") is None,
                    reason="mesh/server.py not written yet")
def test_fetch_against_real_mesh_server(monkeypatch):
    from apex_router.mesh import server
    monkeypatch.setattr(datasets, "ALLOWLIST", {**datasets.ALLOWLIST,
                                                "pressure": lambda: {"level": "GREEN"}})
    srv_ident = _ident(node=PEER_ID)
    table = PeerTable(_card(PEER_ID))
    srv = server.MeshServer(srv_ident, table, lambda: _card(PEER_ID), host="127.0.0.1", port=0,
                            shared=frozenset({"pressure"}))
    srv.start()
    try:
        port = srv.address[1]
        r = collect.fetch(_peer(PEER_ID, port), _ident(), ["pressure", "worker"], timeout=3)
    finally:
        srv.stop()
    assert r["ok"] is True, r
    assert r["data"]["pressure"] == {"level": "GREEN"}
    assert r["data"]["worker"] == {"error": "not_shared"}
