"""Upstream HTTP client — a single shared httpx.AsyncClient with streaming.

M0 forwards byte-identically; the transform pipeline (M3+) sits between the inbound request
and this client. Kept separate so the wire code never re-creates connections (TTFT cost).
"""
from __future__ import annotations

import asyncio
import math
import os
import random
import ssl
import time

import httpx

from apex_router.proxy_engine.config import Config

# Hard safety caps for the connect-retry budget (independent of config, so a hostile/typo'd
# value can never hang or DoS the request path — xval F3).
_MAX_CONNECT_RETRIES = 10
_MAX_CONNECT_BACKOFF_S = 30.0
_MAX_CONNECT_TOTAL_BACKOFF_S = 60.0  # cumulative backoff ceiling across all retries of one request

# Transport errors retried by `send_stream` (which returns at response HEADERS — before the handler has
# streamed a single byte to the client, so a retry is invisible to it). Two classes, two policies:
#
# PRE-WRITE (`_PRE_WRITE_ERRORS`): ConnectError/ConnectTimeout — the connection never came up, so the
# request was never written. Retried on ANY method/path, regardless of how long the attempt took.
#
# BODY-SENT (`_BODY_SENT_ERRORS`): SSLError/ReadError/WriteError/RemoteProtocolError on an established
# connection — the request MAY already have been written and processed. Retried ONLY when the attempt
# was a fast-fail AND idempotent-enough (see `_body_sent_retry_allowed`). Live finding (2026-10-02):
# 402 SSLError + 278 ReadError rows on the anthropic wire in 7 days, all connect_retries=0, ~230 ms
# each — the fast-fail signature of a dead pooled keep-alive connection. `ssl.SSLError` is listed RAW
# because anyio re-raises non-EOF TLS errors unmapped and httpcore's read/write exc_map doesn't wrap
# them (only its start_tls map does, as ConnectError).
# Deliberately EXCLUDED: ReadTimeout/WriteTimeout/PoolTimeout — retrying a 600 s read-timeout would
# multiply the hang, and a pool timeout is local back-pressure, not a transient link fault.
_PRE_WRITE_ERRORS: tuple[type[BaseException], ...] = (httpx.ConnectError, httpx.ConnectTimeout)
_BODY_SENT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    ssl.SSLError,
)
_RETRYABLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = _PRE_WRITE_ERRORS + _BODY_SENT_ERRORS

# Body-sent retry guard (review U1/U2). An attempt that failed only after this long is NOT a dead
# pooled connection — the upstream was likely generating, so a replay would multiply tokens and
# latency (and the client's own retry on the 502 multiplies it again). Env-configurable.
_DEFAULT_RETRY_FAST_FAIL_MS = 3000.0
# Completion endpoints: stateless (a duplicate costs tokens, mutates nothing). Anything else — batch
# creation, file upload, etc. — may create server-side state, so body-sent errors there are not retried.
_COMPLETION_PATH_SUFFIXES = ("/v1/messages", "/v1/chat/completions", "/responses")
_NON_COMPLETION_PATH_MARKERS = ("/batches", "/files")
_BODY_SENT_RETRY_METHODS = frozenset({"POST", "GET"})

# Clock for measuring one attempt's duration (module-level so tests can substitute a fake clock).
_attempt_clock = time.monotonic


def _retry_fast_fail_s() -> float:
    """APEX_RETRY_FAST_FAIL_MS as seconds; a missing/garbage/negative/non-finite value → default."""
    raw = os.environ.get("APEX_RETRY_FAST_FAIL_MS")
    try:
        ms = float(raw) if raw is not None else _DEFAULT_RETRY_FAST_FAIL_MS
    except ValueError:
        ms = _DEFAULT_RETRY_FAST_FAIL_MS
    if not math.isfinite(ms) or ms < 0:
        ms = _DEFAULT_RETRY_FAST_FAIL_MS
    return ms / 1000.0


# Retry-action A/B (L1, docs/research/2026-10-08-l1-regime-and-retry-ab.md). The burst chain found
# that right after a failed call the next one fails 30–40% of the time vs < 4% overall — a HYPOTHESIS
# that waiting (or switching upstream) before a retry beats retrying at once. These knobs instrument
# that question without changing behaviour by default:
#   APEX_RETRY_POLICY = immediate | wait | switch  (default immediate = the jittered backoff below,
#                       unchanged). wait: each pre-retry sleep is at least APEX_RETRY_WAIT_MS
#                       (default 1000, clamped to [0, 2000]). switch: a PLACEHOLDER — there is one
#                       upstream per wire today, so it behaves exactly like immediate and only logs
#                       the arm (the switch target lands with a second endpoint).
#   APEX_RETRY_AB = 1  randomises the arm per request (one coin, drawn at the request's first retry),
#                       over APEX_RETRY_AB_ARMS (default "immediate,wait"); the propensity is logged.
# Telemetry (v11): retry_policy / retry_arm / retry_propensity, set only on rows that retried.
RETRY_POLICIES = ("immediate", "wait", "switch")
_DEFAULT_RETRY_WAIT_MS = 1000.0
_MAX_RETRY_WAIT_MS = 2000.0


