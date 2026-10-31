"""ASGI app — routes both wires through the passthrough handler; owns shared resources.

M0 wires: request → passthrough.handle → upstream. The Store and Upstream are created at
startup and closed at shutdown (one connection pool, one db handle). /healthz and /stats are
apex-local (never forwarded) so the harness and A/B benchmark can poll without hitting the upstream.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import traceback
from collections.abc import Callable
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from apex_router.proxy_engine.config import APEX_VERSION, CONFIG, Config
from apex_router.proxy_engine.logfmt import eprint
from apex_router.proxy_engine.policy import EvidenceBundle, InvalidPolicy, PolicyVersion
from apex_router.proxy_engine.proxy.handlers import passthrough
from apex_router.proxy_engine.proxy.handlers import shadow as shadow_handler
from apex_router.proxy_engine.proxy.upstream import Upstream
from apex_router.proxy_engine.session.store import Store
from apex_router.proxy_engine.telemetry.events import TELEMETRY_SCHEMA_VERSION, TelemetryWriter


# A5: the proxy runs for days, so a startup-only gc lets stale sessions pile up in state.db.
# Run Store.gc() again about once per hour from the heartbeat loop (retention is unchanged).
GC_INTERVAL_S = 3600.0
GC_PAUSE_S = 0.005   # hourly gc: lock-free pause between sessions, so requests get the lock


async def _heartbeat_loop(
    telemetry: TelemetryWriter,
    interval_s: float,
    *,
    gc: Callable[[], int] | None = None,
    gc_interval_s: float = GC_INTERVAL_S,
    on_gc: Callable[[int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Emit a heartbeat + rotate-check every `interval_s`, off the request path. Runs until
    cancelled at shutdown. Fail-open: an exception in a tick is swallowed so the ticker never dies
    silently and never touches the data plane.

    If `gc` is given, it runs in a worker thread when `gc_interval_s` passed since the last run
    (the startup gc counts as the first run). `on_gc` gets the removed-row count. A gc error is
    logged to stderr and the next try is one full interval later (no retry storm)."""
    last_gc = clock()
    while True:
        await asyncio.sleep(interval_s)
        try:
            telemetry.rotate_if_large(prune=False)       # one rename: cheap, stays on the loop
            telemetry.heartbeat()
            if len(telemetry._gens()) > telemetry.KEEP:  # fold + delete: off the event loop
                await asyncio.to_thread(telemetry.prune)
        except Exception:  # noqa: BLE001 — a heartbeat must never take down the proxy
            pass
        if gc is None or clock() - last_gc < gc_interval_s:
            continue
        last_gc = clock()
        try:
            removed = await asyncio.to_thread(gc)
            if on_gc is not None:
                on_gc(removed)
        except Exception:  # noqa: BLE001 — a gc failure must never stop the loop or the proxy
            eprint("apex: periodic state-store gc failed:\n" + traceback.format_exc().rstrip())


def _load_policy(cfg: Config) -> PolicyVersion | None:
    """Load the signed policy bundle for shadow/live via the plane-clean `load_verified` (the only
    policy entry — policy_provenance). Missing file → None (shadow logs raw wire evidence anyway). A
    present-but-invalid bundle is authority-side doubt → fail CLOSED: re-raise so the operator sees
    a forged/tampered/foreign-key bundle at startup rather than silently serving None."""
    import json

    path = cfg.policy_path
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    try:
        return EvidenceBundle.load_verified(data).policy
    except InvalidPolicy:
        raise  # fail-closed: refuse to start shadow on an unverifiable bundle (not a silent None)


LOCAL_PATHS = frozenset({"/healthz", "/status", "/stats"})
DRAIN_MAX_S = 60.0   # a drain that nobody ends expires by itself (the studio may die mid-restart)


def set_drain(activity: dict, on: bool) -> None:
    """SIGUSR1 = drain (new proxied requests get 503 + Retry-After, requests in progress finish),
    SIGUSR2 = end the drain. The flow studio drains before a restart, so no request can start
    between its last idle check and `launchctl bootout`. Only the same user can signal."""
    activity["drain_until"] = time.time() + DRAIN_MAX_S if on else 0.0


def draining(activity: dict) -> bool:
    return activity.get("drain_until", 0.0) > time.time()


class ActivityCounter:
    """Pure ASGI wrapper: counts proxied requests that are in progress, until the LAST byte of a
    streamed body is sent (or the client goes away). The local /healthz /status /stats are not
    counted, so a monitor that polls them does not keep the proxy "busy". `/stats` shows it; the
    flow studio restarts a proxy only at inflight == 0 after an idle window. O(1), no I/O."""

    def __init__(self, app, activity: dict):
        self.app = app
        self.activity = activity

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") in LOCAL_PATHS:
            await self.app(scope, receive, send)
            return
        a = self.activity
        if a["drain_until"] and draining(a):
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"retry-after", b"5"),
                                    (b"connection", b"close")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"apex proxy restarts now, retry in 5 s"}'})
            return
        a["inflight"] += 1
        a["last_start"] = time.time()
        try:
            await self.app(scope, receive, send)
        finally:
            a["inflight"] -= 1
            a["last_end"] = time.time()


