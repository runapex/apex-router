"""Proxy telemetry per session and subagent: requests, the token split, errors, cache share
and the context size of the latest request against its window."""
from __future__ import annotations

import math
import re
import statistics
import time
from collections import Counter

from .base import GRAPH_MODELS_MAX, _num


TELEMETRY_WINDOW_S = 60 * 60


# ---- telemetry: tokens, cache share, context size --------------------------------------------

CTX_WARN_PCT = 85
CTX_200K_WARN = 170_000           # a known 200k window: flag from here (= 85%)
CTX_OBSERVED_1M = 200_000         # a successful request above this proves a 1M window
ERR_RECENT_S = 5 * 60
ERR_RATE_WARN = 0.05
RATE_WINDOW_S = 5 * 60            # the ``r5`` request rate
_CTX_1M_RE = re.compile(r"\[1m\]\s*$", re.IGNORECASE)
_FAMILY_RE = re.compile(r"(?:^|[^a-z0-9])(?:claude-)?(opus|sonnet|haiku|fable)"
                        r"(?:-(\d{1,2})(?!\d)(?:-(\d{1,2})(?!\d))?)?", re.IGNORECASE)


def fresh_input(r: dict) -> int:
    """Uncached input tokens of a row, wire-aware (the rule of ``doctor._fresh_input``): on the
    OpenAI wire ``tokens_in`` is the whole prompt and ``cache_read_tokens`` a subset of it; on the
    Anthropic wire ``tokens_in`` is already the uncached remainder."""
    u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
    ti = _num(r.get("tokens_in")) or _num(u.get("input_tokens")) or 0
    if str(r.get("endpoint_id") or "").lower() == "openai":
        return max(0, int(ti - (_num(r.get("cache_read_tokens")) or 0)))
    return int(ti)


def _cache_read(r):
    u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
    return int(_num(r.get("cache_read_tokens")) or _num(u.get("cache_read_tokens")) or 0)


def _cache_write(r):
    u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
    return int(_num(r.get("cache_write_tokens")) or _num(u.get("cache_creation_tokens")) or 0)


def context_window(model) -> int | None:
    """Context window by model family (VERIFIED 2026-10-06: pi 0.99.1 catalog + a live 283k-token
    request on claude-opus-5-5): ``[1m]`` suffix, claude-opus / claude-sonnet >= 4.6 (any 5.x),
    claude-fable-*, claude-sonnet-4-5 -> 1,000,000; claude-haiku-4-5*, claude-opus-4-5 -> 200,000. Anything else -> None (no guess;
    ``_stats`` may still prove 1M from an observed > 200k request)."""
    if not isinstance(model, str) or not model:
        return None
    if _CTX_1M_RE.search(model):
        return 1_000_000
    m = _FAMILY_RE.search(model)
    if not m:
        return None
    fam = m.group(1).lower()
    major = int(m.group(2)) if m.group(2) else None
    minor = int(m.group(3)) if m.group(3) else None
    if fam == "fable":
        return 1_000_000 if major is not None or "fable-" in model.lower() else None
    if major is None:
        return None
    # pi 0.99.1 models-store: claude-sonnet-4-5 -> 1M, claude-opus-4-5 -> 200k.
    if fam == "sonnet" and major == 4 and minor == 5:
        return 1_000_000
    if fam == "opus" and major == 4 and minor == 5:
        return 200_000
    if fam in ("opus", "sonnet"):
        if major >= 5 or (major == 4 and minor is not None and minor >= 6):
            return 1_000_000
        return None
    if fam == "haiku" and major == 4 and minor == 5:
        return 200_000
    return None


def context_of(r: dict) -> int:
    """Prompt size the model saw on one request: uncached input + cache read + cache write."""
    return fresh_input(r) + _cache_read(r) + _cache_write(r)


def cache_share(st: dict):
    """cached / (uncached input + cached + cache write) — None when there was no input."""
    den = (st.get("tokens_in") or 0) + (st.get("cache_read") or 0) + (st.get("cache_write") or 0)
    return (st.get("cache_read") or 0) / den if den else None


def _ts(r):
    v = _num(r.get("ts"))
    return v if v is not None and math.isfinite(v) else None


