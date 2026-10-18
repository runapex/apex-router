"""``apex-router mesh ...`` subcommands (contract: docs/DESIGN-lan-mesh.md, both "CLI" sections).

``up`` is the only command that opens a listening socket. It writes ``peers.json`` (atomic, 0600)
each beacon cycle; ``peers``, ``leader``, ``pick``, ``models``, ``ask``, ``collect`` and ``advise``
read that file and drop entries older than PEER_TTL_S. ``ask`` sends one prompt to the ollama of
a node that serves the model (``up --serve-model``). The secret is printed only by
``init --export``.

Mesh worker (docs/superpowers/specs/2026-10-17-mesh-worker-design.md): ``submit`` writes a signed
job to the outbox, ``result`` fetches it from the worker, ``jobs`` lists the outbox, and
``worker`` hands its arguments to ``worker.main`` (imported lazily).

Notes (docs/DESIGN-lan-mesh.md, "Notes"): ``note`` sends one short signed text to a peer;
``inbox`` prints the notes this node received. A note is data: nothing runs because of it.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import collect as collect_mod
from . import datasets, identity, infer, notes, protocol
from .identity import Identity, MeshNotInitialized
from .peers import LOAD_MAX, MODELS_MAX, PeerTable, parse_card, valid_model
from .protocol import (
    BEACON_INTERVAL_S,
    DEFAULT_HTTP_PORT,
    MAX_DATAGRAM,
    MCAST_PORT,
    PEER_TTL_S,
)

PEERS_FILE = "peers.json"
PID_FILE = "mesh.pid"
LOCK_FILE = "mesh.lock"            # `up` holds flock(LOCK_EX) on it while it runs


# ---- helpers --------------------------------------------------------------------------------

def _version() -> str:
    try:
        from importlib.metadata import version
        return version("apex-router")[:64] or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _err(msg: str) -> None:
    print(f"apex-router mesh: {msg}", file=sys.stderr)


def _dump(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _write_json(path: Path, obj: Any) -> None:
    identity._ensure_dir(path.parent)
    identity._atomic_write(path, (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _take_lock(home: Path) -> int | None:
    """Open mesh.lock and take flock(LOCK_EX | LOCK_NB). Returns the fd, or None if held."""
    identity._ensure_dir(home)
    fd = os.open(home / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lock_held(home: Path) -> bool:
    """True if a live `mesh up` holds mesh.lock. A reused pid cannot fake this."""
    try:
        fd = os.open(home / LOCK_FILE, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)                       # also drops our shared lock
    return False


def _submodule(name: str):
    """Import a sibling module by name (lets tests inject fakes via sys.modules)."""
    return importlib.import_module(f"{__package__}.{name}")


def _self_card(ident: Identity, host: str, port: int, roles: list[str], started_at: float,
               load: int = 0, models: list[str] | None = None,
               build: dict | None = None) -> dict:
    card = {"node_id": ident.node_id, "name": ident.name, "host": host, "port": port,
            "version": _version(), "roles": list(roles), "load": int(load),
            "started_at": started_at}
    if models:
        card["models"] = list(models)     # optional key: old nodes ignore it
    if build:
        card["build"] = dict(build)       # optional key: commit, update status, caps
    return card


def _local_load() -> int:
    """Running agent count for the card ``load``. Never raises."""
    try:
        from .. import agents
        n = agents.worker().get("queue_running", 0)
        return n if isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 1_000_000 else 0
    except Exception:  # noqa: BLE001
        return 0


# ---- peers.json reader ----------------------------------------------------------------------

def read_peers(ident: Identity, home: Path, *, now: float | None = None) -> dict | None:
    """Fresh view of ``peers.json``: entries older than PEER_TTL_S are dropped.

    Returns ``{"self", "leader", "peers": [...], "n_alive", "ts"}`` or None when the file is
    missing, unreadable, from another mesh/node, or the self entry is stale (``up`` not running).
    """
    t = time.time() if now is None else float(now)
    doc = _read_json(home / PEERS_FILE)
    if not isinstance(doc, dict) or doc.get("mesh_id") != ident.mesh_id:
        return None
    if doc.get("self") != ident.node_id or not isinstance(doc.get("peers"), list):
        return None
    rows = []
    for row in doc["peers"]:
        if not isinstance(row, dict):
            continue
        seen = row.get("last_seen")
        if isinstance(seen, bool) or not isinstance(seen, (int, float)):
            continue
        if t - float(seen) > PEER_TTL_S or parse_card(row, now=float(seen)) is None:
            continue
        rows.append(row)
    rows.sort(key=lambda r: r["node_id"])
    me = next((r for r in rows if r["node_id"] == ident.node_id), None)
    if me is None:
        return None
    lead = min(rows, key=lambda r: (float(r["started_at"]), r["node_id"]))
    out_rows = [{**r, "self": r["node_id"] == ident.node_id,
                 "age_s": round(max(0.0, t - float(r["last_seen"])), 3)} for r in rows]
    return {"self": ident.node_id, "leader": lead["node_id"], "n_alive": len(out_rows),
            "ts": t, "peers": out_rows}


def _table_from(view: dict | None, ident: Identity) -> PeerTable:
    """PeerTable with the fresh peers of ``view``; a self-only table if ``view`` is None."""
    if view is None:
        card = _self_card(ident, "127.0.0.1", DEFAULT_HTTP_PORT, [], time.time())
        return PeerTable(card)
    rows = view["peers"]
    me = next(r for r in rows if r["self"])
    table = PeerTable(me, ttl_s=PEER_TTL_S)
    for r in rows:
        if not r["self"]:
            table.update(r, now=float(r["last_seen"]))
    return table


def _pick(view: dict, role: str) -> dict | None:
    cands = [r for r in view["peers"] if role in (r.get("roles") or [])]
    return min(cands, key=lambda r: (r["load"], r["node_id"])) if cands else None


def _peer_line(r: dict) -> str:
    flags = ("*" if r.get("self") else " ")
    roles = ",".join(r.get("roles") or []) or "-"
    b = r.get("build") if isinstance(r.get("build"), dict) else {}
    build = f"  build={str(b.get('commit') or '?')[:7]}/{b.get('update', '?')}" if b else ""
    return (f"{flags} {r['node_id'][:12]}  {r['name'][:20]:<20}  {r['host']}:{r['port']:<5}  "
            f"load={r['load']:<4} age={r.get('age_s', 0):.0f}s  roles={roles}  v{r['version']}"
            f"{build}")


# ---- commands -------------------------------------------------------------------------------

def _cmd_init(args) -> int:
    home = identity.mesh_home()
    secret = None
    if args.import_token is not None:
        try:
            secret = identity.parse_secret(args.import_token)
        except ValueError as e:
            _err(f"bad token: {e}")
            return 2
    try:
        if args.export and secret is None and not args.force:
            try:
                ident = identity.load(home)
            except MeshNotInitialized:
                ident = identity.init(home, name=args.name)
        else:
            ident = identity.init(home, secret=secret, name=args.name, force=args.force)
    except FileExistsError as e:
        _err(str(e))
        return 1
    if args.export:
        # The only place that prints the secret: on purpose, for out-of-band copy.
        print(identity.export_secret(ident))
        return 0
    print(f"node_id  {ident.node_id}")
    print(f"name     {ident.name}")
    print(f"mesh_id  {ident.mesh_id}")
    print(f"home     {home}")
    return 0


def _cmd_status(args) -> int:
    home = identity.mesh_home()
    try:
        ident = identity.load(home)
    except MeshNotInitialized as e:
        out = {"initialized": False, "home": str(home), "running": False, "error": str(e)}
        if args.json:
            _dump(out)
        else:
            _err(str(e))
        return 1
    pid_doc = _read_json(home / PID_FILE)
    pid = pid_doc.get("pid") if isinstance(pid_doc, dict) else None
    running = _lock_held(home)
    view = read_peers(ident, home)
    out = {"initialized": True, "home": str(home), "node_id": ident.node_id,
           "name": ident.name, "mesh_id": ident.mesh_id, "running": running,
           "pid": pid if running else None,
           "bind": pid_doc.get("host") if running and isinstance(pid_doc, dict) else None,
           "port": pid_doc.get("port") if running and isinstance(pid_doc, dict) else None,
           "n_alive": view["n_alive"] if view else 0,
           "leader": view["leader"] if view else None}
    if args.json:
        _dump(out)
        return 0
    print(f"node_id  {ident.node_id}")
    print(f"name     {ident.name}")
    print(f"mesh_id  {ident.mesh_id}")
    print(f"home     {home}")
    if running:
        print(f"up       running (pid {pid}, {out['bind']}:{out['port']})")
    else:
        print("up       not running")
    print(f"alive    {out['n_alive']}")
    return 0


def _load_ident() -> tuple[Identity, Path] | None:
    home = identity.mesh_home()
    try:
        return identity.load(home), home
    except MeshNotInitialized as e:
        _err(str(e))
        return None


def _cmd_peers(args) -> int:
    got = _load_ident()
    if got is None:
        return 2
    ident, home = got
    view = read_peers(ident, home)
    if args.json:
        _dump(view or {"self": ident.node_id, "leader": None, "n_alive": 0, "peers": [],
                       "ts": time.time()})
        return 0
    if view is None:
        print("no fresh peers.json: run `apex-router mesh up`")
        return 0
    for r in view["peers"]:
        print(_peer_line(r))
    print(f"{view['n_alive']} alive, leader {view['leader'][:12]}")
    return 0


def _cmd_leader(args) -> int:
    got = _load_ident()
    if got is None:
        return 2
    ident, home = got
    view = read_peers(ident, home)
    lead = next((r for r in view["peers"] if r["node_id"] == view["leader"]), None) if view else None
    if args.json:
        _dump({"leader": lead})
    elif lead is None:
        print("no fresh peers.json: run `apex-router mesh up`")
    else:
        print(_peer_line(lead))
    return 0 if lead is not None else 1


def _cmd_pick(args) -> int:
    got = _load_ident()
    if got is None:
        return 2
    ident, home = got
    view = read_peers(ident, home)
    peer = _pick(view, args.role) if view else None
    if args.json:
        _dump({"role": args.role, "peer": peer})
    elif peer is None:
        print(f"no alive node with role {args.role!r}")
    else:
        print(_peer_line(peer))
    return 0 if peer is not None else 1


def _run_collect(names: list[str], timeout: float) -> tuple[dict, bool] | None:
    got = _load_ident()
    if got is None:
        return None
    ident, home = got
    view = read_peers(ident, home)
    table = _table_from(view, ident)
    return collect_mod.collect(table, ident, names, timeout=timeout), view is not None


def _cmd_collect(args) -> int:
    names = list(args.names) or list(datasets.ALLOWLIST)
    bad = [n for n in names if n not in datasets.ALLOWLIST]
    if bad:
        _err(f"unknown dataset(s): {', '.join(bad)}; known: {', '.join(datasets.ALLOWLIST)}")
        return 2
    got = _run_collect(names, args.timeout)
    if got is None:
        return 2
    res, have_view = got
    if args.json:
        _dump(res)
        return 0 if res["n_ok"] else 1
    if not have_view:
        print("note: no fresh peers.json; local node only")
    for r in res["nodes"]:
        state = "ok" if r["ok"] else f"FAIL {r.get('error', '')}"
        print(f"  {r['node_id'][:12]}  {str(r['name'])[:20]:<20}  {state}")
    pooled = res["pooled"]
    print(f"nodes {res['n_ok']}/{res['n_nodes']} ok")
    if "pressure" in names:
        print(f"pressure  worst {pooled['pressure']['level']}")
    if "errors15m" in names:
        print(f"errors15m n={pooled['errors15m']['n']} {pooled['errors15m']['by_cause']}")
    if "route_rates" in names:
        for tt, c in pooled["route_rates"].items():
            print(f"route_rates  {tt:<24} n={c['n']:<6} esc={c['escalated']:<6} "
                  f"rate={c['rate']:.3f}")
    return 0 if res["n_ok"] else 1


def _cmd_advise(args) -> int:
    got = _run_collect(["route_rates", "pressure"], args.timeout)
    if got is None:
        return 2
    res, have_view = got
    advice = res["pooled"]["route_advice_pooled"]
    per_node = res["pooled"].get("per_node") or {}
    pnodes = {n.get("node_id"): n for n in (res["pooled"].get("pressure") or {}).get("nodes") or []
              if isinstance(n, dict)}
    if args.json:
        _dump({"n_nodes": res["n_nodes"], "n_ok": res["n_ok"],
               "route_rates": res["pooled"]["route_rates"], "route_advice_pooled": advice,
               "per_node": per_node, "pressure_nodes": list(pnodes.values())})
        return 0 if res["n_ok"] else 1
    if not have_view:
        print("note: no fresh peers.json; local node only")
    print(f"pooled over {res['n_ok']}/{res['n_nodes']} nodes")
    if not advice:
        print("no route data")
    rates = res["pooled"].get("route_rates") or {}
    for tt, rec in sorted(advice.items()):
        if not isinstance(rec, dict):
            continue
        print(f"  {tt:<24} {rec.get('verdict')!s:<14} n={rec.get('n', 0):<6} "
              f"rate={float(rec.get('rate') or 0):.3f} "
              f"ci=[{float(rec.get('ci_low') or 0):.3f},{float(rec.get('ci_high') or 0):.3f}]")
        # Per-start-tier split (informational; the verdict above stays pooled).
        by_tier = (rates.get(tt) or {}).get("by_tier") if isinstance(rates.get(tt), dict) else None
        for tier, b in sorted((by_tier or {}).items()):
            if isinstance(b, dict):
                print(f"    start {tier:<16} n={b.get('n', 0):<6} esc={b.get('escalated', 0):<6} "
                      f"rate={float(b.get('rate') or 0):.3f}")
    if per_node:
        print("per node (n, escalation rate)")
    for nid in sorted(per_node, key=str):
        for tt, c in sorted(per_node[nid]["route_rates"].items()):
            print(f"  {str(nid)[:12]}  {tt:<24} n={c['n']:<6} rate={c['rate']:.3f}")
    for nid, p in sorted(pnodes.items(), key=lambda kv: str(kv[0])):
        if "n" not in p:
            continue
        ttft = f"{p['p50_ttft_ms']:.0f}" if "p50_ttft_ms" in p else "-"
        rej = f"{p['upstream_rejected_rate']:.3f}" if "upstream_rejected_rate" in p else "-"
        print(f"  {str(nid)[:12]}  pressure n={p['n']:<6} p50_ttft_ms={ttft} rejected_rate={rej}")
    return 0 if res["n_ok"] else 1


# ---- model calls ----------------------------------------------------------------------------

def _model_rows(view: dict | None) -> list[dict]:
    rows = []
    for r in (view or {}).get("peers", []):
        for m in r.get("models") or []:
            rows.append({"node_id": r["node_id"], "name": r["name"], "model": m,
                         "load": r["load"], "self": bool(r.get("self"))})
    rows.sort(key=lambda x: (x["name"], x["model"], x["node_id"]))
    return rows


def _cmd_models(args) -> int:
    got = _load_ident()
    if got is None:
        return 2
    ident, home = got
    view = read_peers(ident, home)
    rows = _model_rows(view)
    if args.json:
        _dump({"models": rows})
        return 0
    if view is None:
        print("no fresh peers.json: run `apex-router mesh up`")
        return 0
    if not rows:
        print("no alive node serves a model (`mesh up --serve-model NAME`)")
    for r in rows:
        print(f"{'*' if r['self'] else ' '} {r['node_id'][:12]}  {r['name'][:20]:<20}  "
              f"load={r['load']:<4} {r['model']}")
    return 0


def _pick_model(view: dict, model: str, node: str | None) -> dict | None:
    cands = [r for r in view["peers"] if model in (r.get("models") or [])]
    if node:
        cands = [r for r in cands if node in (r["name"], r["node_id"])]
    return min(cands, key=lambda r: (r["load"], r["node_id"])) if cands else None


def _cmd_ask(args) -> int:
    if not valid_model(args.model):
        _err(f"bad --model {args.model!r}")
        return 2
    if not 1 <= args.max_tokens <= infer.MAX_TOKENS:
        _err(f"bad --max-tokens {args.max_tokens}: use 1..{infer.MAX_TOKENS}")
        return 2
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    if not prompt or not prompt.strip():
        _err("empty prompt")
        return 2
    got = _load_ident()
    if got is None:
        return 2
    ident, home = got
    view = read_peers(ident, home)
    if view is None:
        _err("no fresh peers.json: run `apex-router mesh up`")
        return 1
    row = _pick_model(view, args.model, args.node)
    if row is None:
        where = f" named {args.node!r}" if args.node else ""
        _err(f"no alive node{where} serves model {args.model!r}")
        if args.json:
            _dump({"ok": False, "model": args.model, "error": "none"})
        return 1
    peer = parse_card(row, now=float(row["last_seen"]))
    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": prompt})
    opener = collect_mod._pinned_opener(collect_mod._local_ip())   # stay on the LAN interface
    res = infer.ask(peer, ident, args.model, messages, args.max_tokens,
                    timeout=args.timeout, opener=opener)
    if args.json:
        _dump(res)
    elif res["ok"]:
        print(res["content"])
        print(f"[{res['name']} {str(res['node_id'])[:12]} {res['model']} "
              f"{res.get('latency_s')}s finish={res.get('finish_reason')}]", file=sys.stderr)
    else:
        _err(f"ask {res['name']} failed: {res.get('error')}")
    return 0 if res["ok"] else 1


# ---- mesh worker: sender side ---------------------------------------------------------------

RESULT_POLL_S = 5.0                 # `mesh result --wait` poll interval
RESULT_HTTP_TIMEOUT_S = 10.0        # one GET /results


def _peer_rows(ident: Identity, home: Path) -> list[dict]:
    """All rows of peers.json (stale ones too) when the file belongs to this node and mesh."""
    doc = _read_json(home / PEERS_FILE)
    if not isinstance(doc, dict) or doc.get("mesh_id") != ident.mesh_id \
            or doc.get("self") != ident.node_id or not isinstance(doc.get("peers"), list):
        return []
    return [r for r in doc["peers"] if isinstance(r, dict) and isinstance(r.get("node_id"), str)]


def _job_src(args, to: str, rows: list[dict], home: Path, jobs) -> str | None:
    """``"mesh"`` when the worker can take the repo as a bundle from this node, else None.

    Needs: the repo name is shared here (share_repos.json, or the built-in xval scratch repo),
    the ref is a branch or tag, the target card shows the ``bundle`` cap, and no --no-share."""
    if getattr(args, "no_share", False) or jobs.valid_sha(args.ref):
        return None
    if args.repo not in jobs.load_share(home):
        return None
    row = next((r for r in rows if r.get("node_id") == to), None)
    caps = ((row or {}).get("build") or {}).get("caps") if isinstance(row, dict) else None
    return jobs.SRC_MESH if isinstance(caps, list) and "bundle" in caps else None


def _cmd_submit(args) -> int:
    sender, jobs = _submodule("sender"), _submodule("jobs")
    task = sys.stdin.read() if args.task == "-" else args.task
    got = _load_ident()
    if got is None:
        return 2
    ident, home = got
    try:
        rows = _peer_rows(ident, home)
        to = sender.resolve_to(args.to, rows)
        jid = sender.submit(ident, to=to, repo=args.repo, ref=args.ref, task=task or "",
                            check=args.check or "", max_effort=args.max_effort,
                            expires_s=int(args.expires_min) * 60,
                            src=_job_src(args, to, rows, home, jobs))
    except jobs.JobError as e:
        _err(f"submit: {e}")
        return 2
    except OSError as e:
        _err(f"submit: cannot write the outbox: {e}")
        return 1
    print(jid)
    return 0


def _result_out(job: dict | None, body: dict | None, jid: str, state: str,
                files: dict, error: str | None, as_json: bool) -> None:
    if as_json:
        doc: dict = {"id": jid, "state": state, "files": files}
        if body is not None:
            doc["result"] = {k: v for k, v in body.items() if k != "patch_b64"}
        if job is not None:
            doc["to"] = job["to"]
        if error:
            doc["error"] = error
        _dump(doc)
        return
    print(f"{jid}  {state}" + (f"  {body.get('reason')}" if body and body.get("reason") else ""))
    if body and body.get("status") == "running":
        run = [s for s in body.get("steps") or [] if isinstance(s, dict)]
        if run:
            print(f"  step {run[-1].get('effort')}")
    for k in ("patch", "report"):
        if files.get(k):
            print(f"  {k:<6} {files[k]}")
    if error:
        _err(f"result {jid}: {error}")


def _cmd_result(args) -> int:
    sender, jobs = _submodule("sender"), _submodule("jobs")
    jid = args.id
    if not jobs.valid_id(jid):
        _err(f"result: bad id {jid!r} (16 hex)")
        return 2
    if args.timeout is not None and args.timeout < 0:
        _err("result: --timeout must be >= 0")
        return 2
    got = _load_ident()
    if got is None:
        return 2
    ident, home = got
    job = sender.read_job(jid, ident)
    if job is None:
        _err(f"result: no job {jid} in the outbox")
        return 2
    body = sender.stored_result(jid)
    error = None
    end = time.monotonic() + (args.timeout if args.timeout is not None else 1800.0)
    peer_name = None
    while body is None or body.get("status") not in sender.TERMINAL:
        view = read_peers(ident, home)
        row = next((r for r in (view or {}).get("peers", []) if r["node_id"] == job["to"]), None)
        if row is None:
            error = "worker not alive in peers.json"
        else:
            peer_name = row.get("name")
            peer = parse_card(row, now=float(row["last_seen"]))
            res = sender.fetch_results(peer, ident, timeout=RESULT_HTTP_TIMEOUT_S, job_id=jid)
            if not res["ok"]:
                error = res.get("error", "fetch failed")
            else:
                error = None
                mine = next((r for r in res["results"] if r["id"] == jid), None)
                if mine is not None and mine.get("truncated"):
                    error = "result truncated by the worker; try again"
                    mine = None
                if mine is not None:
                    body = mine
                    try:
                        sender.store_result(body)
                    except (OSError, jobs.JobError) as e:
                        error = f"cannot store the result: {e}"
        if body is not None and body.get("status") in sender.TERMINAL:
            break
        if not args.wait or time.monotonic() + RESULT_POLL_S > end:
            break
        time.sleep(RESULT_POLL_S)
    if peer_name is None:
        peer_name = next((r.get("name") for r in _peer_rows(ident, home)
                          if r["node_id"] == job["to"]), None)
    state = sender.job_state(job, body)
    files: dict = {}
    if body is not None and body.get("status") in sender.TERMINAL:
        try:
            ppath, mpath = sender.write_outputs(job, body, Path(getattr(args, "out_dir", None)
                                                                or Path.cwd()), peer_name)
        except (OSError, jobs.JobError) as e:
            _result_out(job, body, jid, state, files, f"cannot write outputs: {e}", args.json)
            return 1
        files = {"patch": str(ppath) if ppath else None, "report": str(mpath)}
    if args.apply:
        if state != "done":
            _result_out(job, body, jid, state, files, error or f"not applied: state {state}",
                        args.json)
            return 1
        if not files.get("patch"):
            _result_out(job, body, jid, state, files, "not applied: empty patch", args.json)
            return 1
        rc, msg = sender.apply_patch(Path(files["patch"]), Path.cwd())
        files["applied"] = rc == 0
        _result_out(job, body, jid, state, files, None if rc == 0 else msg, args.json)
        if rc == 0 and not args.json:
            print(f"  {msg}")
        return rc
    _result_out(job, body, jid, state, files, error, args.json)
    return 0 if state == "done" else 1


# ---- xval (independent cross-validation on a worker node) ----------------------------------

XVAL_VERDICTS = ("AGREE", "PARTIAL", "DISAGREE")
_XVAL_RE = re.compile(r"^\W*VERDICT:\s*(AGREE|PARTIAL|DISAGREE)\b", re.M)
XVAL_BRIEF = """You are an independent cross-validator. Another model wrote the work below.
Your job is to find what is wrong with it, not to praise it.
Rules:
1. Check every claim, number, file name and conclusion that the question names. Check the rest when time allows.
2. Give each finding: the location, the problem, the evidence, and a label: MEASURED (you checked it), PLAUSIBLE, or ASSUMED.
3. Use the web search tool to check public facts when it is available. Do not trust a claim because the work states it.
4. Do not edit or commit files. Your report is the result.
5. End with two lines: "VERDICT: AGREE", "VERDICT: PARTIAL" or "VERDICT: DISAGREE", then DONE.
   AGREE = no finding changes the conclusion. PARTIAL = some findings change parts. DISAGREE = the conclusion does not hold.
