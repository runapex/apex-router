"""LAN mesh beacon: UDP multicast announce + listen (see docs/DESIGN-lan-mesh.md).

Sockets (all bound to ONE address, never the wildcard):

- unicast socket on ``(iface_ip, port)``: receives unicast datagrams and sends all datagrams.
  It sends to the multicast group (TTL 1, ``IP_MULTICAST_IF = iface_ip``) and to each target.
- multicast socket on ``(group, port)``, joined on ``iface_ip``: receives group datagrams.
  If the OS refuses the bind or the join, the beacon runs unicast-only (``multicast`` = False).

One daemon thread announces every ``interval_s`` and receives with a 1 s timeout. Every datagram
is hostile until ``protocol.decode`` passes: a bad datagram is dropped silently and counted by
reason in ``stats()["drops"]``. The thread never raises.
"""
from __future__ import annotations

import ipaddress
import math
import re
import select
import socket
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import netif, protocol
from .protocol import (
    BEACON_INTERVAL_S,
    MAX_DATAGRAM,
    MCAST_GROUP,
    MCAST_PORT,
    ProtocolError,
)

if TYPE_CHECKING:
    from .identity import Identity
    from .peers import PeerTable

RECV_TIMEOUT_S = 1.0
JOIN_TIMEOUT_S = 3.0
DRAIN_MAX = 256                  # datagrams per socket per loop: keep announces on time
MAX_TARGETS = 64
_NODE_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")

# ProtocolError message prefix -> drop reason (messages from protocol.decode).
_REASONS = (
    ("message too large", "too_large"),
    ("data must be bytes", "malformed"),
    ("not JSON", "malformed"),
    ("not an envelope", "malformed"),
    ("unknown proto", "malformed"),
    ("other mesh", "other_mesh"),
    ("bad MAC", "bad_mac"),
    ("bad envelope fields", "bad_fields"),
    ("clock skew", "skew"),
    ("replay", "replay"),
)


def drop_reason(err: ProtocolError) -> str:
    msg = str(err)
    for prefix, reason in _REASONS:
        if msg.startswith(prefix):
            return reason
    return "invalid"


