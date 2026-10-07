"""Bounded per-run history for the menu-bar widget, plus the sparklines drawn from it.

``snapshot --menubar`` appends ONE compact JSON line per run to
``$APEX_ROUTER_HOME/widget/history.jsonl`` (default ``~/.apex-router/widget/history.jsonl``);
``--json`` / ``--graph`` never write, and ``--menubar --no-history`` (or
``APEX_WIDGET_NO_HISTORY=1``) does not either. A line::

    {"v": 1, "ts": 1791349875.4,
     "agents": [{"session": "<full id>", "kind": "claude", "status": "busy",
                 "footprint_mb": 705.1, "cpu_pct": 0.5, "io_mbs": 0.01, "ctx": 293000,
                 "req_5m": 22, "subagents": [{"id": "a1c…", "state": "running"}]}],
     "system": {"gpu_util_pct": 6, "gpu_mem_mb": 1510.0, "load1": 3.29}}

Only numbers, states and ids the snapshot already holds — no descriptions, names or paths.
The file keeps at most 24 h and at most 2 MB: when either is exceeded it is rewritten (temp file +
``os.replace``) down to the newest lines within 24 h and 3/4 of the byte cap, so a rewrite does not
happen every run. Appends and rewrites take an ``fcntl`` lock on a sidecar ``history.jsonl.lock`` (the
lock outlives an ``os.replace`` of the data file); a run that cannot get the lock within
``LOCK_WAIT_S`` skips its sample. Every function fails open: an error means no history, never an
exception.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX: append without a lock
    fcntl = None

SCHEMA = 1
KEEP_S = 24 * 3600
MAX_BYTES = 2 * 1024 * 1024
TRIM_TO = 3 * MAX_BYTES // 4
LOCK_WAIT_S = 0.1
TAIL_BYTES = 256 * 1024          # the menu reads only the newest samples
SPARK_POINTS = 30
SUBS_PER_AGENT = 8
AGENTS_MAX = 40
NO_HISTORY_ENV = "APEX_WIDGET_NO_HISTORY"
BLOCKS = "▁▂▃▄▅▆▇█"


def widget_dir() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME") or Path.home() / ".apex-router") / "widget"


def history_path() -> Path:
    return widget_dir() / "history.jsonl"


def disabled() -> bool:
    return os.environ.get(NO_HISTORY_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return v


def _r(v, nd=1):
    v = _num(v)
    return round(v, nd) if v is not None else None


# ---- the sample -----------------------------------------------------------------------------

def sample(snap: dict) -> dict:
    """One compact history row from a snapshot (numbers, states and ids only)."""
    from . import agent_resources as ar
    snap = snap if isinstance(snap, dict) else {}
    ts = _num(snap.get("ts")) or time.time()
    agents = []
    for a in snap.get("agents") or []:
        if not isinstance(a, dict) or a.get("error") or not isinstance(a.get("session_id"), str):
            continue
        res = a.get("res") if isinstance(a.get("res"), dict) else {}
        tree = res.get("tree") if isinstance(res.get("tree"), dict) else {}
        main = res.get("telemetry") if isinstance(res.get("telemetry"), dict) else {}
        tot = ar.session_totals(res)
        alive = bool(tree.get("alive"))
        row = {"session": a["session_id"], "kind": str(a.get("kind") or "?"),
               "status": ar.display_state(a),
               "footprint_mb": _r(tree.get("footprint_mb")) if alive else None,
               "cpu_pct": _r(tree.get("cpu_pct")) if alive and tree.get("cpu_src") == "sampled"
               else None,
               "io_mbs": _r(ar.io_rate(tree), 3) if alive else None,
               "ctx": main.get("ctx_tokens") if _num(main.get("ctx_tokens")) is not None else None,
               "ctx_window": main.get("ctx_window") if _num(main.get("ctx_window")) else None,
               "req_5m": tot.get("req_5m") or 0}
        subs = res.get("subagents") if isinstance(res.get("subagents"), dict) else {}
        sl = [{"id": str(s.get("id")), "state": str(s.get("state") or "?")}
              for s in (subs.get("list") or [])[:SUBS_PER_AGENT]
              if isinstance(s, dict) and s.get("id")]
        if sl:
            row["subagents"] = sl
        agents.append({k: v for k, v in row.items() if v is not None})
        if len(agents) >= AGENTS_MAX:
            break
    s = snap.get("system") if isinstance(snap.get("system"), dict) else {}
    la = s.get("loadavg")
    net = s.get("net") if isinstance(s.get("net"), dict) else {}
    system = {"gpu_util_pct": _num(s.get("gpu_util_pct")), "gpu_mem_mb": _r(s.get("gpu_mem_mb")),
              "load1": _r(la[0], 2) if isinstance(la, list) and la else None,
              # cumulative interface bytes: the next refresh turns them into a rate
              "net_rx": _num(net.get("rx")), "net_tx": _num(net.get("tx")),
              "net_if": net.get("ifs") if isinstance(net.get("ifs"), str) else None}
    return {"v": SCHEMA, "ts": round(ts, 1), "agents": agents,
            "system": {k: v for k, v in system.items() if v is not None}}


# ---- write ----------------------------------------------------------------------------------

def _lock(fh, wait: float = LOCK_WAIT_S) -> bool:
    if fcntl is None:
        return True
    end = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= end:
                return False
            time.sleep(0.005)


def _first_ts(path: Path):
    try:
        with open(path, "rb") as fh:
            line = fh.readline(64 * 1024)
        return _num(json.loads(line).get("ts"))
    except (OSError, ValueError, AttributeError):
        return None


def _trim(path: Path, now: float, keep_s: float, trim_to: int) -> None:
    """Rewrite ``path`` with the newest valid lines within ``keep_s`` and ``trim_to`` bytes."""
    try:
        raw = path.read_bytes().split(b"\n")
    except OSError:
        return
    kept, size = [], 0
    for line in reversed(raw):
        if not line.strip():
            continue
        try:
            ts = _num(json.loads(line).get("ts"))
        except (ValueError, AttributeError):
            continue
        if ts is None or ts < now - keep_s:
            continue
        if size + len(line) + 1 > trim_to:
            break
        kept.append(line)
        size += len(line) + 1
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fh:
            fh.write(b"".join(x + b"\n" for x in reversed(kept)))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def append(snap_or_sample: dict, path: Path | None = None, now: float | None = None,
           keep_s: float = KEEP_S, max_bytes: int = MAX_BYTES,
           trim_to: int | None = None) -> bool:
    """Append one sample (a snapshot is converted with ``sample``); trim when over 24 h / 2 MB.
    Returns True when a line was written. Never raises."""
    try:
        row = snap_or_sample if snap_or_sample.get("v") == SCHEMA else sample(snap_or_sample)
        path = Path(path) if path else history_path()
        now = time.time() if now is None else now
        trim_to = TRIM_TO if trim_to is None else trim_to
        trim_to = min(trim_to, max_bytes)
        line = json.dumps(row, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = path.with_name(path.name + ".lock")
        with os.fdopen(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as lk:
            if not _lock(lk):
                return False
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(fd, 0o600)  # an older 0644 file is tightened on the next append
            with os.fdopen(fd, "ab") as fh:
                fh.write(line)
                fh.flush()
                size = os.fstat(fh.fileno()).st_size
            first = _first_ts(path)
            if size > max_bytes or (first is not None and first < now - keep_s):
                _trim(path, now, keep_s, trim_to)
        return True
    except Exception:  # noqa: BLE001 — history is optional: fail open
        return False


# ---- read -----------------------------------------------------------------------------------

def load(since: float = 0.0, path: Path | None = None, max_bytes: int | None = None) -> list:
    """Samples with ``ts >= since``, oldest first. ``max_bytes`` reads only the file's tail (the
    first, possibly cut, line is then dropped). Bad lines are skipped; never raises."""
    path = Path(path) if path else history_path()
    try:
        with open(path, "rb") as fh:
            if max_bytes:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                data = fh.read()
                lines = data.split(b"\n")
                if size > max_bytes:
                    lines = lines[1:]
            else:
                lines = fh.read().split(b"\n")
    except OSError:
        return []
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(row, dict) or not isinstance(row.get("agents"), list):
            continue
        ts = _num(row.get("ts"))
        if ts is None or ts < since:
            continue
        out.append(row)
    out.sort(key=lambda r: r["ts"])
    return out


def agent_series(samples: list, session_id: str, key: str) -> list:
    """[(ts, value or None)] of one agent's ``key`` over the samples where it appears."""
    out = []
    for s in samples or []:
        for a in s.get("agents") or []:
            if isinstance(a, dict) and a.get("session") == session_id:
                out.append((s["ts"], _num(a.get(key))))
                break
    return out


