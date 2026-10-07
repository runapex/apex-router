"""Per-agent resources and the call graph for the menu-bar widget — read-only, fail-open.

Ties each agent from ``agents.discover()`` to the OS processes it runs and the model calls it
makes. What is read, and nothing more:

  process table  ONE ``ps -axo pid=,ppid=,rss=,%cpu=,lstart=,comm=`` (2 s timeout). ``%cpu`` is
                 the kernel's decayed average — good enough for a stateless 60 s widget.
  libproc        ``proc_pid_rusage(pid, RUSAGE_INFO_V2)`` via ctypes (macOS, unprivileged for the
                 user's own processes): physical footprint, disk bytes read/written, cpu time.
                 ``user``/``system`` are mach absolute-time ticks, converted with
                 ``mach_timebase_info`` (125/3 on Apple Silicon), not nanoseconds.
  Claude Code    ``~/.claude/sessions/<pid>.json`` (``*.json`` only) — exact pid -> sessionId. A
                 file whose pid is dead, or whose ``procStart`` does not match the live process's
                 start time, is ignored. Subagent sidecars ``agent-<id>.meta.json`` (agentType,
                 description, spawnDepth) and the mtime of ``agent-<id>.jsonl``.
  pi / Codex     ONE ``lsof -a -d cwd -Fpn -p <pids>`` (2 s timeout) maps the process to its cwd;
                 a process is credited to a session only when it is the sole process of its kind
                 in that cwd and the cwd has one listed session of that kind.
  GPU            ``ioreg -r -d 1 -c IOAccelerator`` — SYSTEM-WIDE utilisation and in-use memory.
                 Per-process GPU needs root and is not attempted.
  ollama         ``GET http://127.0.0.1:11434/api/ps`` (1 s timeout) — loaded models + VRAM.
  telemetry      the last 60 min of ``~/.apex/telemetry.jsonl`` via ``pressure.tail_rows``:
                 requests / tokens / errors / ttft split by session (main thread, agent_id null)
                 and by subagent (agent_id). Only traffic that went through the proxy is seen.

Subagents run inside their Claude session's process, so OS resources (memory, cpu, io) are per
session, never per subagent; a subagent's "load" is its proxy traffic. No prompt or transcript
content is read. Every source fails open: an error lands in ``system.errors[<source>]``.
"""
from __future__ import annotations

import calendar
import ctypes
import ctypes.util
import json
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
SUBAGENT_ACTIVE_S = 5 * 60
SUBAGENTS_MAX = 20
PROCS_TOP = 3
GRAPH_PROCS_MAX = 5
GRAPH_MODELS_MAX = 8
GRAPH_SESSIONS_MAX = 30
TREE_PIDS_MAX = 300
META_BYTES_MAX = 64 * 1024
SESSION_BYTES_MAX = 64 * 1024
START_TOLERANCE_S = 2.0
OLLAMA_URL = "http://127.0.0.1:11434/api/ps"
_MB = 1024 * 1024
_LSTART_FMT = "%a %b %d %H:%M:%S %Y"
_IOREG_UTIL_RE = re.compile(r'"Device Utilization %"\s*=\s*(\d+)')
_IOREG_MEM_RE = re.compile(r'"In use system memory"\s*=\s*(\d+)')
_AGENT_FILE_RE = re.compile(r"^agent-([A-Za-z0-9_-]+)\.jsonl$")


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def _mb(n_bytes):
    return round(n_bytes / _MB, 1) if isinstance(n_bytes, (int, float)) else None


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


def ps_table(run=run_cmd) -> dict:
    return parse_ps(run(["ps", "-axo", "pid=,ppid=,rss=,%cpu=,lstart=,comm="]))


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
    """{footprint_mb, read_mb, write_mb, cpu_s} for one pid, or None (not macOS, gone, denied)."""
    try:
        lib = _libs()
        ru = _RusageV2()
        if lib["proc"].proc_pid_rusage(int(pid), 2, ctypes.byref(ru)) != 0:
            return None
    except Exception:  # noqa: BLE001 — fail open: no rusage is a missing metric, not an error
        return None
    return {"footprint_mb": _mb(ru.phys_footprint), "read_mb": _mb(ru.diskio_bytesread),
            "write_mb": _mb(ru.diskio_byteswritten),
            "cpu_s": round(mach_ticks_to_s(ru.user + ru.system, lib["numer"], lib["denom"]), 1)}


