"""Workflows for studio v2 (contract: ``docs/design/studio-v2/API.md``, section "Workflows"):
sessions > tasks > sub-agents > the decision line of the newest request.

Sources (read-only, every read capped, no subprocess):
  pi sessions   ``~/.pi/agent/sessions/<slug>/<ts>_<id>.jsonl`` — HEAD_TAIL bytes from the head and
                from the tail only: the ``session`` entry (cwd), message/compaction timestamps, and the
                ``agent_spawn`` toolResults (``details.id`` = job id). No message text is kept.
  pi jobs       ``<router home>/pi-jobs/<id>/meta.json`` (<= META_MAX bytes; names listed with a cap,
                newest JOBS_MAX) and the tail of ``result.md`` (the GO / NO-GO verdict only).
  Claude Code   ``~/.claude/projects/<slug>/<session>.jsonl`` and ``<session>/subagents/agent-<id>.jsonl``
                (+ ``.meta.json``: ``agentType`` only) — timestamps, cwd, gitBranch, agentId.
  telemetry     the main proxy rows of the window, joined by ``session_id`` (and ``agent_id`` for
                Claude Code sub-agents): request counts, tokens, the newest row's decisions.
  git           ``<repo>/.git/HEAD`` (or the worktree ``gitdir``) for the branch; 4 KiB reads.

Linking a job to its parent: exact when the parent's head/tail shows the agent_spawn result (or the
job's own session is ``apexjob-<parent job>``); else by process: the job's ``parent_pid`` equals the pid
of a parent job (depth 2), or the ``parent_pid`` of a job that the session links exactly, while its
``created`` time falls in that session's span. A job with no parent found is counted
(``skipped.jobs_unlinked``) and its session shows as a top-level session when it is in the window.

Privacy: labels come only from a sub-agent brief whose first line starts with ``TASK:`` (our own
text), cut to 60 characters with secret-like words masked; else "<repo> · <branch>". No prompt text,
message text, key or token leaves this module. ``workflows.jsonl`` rows hold ids and timestamps only.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import stat
import threading
import time
from collections import Counter
from pathlib import Path

from .. import agents as agents_mod
from . import decisions as decisions_mod
from . import live
from .flow import Paths

try:
    import fcntl
except ImportError:  # pragma: no cover — not POSIX
    fcntl = None

WINDOWS = {"24h": 86400.0, "7d": 7 * 86400.0}
SESSIONS_MAX = 200
JOBS_MAX = 2000
NAMES_MAX = 20000                 # names looked at per directory listing
SLUG_DIRS_MAX = 5000
HEAD_TAIL = 64 * 1024
META_MAX = 256 * 1024
RESULT_TAIL = 4096
GIT_MAX = 4096
DEPTH_MAX = 2
AGENTS_SHOWN = 12
GAP_S = 30 * 60.0
ACTIVE_S = 5 * 60.0
IDLE_S = 60 * 60.0
PID_SLACK_S = 2 * 3600.0          # a pid-linked job may start this long after the session's last seen point
LABEL_MAX = 60
STORE_TAIL = 1024 * 1024
STORE_IDS_MAX = 20
TELEMETRY_CAP = {"24h": 32 * 1024 * 1024, "7d": 64 * 1024 * 1024}
CACHE_TTL_S = 2.0
EVENT_MIN_S = 1.0
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
JOB_NAME_RE = re.compile(r"(\d{14})-[a-z0-9]{1,16}")
DEFAULT_BRANCHES = frozenset({"main", "master", "dev", "HEAD", ""})
ROLES = ("worker", "xval", "scout", "reviewer", "coordinator")
STATES = ("running", "queued", "done", "failed", "killed", "killed(cost)", "timed_out(idle)", "timed_out(wall)")
PROBLEM = ("failed", "killed", "killed(cost)", "timed_out(idle)", "timed_out(wall)")
CAUSES = (("killed(cost)", "stopped at the cost limit"), ("timed_out(wall)", "ran out of time"),
          ("timed_out(idle)", "stopped: no progress"), ("killed", "were stopped"), ("failed", "failed"))
_VERDICT_RE = re.compile(r"\b(NO-GO|GO)\b")
_SECRET_WORD_RE = re.compile(
    r"(?:sk-|sk_|ghp_|gho_|ghs_|github_pat_|xox[abpr]-|eyJ|AKIA|AIza|glpat-)[A-Za-z0-9._\-/+=]{6,}"
    r"|[A-Za-z0-9+/_=\-]{32,}")
_SESSION_FILE_RE = re.compile(r"(.+)_([^_]+)\.jsonl")


class WorkflowError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _ms_ts(v):
    v = _num(v)
    if v is None or v <= 0:
        return None
    return v / 1000.0 if v > 1e11 else float(v)


def _iso_ts(s):
    if not isinstance(s, str) or not 10 <= len(s) <= 40:
        return None
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


# ---- capped, non-blocking reads -------------------------------------------------------------------

class Reads:
    """Counters for one build: bytes read, files read, files skipped (bad, huge, not regular)."""

    def __init__(self):
        self.bytes = 0
        self.files = 0
        self.bad = 0


def _open_regular(path: Path):
    """An fd of a regular file, opened without blocking (a FIFO cannot hang the read), or None."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None, None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None, None
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        return None, None
    return fd, st