"""


def xval_verdict(report: str) -> str | None:
    """The last ``VERDICT: X`` line of a cross-validation report, or None."""
    found = _XVAL_RE.findall(report or "")
    return found[-1] if found else None


def build_xval_task(question: str, parts: list[tuple[str, str]]) -> str:
    """The xval brief + the question + each artifact as (label, text) between fences."""
    out = [XVAL_BRIEF, f"QUESTION: {question.strip() or 'Is this work correct and are its conclusions supported?'}\n"]
    for label, text in parts:
        out.append(f"===== BEGIN ARTIFACT: {label} =====\n{text.rstrip()}\n===== END ARTIFACT: {label} =====\n")
    return "\n".join(out)


def _xval_parts(args) -> tuple[list[tuple[str, str]] | None, str | None]:
    parts: list[tuple[str, str]] = []
    if args.diff:
        r = subprocess.run(["git", "diff", args.diff], capture_output=True, timeout=60)
        if r.returncode != 0:
            return None, f"git diff {args.diff} failed: {r.stderr.decode(errors='replace').strip()}"
        if not r.stdout.strip():
            return None, f"git diff {args.diff} is empty"
        parts.append((f"git diff {args.diff}", r.stdout.decode("utf-8", errors="replace")))
    for f in args.file or []:
        try:
            parts.append((f, Path(f).read_text(encoding="utf-8", errors="replace")))
        except OSError as e:
            return None, f"cannot read {f}: {e}"
    if args.stdin:
        parts.append(("stdin", sys.stdin.read()))
    if not parts:
        return None, "nothing to review: give --diff REF, --file PATH or --stdin"
    return parts, None


def _cmd_xval(args) -> int:
    """Submit one cross-validation job to a worker node, wait, print the report and verdict.

    Exit 0 AGREE, 1 PARTIAL or DISAGREE, 3 no verdict or the job did not finish, 2 usage."""
    sender, jobs = _submodule("sender"), _submodule("jobs")
    parts, err = _xval_parts(args)
    if parts is None:
        _err(f"xval: {err}")
        return 2
    task = build_xval_task(args.question or "", parts)
    n = len(task.encode("utf-8"))
    if n > jobs.MAX_TASK_BYTES:
        _err(f"xval: the review is {n} bytes; the limit is {jobs.MAX_TASK_BYTES}. "
             "Review the work in parts (one --file each).")
        return 2
    got = _load_ident()
    if got is None:
        return 2
    ident, home = got
    try:
        rows = _peer_rows(ident, home)
        to = sender.resolve_to(args.to, rows)
        if args.repo == jobs.XVAL_REPO and not getattr(args, "no_share", False):
            jobs.ensure_xval_repo(home)
        jid = sender.submit(ident, to=to, repo=args.repo, ref=args.ref, task=task,
                            max_effort=args.max_effort, expires_s=int(args.expires_min) * 60,
                            src=_job_src(args, to, rows, home, jobs))
    except jobs.JobError as e:
        _err(f"xval: submit: {e}")
        return 2
    except OSError as e:
        _err(f"xval: cannot write the outbox: {e}")
        return 1
    print(f"xval: job {jid} to {args.to} ({n} bytes); waiting", file=sys.stderr, flush=True)
    out_dir = Path(args.out_dir) if args.out_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    rargs = argparse.Namespace(id=jid, wait=True, timeout=args.timeout, apply=False, json=True,
                               out_dir=str(out_dir))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _cmd_result(rargs)
    try:
        doc = json.loads(buf.getvalue())
    except ValueError:
        _err(f"xval: job {jid}: no result")
        return 3
    res = doc.get("result") or {}
    report = res.get("report") or ""
    verdict = xval_verdict(report) if doc.get("state") == "done" else None
    if args.json:
        _dump({"id": jid, "state": doc.get("state"), "verdict": verdict,
               "report_file": (doc.get("files") or {}).get("report"), "report": report,
               "error": doc.get("error")})
    else:
        print(report.rstrip())
        print(f"\nxval: job {jid} state {doc.get('state')} verdict {verdict or 'none'}"
              + (f" ({doc.get('error')})" if doc.get("error") else ""))
    if verdict == "AGREE":
        return 0
    return 1 if verdict in XVAL_VERDICTS else 3


def _cmd_jobs(args) -> int:
    sender = _submodule("sender")
    got = _load_ident()
    if got is None:
        return 2
    ident, home = got
    names = {r["node_id"]: r.get("name") for r in _peer_rows(ident, home)}
    rows = sender.list_jobs(ident)
    for r in rows:
        r["to_name"] = names.get(r["to"]) or r["to"][:12]
    if args.json:
        _dump({"jobs": rows})
        return 0
    if not rows:
        print("no jobs in the outbox")
    for r in rows:
        print(f"{r['id']}  {str(r['to_name'])[:20]:<20}  {r['repo'][:24]:<24}  "
              f"{r['ref'][:32]:<32}  {r['state']}")
    return 0


def _cmd_worker(args) -> int:
    return _run_worker(list(args.rest or []))


def _run_worker(argv: list[str]) -> int:
    """Lazy import of ``worker`` (built separately); exit 2 when it is not installed."""
    try:
        worker = _submodule("worker")
    except ModuleNotFoundError as e:
        if e.name not in (f"{__package__}.worker", __package__):
            raise
        print("mesh worker: not installed", file=sys.stderr)
        return 2
    return int(worker.main(list(argv)) or 0)


# ---- notes ----------------------------------------------------------------------------------

NOTE_REASONS = {
    "empty": "the note is empty",
    "too_large": f"the note is over {notes.MAX_TEXT_BYTES} bytes",
    "bad_text": "the note holds control characters (only newline and tab are allowed)",
    "home_path": "the note holds an absolute home path (/Users/ or /home/); use a relative path",
    "secret": "the note looks like it holds a secret (key, token or private key)",
    "subject_too_long": f"--subject is over {notes.MAX_SUBJECT_CHARS} characters",
    "bad_subject": "--subject must be printable text",
}


def _cmd_note(args) -> int:
    if args.file == "-":
        data = sys.stdin.buffer.read(notes.MAX_TEXT_BYTES + 1)
    else:
        try:
            with open(args.file, "rb") as fh:
                data = fh.read(notes.MAX_TEXT_BYTES + 1)
        except OSError as e:
            _err(f"note: cannot read {args.file}: {e.strerror or e}")
            return 2
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _err("note: the text is not UTF-8")
        return 2
    try:
        notes.check_subject(args.subject)
        notes.check_text(text)
    except notes.NoteError as e:
        _err(f"note: refused: {NOTE_REASONS.get(str(e), str(e))}")
        return 2
    got = _load_ident()
    if got is None:
        return 2
    ident, home = got
    sender = _submodule("sender")
    try:
        to = sender.resolve_to(args.to, _peer_rows(ident, home))
    except ValueError as e:
        what = "more than one node has the name" if "ambiguous" in str(e) else "no node named"
        _err(f"note: {what} {args.to!r} in peers.json; use the node_id")
        return 2
    view = read_peers(ident, home)
    row = next((r for r in (view or {}).get("peers", []) if r["node_id"] == to), None)
    if row is None:
        _err(f"note: {args.to} is not alive in peers.json (is `mesh up` running on both nodes?)")
        return 1
    peer = parse_card(row, now=float(row["last_seen"]))
    opener = collect_mod._pinned_opener(collect_mod._local_ip())   # stay on the LAN interface
    res = notes.send(peer, ident, text, args.subject, timeout=args.timeout, opener=opener)
    if args.json:
        _dump(res)
    elif res["ok"]:
        print(f"note {res['id']} sent to {res['name']} ({res['node_id'][:12]}) "
              f"sha256 {res['sha256'][:12]}")
    elif res.get("error") == "peer_too_old":
        _err(f"note: peer {row['name']} (v{row.get('version')}) is too old: it has no "
             f"{notes.NOTE_PATH}. Update apex-router on it and restart `mesh up` there.")
    else:
        _err(f"note to {row['name']} failed: "
             f"{NOTE_REASONS.get(str(res.get('error')), res.get('error'))}")
    return 0 if res["ok"] else 1


def _note_block(r: dict) -> str:
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(r["received_at"])))
    lines = [notes.header_line(r["from_name"], r["from"]),
             f"id {r['id']}  received {when}  sha256 {r['sha256'][:12]}"
             + ("  (read)" if r["read"] else "")]
    if r["subject"]:
        lines.append(f"subject: {r['subject']}")
    lines += ["", r["text"].rstrip("\n"), f"END DATA {r['id']}"]
    return "\n".join(lines)


def _cmd_inbox(args) -> int:
    home = identity.mesh_home()
    rows = notes.read_notes(h=home, unread_only=not args.all, sender=args.sender)
    if args.json:
        _dump({"notes": [{"header": notes.header_line(r["from_name"], r["from"]), **r}
                         for r in rows]})
    elif not rows:
        print("no unread notes" if not args.all else "no notes")
    else:
        print("\n\n".join(_note_block(r) for r in rows))
    if args.mark_read and rows:
        try:
            notes.mark_read({r["id"] for r in rows}, h=home)
        except OSError as e:
            _err(f"inbox: cannot mark the notes read: {e}")
            return 1
    return 0


# ---- up -------------------------------------------------------------------------------------

def _parse_peer(spec: str, default_port: int = MCAST_PORT) -> tuple[str, int] | None:
    """``HOST``, ``HOST:PORT``, ``[V6]`` or ``[V6]:PORT`` -> (host, UDP beacon port), or None.

    A spec without a port (and a bare IPv6 address: 2+ colons, no brackets) takes ``default_port``.
    """
    s = spec.strip()
    if s.startswith("["):
        h, sep, rest = s[1:].partition("]")
        if not sep or (rest and not rest.startswith(":")):
            return None
        p = rest[1:] if rest else None
    elif s.count(":") == 1:
        h, _, p = s.partition(":")
    else:
        h, p = s, None
    if not h:
        return None
    if p is None:
        return h, default_port
    if not p.isdigit() or not 1 <= int(p) <= 65535:
        return None
    return h, int(p)


def run_up(args, stop: threading.Event, *, interval_s: float = BEACON_INTERVAL_S) -> int:
    """Run the node until ``stop`` is set. No signal handling here (testable in a thread)."""
    got = _load_ident()
    if got is None:
        return 2
    ident, home = got
    netif = _submodule("netif")
    try:
        host = netif.choose_bind(args.bind, allow_public=args.allow_public)
        shared = datasets.parse_share(args.share)
    except ValueError as e:
        _err(str(e))
        return 2
    beacon_port = getattr(args, "beacon_port", MCAST_PORT)
    if isinstance(beacon_port, bool) or not isinstance(beacon_port, int) \
            or not 1 <= beacon_port <= 65535:
        _err(f"bad --beacon-port {beacon_port}: use 1..65535")
        return 2
    targets = []
    for spec in args.peer or []:
        got_t = _parse_peer(spec, beacon_port)
        if got_t is None:
            _err(f"bad --peer {spec!r}: use HOST or HOST:PORT (UDP beacon port)")
            return 2
        targets.append(got_t)
    models = list(dict.fromkeys(getattr(args, "serve_model", None) or []))
    bad_m = [m for m in models if not valid_model(m)]
    if bad_m or len(models) > MODELS_MAX:
        _err(f"bad --serve-model: at most {MODELS_MAX} names of [A-Za-z0-9._:/-], <= 128 chars")
        return 2
    slots = getattr(args, "infer_slots", 1)
    if not isinstance(slots, int) or not 1 <= slots <= 4:
        _err(f"bad --infer-slots {slots}: use 1..4")
        return 2
    if models:
        try:
            infer.ollama_url()
        except ValueError as e:
            _err(str(e))
            return 2
    started = time.time()
    if not 0 <= args.port <= 65535:
        _err(f"bad --port {args.port}")
        return 2
    # --port 0 = OS picks; the card needs a valid port until the server reports the real one.
    # load is computed once per beacon cycle (it may run launchctl); card_fn only reads it, so
    # an unauthenticated GET /card never starts a process.
    # While model calls run, the card load also counts them (server.infer_running).
    state: dict[str, Any] = {"port": args.port or DEFAULT_HTTP_PORT, "load": _local_load(),
                             "server": None}
    # Self-update (selfupdate.py): only from `mesh up` with --auto-update (the CLI default).
    updater = _submodule("selfupdate").AutoUpdater(home) \
        if getattr(args, "auto_update", False) else None

    def card_fn() -> dict:
        load = state["load"]
        srv = state["server"]
        running = getattr(srv, "infer_running", 0) if srv is not None else 0
        if isinstance(running, int) and running > 0:
            load = min(LOAD_MAX, load + running)
        build = updater.card_build() if updater is not None else None
        return _self_card(ident, host, state["port"], args.role or [], started, load, models,
                          build)

    try:
        table = PeerTable(card_fn())
    except ValueError as e:
        _err(f"bad self card: {e}")
        return 2
    if models:
        # The beacon drops an announce larger than one datagram: refuse at start, not silently.
        probe = _self_card(ident, host, 65535, args.role or [], started, LOAD_MAX, models)
        if len(protocol.encode("announce", probe, ident)) > MAX_DATAGRAM:
            _err(f"--serve-model names make the beacon card > {MAX_DATAGRAM} bytes; "
                 "serve fewer or shorter names")
            return 2
    server_mod, beacon_mod = _submodule("server"), _submodule("beacon")
    server_kw: dict[str, Any] = {"shared": shared}
    if models:
        server_kw.update(models=tuple(models), infer_slots=slots)
    try:
        server = server_mod.MeshServer(ident, table, card_fn, host=host, port=args.port,
                                       **server_kw)
    except ValueError as e:
        _err(str(e))
        return 2
    state["server"] = server
    beacon = None
    pid_path, peers_path = home / PID_FILE, home / PEERS_FILE
    lock_fd = _take_lock(home)
    if lock_fd is None:
        _err(f"already running (lock {home / LOCK_FILE} is held)")
        return 1
    own_pid = os.getpid()
    restart = False
    try:
        server.start()
        state["port"] = int(server.address[1])
        table.update(card_fn())
        beacon = beacon_mod.Beacon(ident, table, card_fn, iface_ip=host, port=beacon_port,
                                   targets=targets or None)
        beacon.start()
        _write_json(pid_path, {"pid": own_pid, "host": host, "port": state["port"],
                               "beacon_port": beacon_port, "started_at": started,
                               "shared": sorted(shared)})
        print(f"mesh up: {ident.name} {ident.node_id[:12]} on {host}:{state['port']} "
              f"(mesh {ident.mesh_id}, sharing {len(shared)} dataset(s), "
              f"serving {len(models)} model(s))", flush=True)
        while True:
            table.update(card_fn())     # refresh self.last_seen so readers see us alive
            snap = table.snapshot()
            snap["mesh_id"] = ident.mesh_id
            try:
                _write_json(peers_path, snap)
            except (OSError, TypeError, ValueError) as e:
                _err(f"cannot write {PEERS_FILE}: {e}")
            if stop.wait(interval_s):
                break
            state["load"] = _local_load()
            if updater is not None:
                updater.tick(table.alive())
                if updater.restart_needed():
                    print("mesh up: apex-router updated; restarting", flush=True)
                    restart = True
                    break
    finally:
        if beacon is not None:
            try:
                beacon.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            server.stop()
        except Exception:  # noqa: BLE001
            pass
        doc = _read_json(pid_path)
        if isinstance(doc, dict) and doc.get("pid") == own_pid:
            try:
                pid_path.unlink()
            except OSError:
                pass
        os.close(lock_fd)                  # releases the flock
    print("mesh down", flush=True)
    return RESTART if restart else 0


RESTART = 75          # run_up result: an update landed; _cmd_up re-execs with the same argv


def _cmd_up(args) -> int:
    stop = threading.Event()
    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        rc = run_up(args, stop)
    except Exception as e:  # noqa: BLE001 -- one line, no traceback
        _err(f"cannot start: {type(e).__name__}: {e}")
        return 1
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
    if rc == RESTART:
        _submodule("selfupdate").reexec()
    return rc


def _cmd_update(args) -> int:
    """``mesh update``: run the self-update once (or only report with --check)."""
    su = _submodule("selfupdate")
    home = identity.mesh_home()
    res = su.update(home, dry_run=args.check)
    if args.json:
        _dump(res)
    else:
        old, new = (res.get("old") or "?")[:12], (res.get("new") or "?")[:12]
        extra = f" (upstream {str(res['upstream'])[:12]})" if res.get("upstream") else ""
        print(f"mesh update: {res['status']}  {old} -> {new}{extra}")
        if res["status"] == "updated":
            print("mesh update: running `mesh up` and `mesh worker` restart themselves at idle")
    return 0 if res["status"] in ("ok", "updated") else 1


# ---- parser ---------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="apex-router mesh",
                                 description="Opt-in LAN mesh: find, organize and pool metrics "
                                             "of apex-router nodes on one local network.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create or import the node identity and mesh secret")
    p.add_argument("--name")
    p.add_argument("--import", dest="import_token", metavar="TOKEN")
    p.add_argument("--export", action="store_true", help="print the secret token (copy out of band)")
    p.add_argument("--force", action="store_true", help="replace an existing identity")
    p.set_defaults(fn=_cmd_init)

    p = sub.add_parser("up", help="run the node in the foreground (beacon + HTTP)")
    p.add_argument("--bind")
    p.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    p.add_argument("--allow-public", action="store_true")
    p.add_argument("--role", action="append", default=[])
    # A second mesh on one host (own APEX_MESH_HOME) needs its own beacon port: two sockets
    # on one UDP port share it (SO_REUSEPORT) and a unicast announce reaches only one of them.
    p.add_argument("--beacon-port", type=int, default=MCAST_PORT, metavar="PORT",
                   help=f"UDP beacon port (multicast and unicast, default {MCAST_PORT}); "
                        "all nodes of one mesh must use the same port")
    p.add_argument("--peer", action="append", default=[], metavar="HOST[:PORT]",
                   help="extra unicast beacon target; PORT = its UDP beacon port "
                        "(default: --beacon-port), not the HTTP port")
    p.add_argument("--share", action="append", default=[], metavar="NAME")
    p.add_argument("--serve-model", action="append", default=[], metavar="NAME",
                   help="serve this local ollama model to the mesh (POST /mesh/v1/infer); "
                        "repeatable. Default: no model calls")
    p.add_argument("--infer-slots", type=int, default=1, metavar="N",
                   help="parallel model calls (1..4, default 1); more get 429 busy")
    p.add_argument("--no-auto-update", dest="auto_update", action="store_false",
                   help="do not fast-forward this apex-router checkout from its own origin "
                        "when a peer runs a newer build (also: update.json, "
                        "APEX_MESH_NO_UPDATE=1)")
    p.set_defaults(fn=_cmd_up, auto_update=True)

    p = sub.add_parser("update", help="fast-forward this apex-router checkout from its own "
                                      "origin now (clean main only); --check only reports")
    p.add_argument("--check", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=_cmd_update)

    for name, fn, hlp in (("peers", _cmd_peers, "list alive peers"),
                          ("leader", _cmd_leader, "show the leader"),
                          ("status", _cmd_status, "identity and whether `up` runs")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)

    p = sub.add_parser("pick", help="best alive node for ROLE (exit 0 found, 1 none)")
    p.add_argument("role")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=_cmd_pick)

    p = sub.add_parser("models", help="list node, model for every served model")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=_cmd_models)

    p = sub.add_parser("ask", help="run one chat prompt on a node that serves --model "
                                   "(exit 0 ok, 1 none or failed, 2 usage)")
    p.add_argument("--model", required=True)
    p.add_argument("--node", metavar="NAME", help="node name or node_id")
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--system", metavar="S")
    p.add_argument("--timeout", type=float, default=infer.UPSTREAM_TIMEOUT_S)
    p.add_argument("--json", action="store_true")
    p.add_argument("prompt", metavar="PROMPT", help='prompt text, or "-" to read stdin')
    p.set_defaults(fn=_cmd_ask)

    p = sub.add_parser("collect", help="fetch and pool datasets from all alive nodes")
    p.add_argument("names", nargs="*", metavar="NAME")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=3.0)
    p.set_defaults(fn=_cmd_collect)

    p = sub.add_parser("advise", help="pooled route_advice table")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=3.0)
    p.set_defaults(fn=_cmd_advise)

    # mesh worker (sender side + worker launcher)
    p = sub.add_parser("submit", help="offer an agent job to a worker node; prints the job id")
    p.add_argument("--to", required=True, metavar="NAME|NODE_ID")
    p.add_argument("--repo", required=True, metavar="NAME")
    p.add_argument("--ref", required=True, metavar="REF")
    p.add_argument("--check", default="", metavar="CMD")
    p.add_argument("--max-effort", default="high", metavar="LEVEL",
                   help="ladder ceiling: high|xhigh|max|opus (default high)")
    p.add_argument("--expires-min", type=int, default=60, metavar="N", help="1..60 (default 60)")
    p.add_argument("--no-share", action="store_true",
                   help="the worker fetches REF from its own origin, even if REPO is in "
                        "share_repos.json")
    p.add_argument("task", metavar="TASK", help='task text, or "-" to read stdin')
    p.set_defaults(fn=_cmd_submit)

    p = sub.add_parser("result", help="fetch a job result; writes ID.patch and ID.md here "
                                      "(exit 0 done, 1 not done, 2 usage/refused)")
    p.add_argument("id", metavar="ID")
    p.add_argument("--wait", action="store_true", help=f"poll every {RESULT_POLL_S:g} s")
    p.add_argument("--timeout", type=float, default=None, metavar="S",
                   help="--wait limit in seconds (default 1800)")
    p.add_argument("--apply", action="store_true", help="git am --3way the patch in this repo")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=_cmd_result)

    p = sub.add_parser("xval", help="independent cross-validation on a worker node: submit, wait, "
                                    "print the verdict (exit 0 AGREE, 1 PARTIAL/DISAGREE, 3 none)")
    p.add_argument("--to", required=True, metavar="NAME|NODE_ID")
    p.add_argument("--diff", metavar="REF", help="review `git diff REF` of the current repo")
    p.add_argument("--file", action="append", default=[], metavar="PATH", help="repeatable")
    p.add_argument("--stdin", action="store_true", help="review text from stdin")
    p.add_argument("--question", default="", metavar="TEXT", help="what the reviewer must check")
    p.add_argument("--repo", default="xval", metavar="NAME",
                   help="scratch repo in the worker allowlist (default xval)")
    p.add_argument("--ref", default="main", metavar="REF")
    p.add_argument("--max-effort", default="high", metavar="LEVEL",
                   help="ladder ceiling: high|xhigh|max|opus (default high)")
    p.add_argument("--expires-min", type=int, default=60, metavar="N")
    p.add_argument("--timeout", type=float, default=3600.0, metavar="S", help="wait limit (default 3600)")
    p.add_argument("--out-dir", default=None, metavar="DIR", help="where ID.md goes (default .)")
    p.add_argument("--no-share", action="store_true",
                   help="do not send the scratch repo as a bundle (the worker needs its own)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=_cmd_xval)

    p = sub.add_parser("jobs", help="list offered jobs and their state")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=_cmd_jobs)

    p = sub.add_parser("note", help="send one short text note to a peer; the peer only stores "
                                    "it (exit 0 sent, 1 failed, 2 refused/usage)")
    p.add_argument("--to", required=True, metavar="NAME|NODE_ID")
    p.add_argument("--subject", default="", metavar="TEXT",
                   help=f"at most {notes.MAX_SUBJECT_CHARS} characters")
    p.add_argument("--timeout", type=float, default=notes.SEND_TIMEOUT_S, metavar="S")
    p.add_argument("--json", action="store_true")
    p.add_argument("file", metavar="FILE", help=f'UTF-8 text, at most {notes.MAX_TEXT_BYTES} '
                                                'bytes, or "-" to read stdin')
    p.set_defaults(fn=_cmd_note)

    p = sub.add_parser("inbox", help="print the unread notes from peers")
    p.add_argument("--all", action="store_true", help="also print notes already read")
    p.add_argument("--from", dest="sender", metavar="NAME", help="peer name or node_id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--mark-read", action="store_true", help="mark the printed notes read")
    p.set_defaults(fn=_cmd_inbox)

    p = sub.add_parser("worker", help="run the job worker (all args go to the worker)",
                       add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=_cmd_worker)
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["worker"]:
        # argparse REMAINDER drops a first token that starts with "-" (e.g. `worker --pause`).
        # So hand every argument after "worker" to worker.main unchanged.
        return _run_worker(argv[1:])
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
