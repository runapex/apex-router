"""Mesh self-update (src/apex_router/mesh/selfupdate.py): preflight, fast-forward, reinstall and
smoke gates, rollback, status file, AutoUpdater triggers, restart detection, card build key.

Every repo is a temp git repo with a local bare "upstream". No test runs uv or re-execs.
"""
from __future__ import annotations

import json
import subprocess
import time
import types

import pytest

from apex_router.mesh import selfupdate as su
from apex_router.mesh.peers import parse_build, parse_card


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("APEX_MESH_NO_UPDATE", raising=False)
    (tmp_path / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
    up = tmp_path / "upstream.git"
    _git("init", "-q", "--bare", str(up))
    dev = tmp_path / "dev"                       # where "upstream" commits are made
    _git("clone", "-q", str(up), str(dev))
    (dev / "pyproject.toml").write_text("[project]\nname='x'\n")
    (dev / "a.py").write_text("A = 1\n")
    _git("add", "-A", cwd=dev)
    _git("commit", "-q", "-m", "v1", cwd=dev)
    _git("push", "-q", "origin", "HEAD:main", cwd=dev)
    node = tmp_path / "node"                     # the node's own checkout
    _git("clone", "-q", str(up), str(node))
    home = tmp_path / "mesh"
    home.mkdir()
    (home / su.CONFIG_FILE).write_text(json.dumps({"upstreams": [str(up)]}))
    return types.SimpleNamespace(up=up, dev=dev, node=node, home=home, tmp=tmp_path)


def _advance(env, name="a.py", text="A = 2\n", msg="v2"):
    (env.dev / name).write_text(text)
    _git("add", "-A", cwd=env.dev)
    _git("commit", "-q", "-m", msg, cwd=env.dev)
    _git("push", "-q", "origin", "HEAD:main", cwd=env.dev)
    return _git("rev-parse", "HEAD", cwd=env.dev)


def _update(env, **kw):
    calls = {"reinstall": 0, "smoke": 0}

    def reinstall(_repo):
        calls["reinstall"] += 1
        return True

    def smoke():
        calls["smoke"] += 1
        return kw.get("smoke_ok", True)
    res = su.update(env.home, repo=env.node, reinstall_fn=reinstall, smoke_fn=smoke,
                    dry_run=kw.get("dry_run", False))
    return res, calls


# ---- urls and config ----------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://github.com/runapex/apex-router.git",
                                 "https://github.com/runapex/apex-router",
                                 "git@github.com:runapex/apex-router.git",
                                 "ssh://git@github.com:22/runapex/apex-router",
                                 "https://GitHub.com/RunApex/apex-router/"])
def test_norm_url_official_forms(url):
    assert su.norm_url(url) == "github.com/runapex/apex-router"


def test_norm_url_rejects_lookalikes():
    for url in ("https://github.com/evil/apex-router", "https://github.com.evil.io/runapex/apex-router",
                "https://gitlab.com/runapex/apex-router"):
        assert su.norm_url(url) not in su.DEFAULT_UPSTREAMS


def test_config_defaults_and_opt_out(env, monkeypatch):
    (env.home / su.CONFIG_FILE).unlink()
    assert su.load_config(env.home) == {"auto_update": True,
                                        "upstreams": list(su.DEFAULT_UPSTREAMS)}
    (env.home / su.CONFIG_FILE).write_text(json.dumps({"auto_update": False}))
    assert su.load_config(env.home)["auto_update"] is False
    (env.home / su.CONFIG_FILE).write_text("not json")
    assert su.load_config(env.home)["auto_update"] is True
    monkeypatch.setenv("APEX_MESH_NO_UPDATE", "1")
    assert su.load_config(env.home)["auto_update"] is False


# ---- update ---------------------------------------------------------------------------------------

def test_up_to_date_is_ok(env):
    res, calls = _update(env)
    assert res["status"] == "ok" and res["old"] == res["new"]
    assert calls == {"reinstall": 0, "smoke": 0}