def _retry_wait_s() -> float:
    """APEX_RETRY_WAIT_MS as seconds, clamped to [0, 2 s]; garbage/non-finite → default."""
    raw = os.environ.get("APEX_RETRY_WAIT_MS")
    try:
        ms = float(raw) if raw is not None else _DEFAULT_RETRY_WAIT_MS
    except ValueError:
        ms = _DEFAULT_RETRY_WAIT_MS
    if not math.isfinite(ms):
        ms = _DEFAULT_RETRY_WAIT_MS
    return min(max(ms, 0.0), _MAX_RETRY_WAIT_MS) / 1000.0


def retry_assignment(rng: random.Random | None = None) -> tuple[str, str, float]:
    """(retry_policy, retry_arm, retry_propensity) for one request, from the environment.

    Without APEX_RETRY_AB=1 the arm is the configured policy (unknown value → immediate) with
    propensity 1.0. With it, policy is "ab" and the arm is a uniform draw over APEX_RETRY_AB_ARMS
    (unknown names dropped, duplicates removed; fewer than two valid arms → immediate,wait)."""
    r = rng or random
    if os.environ.get("APEX_RETRY_AB", "").strip() == "1":
        raw = os.environ.get("APEX_RETRY_AB_ARMS", "immediate,wait")
        arms = list(dict.fromkeys(a.strip().lower() for a in raw.split(",")
                                  if a.strip().lower() in RETRY_POLICIES))
        if len(arms) < 2:
            arms = ["immediate", "wait"]
        return "ab", arms[r.randrange(len(arms))], 1.0 / len(arms)
    pol = os.environ.get("APEX_RETRY_POLICY", "immediate").strip().lower()
    pol = pol if pol in RETRY_POLICIES else "immediate"
    return pol, pol, 1.0


