#!/usr/bin/env python3
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl, json, os, socket, time
from pathlib import Path
from typing import Any
from urllib import request, error

from . import local_tier

ROOT = Path(os.environ.get("ORNITH_ROOT", Path(__file__).resolve().parent))
# Defaults now describe OLLAMA (:11434), not the retired MLX server (:8080). ORNITH_URL still wins,
# so a deployment fronting the model with a proxy is unaffected.
BASE_URL = os.environ.get("ORNITH_URL", local_tier.DEFAULT_URL).rstrip("/")
LOCK = Path.home() / ".cache/ornith/request.lock"
MAINTENANCE = ROOT / "state/maintenance"

JOBS_INBOX = ROOT / "jobs/inbox"
JOBS_RUNNING = ROOT / "jobs/running"
JOBS_DONE = ROOT / "jobs/done"
JOBS_FAILED = ROOT / "jobs/failed"

MAX_ITEM_BYTES = 100_000

def _env_num(name, default, cast):
    """Parse a numeric env var, falling back to the default on a bad value — so a typo'd
    env var can never crash `import` (Codex #7)."""
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


# Real inference gets the long timeout; probes get their own short ones.
INFER_TIMEOUT = _env_num("ORNITH_SOCKET_TIMEOUT_SECS", "900", float)
READY_TIMEOUT = _env_num("ORNITH_READY_TIMEOUT_SECS", "30", float)
LIVE_TIMEOUT = _env_num("ORNITH_LIVE_TIMEOUT_SECS", "5", float)
STARTUP_RETRIES = _env_num("ORNITH_STARTUP_RETRIES", "12", int)

# Backend profile. The backend is now ollama, which REQUIRES an explicit `model` (the MLX server
# let clients omit it and used its own start-time default) and gates thinking with
# `reasoning_effort`. Both still come from env first; the fallbacks come from the active tier so
# there is one source of truth for "which local model is live" (see local_tier).
# ORNITH_API_MODEL (the API model id) stays deliberately distinct from ORNITH_MODEL (the retired
# MLX filesystem path), so that path can never leak into the API `model` field.
MODEL = os.environ.get("ORNITH_API_MODEL") or local_tier.resolve().api_model
THINKING_STYLE = os.environ.get("ORNITH_THINKING_STYLE", local_tier.THINKING_STYLE)

# Liveness probe path. The MLX server exposed /health returning {"status":"ok"}; ollama has no
# /health at all (404) and answers /v1/models with an OpenAI model list. Probing the wrong one
# reports a healthy server as dead, which strands every lane. Kept configurable for proxied setups.
HEALTH_PATH = os.environ.get("ORNITH_HEALTH_PATH", "/v1/models")