def test_fast_forward_without_dep_change(env):
    old = _git("rev-parse", "HEAD", cwd=env.node)
    new = _advance(env)
    res, calls = _update(env)
    assert res["status"] == "updated" and res["old"] == old and res["new"] == new
    assert _git("rev-parse", "HEAD", cwd=env.node) == new
    assert calls == {"reinstall": 0, "smoke": 1} and res["reinstalled"] is False
    assert su.read_status(env.home)["status"] == "updated"


def test_dep_change_reinstalls(env):
    _advance(env, name="pyproject.toml", text="[project]\nname='x'\nversion='2'\n")
    res, calls = _update(env)
    assert res["status"] == "updated" and res["reinstalled"] is True
    assert calls["reinstall"] == 1


def test_smoke_failure_rolls_back(env):
    old = _git("rev-parse", "HEAD", cwd=env.node)
    _advance(env)
    res, _ = _update(env, smoke_ok=False)
    assert res["status"] == "error" and res["error"] == "smoke"
    assert _git("rev-parse", "HEAD", cwd=env.node) == old


def test_reinstall_failure_rolls_back_and_reinstalls_old(env):
    old = _git("rev-parse", "HEAD", cwd=env.node)
    _advance(env, name="uv.lock", text="lock\n")
    calls = []
    res = su.update(env.home, repo=env.node, smoke_fn=lambda: True,
                    reinstall_fn=lambda p: calls.append(_git("rev-parse", "HEAD", cwd=p)) or False)
    assert res["status"] == "error" and res["error"] == "reinstall"
    assert _git("rev-parse", "HEAD", cwd=env.node) == old
    assert calls[-1] == old                          # the old deps are put back


def test_dirty_tree_is_never_touched(env):
    _advance(env)
    (env.node / "a.py").write_text("local edit\n")
    res, _ = _update(env)
    assert res["status"] == "dirty"
    assert (env.node / "a.py").read_text() == "local edit\n"


def test_untracked_files_do_not_block(env):
    _advance(env)
    (env.node / "scratch.txt").write_text("x\n")
    assert _update(env)[0]["status"] == "updated"


def test_other_branch_is_not_main(env):
    _git("checkout", "-q", "-b", "feature", cwd=env.node)
    assert _update(env)[0]["status"] == "not_main"


def test_diverged_history_is_left_alone(env):
    (env.node / "b.py").write_text("B = 1\n")
    _git("add", "-A", cwd=env.node)
    _git("commit", "-q", "-m", "local", cwd=env.node)
    mine = _git("rev-parse", "HEAD", cwd=env.node)
    _advance(env)
    res, _ = _update(env)
    assert res["status"] == "diverged"
    assert _git("rev-parse", "HEAD", cwd=env.node) == mine


def test_unknown_origin_is_refused(env):
    (env.home / su.CONFIG_FILE).unlink()             # default upstreams: the official repo only
    _advance(env)
    assert _update(env)[0]["status"] == "bad_origin"


def test_dry_run_reports_and_changes_nothing(env):
    old = _git("rev-parse", "HEAD", cwd=env.node)
    new = _advance(env)
    res, calls = _update(env, dry_run=True)
    assert res["status"] == "ok" and res["behind"] is True and res["upstream"] == new
    assert _git("rev-parse", "HEAD", cwd=env.node) == old and calls["smoke"] == 0
    assert not (env.home / su.STATUS_FILE).exists()


def test_fetch_failure(env):
    _git("remote", "set-url", "origin", str(env.tmp / "gone.git"), cwd=env.node)
    (env.home / su.CONFIG_FILE).write_text(json.dumps({"upstreams": [str(env.tmp / "gone.git")]}))
    assert _update(env)[0]["status"] == "fetch_failed"


def test_no_checkout(env):
    res = su.update(env.home, repo=None, run=lambda *_a, **_k: None)
    assert res["status"] == "no_checkout"


# ---- restart detection ----------------------------------------------------------------------------

def test_restart_needed_after_head_moves(env, monkeypatch):
    start = su.head(env.node)
    assert su.restart_needed(start, env.node) is False
    _advance(env)
    _update(env)
    assert su.restart_needed(start, env.node) is True
    monkeypatch.setenv("APEX_MESH_NO_UPDATE", "1")
    assert su.restart_needed(start, env.node) is False
    assert su.restart_needed(None, env.node) is False