def _is_completion_request(method: str, url: str) -> bool:
    """True for POST/GET on a stateless completion endpoint (never /batches or /files)."""
    if method.upper() not in _BODY_SENT_RETRY_METHODS:
        return False
    path = httpx.URL(url).path.rstrip("/")
    if any(m in path for m in _NON_COMPLETION_PATH_MARKERS):
        return False
    return path.endswith(_COMPLETION_PATH_SUFFIXES)

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

    def prepare_body(self, client_kind: str, raw_path: str, body: bytes) -> tuple[bytes, dict | None]:
        """The body to forward, plus a telemetry note when it was edited. Identity unless the
        opt-in stable Codex cache key is on AND this is the Responses wire (proxy.cache_key)."""
        if not (self._cfg.codex_cache_key and client_kind == "codex" and raw_path.endswith("/responses")):
            return body, None
        from apex_router.proxy_engine.proxy import cache_key
        try:
            return cache_key.rewrite(body, self._cfg.codex_cache_key_shards)
        except Exception:  # noqa: BLE001 — fail-open: never break a request over a cache hint
            return body, None

    def build_url(self, client_kind: str, raw_path: str, query_string: bytes) -> str:
        """Preserve the raw path AND query string (xval #1/#2). `?beta=…`, pagination, and
        signed params must reach the upstream verbatim."""
        base = self.base_for(client_kind).rstrip("/")
        qs = query_string.decode("latin-1")
        return base + raw_path + (("?" + qs) if qs else "")

    async def send_stream(
        self, method: str, url: str, *, headers: list[tuple[bytes, bytes]], content: bytes,
        stats: dict | None = None,
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
        provided = {k.lower() for k, _ in headers}
        keep = provided | {b"host", b"content-length"}
        # Transient-transport retry (Finding 4, widened 2026-10-02, narrowed per review U1/U2).
        # `send(stream=True)` returns at response headers, so any raise in this loop happens before the
        # handler has forwarded ANY byte to the client — the client can't observe a retry. Errors after
        # the first body byte are raised from the handler's body_stream and are NEVER retried (bytes
        # can't be un-sent; the client owns that retry). The contract:
        #
        #   - ConnectError/ConnectTimeout (pre-write: the request was never written) — retried on ANY
        #     method and path, with no elapsed-time condition. Cannot double-submit.
        #   - SSLError/ReadError/WriteError/RemoteProtocolError (body MAY have been written) — retried
        #     ONLY when ALL of: (a) the failed attempt took < APEX_RETRY_FAST_FAIL_MS (default 3000 ms,
        #     measured from just before `send` to the raise) — the dead-pooled-connection signature
        #     (~230 ms observed), NOT a reset after minutes of generation, whose replay would multiply
        #     tokens and latency; (b) the path is a completion endpoint (ends with /v1/messages,
        #     /v1/chat/completions or /responses, contains neither /batches nor /files) — those are
        #     stateless, a duplicate costs tokens but mutates nothing; (c) the method is POST or GET.
        #     Otherwise the original exception propagates on that attempt (handler → 502 + error_cause).
        #     Residual risk, accepted: a fast-failing body-sent error on a completion endpoint MAY still
        #     duplicate a completion the upstream had started — bounded by the guard to < 3 s of work.
        #   - Timeouts are never retried (see _RETRYABLE_TRANSPORT_ERRORS).
        #
        # Requires a bytes `content` (both callers buffer `request.body()`) so a fresh request re-sends
        # identical bytes; a streamed/consumed body would replay empty. All retries share one bounded,
        # jittered backoff budget and are counted in `connect_retries` / `connect_backoff_s`.
        #
        # `stats` (optional out-param): total seconds slept in backoff are recorded under
        # 'connect_backoff_s' and 'connect_retries' so the CALLER can bill that apex-controlled sleep
        # to apex_added_ms, not to upstream latency (xval F4 — the sleep happens inside this call,
        # which the handler times as the upstream window).
        # Clamp the retry budget at point-of-use: a hostile/typo'd config (inf, negative, or a huge
        # count that makes 2**i overflow) must not hang the request path or DoS it with an
        # astronomical sleep (xval F3). CAP total attempts and per-sleep seconds to sane bounds.
        retries = self._cfg.upstream_connect_retries
        retries = 0 if not isinstance(retries, int) or retries < 0 else min(retries, _MAX_CONNECT_RETRIES)
        backoff = self._cfg.upstream_connect_backoff_s
        if not math.isfinite(backoff) or backoff < 0:
            backoff = 0.0
        attempts = retries + 1
        fast_fail_s = _retry_fast_fail_s()
        completion = _is_completion_request(method, url)
        slept_total = 0.0  # total-duration budget: stop retrying once cumulative backoff hits the cap
        arm = None  # retry-action A/B arm, drawn lazily at the first retry (see retry_assignment)
        for i in range(attempts):
            # Build a FRESH request each attempt: a consumed/aborted request object must not be
            # re-sent, and the scrub is cheap.
            req = self._client.build_request(method, url, headers=headers, content=content)
            scrubbed = [(k, v) for k, v in req.headers.raw if k.lower() in keep]
            req.headers = httpx.Headers(scrubbed)
            attempt_t0 = _attempt_clock()
            try:
                return await self._client.send(req, stream=True)
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                if not isinstance(exc, _PRE_WRITE_ERRORS):
                    # body-sent class: retry only a fast-fail on a completion endpoint (U1/U2)
                    attempt_s = _attempt_clock() - attempt_t0
                    if attempt_s >= fast_fail_s or not completion:
                        raise
                if i == attempts - 1 or slept_total >= _MAX_CONNECT_TOTAL_BACKOFF_S:
                    raise  # exhausted (attempts OR total-backoff budget) — handler books the error
                # exp backoff, per-sleep capped; then JITTERED (equal-jitter: half fixed + half
                # random in [0, half]) so many clients recovering from the same upstream blip don't
                # re-connect in lockstep (thundering herd). Also bounded by the remaining total
                # budget so cumulative sleep never exceeds _MAX_CONNECT_TOTAL_BACKOFF_S.
                capped = min(backoff * (2**i), _MAX_CONNECT_BACKOFF_S)
                half = capped / 2.0
                delay = half + random.uniform(0.0, half)
                # Retry-action A/B: the arm is drawn ONCE per request, at its first retry (so only
                # requests that actually retry carry it). `wait` raises this sleep to at least the
                # bounded APEX_RETRY_WAIT_MS; `immediate`/`switch` leave it as today.
                if arm is None:
                    policy, arm, propensity = retry_assignment()
                    if stats is not None:
                        stats["retry_policy"] = policy
                        stats["retry_arm"] = arm
                        stats["retry_propensity"] = propensity
                if arm == "wait":
                    delay = max(delay, _retry_wait_s())
                delay = min(delay, _MAX_CONNECT_TOTAL_BACKOFF_S - slept_total)
                slept_total += delay  # budget accounting uses SCHEDULED delay (the cap's currency)
                # But telemetry must bill the ELAPSED sleep, not the scheduled one: under event-loop
                # contention `asyncio.sleep(d)` can take materially longer than `d`, and that real
                # wall-time is what F4 subtracts from the upstream window. Measuring scheduled time
                # would under-remove and re-bill the slack to the upstream (xval #3).
                _t0 = time.perf_counter()
                await asyncio.sleep(delay)
                elapsed = time.perf_counter() - _t0
                if stats is not None:
                    stats["connect_backoff_s"] = stats.get("connect_backoff_s", 0.0) + elapsed
                    stats["connect_retries"] = stats.get("connect_retries", 0) + 1

    async def aclose(self) -> None:
        await self._client.aclose()
