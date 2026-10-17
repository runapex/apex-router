"""LAN mesh wire protocol: envelope encode/decode, HMAC-SHA256, clock skew, replay window.

No network. ``decode`` checks, in this order: size, JSON, proto, mesh id, MAC, skew, replay.
Any failure raises ProtocolError. The replay guard records a nonce only after the MAC and skew
checks pass, so unsigned traffic cannot fill or poison it.
"""
from __future__ import annotations

import hashlib
import heapq
import hmac
import json
import math
import re
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .identity import Identity

PROTO = "apex-mesh/1"
MCAST_GROUP = "239.255.77.77"     # admin-scoped (RFC 2365), stays on the LAN
MCAST_PORT = 47777
DEFAULT_HTTP_PORT = 47778
MAX_SKEW_S = 30
MAX_DATAGRAM = 1400               # one Ethernet frame, no fragmentation
BEACON_INTERVAL_S = 5
PEER_TTL_S = 20                   # 4 missed beacons

ENVELOPE_KEYS = frozenset({"proto", "kind", "mesh", "ts", "nonce", "body", "mac"})
_KIND_RE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")
_NONCE_RE = re.compile(r"\A[0-9a-f]{16}\Z")
_MESH_RE = re.compile(r"\A[0-9a-f]{16}\Z")
_MAC_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class ProtocolError(ValueError):
    """A message failed a wire check. Drop it."""


def canonical(obj: Any) -> bytes:
    """Canonical JSON: UTF-8, sorted keys, compact separators, no NaN/Infinity."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _mac(secret: bytes, unsigned: dict) -> str:
    return hmac.new(secret, canonical(unsigned), hashlib.sha256).hexdigest()


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


class ReplayGuard:
    """Bounded, thread-safe nonce memory.

    ``seen`` returns True (= replay, drop) for a nonce already recorded, or for a ts at or below
    the floor. The floor rises when entries leave memory (age past 2*MAX_SKEW_S behind the newest
    ts, or eviction at ``max_entries``). So a forgotten nonce can never be accepted again: the
    guard fails closed. Call ``seen`` only on a message whose MAC and skew already passed.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max = max_entries
        self._lock = threading.Lock()
        self._nonces: dict[str, float] = {}
        self._heap: list[tuple[float, str]] = []
        self._floor = -math.inf
        self._newest = -math.inf

    def __len__(self) -> int:
        with self._lock:
            return len(self._nonces)

    def _drop_oldest(self) -> None:
        ts, nonce = heapq.heappop(self._heap)
        self._nonces.pop(nonce, None)
        self._floor = max(self._floor, ts)

    def seen(self, nonce: str, ts: float) -> bool:
        with self._lock:
            if nonce in self._nonces or ts <= self._floor:
                return True
            self._newest = max(self._newest, ts)
            horizon = self._newest - 2 * MAX_SKEW_S
            while self._heap and self._heap[0][0] < horizon:
                self._drop_oldest()
            self._floor = max(self._floor, horizon)
            if ts <= self._floor:
                return True
            while len(self._nonces) >= self._max:
                self._drop_oldest()
            if ts <= self._floor:      # evicted entries at/after this ts: fail closed
                return True
            self._nonces[nonce] = ts
            heapq.heappush(self._heap, (ts, nonce))
            return False


def encode(kind: str, body: dict, ident: Identity, *, now: float | None = None) -> bytes:
    """Sign and serialize one envelope. Raises ProtocolError on a bad kind or body."""
    if not isinstance(kind, str) or not _KIND_RE.match(kind):
        raise ProtocolError(f"bad kind {kind!r}")
    if not isinstance(body, dict):
        raise ProtocolError("body must be a dict")
    ts = time.time() if now is None else float(now)
    env = {"proto": PROTO, "kind": kind, "mesh": ident.mesh_id, "ts": ts,
           "nonce": secrets.token_hex(8), "body": body}
    try:
        env["mac"] = _mac(ident.secret, env)
        return canonical(env)
    except (TypeError, ValueError) as e:
        raise ProtocolError(f"body is not JSON-safe: {e}") from e


def decode(data: bytes, ident: Identity, guard: ReplayGuard | None,
           *, now: float | None = None, max_bytes: int = MAX_DATAGRAM) -> dict:
    """Verify one envelope and return it (with ``mac``). Raises ProtocolError.

    Check order: size, JSON, proto, mesh id, MAC, skew, replay. ``guard=None`` skips replay only.
    ``max_bytes`` (extension) lets HTTP callers allow bodies larger than one datagram.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtocolError("data must be bytes")
    if len(data) > max_bytes:
        raise ProtocolError(f"message too large ({len(data)} > {max_bytes} bytes)")
    try:
        env = json.loads(bytes(data).decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ProtocolError(f"not JSON: {e}") from None
    if not isinstance(env, dict) or set(env) != ENVELOPE_KEYS:
        raise ProtocolError("not an envelope")
    if env["proto"] != PROTO:
        raise ProtocolError(f"unknown proto {env['proto']!r}")
    mesh = env["mesh"]
    if not isinstance(mesh, str) or not _MESH_RE.match(mesh) \
            or not hmac.compare_digest(mesh, ident.mesh_id):
        raise ProtocolError("other mesh")
    mac = env["mac"]
    if not isinstance(mac, str) or not _MAC_RE.match(mac):
        raise ProtocolError("bad MAC")
    unsigned = {k: v for k, v in env.items() if k != "mac"}
    try:
        want = _mac(ident.secret, unsigned)
    except (TypeError, ValueError):
        raise ProtocolError("bad MAC") from None
    if not hmac.compare_digest(mac, want):
        raise ProtocolError("bad MAC")
    # Signed from here on; still check field types before trusting them.
    kind, ts, nonce, body = env["kind"], env["ts"], env["nonce"], env["body"]
    if not isinstance(kind, str) or not _KIND_RE.match(kind) or not isinstance(body, dict) \
            or not isinstance(nonce, str) or not _NONCE_RE.match(nonce) or not _is_num(ts):
        raise ProtocolError("bad envelope fields")
    t_now = time.time() if now is None else float(now)
    if abs(t_now - float(ts)) > MAX_SKEW_S:
        raise ProtocolError(f"clock skew {t_now - float(ts):+.1f}s > {MAX_SKEW_S}s")
    if guard is not None and guard.seen(nonce, float(ts)):
        raise ProtocolError("replay")
    return env
