"""The decision flow of one request for studio v2 (contract: ``docs/design/studio-v2/API.md``,
section "Decision flow"): six stations, each with a plain verdict and a state.

    task -> model -> guard -> cache -> send -> answer

Sources: the telemetry row of the request, and the route_log row of the same session with the
nearest earlier ts (at most JOIN_S before the request). The route_log is read from a capped tail
(ROUTE_TAIL bytes, ROUTE_ROWS_MAX rows), never whole.

Privacy: every verdict is built from the fixed maps below plus numbers. No raw row text (task type,
family, model id, cause, stop reason, cache key, session id) leaves this module. An unknown value
maps to a fixed word ("other work", "ended", "cause unknown").
"""
from __future__ import annotations

import bisect
import json
import os
import re
import stat
import threading
from pathlib import Path

from ..agent_resources.telemetry import _cache_read, _cache_write, fresh_input
from . import live
from . import traffic as _traffic       # model_family only; resolved at call time (no import cycle)

JOIN_S = 120.0                          # a route_log row older than this does not belong to the request
ROUTE_TAIL = 2 * 1024 * 1024            # bytes from the end of route_log.jsonl (about 10 000 rows)
ROUTE_ROWS_MAX = 20000
SID_MAX = 200                           # longer session ids are not indexed

STEPS = (("task", "Task"), ("model", "Model choice"), ("guard", "Safety blocks"), ("cache", "Cache"),
         ("send", "Send"), ("answer", "Answer"))
NONE = "—"

TASKS = {"debug": "debug", "generate": "generate", "review": "review", "explore": "explore",
         "refactor": "refactor", "status": "status", "extract": "extract", "find": "find", "draft": "draft",
         "xval": "second opinion", "adhoc": "ad hoc", "plan": "plan", "test": "test", "docs": "docs"}
TASK_OTHER = "other work"
FAMILIES = {"deep": "deep", "frontier": "frontier", "haiku": "fast", "fast": "fast", "review": "review",
            "auto": "auto", "local": "local"}
GUARD_ACTIONS = {"fallback": "sent the safe copy", "invalidate": "dropped the stored copy"}
GUARD_OTHER = "stopped"
BUST_CAUSES = {"ttl": "it expired", "transform": "apex changed the prompt", "client_edit": "the agent changed the prompt",
               "frontier_rerender": "the prompt was rebuilt", "unknown": "cause unknown"}
BUST_OTHER = "cause unknown"
NOT_REACHED = frozenset({"ConnectError", "ConnectTimeout", "PoolTimeout", "WriteTimeout"})
REJECT = {"http_400": "bad request", "http_401": "sign-in failed", "http_403": "access denied",
          "http_404": "model not found", "http_408": "timed out", "http_413": "request too large",
          "http_429": "rate limit", "http_529": "overloaded"}
REJECT_5XX = "provider error"
REJECT_OTHER = "refused"
STOP_OK = {"end_turn": "ended normally", "stop": "ended normally", "completed": "ended normally",
           "stop_sequence": "ended normally", "tool_use": "asked for a tool", "tool_calls": "asked for a tool",
           "pause_turn": "paused"}
STOP_LENGTH = frozenset({"max_tokens", "length", "incomplete", "incomplete:max_output_tokens",
                         "model_context_window_exceeded"})
STOP_REFUSED = frozenset({"refusal", "content_filter", "incomplete:content_filter"})
STOP_OTHER = "ended"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _d(step: str, verdict: str, state: str) -> dict:
    return {"step": step, "label": dict(STEPS)[step], "verdict": verdict, "state": state}


def _secs(ms) -> str:
    ms = _num(ms)
    if ms is None or ms <= 0:
        return ""
    s = ms / 1000.0
    if s < 10:
        return f" · {s:.1f} s"
    if s < 120:
        return f" · {round(s)} s"
    return f" · {round(s / 60)} min"


# ---- route_log: a capped tail, indexed by session ------------------------------------------------

