"""The local HTTP server of apex flow studio (stdlib ``ThreadingHTTPServer``).

Safety:
  - binds 127.0.0.1 only (any other host is refused at start);
  - a random token (``secrets.token_urlsafe(24)``) is in the page URL and is REQUIRED on every API
    call (header ``X-Studio-Token``; constant-time compare);
  - the Host header must be ``127.0.0.1:<port>`` or ``localhost:<port>`` (DNS-rebinding guard);
  - POST/PUT need ``Content-Type: application/json``, a same-origin ``Origin`` when one is sent,
    and a body of at most 64 KiB;
  - the page gets a CSP ``default-src 'none'`` with a fresh per-response nonce for its one inline
    ``<style>`` and one inline ``<script>``; ``connect-src 'self'``; no frames, no forms, no base.

API: GET /api/flow?proxy=  GET /api/live?window=&proxy=  POST /api/test  GET|PUT /api/draft
     POST /api/apply  POST /api/rollback
"""
from __future__ import annotations

import hmac
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import drafts, flow, live

MAX_BODY = 64 * 1024
UI_PATH = Path(__file__).with_name("ui.html")


def _csp(nonce: str) -> str:
    return ("default-src 'none'; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, token: str, paths: flow.Paths | None = None, resolver=None):
        super().__init__(addr, Handler)
        self.token = token
        self.paths = paths
        self.resolver = resolver
        self.lock = threading.Lock()
        self.last_request = time.time()
        self.ui = UI_PATH.read_text(encoding="utf-8")

    @property
    def port(self) -> int:
        return self.server_address[1]

    def get_paths(self) -> flow.Paths:
        return self.paths or flow.resolve_paths()


class Handler(BaseHTTPRequestHandler):
    server: StudioServer
    server_version = "apex-studio"
    sys_version = ""

    def log_message(self, fmt, *args):  # quiet: no request log (URLs carry the token)
        pass

    # ---- helpers ----
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

    def _err(self, status: int, msg: str):
        self._json(status, {"ok": False, "error": msg})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.port
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self, query: dict) -> bool:
        got = self.headers.get("X-Studio-Token") or ""
        if not got and self.command == "GET":
            got = (query.get("token") or [""])[0]
        return bool(got) and hmac.compare_digest(got.encode(), self.server.token.encode())

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server.port
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _body(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise drafts.DraftError("Content-Type must be application/json", 415)
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            raise drafts.DraftError("bad Content-Length", 400) from None
        if n < 0 or n > MAX_BODY:
            raise drafts.DraftError("body too large", 413)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise drafts.DraftError("body is not JSON", 400) from None

    # ---- dispatch ----
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str):
        self.server.last_request = time.time()
        if not self._host_ok():
            return self._err(421, "bad Host header")
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        path = u.path
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if not self._token_ok(q):
            return self._err(403, "missing or bad token")
        if method in ("POST", "PUT", "DELETE") and not self._origin_ok():
            return self._err(403, "bad Origin")
        try:
            if path == "/" and method == "GET":
                return self._page()
            handler = ROUTES.get((method, path))
            if handler is None:
                return self._err(404 if not any(p == path for _, p in ROUTES) else 405, "not found")
            return handler(self, q)
        except drafts.DraftError as e:
            return self._err(e.status, str(e))
        except Exception as e:  # noqa: BLE001 — the page shows an error, the server keeps running
            return self._err(500, f"internal error: {type(e).__name__}")

    def _page(self):
        nonce = secrets.token_urlsafe(18)
        html = self.server.ui.replace("__NONCE__", nonce).encode("utf-8")
        self._send(200, html, "text/html; charset=utf-8", {"Content-Security-Policy": _csp(nonce)})

    # ---- API ----
    def api_flow(self, q):
        proxy = (q.get("proxy") or ["main"])[0]
        self._json(200, flow.build_flow(self.server.get_paths(), proxy))

    def api_live(self, q):
        window = (q.get("window") or ["15m"])[0]
        proxy = (q.get("proxy") or ["main"])[0]
        if window not in live.WINDOWS:
            return self._err(400, "window must be 15m, 1h or 24h")
        if proxy not in flow.PROXIES:
            return self._err(400, "proxy must be main or local")
        self._json(200, live.collect_cached(window, proxy, self.server.get_paths()))

    def api_test(self, q):
        body = self._body()
        prompt = body.get("prompt") if isinstance(body, dict) else None
        self._json(200, flow.test_path(prompt, self.server.get_paths(), resolver=self.server.resolver))

    def api_draft_get(self, q):
        p = self.server.get_paths()
        d = drafts.load_draft(p)
        self._json(200, {"draft": d, "review": drafts.review(p, d)})

    def api_draft_put(self, q):
        body = self._body()
        p = self.server.get_paths()
        with self.server.lock:
            d = drafts.update_draft(p, body)
        self._json(200, {"draft": d, "review": drafts.review(p, d)})

    def api_apply(self, q):
        self._body()
        with self.server.lock:
            out = drafts.apply(self.server.get_paths())
        self._json(200, {"ok": True, **out})

    def api_rollback(self, q):
        self._body()
        with self.server.lock:
            out = drafts.rollback(self.server.get_paths())
        self._json(200, {"ok": True, **out})


ROUTES = {
    ("GET", "/api/flow"): Handler.api_flow,
    ("GET", "/api/live"): Handler.api_live,
    ("POST", "/api/test"): Handler.api_test,
    ("GET", "/api/draft"): Handler.api_draft_get,
    ("PUT", "/api/draft"): Handler.api_draft_put,
    ("POST", "/api/apply"): Handler.api_apply,
    ("POST", "/api/rollback"): Handler.api_rollback,
}


def make_server(port: int = 0, host: str = "127.0.0.1", token: str | None = None,
                paths: flow.Paths | None = None, resolver=None) -> StudioServer:
    if host != "127.0.0.1":
        raise ValueError("apex flow studio binds 127.0.0.1 only")
    return StudioServer((host, int(port)), token or secrets.token_urlsafe(24), paths=paths, resolver=resolver)


def url_of(srv: StudioServer) -> str:
    return f"http://127.0.0.1:{srv.port}/?token={srv.token}"


def serve(port: int = 0, open_browser: bool = True, idle_exit_s: float = 0, emit=print) -> int:
    try:
        srv = make_server(port)
    except OSError as e:
        emit(f"apex-router studio: cannot bind 127.0.0.1:{port} ({e.strerror or e})")
        return 1
    url = url_of(srv)
    emit(url)
    try:
        sys.stdout.flush()   # a pipe or file buffers stdout: the URL must show before serve_forever
    except Exception:  # noqa: BLE001
        pass
    if idle_exit_s and idle_exit_s > 0:
        def watch():
            while True:
                time.sleep(min(30.0, idle_exit_s))
                if time.time() - srv.last_request > idle_exit_s:
                    srv.shutdown()
                    return
        threading.Thread(target=watch, daemon=True).start()
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — the URL is printed; opening is a convenience
            pass
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
