"""LAN mesh HTTP interface (stdlib ``http.server``; see docs/DESIGN-lan-mesh.md, "HTTP").

Routes (GET only, exact paths):

- ``/mesh/v1/card``   -> signed envelope kind ``card``, body = this node card. No auth.
- ``/mesh/v1/peers``  -> signed envelope kind ``peers``, body = ``PeerTable.snapshot()``.
- ``/mesh/v1/leader`` -> signed envelope kind ``leader``, body = the leader Peer dict.
- ``/mesh/v1/data?names=a,b`` -> signed envelope kind ``data``,
  body = ``{"node_id": ..., "data": {name: value}}``. Not-shared names give ``not_shared``.
- ``POST /mesh/v1/infer`` (only with ``models``, i.e. ``mesh up --serve-model``; else 404) ->
  signed envelope kind ``infer``. See "Model calls" in the design doc and ``infer.py``.
- Anything else -> 404. Other methods (and POST to other paths) -> 501.

``/peers``, ``/leader`` and ``/data`` need header ``X-Apex-Mesh`` = signed envelope kind ``auth``
with body ``{"path": "<path?query>", "to": "<server node_id>"}``. The server checks MAC, skew,
kind, path, recipient and replay. Any
failure gives a plain 401 ``{"error": "unauthorized"}`` with no reason (no oracle).

The socket faces the LAN, so: one bind address (never the wildcard), a request body is read only
for an authenticated POST /infer (auth header first, then Content-Length <= 256 KiB, then the body
under its own 10 s deadline), a 5 s socket timeout, an absolute 5 s deadline for the request line
and headers, a cap on parallel connections, no request log, no traceback.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from . import datasets, infer, protocol
from .peers import MODELS_MAX, valid_model
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
BODY_DEADLINE_S = 10.0           # absolute, for the POST body read
MAX_INFER_SLOTS = 4
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_NAME_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")

P_CARD = "/mesh/v1/card"
P_PEERS = "/mesh/v1/peers"
P_LEADER = "/mesh/v1/leader"
P_DATA = "/mesh/v1/data"
P_INFER = infer.INFER_PATH


class _HTTPError(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


def _verify_auth(header: str | None, request_path: str, ident: Identity, *,
                 with_body: bool, now: float | None = None) -> dict | None:
    """The signed ``auth`` envelope if ``header`` is valid for ``request_path``, else None.

    Checks size, MAC + skew (no replay guard), kind, body keys, body.path, body.to. A GET auth
    body is exactly ``{path, to}``; a POST auth body is exactly ``{path, to, body_sha256}``. So a
    header for one method never opens the other. Never raises. The caller does the replay check.
    """
    try:
        if not isinstance(header, str) or not header:
            return None
        raw = header.encode("latin-1")         # http.client headers are latin-1 str
        if len(raw) > MAX_AUTH_BYTES:
            return None
        env = protocol.decode(raw, ident, None, now=now, max_bytes=MAX_AUTH_BYTES)
        if env["kind"] != "auth":
            return None
        body = env["body"]
        want = {"path", "to", "body_sha256"} if with_body else {"path", "to"}
        if set(body) != want or not isinstance(body["path"], str):
            return None
        if body["path"] != request_path or body["to"] != ident.node_id:
            return None
        if with_body and not (isinstance(body["body_sha256"], str)
                              and _SHA256_RE.match(body["body_sha256"])):
            return None
        return env
    except (ProtocolError, UnicodeError, KeyError, TypeError, ValueError):
        return None
    except Exception:  # noqa: BLE001 -- fail closed
        return None


def check_auth(header: str | None, request_path: str, ident: Identity,
               guard: protocol.ReplayGuard, *, now: float | None = None) -> bool:
    """True if ``header`` is a valid GET ``auth`` envelope for ``request_path``. Never raises.

    Order: size, MAC + skew (decode without the guard), kind, body.path, body.to, then replay.
    So a signed message of another kind, path or recipient never takes a slot in the replay
    memory.
    """
    env = _verify_auth(header, request_path, ident, with_body=False, now=now)
    if env is None:
        return False
    try:
        return not guard.seen(env["nonce"], float(env["ts"]))
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

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _not_allowed

    def do_POST(self) -> None:
        self.close_connection = True
        try:
            target = self.path
            if not isinstance(target, str) or not target.startswith(P_INFER):
                raise _HTTPError(501, "not_implemented")    # unchanged v1 behavior
            if target != P_INFER or not self.server.mesh.models:
                raise _HTTPError(404, "not_found")
            self._infer()
        except _HTTPError as e:
            self._try_error(e.status, e.error)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception:  # noqa: BLE001 -- never send a traceback
            self._try_error(500, "internal")

    # -- POST /mesh/v1/infer -----------------------------------------------------------------
    def _content_length(self) -> int:
        if self.headers.get_all("Transfer-Encoding"):
            raise _HTTPError(411, "length_required")    # chunked bodies are not accepted
        vals = self.headers.get_all("Content-Length") or []
        if not vals:
            raise _HTTPError(411, "length_required")
        if len(vals) != 1:
            raise _HTTPError(400, "bad_length")
        v = vals[0].strip()
        if not v.isascii() or not v.isdigit() or len(v) > 12:
            raise _HTTPError(400, "bad_length")
        n = int(v)
        if n > infer.MAX_REQUEST_BYTES:
            raise _HTTPError(413, "too_large")
        return n

    def _read_body(self, n: int) -> bytes:
        """Read exactly ``n`` bytes under an absolute BODY_DEADLINE_S. Raises _HTTPError(408)."""
        timer = threading.Timer(BODY_DEADLINE_S, _shutdown, (self.connection,))
        timer.daemon = True
        timer.start()
        end = time.monotonic() + BODY_DEADLINE_S
        chunks, got = [], 0
        try:
            while got < n:
                try:
                    chunk = self.rfile.read1(min(65536, n - got))
                except (TimeoutError, OSError):
                    chunk = b""
                if not chunk or time.monotonic() > end:
                    raise _HTTPError(408, "body_timeout")
                chunks.append(chunk)
                got += len(chunk)
        finally:
            timer.cancel()
        return b"".join(chunks)

    def _infer(self) -> None:
        mesh = self.server.mesh
        # 1. Auth header BEFORE any body byte is read (MAC, skew, kind, path, to, body_sha256).
        hdrs = self.headers.get_all(AUTH_HEADER) or []
        env = _verify_auth(hdrs[0], P_INFER, mesh.ident, with_body=True) \
            if len(hdrs) == 1 else None
        if env is None:
            raise _HTTPError(401, "unauthorized")
        # 2. Length rules, 3. body under its own deadline.
        n = self._content_length()
        raw = self._read_body(n)
        # 4. Body hash, then replay. A wrong body does not burn the nonce of the real request.
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), env["body"]["body_sha256"]):
            raise _HTTPError(401, "unauthorized")
        if mesh.guard.seen(env["nonce"], float(env["ts"])):
            raise _HTTPError(401, "unauthorized")
        # 5. Request shape, served set, slot.
        try:
            req = infer.parse_request(raw)
        except infer.RequestError as e:
            raise _HTTPError(400, e.code) from None
        if req["model"] not in mesh.models:
            raise _HTTPError(403, "not_served")
        if not mesh.infer_sem.acquire(blocking=False):
            raise _HTTPError(429, "busy")
        try:
            mesh._running(+1)
            t0 = time.monotonic()
            try:
                out = infer.call_upstream(mesh.ollama_url, req)
            except infer.UpstreamError as e:
                raise _HTTPError(502, e.reason) from None
            latency = round(time.monotonic() - t0, 3)
        finally:
            mesh._running(-1)
            mesh.infer_sem.release()
        self._send_signed("infer", {"node_id": mesh.ident.node_id, "model": req["model"],
                                    "content": out["content"], "reasoning": out["reasoning"],
                                    "finish_reason": out["finish_reason"], "usage": out["usage"],
                                    "latency_s": latency})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 16
    mesh: MeshServer

    def server_bind(self) -> None:
        # Skip HTTPServer.server_bind: it does a reverse-DNS getfqdn() on the bind address.
        import socketserver

        from . import netif
        netif.pin_socket(self.socket, str(self.server_address[0]))   # replies stay on the LAN
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
    """HTTP interface of one node. ``shared`` = served dataset names (``datasets.parse_share``).

    ``models`` = model names served on POST /mesh/v1/infer (empty = route off, 404).
    ``infer_slots`` = parallel upstream calls (1..4); a full set gives 429 at once.
    ``ollama`` = upstream base URL (default: $APEX_MESH_OLLAMA or http://127.0.0.1:11434).
    """

    def __init__(self, ident: Identity, table: PeerTable, card_fn: Callable[[], dict], *,
                 host: str, port: int = DEFAULT_HTTP_PORT,
                 shared: frozenset = frozenset(), models: tuple | list | frozenset = (),
                 infer_slots: int = 1, ollama: str | None = None) -> None:
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
        models = tuple(dict.fromkeys(models or ()))
        if len(models) > MODELS_MAX or not all(valid_model(m) for m in models):
            raise ValueError(f"models: at most {MODELS_MAX} names of [A-Za-z0-9._:/-], <= 128 chars")
        if isinstance(infer_slots, bool) or not isinstance(infer_slots, int) \
                or not 1 <= infer_slots <= MAX_INFER_SLOTS:
            raise ValueError(f"infer_slots must be 1..{MAX_INFER_SLOTS}")
        self.models = frozenset(models)
        self.ollama_url = infer.ollama_url(ollama) if models else ""
        self.infer_sem = threading.BoundedSemaphore(infer_slots)
        self._run_lock = threading.Lock()
        self._infer_running = 0
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    def _running(self, delta: int) -> None:
        with self._run_lock:
            self._infer_running += delta

    @property
    def infer_running(self) -> int:
        """Number of upstream model calls that run now (added to the card ``load``)."""
        with self._run_lock:
            return self._infer_running

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