def read_capped(path: Path, cap: int, reads: Reads, tail: bool = False):
    """The whole file when it is at most ``cap`` bytes (``tail``: its last ``cap`` bytes), else None."""
    fd, st = _open_regular(path)
    if fd is None:
        return None
    try:
        size = st.st_size
        if size > cap and not tail:
            return None
        off = max(0, size - cap)
        data = os.pread(fd, min(cap, size), off)
    except OSError:
        return None
    finally:
        os.close(fd)
    reads.bytes += len(data)
    reads.files += 1
    return data


def head_tail_lines(path: Path, reads: Reads, cap: int = HEAD_TAIL):
    """Complete lines of the first and last ``cap`` bytes (all lines when the file is <= 2*cap).
    Returns (lines, size) or (None, 0) when the file is not a readable regular file."""
    fd, st = _open_regular(path)
    if fd is None:
        return None, 0
    try:
        size = st.st_size
        if size <= 2 * cap:
            data = os.pread(fd, size, 0)
            reads.bytes += len(data)
            parts = [data]
        else:
            h = os.pread(fd, cap, 0)
            t = os.pread(fd, cap, size - cap)
            reads.bytes += len(h) + len(t)
            h = h[:h.rfind(b"\n") + 1] if b"\n" in h else b""
            t = t[t.find(b"\n") + 1:] if b"\n" in t else b""
            parts = [h, t]
    except OSError:
        return None, 0
    finally:
        os.close(fd)
    reads.files += 1
    lines = []
    for p in parts:
        lines.extend(ln for ln in p.split(b"\n") if ln.strip())
    return lines, size


def _scan(d: Path, cap: int = NAMES_MAX):
    """(names, more): at most ``cap`` directory entries (name, is_dir, is_file); ``more`` = cut."""
    out = []
    try:
        with os.scandir(d) as it:
            for e in it:
                if len(out) >= cap:
                    return out, True
                try:
                    out.append((e.name, e.is_dir(follow_symlinks=False), e.is_file(follow_symlinks=False), e))
                except OSError:
                    continue
    except OSError:
        return [], False
    return out, False


# ---- git: repo name and branch -----------------------------------------------------------------------

_git_cache: dict = {}
_git_lock = threading.Lock()


def git_place(cwd, reads: Reads):
    """(repo, branch) for a working dir: ``.git/HEAD`` or a worktree's ``gitdir``. Cached 30 s."""
    if not isinstance(cwd, str) or not cwd.startswith("/") or len(cwd) > 1024:
        return None, None
    now = time.time()
    with _git_lock:
        hit = _git_cache.get(cwd)
        if hit and now - hit[0] < 30.0:
            return hit[1]
    out = _git_place(Path(cwd), reads)
    with _git_lock:
        _git_cache[cwd] = (now, out)
        if len(_git_cache) > 512:
            _git_cache.pop(next(iter(_git_cache)))
    return out


def _git_place(cwd: Path, reads: Reads):
    p = cwd
    for _ in range(12):
        g = p / ".git"
        try:
            is_dir, is_file = g.is_dir(), g.is_file()
        except OSError:
            is_dir = is_file = False
        if is_dir:
            return p.name or None, _head_branch(g / "HEAD", reads)
        if is_file:
            raw = read_capped(g, GIT_MAX, reads)
            m = re.match(rb"gitdir:\s*(\S+)", raw or b"")
            if m:
                gd = Path(m.group(1).decode("utf-8", "replace"))
                if not gd.is_absolute():
                    gd = (p / gd)
                main = gd.parent.parent if gd.parent.name == "worktrees" else None
                repo = main.parent.name if main is not None and main.name == ".git" else p.name
                return repo or None, _head_branch(gd / "HEAD", reads)
            return p.name or None, None
        if p.parent == p:
            break
        p = p.parent
    return (cwd.name or None), None


def _head_branch(head: Path, reads: Reads):
    raw = read_capped(head, GIT_MAX, reads)
    m = re.match(rb"ref:\s*refs/heads/(\S{1,200})", raw or b"")
    return m.group(1).decode("utf-8", "replace") if m else None


def _repo_of(cwd, reads):
    repo, branch = git_place(cwd, reads)
    if repo is None and isinstance(cwd, str) and cwd:
        repo = Path(cwd).name or None
    return repo, branch


# ---- labels -----------------------------------------------------------------------------------------

def mask(s: str) -> str:
    return _SECRET_WORD_RE.sub("<secret>", s)


def brief_label(task):
    """The label from a sub-agent brief: its first line only when it starts with ``TASK:``."""
    if not isinstance(task, str):
        return None
    first = task.lstrip().split("\n", 1)[0].strip()
    if not first.upper().startswith("TASK:"):
        return None
    text = mask(" ".join(first[5:].split()))
    if not text:
        return None
    return text if len(text) <= LABEL_MAX else text[:LABEL_MAX - 1].rstrip() + "…"


def repo_label(repo, branch) -> str:
    if repo and branch:
        return f"{repo} · {branch}"
    return repo or "session"


# ---- telemetry ----------------------------------------------------------------------------------------

class Tele:
    """Per session: [(ts, tokens)], the newest row, and per (session, agent_id) the same."""

    def __init__(self):
        self.by_sid: dict = {}
        self.by_agent: dict = {}

    def add(self, row: dict, ts: float, tok: dict):
        sid = row.get("session_id")
        if not isinstance(sid, str) or not sid or len(sid) > 200:
            return
        e = self.by_sid.setdefault(sid, {"pts": [], "last": None, "last_ts": -1.0})
        e["pts"].append((ts, tok))
        if ts >= e["last_ts"]:
            e["last"], e["last_ts"] = row, ts
        aid = row.get("agent_id")
        if isinstance(aid, str) and aid and len(aid) <= 64:
            a = self.by_agent.setdefault((sid, aid), {"n": 0, "last": None, "last_ts": -1.0})
            a["n"] += 1
            if ts >= a["last_ts"]:
                a["last"], a["last_ts"] = row, ts


