"""LAN mesh peer table: card validation, TTL expiry, leader election, role pick. No network.

A card comes from a signed envelope, but a peer with the key can still send junk. So every field
is type- and bound-checked; a bad card is rejected (``update`` returns None), never raised.
"""
from __future__ import annotations

import ipaddress
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from .protocol import PEER_TTL_S

NAME_MAX = 64
VERSION_MAX = 64
HOST_MAX = 64
ROLE_MAX = 32
ROLES_MAX = 16
LOAD_MAX = 1_000_000
MAX_PEERS = 256                     # bound memory if a key holder floods fake node ids
_STARTED_MIN, _STARTED_MAX = 0.0, 32503680000.0   # 1970 .. 3000

_NODE_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_ROLE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,31}\Z")


@dataclass
class Peer:
    node_id: str
    name: str
    host: str
    port: int
    version: str
    roles: tuple[str, ...]
    load: int
    started_at: float
    last_seen: float

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "name": self.name, "host": self.host,
                "port": self.port, "version": self.version, "roles": list(self.roles),
                "load": self.load, "started_at": self.started_at, "last_seen": self.last_seen}


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _short_text(v: Any, limit: int) -> bool:
    return isinstance(v, str) and 0 < len(v) <= limit and v.isprintable()


def parse_card(card: Any, *, now: float) -> Peer | None:
    """Return a Peer for a valid card, else None. Unknown extra keys are ignored."""
    try:
        if not isinstance(card, dict):
            return None
        node_id, name, host = card.get("node_id"), card.get("name"), card.get("host")
        port, version, roles = card.get("port"), card.get("version"), card.get("roles")
        load, started = card.get("load"), card.get("started_at")
        if not isinstance(node_id, str) or not _NODE_ID_RE.match(node_id):
            return None
        if not _short_text(name, NAME_MAX) or not _short_text(version, VERSION_MAX):
            return None
        if not isinstance(host, str) or not 0 < len(host) <= HOST_MAX:
            return None
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            return None
        if not _is_int(port) or not 1 <= port <= 65535:
            return None
        if not isinstance(roles, (list, tuple)) or len(roles) > ROLES_MAX:
            return None
        if not all(isinstance(r, str) and _ROLE_RE.match(r) for r in roles):
            return None
        if not _is_int(load) or not 0 <= load <= LOAD_MAX:
            return None
        if isinstance(started, bool) or not isinstance(started, (int, float)) \
                or not math.isfinite(started) or not _STARTED_MIN <= started <= _STARTED_MAX:
            return None
        return Peer(node_id=node_id, name=name, host=host, port=port, version=version,
                    roles=tuple(dict.fromkeys(roles)), load=load, started_at=float(started),
                    last_seen=float(now))
    except Exception:   # noqa: BLE001 -- contract: a bad card never raises
        return None



class PeerTable:
    """Thread-safe table of alive peers, self included. Self never expires."""

    def __init__(self, self_card: dict, ttl_s: float = PEER_TTL_S) -> None:
        me = parse_card(self_card, now=time.time())
        if me is None:
            raise ValueError("self_card is not a valid node card")
        if not (isinstance(ttl_s, (int, float)) and math.isfinite(ttl_s) and ttl_s > 0):
            raise ValueError("ttl_s must be > 0")
        self._lock = threading.Lock()
        self._self = me
        self._ttl = float(ttl_s)
        self._peers: dict[str, Peer] = {}

    @property
    def self_id(self) -> str:
        return self._self.node_id

    def _now(self, now: float | None) -> float:
        return time.time() if now is None else float(now)

    def _expire(self, t: float) -> None:
        dead = [nid for nid, p in self._peers.items() if t - p.last_seen > self._ttl]
        for nid in dead:
            del self._peers[nid]

    def update(self, card: dict, *, now: float | None = None) -> Peer | None:
        """Insert or refresh a peer from its card. A card with our own node id refreshes self.

        Returns the stored Peer, or None for a bad card or a full table.
        """
        t = self._now(now)
        peer = parse_card(card, now=t)
        if peer is None:
            return None
        with self._lock:
            if peer.node_id == self._self.node_id:
                self._self = peer
                return peer
            self._expire(t)
            if peer.node_id not in self._peers and len(self._peers) >= MAX_PEERS:
                return None
            self._peers[peer.node_id] = peer
            return peer

    def remove(self, node_id: str) -> None:
        """Drop a peer (e.g. on ``bye``). Self cannot be removed."""
        with self._lock:
            self._peers.pop(node_id, None)

    def _alive(self, t: float) -> list[Peer]:
        self._expire(t)
        return sorted([self._self, *self._peers.values()], key=lambda p: p.node_id)

    def alive(self, *, now: float | None = None) -> list[Peer]:
        """Alive peers including self, sorted by node_id."""
        with self._lock:
            return self._alive(self._now(now))

    def leader(self, *, now: float | None = None) -> Peer:
        """Oldest alive node: min (started_at, node_id). Self is always alive, so never None."""
        with self._lock:
            return min(self._alive(self._now(now)), key=lambda p: (p.started_at, p.node_id))

    def pick(self, role: str, *, now: float | None = None) -> Peer | None:
        """Alive node with ``role`` and min (load, node_id); None if no node has the role."""
        with self._lock:
            cands = [p for p in self._alive(self._now(now)) if role in p.roles]
        return min(cands, key=lambda p: (p.load, p.node_id)) if cands else None

    def snapshot(self, *, now: float | None = None) -> dict:
        """JSON-safe view: self id, leader id, ttl and the alive peers with their age."""
        t = self._now(now)
        with self._lock:
            peers = self._alive(t)
            lead = min(peers, key=lambda p: (p.started_at, p.node_id))
            rows = [{**p.to_dict(), "self": p.node_id == self._self.node_id,
                     "age_s": round(max(0.0, t - p.last_seen), 3)} for p in peers]
        return {"self": self._self.node_id, "leader": lead.node_id, "ttl_s": self._ttl,
                "ts": t, "n_alive": len(rows), "peers": rows}