def system_series(samples: list, key: str) -> list:
    return [(s["ts"], _num((s.get("system") or {}).get(key))) for s in samples or []
            if isinstance(s.get("system"), dict)]


# ---- sparklines ------------------------------------------------------------------------------

SPARK_MIN_SPAN = 0.05


def sparkline(values, zero: bool = True, width: int | None = SPARK_POINTS) -> str:
    """Unicode sparkline of the last ``width`` values. ``zero`` scales from 0 to the max (cpu,
    rates); otherwise min..max (memory, context) over a span of at least ``SPARK_MIN_SPAN`` of
    the max, so a 5 MB wobble on 700 MB does not draw full height. None / NaN / inf draw a
    space. A constant
    series draws ``▁`` when it is all zero, else full height (``zero``) or mid (``▄``). An empty
    or all-missing series is ''."""
    vals = [_num(v) for v in (values or [])]
    if width:
        vals = vals[-width:]
    have = [v for v in vals if v is not None]
    if not have:
        return ""
    hi = max(have)
    lo = min(0.0, min(have)) if zero else min(have)
    if not zero and hi != lo:
        lo = min(lo, hi - SPARK_MIN_SPAN * abs(hi))
    out = []
    for v in vals:
        if v is None:
            out.append(" ")
        elif hi == lo:
            out.append(BLOCKS[0] if hi == 0 else (BLOCKS[-1] if zero else BLOCKS[3]))
        else:
            i = round((v - lo) / (hi - lo) * (len(BLOCKS) - 1))
            out.append(BLOCKS[max(0, min(len(BLOCKS) - 1, i))])
    return "".join(out)