def _set_reuse(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass


class Beacon:
    """Announce this node and keep ``table`` filled from the announces of other nodes."""

    def __init__(self, ident: Identity, table: PeerTable, card_fn: Callable[[], dict], *,
                 group: str = MCAST_GROUP, port: int = MCAST_PORT, iface_ip: str,
                 interval_s: float = BEACON_INTERVAL_S,
                 targets: list[tuple[str, int]] | None = None) -> None:
        try:
            g = ipaddress.ip_address(group)
        except (TypeError, ValueError):
            raise ValueError(f"group {group!r} is not an IP address") from None
        if g.version != 4 or not g.is_multicast:
            raise ValueError(f"group {group!r} is not an IPv4 multicast address")
        try:
            iface = ipaddress.ip_address(iface_ip)
        except (TypeError, ValueError):
            raise ValueError(f"iface_ip {iface_ip!r} is not an IP address") from None
        if iface.is_unspecified or iface.is_multicast:
            raise ValueError(f"iface_ip {iface_ip!r} must be one unicast address")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError(f"bad port {port!r}")
        if isinstance(interval_s, bool) or not isinstance(interval_s, (int, float)) \
                or not math.isfinite(interval_s) or interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        tgts = list(targets or [])
        if len(tgts) > MAX_TARGETS:
            raise ValueError(f"at most {MAX_TARGETS} targets")
        for t in tgts:
            if not (isinstance(t, tuple) and len(t) == 2 and isinstance(t[0], str) and t[0]
                    and isinstance(t[1], int) and not isinstance(t[1], bool)
                    and 1 <= t[1] <= 65535):
                raise ValueError(f"bad target {t!r}; want (host, port)")
        self.ident = ident
        self.table = table
        self.card_fn = card_fn
        self.group = str(g)
        self.port = port
        self.iface_ip = str(iface)
        self.interval_s = float(interval_s)
        self.targets = tgts
        self._family = socket.AF_INET if iface.version == 4 else socket.AF_INET6
        self._guard = protocol.ReplayGuard()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ucast: socket.socket | None = None
        self._mcast: socket.socket | None = None
        self._dests: list[Any] = []
        self._mcast_send = False
        self._lock = threading.Lock()
        self._stats: dict[str, Any] = {"rx": 0, "tx": 0, "send_errors": 0, "own": 0,
                                       "announces": 0, "byes": 0, "drops": {}}
        self.multicast_error: str | None = None

    # -- stats -------------------------------------------------------------------------------
    def _count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._stats[key] += n

    def _drop(self, reason: str) -> None:
        with self._lock:
            d = self._stats["drops"]
            d[reason] = d.get(reason, 0) + 1

    def stats(self) -> dict:
        """Counters: rx, tx, send_errors, own, announces, byes, drops {reason: n}."""
        with self._lock:
            return {**self._stats, "drops": dict(self._stats["drops"])}

    @property
    def multicast(self) -> bool:
        """True when the multicast socket joined the group."""
        return self._mcast is not None

    @property
    def address(self) -> tuple[str, int]:
        """Bound unicast address (the real port when ``port=0``)."""
        if self._ucast is None:
            raise RuntimeError("beacon not started")
        name = self._ucast.getsockname()
        return name[0], name[1]

    # -- sockets -----------------------------------------------------------------------------
    def _open(self) -> None:
        info = socket.getaddrinfo(self.iface_ip, self.port, self._family, socket.SOCK_DGRAM,
                                  0, socket.AI_NUMERICHOST)
        bind_addr = info[0][4]
        u = socket.socket(self._family, socket.SOCK_DGRAM)
        try:
            _set_reuse(u)
            u.bind(bind_addr)
            netif.pin_socket(u, self.iface_ip)       # stay on the LAN when a VPN routes its subnet
            u.setblocking(False)
            if self._family == socket.AF_INET:
                try:
                    u.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                    u.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
                    u.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                 socket.inet_aton(self.iface_ip))
                    self._mcast_send = True
                except OSError as e:
                    self.multicast_error = f"multicast send setup: {e}"
        except BaseException:
            u.close()
            raise
        self._ucast = u
        port = u.getsockname()[1]
        if self._family != socket.AF_INET:
            self.multicast_error = "IPv6 interface: unicast only"
        else:
            m = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                _set_reuse(m)
                m.bind((self.group, port))
                mreq = socket.inet_aton(self.group) + socket.inet_aton(self.iface_ip)
                m.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                m.setblocking(False)
                self._mcast = m
            except OSError as e:
                m.close()
                self.multicast_error = f"multicast join: {e}"
        dests: list[Any] = []
        if self._mcast_send:
            dests.append((self.group, port))
        for host, tport in self.targets:
            try:
                ai = socket.getaddrinfo(host, tport, self._family, socket.SOCK_DGRAM)
                dests.append(ai[0][4])
            except (OSError, UnicodeError):
                self._count("send_errors")
        self._dests = dests

    def _close(self) -> None:
        for s in (self._mcast, self._ucast):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    # -- send --------------------------------------------------------------------------------
    def _send(self, data: bytes) -> None:
        u = self._ucast
        if u is None:
            return
        for dest in self._dests:
            try:
                u.sendto(data, dest)
                self._count("tx")
            except OSError:
                self._count("send_errors")

    def _announce(self) -> None:
        try:
            card = self.card_fn()
            data = protocol.encode("announce", card, self.ident)
        except Exception:  # noqa: BLE001 -- a bad card_fn must not stop the beacon
            self._count("send_errors")
            return
        if len(data) > MAX_DATAGRAM:
            self._count("send_errors")
            return
        self._send(data)

    def _bye(self) -> None:
        try:
            data = protocol.encode("bye", {"node_id": self.ident.node_id}, self.ident)
        except ProtocolError:
            return
        self._send(data)

    # -- receive -----------------------------------------------------------------------------
    def handle(self, data: bytes, *, now: float | None = None) -> None:
        """Process one datagram. Never raises; a bad datagram counts as a drop."""
        try:
            self._count("rx")
            if len(data) > MAX_DATAGRAM:
                self._drop("too_large")
                return
            try:
                env = protocol.decode(data, self.ident, self._guard, now=now)
            except ProtocolError as e:
                self._drop(drop_reason(e))
                return
            kind, body = env["kind"], env["body"]
            node_id = body.get("node_id")
            if not isinstance(node_id, str) or not _NODE_ID_RE.match(node_id):
                self._drop("bad_body")
                return
            if node_id == self.ident.node_id:
                self._count("own")
                return
            if kind == "announce":
                if self.table.update(body, now=now) is None:
                    self._drop("bad_card")
                    return
                self._count("announces")
            elif kind == "bye":
                self.table.remove(node_id)
                self._count("byes")
            else:
                self._drop("bad_kind")
        except Exception:  # noqa: BLE001 -- hostile input never stops the thread
            self._drop("error")

    def _drain(self, sock: socket.socket) -> None:
        for _ in range(DRAIN_MAX):
            try:
                data, _addr = sock.recvfrom(MAX_DATAGRAM + 1)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:          # e.g. Windows WSAEMSGSIZE on an oversized datagram
                self._drop("recv_error")
                return
            self.handle(data)

    def _run(self) -> None:
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now >= next_at:
                    self._announce()
                    next_at = now + self.interval_s
                socks = [s for s in (self._ucast, self._mcast) if s is not None]
                timeout = max(0.0, min(RECV_TIMEOUT_S, next_at - time.monotonic()))
                ready, _, _ = select.select(socks, [], [], timeout)
                for s in ready:
                    self._drain(s)
            except (OSError, ValueError):
                if self._stop.is_set():
                    return
                self._drop("loop_error")
                self._stop.wait(0.1)
            except Exception:  # noqa: BLE001
                self._drop("loop_error")
                self._stop.wait(0.1)

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        """Open the sockets (raises OSError if the unicast bind fails) and start the thread."""
        if self._thread is not None:
            raise RuntimeError("beacon already started")
        self._open()
        self._thread = threading.Thread(target=self._run, name="apex-mesh-beacon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the thread, send ``bye``, close the sockets. Safe to call twice."""
        t = self._thread
        if t is None or self._stop.is_set():
            return
        self._stop.set()
        t.join(timeout=RECV_TIMEOUT_S + JOIN_TIMEOUT_S)
        self._bye()
        self._close()
