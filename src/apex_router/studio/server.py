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
     POST /api/apply  POST /api/rollback  GET /api/ping (token check + pid, for single instance)
     v2 (docs/design/studio-v2/API.md; token in the header only, never in the URL):
     GET /api/recent?n=  GET /api/tokens?window=  GET /api/experiments  GET /api/stream (SSE)
     GET /api/records (throughput, latency, disk, pipelines; launchd via an injectable runner)
     GET /api/workflows?window=  POST /api/workflows/join  POST /api/workflows/split (studio/workflows.py)

Streams: one handler thread per stream, at most ``stream_max`` (4) open; the 5th gets 429. A stream
ends when the client goes away (a write error) or the server shuts down. ``last_request`` is set
once per API call, so an open stream does not keep the studio alive past ``--idle-exit``.

Single instance: a started studio writes ``<router home>/studio/instance.json`` ``{pid, port,
token}`` (mode 0600, atomic) and deletes it on a clean exit. ``apex-router studio --open <target>``
reuses the studio that file names when its pid is alive AND it answers ``/api/ping`` with that
token and pid; else it starts a new one and replaces the file. A lock file
(``instance.lock``, ``flock``) makes the check + start one step, so two clicks start one studio.
"""
from __future__ import annotations

import hmac
import http.client
import contextlib
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import restart as restart_mod
from . import drafts, experiments, flow, live, records, traffic, workflows

try:
    import fcntl
except ImportError:  # pragma: no cover — not POSIX: no lock, the check still runs
    fcntl = None

MAX_BODY = 64 * 1024
PING_TIMEOUT_S = 1.0
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
HEADER_TOKEN_ONLY = frozenset({"/api/recent", "/api/tokens", "/api/experiments", "/api/stream", "/api/records",
                               "/api/workflows", "/api/workflows/join", "/api/workflows/split"})
STREAM_MAX = 4
STREAM_WRITE_TIMEOUT_S = 10.0
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
        self.waiter = restart_mod.Waiter()
        self.restart_run = restart_mod._run      # tests replace these (never the real launchctl)
        self.restart_fetch = restart_mod._fetch
        self.stats_fetch = restart_mod._fetch    # the stream snapshot's proxy state (tests replace it)
        self.records_run = records._run          # `launchctl print` for /api/records (tests replace it)
        self.closing = threading.Event()         # set at shutdown: open streams end
        self.stream_max = STREAM_MAX
        self.streams_open = 0
        self.stream_poll_s, self.stream_snap_s, self.stream_hb_s = 0.5, 2.0, 15.0
        self.stream_wf_s = 2.0                   # workflow diff per stream (events: <= 1 per node per s)
        self._snap_lock = threading.Lock()
        self._snap = (0.0, None)

    def shutdown(self):
        self.closing.set()
        super().shutdown()

    def server_close(self):
        self.closing.set()
        super().server_close()

    def stream_snapshot(self) -> dict:
        """One snapshot per SNAPSHOT_TTL_S, shared by every open stream (one /stats call, one read)."""
        with self._snap_lock:
            t, d = self._snap
            now = time.time()
            if d is None or now - t >= traffic.SNAPSHOT_TTL_S:
                p = self.get_paths()
                d = traffic.snapshot(p, self.stats_fetch, restart_mod.proxy_port(p, "main"), now)
                self._snap = (now, d)
            return d

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

    def _token_ok(self, query: dict, path: str = "") -> bool:
        got = self.headers.get("X-Studio-Token") or ""
        if not got and self.command in ("GET", "HEAD") and path not in HEADER_TOKEN_ONLY:
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
        if not self._token_ok(q, path):
            return self._err(403, "missing or bad token")
        if not self._origin_ok():             # every method: a foreign page gets nothing, not even GETs
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
        except restart_mod.RestartError as e:
            return self._err(e.status, str(e))
        except workflows.WorkflowError as e:
            return self._err(e.status, str(e))
        except Exception as e:  # noqa: BLE001 — the page shows an error, the server keeps running
            return self._err(500, f"internal error: {type(e).__name__}")

    def _page(self):
        nonce = secrets.token_urlsafe(18)
        html = self.server.ui.replace("__NONCE__", nonce).encode("utf-8")
        self._send(200, html, "text/html; charset=utf-8", {"Content-Security-Policy": _csp(nonce)})

    # ---- API ----
    def api_ping(self, q):
        self._json(200, {"ok": True, "app": "apex-studio", "pid": os.getpid()})

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


    # ---- restart-class apply: plist + restart only when the proxy is idle ----
    def _proxy_q(self, q, body=None):
        proxy = (body or {}).get("proxy") if isinstance(body, dict) and body.get("proxy") else (q.get("proxy") or ["main"])[0]
        if proxy not in flow.PROXIES:
            raise drafts.DraftError("proxy must be main or local", 400)
        return proxy

    def api_restart_get(self, q):
        p = self.server.get_paths()
        proxy = self._proxy_q(q)
        idle = float((q.get("idle_s") or [restart_mod.DEFAULT_IDLE_S])[0])
        self._json(200, {"proxy": proxy, "label": flow.PROXIES[proxy]["label"],
                         "gate": restart_mod.gate(p, proxy, idle, self.server.restart_fetch),
                         "changes": drafts.restart_changes(p, proxy),
                         "job": self.server.waiter.status(proxy)})

    def api_restart_post(self, q):
        body = self._body()
        if not isinstance(body, dict):
            raise drafts.DraftError("body must be a JSON object", 400)
        p = self.server.get_paths()
        proxy = self._proxy_q(q, body)
        label = flow.PROXIES[proxy]["label"]
        if body.get("confirm") != f"restart {label}":          # no restart by accident or by a stray call
            raise drafts.DraftError(f'confirm must be "restart {label}"', 400)
        try:
            idle = max(restart_mod.MIN_IDLE_S, float(body.get("idle_s", restart_mod.DEFAULT_IDLE_S)))
            max_wait = min(4 * 3600.0, max(0.0, float(body.get("max_wait_s", restart_mod.DEFAULT_MAX_WAIT_S))))
        except (TypeError, ValueError):
            raise drafts.DraftError("idle_s and max_wait_s must be numbers", 400) from None
        with self.server.lock:
            changes = drafts.restart_changes(p, proxy)          # validated values from the draft
        env = {c["name"]: c["value"] for c in changes}
        on_done = lambda: drafts.clear_restart_changes(p, proxy, [c["key"] for c in changes])  # noqa: E731
        if body.get("wait"):
            st = self.server.waiter.start(p, proxy, env, idle, max_wait, self.server.restart_run,
                                          self.server.restart_fetch, on_done=on_done)
            return self._json(202, {"ok": True, "job": st})
        r = restart_mod.restart(p, proxy, env, idle, self.server.restart_run, self.server.restart_fetch)
        on_done()
        self._json(200, {"ok": True, "result": r})

    def api_restart_delete(self, q):
        proxy = self._proxy_q(q)
        self._json(200, {"ok": True, "job": self.server.waiter.cancel(proxy)})

    # ---- v2 (Metro): requests, tokens, experiments, stream ----
    def api_recent(self, q):
        raw = (q.get("n") or ["50"])[0]
        if not re.fullmatch(r"[0-9]{1,3}", raw) or not 1 <= int(raw) <= traffic.RECENT_MAX:
            return self._err(400, f"n must be 1..{traffic.RECENT_MAX}")
        self._json(200, traffic.recent(self.server.get_paths(), int(raw)))

    def api_tokens(self, q):
        window = (q.get("window") or ["15m"])[0]
        if window not in traffic.TOKEN_WINDOWS:
            return self._err(400, "window must be 15m, 1h or 24h")
        self._json(200, traffic.tokens_cached(self.server.get_paths(), window))

    def api_experiments(self, q):
        self._json(200, experiments.collect(self.server.get_paths()))

    def api_records(self, q):
        self._json(200, records.collect_cached(self.server.get_paths(), self.server.records_run))

    def api_workflows(self, q):
        window = (q.get("window") or ["24h"])[0]
        if window not in workflows.WINDOWS:
            return self._err(400, "window must be 24h or 7d")
        self._json(200, workflows.collect_cached(self.server.get_paths(), window))

    def api_workflows_join(self, q):
        body = self._body()
        if not isinstance(body, dict):
            raise drafts.DraftError("body must be a JSON object", 400)
        with self.server.lock:
            workflows.store_for(self.server.get_paths()).join(body.get("sessions"))
        self._json(200, {"ok": True})

    def api_workflows_split(self, q):
        body = self._body()
        if not isinstance(body, dict):
            raise drafts.DraftError("body must be a JSON object", 400)
        with self.server.lock:
            workflows.store_for(self.server.get_paths()).split(body.get("session"))
        self._json(200, {"ok": True})

    def api_stream(self, q):
        srv = self.server
        with srv.lock:
            if srv.streams_open >= srv.stream_max:
                return self._err(429, f"at most {srv.stream_max} open streams")
            srv.streams_open += 1
        try:
            self.close_connection = True
            self.send_response(200)
            for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store"),
                         ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                         ("X-Accel-Buffering", "no"), ("Connection", "close")):
                self.send_header(k, v)
            self.end_headers()
            if self.command == "HEAD":
                return None
            self.connection.settimeout(STREAM_WRITE_TIMEOUT_S)    # a client that stops reading ends it

            def write(b: bytes):
                self.wfile.write(b)
                self.wfile.flush()
            try:
                paths = srv.get_paths()
                traffic.stream(write, paths, srv.closing, srv.stream_snapshot,
                               srv.stream_poll_s, srv.stream_snap_s, srv.stream_hb_s,
                               wf_fn=lambda: workflows.peek_cached(paths, "24h"), wf_s=srv.stream_wf_s)
            except OSError:
                pass                                              # the client went away
            except Exception:  # noqa: BLE001 — headers are sent: end the stream, the client reconnects
                pass
        finally:
            with srv.lock:
                srv.streams_open -= 1
        return None


ROUTES = {
    ("GET", "/api/ping"): Handler.api_ping,
    ("GET", "/api/restart"): Handler.api_restart_get,
    ("POST", "/api/restart"): Handler.api_restart_post,
    ("DELETE", "/api/restart"): Handler.api_restart_delete,
    ("GET", "/api/flow"): Handler.api_flow,
    ("GET", "/api/live"): Handler.api_live,
    ("POST", "/api/test"): Handler.api_test,
    ("GET", "/api/draft"): Handler.api_draft_get,
    ("PUT", "/api/draft"): Handler.api_draft_put,
    ("POST", "/api/apply"): Handler.api_apply,
    ("POST", "/api/rollback"): Handler.api_rollback,
    ("GET", "/api/recent"): Handler.api_recent,
    ("GET", "/api/tokens"): Handler.api_tokens,
    ("GET", "/api/experiments"): Handler.api_experiments,
    ("GET", "/api/records"): Handler.api_records,
    ("GET", "/api/stream"): Handler.api_stream,
    ("GET", "/api/workflows"): Handler.api_workflows,
    ("POST", "/api/workflows/join"): Handler.api_workflows_join,
    ("POST", "/api/workflows/split"): Handler.api_workflows_split,
}


def make_server(port: int = 0, host: str = "127.0.0.1", token: str | None = None,
                paths: flow.Paths | None = None, resolver=None) -> StudioServer:
    if host != "127.0.0.1":
        raise ValueError("apex flow studio binds 127.0.0.1 only")
    return StudioServer((host, int(port)), token or secrets.token_urlsafe(24), paths=paths, resolver=resolver)


def url_of(srv: StudioServer) -> str:
    return f"http://127.0.0.1:{srv.port}/?token={srv.token}"


# ---- single instance -----------------------------------------------------------------------

def instance_path(paths: flow.Paths | None = None) -> Path:
    return (paths or flow.resolve_paths()).studio / "instance.json"


def read_instance(path: Path) -> dict | None:
    """``{pid, port, token}`` from the instance file, or None when missing or malformed."""
    d = flow.read_json(path)
    if not isinstance(d, dict):
        return None
    pid, port, token = d.get("pid"), d.get("port"), d.get("token")
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
        return None
    if not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536):
        return None
    if not (isinstance(token, str) and TOKEN_RE.fullmatch(token)):
        return None
    return {"pid": pid, "port": port, "token": token}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:      # exists, another user: it cannot be our studio, but it is alive
        return True
    except OSError:
        return False
    return True


def ping(port: int, token: str, timeout: float = PING_TIMEOUT_S):
    """The ``/api/ping`` body of a studio on 127.0.0.1:port with this token, or None."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            c.request("GET", "/api/ping", headers={"Host": f"127.0.0.1:{port}", "X-Studio-Token": token})
            r = c.getresponse()
            body = r.read(4096)
            if r.status != 200:
                return None
        finally:
            c.close()
        d = json.loads(body)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return d if isinstance(d, dict) and d.get("ok") is True and d.get("app") == "apex-studio" else None


