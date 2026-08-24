"""ASGI app — routes both wires through the passthrough handler; owns shared resources.

M0 wires: request → passthrough.handle → upstream. The Store and Upstream are created at
startup and closed at shutdown (one connection pool, one db handle). /healthz and /stats are
apex-local (never forwarded) so the harness and A/B benchmark can poll without hitting the upstream.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from apex_router.proxy_engine.config import APEX_VERSION, CONFIG, Config
from apex_router.proxy_engine.policy import EvidenceBundle, InvalidPolicy, PolicyVersion
from apex_router.proxy_engine.proxy.handlers import passthrough
from apex_router.proxy_engine.proxy.handlers import shadow as shadow_handler
from apex_router.proxy_engine.proxy.upstream import Upstream
from apex_router.proxy_engine.session.store import Store
from apex_router.proxy_engine.telemetry.events import TELEMETRY_SCHEMA_VERSION, TelemetryWriter


async def _heartbeat_loop(telemetry: TelemetryWriter, interval_s: float) -> None:
    """Emit a heartbeat + rotate-check every `interval_s`, off the request path. Runs until
    cancelled at shutdown. Fail-open: an exception in a tick is swallowed so the ticker never dies
    silently and never touches the data plane."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            telemetry.rotate_if_large()
            telemetry.heartbeat()
        except Exception:  # noqa: BLE001 — a heartbeat must never take down the proxy
            pass


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


def create_app(cfg: Config | None = None) -> Starlette:
    cfg = cfg or CONFIG
    cfg.ensure_home()

    state: dict = {}

    @asynccontextmanager
    async def lifespan(_app: Starlette):
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
        hb_task = asyncio.ensure_future(_heartbeat_loop(state["telemetry"], cfg.heartbeat_s))
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
                "upstream": {"anthropic": cfg.anthropic_upstream, "openai": cfg.openai_upstream},
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
    return app
