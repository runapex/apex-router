"""Mesh worker, sender side: GET /mesh/v1/jobs and /mesh/v1/results, sender.py clients, and the
CLI `mesh submit | result | jobs | worker`.

Loopback only (127.0.0.1). APEX_MESH_HOME is under tmp_path. Git runs only in temp repos.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import stat
import subprocess
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from apex_router import cli as main_cli
from apex_router.mesh import cli as mesh_cli
from apex_router.mesh import identity, jobs, protocol, sender, server
from apex_router.mesh.identity import Identity
from apex_router.mesh.peers import Peer, PeerTable
from apex_router.mesh.server import MeshServer

LO = "127.0.0.1"
W_ID = "b" * 32                      # worker node
C_ID = "c" * 32                      # another node
T0 = time.time() - 100


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "meshhome"
    monkeypatch.setenv("APEX_MESH_HOME", str(h))
    return h


@pytest.fixture()
def me(home):
    return identity.init(name="sender")


def _w(me_ident, node=W_ID, name="worker"):
    return Identity(node_id=node, name=name, secret=me_ident.secret)


def _card(node_id, port=47778, name="box"):
    return {"node_id": node_id, "name": name, "host": LO, "port": port, "version": "1.0",
            "roles": [], "load": 0, "started_at": T0}


def _start(ident, h, alive=()):
    table = PeerTable(_card(ident.node_id, name=ident.name))
    for n in alive:
        table.update(_card(n))
    srv = MeshServer(ident, table, lambda: _card(ident.node_id, name=ident.name),
                     host=LO, port=0, home=h)
    srv.start()
    return srv


def _peer(srv, name="box"):
    host, port = srv.address
    return Peer(node_id=srv.ident.node_id, name=name, host=host, port=port, version="1.0",
                roles=(), load=0, started_at=T0, last_seen=time.time())


def _get(srv, target, header=None):
    host, port = srv.address
    c = http.client.HTTPConnection(host, port, timeout=5)
    try:
        c.putrequest("GET", target)
        if header is not None:
            c.putheader(server.AUTH_HEADER, header)
        c.endheaders()
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def _auth_get(srv, target, ident, to=None):
    return _get(srv, target, protocol.auth_header(ident, target, to or srv.ident.node_id))


def _row(node_id, name, port=47778, last_seen=None):
    return {**_card(node_id, port=port, name=name),
            "last_seen": time.time() if last_seen is None else last_seen}


def _write_peers(home, ident, rows):
    home.mkdir(parents=True, exist_ok=True)
    (home / "peers.json").write_text(json.dumps(
        {"self": ident.node_id, "leader": None, "mesh_id": ident.mesh_id, "ts": time.time(),
         "peers": [_row(ident.node_id, ident.name), *rows]}))


def _run(capsys, *argv):
    rc = main_cli.main(["mesh", *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


def _result(jid, status="done", patch=b"", report="ok DONE", reason=""):
    return {"id": jid, "status": status, "reason": reason,
            "steps": [{"effort": "medium", "model": jobs.SONNET, "pass": status == "done",
                       "s": 1.5, "cost_usd": 0.01}],
            "patch_b64": base64.b64encode(patch).decode("ascii"), "report": report}


# ---- endpoints: auth and query --------------------------------------------------------------

@pytest.mark.parametrize("base", [jobs.JOBS_PATH, jobs.RESULTS_PATH])
def test_endpoint_needs_auth(me, home, base):
    srv = _start(me, home, alive=[W_ID])
    try:
        w = _w(me)
        target = f"{base}?for={W_ID}"
        assert _get(srv, target)[0] == 401
        assert _get(srv, target, "junk")[0] == 401
        other = Identity(node_id=W_ID, name="x", secret=bytes([1]) * 32)
        assert _auth_get(srv, target, other)[0] == 401                     # other mesh
        assert _auth_get(srv, target, w, to=C_ID)[0] == 401                # wrong recipient
        assert _get(srv, target, protocol.auth_header(w, f"{base}?for={C_ID}",
                                                      me.node_id))[0] == 401  # wrong path
        old = protocol.auth_header(w, target, me.node_id, now=time.time() - 120)
        assert _get(srv, target, old)[0] == 401                            # skew
        hdr = protocol.auth_header(w, target, me.node_id)
        st, body = _get(srv, target, hdr)
        assert st == 200
        assert _get(srv, target, hdr)[0] == 401                            # replay
        st, body = _get(srv, target, "junk")
        assert json.loads(body) == {"error": "unauthorized"}
    finally:
        srv.stop()


@pytest.mark.parametrize("base", [jobs.JOBS_PATH, jobs.RESULTS_PATH])
def test_endpoint_bad_query(me, home, base):
    srv = _start(me, home, alive=[W_ID])
    try:
        w = _w(me)
        for q in ("", "?for=", f"?for={W_ID.upper()}", f"?for={W_ID}&x=1",
                  f"?for={W_ID}&for={W_ID}", f"?names={W_ID}", f"?for={W_ID[:31]}",
                  f"?for={W_ID}0", f"?for=%62{W_ID[1:]}"):
            st, body = _auth_get(srv, base + q, w)
            assert st == 400, q
            assert json.loads(body) == {"error": "bad_query"}
    finally:
        srv.stop()


@pytest.mark.parametrize("base", [jobs.JOBS_PATH, jobs.RESULTS_PATH])
def test_endpoint_unknown_for(me, home, base):
    srv = _start(me, home, alive=[W_ID])
    try:
        st, body = _auth_get(srv, f"{base}?for={C_ID}", _w(me))
        assert st == 403 and json.loads(body) == {"error": "unknown_node"}
    finally:
        srv.stop()


def test_endpoint_other_paths_unchanged(me, home):
    srv = _start(me, home, alive=[W_ID])
    try:
        assert _auth_get(srv, jobs.JOBS_PATH + "/x?for=" + W_ID, _w(me))[0] == 404
        host, port = srv.address
        c = http.client.HTTPConnection(host, port, timeout=5)
        c.request("POST", jobs.JOBS_PATH, body=b"")
        assert c.getresponse().status == 501
        c.close()
    finally:
        srv.stop()


# ---- /jobs: offered set, expiry, client ------------------------------------------------------

def test_jobs_offered_expiry_and_fetch_jobs(me, home):
    j_ok = sender.submit(me, to=W_ID, repo="apex-router", ref="main", task="do it")
    sender.submit(me, to=C_ID, repo="apex-router", ref="main", task="not for W")
    sender.submit(me, to=W_ID, repo="apex-router", ref="main", task="old",
                  expires_s=60, now=time.time() - 120)                      # expired
    st = os.stat(jobs.outbox(home) / f"{j_ok}.job")
    assert stat.S_IMODE(st.st_mode) == 0o600
    srv = _start(me, home, alive=[W_ID, C_ID])
    w = _w(me)
    try:
        st, raw = _auth_get(srv, f"{jobs.JOBS_PATH}?for={W_ID}", w)
        assert st == 200
        env = protocol.decode(raw, w, None, max_bytes=1 << 20)
        assert env["kind"] == "jobs" and set(env["body"]) == {"jobs"}
        assert len(env["body"]["jobs"]) == 1
        res = sender.fetch_jobs(_peer(srv), w, timeout=5)
        assert res["ok"] and len(res["jobs"]) == 1
        body = jobs.open_job(res["jobs"][0], w)
        assert body["id"] == j_ok and body["to"] == W_ID and body["from"] == me.node_id
        # A job with a stored result is no longer offered.
        sender.store_result(_result(j_ok))
        res = sender.fetch_jobs(_peer(srv), w, timeout=5)
        assert res["ok"] and res["jobs"] == []
    finally:
        srv.stop()


def test_fetch_never_raises(me):
    dead = Peer(node_id=W_ID, name="dead", host=LO, port=1, version="1", roles=(), load=0,
                started_at=T0, last_seen=time.time())
    for fn in (sender.fetch_jobs, sender.fetch_results):
        r = fn(dead, me, timeout=1)
        assert r["ok"] is False and r["error"].startswith("unreachable")
        r = fn(None, me, timeout=1)                                         # garbage peer
        assert r["ok"] is False and "error" in r


# ---- /results: filtered by sender, client checks ---------------------------------------------

def test_results_filtered_by_sender(me, home, tmp_path):
    wh = tmp_path / "workerhome"
    w = _w(me)
    jobs.write_result(_result("1" * 16), me.node_id, h=wh)
    jobs.write_result(_result("2" * 16, status="failed"), C_ID, h=wh)
    jobs.write_result(_result("3" * 16, status="running"), me.node_id, h=wh)
    srv = _start(w, wh, alive=[me.node_id, C_ID])
    try:
        res = sender.fetch_results(_peer(srv), me, timeout=5)
        assert res["ok"], res
        assert sorted(r["id"] for r in res["results"]) == ["1" * 16, "3" * 16]
        assert all("_from" not in r for r in res["results"])
        c = Identity(node_id=C_ID, name="c", secret=me.secret)
        res = sender.fetch_results(_peer(srv), c, timeout=5)
        assert [r["id"] for r in res["results"]] == ["2" * 16]
    finally:
        srv.stop()


class _Fake:
    """HTTP server that returns one fixed body (to test client checks)."""

    def __init__(self, payload: bytes, status=200):
        me = self
        self.payload, self.status = payload, status

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                self.send_response(me.status)
                self.send_header("Content-Length", str(len(me.payload)))
                self.end_headers()
                self.wfile.write(me.payload)

        self.httpd = ThreadingHTTPServer((LO, 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def peer(self, node=W_ID):
        return Peer(node_id=node, name="fake", host=LO, port=self.httpd.server_address[1],
                    version="1", roles=(), load=0, started_at=T0, last_seen=time.time())

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_fetch_results_checks_response(me):
    w = _w(me)
    cases = [
        (protocol.encode("results", {"node_id": C_ID, "results": []}, w), "node_id mismatch"),
        (protocol.encode("jobs", {"node_id": W_ID, "results": []}, w), "kind"),
        (protocol.encode("results", {"node_id": W_ID, "results": []},
                         Identity(node_id=W_ID, name="x", secret=bytes([3]) * 32)), "bad response"),
        (protocol.encode("results", {"node_id": W_ID, "results": "x"}, w), "results"),
        (b"not json", "bad response"),
    ]
    for payload, err in cases:
        f = _Fake(payload)
        try:
            r = sender.fetch_results(f.peer(), me, timeout=5)
            assert r["ok"] is False and err in r["error"], (err, r)
        finally:
            f.close()
    good = _result("4" * 16)
    bad = dict(_result("5" * 16), status="weird")
    f = _Fake(protocol.encode("results", {"node_id": W_ID, "results": [good, bad, "x"]}, w))
    try:
        r = sender.fetch_results(f.peer(), me, timeout=5)
        assert r["ok"] and [x["id"] for x in r["results"]] == ["4" * 16] and r["dropped"] == 2
    finally:
        f.close()
    f = _Fake(b'{"error": "unauthorized"}', status=401)
    try:
        assert sender.fetch_results(f.peer(), me, timeout=5)["error"] == "http 401"
    finally:
        f.close()


def test_fetch_jobs_checks_response(me):
    w = _w(me)
    for body in ({"jobs": "x"}, {"jobs": ["a"] * (jobs.MAX_OFFERED + 1)}, {"jobs": [1]}):
        f = _Fake(protocol.encode("jobs", body, me))
        try:
            r = sender.fetch_jobs(f.peer(me.node_id), w, timeout=5)
            assert r["ok"] is False and r["error"] == "bad response: jobs"
        finally:
            f.close()


# ---- CLI: submit / jobs -------------------------------------------------------------------

def test_cli_submit(me, home, capsys):
    _write_peers(home, me, [_row(W_ID, "worker")])
    rc, out, err = _run(capsys, "submit", "--to", "worker", "--repo", "apex-router",
                        "--ref", "main", "--check", "make test", "--max-effort", "xhigh",
                        "--expires-min", "10", "fix the bug")
    assert rc == 0, err
    jid = out.strip()
    assert jobs.valid_id(jid)
    body = sender.read_job(jid, me)
    assert body["to"] == W_ID and body["check"] == "make test" and body["max_effort"] == "xhigh"
    assert body["expires"] - body["created"] == 600
    rc, out, _ = _run(capsys, "submit", "--to", W_ID, "--repo", "r", "--ref", "v1", "t")
    assert rc == 0 and jobs.valid_id(out.strip())


@pytest.mark.parametrize("argv,reason", [
    (["--to", "nobody"], "unknown_worker"),
    (["--ref", "-x"], "bad_ref"),
    (["--ref", "a..b"], "bad_ref"),
    (["--repo", "a/b"], "bad_repo"),
    (["--max-effort", "medium"], "bad_max_effort"),
    (["--expires-min", "0"], "bad_expires"),
    (["--expires-min", "61"], "bad_expires"),
    (["--check", "x" * 2000], "bad_check"),
])
def test_cli_submit_bad_field(me, home, capsys, argv, reason):
    _write_peers(home, me, [_row(W_ID, "worker")])
    base = {"--to": "worker", "--repo": "apex-router", "--ref": "main"}
    for i in range(0, len(argv), 2):
        base[argv[i]] = argv[i + 1]
    flat = [f"{k}={v}" for k, v in base.items()]
    rc, out, err = _run(capsys, "submit", *flat, "task")
    assert rc == 2 and reason in err and out == ""
    assert not jobs.outbox(home).exists() or not list(jobs.outbox(home).glob("*.job"))


def test_cli_submit_empty_task_and_ambiguous(me, home, capsys):
    _write_peers(home, me, [_row(W_ID, "worker"), _row(C_ID, "worker")])
    rc, _, err = _run(capsys, "submit", "--to", W_ID, "--repo", "r", "--ref", "main", " ")
    assert rc == 2 and "bad_task" in err
    rc, _, err = _run(capsys, "submit", "--to", "worker", "--repo", "r", "--ref", "main", "t")
    assert rc == 2 and "ambiguous_worker" in err


def test_cli_jobs(me, home, capsys):
    _write_peers(home, me, [_row(W_ID, "worker")])
    a = sender.submit(me, to=W_ID, repo="apex-router", ref="main", task="a", now=time.time() - 3)
    b = sender.submit(me, to=W_ID, repo="r2", ref="dev", task="b", expires_s=60,
                      now=time.time() - 120)
    c = sender.submit(me, to=C_ID, repo="r3", ref="main", task="c", now=time.time() - 1)
    d = sender.submit(me, to=W_ID, repo="r4", ref="main", task="d")
    sender.store_result(_result(c, status="failed"))
    sender.store_result(_result(d, status="running"))
    rc, out, _ = _run(capsys, "jobs", "--json")
    assert rc == 0
    rows = {r["id"]: r for r in json.loads(out)["jobs"]}
    assert {k: rows[k]["state"] for k in rows} == {a: "offered", b: "expired", c: "failed",
                                                  d: "running"}
    assert rows[a]["to_name"] == "worker" and rows[c]["to_name"] == C_ID[:12]
    assert rows[a]["repo"] == "apex-router" and rows[a]["ref"] == "main"
    rc, out, _ = _run(capsys, "jobs")
    assert rc == 0 and a in out and "offered" in out and "expired" in out


# ---- CLI: result (two nodes on loopback) --------------------------------------------------

@pytest.fixture()
def two_nodes(me, home, tmp_path, monkeypatch):
    """Sender `me` (home) and worker W (own home) with a running worker server."""
    wh = tmp_path / "workerhome"
    w = _w(me)
    srv_w = _start(w, wh, alive=[me.node_id])
    srv_s = _start(me, home, alive=[W_ID])
    _write_peers(home, me, [_row(W_ID, "worker", port=srv_w.address[1])])
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    yield types.SimpleNamespace(w=w, wh=wh, srv_w=srv_w, srv_s=srv_s, cwd=cwd)
    srv_w.stop()
    srv_s.stop()


def test_cli_result_end_to_end(me, home, two_nodes, capsys):
    t = two_nodes
    rc, out, _ = _run(capsys, "submit", "--to", "worker", "--repo", "apex-router", "--ref",
                      "main", "task")
    jid = out.strip()
    # Worker side: pull the job from the sender's server and check it.
    got = sender.fetch_jobs(_peer(t.srv_s), t.w, timeout=5)
    assert got["ok"] and jobs.open_job(got["jobs"][0], t.w)["id"] == jid
    # No result yet: pending, exit 1, no files.
    rc, out, _ = _run(capsys, "result", jid, "--json")
    assert rc == 1 and json.loads(out)["state"] == "offered"
    assert not list(t.cwd.iterdir())
    patch = b"From 0000 Mon Sep 17 00:00:00 2001\nSubject: [PATCH] x\n\n---\n"
    jobs.write_result(_result(jid, patch=patch, report="all good\nDONE"), me.node_id, h=t.wh)
    rc, out, _ = _run(capsys, "result", jid, "--json")
    assert rc == 0, out
    doc = json.loads(out)
    assert doc["state"] == "done" and "patch_b64" not in doc["result"]
    assert (t.cwd / f"{jid}.patch").read_bytes() == patch
    md = (t.cwd / f"{jid}.md").read_text()
    assert "status: done" in md and "all good" in md and "worker" in md
    inbox = jobs.inbox(home) / f"{jid}.json"
    assert stat.S_IMODE(os.stat(inbox).st_mode) == 0o600
    assert sender.stored_result(jid)["status"] == "done"
    # The job is no longer offered; text mode works from the stored result (worker down).
    assert sender.fetch_jobs(_peer(t.srv_s), t.w, timeout=5)["jobs"] == []
    t.srv_w.stop()
    rc, out, _ = _run(capsys, "result", jid)
    assert rc == 0 and "done" in out and f"{jid}.patch" in out


def test_cli_result_usage(me, home, capsys):
    assert _run(capsys, "result", "xyz")[0] == 2
    assert _run(capsys, "result", "0" * 16)[0] == 2                   # not in the outbox
    jid = sender.submit(me, to=W_ID, repo="r", ref="main", task="t")
    rc, _, err = _run(capsys, "result", jid)                          # no peers.json
    assert rc == 1 and "not alive" in err


def test_cli_result_wait(me, home, two_nodes, capsys, monkeypatch):
    t = two_nodes
    monkeypatch.setattr(mesh_cli, "RESULT_POLL_S", 0.1)
    jid = sender.submit(me, to=W_ID, repo="r", ref="main", task="t")
    jobs.write_result(_result(jid, status="running"), me.node_id, h=t.wh)
    t0 = time.monotonic()
    rc, out, _ = _run(capsys, "result", jid, "--wait", "--timeout", "0.5", "--json")
    assert rc == 1 and json.loads(out)["state"] == "running"
    assert 0.3 <= time.monotonic() - t0 < 5

    def later():
        time.sleep(0.4)
        jobs.write_result(_result(jid, status="rejected", reason="bad_repo"), me.node_id,
                          h=t.wh)

    th = threading.Thread(target=later)
    th.start()
    rc, out, _ = _run(capsys, "result", jid, "--wait", "--timeout", "10", "--json")
    th.join()
    doc = json.loads(out)
    assert rc == 1 and doc["state"] == "rejected" and doc["result"]["reason"] == "bad_repo"
    assert (t.cwd / f"{jid}.md").exists() and not (t.cwd / f"{jid}.patch").exists()


# ---- CLI: result --apply on a temp git repo -----------------------------------------------

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.invalid"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.invalid"),
                 ("GIT_CONFIG_NOSYSTEM", "1")):
        monkeypatch.setenv(k, v)
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\ntwo\nthree\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "base")
    (r / "a.txt").write_text("one\nTWO\nthree\n")
    _git(r, "commit", "-q", "-am", "change two")
    patch = _git(r, "format-patch", "-1", "--stdout").stdout.encode()
    _git(r, "reset", "-q", "--hard", "HEAD~1")             # temp repo only
    monkeypatch.chdir(r)
    return types.SimpleNamespace(path=r, patch=patch)


def _done_job(me, patch):
    jid = sender.submit(me, to=W_ID, repo="r", ref="main", task="t")
    sender.store_result(_result(jid, patch=patch))
    return jid


def test_cli_apply_ok(me, home, repo, capsys):
    jid = _done_job(me, repo.patch)
    rc, _, err = _run(capsys, "result", jid, "--apply")
    assert rc == 0, err
    assert (repo.path / "a.txt").read_text() == "one\nTWO\nthree\n"
    assert _git(repo.path, "log", "-1", "--format=%s").stdout.strip() == "change two"


def test_cli_apply_refuses_dirty_tree(me, home, repo, capsys):
    jid = _done_job(me, repo.patch)
    (repo.path / "a.txt").write_text("dirty\n")
    rc, _, err = _run(capsys, "result", jid, "--apply")
    assert rc == 2 and "dirty" in err
    assert _git(repo.path, "log", "-1", "--format=%s").stdout.strip() == "base"


def test_cli_apply_conflict_aborts(me, home, repo, capsys):
    jid = _done_job(me, repo.patch)
    (repo.path / "a.txt").write_text("one\nzwei\nthree\n")
    _git(repo.path, "commit", "-q", "-am", "conflict")
    rc, _, err = _run(capsys, "result", jid, "--apply")
    assert rc == 1 and "git am failed" in err
    gitdir = repo.path / ".git"
    assert not (gitdir / "rebase-apply").exists()
    assert _git(repo.path, "status", "--porcelain", "--untracked-files=no").stdout == ""
    assert _git(repo.path, "log", "-1", "--format=%s").stdout.strip() == "conflict"


def test_cli_apply_needs_done(me, home, repo, capsys):
    jid = sender.submit(me, to=W_ID, repo="r", ref="main", task="t")
    sender.store_result(_result(jid, status="failed", patch=repo.patch))
    rc, _, err = _run(capsys, "result", jid, "--apply")
    assert rc == 1 and "not applied" in err
    assert _git(repo.path, "log", "-1", "--format=%s").stdout.strip() == "base"


def test_cli_apply_not_a_repo(me, home, tmp_path, monkeypatch, capsys):
    d = tmp_path / "plain"
    d.mkdir()
    monkeypatch.chdir(d)
    jid = _done_job(me, b"From x\n")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    rc, _, err = _run(capsys, "result", jid, "--apply")
    assert rc == 2 and "not a git repository" in err


# ---- CLI: worker launcher ---------------------------------------------------------------------

def test_cli_worker_not_installed(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "apex_router.mesh.worker", None)
    rc, _, err = _run(capsys, "worker", "--status")
    assert rc == 2 and "mesh worker: not installed" in err


def test_cli_worker_passes_argv(monkeypatch, capsys):
    seen = []
    fake = types.ModuleType("apex_router.mesh.worker")
    fake.main = lambda argv: seen.append(argv) or 7
    monkeypatch.setitem(sys.modules, "apex_router.mesh.worker", fake)
    assert _run(capsys, "worker", "--pause")[0] == 7
    assert _run(capsys, "worker")[0] == 7
    assert _run(capsys, "worker", "--daily-cap", "3", "--poll-s", "1")[0] == 7
    assert seen == [["--pause"], [], ["--daily-cap", "3", "--poll-s", "1"]]
    args = mesh_cli.build_parser().parse_args(["worker", "x", "--status"])
    assert args.rest == ["x", "--status"] and args.fn is mesh_cli._cmd_worker
    assert mesh_cli.main(["worker", "-h"]) == 7 and seen[-1] == ["-h"]


# ---- xval round: results byte budget, id filter, oversized submit ------------------------------

def test_xval_results_budget_stubs_overflow():
    big = {"id": "a" * 16, "status": "done", "reason": "", "steps": [],
           "patch_b64": "x" * 600_000, "report": ""}
    out = jobs.budget([big, {**big, "id": "b" * 16}])
    assert out[0]["patch_b64"] and out[1].get("truncated") is True and out[1]["patch_b64"] == ""
    from apex_router.mesh import collect
    assert len(json.dumps(out)) < collect.MAX_RESPONSE_BYTES


def test_xval_results_for_only_one(tmp_path):
    for i in ("1" * 16, "2" * 16):
        jobs.write_result({"id": i, "status": "done", "reason": "", "steps": [],
                           "patch_b64": "", "report": ""}, "s" * 32, h=tmp_path)
    got = jobs.results_for("s" * 32, h=tmp_path, only="2" * 16)
    assert [r["id"] for r in got] == ["2" * 16]


def test_xval_submit_refuses_envelope_over_cap(me, home):
    task = "\\" * (jobs.MAX_TASK_BYTES - 10)            # JSON doubles each backslash
    with pytest.raises(jobs.JobError, match="job_too_big"):
        sender.submit(me, to=W_ID, repo="apex-router", ref="main", task=task)


def test_xval_id_fetch_is_never_budgeted(tmp_path):
    big = {"id": "e" * 16, "status": "done", "reason": "", "steps": [],
           "patch_b64": "x" * 690_000, "report": "r" * 60_000}
    jobs.write_result({**big, "id": "d" * 16}, "s" * 32, h=tmp_path)   # sorts first, fills budget
    jobs.write_result(big, "s" * 32, h=tmp_path)
    listed = {r["id"]: r for r in jobs.results_for("s" * 32, h=tmp_path)}
    assert listed["e" * 16].get("truncated") is True
    one = jobs.results_for("s" * 32, h=tmp_path, only="e" * 16)
    assert one[0]["patch_b64"] and not one[0].get("truncated")
    from apex_router.mesh import sender as _s
    assert len(json.dumps(one).encode()) < _s.MAX_RESPONSE_BYTES


def test_xval_truncated_result_is_never_stored(home):
    stub = {"id": "f" * 16, "status": "done", "reason": "", "steps": [],
            "patch_b64": "", "report": "", "truncated": True}
    with pytest.raises(jobs.JobError, match="truncated"):
        sender.store_result(stub)


def test_xval_cached_stub_is_ignored(home):
    stub = {"id": "9" * 16, "status": "done", "reason": "", "steps": [],
            "patch_b64": "", "report": "", "truncated": True}
    jobs.write_private(jobs.inbox() / f"{'9' * 16}.json", json.dumps(stub).encode())
    assert sender.stored_result("9" * 16) is None


def test_step_model_uses_aliases():
    # Aliases let each worker's Claude Code pick its own deployment's model (a pinned ID fails on Foundry).
    assert [jobs.step_model(s) for s in jobs.LADDER] == [
        ("sonnet", "medium"), ("sonnet", "high"), ("sonnet", "xhigh"), ("sonnet", "max"), ("opus", "max")]