def tree_metrics(table: dict, root: int, rusage_fn=rusage, kids: dict | None = None) -> dict:
    """Root + descendants aggregate, plus the top descendant processes by cpu then memory."""
    if root not in table:
        return {"pid": root, "alive": False}
    desc = descendants(table, root, kids)
    pids = [root] + desc
    agg = {"pid": root, "alive": True, "procs": len(pids),
           "rss_mb": round(sum(table[p]["rss_kb"] for p in pids) / 1024, 1),
           "cpu_pct": round(sum(table[p]["cpu"] for p in pids), 1)}
    sums = {"footprint_mb": 0.0, "read_mb": 0.0, "write_mb": 0.0, "cpu_s": 0.0}
    got = 0
    for p in pids:
        r = None
        try:
            r = rusage_fn(p)
        except Exception:  # noqa: BLE001
            r = None
        if not r:
            continue
        got += 1
        for k in sums:
            if isinstance(r.get(k), (int, float)):
                sums[k] += r[k]
    for k, v in sums.items():
        agg[k] = round(v, 1) if got else None
    top = sorted(desc, key=lambda p: (table[p]["cpu"], table[p]["rss_kb"]), reverse=True)
    agg["top"] = [{"pid": p, "name": table[p]["name"], "rss_mb": round(table[p]["rss_kb"] / 1024, 1),
                   "cpu_pct": table[p]["cpu"]} for p in top[:GRAPH_PROCS_MAX]]
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
    """sessionId -> {pid, status, name, cwd, verified} for LIVE Claude processes only."""
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
        match = _start_matches(doc.get("procStart"), table[pid].get("start"))
        if match is False:
            continue                                   # pid reused by another process
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


def lsof_cwds(pids, run=run_cmd) -> dict:
    pids = sorted({int(p) for p in pids})
    if not pids:
        return {}
    return parse_lsof_cwd(run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p",
                               ",".join(str(p) for p in pids)]))


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


def parse_ollama(body) -> list:
    doc = json.loads(body)
    out = []
    for m in (doc.get("models") or []) if isinstance(doc, dict) else []:
        if not isinstance(m, dict) or not isinstance(m.get("name"), str):
            continue
        out.append({"name": m["name"], "size_mb": _mb(m.get("size")),
                    "vram_mb": _mb(m.get("size_vram"))})
    return out


# ---- telemetry ------------------------------------------------------------------------------

def _stats(rows: list) -> dict:
    ttft = [r["ttft_ms"] for r in rows if isinstance(r.get("ttft_ms"), (int, float))]

    def tot(k):
        return int(sum(r[k] for r in rows if isinstance(r.get(k), (int, float))))
    return {"requests": len(rows), "tokens_out": tot("tokens_out"),
            "cache_read": tot("cache_read_tokens"),
            "errors": sum(1 for r in rows if r.get("is_error")),
            "p50_ttft_ms": round(statistics.median(ttft)) if ttft else None,
            "models": dict(Counter(str(r.get("model_requested") or "?") for r in rows)
                           .most_common(GRAPH_MODELS_MAX))}


def telemetry_split(rows) -> dict:
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
        out[sid] = {"main": _stats(by.get(None, [])),
                    "subagents": {a: _stats(rs) for a, rs in by.items() if a is not None}}
    return out


# ---- subagents ------------------------------------------------------------------------------

def subagents(home: Path, session_id: str, now: float, tel: dict | None = None) -> dict:
    """{"list": [...], "more": n}: subagents seen in the last 60 min (log mtime) or with proxy
    traffic in the window, newest first, capped at SUBAGENTS_MAX."""
    tel = tel or {}
    found: dict = {}
    root = Path(home) / ".claude" / "projects"
    try:
        dirs = list(root.glob(f"*/{session_id}/subagents"))
    except OSError:
        dirs = []
    for d in dirs:
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for f in entries:
            m = _AGENT_FILE_RE.match(f.name)
            if not m:
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            aid = m.group(1)
            age = max(0.0, now - mtime)
            if age >= TELEMETRY_WINDOW_S and aid not in tel:
                continue
            meta = {}
            mp = f.with_name(f"agent-{aid}.meta.json")
            try:
                if mp.stat().st_size <= META_BYTES_MAX:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                meta = {}
            meta = meta if isinstance(meta, dict) else {}
            try:
                depth = int(meta.get("spawnDepth"))
            except (TypeError, ValueError):
                depth = None
            found[aid] = {"id": aid, "type": str(meta.get("agentType") or "?"),
                          "description": str(meta.get("description") or ""),
                          "depth": depth, "age_s": round(age, 1),
                          "state": "active" if age < SUBAGENT_ACTIVE_S else "idle",
                          "telemetry": tel.get(aid)}
    for aid, st in tel.items():                         # proxy traffic with no log file seen
        found.setdefault(aid, {"id": aid, "type": "?", "description": "", "depth": None,
                               "age_s": None, "state": "?", "telemetry": st})
    ordered = sorted(found.values(), key=lambda s: (s["age_s"] is None, s["age_s"] or 0))
    return {"list": ordered[:SUBAGENTS_MAX], "more": max(0, len(ordered) - SUBAGENTS_MAX)}