# ---- telemetry buckets -----------------------------------------------------------------------

def buckets(rows, now: float, n: int = 12, size_s: float = 300.0) -> dict:
    """Per session_id: {"req": [n], "out": [n], "in": [n], "cached": [n], "write": [n],
    "err": [n]} — counts / token sums per ``size_s`` bucket, oldest first, the last bucket ending
    at ``now``. Rows outside the span or without a session are skipped."""
    from . import agent_resources as ar
    start = now - n * size_s
    out: dict = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sid = r.get("session_id")
        ts = _num(r.get("ts"))
        if not isinstance(sid, str) or ts is None or ts < start or ts > now:
            continue
        i = min(n - 1, int((ts - start) // size_s))
        b = out.get(sid)
        if b is None:
            b = out[sid] = {k: [0] * n for k in ("req", "out", "in", "cached", "write", "err",
                                                 "net")}
        b["req"][i] += 1
        b["out"][i] += int(_num(r.get("tokens_out")) or 0)
        b["in"][i] += ar.fresh_input(r)
        b["cached"][i] += ar._cache_read(r)
        b["write"][i] += ar._cache_write(r)
        b["err"][i] += 1 if r.get("is_error") else 0
        b["net"][i] += int(_num(r.get("bytes_up")) or 0) + int(_num(r.get("bytes_down")) or 0)
    return out


NET_GAP_MAX_S = 10 * 60          # a longer gap between samples is not one rate (sleep, plugin off)


def net_rates(samples: list) -> list:
    """[(ts, rx B/s, tx B/s, dt s)] between consecutive samples that carry interface counters for the
    same interfaces, at most NET_GAP_MAX_S apart. A counter that went down (reboot, interface
    reset) gives no point rather than a negative or a wrapped rate — the Stats app clamps the
    same way."""
    out = []
    prev = None
    for s in samples or []:
        sy = s.get("system") if isinstance(s.get("system"), dict) else {}
        rx, tx, ifs, ts = _num(sy.get("net_rx")), _num(sy.get("net_tx")), sy.get("net_if"), \
            _num(s.get("ts"))
        if rx is None or tx is None or ts is None:
            continue
        if prev and prev[3] == ifs and 0 < ts - prev[0] <= NET_GAP_MAX_S \
                and rx >= prev[1] and tx >= prev[2]:
            dt = ts - prev[0]
            out.append((ts, (rx - prev[1]) / dt, (tx - prev[2]) / dt, dt))
        prev = (ts, rx, tx, ifs)
    return out
