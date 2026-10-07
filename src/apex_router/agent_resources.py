"""Per-agent resources and the call graph for the menu-bar widget — read-only, fail-open.

Ties each agent from ``agents.discover()`` to the OS processes it runs and the model calls it
makes. What is read, and nothing more:

  process table  ONE ``ps -axo pid=,ppid=,rss=,%cpu=,lstart=,comm=``, plus at most ONE
                 ``ps -o pid=,args= -p <pids>`` for interpreter processes (node, python, ruby, …)
                 inside agent trees (≤ 40 pids). From ``args`` only the basename of the first
                 script argument is kept (``pyright-langserver``); the rest of argv is discarded
                 and never stored or shown.
  libproc        ``proc_pid_rusage(pid, RUSAGE_INFO_V2)`` via ctypes (macOS, unprivileged for the
                 user's own processes): physical footprint, disk bytes read/written, cpu time.
                 ``user``/``system`` are mach absolute-time ticks, converted with
                 ``mach_timebase_info`` (125/3 on Apple Silicon), not nanoseconds. Sampled TWICE
                 ~250 ms apart for the busiest ≤ 40 agent-tree pids: the difference gives the true
                 cpu % and disk read/write MB/s now. Lifetime io and cpu seconds are tooltip detail.
  Claude Code    ``~/.claude/sessions/<pid>.json`` (``*.json`` only) — exact pid -> sessionId and
                 Claude's own busy/idle status. A file whose pid is dead, or whose ``procStart``
                 does not match the live process's start time, is ignored. Subagent sidecars
                 ``agent-<id>.meta.json`` (agentType, description, spawnDepth; its mtime is the
                 spawn time) and the mtime of ``agent-<id>.jsonl`` (the last write).
  pi / Codex     ONE ``lsof -a -d cwd -Fpn -p <pids>`` maps the process to its cwd; a process is
                 credited to a session only when it is the sole process of its kind in that cwd
                 and the cwd has one listed session of that kind.
  GPU            ``ioreg -r -d 1 -c IOAccelerator`` — SYSTEM-WIDE utilisation and in-use memory.
                 Per-process GPU needs root and is not attempted.
  ollama         ``GET http://127.0.0.1:11434/api/ps`` — loaded models, VRAM, unload time.
  telemetry      the last 60 min of ``~/.apex/telemetry.jsonl`` via ``pressure.tail_rows``:
                 requests, the token split (uncached input / cache read / cache write /
                 output), errors, ttft and the context size of the LATEST request, split by
                 session (main thread, agent_id null) and by subagent (agent_id). Only proxy
                 traffic is seen.

No dollar figures: tokens, cache share and context fill only. The context window comes from a
family table (``context_window``): Opus / Sonnet >= 4.6 and Fable -> 1M, Haiku 4.5 -> 200k, a
``[1m]`` suffix -> 1M (VERIFIED 2026-10-06 against the pi 0.99.1 model catalog and a live
283k-token request on claude-opus-5-5). Any other id has NO assumed window — its size is shown
absolute — unless a successful request of the same thread and model exceeded 200k in the window,
which proves 1M.

A request error flags an agent only when it is recent (last 5 min) or the 60-min error rate is
>= 5% (``err_flag``); one old transient error does not.

All subprocess and network calls share one deadline (``DEADLINE_S``, the rate-sample window
included), and so does the subagent filesystem scan; a source that would start after it is
skipped and recorded in ``system.errors``. Subagent logs are scanned only for the active sessions
the menu shows (``SCAN_SESSIONS_MAX``, ranked like the menu); every other session gets its
subagents from proxy traffic alone. The widget's own process, its descendants
and its ancestors below the session root are excluded from every tree, so a refresh run inside a
session does not count itself. Subagents run inside their Claude session's process, so OS
resources are per session, never per subagent; a subagent's "load" is its proxy traffic. No
prompt or transcript content is read.
"""
from __future__ import annotations

import calendar
import ctypes
import ctypes.util
import datetime as _dt
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

from . import pressure

TELEMETRY_WINDOW_S = 60 * 60
SUBAGENT_RUNNING_S = 60
SUBAGENT_QUIET_S = 5 * 60
SUBAGENT_STUCK_S = 20 * 60
SUBAGENTS_MAX = 8                 # subagents kept per session (the rest are summed into "hidden")
SUBAGENT_SCAN_MAX = 500           # log files looked at per session
SCAN_SESSIONS_MAX = 8             # sessions whose subagent logs are scanned (= the menu's ACTIVE_MAX)
PROCS_TOP = 3
GRAPH_PROCS_MAX = 5
GRAPH_MODELS_MAX = 8
GRAPH_SESSIONS_MAX = 30
TREE_PIDS_MAX = 300
RATE_PIDS_MAX = 40                # pids sampled twice for cpu % / disk MB/s
ARGS_PIDS_MAX = 40                # interpreter pids whose script name is looked up
RATE_SAMPLE_S = 0.25
DEADLINE_S = 0.45                 # all subprocess + network timeouts + the rate sample together
DEADLINE_MIN_S = 0.05             # a source is skipped when less than this remains
META_BYTES_MAX = 64 * 1024
SESSION_BYTES_MAX = 64 * 1024
START_TOLERANCE_S = 2.0
OLLAMA_URL = "http://127.0.0.1:11434/api/ps"
_MB = 1024 * 1024
_LSTART_FMT = "%a %b %d %H:%M:%S %Y"
_IOREG_UTIL_RE = re.compile(r'"Device Utilization %"\s*=\s*(\d+)')
_IOREG_MEM_RE = re.compile(r'"In use system memory"\s*=\s*(\d+)')
_AGENT_FILE_RE = re.compile(r"^agent-([A-Za-z0-9_-]+)\.jsonl$")