def _apply_backend(body: dict, *, enable_thinking: bool, model: str | None = None) -> dict:
    """Shape the request body for the configured backend, in place.

    `model` overrides the module-level MODEL for ONE call. That override exists because MODEL binds
    at import: without it, a caller holding a Route that asks for the other tier could not act on it
    without restarting the process, and would silently get whichever tier was bound at startup.
    Note this selects a model, it does not LOAD one — ollama will pull the weights in on first use,
    so an unwarmed tier pays a multi-GB cold start on this request.
    """
    chosen = model or MODEL
    if chosen:
        body["model"] = chosen
    if THINKING_STYLE == "reasoning_effort":
        if not enable_thinking:
            body["reasoning_effort"] = "none"
    else:
        body["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
    return body


class OrnithError(RuntimeError): pass
class OrnithProtocolError(OrnithError): pass          # bad/empty/truncated answer
class OrnithUnavailable(OrnithError): pass            # base: can't serve right now
class OrnithMaintenance(OrnithUnavailable): pass      # maintenance marker present
class OrnithNotListening(OrnithUnavailable): pass     # conn refused before connect
class OrnithAmbiguousFailure(OrnithUnavailable): pass # timeout/reset AFTER send


@dataclass(frozen=True)
class ChatResult:
    answer: str
    reasoning: str | None
    finish_reason: str | None
    usage: dict[str, int] | None
    raw: dict[str, Any]


@contextmanager
def inference_lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _request(method: str, path: str, body, timeout: float):
    data = None if body is None else json.dumps(body).encode()
    req = request.Request(BASE_URL + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(path: str, *, timeout: float, retries: int | None = None):
    """GET only. May retry connection-refused during cold start.
    retries=0 → single attempt (used by the fast liveness health probe)."""
    attempts = STARTUP_RETRIES if retries is None else retries
    last_reason = None
    for attempt in range(attempts + 1):
        try:
            return _request("GET", path, None, timeout)
        except error.HTTPError as e:
            raise OrnithError(f"HTTP {e.code}: {e.read().decode(errors='replace')}") from e
        except error.URLError as e:
            last_reason = e.reason
            if isinstance(e.reason, ConnectionRefusedError) and attempt < attempts:
                time.sleep(min(0.5 * (2 ** attempt), 10)); continue
            raise OrnithNotListening(f"{BASE_URL}{path}: {e.reason}") from e
        except (socket.timeout, TimeoutError) as e:
            raise OrnithUnavailable(f"GET {path} timed out: {e}") from e
    raise OrnithNotListening(f"{BASE_URL}{path}: {last_reason}")  # unreachable; satisfies type-checker


def _post(path: str, body: dict, *, timeout: float):
    """POST once. NEVER retried — an inference request must not be replayed."""
    try:
        return _request("POST", path, body, timeout)
    except error.HTTPError as e:
        raise OrnithError(f"HTTP {e.code}: {e.read().decode(errors='replace')}") from e
    except error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            raise OrnithNotListening(f"{BASE_URL}{path}: {e.reason}") from e
        raise OrnithAmbiguousFailure(f"POST {path} failed after send: {e.reason}") from e
    except (socket.timeout, TimeoutError, ConnectionResetError, BrokenPipeError) as e:
        raise OrnithAmbiguousFailure(f"POST {path} failed after send: {e}") from e


def _parse(payload: dict[str, Any]) -> ChatResult:
    try:
        choice = payload["choices"][0]; msg = choice["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise OrnithProtocolError(f"Unexpected response: {payload!r}") from e
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning") or msg.get("reasoning_content")
    if "</think>" in content:
        legacy, content = content.split("</think>", 1)
        reasoning = reasoning or legacy.removeprefix("<think>").strip()
    if not content.strip():
        raise OrnithProtocolError("No final answer (empty content)")
    return ChatResult(content.strip(), reasoning, choice.get("finish_reason"),
                      payload.get("usage"), payload)


def chat_messages(messages, *, max_tokens=4096, enable_thinking=True,
                  temperature=0.3, top_p=0.95, raise_on_truncation=True,
                  model: str | None = None) -> ChatResult:
    """raise_on_truncation (default True): a finish_reason=length answer raises OrnithProtocolError —
    correct for codegen/extraction where a cut-off answer is useless. Callers whose PARTIAL output is
    still valuable (e.g. the review pre-filter, where partial findings still escalate usefully) pass
    False to receive the truncated ChatResult instead of an exception.

    model: send THIS tier's model id instead of the module-level one — pass `route.model` to honour
    a model_router verdict without restarting the process. Selecting an unwarmed tier makes this
    request pay the cold load."""
    if MAINTENANCE.exists():
        raise OrnithMaintenance("Scheduled maintenance")
    body = _apply_backend(
        {"messages": messages, "max_tokens": max_tokens,
         "temperature": temperature, "top_p": top_p},
        enable_thinking=enable_thinking, model=model)
    with inference_lock():
        if MAINTENANCE.exists():
            raise OrnithMaintenance("Scheduled maintenance")
        result = _parse(_post("/v1/chat/completions", body, timeout=INFER_TIMEOUT))
    if result.finish_reason == "length" and raise_on_truncation:
        raise OrnithProtocolError("Answer truncated (finish_reason=length)")
    return result


def chat(prompt: str, **kwargs) -> ChatResult:
    return chat_messages([{"role": "user", "content": prompt}], **kwargs)


def _is_healthy(payload) -> bool:
    """Accept EITHER health shape, so one probe covers both backends and any proxy in front:
      - MLX/vLLM  /health     -> {"status": "ok"}
      - ollama    /v1/models  -> {"object": "list", "data": [...]}
    An empty `data` list is still 'up': ollama with no model pulled is a live server, and treating
    it as dead would send callers hunting a network fault that isn't there.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("status") == "ok":
        return True
    return isinstance(payload.get("data"), list)


def liveness() -> bool:
    """Process is up. GET HEALTH_PATH, short timeout, NO retry, no lock."""
    try:
        return _is_healthy(_get(HEALTH_PATH, timeout=LIVE_TIMEOUT, retries=0))
    except OrnithError:
        return False


def readiness() -> bool:
    """Actually generates. Thinking OFF, own short timeout, lock-free.
    Call only in startup/maintenance windows, never as a mid-load poll."""
    body = _apply_backend(
        {"messages": [{"role": "user", "content": "Reply exactly: OK"}],
         "max_tokens": 8, "temperature": 0.0, "top_p": 1.0},
        enable_thinking=False)
    try:
        return bool(_parse(_post("/v1/chat/completions", body,
                                 timeout=READY_TIMEOUT)).answer)
    except OrnithError:
        return False


if __name__ == "__main__":
    print(chat("Reply exactly: Ornith ready", max_tokens=32).answer)