def read_telemetry(paths: Paths, since: float, cap: int) -> Tele:
    from . import traffic          # late import: traffic imports this module for the stream
    t = Tele()
    for proxy in ("main", "local"):
        try:
            rows, _ = live.rows_in_window(paths.telemetry(proxy), since, cap=cap, rotated=True)
        except OSError:
            continue
        for r in rows:
            if not traffic.is_request(r):
                continue
            try:
                tok = traffic.tokens_of(r)
            except (TypeError, ValueError):
                continue
            t.add(r, r["_ts"], tok)
    return t


def _zero():
    return {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}


# ---- pi sessions -----------------------------------------------------------------------------------------

class Sess:
    __slots__ = ("id", "kind", "path", "mtime", "cwd", "repo", "branch", "points", "spawns", "start",
                 "claude_agents", "job", "job_children")

    def __init__(self, sid, kind, path, mtime):
        self.id, self.kind, self.path, self.mtime = sid, kind, path, mtime
        self.cwd = self.repo = self.branch = None
        self.points: list = []
        self.spawns: dict = {}        # job id -> ts of the agent_spawn result
        self.start = None
        self.claude_agents: list = []
        self.job = None               # the job of an apexjob-<id> session
        self.job_children: list = []


_sess_cache: dict = {}
_sess_lock = threading.Lock()


def _cached(path: Path, st, build):
    key = str(path)
    sig = (st.st_ino, st.st_size, st.st_mtime_ns)
    with _sess_lock:
        hit = _sess_cache.get(key)
        if hit and hit[0] == sig:
            return hit[1], True
    val = build()
    with _sess_lock:
        _sess_cache[key] = (sig, val)
        while len(_sess_cache) > 4 * SESSIONS_MAX:
            _sess_cache.pop(next(iter(_sess_cache)))
    return val, False


def _pi_parse(lines) -> dict:
    out = {"cwd": None, "points": [], "spawns": {}, "start": None, "bad": 0}
    for ln in lines:
        try:
            r = json.loads(ln)
        except (ValueError, UnicodeDecodeError):
            out["bad"] += 1
            continue
        if not isinstance(r, dict):
            continue
        typ = r.get("type")
        ts = _iso_ts(r.get("timestamp"))
        if typ == "session":
            cwd = r.get("cwd")
            if isinstance(cwd, str) and len(cwd) <= 1024:
                out["cwd"] = cwd
            if ts is not None:
                out["start"] = ts
            continue
        if typ not in ("message", "compaction") or ts is None:
            continue
        out["points"].append(ts)
        m = r.get("message")
        if typ == "message" and isinstance(m, dict) and m.get("role") == "toolResult" \
                and m.get("toolName") == "agent_spawn" and isinstance(m.get("details"), dict):
            jid = m["details"].get("id")
            if isinstance(jid, str) and JOB_NAME_RE.fullmatch(jid):
                out["spawns"][jid] = ts
    return out