_sleep = time.sleep               # module attributes so tests can fake them
_clock = time.monotonic


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def _mb(n_bytes):
    return round(n_bytes / _MB, 1) if isinstance(n_bytes, (int, float)) else None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean_text(s) -> str:
    """One line of display text: whitespace runs (newline, tab, …) -> one space, every other C0/C1
    control character (ESC, DEL, 0x80-0x9f) removed, so a description cannot drive a terminal or
    break a menu line."""
    s = _CTRL_RE.sub(lambda m: " " if m.group().isspace() else "", str(s))
    return " ".join(s.split())


def run_cmd(argv, timeout: float = 2.0) -> str:
    """stdout of a read-only command. Raises on failure — callers record it and fail open."""
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                       errors="replace")
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError(f"{argv[0]} exit {r.returncode}")
    return r.stdout


def http_get(url: str, timeout: float = 1.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(256 * 1024)


class DeadlineSkip(Exception):
    """A source was not started: the shared deadline had (nearly) passed."""


class Deadline:
    """One time budget shared by every subprocess / network call of a refresh."""

    def __init__(self, budget: float = DEADLINE_S, clock=None):
        self.clock = clock or _clock
        self.end = self.clock() + budget

    def remaining(self) -> float:
        return self.end - self.clock()

    def timeout(self, cap: float) -> float:
        """min(cap, time left); raises DeadlineSkip when less than DEADLINE_MIN_S is left."""
        left = self.remaining()
        if left < DEADLINE_MIN_S:
            raise DeadlineSkip("deadline exceeded, source skipped")
        return min(cap, left)


# ---- formatting helpers (shared with snapshot) -----------------------------------------------

def fmt_dur(s) -> str:
    if not isinstance(s, (int, float)) or not math.isfinite(s):
        return "?"
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def fmt_mem(mb) -> str:
    if not isinstance(mb, (int, float)) or not math.isfinite(mb):
        return "?MB"
    return f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb:.0f}MB"


def _k(n) -> str:
    if not isinstance(n, (int, float)):
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


# ---- process table --------------------------------------------------------------------------

def _parse_lstart(tokens) -> float | None:
    try:
        return time.mktime(time.strptime(" ".join(tokens), _LSTART_FMT))
    except (ValueError, OverflowError):
        return None


def parse_ps(text: str) -> dict:
    """``pid ppid rss_kb %cpu <lstart: 5 tokens> comm…`` lines -> {pid: row}. Bad lines skipped."""
    out = {}
    for line in (text or "").splitlines():
        t = line.split()
        if len(t) < 10:
            continue
        try:
            pid, ppid, rss = int(t[0]), int(t[1]), int(t[2])
            cpu = float(t[3])
        except ValueError:
            continue
        comm = " ".join(t[9:])
        out[pid] = {"pid": pid, "ppid": ppid, "rss_kb": rss, "cpu": cpu,
                    "start": _parse_lstart(t[4:9]), "comm": comm,
                    "name": os.path.basename(comm) or comm}
    return out


def ps_table(run=run_cmd, timeout: float = 2.0) -> dict:
    return parse_ps(run(["ps", "-axo", "pid=,ppid=,rss=,%cpu=,lstart=,comm="], timeout=timeout))


def children_map(table: dict) -> dict:
    kids: dict = {}
    for p in table.values():
        kids.setdefault(p["ppid"], []).append(p["pid"])
    return kids


def descendants(table: dict, root: int, kids: dict | None = None,
                cap: int = TREE_PIDS_MAX) -> list:
    """Pids below ``root`` (breadth-first, cycle-safe, at most ``cap``); ``root`` excluded."""
    kids = children_map(table) if kids is None else kids
    out, seen, queue = [], {root}, list(kids.get(root, ()))
    while queue and len(out) < cap:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        queue.extend(kids.get(pid, ()))
    return out


def self_exclusion(table: dict, roots, self_pid: int | None = None,
                   kids: dict | None = None) -> set:
    """Pids to leave out of every tree: the widget itself, its descendants, and — when it runs
    inside an agent's tree — its ancestors below that agent's root pid (the shell that ran it)."""
    me = os.getpid() if self_pid is None else self_pid
    out = {me} | set(descendants(table, me, kids))
    roots = set(roots)
    chain, cur, seen = [], table.get(me, {}).get("ppid"), {me}
    while isinstance(cur, int) and cur not in seen and cur in table:
        if cur in roots:
            out.update(chain)                     # only when a root is above us
            break
        seen.add(cur)
        chain.append(cur)
        cur = table[cur]["ppid"]
    return out


# ---- script names for interpreter processes -------------------------------------------------

_INTERPRETERS = {"node", "bun", "deno", "ruby", "perl", "php", "java", "npx", "tsx", "ts-node",
                 "uv", "uvx", "Python", "python", "python3"}
_INTERP_RE = re.compile(r"^python\d(\.\d+)?t?$")
_ABORT_FLAGS = {"-c", "-e", "--eval", "-p", "--print", "-E"}      # inline code: never a name
_VALUE_FLAGS = {"-X", "-W", "-r", "--require", "--import", "--loader", "--experimental-loader",
                "-cp", "-classpath", "--cwd", "-I"}
_SUBCOMMANDS = {"run", "exec", "x", "tool"}
_SCRIPT_EXT = (".js", ".mjs", ".cjs", ".ts", ".mts", ".py", ".rb", ".pl", ".php", ".jar")
_LABEL_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,40}$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,39}$")


def is_interpreter(name: str) -> bool:
    return name in _INTERPRETERS or bool(_INTERP_RE.match(name or ""))


