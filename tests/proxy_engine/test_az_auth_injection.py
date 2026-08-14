"""Azure-AD auth injection — the opt-in token provider + the strict-superset inject_auth path.

Invariants under test:
  1. The provider mints via `az` once, caches until ~exp-skew, then re-mints (so a fresh `az login`
     is picked up without a restart), and on a refresh FAILURE keeps serving a still-valid cached
     token rather than failing every caller.
  2. `Upstream.inject_auth` is a STRICT SUPERSET of passthrough: it appends `Authorization` ONLY when
     injection is enabled AND this is the anthropic wire AND the upstream is a NON-public gateway AND
     the client (per its RAW headers) sent no auth of its own — and is a byte-identical no-op
     otherwise, including on a mint failure (fail-open).
  3. `_env_bool` only enables on affirmative tokens ('FALSE'/'no'/typo => disabled).

The `az` subprocess and the provider are stubbed — no live Azure, no real `az`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

from apex_router.proxy_engine.config import Config, _env_bool
from apex_router.proxy_engine.proxy import az_auth
from apex_router.proxy_engine.proxy.upstream import Upstream

GW = "https://gw.example/claude"  # a NON-public gateway upstream (injection only fires toward this)


def _jwt(exp: float) -> str:
    """A minimal unsigned JWT carrying just `exp` — enough for the provider's expiry decode."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"h.{payload}.s"


# --------------------------------------------------------------------------- provider

def _stub_az(monkeypatch, exp: float, counter: list[int]):
    """Replace subprocess.run so `_mint` returns a token whose JWT exp == `exp`; count invocations."""
    class _Proc:
        returncode = 0
        stdout = json.dumps({"accessToken": _jwt(exp), "tokenType": "Bearer"})
        stderr = ""

    def fake_run(*_a, **_k):
        counter[0] += 1
        return _Proc()

    monkeypatch.setattr(az_auth.subprocess, "run", fake_run)


def test_provider_caches_until_near_expiry(monkeypatch):
    calls = [0]
    _stub_az(monkeypatch, exp=time.time() + 3600, counter=calls)
    p = az_auth.AzureTokenProvider(resource="r", az_bin="az", skew_s=300)
    t1 = p.get_token()
    t2 = p.get_token()
    assert t1 == t2
    assert calls[0] == 1, "a valid cached token must not re-mint"


def test_provider_remints_when_within_skew(monkeypatch):
    calls = [0]
    _stub_az(monkeypatch, exp=time.time() + 100, counter=calls)  # inside a 300s skew → re-mint
    p = az_auth.AzureTokenProvider(resource="r", az_bin="az", skew_s=300)
    p.get_token()
    p.get_token()
    assert calls[0] == 2, "a token inside the re-mint skew must be refreshed each call"