# ---- AutoUpdater ----------------------------------------------------------------------------------

def _peer(commit):
    return types.SimpleNamespace(build={"commit": commit, "update": "ok", "caps": []})


def test_autoupdater_triggers_on_unknown_peer_commit(env):
    clock = [time.time()]
    calls = []
    au = su.AutoUpdater(env.home, repo=env.node, now=lambda: clock[0],
                        update_fn=lambda: calls.append(1) or {"status": "ok", "at": clock[0]})
    au.last = clock[0]                                 # a check just ran
    assert au.tick([_peer(au.start_commit)]) is False  # same build
    clock[0] += su.MIN_GAP_S + 1
    assert au.tick([_peer(au.start_commit)]) is False  # not due, same build
    assert au.tick([_peer("e" * 40)]) is True          # a commit we do not have
    au._thread.join(5)
    assert calls == [1]
    assert au.tick([_peer("e" * 40)]) is False         # rate limit: gap since the last check


def test_autoupdater_skips_known_older_commit(env):
    old = su.head(env.node)
    _advance(env)
    _update(env)                                       # node now ahead of `old`
    clock = [time.time()]
    au = su.AutoUpdater(env.home, repo=env.node, now=lambda: clock[0],
                        update_fn=lambda: {"status": "ok"})
    au.last = clock[0] - su.MIN_GAP_S - 1
    assert au.tick([_peer(old)]) is False              # the peer is behind us


def test_autoupdater_periodic_and_off(env):
    clock = [time.time()]
    au = su.AutoUpdater(env.home, repo=env.node, now=lambda: clock[0],
                        update_fn=lambda: {"status": "ok"})
    au.last = clock[0] - su.CHECK_EVERY_S - 1
    assert au.tick([]) is True
    au._thread.join(5)
    (env.home / su.CONFIG_FILE).write_text(json.dumps({"auto_update": False}))
    off = su.AutoUpdater(env.home, repo=env.node, update_fn=lambda: {"status": "ok"})
    assert off.enabled is False and off.tick([]) is False
    assert off.card_build()["update"] == "off"


def test_card_build_parses_on_peers(env):
    au = su.AutoUpdater(env.home, repo=env.node, update_fn=lambda: {"status": "ok"})
    b = au.card_build()
    assert b["commit"] == su.head(env.node) and set(b["caps"]) == set(su.CAPS)
    card = {"node_id": "a" * 32, "name": "n", "host": "127.0.0.1", "port": 1, "version": "1",
            "roles": [], "load": 0, "started_at": time.time(), "build": b}
    p = parse_card(card, now=time.time())
    assert p is not None and p.build["commit"] == b["commit"] and p.has_cap("bundle")
    assert p.to_dict()["build"]["update"] == b["update"]


def test_bad_build_key_is_dropped_not_the_card():
    card = {"node_id": "a" * 32, "name": "n", "host": "127.0.0.1", "port": 1, "version": "1",
            "roles": [], "load": 0, "started_at": time.time()}
    for bad in ("x", {"update": "ok", "commit": "zz"}, {"update": "OK!"},
                {"update": "ok", "caps": ["a"] * 20}, {"update": "ok", "at": float("inf")}):
        p = parse_card({**card, "build": bad}, now=time.time())
        assert p is not None and p.build is None
    assert parse_build({"update": "ok"}) == {"update": "ok", "caps": []}


# ---- the real code checkout ----------------------------------------------------------------------

def test_code_checkout_is_this_repo():
    repo = su.code_checkout()
    assert repo is None or (repo / "src" / "apex_router" / "mesh" / "selfupdate.py").is_file()


def test_tool_receipt_reads_extras(tmp_path, monkeypatch):
    d = tmp_path / "tools" / "apex-router"
    d.mkdir(parents=True)
    (d / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "apex-router", extras = ["proxy", "bad extra"], '
        'editable = "/x/apex-router" }]\n')
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "tools"))
    assert su.tool_receipt() == {"editable": "/x/apex-router", "extras": ["proxy"]}
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "none"))
    assert su.tool_receipt() is None