def script_label(args: str) -> str | None:
    """Basename of the FIRST script argument of an interpreter command line, or None.

    Only one token is ever considered, and only when it looks like a script path (absolute or
    relative path, or a bare file with a script extension) or a ``-m`` module name; inline code
    (``-c``/``-e``) yields None. Nothing else from argv is returned, so a secret in an argument
    cannot reach the menu."""
    toks = (args or "").split()[1:]
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok in _ABORT_FLAGS:
            return None
        if tok == "-m":
            mod = toks[i + 1] if i + 1 < len(toks) else ""
            return mod if _MODULE_RE.match(mod) else None
        if tok in _VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-") or tok in _SUBCOMMANDS:
            i += 1
            continue
        pathy = tok.startswith(("/", "./", "../", "~"))
        if not pathy and ("/" in tok or not tok.endswith(_SCRIPT_EXT)):
            return None
        base = os.path.basename(tok.rstrip("/"))
        for ext in _SCRIPT_EXT:
            if base.endswith(ext) and len(base) > len(ext):
                base = base[: -len(ext)]
                break
        return base if _LABEL_RE.match(base) else None
    return None


def parse_ps_args(text: str) -> dict:
    """``pid args…`` lines -> {pid: script label}; lines without a usable label are dropped and
    the raw args are not kept."""
    out = {}
    for line in (text or "").splitlines():
        t = line.strip().split(None, 1)
        if len(t) != 2:
            continue
        try:
            pid = int(t[0])
        except ValueError:
            continue
        lab = script_label(t[1])
        if lab:
            out[pid] = lab
    return out


def script_labels(table: dict, pids, run=run_cmd, timeout: float = 1.0) -> dict:
    want = [p for p in pids if p in table and is_interpreter(table[p]["name"])][:ARGS_PIDS_MAX]
    if not want:
        return {}
    return parse_ps_args(run(["ps", "-o", "pid=,args=", "-p", ",".join(str(p) for p in want)],
                             timeout=timeout))


# ---- libproc rusage -------------------------------------------------------------------------

class _RusageV2(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "user", "system", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired", "resident",
        "phys_footprint", "start_abstime", "exit_abstime", "child_user", "child_system",
        "child_pkg_idle_wkups", "child_interrupt_wkups", "child_pageins",
        "child_elapsed_abstime", "diskio_bytesread", "diskio_byteswritten")]


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_LIBS: dict = {}


def mach_ticks_to_s(ticks: int, numer: int, denom: int) -> float:
    """Mach absolute-time ticks -> seconds: ticks * numer / denom is nanoseconds."""
    if not denom:
        return 0.0
    return ticks * numer / denom / 1e9


def _libs():
    if "proc" not in _LIBS:
        if sys.platform != "darwin":
            raise OSError("libproc is macOS-only")
        proc = ctypes.CDLL(ctypes.util.find_library("proc") or "libproc.dylib", use_errno=True)
        proc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_RusageV2)]
        proc.proc_pid_rusage.restype = ctypes.c_int
        libc = ctypes.CDLL(None)
        tb = _Timebase()
        libc.mach_timebase_info(ctypes.byref(tb))
        _LIBS.update(proc=proc, numer=tb.numer, denom=tb.denom)
    return _LIBS


def rusage(pid: int) -> dict | None:
    """{footprint_mb, read_mb, write_mb, cpu_s} for one pid, or None (not macOS, gone, denied).
    Values are unrounded so two samples can be differenced."""
    try:
        lib = _libs()
        ru = _RusageV2()
        if lib["proc"].proc_pid_rusage(int(pid), 2, ctypes.byref(ru)) != 0:
            return None
    except Exception:  # noqa: BLE001 — fail open: no rusage is a missing metric, not an error
        return None
    return {"footprint_mb": ru.phys_footprint / _MB, "read_mb": ru.diskio_bytesread / _MB,
            "write_mb": ru.diskio_byteswritten / _MB,
            "cpu_s": mach_ticks_to_s(ru.user + ru.system, lib["numer"], lib["denom"])}


def _safe_rusage(fn, pid):
    try:
        r = fn(pid)
    except Exception:  # noqa: BLE001
        return None
    return r if isinstance(r, dict) else None


def rates_from_samples(s1: dict, s2: dict, dt: float) -> dict:
    """{pid: {cpu_pct, read_mbs, write_mbs}} from two rusage samples ``dt`` seconds apart. A pid
    missing from either sample, or a counter that went backwards (pid reused), is skipped."""
    out = {}
    if not dt or dt <= 0:
        return out
    for pid, b in s2.items():
        a = s1.get(pid)
        if not a or not b:
            continue
        d = {}
        for k_in, k_out, scale in (("cpu_s", "cpu_pct", 100.0), ("read_mb", "read_mbs", 1.0),
                                   ("write_mb", "write_mbs", 1.0)):
            x, y = _num(a.get(k_in)), _num(b.get(k_in))
            if x is None or y is None or y < x:
                d = None
                break
            d[k_out] = (y - x) / dt * scale
        if d is not None:
            out[pid] = d
    return out


