"""Upstream HTTP client — a single shared httpx.AsyncClient with streaming.

M0 forwards byte-identically; the transform pipeline (M3+) sits between the inbound request
and this client. Kept separate so the wire code never re-creates connections (TTFT cost).
"""
from __future__ import annotations

import httpx

from apex_router.proxy_engine.config import Config

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1); also drop framing that the
# HTTP client/server layer recomputes (content-length, transfer-encoding, host). Everything
# else — authorization, anthropic-beta, cache_control, accept-encoding, x-claude-code-* —
# passes through VERBATIM. accept-encoding is END-TO-END, not hop-by-hop: stripping it changes
# content negotiation and can bust the cache (cross-validation; confirmed: httpx re-injects its
# own default if we drop it, and aiter_raw forwards the encoded body untouched anyway).
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
    "content-length", "host",
})


def _connection_named(raw_headers: list[tuple[bytes, bytes]]) -> set[str]:
    """Headers named in a `Connection:` header are connection-scoped and must be dropped
    (RFC 7230 §6.1), else a client can leak connection headers upstream (cross-validation)."""
    named: set[str] = set()
    for k, v in raw_headers:
        if k.lower() == b"connection":
            named |= {tok.strip().lower().decode("latin-1") for tok in v.split(b",") if tok.strip()}
    return named


def filter_request_headers(raw_headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Byte-faithful request header filtering from the raw ASGI header list.

    Works on raw (bytes, bytes) pairs — NOT dict(request.headers) — so duplicate headers
    (repeated anthropic-beta, cookie), original casing, and value bytes are preserved
    (cross-validation). Only hop-by-hop + Connection-named headers are removed.
    """
    drop = _HOP_BY_HOP | _connection_named(raw_headers)
    return [(k, v) for k, v in raw_headers if k.decode("latin-1").lower() not in drop]


def filter_response_headers(headers: httpx.Headers) -> list[tuple[str, str]]:
    """Return (name, value) str pairs, dropping hop-by-hop. Starlette re-frames the wire
    (chunked transfer-encoding, content-length) itself, so we must not pass those through.
    content-encoding is KEPT: aiter_raw forwards the encoded body verbatim, so the header
    must match the bytes."""
    drop = _HOP_BY_HOP | _connection_named(headers.raw)
    out = []
    for k, v in headers.multi_items():
        if k.lower() in drop:
            continue
        out.append((k, v))
    return out


class Upstream:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=cfg.upstream_connect_timeout_s,
                read=cfg.upstream_read_timeout_s,
                write=cfg.upstream_read_timeout_s,
                pool=cfg.upstream_connect_timeout_s,
            ),
            follow_redirects=False,
            # No default headers: httpx would otherwise inject its own user-agent/accept/
            # accept-encoding when the client omits them, which is not passthrough (xval #5).
            headers={},
        )

    def base_for(self, client_kind: str) -> str:
        return self._cfg.openai_upstream if client_kind == "codex" else self._cfg.anthropic_upstream

    def endpoint_id(self, client_kind: str) -> str:
        """A stable telemetry label for the endpoint a wire is reached through — derived from the
        SAME client→upstream routing as `base_for`, so a row's endpoint attribution matches where
        the request actually went (Codex cross-validation: a static "anthropic" mislabels codex rows,
        which route to `openai_upstream`, not the Anthropic gateway). Two endpoints exist today — that this is no
        longer a single constant is itself the A-Proto "second endpoint" re-activation trigger; the
        degenerate-constant assumption was wrong, so the label is derived, not hard-coded. Anthropic
        traffic → `the Anthropic gateway` (a gateway in front of the provider); codex traffic → `openai`. When a real
        endpoints table lands (A-Proto), this becomes a lookup on the resolved endpoint profile."""
        return "openai" if client_kind == "codex" else "anthropic"

    def build_url(self, client_kind: str, raw_path: str, query_string: bytes) -> str:
        """Preserve the raw path AND query string (xval #1/#2). `?beta=…`, pagination, and
        signed params must reach the upstream verbatim."""
        base = self.base_for(client_kind).rstrip("/")
        qs = query_string.decode("latin-1")
        return base + raw_path + (("?" + qs) if qs else "")

    async def send_stream(
        self, method: str, url: str, *, headers: list[tuple[bytes, bytes]], content: bytes
    ) -> httpx.Response:
        """Send and return a streaming response. Caller MUST `await response.aclose()`
        (done in the handler's stream `finally`). Using send(stream=True) keeps the body
        un-buffered so TTFT is the upstream's first byte, not full-body download.

        `headers` is the raw (bytes, bytes) list from filter_request_headers. httpx's
        build_request injects default accept/accept-encoding/user-agent/connection headers
        even with an empty client header set (xval #5), so we SCRUB the built request down to
        exactly the client-provided headers plus the transport-required host/content-length.
        Result: apex adds nothing the client didn't send — true passthrough, and no invented
        accept-encoding that could make the upstream gzip a response the client didn't ask for.
        """
        req = self._client.build_request(method, url, headers=headers, content=content)
        provided = {k.lower() for k, _ in headers}
        keep = provided | {b"host", b"content-length"}
        scrubbed = [(k, v) for k, v in req.headers.raw if k.lower() in keep]
        req.headers = httpx.Headers(scrubbed)
        return await self._client.send(req, stream=True)

    async def aclose(self) -> None:
        await self._client.aclose()
