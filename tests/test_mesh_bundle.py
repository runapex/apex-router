"""Job repos over the mesh: job ``src: "mesh"``, GET /mesh/v1/bundle, sender.fetch_bundle, the
worker bundle fetch and auto repos, and the CLI choice of ``src``.

Loopback only. Mesh homes and git repos live under tmp_path.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from apex_router.mesh import cli as mesh_cli
from apex_router.mesh import identity, jobs, protocol, sender, worker
from apex_router.mesh.peers import Peer, PeerTable, parse_card
from apex_router.mesh.server import MeshServer

LO = "127.0.0.1"
FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig")}.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
    wh, sh = tmp_path / "mesh-w", tmp_path / "mesh-s"
    monkeypatch.setenv("APEX_MESH_HOME", str(wh))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "fake.jsonl"))
    w_ident = identity.init(wh, name="worker")
    s_ident = identity.init(sh, secret=w_ident.secret, name="sender")
    src = tmp_path / "src-repo"                       # the sender's repo (no remote at all)
    _git("init", "-q", str(src))
    (src / "README").write_text("from the sender\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-q", "-m", "init", cwd=src)
    (sh / jobs.SHARE_FILE).write_text(json.dumps({"repos": {"proj": str(src)}}))
    (wh / "worker.json").write_text(json.dumps({"repos": {}}))
    peer = parse_card({"node_id": s_ident.node_id, "name": "sender", "host": LO, "port": 47778,
                       "version": "t", "roles": [], "load": 0, "started_at": time.time()},
                      now=time.time())
    return types.SimpleNamespace(wh=wh, sh=sh, w=w_ident, s=s_ident, src=src, peer=peer,
                                 tmp=tmp_path)


def _submit(env, **kw):
    kw.setdefault("repo", "proj")
    kw.setdefault("ref", "main")
    kw.setdefault("task", "ALWAYS_DONE")
    kw.setdefault("src", jobs.SRC_MESH)
    return sender.submit(env.s, to=env.w.node_id, h=env.sh, **kw)


def _direct_fetch(env):
    """fetch_bundle without HTTP: the sender's bundle_for + the worker's decode."""
    def fetch(peer, ident, jid):
        try:
            body = jobs.bundle_for(ident.node_id, jid, env.s, h=env.sh)
            data, ref, commit = jobs.decode_bundle(body, jid)
        except jobs.JobError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "data": data, "ref": ref, "commit": commit}
    return fetch


def _worker(env, *, fetch_bundle=None):
    def fetch(peer, ident, timeout):
        return {"ok": True, "jobs": [p.read_text() for p in jobs.outbox(env.sh).glob("*.job")]}
    return worker.Worker(env.w, env.wh, claude_cmd=[sys.executable, str(FAKE)], fetch=fetch,
                         peers=lambda: [env.peer], pressure=lambda: 0,
                         fetch_bundle=fetch_bundle or _direct_fetch(env),
                         offload_log=env.tmp / "offload.jsonl", quiet=True)


def _result(env, jid):
    return jobs.read_json(jobs.results_dir(env.wh) / f"{jid}.json")


# ---- job field ----------------------------------------------------------------------------------

def test_src_field_is_optional_and_checked(env):
    jid, raw = jobs.make_job(env.s, to=env.w.node_id, repo="proj", ref="main", task="x")
    assert "src" not in protocol.decode(raw, env.w, None, max_bytes=1 << 20)["body"]
    jid, raw = jobs.make_job(env.s, to=env.w.node_id, repo="proj", ref="main", task="x",
                             src="mesh")
    assert jobs.open_job(raw, env.w)["src"] == "mesh"
    with pytest.raises(jobs.JobError, match="bad_src"):
        jobs.make_job(env.s, to=env.w.node_id, repo="proj", ref="main", task="x", src="http")
    with pytest.raises(jobs.JobError, match="bad_src"):
        jobs.make_job(env.s, to=env.w.node_id, repo="proj", ref="a" * 40, task="x", src="mesh")


# ---- sender: bundle_for -------------------------------------------------------------------------

def test_bundle_for_round_trip(env):
    jid = _submit(env)
    body = jobs.bundle_for(env.w.node_id, jid, env.s, h=env.sh)
    data, ref, commit = jobs.decode_bundle(body, jid)
    assert ref == "main" and commit == _git("rev-parse", "HEAD", cwd=env.src)
    assert data.startswith(b"# v2 git bundle") or data.startswith(b"# v3 git bundle")


def test_bundle_for_refuses(env):
    jid = _submit(env)
    with pytest.raises(jobs.JobError, match="no_job"):            # not offered to that node
        jobs.bundle_for("c" * 32, jid, env.s, h=env.sh)
    with pytest.raises(jobs.JobError, match="no_job"):            # unknown id
        jobs.bundle_for(env.w.node_id, "0" * 16, env.s, h=env.sh)
    plain = _submit(env, src=None)
    with pytest.raises(jobs.JobError, match="no_job"):            # no src: mesh
        jobs.bundle_for(env.w.node_id, plain, env.s, h=env.sh)
    other = _submit(env, repo="notshared")
    with pytest.raises(jobs.JobError, match="repo_not_shared"):
        jobs.bundle_for(env.w.node_id, other, env.s, h=env.sh)
    missing = _submit(env, ref="nope")
    with pytest.raises(jobs.JobError, match="bad_ref"):
        jobs.bundle_for(env.w.node_id, missing, env.s, h=env.sh)
    jobs.write_private(jobs.inbox(env.sh) / f"{jid}.json", b"{}")   # answered = closed
    with pytest.raises(jobs.JobError, match="no_job"):
        jobs.bundle_for(env.w.node_id, jid, env.s, h=env.sh)


def test_bundle_too_big(env, monkeypatch):
    jid = _submit(env)
    monkeypatch.setattr(jobs, "MAX_BUNDLE_BYTES", 10)
    jobs._BUNDLE_CACHE.clear()
    with pytest.raises(jobs.JobError, match="bundle_too_big"):
        jobs.bundle_for(env.w.node_id, jid, env.s, h=env.sh)


def test_decode_bundle_rejects_tamper(env):
    jid = _submit(env)
    body = dict(jobs.bundle_for(env.w.node_id, jid, env.s, h=env.sh))
    raw = bytearray(base64.b64decode(body["bundle_b64"]))
    raw[-1] ^= 1
    body["bundle_b64"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(jobs.JobError, match="bad_bundle"):
        jobs.decode_bundle(body, jid)
    with pytest.raises(jobs.JobError, match="bad_bundle"):
        jobs.decode_bundle({**body, "id": "1" * 16}, jid)


def test_xval_scratch_repo_is_shared(env):
    assert jobs.XVAL_REPO not in jobs.load_share(env.sh)
    p = jobs.ensure_xval_repo(env.sh)
    assert p is not None and jobs.load_share(env.sh)[jobs.XVAL_REPO] == p
    assert jobs.ensure_xval_repo(env.sh) == p                       # idempotent
    jid = _submit(env, repo="xval")
    assert jobs.bundle_for(env.w.node_id, jid, env.s, h=env.sh)["ref"] == "main"


# ---- HTTP: GET /mesh/v1/bundle ------------------------------------------------------------------

def _serve(env, alive):
    def card(n, name):
        return {"node_id": n, "name": name, "host": LO, "port": 47778, "version": "1",
                "roles": [], "load": 0, "started_at": time.time() - 10}
    table = PeerTable(card(env.s.node_id, "sender"))
    for n in alive:
        table.update(card(n, "w"))
    srv = MeshServer(env.s, table, lambda: card(env.s.node_id, "sender"), host=LO, port=0,
                     home=env.sh)
    srv.start()
    host, port = srv.address
    return srv, Peer(node_id=env.s.node_id, name="sender", host=host, port=port, version="1",
                     roles=(), load=0, started_at=time.time(), last_seen=time.time())


def test_fetch_bundle_over_http(env):
    jid = _submit(env)
    srv, peer = _serve(env, alive=[env.w.node_id])
    try:
        res = sender.fetch_bundle(peer, env.w, jid, timeout=10)
        assert res["ok"], res
        assert res["commit"] == _git("rev-parse", "HEAD", cwd=env.src)
        bad = sender.fetch_bundle(peer, env.w, "0" * 16, timeout=10)
        assert bad["ok"] is False and bad["error"] == "http 404"
    finally:
        srv.stop()


def test_fetch_bundle_unknown_node(env):
    jid = _submit(env)
    srv, peer = _serve(env, alive=[])                 # worker not alive in the sender's table
    try:
        res = sender.fetch_bundle(peer, env.w, jid, timeout=10)
        assert res["ok"] is False and res["error"] == "http 403"
    finally:
        srv.stop()


# ---- worker -------------------------------------------------------------------------------------

def test_worker_runs_src_mesh_job_in_auto_repo(env):
    jid = _submit(env)
    assert _worker(env).poll_once() == 1
    r = _result(env, jid)
    assert r["status"] == "done", r
    auto = Path(str(env.wh) + "-repos") / env.s.node_id / "proj"
    assert (auto / ".git").is_dir()
    assert _git("cat-file", "-t", _git("rev-parse", "HEAD", cwd=env.src), cwd=auto) == "commit"


def test_worker_uses_allowlisted_repo_for_src_mesh(env):
    mine = env.tmp / "own-repo"
    _git("init", "-q", str(mine))
    (env.wh / "worker.json").write_text(json.dumps({"repos": {"proj": str(mine)}}))
    jid = _submit(env)
    assert _worker(env).poll_once() == 1
    assert _result(env, jid)["status"] == "done"
    assert not Path(str(env.wh) + "-repos").exists()


def test_worker_auto_repos_off(env):
    (env.wh / "worker.json").write_text(json.dumps({"repos": {}, "auto_repos": False}))
    jid = _submit(env)
    assert _worker(env).poll_once() == 0
    assert _result(env, jid)["reason"] == "repo_not_allowed"


def test_worker_rejects_bad_bundle(env):
    jid = _submit(env)

    def wrong_commit(peer, ident, j):
        res = _direct_fetch(env)(peer, ident, j)
        return {**res, "commit": "f" * 40}
    assert _worker(env, fetch_bundle=wrong_commit).poll_once() == 0
    assert _result(env, jid)["reason"] == "bundle_failed"

    jid2 = _submit(env)
    assert _worker(env, fetch_bundle=lambda *a: {"ok": False, "error": "http 404"}
                   ).poll_once() == 0
    assert _result(env, jid2)["reason"] == "bundle_failed"


def test_auto_repo_names_are_safe(env):
    assert worker.auto_repo(env.wh, "x", "proj") == (None, "repo_not_allowed")
    assert worker.auto_repo(env.wh, env.s.node_id, "..")[0] is None
    assert worker.auto_repo(env.wh, env.s.node_id, "a/b")[0] is None


# ---- CLI: when submit sets src ------------------------------------------------------------------

def test_job_src_needs_share_cap_and_branch(env):
    rows = [{"node_id": env.w.node_id, "build": {"update": "ok", "caps": ["bundle"]}}]
    a = types.SimpleNamespace(repo="proj", ref="main", no_share=False)
    assert mesh_cli._job_src(a, env.w.node_id, rows, env.sh, jobs) == "mesh"
    assert mesh_cli._job_src(types.SimpleNamespace(repo="proj", ref="main", no_share=True),
                             env.w.node_id, rows, env.sh, jobs) is None
    assert mesh_cli._job_src(types.SimpleNamespace(repo="proj", ref="a" * 40, no_share=False),
                             env.w.node_id, rows, env.sh, jobs) is None
    assert mesh_cli._job_src(types.SimpleNamespace(repo="other", ref="main", no_share=False),
                             env.w.node_id, rows, env.sh, jobs) is None
    old = [{"node_id": env.w.node_id}]                       # an old worker: no build key
    assert mesh_cli._job_src(a, env.w.node_id, old, env.sh, jobs) is None