def test_provider_raises_on_az_failure(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "Please run 'az login'"

    monkeypatch.setattr(az_auth.subprocess, "run", lambda *a, **k: _Proc())
    p = az_auth.AzureTokenProvider()
    try:
        p.get_token()
        assert False, "expected a RuntimeError on az failure with no cached token"
    except RuntimeError as e:
        assert "az login" in str(e)


def test_refresh_failure_falls_back_to_valid_cached_token(monkeypatch):
    # After a good mint, a subsequent mint FAILURE inside the skew window must keep serving the
    # still-valid cached token (graceful degradation), not raise / drop auth.
    good = _jwt(time.time() + 200)  # valid, but inside a 300s skew → next get_token tries to re-mint
    calls = [0]

    def flaky_run(*_a, **_k):
        calls[0] += 1

        class _Ok:
            returncode = 0
            stdout = json.dumps({"accessToken": good})
            stderr = ""

        class _Fail:
            returncode = 1
            stdout = ""
            stderr = "az login required"

        return _Ok() if calls[0] == 1 else _Fail()

    monkeypatch.setattr(az_auth.subprocess, "run", flaky_run)
    p = az_auth.AzureTokenProvider(resource="r", skew_s=300)
    t1 = p.get_token()   # mint #1 ok, cached
    t2 = p.get_token()   # in skew → mint #2 FAILS → fall back to still-valid cached token
    assert t1 == t2 == good
    assert calls[0] == 2, "should have attempted a re-mint, then reused the cached token"


def test_decode_jwt_exp_roundtrip():
    assert az_auth._decode_jwt_exp(_jwt(1786749348.0)) == 1786749348.0
    assert az_auth._decode_jwt_exp("not-a-jwt") is None


def test_coerce_expiry_handles_epoch_and_datetime_string():
    assert az_auth._coerce_expiry({"expires_on": 1786749348}) == 1786749348.0
    # legacy az datetime-string form must parse, not silently invent a lifetime
    got = az_auth._coerce_expiry({"expiresOn": "2026-08-14 17:14:15"})
    assert got > 0
    # nothing parseable → a window LARGER than the default skew (no re-mint thrash)
    assert az_auth._coerce_expiry({}) > time.time() + 300


# --------------------------------------------------------------------------- config bool parse

def test_env_bool_only_affirmative_enables(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv("X_FLAG", v)
        assert _env_bool("X_FLAG") is True, v
    for v in ("0", "false", "False", "FALSE", "no", "off", ""):
        monkeypatch.setenv("X_FLAG", v)
        assert _env_bool("X_FLAG") is False, v
    monkeypatch.delenv("X_FLAG", raising=False)
    assert _env_bool("X_FLAG") is False  # unset default


# --------------------------------------------------------------------------- inject_auth superset

class _FakeProvider:
    async def token_async(self):
        return "MINTED"


def _inject(cfg, headers, client_kind, raw_headers=None):
    """Run inject_auth; `raw_headers` defaults to `headers` (the client-auth check reads the raw set)."""
    u = Upstream(cfg)
    try:
        return asyncio.run(u.inject_auth(
            headers, client_kind, raw_headers=headers if raw_headers is None else raw_headers))
    finally:
        asyncio.run(u.aclose())


def test_no_injection_when_disabled():
    cfg = Config(inject_azure_token=False, anthropic_upstream=GW)
    hdrs = [(b"anthropic-version", b"2023-06-01")]
    assert _inject(cfg, hdrs, "claude-code") == hdrs  # untouched


def test_no_injection_when_client_already_authed(monkeypatch):
    monkeypatch.setattr(az_auth, "get_provider", lambda *a, **k: _FakeProvider())
    cfg = Config(inject_azure_token=True, anthropic_upstream=GW)
    # Authorization, x-api-key, AND the Azure/APIM alternatives all suppress injection.
    for auth in ([(b"authorization", b"Bearer client")], [(b"x-api-key", b"sk-abc")],
                 [(b"ocp-apim-subscription-key", b"key")], [(b"api-key", b"key")]):
        assert _inject(cfg, list(auth), "claude-code") == auth  # never override the client's own


def test_no_injection_on_codex_wire(monkeypatch):
    monkeypatch.setattr(az_auth, "get_provider", lambda *a, **k: _FakeProvider())
    cfg = Config(inject_azure_token=True, anthropic_upstream=GW)
    hdrs = [(b"content-type", b"application/json")]
    assert _inject(cfg, hdrs, "codex") == hdrs  # openai wire has its own auth


def test_no_injection_toward_public_provider_host(monkeypatch):
    # SECURITY: never attach the Azure bearer to the public provider default, even enabled+unauthed —
    # that would disclose the Cognitive Services token to api.anthropic.com.
    monkeypatch.setattr(az_auth, "get_provider", lambda *a, **k: _FakeProvider())
    cfg = Config(inject_azure_token=True)  # default upstream == https://api.anthropic.com
    hdrs = [(b"anthropic-version", b"2023-06-01")]
    assert _inject(cfg, hdrs, "claude-code") == hdrs  # untouched — no token leaked to Anthropic


def test_connection_stripped_auth_is_not_overridden(monkeypatch):
    # A client sending its own Authorization + `Connection: authorization` has that header removed by
    # filtering; the injector must still SEE it (raw headers) and NOT inject the proxy bearer over it.
    monkeypatch.setattr(az_auth, "get_provider", lambda *a, **k: _FakeProvider())
    cfg = Config(inject_azure_token=True, anthropic_upstream=GW)
    raw = [(b"authorization", b"Bearer client"), (b"connection", b"authorization")]
    filtered = [(b"anthropic-version", b"2023-06-01")]  # what filtering leaves (auth dropped)
    assert _inject(cfg, filtered, "claude-code", raw_headers=raw) == filtered  # no proxy bearer added


def test_injects_bearer_when_enabled_unauthed_and_nonpublic_upstream(monkeypatch):
    monkeypatch.setattr(az_auth, "get_provider", lambda *a, **k: _FakeProvider())
    cfg = Config(inject_azure_token=True, anthropic_upstream=GW)
    hdrs = [(b"anthropic-version", b"2023-06-01")]
    out = _inject(cfg, hdrs, "claude-code")
    assert (b"authorization", b"Bearer MINTED") in out
    assert (b"anthropic-version", b"2023-06-01") in out  # existing headers preserved


def test_mint_failure_is_fail_open(monkeypatch):
    class _Boom:
        async def token_async(self):
            raise RuntimeError("az login required")

    monkeypatch.setattr(az_auth, "get_provider", lambda *a, **k: _Boom())
    cfg = Config(inject_azure_token=True, anthropic_upstream=GW)
    hdrs = [(b"anthropic-version", b"2023-06-01")]
    # fail-open: forward unchanged (upstream will 401), never raise into the request path
    assert _inject(cfg, hdrs, "claude-code") == hdrs


def test_empty_auth_header_does_not_suppress_injection(monkeypatch):
    # An empty/whitespace authorization is not a real credential — it must NOT block injection.
    monkeypatch.setattr(az_auth, "get_provider", lambda *a, **k: _FakeProvider())
    cfg = Config(inject_azure_token=True, anthropic_upstream=GW)
    fwd = [(b"content-type", b"application/json")]
    raw = [(b"authorization", b"   "), (b"content-type", b"application/json")]
    out = _inject(cfg, fwd, "claude-code", raw_headers=raw)
    assert (b"authorization", b"Bearer MINTED") in out


def test_trailing_dot_public_host_still_blocked(monkeypatch):
    # SECURITY: `api.anthropic.com.` (FQDN trailing dot) is the same host — still deny injection.
    monkeypatch.setattr(az_auth, "get_provider", lambda *a, **k: _FakeProvider())
    cfg = Config(inject_azure_token=True, anthropic_upstream="https://api.anthropic.com./v1")
    hdrs = [(b"anthropic-version", b"2023-06-01")]
    assert _inject(cfg, hdrs, "claude-code") == hdrs  # untouched — no token leaked


def test_failed_refresh_backoff_avoids_remint_stampede(monkeypatch):
    # After a failed refresh, callers inside the backoff window serve the cached token WITHOUT each
    # firing another `az` mint (no serialized 30s convoy under an az outage).
    good = _jwt(time.time() + 200)  # valid but inside the 300s skew → get_token tries to re-mint
    calls = [0]

    def flaky(*_a, **_k):
        calls[0] += 1

        class _Ok:
            returncode = 0
            stdout = json.dumps({"accessToken": good})
            stderr = ""

        class _Fail:
            returncode = 1
            stdout = ""
            stderr = "down"

        return _Ok() if calls[0] == 1 else _Fail()

    monkeypatch.setattr(az_auth.subprocess, "run", flaky)
    p = az_auth.AzureTokenProvider(resource="r", skew_s=300, retry_backoff_s=999)
    p.get_token()   # mint #1 ok (calls=1)
    p.get_token()   # in skew → mint #2 FAILS, sets backoff, serves cached (calls=2)
    p.get_token()   # within backoff → serve cached, NO new mint attempt
    assert calls[0] == 2, "backoff must prevent a re-mint stampede"
