"""LAN mesh HTTP interface (stdlib ``http.server``; see docs/DESIGN-lan-mesh.md, "HTTP").

Routes (GET only, exact paths):

- ``/mesh/v1/card``   -> signed envelope kind ``card``, body = this node card. No auth.
- ``/mesh/v1/peers``  -> signed envelope kind ``peers``, body = ``PeerTable.snapshot()``.
- ``/mesh/v1/leader`` -> signed envelope kind ``leader``, body = the leader Peer dict.
- ``/mesh/v1/data?names=a,b`` -> signed envelope kind ``data``,
  body = ``{"node_id": ..., "data": {name: value}}``. Not-shared names give ``not_shared``.
- Anything else -> 404. Other methods -> 501.

``/peers``, ``/leader`` and ``/data`` need header ``X-Apex-Mesh`` = signed envelope kind ``auth``
with body ``{"path": "<path?query>", "to": "<server node_id>"}``. The server checks MAC, skew,
kind, path, recipient and replay. Any
failure gives a plain 401 ``{"error": "unauthorized"}`` with no reason (no oracle).

The socket faces the LAN, so: one bind address (never the wildcard), a request body is never
read, a 5 s socket timeout, an absolute 5 s deadline per connection, a cap on parallel
connections, no request log, no traceback.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from . import datasets, protocol
from .protocol import (  # noqa: F401 -- re-export
    DEFAULT_HTTP_PORT,
    ProtocolError,
    auth_header,
)

if TYPE_CHECKING:
    from .identity import Identity
    from .peers import PeerTable

AUTH_HEADER = "X-Apex-Mesh"
REQUEST_TIMEOUT_S = 5.0
REQUEST_DEADLINE_S = 5.0         # absolute, per connection: stops a slowloris client
MAX_AUTH_BYTES = 4096            # one auth envelope is ~250 bytes
MAX_PATH_BYTES = 2048
MAX_NAMES = 16
MAX_CONNECTIONS = 32
_NAME_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")

P_CARD = "/mesh/v1/card"
P_PEERS = "/mesh/v1/peers"
P_LEADER = "/mesh/v1/leader"
P_DATA = "/mesh/v1/data"


class _HTTPError(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


def check_auth(header: str | None, request_path: str, ident: Identity,
               guard: protocol.ReplayGuard, *, now: float | None = None) -> bool:
    """True if ``header`` is a valid ``auth`` envelope for ``request_path``. Never raises.

    Order: size, MAC + skew (decode without the guard), kind, body.path, body.to, then replay.
    So a signed message of another kind, path or recipient never takes a slot in the replay
    memory.
    """
    try:
        if not isinstance(header, str) or not header:
            return False
        raw = header.encode("latin-1")         # http.client headers are latin-1 str
        if len(raw) > MAX_AUTH_BYTES:
            return False
        env = protocol.decode(raw, ident, None, now=now, max_bytes=MAX_AUTH_BYTES)
        if env["kind"] != "auth":
            return False
        body = env["body"]
        if set(body) != {"path", "to"} or not isinstance(body["path"], str):
            return False
        if body["path"] != request_path or body["to"] != ident.node_id:
            return False
        return not guard.seen(env["nonce"], float(env["ts"]))
    except (ProtocolError, UnicodeError, KeyError, TypeError, ValueError):
        return False
    except Exception:  # noqa: BLE001 -- fail closed
        return False


def parse_names(query: str) -> list[str]:
    """Parse ``names=a,b`` (one ``names`` field). Raises _HTTPError(400)."""
    try:
        qs = parse_qs(query, keep_blank_values=True, strict_parsing=False,
                      max_num_fields=MAX_NAMES + 4, separator="&")
    except ValueError:
        raise _HTTPError(400, "bad_query") from None
    if set(qs) - {"names"}:
        raise _HTTPError(400, "bad_query")
    vals = qs.get("names", [])
    if len(vals) != 1:
        raise _HTTPError(400, "bad_query")
    names = list(dict.fromkeys(n.strip() for n in vals[0].split(",") if n.strip()))
    if not names:
        raise _HTTPError(400, "no_names")
    if len(names) > MAX_NAMES:
        raise _HTTPError(400, "too_many_names")
    if not all(_NAME_RE.match(n) for n in names):
        raise _HTTPError(400, "bad_name")
    return names


class _Handler(BaseHTTPRequestHandler):
    server: _Server                      # type: ignore[assignment]
    timeout = REQUEST_TIMEOUT_S
    server_version = "apex-mesh/1"
    sys_version = ""
    error_content_type = "application/json"
    error_message_format = '{"error": "http_%(code)d"}'

    deadline: threading.Timer | None = None   # set by _Server.finish_request

    def parse_request(self) -> bool:
        # The absolute deadline guards only the pre-auth phase (request line + headers). Once the
        # headers are in, cancel it, so slow dataset work does not lose a valid response.
        ok = super().parse_request()
        if self.deadline is not None:
            self.deadline.cancel()
        return ok

    # -- silence -----------------------------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:
        return

    def version_string(self) -> str:
        return self.server_version

    # -- responses ---------------------------------------------------------------------------
    def _send_json(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _send_error_json(self, status: int, error: str) -> None:
        self._send_json(status, json.dumps({"error": error}).encode("ascii"))

    def _send_signed(self, kind: str, body: dict) -> None:
        self._send_json(200, protocol.encode(kind, body, self.server.mesh.ident))

    # -- routing -----------------------------------------------------------------------------
    def do_GET(self) -> None:
        self.close_connection = True
        try:
            self._route()
        except _HTTPError as e:
            self._try_error(e.status, e.error)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception:  # noqa: BLE001 -- never send a traceback
            self._try_error(500, "internal")

    def _try_error(self, status: int, error: str) -> None:
        try:
            self._send_error_json(status, error)
        except Exception:  # noqa: BLE001
            self.close_connection = True

    def _route(self) -> None:
        mesh = self.server.mesh
        target = self.path
        if not isinstance(target, str) or not target.startswith("/") \
                or len(target.encode("utf-8", "surrogateescape")) > MAX_PATH_BYTES \
                or "#" in target:
            raise _HTTPError(404, "not_found")
        path, _, query = target.partition("?")
        if path == P_CARD:
            if query:
                raise _HTTPError(404, "not_found")
            self._send_signed("card", mesh.card())
            return
        if path not in (P_PEERS, P_LEADER, P_DATA):
            raise _HTTPError(404, "not_found")
        hdrs = self.headers.get_all(AUTH_HEADER) or []
        if len(hdrs) != 1 or not check_auth(hdrs[0], target, mesh.ident, mesh.guard):
            raise _HTTPError(401, "unauthorized")
        if path == P_PEERS:
            if query:
                raise _HTTPError(400, "bad_query")
            self._send_signed("peers", mesh.table.snapshot())
        elif path == P_LEADER:
            if query:
                raise _HTTPError(400, "bad_query")
            self._send_signed("leader", mesh.table.leader().to_dict())
        else:
            names = parse_names(query)
            data = datasets.read(names, mesh.shared)
            self._send_signed("data", {"node_id": mesh.ident.node_id, "data": data})

    def _not_allowed(self) -> None:
        self._try_error(501, "not_implemented")

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _not_allowed


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 16
    mesh: MeshServer

    def server_bind(self) -> None:
        # Skip HTTPServer.server_bind: it does a reverse-DNS getfqdn() on the bind address.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.mesh.slots.acquire(blocking=False):
            self.shutdown_request(request)      # too many open connections: drop
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.mesh.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.mesh.slots.release()

    def finish_request(self, request: Any, client_address: Any) -> None:
        try:
            request.settimeout(REQUEST_TIMEOUT_S)
        except OSError:
            return
        # The timeout is per read; a client that sends 1 byte per 4 s never hits it. So also
        # shut the socket down at an absolute deadline: the blocked read returns, the handler ends.
        timer = threading.Timer(REQUEST_DEADLINE_S, _shutdown, (request,))
        timer.daemon = True
        handler = type("_TimedHandler", (self.RequestHandlerClass,), {"deadline": timer})
        timer.start()
        try:
            handler(request, client_address, self)
        finally:
            timer.cancel()

    def handle_error(self, request: Any, client_address: Any) -> None:
        return                                  # no traceback on stderr


def _shutdown(sock: Any) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _Server6(_Server):
    address_family = socket.AF_INET6


class MeshServer:
    """HTTP interface of one node. ``shared`` = served dataset names (``datasets.parse_share``)."""

    def __init__(self, ident: Identity, table: PeerTable, card_fn: Callable[[], dict], *,
                 host: str, port: int = DEFAULT_HTTP_PORT,
                 shared: frozenset = frozenset()) -> None:
        try:
            addr = ipaddress.ip_address(host)
        except (TypeError, ValueError):
            raise ValueError(f"host {host!r} is not an IP address") from None
        if addr.is_unspecified or addr.is_multicast:
            raise ValueError(f"host {host!r} must be one unicast address, not all interfaces")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError(f"bad port {port!r}")
        self.ident = ident
        self.table = table
        self.card_fn = card_fn
        self.host = str(addr)
        self.port = port
        self.shared = frozenset(n for n in shared if n in datasets.ALLOWLIST)
        self.guard = protocol.ReplayGuard()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    def card(self) -> dict:
        card = self.card_fn()
        if not isinstance(card, dict):
            raise TypeError("card_fn must return a dict")
        return card

    @property
    def address(self) -> tuple[str, int]:
        """Bound (host, port); the real port when ``port=0``."""
        if self._httpd is None:
            raise RuntimeError("server not started")
        a = self._httpd.server_address
        return str(a[0]), int(a[1])

    def start(self) -> None:
        """Bind and serve in a daemon thread. Raises OSError if the bind fails."""
        if self._httpd is not None:
            raise RuntimeError("server already started")
        cls = _Server6 if ipaddress.ip_address(self.host).version == 6 else _Server
        info = socket.getaddrinfo(self.host, self.port, cls.address_family, socket.SOCK_STREAM,
                                  0, socket.AI_NUMERICHOST)
        httpd = cls(info[0][4], _Handler, bind_and_activate=False)
        httpd.mesh = self
        try:
            httpd.server_bind()
            httpd.server_activate()
        except BaseException:
            httpd.server_close()
            raise
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="apex-mesh-http", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and close the socket. Safe to call twice."""
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=REQUEST_TIMEOUT_S)
            self._thread = None
