"""Requests and tokens for studio v2 (Metro): ``/api/recent``, ``/api/tokens`` and the
``/api/stream`` tail + snapshot. Read-only; contract: ``docs/design/studio-v2/API.md``.

Sources: the main proxy telemetry (``<main home>/telemetry.jsonl``) and the local proxy telemetry
(``<local home>/telemetry.jsonl``, the ollama lane), plus the newest rotated generation.

Agent attribution: pi sends Claude-style headers, so the telemetry ``client`` field cannot tell pi
from Claude Code. The row's ``session_id`` is looked up in ``agents.session_kinds`` (the id rules
the menu snapshot uses for pi / Claude Code / Codex session files). No match = ``other``.

Token normalisation (one rule, ``agent_resources.telemetry.fresh_input``): on the Anthropic wire
``tokens_in`` is the uncached remainder; on the OpenAI wire (Codex, ollama via the local proxy)
``tokens_in`` is the whole prompt and ``cache_read_tokens`` is inside it, so ``in`` = tokens_in -
cache_read. ``in + cache_read + cache_write`` is then the whole prompt, counted once.

Privacy: a response item holds numbers, fixed labels and fixed sentences only. No prompt text,
header value, session id, request id, model id string, error detail or key leaves this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import threading
import time
from pathlib import Path

from .. import agents as agents_mod
from ..agent_resources.telemetry import _cache_read, _cache_write, fresh_input
from . import live
from .flow import Paths

AGENT_LABELS = {"pi": "pi", "claude": "Claude Code", "codex": "Codex", "other": "Other"}
PROVIDER_LABELS = {"anthropic": "Claude models", "openai": "GPT models", "ollama": "Models on this Mac",
                   "other": "Other"}
TOKEN_WINDOWS = live.WINDOWS
RECENT_MAX = 200
RECENT_CAP = 16 * 1024 * 1024          # bytes read from the end of one telemetry file for /api/recent
SNAPSHOT_SPAN_S = 5 * 60
SNAPSHOT_CAP = 8 * 1024 * 1024
LIVE_S = 60.0
TAIL_READ_CAP = 4 * 1024 * 1024        # bytes per poll per file (the rest comes next poll)
TAIL_LINE_MAX = 1024 * 1024            # a partial line longer than this is dropped
KINDS_TTL_S = 30.0
KINDS_MISS_TTL_S = 3.0
TOKENS_TTL_S = 8.0
SNAPSHOT_TTL_S = 1.5

_CAUSES = {
    "ConnectError": "Provider not reached.",
    "ConnectTimeout": "Provider not reached in time.",
    "PoolTimeout": "Too many open requests to the provider.",
    "ReadTimeout": "The provider stopped answering.",
    "WriteTimeout": "The request could not be sent in time.",
    "RemoteProtocolError": "The provider closed the connection.",
    "http_400": "The provider refused the request.",
    "http_401": "Sign-in to the provider failed.",
    "http_403": "The provider denied access.",
    "http_404": "The model or route was not found.",
    "http_408": "The provider timed out.",
    "http_413": "The request is too large for the provider.",
    "http_429": "The provider rate limit was reached.",
    "http_529": "The provider is overloaded.",
}
_CAUSE_5XX = "The provider had an internal error."
_CAUSE_MIDSTREAM = "The answer stopped in the middle."
_CAUSE_OTHER = "The request failed."
CAUSE_RETRIED = "The first try failed. A retry worked."

_FAMILY_CLAUDE = re.compile(r"(opus|sonnet|haiku|fable)")
_FAMILY_GPT = re.compile(r"gpt[-_]?(\d{1,2})(?:[.-](\d{1,2}))?(?![0-9])")
_FAMILY_LOCAL = (("qwen", "Qwen"), ("llama", "Llama"), ("gemma", "Gemma"), ("mistral", "Mistral"),
                 ("deepseek", "DeepSeek"), ("kimi", "Kimi"), ("glm", "GLM"), ("phi", "Phi"))


# ---- attribution -----------------------------------------------------------------------------

class _Kinds:
    """A cached ``agents.session_kinds`` index for one home: rebuilt after KINDS_TTL_S, or after
    KINDS_MISS_TTL_S when a lookup misses (a session that started since the last build)."""

    def __init__(self, home: Path):
        self.home = home
        self.lock = threading.Lock()
        self.index: dict = {}
        self.built = 0.0

    def _build(self, now: float):
        self.index = agents_mod.session_kinds(self.home)
        self.built = now

    def kind(self, sid) -> str:
        if not isinstance(sid, str) or not sid:
            return "other"
        now = time.time()
        with self.lock:
            if now - self.built > KINDS_TTL_S:
                self._build(now)
            k = self.index.get(sid)
            if k is None and now - self.built > KINDS_MISS_TTL_S:
                self._build(now)
                k = self.index.get(sid)
        return k if k in AGENT_LABELS else "other"


_kinds: dict = {}
_kinds_lock = threading.Lock()


def kinds_for(paths: Paths) -> _Kinds:
    home = Path(paths.env.get("HOME") or Path.home())
    with _kinds_lock:
        k = _kinds.get(str(home))
        if k is None:
            k = _kinds[str(home)] = _Kinds(home)
            if len(_kinds) > 8:
                _kinds.pop(next(iter(_kinds)))
        return k


def agent_of(row: dict, kinds) -> str:
    return kinds.kind(row.get("session_id"))


# ---- one row -> plain fields -----------------------------------------------------------------

def provider_of(row: dict, proxy: str) -> str:
    if proxy == "local":
        return "ollama"
    ep = str(row.get("endpoint_id") or "anthropic").lower()
    if ep in ("anthropic", "foundry"):
        return "anthropic"
    if ep in ("openai", "azure", "azure-openai"):
        return "openai"
    if ep in ("ollama", "local"):
        return "ollama"
    return "other"


def model_family(model) -> str:
    """A plain family name from a model id (never the id itself)."""
    s = str(model or "").lower()
    m = _FAMILY_CLAUDE.search(s)
    if m:
        return "Claude " + m.group(1).title()
    if "claude" in s:
        return "Claude"
    m = _FAMILY_GPT.search(s)
    if m:
        return "GPT " + m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
    for key, name in _FAMILY_LOCAL:
        if key in s:
            return name
    return "Other"


def tokens_of(row: dict) -> dict:
    """{in, out, cache_read, cache_write} with ``in`` = uncached input on every wire."""
    u = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    out = row.get("tokens_out")
    if not isinstance(out, (int, float)) or isinstance(out, bool) or out <= 0:
        out = u.get("output_tokens") if isinstance(u.get("output_tokens"), (int, float)) else 0
    return {"in": int(fresh_input(row)), "out": int(out or 0), "cache_read": int(_cache_read(row)),
            "cache_write": int(_cache_write(row))}


def cause_of(row: dict) -> str:
    c = str(row.get("error_cause") or "")
    if c in _CAUSES:
        return _CAUSES[c]
    if re.fullmatch(r"http_5\d\d", c):
        return _CAUSE_5XX
    if c.startswith("midstream_"):
        return _CAUSE_MIDSTREAM
    return _CAUSE_OTHER


def _retries(row: dict) -> int:
    v = row.get("connect_retries")
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def status_of(row: dict) -> str:
    if row.get("is_error"):
        return "error"
    return "retried" if _retries(row) > 0 else "ok"


def row_id(ts: float, raw: bytes) -> str:
    """Stable per telemetry line (the same in /api/recent and in the stream): ms ts + line hash."""
    return f"{int(ts * 1000)}-{hashlib.sha256(raw).hexdigest()[:10]}"


def _num_ms(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return round(float(v))
    return None


def item(row: dict, raw: bytes, kinds, proxy: str = "main") -> dict:
    """One request in the contract shape. ``ms_total`` is null: telemetry has no end time."""
    ts = row["_ts"]
    st = status_of(row)
    return {"id": row_id(ts, raw), "ts": round(ts, 3), "agent": agent_of(row, kinds),
            "provider": provider_of(row, proxy), "model_family": model_family(row.get("model_requested")),
            "status": st,
            "cause": None if st == "ok" else (CAUSE_RETRIED if st == "retried" else cause_of(row)),
            "ms_total": None, "ttft_ms": _num_ms(row.get("ttft_ms")), "tokens": tokens_of(row)}


def is_request(row) -> bool:
    return isinstance(row, dict) and row.get("ev") != "hb" and "client" in row


def parse_line(raw: bytes):
    """A request row (with ``_ts``) from one telemetry line, or None."""
    try:
        row = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not is_request(row):
        return None
    ts = live._norm_ts(row.get("ts"))
    if ts is None:
        return None
    row["_ts"] = ts
    return row


# ---- /api/recent -----------------------------------------------------------------------------

def _newest_gen(path: Path):
    pat = re.compile(re.escape(path.name) + r"\.(\d+)$")
    try:
        gens = sorted((int(m.group(1)), q) for q in path.parent.iterdir()
                      if (m := pat.fullmatch(q.name)) and q.is_file())
    except OSError:
        return None
    return gens[-1][1] if gens else None


def _last_requests(path: Path, n: int, cap: int = RECENT_CAP) -> list:
    """Up to n (row, raw) pairs, newest first, from the end of one file (grows the read 1 -> cap MiB)."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    want = min(size, 1024 * 1024)
    while True:
        out = []
        for raw in reversed(live._lines(path, want)):
            row = parse_line(raw)
            if row is not None:
                out.append((row, raw))
                if len(out) >= n:
                    return out
        if want >= size or want >= cap:
            return out
        want = min(size, cap, want * 4)


