"""LAN mesh model calls: POST /mesh/v1/infer, infer.ask client, card ``models``, CLI models/ask.

Loopback only (127.0.0.1). A fake ollama (OpenAI chat API) runs on 127.0.0.1 port 0.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from apex_router import cli as main_cli
from apex_router.mesh import cli as mesh_cli
from apex_router.mesh import identity, infer, protocol, server
from apex_router.mesh.identity import Identity
from apex_router.mesh.peers import Peer, PeerTable, parse_card
from apex_router.mesh.server import MeshServer

LO = "127.0.0.1"
NODE_A, NODE_B = "a" * 32, "b" * 32
SECRET = bytes([9]) * 32
T0 = time.time() - 100
MODEL = "qwen3:8b"
P = "/mesh/v1/infer"


@pytest.fixture(autouse=True)
def _mesh_home(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_MESH_HOME", str(tmp_path / "meshhome"))


def _ident(node=NODE_A, secret=SECRET):
    return Identity(node_id=node, name="n", secret=secret)


def _card(node_id=NODE_A, port=47778, **kw):
    c = {"node_id": node_id, "name": "box", "host": LO, "port": port, "version": "1.0",
         "roles": [], "load": 0, "started_at": T0}
    c.update(kw)
    return c


# ---- fake ollama ----------------------------------------------------------------------------

class _Ollama:
    def __init__(self, mode="ok", delay=0.0):
        self.mode, self.delay, self.calls = mode, delay, []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                me.calls.append((self.path, body))
                if me.delay:
                    time.sleep(me.delay)
                if me.mode == "http500":
                    out, st = b'{"error":"boom"}', 500
                elif me.mode == "garbage":
                    out, st = b"not json", 200
                else:
                    out = json.dumps({"choices": [{"message": {
                        "role": "assistant", "content": "hello from fake",
                        "reasoning": "thought"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                                  "total_tokens": 8, "junk": "x"}}).encode()
                    st = 200
                self.send_response(st)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = ThreadingHTTPServer((LO, 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://{LO}:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def ollama(monkeypatch):
    o = _Ollama()
    monkeypatch.setenv("APEX_MESH_OLLAMA", o.url)
    yield o
    o.close()


def _start(models=(MODEL,), slots=1, node=NODE_A):
    ident = _ident(node)
    srv = MeshServer(ident, PeerTable(_card(node)), lambda: _card(node), host=LO, port=0,
                     models=models, infer_slots=slots)
    srv.start()
    return srv


@pytest.fixture()
def srv(ollama):
    s = _start()
    yield s
    s.stop()


def _body(model=MODEL, **kw):
    b = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    b.update(kw)
    return json.dumps(b).encode()


def _hdr(body, *, ident=None, to=NODE_A, path=P, sha=None):
    sha = hashlib.sha256(body).hexdigest() if sha is None else sha
    return protocol.auth_header(ident or _ident(NODE_B), path, to, body_sha256=sha)


def _post(s, body, headers=None, *, path=P, length=True, send=True):
    host, port = s.address
    c = http.client.HTTPConnection(host, port, timeout=10)
    try:
        c.putrequest("POST", path, skip_accept_encoding=True)
        if length is True:
            c.putheader("Content-Length", str(len(body)))
        elif length:
            c.putheader("Content-Length", length)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        c.endheaders()
        if send:
            try:
                c.send(body)
            except OSError:
                pass
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def _ok_post(s, body=None, **kw):
    body = _body() if body is None else body
    return _post(s, body, {server.AUTH_HEADER: _hdr(body, **kw)})


# ---- server: gating and auth ----------------------------------------------------------------

def test_no_serve_model_404(ollama):
    s = _start(models=())
    try:
        st, body = _ok_post(s)
        assert st == 404 and json.loads(body) == {"error": "not_found"}
    finally:
        s.stop()


def test_post_other_path_unchanged(srv):
    assert _post(srv, b"", path="/mesh/v1/card")[0] == 501
    assert _post(srv, b"x", {server.AUTH_HEADER: _hdr(b"x", path=P + "?a=1")},
                 path=P + "?a=1")[0] == 404


def test_bad_auth_401(srv):
    body = _body()
    cases = [
        {},                                                         # no header
        {server.AUTH_HEADER: "junk"},
        {server.AUTH_HEADER: _hdr(body, ident=_ident(NODE_B, bytes([1]) * 32))},  # other secret
        {server.AUTH_HEADER: _hdr(body, to=NODE_B)},                # wrong recipient
        {server.AUTH_HEADER: _hdr(body, path="/mesh/v1/peers")},    # wrong path
        {server.AUTH_HEADER: _hdr(b"other body")},                  # body hash mismatch
        {server.AUTH_HEADER: protocol.auth_header(_ident(NODE_B), P, NODE_A)},  # no body_sha256
    ]
    for h in cases:
        st, out = _post(srv, body, h)
        assert st == 401, h
        assert json.loads(out) == {"error": "unauthorized"}


def test_get_header_not_valid_for_post_and_reverse(srv):
    body = _body()
    # A POST auth (with body_sha256) must not open a GET route.
    host, port = srv.address
    c = http.client.HTTPConnection(host, port, timeout=5)
    c.putrequest("GET", "/mesh/v1/peers")
    c.putheader(server.AUTH_HEADER, _hdr(body, path="/mesh/v1/peers"))
    c.endheaders()
    assert c.getresponse().status == 401
    c.close()


def test_replay_401(srv):
    body = _body()
    h = {server.AUTH_HEADER: _hdr(body)}
    assert _post(srv, body, h)[0] == 200
    assert _post(srv, body, h)[0] == 401


def test_hash_mismatch_does_not_burn_nonce(srv):
    body = _body()
    h = {server.AUTH_HEADER: _hdr(body)}
    assert _post(srv, _body(max_tokens=17), h)[0] == 401
    assert _post(srv, body, h)[0] == 200


def test_too_big_413_and_no_length_411(srv):
    big = b"x" * (infer.MAX_REQUEST_BYTES + 1)
    st, out = _post(srv, big, {server.AUTH_HEADER: _hdr(big)}, send=False)
    assert st == 413 and json.loads(out) == {"error": "too_large"}
    body = _body()
    st, out = _post(srv, body, {server.AUTH_HEADER: _hdr(body)}, length=None, send=False)
    assert st == 411 and json.loads(out) == {"error": "length_required"}
    for bad in ("-1", "abc", "1e3"):
        st, _ = _post(srv, body, {server.AUTH_HEADER: _hdr(body)}, length=bad, send=False)
        assert st == 400, bad


def test_too_big_before_auth_body_never_read(srv):
    # Auth fails first: 401 even with a huge length, and the body is never read.
    st, _ = _post(srv, b"", {}, length=str(10 ** 9), send=False)
    assert st == 401


def test_short_body_times_out(srv, monkeypatch):
    monkeypatch.setattr(server, "BODY_DEADLINE_S", 0.5)
    body = _body()
    s = socket.create_connection(srv.address, timeout=5)
    try:
        req = (f"POST {P} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n"
               f"{server.AUTH_HEADER}: {_hdr(body)}\r\n\r\n").encode("latin-1") + body[:5]
        s.sendall(req)
        t0 = time.monotonic()
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        assert time.monotonic() - t0 < 4
        assert b" 200 " not in data[:20]
    finally:
        s.close()


@pytest.mark.parametrize("raw,err", [
    (b"not json", "bad_json"),
    (b"[1,2]", "bad_request"),
    (json.dumps({"model": MODEL, "messages": [], "max_tokens": 5}).encode(), "bad_messages"),
    (json.dumps({"model": MODEL, "messages": [{"role": "tool", "content": "x"}],
                 "max_tokens": 5}).encode(), "bad_messages"),
    (json.dumps({"model": MODEL, "messages": [{"role": "user", "content": 5}],
                 "max_tokens": 5}).encode(), "bad_messages"),
    (json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "x"}] * 65,
                 "max_tokens": 5}).encode(), "bad_messages"),
    (_body(max_tokens=0), "bad_max_tokens"),
    (_body(max_tokens=8193), "bad_max_tokens"),
    (_body(max_tokens=True), "bad_max_tokens"),
    (_body(temperature=2.5), "bad_temperature"),
    (_body(temperature="1"), "bad_temperature"),
    (_body(model="bad model!"), "bad_model"),
    (_body(extra=1), "bad_request"),
    (b'{"model": "m", "messages": [], "max_tokens": NaN}', "bad_json"),
])
def test_bad_json_400(srv, raw, err):
    st, out = _ok_post(srv, raw)
    assert st == 400 and json.loads(out) == {"error": err}


# ---- server: policy and upstream -------------------------------------------------------------

def test_not_served_403(srv, ollama):
    st, out = _ok_post(srv, _body(model="llama3:70b"))
    assert st == 403 and json.loads(out) == {"error": "not_served"}
    assert ollama.calls == []


def test_busy_429(monkeypatch):
    o = _Ollama(delay=1.0)
    monkeypatch.setenv("APEX_MESH_OLLAMA", o.url)
    s = _start(slots=1)
    try:
        box = []
        th = threading.Thread(target=lambda: box.append(_ok_post(s)))
        th.start()
        assert _wait(lambda: s.infer_running == 1)
        t0 = time.monotonic()
        st, out = _ok_post(s)
        assert st == 429 and json.loads(out) == {"error": "busy"}
        assert time.monotonic() - t0 < 0.8                # at once, no wait
        th.join(5)
        assert box[0][0] == 200 and s.infer_running == 0
    finally:
        s.stop()
        o.close()


def test_infer_slots_bounds(ollama):
    for bad in (0, 5, True):
        with pytest.raises(ValueError):
            MeshServer(_ident(), PeerTable(_card()), _card, host=LO, port=0, models=(MODEL,),
                       infer_slots=bad)


def test_upstream_down_502(monkeypatch):
    with socket.socket() as sk:
        sk.bind((LO, 0))
        port = sk.getsockname()[1]
    monkeypatch.setenv("APEX_MESH_OLLAMA", f"http://{LO}:{port}")
    s = _start()
    try:
        st, out = _ok_post(s)
        assert st == 502 and json.loads(out) == {"error": "upstream_unreachable"}
        assert b"Traceback" not in out
    finally:
        s.stop()


@pytest.mark.parametrize("mode,err", [("http500", "upstream_http_500"),
                                      ("garbage", "upstream_bad_response")])
def test_upstream_bad_502(monkeypatch, mode, err):
    o = _Ollama(mode=mode)
    monkeypatch.setenv("APEX_MESH_OLLAMA", o.url)
    s = _start()
    try:
        st, out = _ok_post(s)
        assert st == 502 and json.loads(out) == {"error": err}
    finally:
        s.stop()
        o.close()


def test_bad_ollama_env(monkeypatch):
    for bad in ("ftp://x", "http://", "not a url", "http://h/x?q=1"):
        monkeypatch.setenv("APEX_MESH_OLLAMA", bad)
        with pytest.raises(ValueError):
            MeshServer(_ident(), PeerTable(_card()), _card, host=LO, port=0, models=(MODEL,))


def test_happy_path_raw_envelope(srv, ollama):
    st, out = _ok_post(srv, _body(temperature=0.2))
    assert st == 200
    env = protocol.decode(out, _ident(NODE_B), None, max_bytes=1 << 20)
    assert env["kind"] == "infer"
    b = env["body"]
    assert set(b) == {"node_id", "model", "content", "reasoning", "finish_reason", "usage",
                      "latency_s"}
    assert b["node_id"] == NODE_A and b["model"] == MODEL
    assert b["content"] == "hello from fake" and b["reasoning"] == "thought"
    assert b["usage"] == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    path, sent = ollama.calls[0]
    assert path == "/v1/chat/completions"
    assert sent == {"model": MODEL, "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 16, "temperature": 0.2, "stream": False}


def _peer(s, node=NODE_A, **kw):
    port = s.address[1]
    return parse_card(_card(node, port=port, **kw), now=time.time())


def test_ask_happy_path(srv):
    r = infer.ask(_peer(srv), _ident(NODE_B), MODEL, [{"role": "user", "content": "hi"}], 16)
    assert r["ok"] is True, r
    assert r["content"] == "hello from fake" and r["reasoning"] == "thought"
    assert r["finish_reason"] == "stop" and r["node_id"] == NODE_A
    assert r["usage"]["total_tokens"] == 8 and r["latency_s"] >= 0


def test_ask_errors_never_raise(srv):
    msgs = [{"role": "user", "content": "hi"}]
    r = infer.ask(_peer(srv), _ident(NODE_B), "other:1b", msgs, 16)
    assert r["ok"] is False and r["error"] == "http 403 not_served"
    wrong = _peer(srv, node=NODE_B)                   # server is A; header goes to B
    assert infer.ask(wrong, _ident(NODE_B), MODEL, msgs, 16)["error"] == "http 401 unauthorized"
    other_mesh = _ident(NODE_B, bytes([1]) * 32)
    assert infer.ask(_peer(srv), other_mesh, MODEL, msgs, 16)["ok"] is False
    assert infer.ask(_peer(srv), _ident(NODE_B), MODEL, "x", 16)["error"] == "bad_messages"
    assert infer.ask(_peer(srv), _ident(NODE_B), MODEL, msgs, 0)["error"] == "bad_max_tokens"
    with socket.socket() as sk:
        sk.bind((LO, 0))
        dead = sk.getsockname()[1]
    r = infer.ask(parse_card(_card(port=dead), now=time.time()), _ident(NODE_B), MODEL, msgs, 16)
    assert r["ok"] is False and r["error"].startswith("unreachable")
    assert infer.ask(None, None, MODEL, msgs, 16)["ok"] is False


class _FakeMesh:
    """Mesh-like HTTP server that answers every POST with a fixed (signed) payload."""

    def __init__(self, payload):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer((LO, 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.address = self.httpd.server_address

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.mark.parametrize("kind,body,err", [
    ("data", {"node_id": NODE_A}, "bad response: kind"),
    ("infer", {"node_id": NODE_B, "model": MODEL, "content": "x", "reasoning": None,
               "finish_reason": "stop", "usage": {}, "latency_s": 0.1}, "bad response: node_id"),
    ("infer", {"node_id": NODE_A, "model": MODEL, "content": 5, "reasoning": None,
               "finish_reason": "stop", "usage": {}, "latency_s": 0.1}, "bad response: content"),
])
def test_ask_verifies_response(kind, body, err):
    f = _FakeMesh(protocol.encode(kind, body, _ident(NODE_A)))
    try:
        peer = parse_card(_card(port=f.address[1]), now=time.time())
        r = infer.ask(peer, _ident(NODE_B), MODEL, [{"role": "user", "content": "hi"}], 16)
        assert r["ok"] is False and r["error"].startswith(err), r
    finally:
        f.close()


def test_ask_rejects_replayed_response():
    payload = protocol.encode("infer", {"node_id": NODE_A, "model": MODEL, "content": "x",
                                        "reasoning": None, "finish_reason": "stop", "usage": {},
                                        "latency_s": 0.1}, _ident(NODE_A))
    f = _FakeMesh(payload)
    try:
        peer = parse_card(_card(port=f.address[1]), now=time.time())
        msgs = [{"role": "user", "content": "hi"}]
        assert infer.ask(peer, _ident(NODE_B), MODEL, msgs, 16)["ok"] is True
        r = infer.ask(peer, _ident(NODE_B), MODEL, msgs, 16)
        assert r["ok"] is False and "replay" in r["error"]
    finally:
        f.close()


# ---- card models -----------------------------------------------------------------------------

def test_card_models_parse_and_limits():
    now = time.time()
    p = parse_card(_card(models=[MODEL, "llama3.1:8b", MODEL]), now=now)
    assert isinstance(p, Peer) and p.models == (MODEL, "llama3.1:8b")
    assert p.to_dict()["models"] == [MODEL, "llama3.1:8b"]
    old = parse_card(_card(), now=now)                 # old card without models stays valid
    assert old is not None and old.models == ()
    assert parse_card(_card(models=[f"m{i}" for i in range(16)]), now=now) is not None
    assert parse_card(_card(models=["a" * 128]), now=now) is not None
    assert parse_card(_card(models=["hf.co/org/x:Q4_K_M"]), now=now) is not None
    for bad in ([f"m{i}" for i in range(17)], ["a" * 129], ["bad model"], ["x;rm"], [""],
                [5], "qwen", {"a": 1}, None):
        assert parse_card(_card(models=bad), now=now) is None, bad


# ---- CLI -------------------------------------------------------------------------------------

def _run(capsys, *argv):
    rc = main_cli.main(["mesh", *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


def _row(node_id, *, port=47778, load=0, models=(), name="box", last_seen=None):
    return {"node_id": node_id, "name": name, "host": LO, "port": port, "version": "1.0",
            "roles": [], "load": load, "started_at": T0, "models": list(models),
            "last_seen": time.time() if last_seen is None else last_seen}


def _write_peers(home, ident, rows):
    home.mkdir(parents=True, exist_ok=True)
    (home / "peers.json").write_text(json.dumps(
        {"self": ident.node_id, "leader": None, "mesh_id": ident.mesh_id, "ts": time.time(),
         "peers": rows}))


def _home():
    return identity.mesh_home()


def test_cli_models(capsys):
    ident = identity.init(name="me")
    _write_peers(_home(), ident, [
        _row(ident.node_id, name="me"),
        _row("1" * 32, name="gpu1", models=[MODEL, "llama3:8b"]),
        _row("2" * 32, name="stale", models=["old:1b"], last_seen=time.time() - 999)])
    rc, out, _ = _run(capsys, "models", "--json")
    assert rc == 0
    rows = json.loads(out)["models"]
    assert [(r["name"], r["model"]) for r in rows] == [("gpu1", "llama3:8b"), ("gpu1", MODEL)]
    rc, out, _ = _run(capsys, "models")
    assert rc == 0 and "gpu1" in out and MODEL in out and "old:1b" not in out


def test_cli_ask_usage_and_none(capsys):
    ident = identity.init(name="me")
    _write_peers(_home(), ident, [_row(ident.node_id, name="me")])
    assert _run(capsys, "ask", "--model", MODEL, "hi")[0] == 1          # nobody serves it
    assert _run(capsys, "ask", "--model", MODEL, "--max-tokens", "0", "hi")[0] == 2
    assert _run(capsys, "ask", "--model", "bad model", "hi")[0] == 2
    assert _run(capsys, "ask", "--model", MODEL, "")[0] == 2
    with pytest.raises(SystemExit) as e:
        main_cli.main(["mesh", "ask", "hi"])                               # no --model
    assert e.value.code == 2


def test_cli_ask_picks_min_load_and_node(capsys, monkeypatch, ollama):
    # Two real servers with one identity secret; peers.json lists both with loads.
    ident = identity.init(name="me")
    sa = _start(node="1" * 32)
    sb = _start(node="2" * 32)
    try:
        sa.ident = Identity(node_id="1" * 32, name="a", secret=ident.secret)
        sb.ident = Identity(node_id="2" * 32, name="b", secret=ident.secret)
        _write_peers(_home(), ident, [
            _row(ident.node_id, name="me"),
            _row("1" * 32, name="a", port=sa.address[1], load=3, models=[MODEL]),
            _row("2" * 32, name="b", port=sb.address[1], load=0, models=[MODEL])])
        rc, out, _ = _run(capsys, "ask", "--model", MODEL, "--json", "hi")
        assert rc == 0, out
        r = json.loads(out)
        assert r["ok"] and r["node_id"] == "2" * 32 and r["content"] == "hello from fake"
        rc, out, _ = _run(capsys, "ask", "--model", MODEL, "--node", "a", "--json", "hi")
        assert rc == 0 and json.loads(out)["node_id"] == "1" * 32
        assert _run(capsys, "ask", "--model", MODEL, "--node", "zzz", "hi")[0] == 1
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("from stdin"))
        rc, out, _ = _run(capsys, "ask", "--model", MODEL, "--system", "be short", "-")
        assert rc == 0 and out.strip() == "hello from fake"
        sent = ollama.calls[-1][1]["messages"]
        assert sent == [{"role": "system", "content": "be short"},
                        {"role": "user", "content": "from stdin"}]
        sb.stop()                                       # chosen node down -> exit 1
        assert _run(capsys, "ask", "--model", MODEL, "hi")[0] == 1
    finally:
        sa.stop()
        sb.stop()


def test_cli_up_rejects_bad_serve_model(tmp_path, monkeypatch):
    identity.init()
    for argv in (["up", "--bind", LO, "--serve-model", "bad model"],
                 ["up", "--bind", LO, "--serve-model", MODEL, "--infer-slots", "5"],
                 ["up", "--bind", LO, *[a for i in range(17) for a in ("--serve-model", f"m{i}")]]):
        args = mesh_cli.build_parser().parse_args(argv)
        assert mesh_cli.run_up(args, threading.Event()) == 2


# ---- end to end: two run_up nodes on 127.0.0.1 -----------------------------------------------

def _wait(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _free_udp():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((LO, 0))
        return s.getsockname()[1]


def test_e2e_two_nodes(tmp_path, monkeypatch, capsys):
    o = _Ollama(delay=0.6)
    monkeypatch.setenv("APEX_MESH_OLLAMA", o.url)
    monkeypatch.setattr(mesh_cli, "_local_load", lambda: 0)
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("APEX_MESH_HOME", str(home_a))
    ia = identity.init(name="node-a")
    monkeypatch.setenv("APEX_MESH_HOME", str(home_b))
    ib = identity.init(name="node-b", secret=ia.secret)
    udp = {ia.node_id: _free_udp(), ib.node_id: _free_udp()}

    from apex_router.mesh import beacon as real_beacon

    class _Beacon(real_beacon.Beacon):
        def __init__(self, ident, table, card_fn, **kw):
            super().__init__(ident, table, card_fn, port=udp[ident.node_id], interval_s=0.1,
                             **kw)

    fake = type(sys)("apex_router.mesh.beacon")
    fake.Beacon = _Beacon
    monkeypatch.setitem(sys.modules, "apex_router.mesh.beacon", fake)

    stops, threads = [], []

    def _up(home, argv):
        monkeypatch.setenv("APEX_MESH_HOME", str(home))
        args = mesh_cli.build_parser().parse_args(argv)
        stop = threading.Event()
        th = threading.Thread(target=mesh_cli.run_up, args=(args, stop),
                              kwargs={"interval_s": 0.1}, daemon=True)
        th.start()
        stops.append(stop)
        threads.append(th)
        assert _wait(lambda: (home / "mesh.pid").exists())

    try:
        _up(home_a, ["up", "--bind", LO, "--port", "0", "--serve-model", MODEL,
                     "--peer", f"{LO}:{udp[ib.node_id]}"])
        _up(home_b, ["up", "--bind", LO, "--port", "0", "--peer", f"{LO}:{udp[ia.node_id]}"])
        # B (current APEX_MESH_HOME) must see A with its model.

        def _b_sees_model():
            v = mesh_cli.read_peers(ib, home_b)
            return v and any(MODEL in (r.get("models") or []) for r in v["peers"])
        assert _wait(_b_sees_model)
        capsys.readouterr()                                     # drop `mesh up` lines
        rc, out, _ = _run(capsys, "models", "--json")
        assert rc == 0 and [(r["node_id"], r["model"]) for r in json.loads(out)["models"]] \
            == [(ia.node_id, MODEL)]
        port_a = json.loads((home_a / "mesh.pid").read_text())["port"]
        capsys.readouterr()                                     # drop `mesh up` lines
        box = []
        th = threading.Thread(target=lambda: box.append(
            main_cli.main(["mesh", "ask", "--model", MODEL, "--json", "hello?"])))
        th.start()

        def _a_load():
            c = http.client.HTTPConnection(LO, port_a, timeout=3)
            c.request("GET", "/mesh/v1/card")
            env = protocol.decode(c.getresponse().read(), ib, None, max_bytes=1 << 20)
            c.close()
            return env["body"]["load"]
        assert _wait(lambda: _a_load() >= 1, timeout=5)          # running inference in load
        th.join(10)
        assert box == [0]
        out = capsys.readouterr().out
        r = json.loads(out)
        assert r["ok"] and r["node_id"] == ia.node_id and r["content"] == "hello from fake"
        assert _wait(lambda: _a_load() == 0, timeout=5)
    finally:
        for s in stops:
            s.set()
        for t in threads:
            t.join(5)
        o.close()
