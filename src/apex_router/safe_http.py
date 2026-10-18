"""One opener for outbound requests that carry a credential header.

The stdlib redirect handler copies every request header to the redirect target, so a hostile or
misconfigured endpoint that answers `302 Location: https://attacker/...` (or `http://same-host/...`)
receives the API key. `urlopen` here follows redirects only with `NoAuthRedirect`, which drops the
credential headers whenever the origin (scheme, host, port) changes.
"""
from __future__ import annotations

import urllib.parse
import urllib.request

SENSITIVE_HEADERS = ("authorization", "proxy-authorization", "x-api-key", "api-key",
                     "ocp-apim-subscription-key", "cookie")


def strip_auth_headers(headers) -> dict:
    """Drop credential headers. Accepts any mapping (a Request's headers are a MutableMapping)."""
    return {k: v for k, v in dict(headers).items() if str(k).lower() not in SENSITIVE_HEADERS}


def origin(url: str) -> tuple:
    """(scheme, host, port) with the default port filled in: the browser notion of an origin."""
    u = urllib.parse.urlsplit(url)
    scheme = u.scheme.lower()
    try:
        port = u.port
    except ValueError:
        port = None
    return scheme, (u.hostname or "").lower(), port or {"https": 443, "http": 80}.get(scheme)


class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, but without credential headers when the origin changes."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and origin(req.full_url) != origin(newurl):
            new.headers = strip_auth_headers(new.headers)
            new.unredirected_hdrs = strip_auth_headers(new.unredirected_hdrs)
        return new


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(NoAuthRedirect())


def urlopen(req, timeout: float):
    """Drop-in for urllib.request.urlopen(req, timeout=...) for credentialed requests."""
    return opener().open(req, timeout=timeout)
