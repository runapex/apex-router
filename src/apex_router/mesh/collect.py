"""LAN mesh data plane: fetch allowlisted datasets from alive peers in parallel and pool them.

Contract: docs/DESIGN-lan-mesh.md, "Pooling" and "HTTP". ``fetch`` never raises. Self is read
locally with ``datasets.read`` (no HTTP). Every remote response is a signed envelope of kind
``data`` whose ``body.node_id`` must equal the peer we asked.
"""
from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Any

from . import datasets, identity, protocol

if TYPE_CHECKING:
    from .identity import Identity
    from .peers import Peer, PeerTable

MAX_RESPONSE_BYTES = 1 << 20          # 1 MiB cap on one HTTP response body
DATA_PATH = "/mesh/v1/data"
AUTH_HEADER = "X-Apex-Mesh"
PID_FILE = "mesh.pid"                 # same file as cli.PID_FILE (written by `mesh up`)
LEVEL_RANK = {"UNKNOWN": 0, "GREEN": 1, "AMBER": 2, "RED": 3}

# Responses are signed envelopes; remember their nonces so a replayed response is rejected.
_RESPONSE_GUARD = protocol.ReplayGuard()
# LAN traffic must never go through an HTTP(S)_PROXY from the environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _host_for_url(host: str) -> str:
    try:
        if ipaddress.ip_address(host).version == 6:
            return f"[{host}]"
    except ValueError:
        pass
    return host


def data_path(names: list[str]) -> str:
    """Request path + query for ``names``. The auth envelope signs exactly this string."""
    return f"{DATA_PATH}?names=" + urllib.parse.quote(",".join(names), safe=",")


def _result(peer: Peer, ok: bool, data: dict | None = None, error: str | None = None) -> dict:
    out = {"node_id": peer.node_id, "name": peer.name, "ok": ok, "data": data or {}}
    if error is not None:
        out["error"] = error
    return out


def fetch(peer: Peer, ident: Identity, names: list[str], *, timeout: float = 3.0) -> dict:
    """GET /mesh/v1/data?names=... from one peer. Returns the result dict; never raises."""
    try:
        names = [str(n) for n in names]
        path = data_path(names)
        url = f"http://{_host_for_url(peer.host)}:{int(peer.port)}{path}"
        req = urllib.request.Request(url, method="GET",
                                     headers={AUTH_HEADER: protocol.auth_header(
                                         ident, path, peer.node_id)})
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as e:
            return _result(peer, False, error=f"http {e.code}")
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            return _result(peer, False, error=f"unreachable: {type(reason).__name__}")
        try:
            env = protocol.decode(raw, ident, _RESPONSE_GUARD, max_bytes=MAX_RESPONSE_BYTES)
        except protocol.ProtocolError as e:
            return _result(peer, False, error=f"bad response: {e}")
        body = env["body"]
        if env["kind"] != "data":
            return _result(peer, False, error=f"bad response: kind {env['kind']!r}")
        if body.get("node_id") != peer.node_id:
            return _result(peer, False, error="bad response: node_id mismatch")
        data = body.get("data")
        if not isinstance(data, dict):
            return _result(peer, False, error="bad response: data is not an object")
        return _result(peer, True, {n: data[n] for n in names if n in data})
    except Exception as e:  # noqa: BLE001 -- contract: fetch never raises
        return _result(peer, False, error=f"error: {type(e).__name__}")


