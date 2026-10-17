"""``apex-router mesh ...`` subcommands (contract: docs/DESIGN-lan-mesh.md, both "CLI" sections).

``up`` is the only command that opens sockets. It writes ``peers.json`` (atomic, 0600) each
beacon cycle; ``peers``, ``leader``, ``pick``, ``collect`` and ``advise`` read that file and drop
entries older than PEER_TTL_S. The secret is printed only by ``init --export``.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import collect as collect_mod
from . import datasets, identity
from .identity import Identity, MeshNotInitialized
from .peers import PeerTable, parse_card
from .protocol import BEACON_INTERVAL_S, DEFAULT_HTTP_PORT, PEER_TTL_S

PEERS_FILE = "peers.json"
PID_FILE = "mesh.pid"


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


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _submodule(name: str):
    """Import a sibling module by name (lets tests inject fakes via sys.modules)."""
    return importlib.import_module(f"{__package__}.{name}")


def _self_card(ident: Identity, host: str, port: int, roles: list[str], started_at: float,
               load: int = 0) -> dict:
    return {"node_id": ident.node_id, "name": ident.name, "host": host, "port": port,
            "version": _version(), "roles": list(roles), "load": int(load),
            "started_at": started_at}


def _local_load() -> int:
    """Running agent count for the card ``load``. Never raises."""
    try:
        from .. import agents
        n = agents.worker().get("running", 0)
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
    return (f"{flags} {r['node_id'][:12]}  {r['name'][:20]:<20}  {r['host']}:{r['port']:<5}  "
            f"load={r['load']:<4} age={r.get('age_s', 0):.0f}s  roles={roles}  v{r['version']}")


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
    running = _pid_alive(pid)
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
    got = _run_collect(["route_rates"], args.timeout)
    if got is None:
        return 2
    res, have_view = got
    advice = res["pooled"]["route_advice_pooled"]
    if args.json:
        _dump({"n_nodes": res["n_nodes"], "n_ok": res["n_ok"],
               "route_rates": res["pooled"]["route_rates"], "route_advice_pooled": advice})
        return 0 if res["n_ok"] else 1
    if not have_view:
        print("note: no fresh peers.json; local node only")
    print(f"pooled over {res['n_ok']}/{res['n_nodes']} nodes")
    if not advice:
        print("no route data")
    for tt, rec in sorted(advice.items()):
        if not isinstance(rec, dict):
            continue
        print(f"  {tt:<24} {str(rec.get('verdict')):<14} n={rec.get('n', 0):<6} "
              f"rate={float(rec.get('rate') or 0):.3f} "
              f"ci=[{float(rec.get('ci_low') or 0):.3f},{float(rec.get('ci_high') or 0):.3f}]")
    return 0 if res["n_ok"] else 1


# ---- up -------------------------------------------------------------------------------------

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
    targets = []
    for spec in args.peer or []:
        h, sep, p = spec.rpartition(":")
        h = h.strip("[]")
        if not sep or not h or not p.isdigit() or not 1 <= int(p) <= 65535:
            _err(f"bad --peer {spec!r}: use HOST:PORT")
            return 2
        targets.append((h, int(p)))
    started = time.time()
    if not 0 <= args.port <= 65535:
        _err(f"bad --port {args.port}")
        return 2
    # --port 0 = OS picks; the card needs a valid port until the server reports the real one.
    state = {"port": args.port or DEFAULT_HTTP_PORT}

    def card_fn() -> dict:
        return _self_card(ident, host, state["port"], args.role or [], started, _local_load())

    try:
        table = PeerTable(card_fn())
    except ValueError as e:
        _err(f"bad self card: {e}")
        return 2
    server_mod, beacon_mod = _submodule("server"), _submodule("beacon")
    server = server_mod.MeshServer(ident, table, card_fn, host=host, port=args.port,
                                   shared=shared)
    beacon = None
    pid_path, peers_path = home / PID_FILE, home / PEERS_FILE
    try:
        server.start()
        state["port"] = int(server.address[1])
        table.update(card_fn())
        beacon = beacon_mod.Beacon(ident, table, card_fn, iface_ip=host,
                                   targets=targets or None)
        beacon.start()
        _write_json(pid_path, {"pid": os.getpid(), "host": host, "port": state["port"],
                               "started_at": started, "shared": sorted(shared)})
        print(f"mesh up: {ident.name} {ident.node_id[:12]} on {host}:{state['port']} "
              f"(mesh {ident.mesh_id}, sharing {len(shared)} dataset(s))", flush=True)
        while True:
            table.update(card_fn())     # refresh self.last_seen so readers see us alive
            snap = table.snapshot()
            snap["mesh_id"] = ident.mesh_id
            _write_json(peers_path, snap)
            if stop.wait(interval_s):
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
        try:
            pid_path.unlink()
        except OSError:
            pass
    print("mesh down", flush=True)
    return 0


def _cmd_up(args) -> int:
    stop = threading.Event()
    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        return run_up(args, stop)
    except (OSError, ValueError) as e:
        _err(f"cannot start: {e}")
        return 1
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)


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
    p.add_argument("--peer", action="append", default=[], metavar="HOST:PORT")
    p.add_argument("--share", action="append", default=[], metavar="NAME")
    p.set_defaults(fn=_cmd_up)

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

    p = sub.add_parser("collect", help="fetch and pool datasets from all alive nodes")
    p.add_argument("names", nargs="*", metavar="NAME")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=3.0)
    p.set_defaults(fn=_cmd_collect)

    p = sub.add_parser("advise", help="pooled route_advice table")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=3.0)
    p.set_defaults(fn=_cmd_advise)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