def live_instance(path: Path) -> dict | None:
    """The instance the file names when its pid is alive AND it answers with that token and pid."""
    inst = read_instance(path)
    if not inst or not pid_alive(inst["pid"]):
        return None
    d = ping(inst["port"], inst["token"])
    if not d or d.get("pid") != inst["pid"]:
        return None
    return inst


def write_instance(path: Path, srv: StudioServer) -> None:
    """Atomic write, mode 0600 (the file holds the token)."""
    drafts._atomic_write(path, json.dumps({"pid": os.getpid(), "port": srv.port, "token": srv.token}) + "\n",
                         0o600)


def clear_instance(path: Path, srv: StudioServer) -> None:
    """Delete the instance file — only when it still names this server (a newer studio may own it)."""
    inst = read_instance(path)
    if inst and inst["token"] == srv.token and inst["port"] == srv.port:
        try:
            path.unlink()
        except OSError:
            pass


class _instance_lock:
    """``flock`` on ``instance.lock`` next to the instance file: check + start is one step."""

    def __init__(self, path: Path):
        self.path = path.with_name("instance.lock")
        self.fd = None

    def __enter__(self):
        if fcntl is None:
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except OSError:
            self._close()
        return self

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)   # closing drops the lock
            except OSError:
                pass
            self.fd = None

    def __exit__(self, *exc):
        self._close()
        return False