def _claude_parse(lines, sub: bool) -> dict:
    out = {"cwd": None, "branch": None, "points": [], "agent_id": None, "bad": 0}
    for ln in lines:
        try:
            r = json.loads(ln)
        except (ValueError, UnicodeDecodeError):
            out["bad"] += 1
            continue
        if not isinstance(r, dict):
            continue
        ts = _iso_ts(r.get("timestamp"))
        if ts is None:
            continue
        if r.get("type") in ("user", "assistant"):
            out["points"].append(ts)
        cwd, br = r.get("cwd"), r.get("gitBranch")
        if isinstance(cwd, str) and len(cwd) <= 1024:
            out["cwd"] = cwd
        if isinstance(br, str) and 0 < len(br) <= 200:
            out["branch"] = br
        aid = r.get("agentId")
        if sub and isinstance(aid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", aid):
            out["agent_id"] = aid
    return out


def _list_pi(root: Path, since: float, skipped: Counter):
    """(mtime, sid, path) of every pi session file modified since ``since`` (capped listings)."""
    out = []
    dirs, more = _scan(root, SLUG_DIRS_MAX)
    if more:
        skipped["listing"] += 1
    for name, is_dir, _, e in dirs:
        if not is_dir:
            continue
        files, more = _scan(Path(e.path))
        if more:
            skipped["listing"] += 1
        for fname, f_is_dir, f_is_file, fe in files:
            if not fname.endswith(".jsonl"):
                continue
            try:
                m = fe.stat(follow_symlinks=False).st_mtime
            except OSError:
                skipped["files"] += 1
                continue
            if m < since:
                continue
            mm = _SESSION_FILE_RE.fullmatch(fname)
            sid = mm.group(2) if mm else fname[:-6]
            out.append((m, sid, Path(fe.path), "pi"))
    return out


def _list_claude(root: Path, since: float, skipped: Counter):
    out = []
    dirs, more = _scan(root, SLUG_DIRS_MAX)
    if more:
        skipped["listing"] += 1
    for name, is_dir, _, e in dirs:
        if not is_dir:
            continue
        files, more = _scan(Path(e.path))
        if more:
            skipped["listing"] += 1
        for fname, _, _, fe in files:
            if not fname.endswith(".jsonl"):
                continue
            try:
                m = fe.stat(follow_symlinks=False).st_mtime
            except OSError:
                skipped["files"] += 1
                continue
            sub = Path(e.path) / fname[:-6] / "subagents"
            sm = _newest_mtime(sub)
            m = max(m, sm or 0)
            if m < since:
                continue
            out.append((m, fname[:-6], Path(fe.path), "claude"))
    return out


def _newest_mtime(d: Path):
    newest = None
    files, _ = _scan(d, 500)
    for fname, _, is_file, fe in files:
        if not fname.endswith(".jsonl"):
            continue
        try:
            m = fe.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        newest = m if newest is None else max(newest, m)
    return newest


def _read_session(kind, sid, path: Path, mtime, reads: Reads, skipped: Counter):
    fd, st = _open_regular(path)
    if fd is None:
        skipped["files"] += 1
        return None
    os.close(fd)

    def build():
        lines, _ = head_tail_lines(path, reads)
        if lines is None:
            return None
        return _pi_parse(lines) if kind == "pi" else _claude_parse(lines, False)
    parsed, _ = _cached(path, st, build)
    if parsed is None or (not parsed["points"] and parsed.get("start") is None and parsed["cwd"] is None):
        skipped["files"] += 1
        return None
    s = Sess(sid, kind, path, mtime)
    s.cwd = parsed["cwd"]
    s.points = list(parsed["points"])
    if kind == "pi":
        s.spawns = dict(parsed["spawns"])
        s.start = parsed["start"] if parsed["start"] is not None else (min(s.points) if s.points else mtime)
        if not s.points:
            s.points.append(s.start)
    else:
        s.branch = parsed["branch"]
        s.start = min(s.points) if s.points else mtime
        if not s.points:
            s.points.append(mtime)
        s.claude_agents = _claude_subagents(path, reads, skipped)
    return s


def _claude_subagents(path: Path, reads: Reads, skipped: Counter) -> list:
    d = path.parent / path.stem / "subagents"
    files, _ = _scan(d, 500)
    out = []
    for fname, _, is_file, fe in files:
        if not (fname.startswith("agent-") and fname.endswith(".jsonl")):
            continue
        fp = Path(fe.path)
        fd, st = _open_regular(fp)
        if fd is None:
            skipped["files"] += 1
            continue
        os.close(fd)

        def build(fp=fp):
            lines, _ = head_tail_lines(fp, reads)
            if lines is None:
                return None
            p = _claude_parse(lines, True)
            meta = read_capped(fp.with_name(fp.name[:-6] + ".meta.json"), 64 * 1024, reads)
            atype = None
            try:
                mj = json.loads(meta) if meta else None
                atype = mj.get("agentType") if isinstance(mj, dict) else None
            except (ValueError, UnicodeDecodeError):
                pass
            p["role"] = _claude_role(atype)
            return p
        p, _ = _cached(fp, st, build)
        if p is None:
            skipped["files"] += 1
            continue
        aid = p["agent_id"] or fname[6:-6]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", aid):
            continue
        out.append({"id": aid, "role": p["role"], "branch": p["branch"],
                    "start": min(p["points"]) if p["points"] else st.st_mtime,
                    "last": max(p["points"]) if p["points"] else st.st_mtime, "mtime": st.st_mtime})
    out.sort(key=lambda a: a["start"])
    return out


def _claude_role(atype) -> str:
    a = atype.lower() if isinstance(atype, str) else ""
    if "explore" in a or "scout" in a or "search" in a:
        return "scout"
    if "review" in a:
        return "reviewer"
    if "plan" in a or "coordinat" in a:
        return "coordinator"
    return "worker"


# ---- pi jobs ----------------------------------------------------------------------------------------------

class Job:
    __slots__ = ("id", "role", "state", "cost", "cwd", "created", "started", "ended", "pid", "parent_pid",
                 "task", "branch", "repo", "verdict", "parent", "linked_by", "children")


def _job_time(name: str):
    m = JOB_NAME_RE.fullmatch(name)
    if not m:
        return None
    try:
        return _dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


def _list_jobs(root: Path, since: float, skipped: Counter, counts: Counter):
    entries, more = _scan(root)
    if more:
        skipped["listing"] += 1
    named = sorted(((t, n) for n, is_dir, _, _ in entries if is_dir and (t := _job_time(n)) is not None),
                   reverse=True)
    keep = named[:JOBS_MAX]
    skipped["jobs"] += len(named) - len(keep)
    counts["job_dirs"] = len(keep)
    # a job named (started) up to a day before the window can still run inside it
    return [n for t, n in keep if t >= since - 86400.0]


def _read_job(root: Path, name: str, reads: Reads, skipped: Counter):
    d = root / name
    raw = read_capped(d / "meta.json", META_MAX, reads)
    if raw is None:                          # missing, not a regular file, or over META_MAX
        skipped["jobs_bad"] += 1
        return None
    try:
        m = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        skipped["jobs_bad"] += 1
        return None
    if not isinstance(m, dict) or m.get("id") not in (None, name):
        skipped["jobs_bad"] += 1
        return None
    j = Job()
    j.id = name
    role = m.get("agent")
    j.role = role if role in ROLES else "worker"
    st = m.get("state")
    j.state = st if st in STATES else ("failed" if isinstance(st, str) and st.startswith(("fail", "error"))
                                       else "done" if m.get("ended") else "running")
    u = m.get("usage") if isinstance(m.get("usage"), dict) else {}
    c = _num(u.get("cost"))
    j.cost = round(float(c), 4) if c is not None else None
    j.cwd = m.get("cwd") if isinstance(m.get("cwd"), str) else None
    j.created = _ms_ts(m.get("created")) or _job_time(name)
    j.started = _ms_ts(m.get("started")) or j.created
    j.ended = _ms_ts(m.get("ended"))
    j.pid = m.get("pid") if isinstance(m.get("pid"), int) else None
    j.parent_pid = m.get("parent_pid") if isinstance(m.get("parent_pid"), int) else None
    j.task = m.get("task") if isinstance(m.get("task"), str) else None
    br = m.get("branch")
    j.branch = br if isinstance(br, str) and 0 < len(br) <= 200 else None
    j.repo = None
    j.verdict = None
    if j.role in ("xval", "reviewer") and j.ended:
        tail = read_capped(d / "result.md", RESULT_TAIL, reads, tail=True)
        j.verdict = verdict_of(tail)
    j.parent = None
    j.linked_by = None
    j.children = []
    return j


def verdict_of(raw) -> str | None:
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    for ln in reversed([x for x in text.splitlines() if x.strip()][-3:]):
        m = _VERDICT_RE.findall(ln)
        if m:
            return m[-1]
    return None


# ---- manual store -----------------------------------------------------------------------------------------

class Store:
    """``workflows.jsonl``: append-only rows ``{"ts", "op": "join"|"split", "sessions": [ids]}``, written
    under an exclusive flock; read from a capped tail. The latest row per session wins."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def _check(sid):
        if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
            raise WorkflowError("session ids must match [A-Za-z0-9_.:-], 1..128 characters")
        return sid

    def _append(self, row: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(row, separators=(",", ":")) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line)
        finally:
            os.close(fd)               # closing drops the lock
        with _cache_lock:
            _doc_cache.clear()

    def join(self, sessions, now: float | None = None):
        if not isinstance(sessions, list) or not 2 <= len(sessions) <= STORE_IDS_MAX:
            raise WorkflowError(f"sessions must be a list of 2..{STORE_IDS_MAX} ids")
        ids = [self._check(s) for s in sessions]
        if len(set(ids)) != len(ids):
            raise WorkflowError("session ids must differ")
        self._append({"ts": round(time.time() if now is None else now, 3), "op": "join", "sessions": ids})

    def split(self, session, now: float | None = None):
        sid = self._check(session)
        self._append({"ts": round(time.time() if now is None else now, 3), "op": "split", "sessions": [sid]})

    def rows(self) -> list:
        raw = read_capped(self.path, STORE_TAIL, Reads(), tail=True)
        if not raw:
            return []
        lines = raw.split(b"\n")
        try:
            if os.path.getsize(self.path) > STORE_TAIL:
                lines = lines[1:]
        except OSError:
            pass
        out = []
        for i, ln in enumerate(lines):
            try:
                r = json.loads(ln)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(r, dict) or r.get("op") not in ("join", "split") or _num(r.get("ts")) is None:
                continue
            ss = r.get("sessions")
            if not isinstance(ss, list) or not all(isinstance(s, str) and ID_RE.fullmatch(s) for s in ss):
                continue
            if (r["op"] == "join" and len(ss) < 2) or (r["op"] == "split" and len(ss) != 1):
                continue
            out.append((float(r["ts"]), i, r["op"], ss))
        out.sort()
        return out

    def state(self) -> dict:
        """The latest row per session wins: a session whose latest row is a split stands alone (no auto
        join); a join row holds each member that has no split after it. ``groups``: the live members of
        each join row (2 or more); ``joins`` = that count; ``splits`` = sessions whose latest row is a split."""
        rows = self.rows()
        last_split: dict = {}
        latest: dict = {}
        for k, (ts, i, op, ss) in enumerate(rows):
            for s in ss:
                latest[s] = op
                if op == "split":
                    last_split[s] = k
        groups = []
        for k, (ts, i, op, ss) in enumerate(rows):
            if op == "join":
                live_m = [s for s in ss if last_split.get(s, -1) < k]
                if len(live_m) >= 2:
                    groups.append(live_m)
        split = {s for s, op in latest.items() if op == "split"}
        return {"split": split, "groups": groups, "joins": len(groups), "splits": len(split)}


def store_for(paths: Paths) -> Store:
    return Store(paths.router_home / "workflows.jsonl")


# ---- build ------------------------------------------------------------------------------------------------

def _runs(points: list) -> list:
    pts = sorted(p for p in points if p is not None)
    runs = []
    for p in pts:
        if runs and p - runs[-1][1] <= GAP_S:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    return runs


def _state_of(last: float, now: float) -> str:
    age = now - last
    return "running" if age < ACTIVE_S else "idle" if age < IDLE_S else "done"


def _tree(nodes) -> list:
    out = []
    for n in nodes:
        out.append(n)
        out.extend(_tree(n.get("agents") or []))
    return out


def _cause(agents) -> str | None:
    c = Counter(a["state"] for a in _tree(agents) if a["state"] in PROBLEM)
    for st, words in CAUSES:
        if c.get(st):
            n = c[st]
            return f"{n} sub-agent{'s' if n != 1 else ''} {words}"
    return None


def _decisions(row, routes):
    return decisions_mod.decisions(row, routes) if row is not None else []


def _job_node(j: Job, tele: Tele, routes, depth: int, skipped: Counter, reads: Reads) -> dict:
    sid = f"apexjob-{j.id}"
    e = tele.by_sid.get(sid)
    if j.branch is None and j.cwd:
        j.repo, j.branch = _repo_of(j.cwd, reads)
    kids = sorted(j.children, key=lambda c: c.created or 0)
    shown = []
    if depth < DEPTH_MAX:
        shown = [_job_node(c, tele, routes, depth + 1, skipped, reads) for c in kids[:AGENTS_SHOWN]]
    else:
        skipped["depth"] += len(kids)
    node = {"id": j.id, "role": j.role, "state": j.state, "verdict": j.verdict, "cost": j.cost,
            "branch": j.branch, "start": j.started, "end": j.ended, "session_id": sid,
            "requests": len(e["pts"]) if e else 0, "decisions_last": _decisions(e["last"] if e else None, routes),
            "agents": shown, "linked_by": j.linked_by}
    if depth < DEPTH_MAX and len(kids) > AGENTS_SHOWN:
        node["agents_more"] = len(kids) - AGENTS_SHOWN
    return node


def _claude_node(a: dict, sid: str, tele: Tele, routes, now: float) -> dict:
    e = tele.by_agent.get((sid, a["id"]))
    running = now - a["mtime"] < ACTIVE_S
    return {"id": a["id"], "role": a["role"], "state": "running" if running else "done", "verdict": None,
            "cost": None, "branch": a["branch"], "start": a["start"], "end": None if running else a["last"],
            "session_id": sid, "requests": e["n"] if e else 0,
            "decisions_last": _decisions(e["last"] if e else None, routes), "agents": [], "linked_by": "file"}


def _session_node(s: Sess, tele: Tele, routes, now: float, since: float, skipped: Counter, reads: Reads):
    e = tele.by_sid.get(s.id)
    pts = list(s.points)
    if e:
        pts.extend(t for t, _ in e["pts"])
    kids = []                                       # (spawn ts, node, brief)
    if s.kind == "pi":
        for j in s.job_children:
            ts = s.spawns.get(j.id) or j.created or j.started
            pts.append(ts)
            kids.append((ts, j, j.task))
    else:
        for a in s.claude_agents:
            pts.append(a["start"])
            kids.append((a["start"], a, None))
    runs = _runs(pts)
    last = runs[-1][1] if runs else s.mtime
    any_running = False
    tasks = []
    for i, (a, b) in enumerate(runs, 1):
        tasks.append({"i": i, "a": a, "b": b, "kids": []})
    for ts, k, brief in sorted(kids, key=lambda x: x[0] or 0):
        tgt = None
        for t in tasks:
            if t["a"] <= ts <= t["b"]:
                tgt = t
                break
        if tgt is None:
            before = [t for t in tasks if t["a"] <= ts]
            tgt = before[-1] if before else (tasks[0] if tasks else None)
        if tgt is not None:
            tgt["kids"].append((k, brief))
    out_tasks = []
    for t in tasks:
        if t["b"] < since and t is not tasks[-1]:
            continue
        nodes, label = [], None
        for k, brief in t["kids"]:
            if label is None:
                label = brief_label(brief)
        for k, _ in t["kids"][:AGENTS_SHOWN]:
            if isinstance(k, Job):
                nodes.append(_job_node(k, tele, routes, 1, skipped, reads))
            else:
                nodes.append(_claude_node(k, s.id, tele, routes, now))
        tok, nreq = _zero(), 0
        if e:
            for ts, tk in e["pts"]:
                if t["a"] - 1 <= ts <= t["b"] + 1:
                    nreq += 1
                    for kk in tok:
                        tok[kk] += tk[kk]
        is_last = t is tasks[-1]
        live_kid = any(n["state"] in ("running", "queued") for n in _tree(nodes))
        running = live_kid or (is_last and now - t["b"] < GAP_S)
        any_running = any_running or live_kid
        cause = _cause(nodes)
        state = "problem" if cause else ("running" if running else "done")
        task = {"id": f"{s.id}:t{t['i']}", "label": label or repo_label(s.repo, s.branch),
                "label_from": "brief" if label else "repo", "start": round(t["a"], 3),
                "end": None if running else round(t["b"], 3), "state": state, "cause": cause,
                "tokens": tok, "requests": nreq, "agents": nodes}
        if len(t["kids"]) > AGENTS_SHOWN:
            task["agents_more"] = len(t["kids"]) - AGENTS_SHOWN
        out_tasks.append(task)
    st = "running" if any_running else _state_of(last, now)
    return {"id": s.id, "agent": s.kind, "short": agents_mod.short_id(s.id), "state": st,
            "start": round(s.start if s.start is not None else (runs[0][0] if runs else s.mtime), 3),
            "last": round(last, 3), "tasks": out_tasks}


class _UF:
    def __init__(self, keys):
        self.p = {k: k for k in keys}
        self.kinds: dict = {}

    def find(self, k):
        while self.p[k] != k:
            self.p[k] = self.p[self.p[k]]
            k = self.p[k]
        return k

    def union(self, a, b, kind):
        ra, rb = self.find(a), self.find(b)
        ks = self.kinds.pop(ra, set()) | self.kinds.pop(rb, set()) | {kind}
        if ra != rb:
            self.p[rb] = ra
        self.kinds[ra] = ks


def _group(nodes: list, sess: dict, manual: dict) -> list:
    ids = [n["id"] for n in nodes]
    uf = _UF(ids)
    split = manual["split"]
    by = {n["id"]: n for n in nodes}
    span = {n["id"]: (n["start"], n["last"]) for n in nodes}
    order = sorted(ids, key=lambda k: span[k][0])
    for x in range(len(order)):
        a = order[x]
        if a in split:
            continue
        sa = sess[a]
        for y in range(x + 1, len(order)):
            b = order[y]
            if b in split:
                continue
            sb = sess[b]
            if sa.repo and sa.repo == sb.repo and span[b][0] <= span[a][1] and span[a][0] <= span[b][1]:
                uf.union(a, b, "auto:repo-overlap")
            if sa.branch and sa.branch == sb.branch and sa.branch not in DEFAULT_BRANCHES:
                uf.union(a, b, "auto:branch")
    present = set(ids)
    for members in manual["groups"]:
        live_members = [m for m in members if m in present]
        for m in live_members[1:]:
            uf.union(live_members[0], m, "manual")
    comps: dict = {}
    for k in ids:
        comps.setdefault(uf.find(k), []).append(k)
    out = []
    for root, members in comps.items():
        members.sort(key=lambda k: span[k][0])
        ss = [by[k] for k in members]
        repos = Counter(sess[k].repo for k in members if sess[k].repo)
        repo = repos.most_common(1)[0][0] if repos else None
        branches = sorted({sess[k].branch for k in members if sess[k].branch})
        newest = max(members, key=lambda k: span[k][1])
        tasks = [t for s in ss for t in s["tasks"]]
        cause = next((t["cause"] for t in reversed(tasks) if t["state"] == "problem"), None)
        if any(t["state"] == "problem" for t in tasks):
            state = "problem"
        else:
            sts = {s["state"] for s in ss}
            state = "running" if "running" in sts else "idle" if "idle" in sts else "done"
        joined = sorted(uf.kinds.get(root, set()), key=["auto:repo-overlap", "auto:branch", "manual"].index)
        out.append({"id": "wf-" + hashlib.sha256(members[0].encode()).hexdigest()[:12],
                    "label": repo_label(repo or sess[newest].repo, sess[newest].branch or (branches[0] if branches else None)),
                    "repo": repo, "branches": branches, "state": state, "cause": cause, "joined_by": joined,
                    "start": min(s["start"] for s in ss),
                    "end": None if state in ("running", "idle") or any(s["state"] != "done" for s in ss)
                    else max(s["last"] for s in ss),
                    "sessions": ss})
    out.sort(key=lambda w: -max(s["last"] for s in w["sessions"]))
    return out


def collect(paths: Paths, window: str = "24h", now: float | None = None) -> dict:
    if window not in WINDOWS:
        raise WorkflowError("window must be 24h or 7d")
    now = time.time() if now is None else now
    since = now - WINDOWS[window]
    home = Path(paths.env.get("HOME") or Path.home())
    reads, skipped, counts = Reads(), Counter(), Counter()
    # 1. session files in the window, newest SESSIONS_MAX
    listed = _list_pi(home / ".pi" / "agent" / "sessions", since, skipped)
    listed += _list_claude(home / ".claude" / "projects", since, skipped)
    listed.sort(key=lambda t: -t[0])
    skipped["sessions"] += max(0, len(listed) - SESSIONS_MAX)
    sess: dict = {}
    for m, sid, path, kind in listed[:SESSIONS_MAX]:
        if sid in sess:
            continue
        s = _read_session(kind, sid, path, m, reads, skipped)
        if s is not None:
            sess[sid] = s
    counts["sessions"] = len(sess)
    # 2. jobs
    jroot = paths.router_home / "pi-jobs"
    jobs: dict = {}
    for name in _list_jobs(jroot, since, skipped, counts):
        j = _read_job(jroot, name, reads, skipped)
        if j is not None:
            jobs[name] = j
    counts["jobs"] = len(jobs)
    # 3. links: exact (spawn result in the parent's head/tail; apexjob-<id> sessions), then by pid
    for s in sess.values():
        if s.id.startswith("apexjob-") and s.id[8:] in jobs:
            s.job = jobs[s.id[8:]]
    owners: dict = {}                      # job id -> owner key ("s:<sid>" or "j:<job id>")
    for s in sess.values():
        for jid in s.spawns:
            j = jobs.get(jid)
            if j is None or j.parent is not None:
                continue
            if s.job is not None:
                if s.job.id != jid:
                    j.parent, j.linked_by = ("job", s.job.id), "spawn"
            else:
                j.parent, j.linked_by = ("sess", s.id), "spawn"
    by_pid: dict = {}
    for j in jobs.values():
        if j.pid is not None:
            by_pid.setdefault(j.pid, []).append(j)
    sess_pids: dict = {}                   # pid of a pi process -> [session ids it ran]
    for j in jobs.values():
        if j.parent and j.parent[0] == "sess" and j.parent_pid is not None:
            sess_pids.setdefault(j.parent_pid, set()).add(j.parent[1])
    for j in sorted(jobs.values(), key=lambda x: x.created or 0):
        if j.parent is not None or j.parent_pid is None or j.created is None:
            continue
        for p in by_pid.get(j.parent_pid, []):
            if p is not j and (p.started or 0) - 5 <= j.created <= (p.ended or now) + 5:
                j.parent, j.linked_by = ("job", p.id), "pid"
                break
        if j.parent is not None:
            continue
        best = None
        for sid in sess_pids.get(j.parent_pid, ()):
            s = sess[sid]
            lo, hi = s.start or min(s.points), max(s.points + [s.mtime])
            if lo - 5 <= j.created <= hi + PID_SLACK_S:
                d = 0 if j.created <= hi else j.created - hi
                if best is None or d < best[0]:
                    best = (d, sid)
        if best is not None:
            j.parent, j.linked_by = ("sess", best[1]), "pid"
    # 4. attach
    for j in jobs.values():
        if j.parent is None:
            continue
        kind, pid = j.parent
        if kind == "job" and pid in jobs:
            jobs[pid].children.append(j)
        elif kind == "sess" and pid in sess:
            sess[pid].job_children.append(j)
    # a sub-agent session joins its parent: it is not a top-level session when its job has a parent
    top = []
    for s in sess.values():
        if s.job is not None and s.job.parent is not None:
            continue
        top.append(s)
    for j in jobs.values():
        if j.parent is None:
            if f"apexjob-{j.id}" in sess:
                sess[f"apexjob-{j.id}"].job_children = sorted(j.children, key=lambda c: c.created or 0)
            elif (j.ended or now) >= since:
                skipped["jobs_unlinked"] += 1
    # 5. nodes
    for s in top:
        if s.repo is None:
            if s.kind == "pi" or not s.branch:
                repo, branch = _repo_of(s.cwd, reads)
                s.repo, s.branch = repo, s.branch or branch
            else:
                s.repo = _repo_of(s.cwd, reads)[0]
    cap = TELEMETRY_CAP[window]
    tele = read_telemetry(paths, since, cap)
    routes = decisions_mod.route_index_cached(paths.router_home / "route_log.jsonl")
    nodes = [_session_node(s, tele, routes, now, since, skipped, reads) for s in top]
    manual = store_for(paths).state()
    wfs = _group(nodes, {s.id: s for s in top}, manual)
    counts["bytes_read"] = reads.bytes
    counts["files_read"] = reads.files
    sk = {k: int(skipped.get(k, 0)) for k in ("sessions", "files", "jobs", "jobs_bad", "jobs_unlinked",
                                              "depth", "listing")}
    complete = not any(sk[k] for k in ("files", "jobs_bad", "listing", "sessions"))
    return {"ts": round(now, 3), "window": window, "workflows": wfs,
            "manual": {"joins": manual["joins"], "splits": manual["splits"]},
            "counts": {k: int(v) for k, v in counts.items()}, "skipped": sk, "complete": complete}


_doc_cache: dict = {}
_cache_lock = threading.Lock()


def collect_cached(paths: Paths, window: str = "24h") -> dict:
    """One build per CACHE_TTL_S per (home, window); a join or split clears the cache."""
    if window not in WINDOWS:
        raise WorkflowError("window must be 24h or 7d")
    key = (window, str(paths.router_home), paths.env.get("HOME"))
    now = time.time()
    with _cache_lock:
        hit = _doc_cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    doc = collect(paths, window, now)
    with _cache_lock:
        _doc_cache[key] = (now, doc)
        if len(_doc_cache) > 8:
            _doc_cache.pop(next(iter(_doc_cache)))
    return doc


def clear_caches():
    with _cache_lock:
        _doc_cache.clear()
    with _sess_lock:
        _sess_cache.clear()
    with _git_lock:
        _git_cache.clear()


# ---- SSE: workflow events -------------------------------------------------------------------------------

class EventState:
    """Per stream: the last sent signature per node, the time of the last event per node, and held
    changes (a node changes again inside EVENT_MIN_S: it goes out once the second has passed)."""

    def __init__(self):
        self.sig: dict = {}
        self.sent_at: dict = {}
        self.base = False


def _nodes(doc: dict):
    for w in doc.get("workflows") or []:
        for s in w.get("sessions") or []:
            for t in s.get("tasks") or []:
                tnode = {k: v for k, v in t.items() if k != "agents"}
                yield ("task", t["id"]), {"kind": "task", "workflow": w["id"], "session": s["id"], "task": t["id"],
                                          "parent": None, "node": t}, json.dumps(tnode, sort_keys=True, default=str)
                stack = [(a, None) for a in t.get("agents") or []]
                while stack:
                    a, parent = stack.pop()
                    anode = {k: v for k, v in a.items() if k != "agents"}
                    yield ("agent", a["id"]), {"kind": "agent", "workflow": w["id"], "session": s["id"],
                                               "task": t["id"], "parent": parent, "node": a}, \
                        json.dumps(anode, sort_keys=True, default=str)
                    stack.extend((c, a["id"]) for c in a.get("agents") or [])


def node_events(state: EventState, doc: dict, now: float) -> list:
    """The ``workflow`` events for the nodes that changed since the last call: at most one per node
    per EVENT_MIN_S. The first call sets the baseline (the client has the full GET)."""
    out = []
    seen = set()
    for key, ev, sig in _nodes(doc):
        seen.add(key)
        if not state.base:
            state.sig[key] = sig
            continue
        if state.sig.get(key) == sig:
            continue
        if now - state.sent_at.get(key, -1e18) < EVENT_MIN_S:
            continue                      # held: the next call sends it
        state.sig[key] = sig
        state.sent_at[key] = now
        out.append(ev)
    if state.base:
        for key in [k for k in state.sig if k not in seen]:
            state.sig.pop(key, None)
            state.sent_at.pop(key, None)
    state.base = True
    return out
