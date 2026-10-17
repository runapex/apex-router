"""LAN mesh model calls: one node runs a chat prompt on the local ollama of another node.

Contract: docs/DESIGN-lan-mesh.md, "Model calls". Opt-in on the serving side only
(`mesh up --serve-model NAME`). Stdlib only.

- ``validate_request`` is the one request check; the server and the client both use it.
- ``call_upstream`` (server side) posts to the local ollama OpenAI API, proxy-free, and returns
  a short reason on failure (never a traceback).
- ``ask`` (client side) signs the POST (auth header with ``body_sha256``), verifies the signed
  ``infer`` response and its ``node_id``, and never raises.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

from . import protocol
from .peers import valid_model

if TYPE_CHECKING:
    from .identity import Identity
    from .peers import Peer

INFER_PATH = "/mesh/v1/infer"
AUTH_HEADER = "X-Apex-Mesh"
MAX_REQUEST_BYTES = 256 * 1024             # request body cap (server reads at most this)
MAX_UPSTREAM_BYTES = 4 << 20               # ollama response cap
MAX_RESPONSE_BYTES = 32 << 20              # signed `infer` envelope cap (JSON escaping grows text)
MAX_ERROR_BYTES = 4096
MAX_MESSAGES = 64
MAX_TOKENS = 8192
MAX_TEMPERATURE = 2.0
ROLES = frozenset({"system", "user", "assistant"})
REQUEST_KEYS = frozenset({"model", "messages", "max_tokens", "temperature"})
UPSTREAM_TIMEOUT_S = 600.0
DEFAULT_OLLAMA = "http://127.0.0.1:11434"
OLLAMA_ENV = "APEX_MESH_OLLAMA"
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
FINISH_MAX = 32

# Responses are signed envelopes; remember their nonces so a replayed response is rejected.
_RESPONSE_GUARD = protocol.ReplayGuard()
# The upstream is local; never send a prompt through an HTTP(S)_PROXY from the environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RequestError(ValueError):
    """A request body failed a check. ``code`` is the short error for the 400 response."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def validate_request(doc: Any) -> dict:
    """Check a request dict. Return the normalized dict; raise RequestError(code).

    Keys: model (model name), messages (1..64 of {role in system|user|assistant, content str}),
    max_tokens (int 1..8192), temperature (optional, number 0..2). No other keys.
    """
    if not isinstance(doc, dict) or set(doc) - REQUEST_KEYS:
        raise RequestError("bad_request")
    if not valid_model(doc.get("model")):
        raise RequestError("bad_model")
    msgs = doc.get("messages")
    if not isinstance(msgs, list) or not 1 <= len(msgs) <= MAX_MESSAGES:
        raise RequestError("bad_messages")
    clean = []
    for m in msgs:
        if not isinstance(m, dict) or set(m) != {"role", "content"} \
                or m["role"] not in ROLES or not isinstance(m["content"], str):
            raise RequestError("bad_messages")
        clean.append({"role": m["role"], "content": m["content"]})
    mt = doc.get("max_tokens")
    if not _is_int(mt) or not 1 <= mt <= MAX_TOKENS:
        raise RequestError("bad_max_tokens")
    out = {"model": doc["model"], "messages": clean, "max_tokens": mt}
    if "temperature" in doc and doc["temperature"] is not None:
        t = doc["temperature"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) \
                or not 0 <= t <= MAX_TEMPERATURE:
            raise RequestError("bad_temperature")
        out["temperature"] = t
    return out


def parse_request(raw: bytes) -> dict:
    """Parse and validate a request body. Raises RequestError."""
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RequestError("bad_json") from None
    return validate_request(doc)


# ---- server side: upstream -----------------------------------------------------------------

def ollama_url(base: str | None = None) -> str:
    """Chat completions URL from ``base`` or $APEX_MESH_OLLAMA. Raises ValueError on a bad URL."""
    raw = base if base is not None else (os.environ.get(OLLAMA_ENV) or DEFAULT_OLLAMA)
    try:
        u = urllib.parse.urlsplit(raw.strip())
        host = u.hostname
        _ = u.port                          # raises ValueError on a bad port
    except (ValueError, AttributeError):
        raise ValueError(f"{OLLAMA_ENV} {raw!r} is not a URL") from None
    if u.scheme not in ("http", "https") or not host or u.query or u.fragment \
            or u.username or u.password:
        raise ValueError(f"{OLLAMA_ENV} {raw!r}: want http(s)://HOST[:PORT]")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}/v1/chat/completions"