def tree_metrics(table: dict, root: int, rusage_fn=rusage, kids: dict | None = None,
                 exclude=(), rates: dict | None = None, labels: dict | None = None,
                 now: float | None = None) -> dict:
    """Root + descendants aggregate, plus the top descendant processes.

    Memory is the physical footprint (``footprint_mb``; ``rss_mb`` is kept for tooltips). With
    ``rates`` (from two samples) ``cpu_pct`` and ``read_mbs``/``write_mbs`` are the current rates
    over the sampled pids; without, ``cpu_pct`` is the kernel's decayed ps %cpu. ``exclude`` pids
    (the widget itself) are left out."""
    if root not in table:
        return {"pid": root, "alive": False}
    excl = set(exclude)
    desc = [p for p in descendants(table, root, kids) if p not in excl]
    pids = [root] + desc
    ru = {p: _safe_rusage(rusage_fn, p) for p in pids}
    agg = {"pid": root, "alive": True, "procs": len(pids),
           "rss_mb": round(sum(table[p]["rss_kb"] for p in pids) / 1024, 1),
           "cpu_pct_ps": round(sum(table[p]["cpu"] for p in pids), 1)}
    st = table[root].get("start")
    if isinstance(st, (int, float)):
        agg["uptime_s"] = round(max(0.0, (time.time() if now is None else now) - st))
    sums = {"footprint_mb": 0.0, "read_mb": 0.0, "write_mb": 0.0, "cpu_s": 0.0}
    got = [r for r in ru.values() if r]
    for r in got:
        for k in sums:
            if isinstance(r.get(k), (int, float)):
                sums[k] += r[k]
    for k, v in sums.items():
        agg[k] = round(v, 1) if got else None
    rates = rates or {}
    sampled = [rates[p] for p in pids if p in rates]
    if sampled:
        agg["cpu_pct"] = round(sum(r["cpu_pct"] for r in sampled), 1)
        agg["read_mbs"] = round(sum(r["read_mbs"] for r in sampled), 2)
        agg["write_mbs"] = round(sum(r["write_mbs"] for r in sampled), 2)
        agg["cpu_src"] = "sampled"
    else:
        agg["cpu_pct"] = agg["cpu_pct_ps"]
        agg["read_mbs"] = agg["write_mbs"] = None
        agg["cpu_src"] = "ps"
    labels = labels or {}

    def cpu_of(p):
        return rates[p]["cpu_pct"] if p in rates else table[p]["cpu"]
    top = sorted(desc, key=lambda p: (cpu_of(p), table[p]["rss_kb"]), reverse=True)
    agg["top"] = []
    for p in top[:GRAPH_PROCS_MAX]:
        r = ru.get(p) or {}
        fp = r.get("footprint_mb")
        st = table[p].get("start")
        agg["top"].append({
            "pid": p, "name": labels.get(p) or table[p]["name"],
            "footprint_mb": round(fp, 1) if isinstance(fp, (int, float)) else None,
            "rss_mb": round(table[p]["rss_kb"] / 1024, 1), "cpu_pct": round(cpu_of(p), 1),
            "cpu_s": round(r["cpu_s"], 1) if isinstance(r.get("cpu_s"), (int, float)) else None,
            "uptime_s": round(max(0.0, (time.time() if now is None else now) - st))
            if isinstance(st, (int, float)) else None})
    return agg


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


# ---- GPU / ollama ---------------------------------------------------------------------------

def parse_ioreg(text: str) -> dict:
    util = [int(m) for m in _IOREG_UTIL_RE.findall(text or "")]
    mem = [int(m) for m in _IOREG_MEM_RE.findall(text or "")]
    return {"gpu_util_pct": max(util) if util else None,
            "gpu_mem_mb": _mb(sum(mem)) if mem else None}


_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def _iso_epoch(v) -> float | None:
    """ollama's ``expires_at`` (RFC 3339, nanosecond fraction, offset or Z) -> epoch seconds."""
    if not isinstance(v, str) or not v:
        return None
    s = _FRAC_RE.sub(r"\1", v.strip()).replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        return None
    return d.timestamp()


def parse_ollama(body, now: float | None = None) -> list:
    doc = json.loads(body)
    now = time.time() if now is None else now
    out = []
    for m in (doc.get("models") or []) if isinstance(doc, dict) else []:
        if not isinstance(m, dict) or not isinstance(m.get("name"), str):
            continue
        row = {"name": m["name"], "size_mb": _mb(m.get("size")), "vram_mb": _mb(m.get("size_vram"))}
        exp = _iso_epoch(m.get("expires_at"))
        if exp is not None:
            row["unloads_in_s"] = round(max(0.0, exp - now))
        out.append(row)
    return out


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
           "errors_5m", "req_5m")


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


def subagents(home: Path, session_id: str, now: float, tel: dict | None = None,
              keep: int = SUBAGENTS_MAX, scan: bool = True,
              deadline: Deadline | None = None) -> dict:
    """Subagents seen in the last 60 min (log mtime) or with proxy traffic in the window.

    ``scan=False`` skips the filesystem (traffic only). With a ``deadline`` the log scan stops
    when it runs out (``partial`` is then True); traffic-only subagents are always included.

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
    for d in dirs:
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
            if age >= TELEMETRY_WINDOW_S and aid not in tel:
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
        if partial:
            break
    for aid, st in tel.items():                         # proxy traffic with no log file seen
        if aid not in found:
            state, flags = _lifecycle(None, None, st)
            found[aid] = {"id": aid, "type": "?", "description": "", "depth": None,
                          "age_s": None, "run_s": None, "state": state, "flags": flags,
                          "telemetry": st, "last_s": _last_s(None, st, now)}
    ordered = sorted(found.values(), key=_sub_sort_key)
    shown, rest = ordered[:keep], ordered[keep:]
    return {"list": shown, "more": len(rest),
            "hidden": merge_stats(*[s.get("telemetry") for s in rest]),
            "totals": merge_stats(*[s.get("telemetry") for s in ordered]),
            "count": len(ordered),
            "running": sum(1 for s in ordered if s["state"] == "running"),
            "quiet": sum(1 for s in ordered if s["state"] == "quiet"),
            "flagged": sum(1 for s in ordered if s["flags"]),
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
    """A stuck or erroring agent: any flagged subagent, main-thread errors in 60 min, or a known
    context window ≥ CTX_WARN_PCT full."""
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


# ---- graph ----------------------------------------------------------------------------------

GRAPH_REPO_MAX = 32
GRAPH_WIDTH = 120


def _session_label(a: dict) -> str:
    bits = [str(a.get("kind", "?"))]
    if a.get("repo"):
        repo = clean_text(a["repo"])
        bits.append(repo if len(repo) <= GRAPH_REPO_MAX else repo[:GRAPH_REPO_MAX - 1] + "…")
    bits.append(str(a.get("session", "")))             # the 8-char id is never cut
    return " · ".join(bits)


def display_state(a: dict) -> str:
    """Claude's own busy/idle when known, else the log-mtime state."""
    res = a.get("res") if isinstance(a.get("res"), dict) else {}
    return str(res.get("status") or a.get("state") or "?")