def create_app(cfg: Config | None = None) -> Starlette:
    cfg = cfg or CONFIG
    cfg.ensure_home()

    state: dict = {}
    activity = {"inflight": 0, "last_start": 0.0, "last_end": 0.0, "started": time.time(), "drain_until": 0.0}

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        with contextlib.suppress(ValueError, RuntimeError, NotImplementedError):   # not main thread (tests)
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGUSR1, set_drain, activity, True)
            loop.add_signal_handler(signal.SIGUSR2, set_drain, activity, False)
        state["upstream"] = Upstream(cfg)
        state["store"] = Store(cfg.db_path, retention_days=cfg.retention_days)
        state["telemetry"] = TelemetryWriter(cfg.telemetry_path)
        state["gc_removed"] = state["store"].gc()
        # Load the signed policy (if present) at startup via plane-clean load_verified — the ONLY
        # policy entry (policy_provenance). Loaded in BOTH modes now: active runs the same byte-only
        # composition compute as shadow (measurement always-on — telemetry contract), so it needs a
        # policy too. A missing file is fine (both modes log raw bytes_by_class + usage); a
        # PRESENT-but-invalid file fails CLOSED (refuse to serve a forged bundle — authority-side
        # doubt refuses in either mode, unlike a block-side doubt which ships raw).
        state["policy"] = _load_policy(cfg)
        # Heartbeat ticker: emit a heartbeat every heartbeat_s so an idle proxy is visibly alive to
        # a consumer (the TUI), and rotate the telemetry file off the hot path. Off the request path
        # entirely; cancelled on shutdown.
        def _gc_done(removed: int) -> None:
            state["gc_removed"] = state.get("gc_removed", 0) + removed  # cumulative since start

        hb_task = asyncio.ensure_future(_heartbeat_loop(
            state["telemetry"], cfg.heartbeat_s,
            gc=lambda: state["store"].gc(pause_s=GC_PAUSE_S), gc_interval_s=GC_INTERVAL_S, on_gc=_gc_done,
        ))
        try:
            yield
        finally:
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task
            await state["upstream"].aclose()
            state["store"].close()

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"ok": True, "version": APEX_VERSION, "port": cfg.port})

    async def stats(_request: Request) -> Response:
        store: Store = state["store"]
        policy: PolicyVersion | None = state.get("policy")
        return JSONResponse(
            {
                "version": APEX_VERSION,
                "db": {
                    "path": str(cfg.db_path),
                    "bytes": store.size_bytes(),
                    "counts": store.counts(),
                    "gc_removed": state.get("gc_removed", 0),
                },
                "upstream": {
                    "anthropic": cfg.anthropic_upstream,
                    "openai": cfg.openai_upstream,
                    "xai": cfg.xai_upstream,
                },
                # proxied requests in progress (stream end counted) and seconds since the last one
                # started or ended; idle_s is 0 while one is in progress
                "activity": {
                    "inflight": activity["inflight"],
                    "idle_s": 0.0 if activity["inflight"] > 0 else round(
                        time.time() - max(activity["last_start"], activity["last_end"], activity["started"]), 1),
                    "started_s_ago": round(time.time() - activity["started"], 1),
                    "pid": os.getpid(),
                    "draining": draining(activity),
                },
                "ttft_budget_ms": cfg.ttft_budget_ms,
                "shadow": {
                    "enabled": cfg.shadow_mode,
                    "policy_loaded": policy is not None,
                    "policy_epoch": policy.policy_epoch if policy else None,
                    "has_active_policy": policy.has_active_policy() if policy else False,
                },
            }
        )

    async def status(_request: Request) -> Response:
        """The human 'is it healthy and in what posture' surface — the endpoint a confused operator
        hits first. Flat, self-explaining, and HONEST: `null` is the one value indistinguishable
        from 'broken', so posture is an explicit string, never an omitted or null key. `no_policy`
        (the raw telemetry reason) is TRANSLATED here to 'measure-only' for a reader who hasn't read
        the decision log. Local (carved out of the proxy path space), never forwarded upstream."""
        policy: PolicyVersion | None = state.get("policy")
        loaded = policy is not None
        return JSONResponse(
            {
                "status": "ok",
                "version": APEX_VERSION,
                "mode": "shadow" if cfg.shadow_mode else "active",
                "policy_loaded": loaded,
                # posture: measure-only when no policy admits transforms; 'enforcing' once a signed
                # policy with an active cell is loaded. A present-but-invalid policy never reaches
                # here — the proxy fails closed at startup (app._load_policy), so this endpoint only
                # ever reports a determinate posture.
                "posture": (
                    "enforcing" if (loaded and policy.has_active_policy()) else "measure-only"
                ),
                "policy_epoch": policy.policy_epoch if loaded else None,
                "schema_version": TELEMETRY_SCHEMA_VERSION,
                "port": cfg.port,
            }
        )

    async def proxy(request: Request) -> Response:
        # Shadow mode: full pipeline + usage capture, passthrough emission. Otherwise pure M0.
        if cfg.shadow_mode:
            return await shadow_handler.handle(
                request, state["upstream"], state["telemetry"], state.get("policy"),
                store=state["store"],
            )
        return await passthrough.handle(
            request, state["upstream"], state["telemetry"], state.get("policy"),
            store=state["store"],
        )

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/status", status, methods=["GET"]),
        Route("/stats", stats, methods=["GET"]),
        # Catch-all: everything else is proxied (both /v1/messages and /v1/chat/completions).
        # All methods incl. HEAD/OPTIONS so the proxy is method-transparent (xval #16).
        Route(
            "/{path:path}",
            proxy,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
        ),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.apex = state
    app.add_middleware(ActivityCounter, activity=activity)
    return app
