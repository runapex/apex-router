"""Azure AD bearer-token provider for the measuring proxy (opt-in).

Some deployments front the Anthropic API with an Azure API Management gateway that authenticates
with a short-lived Azure AD token (`aud=https://cognitiveservices.azure.com`, ~1h TTL). The proxy
is byte-identical passthrough, so a client that already sends its own `Authorization`/`x-api-key`
is never touched. But a client with NO auth of its own — notably a BACKGROUND daemon that has no
interactive shell to carry `ANTHROPIC_FOUNDRY_AUTH_TOKEN` — can have the proxy MINT and inject a
fresh token, refreshed automatically as `az login` rotates credentials. No restart, no baked secret.

Design (the non-obvious calls):
  - **Opt-in.** Active only when `APEX_INJECT_AZURE_TOKEN` is truthy (default OFF → pure
    passthrough, so the public default and every existing deployment are unchanged).
  - **Fresh, never baked.** Mints via `az account get-access-token --resource <res>` on demand and
    caches the token only until ~`skew_s` before its OWN `exp` (decoded from the JWT). This is the
    whole point of "after login": a token baked into a plist/env is stale within the hour, but a
    provider that re-mints past cache-expiry picks up whatever `az login` last wrote to ~/.azure —
    the daemon never sees the old credentials again.
  - **Fail-open.** A mint failure raises to the caller, which forwards the request UNCHANGED and
    lets the upstream return its own 401 — the proxy never invents an error or drops traffic.

Pure stdlib + subprocess (`az`); no azure SDK, so the [proxy] extra stays lean.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import subprocess
import threading
import time

# Default audience for the Anthropic-via-Azure gateway (the `aud` the gateway validates). Overridable
# via config (APEX_AZURE_TOKEN_RESOURCE) — nothing tenant-specific is hardcoded beyond this public
# Cognitive Services resource id, which is the same for every Azure tenant.
DEFAULT_RESOURCE = "https://cognitiveservices.azure.com"


def _decode_jwt_exp(token: str) -> float | None:
    """Return the JWT's `exp` (epoch seconds) from its unverified payload, or None if unreadable.

    We do NOT verify the signature — the gateway does that. We only read `exp` to know when to
    re-mint, so a best-effort decode is correct: any failure falls back to the caller's own expiry
    source. base64url needs padding restored before decoding.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore stripped base64url padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 — a malformed token just means "no exp hint"; caller falls back
        return None


def _coerce_expiry(data: dict) -> float:
    """Best-effort expiry (epoch seconds) from az's non-JWT fields, for the rare opaque token.

    Modern az emits `expires_on` (epoch int). Older az emits `expiresOn` as a LOCAL-time datetime
    string ("YYYY-MM-DD HH:MM:SS[.ffffff]") — `float()` on that raises, so parse it explicitly rather
    than silently inventing a lifetime. Last resort (nothing parseable) is a window comfortably LARGER
    than the skew, so a miss doesn't make `get_token` re-mint on every single call (a thrash convoy).
    """
    eo = data.get("expires_on")
    if eo is not None:
        try:
            return float(eo)
        except (TypeError, ValueError):
            pass
    s = data.get("expiresOn")
    if isinstance(s, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return _dt.datetime.strptime(s.strip(), fmt).timestamp()  # naive == local time
            except ValueError:
                continue
    return time.time() + 1800.0


class AzureTokenProvider:
    """Caches one Azure AD token per resource, re-minting only when it's within `skew_s` of expiry.

    Thread-safe: `get_token()` mints under a lock so concurrent requests don't spawn a herd of `az`
    subprocesses on a cold/expired cache. Minting is blocking (a subprocess), so the async wrapper
    `token_async()` offloads it to a thread — the event loop is never blocked on `az`.
    """

    def __init__(self, resource: str = DEFAULT_RESOURCE, az_bin: str = "az",
                 skew_s: int = 300, timeout_s: float = 30.0, retry_backoff_s: float = 30.0) -> None:
        self._resource = resource
        self._az_bin = az_bin
        self._skew_s = skew_s          # re-mint this many seconds BEFORE the real exp
        self._timeout_s = timeout_s
        self._retry_backoff_s = retry_backoff_s  # after a failed mint, don't re-attempt for this long
        self._token: str | None = None
        self._exp: float = 0.0         # epoch seconds; 0 → nothing cached yet
        self._next_retry: float = 0.0  # earliest time a re-mint may be attempted after a failure
        self._lock = threading.Lock()

    def _mint(self) -> tuple[str, float]:
        """Run `az account get-access-token` and return (token, exp_epoch). Raises on any failure."""
        proc = subprocess.run(
            [self._az_bin, "account", "get-access-token",
             "--resource", self._resource, "--output", "json"],
            capture_output=True, text=True, timeout=self._timeout_s,
        )
        if proc.returncode != 0:
            # Surface az's own message (e.g. "az login" required) — the caller logs it and fails open.
            raise RuntimeError(
                f"`az account get-access-token` exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[:300]}")
        data = json.loads(proc.stdout)
        token = data.get("accessToken")
        if not isinstance(token, str) or not token:
            raise RuntimeError("az returned no usable accessToken")
        # Prefer the token's own `exp` (unambiguous UTC, exactly what the gateway checks); fall back
        # to az's expiry fields only if the JWT is opaque for some reason.
        exp = _decode_jwt_exp(token)
        if exp is None:
            exp = _coerce_expiry(data)
        return token, exp

    def get_token(self) -> str:
        """Return a valid token, minting a fresh one if the cache is empty or near expiry.

        Graceful degradation on refresh failure: inside the skew window the cached token is still
        VALID (its hard `exp` hasn't passed), so if a re-mint fails we keep serving the cached token
        rather than failing every caller through the whole skew window. We only propagate the error
        when there is no usable token left (cache empty, or the cached token is truly past `exp`).
        """
        now = time.time()
        with self._lock:
            if self._token is not None and now < self._exp - self._skew_s:
                return self._token
            # A recent refresh failed → don't stampede `az`: while the cached token is still valid,
            # only ONE caller per backoff window re-attempts a mint; the rest serve the cached token
            # immediately (no serialized convoy of 30s `az` calls exhausting the thread pool).
            if self._token is not None and now < self._next_retry and now < self._exp:
                return self._token
            try:
                token, exp = self._mint()
            except Exception:
                self._next_retry = time.time() + self._retry_backoff_s
                # Re-read the clock: _mint() may have blocked for seconds, so the cached token could
                # have crossed its hard exp during the attempt — never hand back an expired token.
                if self._token is not None and time.time() < self._exp:
                    return self._token
                raise
            self._next_retry = 0.0
            self._token, self._exp = token, exp
            return token

    async def token_async(self) -> str:
        """Async wrapper — mints off the event loop (the `az` subprocess is blocking)."""
        import asyncio
        return await asyncio.to_thread(self.get_token)


# One provider per (resource, az_bin) so the cache is shared across all requests in the process.
_providers: dict[tuple[str, str], AzureTokenProvider] = {}
_providers_lock = threading.Lock()


def get_provider(resource: str = DEFAULT_RESOURCE, az_bin: str = "az") -> AzureTokenProvider:
    key = (resource, az_bin)
    with _providers_lock:
        prov = _providers.get(key)
        if prov is None:
            prov = AzureTokenProvider(resource=resource, az_bin=az_bin)
            _providers[key] = prov
        return prov