def _self_shared() -> frozenset:
    """Names this node serves: ``shared`` from mesh.pid (``--share``) when present, else all."""
    try:
        doc = json.loads((identity.mesh_home() / PID_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset(datasets.ALLOWLIST)
    shared = doc.get("shared") if isinstance(doc, dict) else None
    if shared is None:
        return frozenset(datasets.ALLOWLIST)
    if not isinstance(shared, list):
        return frozenset()                 # malformed: fail closed
    return frozenset(n for n in shared if isinstance(n, str))


def _read_self(peer: Peer, names: list[str]) -> dict:
    try:
        return _result(peer, True, datasets.read(names, _self_shared()))
    except Exception as e:  # noqa: BLE001
        return _result(peer, False, error=f"error: {type(e).__name__}")


def collect(table: PeerTable, ident: Identity, names: list[str], *,
            include_self: bool = True, timeout: float = 3.0, max_workers: int = 16) -> dict:
    """Fetch ``names`` from every alive peer in parallel; self is read locally.

    Returns ``{"nodes": [fetch results], "pooled": pool(nodes), "n_nodes": int, "n_ok": int}``.
    Node order follows ``table.alive()`` (sorted by node_id). ``timeout`` is also the total
    deadline of the remote fan-out: a peer with no result by then gives error ``timeout``.
    """
    names = list(names)
    alive = table.alive()
    self_id = table.self_id
    targets = [p for p in alive if p.node_id != self_id or include_self]
    remote = [p for p in targets if p.node_id != self_id]
    results: dict[str, dict] = {}
    if remote:
        workers = max(1, min(int(max_workers), len(remote)))
        ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mesh-collect")
        try:
            futs = [(p, ex.submit(fetch, p, ident, names, timeout=timeout)) for p in remote]
            wait([f for _, f in futs], timeout=timeout)
            for p, fut in futs:
                results[p.node_id] = (fut.result() if fut.done()
                                      else _result(p, False, error="timeout"))
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    for p in targets:
        if p.node_id == self_id:
            results[p.node_id] = _read_self(p, names)
    nodes = [results[p.node_id] for p in targets]
    return {"nodes": nodes, "pooled": pool(nodes), "n_nodes": len(nodes),
            "n_ok": sum(1 for r in nodes if r.get("ok"))}


def _dataset(result: dict, name: str) -> Any:
    """The value of ``name`` from an ok result, or None if missing or an error stub."""
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    val = data.get(name)
    if isinstance(val, dict) and set(val) == {"error"}:
        return None
    return val


def _pool_rates(results: list[dict]) -> dict:
    pooled: dict[str, dict] = {}
    for r in results:
        rates = _dataset(r, "route_rates")
        if not isinstance(rates, dict):
            continue
        for tt, cell in rates.items():
            if not isinstance(tt, str) or not isinstance(cell, dict):
                continue
            n, esc = cell.get("n"), cell.get("escalated")
            if not _is_int(n) or not _is_int(esc) or n < 0 or not 0 <= esc <= n:
                continue
            null_ts = cell.get("null_ts", 0)
            if not _is_int(null_ts) or null_ts < 0:
                null_ts = 0
            acc = pooled.setdefault(tt, {"n": 0, "escalated": 0, "null_ts": 0})
            acc["n"] += n
            acc["escalated"] += esc
            acc["null_ts"] += null_ts
    out: dict[str, dict] = {}
    for tt in sorted(pooled):
        acc = pooled[tt]
        cell = {"n": acc["n"], "escalated": acc["escalated"],
                "rate": acc["escalated"] / acc["n"] if acc["n"] else 0.0}
        if acc["null_ts"]:
            cell["null_ts"] = acc["null_ts"]
        out[tt] = cell
    return out


def _pool_pressure(results: list[dict]) -> dict:
    worst = "UNKNOWN"
    nodes = []
    for r in results:
        if not isinstance(r, dict) or not r.get("ok"):
            continue
        val = _dataset(r, "pressure")
        level = val.get("level") if isinstance(val, dict) else None
        if level not in LEVEL_RANK:
            level = "UNKNOWN"
        nodes.append({"node_id": r.get("node_id"), "name": r.get("name"), "level": level})
        if LEVEL_RANK[level] > LEVEL_RANK[worst]:
            worst = level
    return {"level": worst, "nodes": nodes}


def _pool_errors(results: list[dict]) -> dict:
    total, by_cause = 0, {}
    for r in results:
        val = _dataset(r, "errors15m")
        if not isinstance(val, dict):
            continue
        if _is_int(val.get("n")) and val["n"] >= 0:
            total += val["n"]
        causes = val.get("by_cause")
        if isinstance(causes, dict):
            for cause, cnt in causes.items():
                if isinstance(cause, str) and _is_int(cnt) and cnt >= 0:
                    by_cause[cause] = by_cause.get(cause, 0) + cnt
    by_cause = dict(sorted(by_cause.items(), key=lambda kv: (-kv[1], kv[0])))
    return {"n": total, "by_cause": by_cause}


def _per_node(results: list[dict], name: str) -> dict:
    out = {}
    for r in results:
        val = _dataset(r, name)
        if val is not None:
            out[r.get("node_id")] = val
    return out


def pool(results: list[dict]) -> dict:
    """Merge ok fetch results. Bad cells and error stubs are skipped, never raised."""
    results = [r for r in (results or []) if isinstance(r, dict)]
    rates = _pool_rates(results)
    try:
        from .. import route_advise
        advice = route_advise.advise(rates=rates)
    except Exception:  # noqa: BLE001 -- advise is fail-safe; keep pool fail-safe too
        advice = {}
    return {
        "route_rates": rates,
        "route_advice_pooled": advice,
        "pressure": _pool_pressure(results),
        "errors15m": _pool_errors(results),
        "measure": _per_node(results, "measure"),
    }
