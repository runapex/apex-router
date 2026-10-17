"""DeepSeek chat client for advisory offload. Pure stdlib; the transport is injectable for tests.

The API is hosted outside the owner's control, so `chat()` runs `check_text()` on the full
request BEFORE any network call: secrets, e-mail addresses and the overlay's `deny_regex`
patterns refuse the request (RemoteRefused). Any failure to get an answer (no key, HTTP error,
402 no credit, timeout, empty answer) is RemoteUnavailable: the caller does the work itself.

Cost: DeepSeek bills cache-hit input, cache-miss input and output at the registry `prices`
(PEAK rates); off-peak hours bill `offpeak_factor` x that. Peak = `peak_utc` windows, Mon-Fri.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import model_registry as mr

BACKEND = "deepseek"

_BUILTIN_DENY = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|"
                r"(?i:password|passwd|secret|token|api[_-]?key)[\"']?\s*[:=]\s*[\"']?[^\s\"']{6,}"), "secret"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "e-mail address"),
]


class RemoteRefused(Exception):
    """The request text failed the hygiene check; nothing was sent."""

    def __init__(self, reason: str):
        super().__init__(f"refused: the text holds a {reason}")
        self.reason = reason


class RemoteUnavailable(Exception):
    """No answer (no key, HTTP error, no credit, timeout, empty answer). `cost_usd` is what the
    failed call still billed (an empty or malformed answer that reported usage), else 0."""

    def __init__(self, reason: str, *, cost_usd: float = 0.0, cost_known: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.cost_usd = cost_usd
        # False when the request may have been processed and billed but no usage came back
        # (timeout or connection drop after send): the spend is unknown, not zero.
        self.cost_known = cost_known


@dataclass
class RemoteResult:
    answer: str
    model: str
    finish_reason: str | None
    usage: dict
    peak: bool
    cost_usd: float
    latency_ms: int
    raw: dict = field(default_factory=dict, repr=False)


def _cfg(registry: dict | None) -> dict:
    cfg = mr.remote(BACKEND, registry=registry)
    if cfg is None:
        cfg = mr.remote(BACKEND)
    return cfg or {}


def is_peak(now: datetime, *, registry: dict | None = None) -> bool:
    """True inside a `peak_utc` window on Monday-Friday (UTC, window end exclusive)."""
    now = now.astimezone(timezone.utc)
    windows = _cfg(registry).get("peak_utc") or []
    return now.weekday() < 5 and any(a <= now.hour < b for a, b in windows)


def call_cost(model: str, usage: dict, *, peak: bool, registry: dict | None = None) -> float:
    """USD for one call. Without a cache split, all prompt tokens count as cache misses."""
    p = mr.price(model, registry=registry)
    if p is None:
        raise ValueError(f"model {model!r} has no price in the registry")
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = usage.get("prompt_cache_miss_tokens")
    miss = int(miss) if miss is not None else max(int(usage.get("prompt_tokens") or 0) - hit, 0)
    out = int(usage.get("completion_tokens") or 0)
    usd = (hit * p["cached"] + miss * p["in"] + out * p["out"]) / 1e6
    factor = 1.0 if peak else float(_cfg(registry).get("offpeak_factor", 0.5))
    return usd * factor


def baseline_cost(tier: str, completion_tokens: int, *, registry: dict | None = None) -> float | None:
    """What a Claude tier would charge to WRITE the same output (gross: excludes the tokens
    Claude spends reading and checking the answer). None for an unknown tier or model."""
    p = mr.price(tier, registry=registry)
    return None if p is None else completion_tokens * p["out"] / 1e6


def check_model(model: str, *, registry: dict | None = None) -> None:
    """ValueError unless `model` is a listed DeepSeek model with a price (an unpriced call would
    book as free and inflate the saving)."""
    if model not in (_cfg(registry).get("models") or []):
        raise ValueError(f"model {model!r} is not in remote.deepseek.models")
    if mr.price(model, registry=registry) is None:
        raise ValueError(f"model {model!r} has no price in the registry")


def check_text(text: str, *, registry: dict | None = None) -> None:
    """Raise RemoteRefused if `text` holds a secret, an e-mail address or a denied pattern."""
    for rx, what in _BUILTIN_DENY:
        if rx.search(text):
            raise RemoteRefused(what)
    rules = _cfg(registry).get("deny_regex") or []
    if not isinstance(rules, list):
        raise RemoteRefused("invalid deny rule")  # a broken deny rule fails closed
    for pat in rules:
        try:
            rx = re.compile(pat)
        except (re.error, TypeError):
            raise RemoteRefused("invalid deny rule") from None
        if rx.search(text):
            raise RemoteRefused("denied pattern")


def _keychain(service: str) -> str:
    user = os.environ.get("USER") or subprocess.getoutput("id -un")
    return subprocess.run(["security", "find-generic-password", "-a", user, "-s", service, "-w"],
                          capture_output=True, text=True, check=True).stdout


def api_key(registry: dict | None = None, *, env=None, runner=None) -> str:
    """The key from env `key_env`, else the Keychain. Never printed or logged."""
    cfg = _cfg(registry)
    env = os.environ if env is None else env
    k = env.get(cfg.get("key_env", "DEEPSEEK_API_KEY"), "").strip()
    if k:
        return k
    runner = runner or (lambda cmd: _keychain(cmd[-1]))
    try:
        k = (runner(["keychain", cfg.get("keychain_service", "deepseek-api")]) or "").strip()
    except (OSError, subprocess.CalledProcessError) as e:
        raise RemoteUnavailable(f"no API key ({type(e).__name__})") from None
    if not k:
        raise RemoteUnavailable("no API key")
    return k


def chat(prompt: str, *, model: str | None = None, max_tokens: int = 4096, temperature: float = 0.3,
         registry: dict | None = None, transport=None, key: str | None = None,
         now: datetime | None = None, timeout_s: float = 300) -> RemoteResult:
    """One user-turn chat call. Refuses (before sending) text that fails `check_text`."""
    cfg = _cfg(registry)
    model = model or cfg.get("default_model", "deepseek-v4-pro")
    check_model(model, registry=registry)
    check_text(prompt, registry=registry)
    key = key or api_key(registry)
    now = now or datetime.now(timezone.utc)
    peak = is_peak(now, registry=registry)
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": temperature, "stream": False}).encode()
    req = urllib.request.Request(cfg.get("base_url", "https://api.deepseek.com").rstrip("/") + "/chat/completions",
                                 data=body, headers={"Authorization": f"Bearer {key}",
                                                     "Content-Type": "application/json"})
    send = transport or (lambda r, timeout: urllib.request.urlopen(r, timeout=timeout))
    t0 = time.monotonic()
    try:
        with send(req, timeout_s) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        note = ", no credit" if e.code == 402 else ""
        raise RemoteUnavailable(f"HTTP {e.code}{note}") from None
    except (OSError, ValueError) as e:  # URLError, timeout, bad JSON: may have billed
        raise RemoteUnavailable(type(e).__name__, cost_known=False) from None
    ms = int((time.monotonic() - t0) * 1000)
    usage = data.get("usage") if isinstance(data, dict) and isinstance(data.get("usage"), dict) else {}
    cost = call_cost(model, usage, peak=peak, registry=registry)
    try:
        choice = data["choices"][0]
        answer = (choice["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise RemoteUnavailable("malformed response", cost_usd=cost) from None
    if not answer:
        raise RemoteUnavailable("empty answer", cost_usd=cost)
    return RemoteResult(answer=answer, model=model, finish_reason=choice.get("finish_reason"),
                        usage=usage, peak=peak, cost_usd=cost, latency_ms=ms, raw=data)