def _stats(rows: list, now: float | None = None) -> dict:
    """Traffic of one thread (or any row set). ``now`` anchors the 5-min counts (``req_5m``,
    ``errors_5m``); default: the clock."""
    now = time.time() if now is None else now
    ttft = [r["ttft_ms"] for r in rows if isinstance(r.get("ttft_ms"), (int, float))]
    stamps = [t for t in (_ts(r) for r in rows) if t is not None]

    def tot(k):
        return int(sum(r[k] for r in rows if isinstance(r.get(k), (int, float))))
    out = {"requests": len(rows), "tokens_out": tot("tokens_out"),
           "tokens_in": sum(fresh_input(r) for r in rows),
           "cache_read": sum(_cache_read(r) for r in rows),
           "cache_write": sum(_cache_write(r) for r in rows),
           "errors": sum(1 for r in rows if r.get("is_error")),
           "errors_5m": sum(1 for r in rows if r.get("is_error")
                            and (_ts(r) or 0) >= now - ERR_RECENT_S),
           "req_5m": sum(1 for t in stamps if t >= now - RATE_WINDOW_S),
           "net_up": tot("bytes_up"), "net_down": tot("bytes_down"),   # v10 proxy wire bytes
           "last_ts": max(stamps) if stamps else None,
           "p50_ttft_ms": round(statistics.median(ttft)) if ttft else None,
           "models": dict(Counter(str(r.get("model_requested") or "?") for r in rows)
                          .most_common(GRAPH_MODELS_MAX))}
    # the LATEST request that carried a prompt: its size is the current context fill
    sized = [r for r in rows if context_of(r) > 0]
    if sized:
        last = max(sized, key=lambda r: _num(r.get("ts")) or 0)
        out["ctx_tokens"] = context_of(last)
        out["ctx_ts"] = _num(last.get("ts"))
        model = last.get("model_requested")
        win = context_window(model)
        src = "family" if win else None
        if win is None and any(r.get("model_requested") == model and not r.get("is_error")
                               and context_of(r) > CTX_OBSERVED_1M for r in rows):
            win, src = 1_000_000, "observed"           # > 200k succeeded: the window is 1M
        out["ctx_window"] = win
        out["ctx_window_src"] = src
        out["ctx_pct"] = round(100 * out["ctx_tokens"] / win) if win else None
    return out


_SUMMED = ("requests", "tokens_out", "tokens_in", "cache_read", "cache_write", "errors",
           "errors_5m", "req_5m", "net_up", "net_down")


def merge_stats(*parts) -> dict:
    """Sum of stats dicts (requests, token split, errors, per-model counts). Medians and the
    context size (a latest-request value, not a sum) do not merge and are dropped."""
    out = {k: 0 for k in _SUMMED}
    models = Counter()
    last = None
    for p in parts:
        if not isinstance(p, dict):
            continue
        for k in _SUMMED:
            if isinstance(p.get(k), (int, float)):
                out[k] += p[k]
        t = _num(p.get("last_ts"))
        if t is not None and math.isfinite(t):
            last = t if last is None else max(last, t)
        models.update(p.get("models") or {})
    out["models"] = dict(models.most_common())
    out["last_ts"] = last                              # newest request: a max, not a sum
    return out


def ctx_flag(st) -> bool:
    """Context nearly full: >= CTX_WARN_PCT of a known window, or >= 170k of a known 200k one."""
    st = st or {}
    if isinstance(st.get("ctx_pct"), (int, float)) and st["ctx_pct"] >= CTX_WARN_PCT:
        return True
    n = st.get("ctx_tokens")
    return st.get("ctx_window") == 200_000 and isinstance(n, (int, float)) and n >= CTX_200K_WARN


def err_rate(st) -> float | None:
    st = st or {}
    n, req = st.get("errors") or 0, st.get("requests") or 0
    return n / req if req else (1.0 if n else None)


def err_flag(st) -> bool:
    """An error worth a ⚠: one in the last 5 min, or a 60-min error rate >= 5%."""
    st = st or {}
    if not st.get("errors"):
        return False
    if (st.get("errors_5m") or 0) > 0:
        return True
    rate = err_rate(st)
    return rate is not None and rate >= ERR_RATE_WARN


def telemetry_split(rows, now: float | None = None) -> dict:
    """session_id -> {"main": stats, "subagents": {agent_id: stats}}; rows without a session_id
    are dropped (they cannot be attributed)."""
    groups: dict = {}
    for r in rows:
        sid = r.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        aid = r.get("agent_id") if isinstance(r.get("agent_id"), str) and r.get("agent_id") else None
        groups.setdefault(sid, {}).setdefault(aid, []).append(r)
    out = {}
    for sid, by in groups.items():
        out[sid] = {"main": _stats(by.get(None, []), now),
                    "subagents": {a: _stats(rs, now) for a, rs in by.items() if a is not None}}
    return out


def _buckets(rows, now):
    from ..widget_history import buckets
    return buckets(rows, now)