def _age_since(ts, now):
    t = _num(ts)
    if t is None or not math.isfinite(t):
        return None
    return round(max(0.0, (time.time() if now is None else now) - t), 1)


def build_graph(agents: list, now: float | None = None) -> dict:
    """{nodes, edges} from enriched agents (each may carry ``res``). Bounded by the caps; the
    subagents past the cap are one ``more`` node carrying their summed traffic and model calls."""
    nodes, edges, models = [], [], {}

    def model_edge(src, counts):
        for name, n in list((counts or {}).items())[:GRAPH_MODELS_MAX]:
            mid = f"m:{name}"
            if mid not in models:
                models[mid] = {"id": mid, "kind": "model", "label": name, "requests": 0}
            models[mid]["requests"] += n
            edges.append({"from": src, "to": mid, "kind": "calls", "requests": n})

    for a in [a for a in agents if isinstance(a, dict) and not a.get("error")][:GRAPH_SESSIONS_MAX]:
        res = a.get("res") or {}
        sid = f"s:{a.get('kind')}:{a.get('session_id') or a.get('session')}"
        tree = res.get("tree") or {}
        main = res.get("telemetry") or {}
        tot = session_totals(res)
        nodes.append({"id": sid, "kind": "session", "label": _session_label(a),
                      "state": display_state(a), "status": res.get("status"),
                      "pid": tree.get("pid"), "rss_mb": tree.get("rss_mb"),
                      "footprint_mb": tree.get("footprint_mb"), "cpu_pct": tree.get("cpu_pct"),
                      "read_mbs": tree.get("read_mbs"), "write_mbs": tree.get("write_mbs"),
                      "read_mb": tree.get("read_mb"), "write_mb": tree.get("write_mb"),
                      "requests": main.get("requests"), "requests_total": tot["requests"],
                      "tokens_out_total": tot["tokens_out"], "cache_share": cache_share(tot),
                      "ctx_tokens": main.get("ctx_tokens"), "ctx_pct": main.get("ctx_pct"),
                      "ctx_window": main.get("ctx_window"),
                      "req_5m": tot.get("req_5m") if main or tot["requests"] else None,
                      "last_s": _age_since(tot.get("last_ts"), now),
                      "flagged": agent_flagged(a)})
        subs = res.get("subagents") or {}
        lst = [s for s in subs.get("list") or [] if isinstance(s, dict)]
        cut = lst[SUBAGENTS_MAX:]                      # an over-long list: sum, never drop
        for s in lst[:SUBAGENTS_MAX]:
            aid = f"a:{s['id']}"
            st = s.get("telemetry") or {}
            nodes.append({"id": aid, "kind": "subagent", "label": f"{s['type']} · {s['description']}",
                          "state": s.get("state"), "depth": s.get("depth"),
                          "run_s": s.get("run_s"), "age_s": s.get("age_s"),
                          "flags": s.get("flags") or [],
                          "requests": st.get("requests", 0), "tokens_out": st.get("tokens_out", 0),
                          "errors": st.get("errors", 0), "tokens_in": st.get("tokens_in", 0),
                          "cache_read": st.get("cache_read", 0),
                          "cache_write": st.get("cache_write", 0),
                          "ctx_tokens": st.get("ctx_tokens"), "ctx_pct": st.get("ctx_pct"),
                          "ctx_window": st.get("ctx_window"), "errors_5m": st.get("errors_5m", 0)})
            edges.append({"from": sid, "to": aid, "kind": "spawned"})
            model_edge(aid, st.get("models"))
        more = (subs.get("more") or 0) + len(cut)
        if more:
            hid = merge_stats(subs.get("hidden"), *[c.get("telemetry") for c in cut])
            mid = f"{sid}:more"
            nodes.append({"id": mid, "kind": "more", "label": f"{more} more subagents",
                          "count": more, "requests": hid.get("requests", 0),
                          "tokens_out": hid.get("tokens_out", 0), "errors": hid.get("errors", 0)})
            edges.append({"from": sid, "to": mid, "kind": "spawned"})
            model_edge(mid, hid.get("models"))
        for p in tree.get("top", [])[:GRAPH_PROCS_MAX]:
            pid = f"p:{p['pid']}"
            nodes.append({"id": pid, "kind": "process", "label": p["name"], "pid": p["pid"],
                          "footprint_mb": p.get("footprint_mb"), "rss_mb": p.get("rss_mb"),
                          "cpu_pct": p.get("cpu_pct")})
            edges.append({"from": sid, "to": pid, "kind": "runs"})
        model_edge(sid, main.get("models"))
    return {"nodes": nodes + list(models.values()), "edges": edges}


def io_rate(tree: dict):
    r, w = tree.get("read_mbs"), tree.get("write_mbs")
    if not isinstance(r, (int, float)) and not isinstance(w, (int, float)):
        return None
    return (r or 0.0) + (w or 0.0)


def fmt_rate(mbs) -> str:
    if not isinstance(mbs, (int, float)):
        return "?MB/s"
    return f"{mbs:.0f}MB/s" if mbs >= 10 else f"{mbs:.1f}MB/s"


def metrics_text(tree: dict) -> str:
    """``412MB · 14% · 0.4MB/s`` (footprint · cpu now · disk read+write now) — empty when the
    tree is unknown; the io rate only when two samples were taken."""
    if not tree or not tree.get("alive"):
        return ""
    bits = [fmt_mem(tree.get("footprint_mb")), f"{tree.get('cpu_pct') or 0:g}%"]
    io = io_rate(tree)
    if io is not None:
        bits.append(fmt_rate(io))
    return " · ".join(bits)


