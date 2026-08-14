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


# Public provider endpoints an Azure-AD gateway token must never be sent to (credential disclosure).
_PUBLIC_PROVIDER_HOSTS = frozenset({"api.anthropic.com", "api.openai.com"})

# Header names that mean "the client already authenticated itself" — Authorization/x-api-key plus the
# Azure APIM alternatives. Checked against RAW inbound headers so a `Connection:`-stripped auth header
# can't fool the injector into overriding a client's own credential.
_CLIENT_AUTH_HEADERS = frozenset({
    b"authorization", b"x-api-key", b"ocp-apim-subscription-key", b"api-key",
})


def _client_has_auth(raw_headers: list[tuple[bytes, bytes]]) -> bool:
    # A NON-EMPTY value is required: an empty/whitespace `authorization:`/`x-api-key:` is not a real
    # credential, so it must not suppress injection (else the client 401s with the proxy standing by).
    return any(k.lower() in _CLIENT_AUTH_HEADERS and v.strip() for k, v in raw_headers)


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

    async def inject_auth(
        self,
        headers: list[tuple[bytes, bytes]],
        client_kind: str,
        *,
        raw_headers: list[tuple[bytes, bytes]],
    ) -> list[tuple[bytes, bytes]]:
        """Opt-in Azure-AD auth injection — a STRICT SUPERSET of passthrough.

        Returns `headers` UNCHANGED unless ALL hold. Only then is a freshly-minted
        `Authorization: Bearer` appended, so an already-authed request is byte-identical to before:

          1. injection is enabled (`cfg.inject_azure_token`);
          2. this is the Anthropic wire (the codex/OpenAI wire has its own auth);
          3. the target upstream is NOT a public provider endpoint — an Azure AD token minted for
             `cognitiveservices.azure.com` must NEVER be sent to api.anthropic.com/api.openai.com
             (that would DISCLOSE the credential to an unrelated host — the whole feature only makes
             sense in front of an internal gateway, so we refuse to attach it to the public default);
          4. the client sent NO auth of its own. This is checked against the RAW inbound headers, not
             the hop-by-hop-filtered set: a client that sends `Connection: authorization` + its own
             `Authorization` would have that stripped by `filter_request_headers`, and a post-filter
             check would then wrongly inject over it. Reading the raw headers closes that bypass. The
             recognized schemes include the Azure/APIM alternatives (subscription key, api-key), so a
             client authed by any of them is left untouched.

        A mint/encode failure is swallowed (fail-open): forward unchanged and let the upstream return
        its own 401, never a proxy-invented 500. The appended header lands in the raw list BEFORE
        `send_stream`'s scrub, so it survives (send_stream keeps the client-provided keys + host/len).
        """
        if not self._cfg.inject_azure_token or client_kind == "codex":
            return headers
        # (3) never attach the Azure bearer to a public provider host. Parse DEFENSIVELY: a malformed
        # configured upstream must not become a 500 (this runs before the handler's upstream try), and
        # if the host can't be determined we skip injection (the safe direction). Strip a trailing
        # FQDN dot so `api.anthropic.com.` can't slip past the denylist.
        from urllib.parse import urlsplit
        try:
            host = (urlsplit(self.base_for(client_kind)).hostname or "").lower().rstrip(".")
        except ValueError:
            return headers
        if not host or host in _PUBLIC_PROVIDER_HOSTS:
            return headers
        # (4) client-auth check on the RAW headers (pre-filter), across all recognized schemes.
        if _client_has_auth(raw_headers):
            return headers  # client already authed — never override (passthrough for authed reqs)
        try:
            from apex_router.proxy_engine.proxy.az_auth import get_provider
            provider = get_provider(self._cfg.azure_token_resource, self._cfg.az_bin)
            token = await provider.token_async()
            if not isinstance(token, str):  # malformed CLI output → fail-open, not a 500
                raise TypeError(f"az token is not a string: {type(token).__name__}")
            auth_value = b"Bearer " + token.encode("latin-1")  # inside the guard (finding: encode)
        except Exception as e:  # noqa: BLE001 — fail-open: forward unauthed, upstream returns 401
            import sys
            print(f"apex: azure token injection failed, forwarding without auth: {e!r}",
                  file=sys.stderr)
            return headers
        return [*headers, (b"authorization", auth_value)]

    def endpoint_id(self, client_kind: str) -> str:
        """A stable telemetry label for the endpoint a wire is reached through — derived from the
        SAME client→upstream routing as `base_for`, so a row's endpoint attribution matches where
        the request actually went (Codex cross-validation: a static "anthropic" mislabels codex rows,
        which route to `openai_upstream`, not the anthropic wire). Two endpoints exist today — that this is no
        longer a single constant is itself the A-Proto "second endpoint" re-activation trigger; the
        degenerate-constant assumption was wrong, so the label is derived, not hard-coded. Anthropic
        traffic → `anthropic` ; codex traffic → `openai`. When a real
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
