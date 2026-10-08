"""Claude Code subagents from their sidecar logs (mtimes and ``meta.json`` only), their
lifecycle and flags, the ``waiting?`` heuristic, and the per-session rollups the menu ranks by."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .base import DEADLINE_MIN_S, Deadline, _num
from .telemetry import TELEMETRY_WINDOW_S, ctx_flag, err_flag, merge_stats


SUBAGENT_RUNNING_S = 60
SUBAGENT_QUIET_S = 5 * 60
SUBAGENT_STUCK_S = 20 * 60
SUBAGENT_WAITING_FLAG_S = 10 * 60  # a ``waiting?`` subagent is a ⚠ reason after this
SUBAGENTS_MAX = 8                 # subagents kept per session (the rest are summed into "hidden")
SUBAGENT_SCAN_MAX = 500           # log files looked at per session
SCAN_SESSIONS_MAX = 8             # sessions whose subagent logs are scanned (= the menu's ACTIVE_MAX)
META_BYTES_MAX = 64 * 1024
_AGENT_FILE_RE = re.compile(r"^agent-([A-Za-z0-9_-]+)\.jsonl$")


# ---- subagents ------------------------------------------------------------------------------

def _lifecycle(age, run_s, tel) -> tuple:
    if age is None:
        state = "?"
    elif age < SUBAGENT_RUNNING_S:
        state = "running"
    elif age < SUBAGENT_QUIET_S:
        state = "quiet"
    else:
        state = "done"
    flags = []
    if state == "running" and isinstance(run_s, (int, float)) and run_s > SUBAGENT_STUCK_S:
        flags.append("long")
    # A finished/quiet subagent's old errors are history: only a fresh one (5 min) flags it.
    if err_flag(tel) if state == "running" else (tel or {}).get("errors_5m"):
        flags.append("errors")
    if ctx_flag(tel):
        flags.append("ctx")
    return state, flags


def _sub_sort_key(s):
    st = s.get("telemetry") or {}
    return (s["state"] != "running", "errors" not in s["flags"], -(st.get("tokens_out") or 0),
            s["age_s"] is None, s["age_s"] or 0)


def _last_s(age, tel, now):
    """Seconds since the subagent last did anything: its log write or its newest request."""
    t = _num((tel or {}).get("last_ts"))
    cands = [x for x in (age, (max(0.0, now - t) if t is not None and math.isfinite(t) else None))
             if x is not None]
    return round(min(cands), 1) if cands else None


def mark_waiting(found: dict, status, main_age) -> None:
    """Flag the subagent the busy session is probably still waiting on — a HEURISTIC.

    A finished subagent and a hung one look the same from outside: both stopped writing. The one a
    ``busy`` session is still waiting on is the stream that went quiet LAST: its log has been quiet
    for more than ``SUBAGENT_RUNNING_S`` and nothing in the session — the main log nor any other
    subagent — has written since. Such a subagent gains ``waiting: True`` and ``waiting_s`` (its
    quiet time); past ``SUBAGENT_WAITING_FLAG_S`` it also gets the ``waiting`` flag (a ⚠ reason).
    Needs the main log's age (``main_age``): without it nothing is marked. A long tool call inside
    the subagent (a test run, a build) also writes nothing, so a ``waiting?`` subagent can be
    healthy; the question mark stays on purpose."""
    if status != "busy" or not isinstance(main_age, (int, float)):
        return
    for aid, s in found.items():
        last = s.get("last_s")
        if s.get("age_s") is None or not isinstance(last, (int, float)) \
                or last <= SUBAGENT_RUNNING_S or main_age < last:
            continue
        if any(isinstance(o.get("last_s"), (int, float)) and o["last_s"] < last
               for oid, o in found.items() if oid != aid):
            continue                                   # a newer write elsewhere: not this one
        s["waiting"] = True
        s["waiting_s"] = last
        if last > SUBAGENT_WAITING_FLAG_S and "waiting" not in s["flags"]:
            s["flags"].append("waiting")


def subagents(home: Path, session_id: str, now: float, tel: dict | None = None,
              keep: int = SUBAGENTS_MAX, scan: bool = True,
              deadline: Deadline | None = None, window_s: float = TELEMETRY_WINDOW_S,
              times: bool = False, status: str | None = None) -> dict:
    """Subagents seen in the last 60 min (log mtime) or with proxy traffic in the window.

    ``scan=False`` skips the filesystem (traffic only). ``window_s`` widens the log-mtime window
    (the detail page lists 6 h); ``times`` adds ``spawn_ts`` / ``mtime`` (epoch) to each. With a ``deadline`` the log scan stops
    when it runs out (``partial`` is then True); traffic-only subagents are always included.
    With the session's ``status`` (Claude's busy/idle) a scan also reads the main log's mtime
    (``main_age_s``) and marks the subagent a busy session is probably waiting on
    (``mark_waiting``, a heuristic).

    Returns ``{"list", "more", "hidden", "totals", "count", "running", "quiet", "flagged"}``:
    ``list`` = the first ``keep`` sorted running -> erroring -> output tokens -> newest; ``hidden`` = the
    summed traffic of the rest; ``totals`` = summed traffic of ALL of them (computed before the
    cut). Counts use the same 60 min window as the list."""
    tel = tel or {}
    found: dict = {}
    partial = False

    def out_of_time():
        return deadline is not None and deadline.remaining() < DEADLINE_MIN_S
    root = Path(home) / ".claude" / "projects"
    dirs = []
    if scan and not out_of_time():
        try:
            dirs = list(root.glob(f"*/{session_id}/subagents"))
        except OSError:
            dirs = []
    elif scan:
        partial = True
    scanned = 0
    main_age = None
    for d in dirs:
        if status is not None and main_age is None:
            try:
                main_age = round(max(0.0, now - (d.parent.parent / f"{session_id}.jsonl")
                                     .stat().st_mtime), 1)
            except OSError:
                main_age = None
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for f in entries:
            m = _AGENT_FILE_RE.match(f.name)
            if not m or scanned >= SUBAGENT_SCAN_MAX:
                continue
            if scanned % 50 == 0 and out_of_time():
                partial = True
                break
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            scanned += 1
            aid = m.group(1)
            age = max(0.0, now - mtime)
            if age >= window_s and aid not in tel:
                continue
            meta, spawn = {}, None
            mp = f.with_name(f"agent-{aid}.meta.json")
            try:
                mst = mp.stat()
                spawn = mst.st_mtime
                if mst.st_size <= META_BYTES_MAX:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                meta = {}
            meta = meta if isinstance(meta, dict) else {}
            try:
                depth = int(meta.get("spawnDepth"))
            except (TypeError, ValueError):
                depth = None
            run_s = round(max(0.0, mtime - spawn), 1) if spawn is not None else None
            state, flags = _lifecycle(age, run_s, tel.get(aid))
            found[aid] = {"id": aid, "type": str(meta.get("agentType") or "?"),
                          "description": str(meta.get("description") or ""),
                          "depth": depth, "age_s": round(age, 1), "run_s": run_s,
                          "state": state, "flags": flags, "telemetry": tel.get(aid),
                          "last_s": _last_s(age, tel.get(aid), now)}
            if times:
                found[aid].update(spawn_ts=spawn, mtime=mtime)
        if partial:
            break
    for aid, st in tel.items():                         # proxy traffic with no log file seen
        if aid not in found:
            state, flags = _lifecycle(None, None, st)
            found[aid] = {"id": aid, "type": "?", "description": "", "depth": None,
                          "age_s": None, "run_s": None, "state": state, "flags": flags,
                          "telemetry": st, "last_s": _last_s(None, st, now)}
    if status is not None and not partial:
        mark_waiting(found, status, main_age)
    ordered = sorted(found.values(), key=_sub_sort_key)
    shown, rest = ordered[:keep], ordered[keep:]
    extra = {"main_age_s": main_age} if main_age is not None else {}
    return {**extra, "list": shown, "more": len(rest),
            "hidden": merge_stats(*[s.get("telemetry") for s in rest]),
            "totals": merge_stats(*[s.get("telemetry") for s in ordered]),
            "count": len(ordered),
            "running": sum(1 for s in ordered if s["state"] == "running"),
            "quiet": sum(1 for s in ordered if s["state"] == "quiet"),
            "flagged": sum(1 for s in ordered if s["flags"]),
            "waiting": sum(1 for s in ordered if s.get("waiting")),
            "scanned": bool(scan) and not partial, "partial": partial}


def session_totals(res: dict) -> dict:
    """Main thread + every subagent of one agent (merged before any display cut)."""
    res = res if isinstance(res, dict) else {}
    subs = res.get("subagents") if isinstance(res.get("subagents"), dict) else {}
    if "totals" in subs:
        parts = [subs.get("totals")]
    else:                                              # older shape: sum what is listed
        parts = [s.get("telemetry") for s in subs.get("list") or [] if isinstance(s, dict)]
    return merge_stats(res.get("telemetry"), *parts)


def agent_flagged(a: dict) -> bool:
    """A stuck or erroring agent: any flagged subagent (running > 20 min, recent errors, context,
    or ``waiting?`` > 10 min), main-thread errors in 60 min, or a known context window ≥
    CTX_WARN_PCT full."""
    res = a.get("res") if isinstance(a, dict) and isinstance(a.get("res"), dict) else {}
    subs = res.get("subagents") if isinstance(res.get("subagents"), dict) else {}
    if subs.get("flagged"):
        return True
    if any(isinstance(s, dict) and s.get("flags") for s in subs.get("list") or []):
        return True
    main = res.get("telemetry") or {}
    return err_flag(main) or ctx_flag(main)


def agent_active(a: dict) -> bool:
    """Active = Claude's own status says ``busy`` (``idle`` = idle), from
    ``~/.claude/sessions/<pid>.json``. Log mtime decides only when there is no status: something
    outside the session can touch an idle session's log."""
    if not isinstance(a, dict):
        return False
    res = a.get("res") if isinstance(a.get("res"), dict) else {}
    st = res.get("status")
    if st == "busy":
        return True
    if st == "idle":
        return False
    return a.get("state") == "active"


def rank_key(a: dict):
    """The menu's order of active sessions: output tokens (main + every subagent), then cpu."""
    res = a.get("res") if isinstance(a, dict) and isinstance(a.get("res"), dict) else {}
    tot = session_totals(res)
    tree = res.get("tree") or {}
    return (-(tot.get("tokens_out") or 0), -(tree.get("cpu_pct") or 0))