def fmt_pct(x) -> str:
    """A 0..1 share as a whole percent; a nonzero share below 1% is ``<1%``, never ``0%``."""
    if not isinstance(x, (int, float)) or not math.isfinite(x):
        return "?"
    return "<1%" if 0 < x < 0.01 else f"{100 * x:.0f}%"


def err_text(st: dict) -> str:
    n, req = st.get("errors") or 0, st.get("requests") or 0
    return f"{n} err {fmt_pct(n / req) if req else '?'}" if n else ""


def _kc(n) -> str:
    """Compact token count for context sizes: ``283k``, ``1M``, ``1.2M``, ``950``."""
    if not isinstance(n, (int, float)) or not math.isfinite(n):
        return "?"
    if n >= 999_500:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{n / 1000:.0f}k" if n >= 1000 else str(int(n))


def fmt_rate_min(n5) -> str:
    """``r5 3.2/min`` from a 5-min request count."""
    if not isinstance(n5, (int, float)):
        return ""
    x = n5 / (RATE_WINDOW_S / 60)
    v = f"{x:.0f}" if x >= 10 else f"{x:.1f}".rstrip("0").rstrip(".")
    return f"r5 {v or '0'}/min"


def tokens_text(st: dict | None) -> str:
    """``in 12k · cached 3.7M · write 40k · out 54k`` (input parts only when non-zero)."""
    st = st or {}
    bits = []
    if st.get("tokens_in") or st.get("cache_read") or st.get("cache_write"):
        bits += [f"in {_k(st.get('tokens_in') or 0)}", f"cached {_k(st.get('cache_read') or 0)}"]
    if st.get("cache_write"):
        bits.append(f"write {_k(st['cache_write'])}")
    bits.append(f"out {_k(st.get('tokens_out', 0))}")
    return " · ".join(bits)


def ctx_text(st: dict | None) -> str:
    """``ctx 283k/1M 28%`` with a known window, ``ctx 136k`` without (never a guessed window);
    '' when no request carried a prompt."""
    st = st or {}
    n = st.get("ctx_tokens")
    if not isinstance(n, (int, float)):
        return ""
    win, pct = st.get("ctx_window"), st.get("ctx_pct")
    if isinstance(win, (int, float)) and win > 0:
        pct = pct if isinstance(pct, (int, float)) else round(100 * n / win)
        return f"ctx {_kc(n)}/{_kc(win)} {pct:g}%"
    return f"ctx {_kc(n)}" + (f" {pct:g}%" if isinstance(pct, (int, float)) else "")


def tel_text(st: dict | None) -> str:
    """``12 req · in 2 · cached 115k · out 130 · cache 99%`` (+ ``5 err 42%`` only with errors,
    + ``ctx 245k`` when a context size is known)."""
    st = st or {}
    bits = [f"{st.get('requests', 0)} req", tokens_text(st)]
    share = cache_share(st)
    if share is not None:
        bits.append(f"cache {fmt_pct(share)}")
    e = err_text(st)
    if e:
        bits.append(e)
    c = ctx_text(st)
    if c:
        bits.append(c)
    return " · ".join(bits)


def lifecycle_text(s: dict) -> str:
    """``run 14m · last 2s`` / ``run 12m · quiet 3m`` / ``run 40m · done 20m``."""
    bits = []
    if isinstance(s.get("run_s"), (int, float)):
        bits.append(f"run {fmt_dur(s['run_s'])}")
    age, state = s.get("age_s"), s.get("state")
    if isinstance(age, (int, float)):
        word = {"running": "last", "quiet": "quiet", "done": "done"}.get(state, "last")
        if "errors" in (s.get("flags") or []) and state != "running":
            word = "last"
        bits.append(f"{word} {fmt_dur(age)}")
    return " · ".join(bits) if bits else "no log"


def graph_text(graph: dict) -> str:
    """Indented text tree: session -> spawned/runs/calls, subagent -> calls."""
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    out_edges: dict = {}
    for e in graph.get("edges", []):
        out_edges.setdefault(e["from"], []).append(e)
    lines = []

    def node_bits(n, e=None) -> list:
        """The node's text as ' · '-joined parts (wrapped at GRAPH_WIDTH by emit)."""
        k = n["kind"]
        if k == "session":
            m = metrics_text({"alive": n.get("pid") is not None, **n})
            bits = [("⚠ " if n.get("flagged") else "") + clean_text(n["label"]),
                    clean_text(n.get("state") or "?")]
            if n.get("pid"):
                bits.append(f"pid {n['pid']}")
            if m:
                bits.append(m)
            if n.get("requests_total"):
                bits += [f"{n['requests_total']} req/h", f"out {_k(n.get('tokens_out_total'))}/h"]
                if n.get("cache_share") is not None:
                    bits.append(f"cache {fmt_pct(n['cache_share'])}")
            c = ctx_text(n)
            if c:
                bits.append(c)
            if n.get("req_5m"):
                bits.append(fmt_rate_min(n["req_5m"]))
            if isinstance(n.get("last_s"), (int, float)) and n.get("requests_total"):
                bits.append(f"last {fmt_dur(n['last_s'])}")
            return bits
        if k == "subagent":
            flag = "⚠ " if n.get("flags") else ""
            life = lifecycle_text(n)
            st = n.get("state")                        # quiet/done already name themselves
            label = clean_text(n["label"])[:60]
            bits = [f"spawned {flag}{label}", tel_text(n)]
            if f"{st} " not in life:
                bits.append(str(st))
            bits.append(life)
            if n.get("depth"):
                bits.append(f"depth {n['depth']}")
            return bits
        if k == "more":
            return [f"… {n.get('count', 0)} more subagents ({n.get('requests', 0)} req, "
                    f"out {_k(n.get('tokens_out', 0))})"]
        if k == "process":
            return [f"runs {clean_text(n['label'])} (pid {n['pid']})",
                    fmt_mem(n.get("footprint_mb")), f"{n.get('cpu_pct') or 0:g}%"]
        return [f"calls {clean_text(n['label'])} "
                f"×{e.get('requests', 0) if e else n.get('requests', 0)}"]

    def emit(depth, bits):
        """Pack the parts into lines of at most GRAPH_WIDTH; continuation lines indent 4 more.
        A single part longer than a line is cut with '…'."""
        indent, cont = "  " * depth, "  " * depth + "    "
        cur = indent
        for b in (x for x in " · ".join(bits).split(" · ") if x != ""):
            sep = "" if cur in (indent, cont) else " · "
            if len(cur) + len(sep) + len(b) <= GRAPH_WIDTH:
                cur += sep + b
                continue
            if cur not in (indent, cont):
                lines.append(cur)
            cur = cont
            room = GRAPH_WIDTH - len(cur)
            cur += b if len(b) <= room else b[:room - 1] + "…"
        lines.append(cur)

    def walk(nid, depth):
        for e in out_edges.get(nid, []):
            child = by_id.get(e["to"])
            if child is None:
                continue
            emit(depth, node_bits(child, e))
            if child["kind"] in ("subagent", "more"):
                walk(child["id"], depth + 1)

    for n in graph.get("nodes", []):
        if n["kind"] == "session":
            emit(0, node_bits(n))
            walk(n["id"], 1)
    return "\n".join(lines) if lines else "no agents in the last hour"


