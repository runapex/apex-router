"""Which process is which session: Claude Code via ``~/.claude/sessions/<pid>.json``, pi /
Codex via one ``lsof`` cwd lookup, and the live pi / Codex processes no listed session accounts
for (``quiet_procs``)."""
from __future__ import annotations

import calendar
import json
import os
import time
from pathlib import Path

from .base import _LSTART_FMT, run_cmd


LSOF_PIDS_MAX = 40                # pi / codex pids whose cwd one lsof call looks up
QUIET_PROCS_MAX = 8               # live pi / codex processes with no recent session log, listed
SESSION_BYTES_MAX = 64 * 1024
START_TOLERANCE_S = 2.0


# ---- Claude sessions ------------------------------------------------------------------------

def _start_matches(proc_start: str, actual: float | None) -> bool | None:
    """True/False when both start times are known; None when either is not. Claude writes
    ``procStart`` in UTC on this machine (VERIFIED 2026-10-06); local time is also accepted."""
    if not isinstance(proc_start, str) or actual is None:
        return None
    try:
        st = time.strptime(" ".join(proc_start.split()), _LSTART_FMT)
    except ValueError:
        return None
    for cand in (calendar.timegm(st), time.mktime(st)):
        if abs(cand - actual) <= START_TOLERANCE_S:
            return True
    return False


def claude_sessions(home: Path, table: dict) -> dict:
    """sessionId -> {pid, status, name, verified} for LIVE Claude processes only."""
    out = {}
    d = Path(home) / ".claude" / "sessions"
    try:
        files = list(d.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            if f.stat().st_size > SESSION_BYTES_MAX:
                continue
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        pid, sid = doc.get("pid"), doc.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sid, str) or pid not in table:
            continue                                   # dead pid: stale file
        proc_start, actual = doc.get("procStart"), table[pid].get("start")
        match = _start_matches(proc_start, actual)
        if match is False:
            continue                                   # pid reused by another process
        if match is None and proc_start is not None and actual is not None:
            continue                                   # unverifiable procStart: never a match
        if match is None and "claude" not in table[pid]["name"].lower():
            continue
        out[sid] = {"pid": pid, "status": doc.get("status") if isinstance(doc.get("status"), str)
                    else None, "name": doc.get("name") if isinstance(doc.get("name"), str) else None,
                    "verified": "procStart" if match else "comm"}
    return out


# ---- pi / Codex via lsof cwd ----------------------------------------------------------------

def parse_lsof_cwd(text: str) -> dict:
    """``lsof -Fpn`` output (``p<pid>`` then ``n<path>`` lines) -> {pid: cwd}."""
    out, pid = {}, None
    for line in (text or "").splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None and pid not in out:
            out[pid] = line[1:]
    return out


def lsof_cwds(pids, run=run_cmd, timeout: float = 2.0) -> dict:
    pids = sorted({int(p) for p in pids})
    if not pids:
        return {}
    return parse_lsof_cwd(run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p",
                               ",".join(str(p) for p in pids)], timeout=timeout))


def match_by_cwd(agents: list, table: dict, cwds: dict) -> dict:
    """agent index -> pid for pi/codex agents; only unambiguous pairs (one process of that kind in
    the cwd, one listed session of that kind in the cwd). Ambiguous indexes map to a count."""
    procs: dict = {}
    for pid, cwd in cwds.items():
        if pid in table:
            procs.setdefault((table[pid]["name"], os.path.realpath(cwd)), []).append(pid)
    sessions: dict = {}
    for i, a in enumerate(agents):
        if a.get("kind") in ("pi", "codex") and a.get("cwd"):
            sessions.setdefault((a["kind"], os.path.realpath(a["cwd"])), []).append(i)
    out = {}
    for key, idx in sessions.items():
        cand = procs.get(key, [])
        if len(cand) == 1 and len(idx) == 1:
            out[idx[0]] = cand[0]
        elif cand:
            for i in idx:
                out[i] = {"ambiguous": len(cand), "sessions": len(idx)}
    return out


def quiet_procs(agents: list, table: dict, cwds: dict, matched, kinds=("pi", "codex"),
                cap: int = QUIET_PROCS_MAX) -> list:
    """Live pi / Codex processes that no listed session accounts for: their session log has not
    been written in the last hour (``agents.discover`` lists only those), yet they still hold
    memory. A process is left out when it was matched to a session (``matched``), when a listed
    session of its kind shares its cwd (it may be that one), when its cwd is unknown and any
    session of its kind is listed, or when its parent is a process of the same kind (counted in
    that tree). ``[{pid, kind, cwd_name}]`` newest first, at most ``cap``; ``cwd_name`` is the
    cwd's basename (None when lsof did not give one) — never the full path."""
    listed: dict = {}
    for a in agents:
        if isinstance(a, dict) and a.get("kind") in kinds:
            cwd = a.get("cwd")
            listed.setdefault(a["kind"], set()).add(os.path.realpath(cwd) if cwd else None)
    matched = set(matched)
    out = []
    for pid, p in table.items():
        kind = p.get("name")
        if kind not in kinds or pid in matched:
            continue
        parent = table.get(p.get("ppid"))
        if parent is not None and parent.get("name") == kind:
            continue
        cwd = cwds.get(pid)
        if cwd is None and listed.get(kind):
            continue
        if cwd is not None and os.path.realpath(cwd) in listed.get(kind, set()):
            continue
        out.append({"pid": pid, "kind": kind,
                    "cwd_name": (os.path.basename(cwd.rstrip("/")) or "/") if cwd else None,
                    "_start": p.get("start")})
    out.sort(key=lambda q: (-(q["_start"] or 0), q["pid"]))
    for q in out:
        q.pop("_start")
    return out[:cap]