def recent(paths: Paths, n: int = 50) -> dict:
    n = max(1, min(RECENT_MAX, int(n)))
    kinds = kinds_for(paths)
    got = []
    for proxy in ("main", "local"):
        p = paths.telemetry(proxy)
        rows = _last_requests(p, n)
        if len(rows) < n:
            g = _newest_gen(p) if p.parent.is_dir() else None
            if g is not None:
                rows += _last_requests(g, n - len(rows))
        got += [(r, raw, proxy) for r, raw in rows]
    got.sort(key=lambda t: t[0]["_ts"], reverse=True)
    return {"requests": [item(r, raw, kinds, proxy) for r, raw, proxy in got[:n]]}


# ---- /api/tokens -----------------------------------------------------------------------------

def _zero():
    return {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}


def _add(acc: dict, t: dict):
    for k in acc:
        acc[k] += t[k]


def tokens(paths: Paths, window: str = "15m", now: float | None = None) -> dict:
    now = time.time() if now is None else now
    span = TOKEN_WINDOWS[window]
    kinds = kinds_for(paths)
    total, by_agent, by_provider, by_model = _zero(), {}, {}, {}
    for proxy in ("main", "local"):
        rows, _ = live.rows_in_window(paths.telemetry(proxy), now - span, rotated=True)
        for r in rows:
            if not is_request(r):
                continue
            t = tokens_of(r)
            _add(total, t)
            _add(by_agent.setdefault(agent_of(r, kinds), _zero()), t)
            _add(by_provider.setdefault(provider_of(r, proxy), _zero()), t)
            _add(by_model.setdefault(model_family(r.get("model_requested")), _zero()), t)
    den = total["in"] + total["cache_read"] + total["cache_write"]
    return {"window": window, "total": total, "by_agent": by_agent, "by_provider": by_provider,
            "by_model": by_model, "cache_share": round(total["cache_read"] / den, 4) if den else None}


