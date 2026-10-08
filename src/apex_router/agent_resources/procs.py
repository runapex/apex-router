"""The process table (``ps``), process trees, interpreter script names and libproc rusage
sampled twice for cpu % and disk MB/s."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import sys
import time

from .base import GRAPH_PROCS_MAX, _LSTART_FMT, _MB, _num, run_cmd


TREE_PIDS_MAX = 300
RATE_PIDS_MAX = 40                # pids sampled twice for cpu % / disk MB/s
ARGS_PIDS_MAX = 40                # interpreter pids whose script name is looked up
RATE_SAMPLE_S = 0.25


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