# ---- collect --------------------------------------------------------------------------------

def collect(agents: list, *, home=None, telemetry=None, now: float | None = None,
            run=None, rusage_fn=None, fetch=None, loadavg=None, worker_pid=None,
            deadline: Deadline | None = None, self_pid: int | None = None,
            sample_s: float = RATE_SAMPLE_S) -> dict:
    """Enrich ``agents`` (a new list; each matched agent gains ``res``) and return
    ``{"agents", "system", "graph", "worker"}``. The injectables default to the real sources,
    looked up at call time (tests patch the module attributes or pass fakes). Never raises.

    Order: ps -> (lsof) -> first rusage sample of every tree pid -> telemetry, subagent logs of
    the sessions the menu will show, ioreg, ollama, script names (these fill the gap) -> sleep
    the rest of ``sample_s`` (never past the deadline) -> second sample of the ≤ RATE_PIDS_MAX
    busiest pids -> rates -> trees. Subagent logs are read only for the ≤ SCAN_SESSIONS_MAX
    active Claude sessions ranked first by ``rank_key`` (re-checked once the cpu rates are in);
    every other session's subagents come from proxy traffic alone."""
    now = time.time() if now is None else now
    run = run or run_cmd
    rusage_fn = rusage_fn or rusage
    loadavg = loadavg or os.getloadavg
    dl = deadline or Deadline()
    if fetch is None:
        def fetch(url):
            return http_get(url, timeout=dl.timeout(0.5))
    home = Path(home).expanduser() if home else Path.home()
    errors: dict = {}

    def guard(name, fn, default):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — each source fails open on its own
            errors[name] = _err(e)
            return default

    agents = [dict(a) if isinstance(a, dict) else a for a in agents]
    table = guard("ps", lambda: ps_table(run, dl.timeout(1.0)), {})
    kids = children_map(table)
    sessions = guard("claude_sessions", lambda: claude_sessions(home, table), {})
    need_lsof = {p for p, r in table.items() if r["name"] in ("pi", "codex")} if any(
        isinstance(a, dict) and a.get("kind") in ("pi", "codex") for a in agents) else set()
    cwds = guard("lsof", lambda: lsof_cwds(need_lsof, run, dl.timeout(0.5)), {}) if need_lsof else {}
    by_cwd = match_by_cwd([a if isinstance(a, dict) else {} for a in agents], table, cwds)

    # roots: agent index -> pid
    roots: dict = {}
    for i, a in enumerate(agents):
        if not isinstance(a, dict) or a.get("error"):
            continue
        sid = a.get("session_id")
        if a.get("kind") == "claude" and sid in sessions:
            roots[i] = sessions[sid]["pid"]
        elif isinstance(by_cwd.get(i), int):
            roots[i] = by_cwd[i]
    ol = {p["pid"]: p for p in table.values() if p["name"] == "ollama"}
    ol_roots = [p for p in ol.values() if p["ppid"] not in ol]   # the server, not its runners
    ol_pid = min(ol_roots, key=lambda p: p["pid"])["pid"] if ol_roots else None
    extra_roots = [p for p in (worker_pid, ol_pid) if isinstance(p, int)]
    all_roots = set(roots.values()) | set(extra_roots)
    excl = self_exclusion(table, all_roots, self_pid, kids) if table else set()

    tree_pids: dict = {}
    for r in all_roots:
        if r in table:
            tree_pids[r] = [r] + [p for p in descendants(table, r, kids) if p not in excl]
    every = {p for ps in tree_pids.values() for p in ps}
    s1 = {p: _safe_rusage(rusage_fn, p) for p in every}
    t1 = _clock()

    # gap work
    path = Path(telemetry) if telemetry else pressure.default_telemetry_path()
    rows = guard("telemetry", lambda: list(pressure.tail_rows(path, now - TELEMETRY_WINDOW_S)), [])
    tel = guard("telemetry_split", lambda: telemetry_split(rows, now), {})
    traffic = guard("telemetry_split", lambda: _stats(rows, now), None)
    # subagents: traffic-only for every Claude session; logs for the ones the menu will show
    empty_subs = {"list": [], "more": 0, "count": 0, "flagged": 0}
    subs_by: dict = {}
    for i, a in enumerate(agents):
        if isinstance(a, dict) and not a.get("error") and a.get("kind") == "claude" \
                and isinstance(a.get("session_id"), str):
            t = tel.get(a["session_id"]) or {}
            subs_by[i] = guard("subagents", lambda: subagents(
                home, a["session_id"], now, t.get("subagents"), scan=False), empty_subs)

    def provisional(i, cpu_of):
        a = agents[i]
        sid = a["session_id"]
        st = sessions.get(sid, {}).get("status") if sid in sessions else None
        pid = roots.get(i)
        res = {"status": st, "subagents": subs_by.get(i),
               "telemetry": (tel.get(sid) or {}).get("main"),
               "tree": {"cpu_pct": cpu_of(pid) if pid is not None else 0}}
        return dict(a, res=res)

    scanned: set = set()

    def scan_shown(cpu_of):
        cands = [provisional(i, cpu_of) for i in subs_by]
        idx = list(subs_by)
        order = sorted(range(len(cands)), key=lambda k: rank_key(cands[k]))
        shown = [idx[k] for k in order if agent_active(cands[k])][:SCAN_SESSIONS_MAX]
        for i in shown:
            if i in scanned:
                continue
            scanned.add(i)
            sid = agents[i]["session_id"]
            t = tel.get(sid) or {}
            r = guard("subagents", lambda: subagents(home, sid, now, t.get("subagents"),
                                                     deadline=dl), None)
            if isinstance(r, dict):
                subs_by[i] = r
                if r.get("partial"):
                    errors["subagents"] = "partial: deadline"

    def ps_tree_cpu(pid):
        return round(sum(table[p]["cpu"] for p in tree_pids.get(pid, []) if p in table), 1)
    scan_shown(ps_tree_cpu)

    system: dict = {"gpu_scope": "system-wide"}
    system.update(guard("ioreg", lambda: parse_ioreg(
        run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"], timeout=dl.timeout(0.5))), {}))
    system["ollama"] = guard("ollama", lambda: parse_ollama(fetch(OLLAMA_URL), now), None)
    agent_pids = {p for r in roots.values() for p in tree_pids.get(r, [])}
    labels = guard("ps_args", lambda: script_labels(
        table, sorted(agent_pids, key=lambda p: -table[p]["cpu"]), run, dl.timeout(0.5)),
        {}) if agent_pids else {}

    # second sample of the busiest pids (roots first), inside the shared deadline
    have = [p for p in every if s1.get(p)]
    rates: dict = {}
    if have and sample_s > 0 and dl.remaining() > DEADLINE_MIN_S:
        pri = sorted(have, key=lambda p: (p not in all_roots, -table[p]["cpu"],
                                          -table[p]["rss_kb"]))[:RATE_PIDS_MAX]
        wait = min(sample_s - (_clock() - t1), dl.remaining() - DEADLINE_MIN_S)
        if wait > 0:
            _sleep(wait)
        s2 = {p: _safe_rusage(rusage_fn, p) for p in pri}
        dt = _clock() - t1
        rates = rates_from_samples({p: s1[p] for p in pri}, s2, dt)
        system["rate_window_s"] = round(dt, 3)
        system["rate_pids"] = len(pri)

    def tree_of(pid):
        return tree_metrics(table, pid, lambda p: s1.get(p), kids, excl, rates, labels, now)

    def sampled_tree_cpu(pid):
        ps = tree_pids.get(pid, [])
        if not any(p in rates for p in ps):
            return ps_tree_cpu(pid)
        return round(sum(rates[p]["cpu_pct"] for p in ps if p in rates), 1)
    if rates:
        scan_shown(sampled_tree_cpu)                    # the menu ranks by the sampled cpu

    for i, a in enumerate(agents):
        if not isinstance(a, dict) or a.get("error"):
            continue
        res: dict = {}
        sid = a.get("session_id")
        if a.get("kind") == "claude" and sid in sessions:
            s = sessions[sid]
            res.update(status=s["status"], name=s["name"], pid_source=s["verified"])
            res["tree"] = guard("rusage", lambda: tree_of(s["pid"]), {})
        elif isinstance(by_cwd.get(i), int):
            res["pid_source"] = "lsof-cwd"
            res["tree"] = guard("rusage", lambda: tree_of(by_cwd[i]), {})
        elif isinstance(by_cwd.get(i), dict):
            res["unattributed"] = by_cwd[i]
        t = tel.get(sid) if isinstance(sid, str) else None
        if t:
            res["telemetry"] = t["main"]
        if i in subs_by:
            res["subagents"] = subs_by[i]
        if res:
            a["res"] = res

    la = guard("loadavg", loadavg, None)
    system["loadavg"] = [round(x, 2) for x in la] if la else None
    system["agents_rss_mb"] = round(sum(table[p]["rss_kb"] for p in agent_pids if p in table)
                                    / 1024, 1)
    fps = [s1[p]["footprint_mb"] for p in agent_pids if s1.get(p)
           and isinstance(s1[p].get("footprint_mb"), (int, float))]
    system["agents_footprint_mb"] = round(sum(fps), 1) if fps else None
    system["agents_cpu_pct"] = round(sum(rates[p]["cpu_pct"] for p in agent_pids if p in rates),
                                     1) if rates else None
    system["agents_procs"] = len(agent_pids)
    system["excluded_self_pids"] = len(excl)
    if traffic:
        system["traffic_60m"] = traffic
    worker = {}
    if isinstance(worker_pid, int):
        worker["tree"] = guard("rusage", lambda: tree_of(worker_pid), {})
    if ol_pid is not None:
        worker["ollama_tree"] = guard("rusage", lambda: tree_of(ol_pid), {})
    worker["ollama_models"] = system["ollama"]
    if errors:
        system["errors"] = errors
    system["subagent_scans"] = len(scanned)
    return {"agents": agents, "system": system, "graph": build_graph(agents, now),
            "worker": worker}