class RouteIndex:
    """session id -> [(ts, task, family, escalated)], sorted by ts. Only fixed-map words are kept."""

    def __init__(self, by_sid: dict, rows: int):
        self.by_sid = by_sid
        self.ts = {k: [x[0] for x in v] for k, v in by_sid.items()}
        self.rows = rows

    @classmethod
    def empty(cls) -> "RouteIndex":
        return cls({}, 0)

    def lookup(self, sid, ts) -> dict | None:
        if not isinstance(sid, str) or not sid or len(sid) > SID_MAX:
            return None
        ts = _num(ts)
        rows = self.by_sid.get(sid)
        if ts is None or not rows:
            return None
        i = bisect.bisect_right(self.ts[sid], ts) - 1
        if i < 0 or ts - rows[i][0] > JOIN_S:
            return None
        _, task, family, esc = rows[i]
        return {"task": task, "family": family, "escalated": esc}


def _regular(p: Path) -> bool:
    """A regular file (not a FIFO, a device or a directory): a read of it cannot block."""
    try:
        return stat.S_ISREG(os.stat(p).st_mode)
    except OSError:
        return False


def route_index(path: Path) -> RouteIndex:
    path = Path(path)
    if not _regular(path):
        return RouteIndex.empty()
    lines = live._lines(path, ROUTE_TAIL)[-ROUTE_ROWS_MAX:]
    by_sid: dict = {}
    n = 0
    for ln in lines:
        try:
            r = json.loads(ln)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(r, dict):
            continue
        sid, ts = r.get("session_id"), live._norm_ts(r.get("ts"))
        if not isinstance(sid, str) or not sid or len(sid) > SID_MAX or ts is None:
            continue
        task = r.get("task_type")
        task = TASKS.get(task, TASK_OTHER) if isinstance(task, str) and task else TASK_OTHER
        fam = r.get("family")
        fam = FAMILIES.get(fam) if isinstance(fam, str) else None
        by_sid.setdefault(sid, []).append((ts, task, fam, r.get("escalated") is True))
        n += 1
    for v in by_sid.values():
        v.sort(key=lambda x: x[0])
    return RouteIndex(by_sid, n)


_idx_cache: dict = {}
_idx_lock = threading.Lock()


def route_index_cached(path: Path) -> RouteIndex:
    """One index per file, rebuilt when its size or mtime changes (each build is one capped read)."""
    path = Path(path)
    try:
        st = os.stat(path)
        sig = (st.st_ino, st.st_size, st.st_mtime_ns) if stat.S_ISREG(st.st_mode) else None
    except OSError:
        sig = None
    if sig is None:
        return RouteIndex.empty()
    key = str(path)
    with _idx_lock:
        hit = _idx_cache.get(key)
        if hit and hit[0] == sig:
            return hit[1]
    idx = route_index(path)
    with _idx_lock:
        _idx_cache[key] = (sig, idx)
        if len(_idx_cache) > 8:
            _idx_cache.pop(next(iter(_idx_cache)))
    return idx


# ---- one row -> six stations -----------------------------------------------------------------------

def _task(route) -> dict:
    if route is None:
        return _d("task", NONE, "none")
    return _d("task", route["task"], "ok")


def _model(row: dict, route) -> dict:
    req, res = row.get("model_requested"), row.get("model_resolved")
    req = req if isinstance(req, str) and req else None
    res = res if isinstance(res, str) and res else None
    fam_req = _traffic.model_family(req) if req else None
    fam_res = _traffic.model_family(res) if res else None
    if route is not None and route["escalated"]:
        return _d("model", "escalated → " + (fam_res or fam_req or "another model"), "warn")
    if req and res and req != res:
        if fam_req == fam_res:
            return _d("model", f"{fam_req} → other version", "warn")
        return _d("model", f"{fam_req} → {fam_res}", "warn")
    shown = fam_res or fam_req
    if shown is None:
        return _d("model", NONE, "none")
    if route is not None and route["family"]:
        return _d("model", f"{route['family']} → {shown}", "ok")
    return _d("model", shown, "ok")


