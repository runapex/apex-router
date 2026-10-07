"""Read-only discovery of the agents running on this machine — the menu-bar widget's agent list.

The primary signal is file mtime, not ``ps``: an agent that is working appends to its session log,
and mtime also covers headless ``claude -p`` loops. Thresholds: modified < 5 min = active,
< 60 min = idle, older = not listed. What is read, and nothing more:

  Claude Code  ``~/.claude/projects/<slug>/*.jsonl`` (top level) — names and mtimes only; repo
               label decoded from the slug; subagents = ``<slug>/<session>/subagents/*.jsonl`` (or
               ``<slug>/subagents/*.jsonl``) modified < 5 min.
  pi           ``~/.pi/agent/sessions/**/*.jsonl`` — mtimes, plus the FIRST line only, for ``cwd``.
  Codex        ``~/.codex/sessions/YYYY/MM/DD/*.jsonl`` for today and yesterday — mtimes only.
  local worker ``launchctl list <label>`` (pid) + ``queue/jobs/{inbox,running}`` file counts.
  proxy        ``GET http://127.0.0.1:<port>/healthz`` (loopback, 1 s timeout).

No prompt or transcript content is read. Every collector fails open: an error becomes an
``error`` field, never an exception. Stdlib only; writes nothing.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from .ornith.queue_paths import queue_root

ACTIVE_S = 5 * 60
IDLE_S = 60 * 60
DEFAULT_WORKER_LABEL = "com.ornith.worker"
DEFAULT_PROXY_PORT = 8788
FIRST_LINE_MAX = 64 * 1024
_PID_RE = re.compile(r'"PID"\s*=\s*(\d+)\s*;')
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
SLUG_LIST_MAX = 5000
_USER_HOME_SLUG_RE = re.compile(r"^-(?:Users|home)-[^-]+-")
_UUID7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _home(home=None) -> Path:
    return Path(home).expanduser() if home else Path.home()


def _state(age_s: float) -> str | None:
    if age_s < ACTIVE_S:
        return "active"
    if age_s < IDLE_S:
        return "idle"
    return None


def _mtime(p: Path):
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _agent(kind: str, repo, session: str, mtime: float, now: float, **extra) -> dict | None:
    age = max(0.0, now - mtime)
    st = _state(age)
    if st is None:
        return None
    return {"kind": kind, "repo": repo, "session": short_id(session), "state": st,
            "age_s": round(age, 1), **extra}


def short_id(session: str) -> str:
    """8-char session label. A time-ordered UUIDv7 (pi, Codex) shares its leading hex across
    sessions started together, so its last 8 chars are used; anything else keeps the prefix."""
    if _UUID7_RE.match(session):
        return session[-8:]
    return session[:8]


# ---- Claude Code ----------------------------------------------------------------------------

def _slugify(name: str) -> str:
    return _NON_ALNUM_RE.sub("-", name)


def _resolve_slug(rest: str, base: Path, depth: int = 0):
    """Rebuild a real path from a slug remainder by listing directories: Claude stores a path
    with every non-alphanumeric as '-', so a child matches when its slugified name is a
    '-'-bounded prefix of ``rest``. Longest match first; bounded depth and listing size."""
    if depth > 32:
        return None
    try:
        children = []
        for i, c in enumerate(base.iterdir()):
            if i >= SLUG_LIST_MAX:
                break
            children.append((_slugify(c.name), c))
    except OSError:
        return None
    children.sort(key=lambda t: len(t[0]), reverse=True)
    for n, child in children:
        if not n:
            continue
        if rest == n:
            return child
        if rest.startswith(n + "-"):
            try:
                is_dir = child.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                found = _resolve_slug(rest[len(n) + 1:], child, depth + 1)
                if found is not None:
                    return found
    return None


def decode_slug(slug: str, home=None) -> str:
    """Best-effort repo label from a Claude project slug (``/a/b-c`` is stored as ``-a-b-c``).
    The last path component when the path resolves on disk; else the slug minus the home prefix.
    A Claude worktree (``<repo>/.claude/worktrees/<name>``) is labelled ``<repo>/<name>``."""
    head, sep, wt = slug.partition("--claude-worktrees-")
    path = _resolve_slug(head[1:], Path("/")) if head.startswith("-") and len(head) > 1 else None
    if path is not None:
        label = path.name or str(path)
    else:
        home_slug = _slugify(str(_home(home))) + "-"
        label = (head[len(home_slug):] if head.startswith(home_slug)
                 else _USER_HOME_SLUG_RE.sub("", head).lstrip("-"))
        label = label.removeprefix("src-") or slug
    return f"{label}/{wt}" if sep and wt else label


def _count_recent(paths, now: float) -> int:
    n = 0
    for p in paths:
        m = _mtime(p)
        if m is not None and now - m < ACTIVE_S:
            n += 1
    return n


def claude_agents(home=None, now: float | None = None) -> list:
    now = time.time() if now is None else now
    root = _home(home) / ".claude" / "projects"
    out = []
    try:
        projects = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return out
    for proj in projects:
        try:
            files = [f for f in proj.glob("*.jsonl") if f.is_file()]
        except OSError:
            continue
        shared = None
        for f in files:
            m = _mtime(f)
            if m is None or now - m >= IDLE_S:
                continue
            sub = _count_recent((proj / f.stem / "subagents").glob("*.jsonl"), now)
            if shared is None:
                shared = _count_recent((proj / "subagents").glob("*.jsonl"), now)
            a = _agent("claude", decode_slug(proj.name, home), f.stem, m, now,
                       subagents=sub + shared)
            shared = 0                          # a flat subagents/ dir is credited once
            if a:
                out.append(a)
    return out


# ---- pi -------------------------------------------------------------------------------------

def _first_line_cwd(path: Path):
    try:
        with open(path, "rb") as fh:
            line = fh.readline(FIRST_LINE_MAX)
        row = json.loads(line)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    cwd = row.get("cwd") if isinstance(row, dict) else None
    return cwd if isinstance(cwd, str) and cwd else None


def pi_agents(home=None, now: float | None = None) -> list:
    now = time.time() if now is None else now
    root = _home(home) / ".pi" / "agent" / "sessions"
    out = []
    try:
        files = list(root.rglob("*.jsonl"))
    except OSError:
        return out
    for f in files:
        m = _mtime(f)
        if m is None or now - m >= IDLE_S:
            continue
        cwd = _first_line_cwd(f)
        repo = Path(cwd).name if cwd else None
        session = f.stem.rpartition("_")[2] or f.stem
        a = _agent("pi", repo, session, m, now)
        if a:
            out.append(a)
    return out


# ---- Codex ----------------------------------------------------------------------------------

def codex_agents(home=None, now: float | None = None) -> list:
    now = time.time() if now is None else now
    root = _home(home) / ".codex" / "sessions"
    today = _dt.datetime.fromtimestamp(now)
    days = {today.date(), (today - _dt.timedelta(days=1)).date(),
            _dt.datetime.fromtimestamp(now, _dt.timezone.utc).date()}
    out = []
    for d in sorted(days):
        day_dir = root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
        try:
            files = list(day_dir.glob("*.jsonl"))
        except OSError:
            continue
        for f in files:
            m = _mtime(f)
            if m is None:
                continue
            session = f.stem.rsplit("-", 5)
            sid = "-".join(session[-5:]) if len(session) == 6 else f.stem
            a = _agent("codex", None, sid, m, now)
            if a:
                out.append(a)
    return out


def discover(home=None, now: float | None = None) -> list:
    """All coding agents seen active/idle, newest first. Never raises."""
    now = time.time() if now is None else now
    out = []
    for fn in (claude_agents, pi_agents, codex_agents):
        try:
            out.extend(fn(home, now))
        except Exception as e:  # noqa: BLE001 — one broken source must not hide the others
            out.append({"kind": fn.__name__.split("_")[0], "error": f"{type(e).__name__}: {e}"})
    out.sort(key=lambda a: a.get("age_s", float("inf")))
    return out


# ---- local worker ---------------------------------------------------------------------------

def _launchd_pid(label: str, timeout: float = 2.0):
    try:
        r = subprocess.run(["launchctl", "list", label], capture_output=True, text=True,
                           timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        return None, "not loaded"
    m = _PID_RE.search(r.stdout or "")
    return (int(m.group(1)) if m else None), None


def _count_files(d: Path):
    try:
        return sum(1 for p in d.iterdir() if p.is_file() and not p.name.startswith("."))
    except OSError:
        return None


def worker(label: str | None = None, queue: Path | None = None, pid_fn=None) -> dict:
    label = label or os.environ.get("APEX_WORKER_LABEL") or DEFAULT_WORKER_LABEL
    out: dict = {"label": label}
    try:
        pid, err = (pid_fn or _launchd_pid)(label)
        out["pid"] = pid
        out["running"] = pid is not None
        if err:
            out["error"] = err
        jobs = (Path(queue) if queue else queue_root()) / "jobs"
        out["inbox"] = _count_files(jobs / "inbox")
        out["queue_running"] = _count_files(jobs / "running")
    except Exception as e:  # noqa: BLE001 — fail open
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ---- proxy ----------------------------------------------------------------------------------

def proxy_port() -> int:
    try:
        return int(os.environ.get("APEX_PORT") or DEFAULT_PROXY_PORT)
    except ValueError:
        return DEFAULT_PROXY_PORT


def proxy(url: str | None = None, timeout: float = 1.0) -> dict:
    url = url or f"http://127.0.0.1:{proxy_port()}/healthz"
    out: dict = {"url": url, "up": False}
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            out["up"] = r.status == 200
            try:
                body = json.loads(r.read(64 * 1024))
            except ValueError:
                body = None
            if isinstance(body, dict):
                for k in ("version", "port"):
                    if k in body:
                        out[k] = body[k]
    except Exception as e:  # noqa: BLE001 — a down proxy is a state, not a failure
        out["error"] = f"{type(e).__name__}: {e}"
    return out