# ---- graph ----------------------------------------------------------------------------------

def _session_label(a: dict) -> str:
    bits = [str(a.get("kind", "?"))]
    if a.get("repo"):
        bits.append(str(a["repo"]))
    bits.append(str(a.get("session", "")))
    return " · ".join(bits)


def build_graph(agents: list) -> dict:
    """{nodes, edges} from enriched agents (each may carry ``res``). Bounded by the caps."""
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
        nodes.append({"id": sid, "kind": "session", "label": _session_label(a),
                      "state": a.get("state"), "status": res.get("status"),
                      "pid": tree.get("pid"), "rss_mb": tree.get("rss_mb"),
                      "footprint_mb": tree.get("footprint_mb"), "cpu_pct": tree.get("cpu_pct"),
                      "read_mb": tree.get("read_mb"), "write_mb": tree.get("write_mb"),
                      "requests": main.get("requests")})
        for s in (res.get("subagents") or {}).get("list", [])[:SUBAGENTS_MAX]:
            aid = f"a:{s['id']}"
            st = s.get("telemetry") or {}
            nodes.append({"id": aid, "kind": "subagent", "label": f"{s['type']} · {s['description']}",
                          "state": s.get("state"), "depth": s.get("depth"),
                          "requests": st.get("requests", 0), "tokens_out": st.get("tokens_out", 0),
                          "errors": st.get("errors", 0)})
            edges.append({"from": sid, "to": aid, "kind": "spawned"})
            model_edge(aid, st.get("models"))
        for p in tree.get("top", [])[:GRAPH_PROCS_MAX]:
            pid = f"p:{p['pid']}"
            nodes.append({"id": pid, "kind": "process", "label": p["name"], "pid": p["pid"],
                          "rss_mb": p["rss_mb"], "cpu_pct": p["cpu_pct"]})
            edges.append({"from": sid, "to": pid, "kind": "runs"})
        model_edge(sid, main.get("models"))
    return {"nodes": nodes + list(models.values()), "edges": edges}


def _k(n) -> str:
    if not isinstance(n, (int, float)):
        return "?"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def metrics_text(tree: dict) -> str:
    """``412MB · 14% · io 745/569MB`` — empty when the tree is unknown."""
    if not tree or not tree.get("alive"):
        return ""
    mem = tree.get("footprint_mb") or tree.get("rss_mb")
    bits = [f"{mem:.0f}MB" if isinstance(mem, (int, float)) else "?MB",
            f"{tree.get('cpu_pct', 0):g}%"]
    if tree.get("read_mb") is not None:
        bits.append(f"io {tree['read_mb']:.0f}/{tree.get('write_mb') or 0:.0f}MB")
    return " · ".join(bits)


def tel_text(st: dict | None) -> str:
    st = st or {}
    return (f"{st.get('requests', 0)} req · {_k(st.get('tokens_out', 0))} out · "
            f"{st.get('errors', 0)} err")


def graph_text(graph: dict) -> str:
    """Indented text tree: session -> spawned/runs/calls, subagent -> calls."""
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    out_edges: dict = {}
    for e in graph.get("edges", []):
        out_edges.setdefault(e["from"], []).append(e)
    lines = []

    def node_text(n, e=None):
        k = n["kind"]
        if k == "session":
            m = metrics_text({"alive": n.get("pid") is not None, **n})
            bits = [n["label"], n.get("state") or "?"]
            if n.get("status"):
                bits.append(n["status"])
            if n.get("pid"):
                bits.append(f"pid {n['pid']}")
            return " · ".join(bits + ([m] if m else []))
        if k == "subagent":
            d = f" · depth {n['depth']}" if n.get("depth") else ""
            return f"spawned {n['label'][:60]} · {tel_text(n)} · {n.get('state')}{d}"
        if k == "process":
            return f"runs {n['label']} (pid {n['pid']}) · {n['rss_mb']:.0f}MB · {n['cpu_pct']:g}%"
        return f"calls {n['label']} ×{e.get('requests', 0) if e else n.get('requests', 0)}"

    def walk(nid, depth):
        for e in out_edges.get(nid, []):
            child = by_id.get(e["to"])
            if child is None:
                continue
            lines.append("  " * depth + node_text(child, e))
            if child["kind"] == "subagent":
                walk(child["id"], depth + 1)

    for n in graph.get("nodes", []):
        if n["kind"] == "session":
            lines.append(node_text(n))
            walk(n["id"], 1)
    return "\n".join(lines) if lines else "no agents in the last hour"