def _guard(row: dict) -> dict:
    fired = row.get("guard_fired")
    if not isinstance(fired, bool):
        return _d("guard", NONE, "none")
    if not fired:
        return _d("guard", "passed", "ok")
    act = row.get("guard_action")
    return _d("guard", "blocked: " + (GUARD_ACTIONS.get(act, GUARD_OTHER) if isinstance(act, str) else GUARD_OTHER),
              "error")


def _cache(row: dict) -> dict:
    if row.get("bust") is True:
        c = row.get("bust_cause")
        return _d("cache", "cache missed: " + (BUST_CAUSES.get(c, BUST_OTHER) if isinstance(c, str) else BUST_OTHER),
                  "warn")
    try:
        cr, cw, fi = max(0, _cache_read(row)), max(0, _cache_write(row)), max(0, fresh_input(row))
    except (TypeError, ValueError):
        return _d("cache", NONE, "none")
    den = fi + cr + cw
    if den <= 0:
        return _d("cache", NONE, "none")
    return _d("cache", f"{round(100 * cr / den)} % from cache", "ok")


def _cause(row: dict) -> str:
    c = row.get("error_cause")
    return c if isinstance(c, str) else ""


def _send(row: dict) -> dict:
    n = _num(row.get("connect_retries"))
    n = int(n) if n is not None and n > 0 else 0
    tries = f"{n} {'retry' if n == 1 else 'retries'}"
    cause = _cause(row)
    if row.get("is_error") is True and cause in NOT_REACHED:
        return _d("send", (tries + ", not reached") if n else "not reached", "error")
    if row.get("upstream_rejected") is True or cause.startswith("http_"):
        why = REJECT.get(cause) or (REJECT_5XX if re.fullmatch(r"http_5\d\d", cause) else REJECT_OTHER)
        return _d("send", "provider said no: " + why, "error")
    if n:
        return _d("send", tries + ", then sent", "warn")
    return _d("send", "sent", "ok")


def _answer(row: dict) -> dict:
    took = _secs(row.get("total_ms"))
    cause = _cause(row)
    if row.get("is_error") is True:
        if cause in NOT_REACHED or cause.startswith("http_") or row.get("upstream_rejected") is True:
            return _d("answer", "no answer", "none")
        if cause.startswith("midstream_"):
            return _d("answer", "stopped in the middle" + took, "error")
        if cause == "ReadTimeout":
            return _d("answer", "no answer in time" + took, "error")
        if cause == "RemoteProtocolError":
            return _d("answer", "connection closed" + took, "error")
        return _d("answer", "failed" + took, "error")
    if row.get("upstream_rejected") is True:
        return _d("answer", "no answer", "none")
    sr = row.get("stop_reason")
    sr = sr if isinstance(sr, str) else None
    if row.get("refusal") is True or sr in STOP_REFUSED:
        return _d("answer", "refused" + took, "warn")
    if sr in STOP_LENGTH:
        return _d("answer", "stopped: length" + took, "warn")
    if sr == "failed":
        return _d("answer", "failed" + took, "error")
    if sr is None and not took:
        return _d("answer", NONE, "none")
    return _d("answer", STOP_OK.get(sr, STOP_OTHER) + took, "ok")


def decisions(row: dict, routes: RouteIndex | None = None) -> list:
    """The six stations of one telemetry row, in contract order. Never raises on a bad row."""
    if not isinstance(row, dict):
        row = {}
    routes = routes or RouteIndex.empty()
    ts = row.get("_ts")
    if _num(ts) is None:
        ts = live._norm_ts(row.get("ts"))
    route = routes.lookup(row.get("session_id"), ts)
    return [_task(route), _model(row, route), _guard(row), _cache(row), _send(row), _answer(row)]