NODE_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z0-9]+)?")


def target_hash(target: str | None) -> str | None:
    """The page hash for an ``--open`` target, or None when it is not allowed. Only fixed shapes:
    '' (no hash), ``review``, ``branches``, ``node:<id>`` (or ``#review``, ``#branches``,
    ``#node=<id>``) where <id> is a node of the flow. No free text (no ``#test=``)."""
    if target is None or target == "":
        return ""
    t = target[1:] if target.startswith("#") else target
    if t in ("review", "branches"):
        return "#" + t
    m = re.fullmatch(r"node[:=](.+)", t)
    if m and NODE_ID_RE.fullmatch(m.group(1)) and m.group(1) in {n["id"] for n in flow.NODES}:
        return "#node=" + m.group(1)
    return None


def _open_url(url: str, opener=None) -> None:
    try:
        if opener is None:
            import webbrowser
            opener = webbrowser.open
        opener(url)
    except Exception:  # noqa: BLE001 — the URL is printed; opening is a convenience
        pass


def _flush():
    try:
        sys.stdout.flush()   # a pipe or file buffers stdout: the URL must show before serve_forever
    except Exception:  # noqa: BLE001
        pass


def serve(port: int = 0, open_browser: bool = True, idle_exit_s: float = 0, emit=print,
          open_target: str | None = None, paths: flow.Paths | None = None, opener=None,
          on_start=None) -> int:
    """Start the studio (or, with ``open_target``, reuse the running one) and open the browser at
    its URL + the target hash. Blocks while it serves; returns at once on reuse. ``on_start(srv)``
    (tests) runs once the server and its instance file exist."""
    h = target_hash(open_target) if open_target is not None else ""
    if h is None:
        emit("apex-router studio: bad --open target (use node:<id>, review or branches)")
        return 2
    ipath = instance_path(paths)
    # SIGTERM (launchd, kill) = a clean exit, from the first line on: the finally below clears the
    # instance file on every path after it is written
    prev_term = None
    if threading.current_thread() is threading.main_thread():
        def on_term(signum, frame):
            raise KeyboardInterrupt
        try:
            prev_term = signal.signal(signal.SIGTERM, on_term)
        except (ValueError, OSError):
            prev_term = None
    srv = loop = None
    try:
        with _instance_lock(ipath):
            inst = live_instance(ipath) if open_target is not None else None
            if inst:
                url = f"http://127.0.0.1:{inst['port']}/?token={inst['token']}"
                emit(url + h)
                _flush()
                if open_browser:
                    _open_url(url + h, opener)
                return 0
            try:
                srv = make_server(port, paths=paths)
            except OSError as e:
                emit(f"apex-router studio: cannot bind 127.0.0.1:{port} ({e.strerror or e})")
                return 1
            # serve BEFORE the file names this studio and before the lock goes: a second start
            # that finds the file must get its /api/ping answer at once, or it calls this stale
            loop = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5},
                                    name="studio-serve", daemon=True)
            loop.start()
            try:
                write_instance(ipath, srv)   # replaces a stale file (dead pid or no answer)
            except OSError:
                pass                          # read-only home: the studio still runs, no reuse
        url = url_of(srv)
        if on_start:
            on_start(srv)
        emit(url + h)
        _flush()
        if idle_exit_s and idle_exit_s > 0:
            def watch():
                while True:
                    time.sleep(min(30.0, idle_exit_s))
                    if time.time() - srv.last_request > idle_exit_s and not srv.waiter.busy():
                        srv.shutdown()
                        return
            threading.Thread(target=watch, daemon=True).start()
        if open_browser:
            # in a thread: a slow browser start must not hold up Ctrl-C / SIGTERM or the cleanup
            threading.Thread(target=_open_url, args=(url + h, opener), name="studio-open", daemon=True).start()
        while loop.is_alive():
            loop.join(0.5)                    # the main thread stays free for SIGTERM / Ctrl-C
    except KeyboardInterrupt:
        pass
    finally:
        # cleanup must finish: a 2nd SIGTERM (or Ctrl-C) here is ignored, and the file goes FIRST
        if prev_term is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if srv is not None:
            clear_instance(ipath, srv)       # removes the file only if it names this studio
            if loop is not None and loop.is_alive():   # shutdown() waits for serve_forever
                stopper = threading.Thread(target=srv.shutdown, daemon=True)
                stopper.start()
                with contextlib.suppress(KeyboardInterrupt):
                    stopper.join(5)
            srv.server_close()
        if prev_term is not None:
            try:
                signal.signal(signal.SIGTERM, prev_term)
            except (ValueError, OSError):
                pass
    return 0