# ---- collect --------------------------------------------------------------------------------

def collect(agents: list, *, home=None, telemetry=None, now: float | None = None,
            run=None, rusage_fn=None, fetch=None, loadavg=None, worker_pid=None) -> dict:
    """Enrich ``agents`` (a new list; each matched agent gains ``res``) and return
    ``{"agents", "system", "graph", "worker"}``. The injectables default to the real sources,
    looked up at call time (tests patch the module attributes or pass fakes). Never raises."""
    now = time.time() if now is None else now
    run = run or run_cmd
    rusage_fn = rusage_fn or rusage
    fetch = fetch or http_get
    loadavg = loadavg or os.getloadavg
    home = Path(home).expanduser() if home else Path.home()
    errors: dict = {}

    def guard(name, fn, default):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — each source fails open on its own
            errors[name] = _err(e)
            return default

    agents = [dict(a) if isinstance(a, dict) else a for a in agents]
    table = guard("ps", lambda: ps_table(run), {})
    kids = children_map(table)
    sessions = guard("claude_sessions", lambda: claude_sessions(home, table), {})
    path = Path(telemetry) if telemetry else pressure.default_telemetry_path()
    tel = guard("telemetry", lambda: telemetry_split(
        pressure.tail_rows(path, now - TELEMETRY_WINDOW_S)), {})
    need_lsof = {p for p, r in table.items() if r["name"] in ("pi", "codex")} if any(
        isinstance(a, dict) and a.get("kind") in ("pi", "codex") for a in agents) else set()
    cwds = guard("lsof", lambda: lsof_cwds(need_lsof, run), {}) if need_lsof else {}
    by_cwd = match_by_cwd([a if isinstance(a, dict) else {} for a in agents], table, cwds)

    tree_pids: set = set()
    for i, a in enumerate(agents):
        if not isinstance(a, dict) or a.get("error"):
            continue
        res: dict = {}
        sid = a.get("session_id")
        if a.get("kind") == "claude" and sid in sessions:
            s = sessions[sid]
            res.update(status=s["status"], name=s["name"], pid_source=s["verified"])
            res["tree"] = guard("rusage", lambda: tree_metrics(table, s["pid"], rusage_fn, kids), {})
        elif isinstance(by_cwd.get(i), int):
            res["pid_source"] = "lsof-cwd"
            res["tree"] = guard("rusage", lambda: tree_metrics(table, by_cwd[i], rusage_fn, kids), {})
        elif isinstance(by_cwd.get(i), dict):
            res["unattributed"] = by_cwd[i]
        t = tel.get(sid) if isinstance(sid, str) else None
        if t:
            res["telemetry"] = t["main"]
        if a.get("kind") == "claude" and isinstance(sid, str):
            res["subagents"] = guard("subagents", lambda: subagents(
                home, sid, now, (t or {}).get("subagents")), {"list": [], "more": 0})
        tr = res.get("tree") or {}
        if tr.get("alive"):
            tree_pids.add(tr["pid"])
            tree_pids.update(descendants(table, tr["pid"], kids))
        if res:
            a["res"] = res

    system: dict = {"gpu_scope": "system-wide"}
    system.update(guard("ioreg", lambda: parse_ioreg(
        run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"])), {}))
    system["ollama"] = guard("ollama", lambda: parse_ollama(fetch(OLLAMA_URL)), None)
    la = guard("loadavg", loadavg, None)
    system["loadavg"] = [round(x, 2) for x in la] if la else None
    system["agents_rss_mb"] = round(sum(table[p]["rss_kb"] for p in tree_pids if p in table) / 1024,
                                    1)
    system["agents_procs"] = len(tree_pids)
    worker = {}
    if isinstance(worker_pid, int):
        worker["tree"] = guard("rusage", lambda: tree_metrics(table, worker_pid, rusage_fn, kids), {})
    ol = {p["pid"]: p for p in table.values() if p["name"] == "ollama"}
    roots = [p for p in ol.values() if p["ppid"] not in ol]   # the server, not its runners
    if roots:
        server = min(roots, key=lambda p: p["pid"])
        worker["ollama_tree"] = guard("rusage", lambda: tree_metrics(
            table, server["pid"], rusage_fn, kids), {})
    worker["ollama_models"] = system["ollama"]
    if errors:
        system["errors"] = errors
    return {"agents": agents, "system": system, "graph": build_graph(agents), "worker": worker}
