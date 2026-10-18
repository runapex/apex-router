"""Credentials never follow a redirect to another origin, on any outbound client that sends one.

safe_http.urlopen is the one opener for requests that carry a credential header. The guard test
fails if a source file builds such a request and still calls the stdlib urlopen, whose redirect
handler copies every header to the new URL.
"""
import io
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from apex_router import safe_http  # noqa: E402
from apex_router.remote import deepseek_client  # noqa: E402

_CRED = re.compile(r"[\"'](authorization|x-api-key|api-key|ocp-apim-subscription-key)[\"']\s*:", re.I)


def _redirected(src, dst, extra=None):
    hdrs = {"Authorization": "Bearer secret", "X-Api-Key": "k", "Accept": "application/json", **(extra or {})}
    req = urllib.request.Request(src, headers=hdrs)
    new = safe_http.NoAuthRedirect().redirect_request(req, io.BytesIO(), 302, "Found", {}, dst)
    return {k.lower(): v for k, v in new.headers.items()}


def test_credentials_stay_only_on_the_same_origin():
    assert "authorization" in _redirected("https://api.test/a", "https://api.test:443/b")
    for dst in ("https://evil.test/a", "https://api.test:8443/a", "http://api.test/a"):
        h = _redirected("https://api.test/a", dst)
        assert "authorization" not in h and "x-api-key" not in h, dst
        assert h["accept"] == "application/json"


def test_opener_uses_the_no_auth_redirect_handler():
    handlers = safe_http.opener().handlers
    assert any(isinstance(h, safe_http.NoAuthRedirect) for h in handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in handlers)


def test_no_credentialed_request_uses_the_stdlib_urlopen():
    offenders = []
    for path in (ROOT / "src" / "apex_router").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name != "safe_http.py" and _CRED.search(text) and re.search(r"\b(urllib\.)?request\.urlopen\(", text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_deepseek_preflight_refuses_common_token_formats():
    fakes = [
        "AKIA" + "ABCDEFGHIJKLMNOP",                               # AWS access key id
        "ghp_" + "a" * 36,                                         # GitHub token
        "github_pat_" + "a" * 40,
        "xoxb-" + "1234567890-abcdefghij",                         # Slack bot token
        "sk-ant-api03-" + "a" * 40,                                # Anthropic key
        "sk-proj-" + "a" * 40,                                     # OpenAI project key
        "Authorization: Bearer " + "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop",   # bare JWT
    ]
    for f in fakes:
        try:
            deepseek_client.check_text(f"context {f} more")
        except deepseek_client.RemoteRefused:
            continue
        raise AssertionError(f"not refused: {f[:12]}...")


def test_deepseek_preflight_keeps_plain_text():
    deepseek_client.check_text("Refactor the parser; keep the tests green. Ask before deleting files.")