class UpstreamError(Exception):
    """The upstream call failed. ``reason`` is short and safe to send (no traceback)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _short_str(v: Any, limit: int) -> str | None:
    return v if isinstance(v, str) and len(v) <= limit and v.isprintable() else None


def call_upstream(url: str, req: dict, *, timeout: float = UPSTREAM_TIMEOUT_S,
                  opener: Any = None) -> dict:
    """POST ``req`` (validated) to ollama. Return {content, reasoning, finish_reason, usage}.

    Raises UpstreamError with a short reason: upstream_http_<code>, upstream_unreachable,
    upstream_timeout, upstream_too_large, upstream_bad_response.
    """
    payload = dict(req)
    payload["stream"] = False
    data = json.dumps(payload).encode("utf-8")
    hreq = urllib.request.Request(url, data=data, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with (opener or _OPENER).open(hreq, timeout=timeout) as resp:
            raw = resp.read(MAX_UPSTREAM_BYTES + 1)
    except urllib.error.HTTPError as e:
        code = e.code if _is_int(e.code) and 100 <= e.code <= 599 else 0
        raise UpstreamError(f"upstream_http_{code}") from None
    except TimeoutError:
        raise UpstreamError("upstream_timeout") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise UpstreamError("upstream_timeout") from None
        raise UpstreamError("upstream_unreachable") from None
    except OSError:
        raise UpstreamError("upstream_unreachable") from None
    except Exception:  # noqa: BLE001 -- e.g. http.client.RemoteDisconnected / IncompleteRead
        raise UpstreamError("upstream_unreachable") from None
    if len(raw) > MAX_UPSTREAM_BYTES:
        raise UpstreamError("upstream_too_large")
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        choice = doc["choices"][0]
        msg = choice["message"]
        content = msg.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise TypeError("content")
        reasoning = msg.get("reasoning")
        if reasoning is None:
            reasoning = msg.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            reasoning = None
        finish = _short_str(choice.get("finish_reason"), FINISH_MAX)
        usage_in = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
        usage = {k: usage_in[k] for k in USAGE_KEYS
                 if _is_int(usage_in.get(k)) and 0 <= usage_in[k] <= 10**9}
    except Exception:  # noqa: BLE001 -- any shape problem is one short reason
        raise UpstreamError("upstream_bad_response") from None
    return {"content": content, "reasoning": reasoning, "finish_reason": finish, "usage": usage}


# ---- client side ---------------------------------------------------------------------------

def _result(peer: Any, model: Any, ok: bool, error: str | None = None, **kw: Any) -> dict:
    out = {"ok": ok, "node_id": getattr(peer, "node_id", None), "name": getattr(peer, "name", None),
           "model": model if isinstance(model, str) else None, "content": None,
           "reasoning": None, "finish_reason": None, "usage": {}, "latency_s": None}
    out.update(kw)
    if error is not None:
        out["error"] = error
    return out


def _host_for_url(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _read_capped(resp: Any, cap: int, end: float) -> bytes | None:
    """Read at most ``cap`` + 1 bytes before ``end`` (monotonic). None on the deadline."""
    chunks, size = [], 0
    while size <= cap:
        if time.monotonic() > end:
            return None
        chunk = resp.read(min(65536, cap + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _http_error_text(e: urllib.error.HTTPError) -> str:
    code = f"http {e.code}"
    try:
        doc = json.loads(e.read(MAX_ERROR_BYTES).decode("utf-8"))
        err = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(err, str) and 0 < len(err) <= 64 and err.replace("_", "").isalnum():
            return f"{code} {err}"
    except Exception:  # noqa: BLE001 -- the code alone is enough
        return code
    return code


def ask(peer: Peer, ident: Identity, model: str, messages: list, max_tokens: int,
        temperature: float | None = None, timeout: float = UPSTREAM_TIMEOUT_S, *,
        opener: Any = None) -> dict:
    """POST /mesh/v1/infer to ``peer``. Return a result dict; never raises.

    Result: {ok, node_id, name, model, content, reasoning, finish_reason, usage, latency_s,
    error?}. ``latency_s`` is the server-side upstream time. The response must be a signed
    envelope of kind ``infer`` whose ``body.node_id`` equals ``peer.node_id``.
    """
    try:
        req: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            req["temperature"] = temperature
        try:
            req = validate_request(req)
        except RequestError as e:
            return _result(peer, model, False, e.code)
        body = json.dumps(req, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            return _result(peer, model, False, "too_large")
        url = f"http://{_host_for_url(peer.host)}:{int(peer.port)}{INFER_PATH}"
        hdr = protocol.auth_header(ident, INFER_PATH, peer.node_id,
                                   body_sha256=hashlib.sha256(body).hexdigest())
        hreq = urllib.request.Request(url, data=body, method="POST",
                                      headers={AUTH_HEADER: hdr,
                                               "Content-Type": "application/json"})
        end = time.monotonic() + float(timeout)
        try:
            with (opener or _OPENER).open(hreq, timeout=timeout) as resp:
                raw = _read_capped(resp, MAX_RESPONSE_BYTES, end)
        except urllib.error.HTTPError as e:
            return _result(peer, model, False, _http_error_text(e))
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, TimeoutError) or isinstance(e, TimeoutError):
                return _result(peer, model, False, "timeout")
            return _result(peer, model, False, f"unreachable: {type(reason).__name__}")
        if raw is None:
            return _result(peer, model, False, "timeout")
        try:
            env = protocol.decode(raw, ident, _RESPONSE_GUARD, max_bytes=MAX_RESPONSE_BYTES)
        except protocol.ProtocolError as e:
            return _result(peer, model, False, f"bad response: {e}")
        if env["kind"] != "infer":
            return _result(peer, model, False, f"bad response: kind {env['kind']!r}")
        b = env["body"]
        if b.get("node_id") != peer.node_id:
            return _result(peer, model, False, "bad response: node_id mismatch")
        if b.get("model") != req["model"]:
            return _result(peer, model, False, "bad response: model mismatch")
        content, reasoning = b.get("content"), b.get("reasoning")
        if not isinstance(content, str):
            return _result(peer, model, False, "bad response: content")
        if reasoning is not None and not isinstance(reasoning, str):
            return _result(peer, model, False, "bad response: reasoning")
        usage = b.get("usage") if isinstance(b.get("usage"), dict) else {}
        usage = {k: usage[k] for k in USAGE_KEYS if _is_int(usage.get(k)) and usage[k] >= 0}
        lat = b.get("latency_s")
        lat = float(lat) if isinstance(lat, (int, float)) and not isinstance(lat, bool) \
            and math.isfinite(lat) and lat >= 0 else None
        return _result(peer, model, True, content=content, reasoning=reasoning,
                       finish_reason=_short_str(b.get("finish_reason"), FINISH_MAX),
                       usage=usage, latency_s=lat)
    except Exception as e:  # noqa: BLE001 -- contract: ask never raises
        return _result(peer, model, False, f"error: {type(e).__name__}")