_tok_cache: dict = {}
_tok_lock = threading.Lock()


def tokens_cached(paths: Paths, window: str) -> dict:
    key = (window, str(paths.telemetry("main")), str(paths.telemetry("local")), paths.env.get("HOME"))
    now = time.time()
    with _tok_lock:
        hit = _tok_cache.get(key)
        if hit and now - hit[0] < TOKENS_TTL_S:
            return hit[1]
    out = tokens(paths, window, now)
    with _tok_lock:
        _tok_cache[key] = (now, out)
        if len(_tok_cache) > 16:
            _tok_cache.pop(next(iter(_tok_cache)))
    return out


# ---- stream: tail by offset ------------------------------------------------------------------

class Tail:
    """Follows one file by offset. Starts at the end (only new lines). A replaced file (rotation:
    a new inode) is read to its end first, then the new file from offset 0; a truncated file
    restarts at 0. A file that is missing at start is read from 0 when it appears."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.f = None
        self.ino = None
        self.buf = b""
        self._open(at_end=True)

    def _open(self, at_end: bool):
        try:
            f = open(self.path, "rb")
        except OSError:
            self.f = self.ino = None
            return
        st = os.fstat(f.fileno())
        self.ino = (st.st_dev, st.st_ino)
        if at_end:
            f.seek(st.st_size)
        self.f, self.buf = f, b""

    def _drain(self, cap: int) -> list:
        try:
            data = self.f.read(cap)
        except OSError:
            return []
        if not data:
            return []
        lines = (self.buf + data).split(b"\n")
        self.buf = lines.pop()
        if len(self.buf) > TAIL_LINE_MAX:
            self.buf = b""
        return [ln for ln in lines if ln.strip()]

    def read(self, cap: int = TAIL_READ_CAP) -> list:
        if self.f is None:
            self._open(at_end=False)
            if self.f is None:
                return []
        out = self._drain(cap)
        try:
            st = os.stat(self.path)
        except OSError:
            return out                       # rotated, the new file not yet written
        if (st.st_dev, st.st_ino) != self.ino:
            # leave the old generation only at its end: a capped read per poll, no row lost
            try:
                at_end = self.f.tell() >= os.fstat(self.f.fileno()).st_size
            except OSError:
                at_end = True
            if not at_end:
                return out
            self.close()
            self._open(at_end=False)
            if self.f is not None:
                out += self._drain(cap)
        elif self.f.tell() > st.st_size:     # truncated in place
            self.f.seek(0)
            self.buf = b""
        return out

    def close(self):
        if self.f is not None:
            try:
                self.f.close()
            except OSError:
                pass
        self.f = None


# ---- stream: snapshot ------------------------------------------------------------------------

def _fmt_age(s: float) -> str:
    s = max(0, int(round(s)))
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    return f"{s // 3600} h"


def proxy_state(paths: Paths, fetch, port: int, hb_rows: list, now: float) -> dict:
    st = fetch(f"http://127.0.0.1:{port}/stats") if fetch else None
    a = st.get("activity") if isinstance(st, dict) else None
    if isinstance(a, dict):
        inflight = a.get("inflight") if isinstance(a.get("inflight"), int) else None
        idle = a.get("idle_s")
        return {"up": True, "inflight": inflight,
                "idle_s": round(float(idle), 1) if isinstance(idle, (int, float)) and not isinstance(idle, bool) else None,
                "draining": bool(a.get("draining"))}
    if isinstance(st, dict):
        return {"up": True, "inflight": None, "idle_s": None, "draining": False}
    last_hb = max((r["_ts"] for r in hb_rows), default=None)
    return {"up": bool(last_hb and now - last_hb <= live.HB_ALIVE_S), "inflight": None, "idle_s": None,
            "draining": False}


def snapshot(paths: Paths, fetch=None, port: int = 8788, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    kinds = kinds_for(paths)
    lines: dict = {}
    reqs, hbs = [], []
    for proxy in ("main", "local"):
        rows, _ = live.rows_in_window(paths.telemetry(proxy), now - SNAPSHOT_SPAN_S, cap=SNAPSHOT_CAP,
                                      rotated=True)
        for r in rows:
            if r.get("ev") == "hb":
                if proxy == "main":
                    hbs.append(r)
                continue
            if not is_request(r):
                continue
            reqs.append(r)
            key = (agent_of(r, kinds), provider_of(r, proxy))
            ln = lines.setdefault(key, {"n": 0, "err": 0, "last": 0.0})
            ln["n"] += 1
            ln["err"] += 1 if r.get("is_error") else 0
            ln["last"] = max(ln["last"], r["_ts"])
    out_lines = [{"agent": a, "provider": p, "rpm": round(v["n"] / (SNAPSHOT_SPAN_S / 60), 1),
                  "err_pct": round(100.0 * v["err"] / v["n"], 1), "live": now - v["last"] <= LIVE_S}
                 for (a, p), v in sorted(lines.items(), key=lambda kv: -kv[1]["n"])]
    proxy = proxy_state(paths, fetch, port, hbs, now)
    n = len(reqs)
    errs = sum(1 for r in reqs if r.get("is_error"))
    ttft = [float(r["ttft_ms"]) for r in reqs if not r.get("is_error")
            and isinstance(r.get("ttft_ms"), (int, float)) and not isinstance(r.get("ttft_ms"), bool)
            and r["ttft_ms"] > 0]
    p50 = statistics.median(ttft) if ttft else None
    last = max((r["_ts"] for r in reqs), default=None)
    if not proxy["up"]:
        level, sentence = "problem", "The proxy does not answer."
    elif n >= 3 and errs * 100 >= 10 * n:
        level, sentence = "problem", f"{errs} of {n} requests failed in the last 5 min."
    elif proxy["draining"]:
        level, sentence = "slow", "The proxy is draining for a restart."
    elif errs and errs * 100 >= 2 * n:
        level, sentence = "slow", f"{errs} of {n} requests failed in the last 5 min."
    elif p50 is not None and p50 > 15000:
        level, sentence = "slow", f"Answers are slow. First token after {p50 / 1000:.0f} s (median)."
    elif last is None:
        level, sentence = "ok", "All good. No requests in the last 5 min."
    else:
        level, sentence = "ok", f"All good. Last request {_fmt_age(now - last)} ago."
    return {"status": {"level": level, "sentence": sentence}, "lines": out_lines, "proxy": proxy}


def sse(event: str | None, data) -> bytes:
    body = json.dumps(data, separators=(",", ":"), default=str)
    return ((f"event: {event}\n" if event else "") + f"data: {body}\n\n").encode()


def stream(write, paths: Paths, closing: threading.Event, snap_fn, poll_s: float = 0.5,
           snap_s: float = 2.0, hb_s: float = 15.0) -> None:
    """The SSE loop of one client: a snapshot at connect and every ``snap_s``, a ``request`` event
    per new telemetry row, ``: hb`` every ``hb_s``. Returns when ``closing`` is set; a write error
    (the client went away) propagates to the caller."""
    kinds = kinds_for(paths)
    tails = {proxy: Tail(paths.telemetry(proxy)) for proxy in ("main", "local")}
    try:
        write(sse("snapshot", snap_fn()))
        last_snap = last_hb = time.monotonic()
        while not closing.wait(poll_s):
            for proxy, t in tails.items():
                for raw in t.read():
                    row = parse_line(raw)
                    if row is not None:
                        write(sse("request", item(row, raw, kinds, proxy)))
            m = time.monotonic()
            if m - last_snap >= snap_s:
                write(sse("snapshot", snap_fn()))
                last_snap = m
            if m - last_hb >= hb_s:
                write(b": hb\n\n")
                last_hb = m
    finally:
        for t in tails.values():
            t.close()
