"""Security core of the LAN mesh: identity, protocol (HMAC/skew/replay/size), peer table.

No sockets. All state goes to tmp_path / APEX_MESH_HOME (never the real home).
"""
from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from apex_router.mesh import identity, peers, protocol
from apex_router.mesh.identity import Identity, MeshNotInitialized
from apex_router.mesh.peers import PeerTable
from apex_router.mesh.protocol import (
    MAX_DATAGRAM,
    MAX_SKEW_S,
    ProtocolError,
    ReplayGuard,
)

T0 = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _mesh_home(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_MESH_HOME", str(tmp_path / "meshhome"))


def _ident(byte=1, node="a" * 32):
    return Identity(node_id=node, name="n", secret=bytes([byte]) * 32)


def _card(node_id, **kw):
    c = {"node_id": node_id, "name": "box", "host": "192.168.1.10", "port": 47778,
         "version": "1.0", "roles": ["worker"], "load": 0, "started_at": T0}
    c.update(kw)
    return c


def _nid(ch):
    return ch * 32


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


# ---------------------------------------------------------------- identity
def test_mesh_home_env(tmp_path):
    assert identity.mesh_home() == tmp_path / "meshhome"


def test_mesh_home_default_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("APEX_MESH_HOME")
    assert identity.mesh_home() == tmp_path / ".apex-router" / "mesh"   # sandbox HOME


def test_init_load_roundtrip_and_modes():
    home = identity.mesh_home()
    ident = identity.init(name="alpha")
    assert len(ident.secret) == 32 and len(ident.node_id) == 32
    int(ident.node_id, 16)
    assert _mode(home) == 0o700
    assert _mode(home / "mesh.key") == 0o600 and _mode(home / "node.json") == 0o600
    assert (home / "mesh.key").read_bytes() == ident.secret
    node = json.loads((home / "node.json").read_text())
    assert node == {"node_id": ident.node_id, "name": "alpha"}
    assert ident.secret.hex() not in (home / "node.json").read_text()
    assert identity.load() == ident
    assert not [p for p in home.iterdir() if p.name.endswith(".tmp")]


def test_secret_not_in_repr():
    ident = _ident(7)
    assert ident.secret.hex() not in repr(ident) and "secret" not in repr(ident)


def test_init_keeps_existing_and_force_replaces(tmp_path):
    a = identity.init(name="a")
    assert identity.init(name="b") == a
    with pytest.raises(FileExistsError):
        identity.init(secret=b"\x09" * 32)
    assert identity.init(secret=a.secret) == a
    b = identity.init(name="b", force=True)
    assert b.node_id != a.node_id and b.secret != a.secret and identity.load() == b


def test_init_refuses_corrupt_without_force():
    home = identity.mesh_home()
    home.mkdir(parents=True)
    (home / "mesh.key").write_bytes(b"short")
    with pytest.raises(FileExistsError):
        identity.init()
    assert identity.init(force=True).secret != b"short"


def test_init_with_secret_and_bad_secret(tmp_path):
    s = b"\x05" * 32
    ident = identity.init(tmp_path / "x", secret=s)
    assert ident.secret == s
    with pytest.raises(ValueError):
        identity.init(tmp_path / "y", secret=b"\x05" * 31)


def test_load_missing_and_corrupt(tmp_path):
    with pytest.raises(MeshNotInitialized):
        identity.load(tmp_path / "none")
    home = tmp_path / "c"
    identity.init(home)
    (home / "node.json").write_text("{not json")
    with pytest.raises(MeshNotInitialized):
        identity.load(home)
    (home / "node.json").write_text(json.dumps({"node_id": "XYZ", "name": "n"}))
    with pytest.raises(MeshNotInitialized):
        identity.load(home)
    (home / "node.json").write_text(json.dumps({"node_id": "a" * 32, "name": "n"}))
    (home / "mesh.key").write_bytes(b"x" * 31)
    with pytest.raises(MeshNotInitialized):
        identity.load(home)


def test_load_tightens_loose_mode(tmp_path):
    home = tmp_path / "m"
    identity.init(home)
    os.chmod(home / "mesh.key", 0o644)
    identity.load(home)
    assert _mode(home / "mesh.key") == 0o600


def test_name_is_cleaned():
    ident = identity.init(name="x" * 100 + "\n")
    assert ident.name == "x" * 64
    assert identity.clean_name("\x00\x07") == "node"


def test_mesh_id():
    a, b = _ident(1), _ident(2)
    assert len(a.mesh_id) == 16 and a.mesh_id != b.mesh_id
    assert a.mesh_id == _ident(1, node="b" * 32).mesh_id


def test_export_parse_roundtrip():
    ident = _ident(0xFB)
    tok = identity.export_secret(ident)
    assert tok.startswith("apexmesh1:") and "=" not in tok
    assert identity.parse_secret(tok) == ident.secret
    assert identity.parse_secret("  " + tok + "\n") == ident.secret


@pytest.mark.parametrize("tok", [
    "", "apexmesh1:", "apexmesh2:AAAA", "AAAA", "apexmesh1:!!!!",
    "apexmesh1:" + "A" * 42,          # 31 bytes
    "apexmesh1:" + "A" * 44,          # 33 bytes
    "apexmesh1:AA AA",
])
def test_parse_secret_rejects(tok):
    with pytest.raises(ValueError):
        identity.parse_secret(tok)


def test_parse_secret_rejects_non_str():
    with pytest.raises(ValueError):
        identity.parse_secret(b"apexmesh1:AAAA")  # type: ignore[arg-type]


# ---------------------------------------------------------------- protocol
def test_constants():
    assert protocol.PROTO == "apex-mesh/1"
    assert (protocol.MCAST_GROUP, protocol.MCAST_PORT) == ("239.255.77.77", 47777)
    assert protocol.DEFAULT_HTTP_PORT == 47778
    assert (MAX_SKEW_S, MAX_DATAGRAM) == (30, 1400)
    assert (protocol.BEACON_INTERVAL_S, protocol.PEER_TTL_S) == (5, 20)


def test_roundtrip_and_wire_shape():
    ident = _ident()
    raw = protocol.encode("announce", _card(_nid("a")), ident, now=T0)
    env = json.loads(raw)
    assert set(env) == {"proto", "kind", "mesh", "ts", "nonce", "body", "mac"}
    assert raw == protocol.canonical(env)                     # sorted keys, compact
    assert b" " not in raw.replace(b'"192.168.1.10"', b"")
    assert len(env["nonce"]) == 16 and len(env["mac"]) == 64
    assert ident.secret not in raw and ident.secret.hex().encode() not in raw
    out = protocol.decode(raw, ident, ReplayGuard(), now=T0 + 1)
    assert out["kind"] == "announce" and out["body"] == _card(_nid("a"))
    assert out["mesh"] == ident.mesh_id


def test_nonces_differ():
    ident = _ident()
    a = json.loads(protocol.encode("announce", {}, ident, now=T0))["nonce"]
    b = json.loads(protocol.encode("announce", {}, ident, now=T0))["nonce"]
    assert a != b


@pytest.mark.parametrize("field,value", [
    ("kind", "bye"), ("ts", T0 + 1), ("nonce", "0" * 16), ("body", {"x": 1}),
    ("mac", "0" * 64),
])
def test_tamper_rejected(field, value):
    ident = _ident()
    env = json.loads(protocol.encode("announce", {"a": 1}, ident, now=T0))
    env[field] = value
    with pytest.raises(ProtocolError, match="MAC"):
        protocol.decode(protocol.canonical(env), ident, None, now=T0)


def test_tamper_body_nested():
    ident = _ident()
    env = json.loads(protocol.encode("card", _card(_nid("a")), ident, now=T0))
    env["body"]["load"] = 99
    with pytest.raises(ProtocolError, match="MAC"):
        protocol.decode(protocol.canonical(env), ident, None, now=T0)


def test_wrong_key_same_mesh_id_fails_mac():
    ident = _ident(1)
    other = _ident(2)
    env = json.loads(protocol.encode("announce", {}, other, now=T0))
    env["mesh"] = ident.mesh_id                     # forge mesh id; MAC still wrong
    with pytest.raises(ProtocolError, match="MAC"):
        protocol.decode(protocol.canonical(env), ident, None, now=T0)


def test_wrong_mesh():
    raw = protocol.encode("announce", {}, _ident(2), now=T0)
    with pytest.raises(ProtocolError, match="other mesh"):
        protocol.decode(raw, _ident(1), None, now=T0)


def test_mac_uppercase_rejected():
    ident = _ident()
    env = json.loads(protocol.encode("announce", {}, ident, now=T0))
    env["mac"] = env["mac"].upper()
    with pytest.raises(ProtocolError):
        protocol.decode(protocol.canonical(env), ident, None, now=T0)


@pytest.mark.parametrize("delta,ok", [
    (0, True), (MAX_SKEW_S, True), (-MAX_SKEW_S, True),
    (MAX_SKEW_S + 0.01, False), (-MAX_SKEW_S - 0.01, False), (3600, False),
])
def test_skew(delta, ok):
    ident = _ident()
    raw = protocol.encode("announce", {}, ident, now=T0)
    if ok:
        protocol.decode(raw, ident, None, now=T0 + delta)
    else:
        with pytest.raises(ProtocolError, match="skew"):
            protocol.decode(raw, ident, None, now=T0 + delta)


def test_replay():
    ident = _ident()
    g = ReplayGuard()
    raw = protocol.encode("announce", {}, ident, now=T0)
    protocol.decode(raw, ident, g, now=T0)
    with pytest.raises(ProtocolError, match="replay"):
        protocol.decode(raw, ident, g, now=T0 + 1)
    protocol.decode(raw, ident, None, now=T0)                  # guard=None: no replay check


def test_bad_mac_does_not_burn_nonce():
    ident = _ident()
    g = ReplayGuard()
    raw = protocol.encode("announce", {}, ident, now=T0)
    env = json.loads(raw)
    forged = dict(env, mac="f" * 64)
    with pytest.raises(ProtocolError, match="MAC"):
        protocol.decode(protocol.canonical(forged), ident, g, now=T0)
    assert len(g) == 0
    protocol.decode(raw, ident, g, now=T0)                     # real message still accepted


def test_skewed_message_does_not_burn_nonce():
    ident = _ident()
    g = ReplayGuard()
    raw = protocol.encode("announce", {}, ident, now=T0)
    with pytest.raises(ProtocolError, match="skew"):
        protocol.decode(raw, ident, g, now=T0 + 100)
    assert len(g) == 0


def test_oversize():
    ident = _ident()
    raw = protocol.encode("announce", {"pad": "x" * 2000}, ident, now=T0)
    with pytest.raises(ProtocolError, match="too large"):
        protocol.decode(raw, ident, None, now=T0)
    assert protocol.decode(raw, ident, None, now=T0, max_bytes=1 << 16)["body"]["pad"]


def test_size_checked_before_json():
    with pytest.raises(ProtocolError, match="too large"):
        protocol.decode(b"{" * (MAX_DATAGRAM + 1), _ident(), None, now=T0)


def test_beacon_card_fits_datagram():
    card = _card(_nid("a"), name="n" * 64, version="v" * 64,
                 roles=[f"role{i:02d}" for i in range(16)], load=10**6,
                 host="fe80::1234:5678:9abc:def0")
    assert len(protocol.encode("announce", card, _ident(), now=T0)) <= MAX_DATAGRAM


@pytest.mark.parametrize("data", [
    b"", b"\xff\xfe", b"not json", b"[]", b"null", b'{"proto":"apex-mesh/1"}',
    b'{"ts": NaN}', b"[" * 1000 + b"]" * 1000,
])
def test_garbage_rejected(data):
    with pytest.raises(ProtocolError):
        protocol.decode(data, _ident(), ReplayGuard(), now=T0)


def test_wrong_proto_and_extra_keys():
    ident = _ident()
    env = json.loads(protocol.encode("announce", {}, ident, now=T0))
    with pytest.raises(ProtocolError, match="proto"):
        protocol.decode(protocol.canonical(dict(env, proto="apex-mesh/2")), ident, None, now=T0)
    with pytest.raises(ProtocolError, match="envelope"):
        protocol.decode(protocol.canonical(dict(env, extra=1)), ident, None, now=T0)


def test_signed_but_bad_fields_rejected():
    """A key holder signs junk field types: still rejected."""
    ident = _ident()
    base = {"proto": protocol.PROTO, "kind": "announce", "mesh": ident.mesh_id, "ts": T0,
            "nonce": "0123456789abcdef", "body": {}}
    for k, v in [("ts", "now"), ("ts", True), ("nonce", "short"), ("body", []),
                 ("kind", "BAD KIND")]:
        env = dict(base, **{k: v})
        env["mac"] = protocol._mac(ident.secret, env)
        with pytest.raises(ProtocolError, match="fields"):
            protocol.decode(protocol.canonical(env), ident, None, now=T0)


def test_decode_check_order_proto_before_mac():
    ident = _ident()
    env = json.loads(protocol.encode("announce", {}, ident, now=T0))
    env.update(proto="x", mac="0" * 64)
    with pytest.raises(ProtocolError, match="proto"):
        protocol.decode(protocol.canonical(env), ident, None, now=T0)


def test_encode_rejects_bad_input():
    ident = _ident()
    with pytest.raises(ProtocolError):
        protocol.encode("Bad Kind", {}, ident)
    with pytest.raises(ProtocolError):
        protocol.encode("announce", [], ident)            # type: ignore[arg-type]
    with pytest.raises(ProtocolError):
        protocol.encode("announce", {"x": float("nan")}, ident)
    with pytest.raises(ProtocolError):
        protocol.encode("announce", {"x": object()}, ident)


# ---------------------------------------------------------------- replay guard
def test_guard_bounded_fails_closed():
    g = ReplayGuard(max_entries=3)
    for i in range(3):
        assert not g.seen(f"{i:016x}", T0 + i)
    assert not g.seen(f"{9:016x}", T0 + 9)       # evicts oldest (ts T0) -> floor = T0
    assert len(g) == 3
    assert g.seen(f"{0:016x}", T0)                # forgotten nonce still rejected (floor)
    assert g.seen(f"{8:016x}", T0 - 1)            # new nonce below floor rejected
    assert not g.seen(f"{7:016x}", T0 + 5)


def test_guard_ages_out_old_entries():
    g = ReplayGuard()
    assert not g.seen("a" * 16, T0)
    assert not g.seen("b" * 16, T0 + 2 * MAX_SKEW_S + 1)
    assert len(g) == 1
    assert g.seen("a" * 16, T0)                   # aged-out nonce: floor rejects it


def test_guard_rejects_bad_size():
    with pytest.raises(ValueError):
        ReplayGuard(0)


def test_guard_thread_safe():
    g = ReplayGuard(max_entries=100_000)
    hits = []

    def run():
        hits.append(sum(not g.seen(f"{i:016x}", T0) for i in range(2000)))

    ts = [threading.Thread(target=run) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(hits) == 2000                      # each nonce accepted exactly once


# ---------------------------------------------------------------- peers
def _table(**kw):
    return PeerTable(_card(_nid("5"), **kw), ttl_s=20)


@pytest.mark.parametrize("patch", [
    {"node_id": "A" * 32}, {"node_id": "a" * 31}, {"node_id": 1},
    {"name": ""}, {"name": "x" * 65}, {"name": "a\nb"}, {"name": 5},
    {"host": "not-an-ip"}, {"host": "example.com"}, {"host": ""}, {"host": 1},
    {"port": 0}, {"port": 65536}, {"port": "80"}, {"port": True}, {"port": 1.5},
    {"version": ""}, {"version": "v" * 65}, {"version": None},
    {"roles": "worker"}, {"roles": [1]}, {"roles": ["bad role"]}, {"roles": ["x" * 33]},
    {"roles": [f"r{i}" for i in range(17)]}, {"roles": None},
    {"load": -1}, {"load": 1.5}, {"load": True}, {"load": "0"}, {"load": 10**7},
    {"started_at": float("nan")}, {"started_at": float("inf")}, {"started_at": -1},
    {"started_at": "x"}, {"started_at": False}, {"started_at": 1e12},
])
def test_bad_card_rejected(patch):
    t = _table()
    assert t.update({**_card(_nid("a")), **patch}, now=T0) is None
    assert [p.node_id for p in t.alive(now=T0)] == [_nid("5")]


@pytest.mark.parametrize("card", [None, [], "x", 1, {}, {"node_id": _nid("a")}])
def test_non_dict_or_partial_card(card):
    assert _table().update(card, now=T0) is None   # type: ignore[arg-type]


def test_missing_field_rejected():
    for k in _card(_nid("a")):
        c = _card(_nid("a"))
        del c[k]
        assert _table().update(c, now=T0) is None, k


def test_good_card_normalized():
    t = _table()
    p = t.update(_card(_nid("a"), host="FE80::1", roles=["w", "w", "gpu"], extra="ok",
                       started_at=int(T0)), now=T0)
    assert p is not None and p.host == "fe80::1" and p.roles == ("w", "gpu")
    assert isinstance(p.started_at, float) and p.last_seen == T0


def test_bad_self_card_raises():
    with pytest.raises(ValueError):
        PeerTable({"node_id": "x"})
    with pytest.raises(ValueError):
        PeerTable(_card(_nid("a")), ttl_s=0)


def test_ttl_expiry_and_self_never_expires():
    t = _table()
    t.update(_card(_nid("a")), now=T0)
    assert [p.node_id for p in t.alive(now=T0 + 20)] == [_nid("5"), _nid("a")]
    assert [p.node_id for p in t.alive(now=T0 + 20.01)] == [_nid("5")]
    assert [p.node_id for p in t.alive(now=T0 + 10_000)] == [_nid("5")]


def test_refresh_extends_ttl_and_remove():
    t = _table()
    t.update(_card(_nid("a")), now=T0)
    t.update(_card(_nid("a"), load=3), now=T0 + 15)
    alive = t.alive(now=T0 + 30)
    assert [p.node_id for p in alive] == [_nid("5"), _nid("a")] and alive[1].load == 3
    t.remove(_nid("a"))
    t.remove(_nid("5"))                            # self cannot be removed
    t.remove("nope")
    assert [p.node_id for p in t.alive(now=T0 + 30)] == [_nid("5")]


def test_alive_sorted_by_node_id():
    t = _table()
    for ch in "c0fa":
        t.update(_card(_nid(ch)), now=T0)
    assert [p.node_id for p in t.alive(now=T0)] == sorted(_nid(c) for c in "c0fa5")


def test_own_id_card_refreshes_self():
    t = _table()
    p = t.update(_card(_nid("5"), load=7), now=T0)
    assert p is not None and p.load == 7
    assert len(t.alive(now=T0)) == 1 and t.alive(now=T0)[0].load == 7


def test_leader_min_started_then_node_id():
    t = _table(started_at=T0)
    assert t.leader(now=T0).node_id == _nid("5")
    t.update(_card(_nid("9"), started_at=T0 - 5), now=T0)
    t.update(_card(_nid("8"), started_at=T0 - 5), now=T0)
    assert t.leader(now=T0).node_id == _nid("8")   # tie on started_at -> min node_id
    t.update(_card(_nid("1"), started_at=T0 + 1), now=T0)
    assert t.leader(now=T0).node_id == _nid("8")
    assert t.leader(now=T0 + 21).node_id == _nid("5")   # older peers expired


def test_pick_role_load_ties():
    t = _table(roles=["worker"], load=5)
    t.update(_card(_nid("b"), roles=["worker", "gpu"], load=2), now=T0)
    t.update(_card(_nid("a"), roles=["gpu"], load=2), now=T0)
    t.update(_card(_nid("c"), roles=["worker"], load=1), now=T0)
    assert t.pick("worker", now=T0).node_id == _nid("c")
    assert t.pick("gpu", now=T0).node_id == _nid("a")     # tie on load -> min node_id
    assert t.pick("none", now=T0) is None
    assert t.pick("worker", now=T0 + 21).node_id == _nid("5")   # peers expired; self left


def test_snapshot_json_safe():
    t = _table()
    t.update(_card(_nid("a"), started_at=T0 - 1), now=T0)
    snap = t.snapshot(now=T0 + 2)
    assert json.loads(json.dumps(snap, allow_nan=False)) == snap
    assert snap["self"] == _nid("5") and snap["leader"] == _nid("a")
    assert snap["n_alive"] == 2 and [r["node_id"] for r in snap["peers"]] == [_nid("5"), _nid("a")]
    row = snap["peers"][1]
    assert row["age_s"] == 2 and row["self"] is False and row["roles"] == ["worker"]


def test_table_bounded(monkeypatch):
    monkeypatch.setattr(peers, "MAX_PEERS", 3)
    t = _table()
    ids = [f"{i:032x}" for i in range(1, 6)]
    got = [t.update(_card(n), now=T0) for n in ids]
    assert [g is not None for g in got] == [True, True, True, False, False]
    assert t.update(_card(ids[0], load=1), now=T0) is not None    # refresh still OK
    assert t.update(_card(ids[4]), now=T0 + 21) is not None       # room after expiry


def test_table_thread_safe():
    t = _table()

    def run(k):
        for i in range(200):
            t.update(_card(f"{k:02x}{i:030x}"), now=T0)
            t.alive(now=T0)
            t.leader(now=T0)

    ths = [threading.Thread(target=run, args=(k,)) for k in range(6)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    assert len(t.alive(now=T0)) == peers.MAX_PEERS + 1


def test_end_to_end_signed_card_into_table():
    ident = _ident()
    g = ReplayGuard()
    t = _table()
    raw = protocol.encode("announce", _card(_nid("e"), load=4), ident, now=T0)
    env = protocol.decode(raw, ident, g, now=T0)
    assert t.update(env["body"], now=T0).load == 4
