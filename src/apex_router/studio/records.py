"""Records for studio v2 (Metro): ``GET /api/records``. Read-only; contract: docs/design/studio-v2/API.md.

What it shows (the owner's question: "how fast, how slow, how much on disk, how much analyzed and
through what"):

  throughput  wire bytes (telemetry ``bytes_up`` / ``bytes_down``, v10) per second over 1 min and
              15 min, both proxies. A row counts in a window when it ENDED in it (``ts`` + v13
              ``total_ms``; ``ts`` alone on older rows).
  latency     p50 / p95 of ``ttft_ms`` (first word) and ``total_ms`` (whole answer, v13) over 15 min
              and 1 h, successful rows only. Rows from before v13 have no whole-answer time.
  disk        the live telemetry files (active), the rotated generations ``telemetry.jsonl.<N>``
              (archived: the proxy keeps the newest APEX_TELEMETRY_KEEP, default 20), and a fixed
              list of other stores with their size.
  pipelines   PIPELINES, a fixed list of every job that reads the telemetry (code + launchd). For
              each: what it reads, what it makes, its schedule, its last run (launchd + output file
              mtime) and how much it has read (a cursor when it keeps one, else the rows of its last
              window). Each entry fails open: an error gives that entry an ``error``, never a 500.

Bounds: one tail read of at most READ_CAP bytes per telemetry file (plus the newest rotated
generation only when the live file starts inside the window), ``stat`` calls for sizes, one
``launchctl print`` per installed job (cached LAUNCHD_TTL_S; tests inject the runner). No file is
read whole. Privacy: numbers, fixed labels and fixed sentences only; no path, no row text.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import threading
import time
from pathlib import Path

from . import live
from .flow import Paths

READ_CAP = 16 * 1024 * 1024       # bytes read from the end of one telemetry file
LIST_MAX = 2000                   # directory entries looked at, at most, per listing
GENS_MAX = 64                     # rotated generations kept, at most (newest)
PLIST_MAX = 256 * 1024            # bytes of one launchd plist
STORE_MAX = 32 * 1024 * 1024      # bytes of one JSON store
RUN_OUT_MAX = 64 * 1024           # bytes kept from one launchctl call
SPAN_S = 2 * 3600                 # rows parsed: the last 2 h (the widest window below + slack)
TTL_S = 8.0
LAUNCHD_TTL_S = 60.0
THROUGHPUT_WINDOWS = {"1m": 60, "15m": 15 * 60}
LATENCY_WINDOWS = {"15m": 15 * 60, "1h": 3600}
READ_TO_NOW_S = 120               # "read to now" when the last read ended this close to now


def _home(paths: Paths) -> Path:
    return Path(paths.env.get("HOME") or Path.home())


# ---- the pipeline map (fixed; no path from the request) ----------------------------------------
# reads: "window" = the rows of the last `window_s` before each run; "files" = the live file + the
# newest rotated file, whole; "cursor" = it records what it has read (the reservoir fold).
# schedule: launchd label (StartInterval / StartCalendarInterval in its plist) or a fixed text.
PIPELINES = (
    {"id": "menubar", "name": "Menu bar", "source": "proxy telemetry",
     "reads": "window", "window_s": 3600,
     "makes": "the menu-bar readout and its history (pressure, errors, tokens per agent)",
     "schedule": {"kind": "interval", "every_s": 60, "text": "every minute (SwiftBar)"},
     "outputs": ("router:widget/history.jsonl",), "swiftbar": "apex.1m.sh"},
    {"id": "daily", "name": "Daily report", "source": "proxy telemetry",
     "reads": "files", "gens": 1,
     "makes": "the daily digest: retry test, refusals, route labels, cache hand-off threshold",
     "schedule": {"kind": "launchd", "label": "com.apex-router.daily", "text": "daily at 09:00"},
     "outputs": ("router:offload_daily.md", "router:labeled_table.jsonl", "router:handoff_threshold.json",
                 "router:logs/com.apex-router.daily.log")},
    {"id": "archive", "name": "Archive keeper", "source": "rotated telemetry",
     "reads": "cursor",
     "makes": "a sample of every old request log before it is deleted (counts per error cause)",
     "schedule": {"kind": "event", "text": "when the request log reaches 64 MB"},
     "outputs": ("main:telemetry_reservoir.json",)},
    {"id": "pressure", "name": "Provider load", "source": "proxy telemetry",
     "reads": "window", "window_s": 15 * 60,
     "makes": "the provider load level (OK, busy, overloaded) for agents that start work",
     "schedule": {"kind": "demand", "text": "on demand (apex-router pressure)"},
     "outputs": ("router:pressure.json",)},
    {"id": "offload", "name": "Local-model report", "source": "offload log",
     "reads": "files", "gens": 0,
     "makes": "the daily report of work done by models on this Mac",
     "schedule": {"kind": "launchd", "label": "com.apex.ornith-daily", "text": "daily at 09:00"},
     "outputs": ("home:.apex/offload_daily.md",), "input": "home:.apex/offload_telemetry.jsonl"},
    {"id": "worldmodel", "name": "World-model data", "source": "proxy telemetry",
     "reads": "files", "gens": 0,
     "makes": "training steps for the world model (the model of each step comes from telemetry)",
     "schedule": {"kind": "demand", "text": "on demand (apex-router worldmodel build)"},
     "outputs": ("router:worldmodel/manifest.json",)},
)

# other stores on disk (fixed labels; sizes only)
STORES = (
    ("Request index", "main:state.db"),
    ("Archive sample", "main:telemetry_reservoir.json"),
    ("Local-model work log", "home:.apex/offload_telemetry.jsonl"),
    ("Model-choice log", "router:route_log.jsonl"),
    ("Resolve log", "router:conformance.jsonl"),
    ("Route labels", "router:labeled_table.jsonl"),
    ("Menu-bar history", "router:widget/history.jsonl"),
    ("Second-opinion runs", "router:xval_runs.jsonl"),
)


def _resolve(paths: Paths, ref: str) -> Path:
    where, rel = ref.split(":", 1)
    base = {"router": paths.router_home, "main": paths.proxy_home("main"),
            "local": paths.proxy_home("local"), "home": _home(paths)}[where]
    return base / rel


def _stat(p: Path):
    try:
        st = p.stat()
    except OSError:
        return None
    return st if p.is_file() else None


def _size(p: Path) -> int:
    st = _stat(p)
    return int(st.st_size) if st else 0


def read_capped(p: Path, cap: int) -> bytes:
    """The whole file, or ValueError when it is larger than ``cap`` (one read of cap+1 bytes: no
    size-check-then-read gap)."""
    with open(p, "rb") as f:
        raw = f.read(cap + 1)
    if len(raw) > cap:
        raise ValueError("store too large")
    return raw


def generations(path: Path) -> list:
    """Rotated generations ``<name>.<N>`` (only exact integer suffixes), oldest first; at most
    LIST_MAX entries looked at and the newest GENS_MAX kept."""
    pat = re.compile(re.escape(path.name) + r"\.(\d+)$")
    found = []
    try:
        with os.scandir(path.parent) as it:
            for i, e in enumerate(it):
                if i >= LIST_MAX:
                    break
                m = pat.fullmatch(e.name)
                if m and e.is_file(follow_symlinks=False):
                    found.append((int(m.group(1)), path.parent / e.name))
    except OSError:
        return []
    return [q for _, q in sorted(found)[-GENS_MAX:]]


# ---- telemetry rows (one bounded read) ------------------------------------------------------------

def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _parse(lines: list, since: float, px: str = "main") -> tuple[list, float | None]:
    out, first = [], None
    for ln in lines:
        m = live._TS_RE.search(ln[:400]) or live._TS_RE.search(ln)
        if not m:
            continue
        try:
            ts = live._norm_ts(float(m.group(1)))
        except ValueError:
            continue
        if ts is None:
            continue
        first = ts if first is None else min(first, ts)
        if ts < since - 3600:            # a request that started before the span can end inside it
            continue
        try:
            row = json.loads(ln)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if not isinstance(row, dict) or row.get("ev") == "hb" or "client" not in row:
            continue
        tot = _num(row.get("total_ms"))
        end = ts + (tot / 1000.0 if tot and tot > 0 else 0.0)
        if end < since:
            continue
        out.append({"px": px, "ts": ts, "end": end, "up": max(0, int(_num(row.get("bytes_up")) or 0)),
                    "down": max(0, int(_num(row.get("bytes_down")) or 0)),
                    "ttft": _num(row.get("ttft_ms")), "total": tot, "err": bool(row.get("is_error"))})
    return out, first


def rows(path: Path, since: float, cap: int = READ_CAP, px: str = "main") -> dict:
    """Request rows that ended at or after ``since`` (live file tail + the newest rotated file when
    the live file starts inside the window). ``complete`` is False when the cap cut the window."""
    path = Path(path)
    present = _stat(path) is not None
    lines = live._lines(path, cap) if present else []
    out, first = _parse(lines, since, px)
    complete = not present or _size(path) <= cap or (first is not None and first <= since)
    if present and (first is None or first > since) and _size(path) <= cap:
        gens = generations(path)
        if gens:
            older, ofirst = _parse(live._lines(gens[-1], cap), since, px)
            out = older + out
            complete = _size(gens[-1]) <= cap or (ofirst is not None and ofirst <= since)
    return {"rows": out, "present": present, "complete": complete}


# ---- throughput / latency ----------------------------------------------------------------------

def _pct(vals: list, q: float):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def throughput(rs: list, now: float) -> dict:
    out = {}
    for name, span in THROUGHPUT_WINDOWS.items():
        w = [r for r in rs if now - span <= r["end"] <= now]
        up, down = sum(r["up"] for r in w), sum(r["down"] for r in w)
        out[name] = {"up_bps": round(up / span, 1), "down_bps": round(down / span, 1),
                     "up_bytes": up, "down_bytes": down, "requests": len(w)}
    return out


def latency(rs: list, now: float) -> dict:
    out = {}
    for name, span in LATENCY_WINDOWS.items():
        ok = [r for r in rs if now - span <= r["end"] <= now and not r["err"]]
        ttft = [r["ttft"] for r in ok if r["ttft"] and r["ttft"] > 0]
        tot = [r["total"] for r in ok if r["total"] and r["total"] > 0]

        def pq(v):
            return {"p50": round(statistics.median(v)) if v else None, "p95": round(_pct(v, 0.95)) if v else None,
                    "n": len(v)}
        out[name] = {"first_word": pq(ttft), "whole_answer": pq(tot), "requests": len(ok)}
    return out


# ---- disk ------------------------------------------------------------------------------------

def disk(paths: Paths) -> dict:
    active = archived = n_arch = 0
    for proxy in ("main", "local"):
        p = paths.telemetry(proxy)
        active += _size(p)
        for g in generations(p) if p.parent.is_dir() else []:
            archived += _size(g)
            n_arch += 1
    other = []
    for label, ref in STORES:
        try:
            st = _stat(_resolve(paths, ref))
        except Exception:  # noqa: BLE001 — a bad store is "absent", never an error
            st = None
        other.append({"label": label, "bytes": int(st.st_size) if st else 0, "present": st is not None})
    try:
        keep = max(1, int(paths.env.get("APEX_TELEMETRY_KEEP") or 20))
    except ValueError:
        keep = 20
    return {"active_bytes": active, "archived_bytes": archived, "archived_files": n_arch, "keep_files": keep,
            "other": other}


# ---- launchd (injected runner; tests never call the real launchctl) ---------------------------------

def _run(argv: list, timeout: float = 5.0) -> tuple:
    """(rc, text): stdout+stderr, at most RUN_OUT_MAX bytes kept; time, bytes and the whole process
    tree are bounded by safe_proc.run_capped (raises subprocess.TimeoutExpired)."""
    from ..safe_proc import run_capped
    rc, out = run_capped(argv, timeout, RUN_OUT_MAX, merge_stderr=True)
    return rc, out.decode("utf-8", "replace")


_ld_cache: dict = {}
_ld_lock = threading.Lock()
_LD_RE = {"state": re.compile(r"^\s*state = ([a-z ]{1,24})$", re.M), "runs": re.compile(r"^\s*runs = (\d+)", re.M),
          "last_exit": re.compile(r"^\s*last exit code = (-?\d+)", re.M)}


def launchd_state(paths: Paths, label: str, run=None) -> dict:
    """{installed, loaded, state, runs, last_exit} for a job label; fail-open (None fields)."""
    plist = paths.launch_agents / f"{label}.plist"
    out = {"installed": plist.is_file(), "loaded": None, "state": None, "runs": None, "last_exit": None}
    if not out["installed"]:
        return out
    run = run or _run
    key = (label, id(run))
    now = time.time()
    with _ld_lock:
        hit = _ld_cache.get(key)
        if hit and now - hit[0] < LAUNCHD_TTL_S:
            return dict(hit[1])
    try:
        rc, txt = run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"])
        out["loaded"] = rc == 0
        if rc == 0:
            for k, rx in _LD_RE.items():
                m = rx.search(txt or "")
                if m:
                    out[k] = int(m.group(1)) if k != "state" else m.group(1).strip()
    except Exception:  # noqa: BLE001 — launchd is extra detail; the entry still shows
        pass
    with _ld_lock:
        _ld_cache[key] = (now, dict(out))
        if len(_ld_cache) > 32:
            _ld_cache.pop(next(iter(_ld_cache)))
    return out


def _plist_schedule(paths: Paths, label: str) -> dict | None:
    import plistlib
    try:
        d = plistlib.loads(read_capped(paths.launch_agents / f"{label}.plist", PLIST_MAX))
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    if isinstance(d.get("StartInterval"), int):
        return {"every_s": d["StartInterval"]}
    c = d.get("StartCalendarInterval")
    if isinstance(c, dict) and isinstance(c.get("Hour"), int):
        return {"every_s": 86400, "at": f"{c['Hour']:02d}:{int(c.get('Minute') or 0):02d}"}
    return {"every_s": None}


# ---- one pipeline -----------------------------------------------------------------------------

def _rows_before(rs: list, end: float, span: float) -> int:
    return sum(1 for r in rs if r["px"] == "main" and end - span <= r["ts"] <= end)


def _reservoir(paths: Paths) -> dict:
    p = paths.proxy_home("main") / "telemetry_reservoir.json"
    try:
        doc = json.loads(read_capped(p, STORE_MAX))
    except FileNotFoundError:
        doc = {"folded": []}
    if not isinstance(doc, dict):
        raise ValueError("store not an object")
    folded = [f for f in (doc.get("folded") or []) if isinstance(f, dict)]
    names = {str(f.get("name")) for f in folded}
    gens = generations(paths.telemetry("main"))
    waiting = [g for g in gens if g.name not in names]
    at = max((_num(f.get("at")) or 0 for f in folded), default=0) or None
    return {"files_read": len(folded), "rows": sum(int(_num(f.get("rows")) or 0) for f in folded),
            "files_waiting": len(waiting), "archived_files": len(gens), "last_at": at}


def pipeline(paths: Paths, spec: dict, rs: list, complete: bool, now: float, run=None) -> dict:
    sched = dict(spec["schedule"])
    item = {"id": spec["id"], "name": spec["name"], "source": spec["source"], "makes": spec["makes"],
            "schedule": {"kind": sched["kind"], "text": sched["text"], "every_s": sched.get("every_s")},
            "launchd": None, "last_run_ts": None, "read_to_ts": None, "behind_s": None, "late": False,
            "reads": {"kind": spec["reads"], "window_s": spec.get("window_s"), "rows": None, "bytes": None,
                      "files": None, "files_waiting": None},
            "state": "unknown", "error": None}
    try:
        if sched["kind"] == "launchd":
            ld = launchd_state(paths, sched["label"], run)
            item["launchd"] = {k: ld[k] for k in ("installed", "loaded", "state", "runs", "last_exit")}
            ps = _plist_schedule(paths, sched["label"]) if ld["installed"] else None
            if ps and ps.get("every_s"):
                item["schedule"]["every_s"] = ps["every_s"]
                if ps.get("at"):
                    item["schedule"]["text"] = f"daily at {ps['at']}"
        installed = True
        if spec.get("swiftbar"):
            installed = (_home(paths) / "Library" / "Application Support" / "SwiftBar" / "Plugins"
                         / spec["swiftbar"]).exists()
        elif item["launchd"] is not None:
            installed = bool(item["launchd"]["installed"])
        mt = [st.st_mtime for ref in spec["outputs"] if (st := _stat(_resolve(paths, ref)))]
        last = max(mt) if mt else None
        r = item["reads"]
        if spec["reads"] == "cursor":
            res = _reservoir(paths)
            r.update(rows=res["rows"], files=res["files_read"], files_waiting=res["files_waiting"])
            last = res["last_at"] or last
            item["read_to_ts"] = last
        elif spec["reads"] == "window":
            if last is not None:
                item["read_to_ts"] = last
                if complete and now - last <= SPAN_S - spec["window_s"]:
                    r["rows"] = _rows_before(rs, last, spec["window_s"])
        else:
            src = _resolve(paths, spec["input"]) if spec.get("input") else paths.telemetry("main")
            files = [src] + (generations(src)[-spec["gens"]:] if spec["gens"] else [])
            r.update(bytes=sum(_size(f) for f in files), files=sum(1 for f in files if _stat(f)))
            item["read_to_ts"] = last
        item["last_run_ts"] = last
        if item["read_to_ts"] is not None and spec["reads"] != "cursor":
            item["behind_s"] = round(max(0.0, now - item["read_to_ts"]), 1)
        every = item["schedule"]["every_s"]
        if last is not None and every:
            item["late"] = now - last > 2 * every + 300
        ld = item["launchd"]
        if not installed:
            item["state"] = "not installed"
        elif ld and ld.get("last_exit") not in (None, 0):
            item["state"] = "failed"
        elif last is None:
            item["state"] = "never ran"
        elif spec["reads"] == "cursor":
            item["state"] = "ok"          # files wait by design: each is read just before it is deleted
        elif item["late"]:
            item["state"] = "late"
        else:
            item["state"] = "ok"
    except Exception as e:  # noqa: BLE001 — one bad entry shows its error; the others still show
        item["error"] = f"cannot read: {type(e).__name__}"
        item["state"] = "unknown"
    return item


# ---- the endpoint ------------------------------------------------------------------------------

def collect(paths: Paths, now: float | None = None, run=None) -> dict:
    now = time.time() if now is None else now
    rs, complete, present = [], True, False
    for proxy in ("main", "local"):
        got = rows(paths.telemetry(proxy), now - SPAN_S, px=proxy)
        rs += got["rows"]
        complete = complete and got["complete"]
        present = present or got["present"]
    rs.sort(key=lambda r: r["end"])
    out = {"generated_at": round(now, 3), "telemetry_present": present, "complete": complete,
           "throughput": throughput(rs, now), "latency": latency(rs, now)}
    try:
        out["disk"] = disk(paths)
    except Exception as e:  # noqa: BLE001
        out["disk"] = {"error": f"cannot read: {type(e).__name__}"}
    out["pipelines"] = [pipeline(paths, spec, rs, complete, now, run) for spec in PIPELINES]
    return out


_cache: dict = {}
_lock = threading.Lock()


def collect_cached(paths: Paths, run=None) -> dict:
    key = (str(paths.telemetry("main")), str(paths.telemetry("local")), str(paths.router_home),
           paths.env.get("HOME"), id(run))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL_S:
            return hit[1]
    out = collect(paths, now, run)
    with _lock:
        _cache[key] = (now, out)
        if len(_cache) > 8:
            _cache.pop(next(iter(_cache)))
    return out
