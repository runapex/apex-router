"""One read-only snapshot of everything the menu-bar widget shows: ``apex-router snapshot``.

Composes, without writing anything (no ``pressure.json``, no cache, no log):

  pressure   ``pressure.compute()`` over the last 15 min of proxy telemetry (level, sample, causes;
             ``families`` maps family -> GREEN/AMBER/RED like the plugin's ``parsePressure``)
  errors15m  client-visible failures (``is_error`` rows) in the last 15 min, by ``error_cause``
  measure    the newest plugin ``measure`` event in ``~/.apex-router/observe/*.jsonl`` (5h/7d limit
             meter, cost) with its age — only as fresh as the last open plugin session
  agents     ``agents.discover()`` — Claude Code / pi / Codex sessions by log mtime
  worker     local worker launchd pid + queue counts (``res``: its process tree, the ollama
             server's tree and loaded models with VRAM)
  system     ``agent_resources`` — system-wide GPU utilisation / in-use memory, load average,
             ollama models (VRAM, unload time), footprint / cpu of all agent process trees, all
             proxy traffic 60 min; each agent gains ``res`` (pid, footprint, cpu % and disk MB/s
             from two samples, top child processes, subagents with lifecycle, token split and
             context size of the latest request)
  graph      ``{nodes, edges}``: session -spawned-> subagent, session -runs-> process,
             session/subagent -calls-> model
  proxy      ``/healthz`` on the loopback proxy
  adapters   ``$DATAPCE_HOME/adapters/*.json`` (default ``~/.datapce``) and
             ``~/.apex-router/adapters/*.json``, de-duplicated by file name (the datapce home
             wins); ``$APEX_ADAPTERS_DIR`` replaces both. Each file is
             ``{"title": str, "rows": [str], "ts": epoch}``, written by anything; shown as one extra
             menu section. Capped at 5 files x 10 rows x 120 chars; malformed files are skipped.

``--json`` prints the snapshot; ``--menubar`` prints SwiftBar/xbar plugin output (bar cell, ``---``,
menu lines); ``--graph`` prints the call graph as an indented text tree. Every sub-collector fails open: an error becomes an ``error`` field, never an
exception, and the command always exits 0. Stdlib only.

The menu shows no dollar amounts (``measure.cost_usd`` stays in ``--json``). It is bounded: 8
active sessions, 8 subagents each, about ``MENU_LINES_MAX`` lines; hidden rows are summed into a
"… N more" line; every visible line is at most ``MENU_WIDTH`` characters (SwiftBar parameters and
``--`` submenu markers not counted), ``--graph`` lines at most 120. All subprocess / network calls
and the subagent log scan share one ``agent_resources.Deadline``. Control characters (C0, DEL,
C1) never reach a line.

Active / idle for a Claude session is Claude's own status (``busy`` / ``idle`` in
``~/.claude/sessions/<pid>.json``); the log-mtime state is only the fallback
(``agent_resources.agent_active``).

Bar: ``● N`` plus `` ⚠`` when an agent is stuck or erroring (``agent_resources.agent_flagged``:
a subagent running > 20 min, an error in the last 5 min or a 60-min error rate >= 5%, or a
context >= 85% of a known window).
Bar dot colour: green = GREEN with a sufficient sample; orange = AMBER; red = RED; gray when the
sample is insufficient (most 15-min windows), UNKNOWN, or the snapshot itself failed. A RED forced
by a fresh retry-after stays red even on a small sample (the provider said back off).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path

from . import agent_resources
from . import agents as agents_mod
from . import pressure

SCHEMA = 1
ERRORS_WINDOW_S = 15 * 60
OBSERVE_FILES_MAX = 3
OBSERVE_TAIL_BYTES = 1 << 20
ADAPTERS_MAX = 5
ADAPTER_ROWS_MAX = 10
ROW_CHARS_MAX = 120
ADAPTER_BYTES_MAX = 256 * 1024
STALE_S = 24 * 3600

COLORS = {"green": "#34C759", "orange": "#FF9500", "red": "#FF3B30", "gray": "#8E8E93"}
_PLUGIN_LEVELS = ("GREEN", "AMBER", "RED")
_LIMIT_SHORT = {"five_hour": "5h", "seven_day": "7d", "seven_day_opus": "7d opus",
                "seven_day_sonnet": "7d sonnet"}


def router_home() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME") or Path.home() / ".apex-router")


def datapce_home() -> Path:
    d = os.environ.get("DATAPCE_HOME")
    return Path(d).expanduser() if d else Path.home() / ".datapce"


def adapters_dirs() -> list:
    """Adapter directories, highest precedence first. ``$APEX_ADAPTERS_DIR`` replaces both
    defaults; otherwise ``$DATAPCE_HOME/adapters`` then ``~/.apex-router/adapters``."""
    d = os.environ.get("APEX_ADAPTERS_DIR")
    if d:
        return [Path(d).expanduser()]
    return [datapce_home() / "adapters", router_home() / "adapters"]


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _epoch_s(v):
    """Seconds since the epoch from seconds or milliseconds; None if not a number."""
    v = _num(v)
    if v is None or not math.isfinite(v):  # json accepts Infinity/NaN
        return None
    return v / 1000.0 if v > 1e12 else float(v)


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


# ---- collectors -----------------------------------------------------------------------------

def pressure_block(telemetry=None, now: float | None = None) -> dict:
    r = pressure.compute(telemetry, now=now)          # compute() never writes pressure.json
    ov = r.get("overall") or {}
    out = {
        "level": r.get("level", "UNKNOWN"),
        "insufficient_sample": bool(r.get("insufficient_sample", True)),
        "min_sample": r.get("min_sample"),
        "window_min": r.get("window_min"),
        "n": ov.get("requests", 0),
        "causes": {k: ov.get(k) for k in ("rate_limited", "rate_limited_rate", "transport_errors",
                                          "transport_rate", "retried", "local",
                                          "upstream_rejected")},
        "retry_after_s": r.get("retry_after_s"),
        "retry_after_recent": r.get("retry_after_recent", False),
        "recommendation": r.get("recommendation"),
        "families": {f: b.get("level") for f, b in (r.get("families") or {}).items()
                     if isinstance(b, dict) and b.get("level") in _PLUGIN_LEVELS},
    }
    if r.get("error"):
        out["error"] = r["error"]
    return out


def errors_block(telemetry=None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    path = Path(telemetry) if telemetry else pressure.default_telemetry_path()
    causes: Counter = Counter()
    for row in pressure.tail_rows(path, now - ERRORS_WINDOW_S):
        if row.get("is_error"):
            causes[str(row.get("error_cause") or "unlabeled")] += 1
    return {"n": sum(causes.values()), "by_cause": dict(causes.most_common()),
            "window_min": ERRORS_WINDOW_S // 60}


def _tail_lines(path: Path, max_bytes: int = OBSERVE_TAIL_BYTES) -> list:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        data = fh.read()
    lines = data.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]                               # first line may be cut mid-row
    return lines


def _pick(row: dict, *keys):
    for k in keys:
        if row.get(k) is not None:
            return row[k]
    return None


def measure_block(observe_dir=None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    d = Path(observe_dir) if observe_dir else router_home() / "observe"
    try:
        files = sorted(d.glob("*.jsonl"), reverse=True)[:OBSERVE_FILES_MAX]
    except OSError:
        files = []
    for f in files:
        try:
            lines = _tail_lines(f)
        except OSError:
            continue
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict) or row.get("ev") != "measure":
                continue
            ts = _epoch_s(row.get("ts"))
            return {
                "limit_kind": _pick(row, "limit_kind", "limitKind"),
                "limit_pct": _num(_pick(row, "limit_pct", "limit_percent", "limitPercent")),
                "resets_at": _pick(row, "resets_at", "resetsAt"),
                "cost_usd": _num(_pick(row, "cost_usd", "costUsd")),
                "ctx_pct": _num(_pick(row, "ctx_pct", "ctx_percent", "ctxPercent")),
                "ts": ts,
                "age_s": round(max(0.0, now - ts), 1) if ts is not None else None,
            }
    return {"limit_kind": None, "limit_pct": None, "resets_at": None, "cost_usd": None,
            "ctx_pct": None, "ts": None, "age_s": None, "missing": True}


def _clip(s, n: int = ROW_CHARS_MAX) -> str:
    s = agent_resources.clean_text(s)                  # one line, no C0 / DEL / C1 controls
    return s if len(s) <= n else s[:n - 1] + "…"


def adapters_block(directory=None, now: float | None = None) -> list:
    now = time.time() if now is None else now
    if directory is None:
        dirs = adapters_dirs()
    elif isinstance(directory, (list, tuple)):
        dirs = [Path(x) for x in directory]
    else:
        dirs = [Path(directory)]
    by_name: dict = {}
    for d in dirs:                                      # first dir wins a name collision
        try:
            for p in d.glob("*.json"):
                if not p.name.startswith(".") and p.name not in by_name and p.is_file():
                    by_name[p.name] = p
        except OSError:
            continue
    files = [by_name[n] for n in sorted(by_name)]
    out = []
    for f in files:
        if len(out) >= ADAPTERS_MAX:
            break
        try:
            if f.stat().st_size > ADAPTER_BYTES_MAX:
                continue
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        title, rows, ts = doc.get("title"), doc.get("rows"), _epoch_s(doc.get("ts"))
        if not isinstance(title, str) or not title.strip() or not isinstance(rows, list):
            continue
        age = round(max(0.0, now - ts), 1) if ts is not None else None
        out.append({
            "title": _clip(title),
            "rows": [_clip(r) for r in rows[:ADAPTER_ROWS_MAX]
                     if isinstance(r, (str, int, float)) and not isinstance(r, bool)],
            "ts": ts,
            "age_s": age,
            "stale": age is None or age > STALE_S,
        })
    return out


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 — every sub-collector fails open
        return {"error": _err(e)}


def _default_worker(dl):
    def pid_fn(label):
        return agents_mod._launchd_pid(label, timeout=dl.timeout(0.3))
    return agents_mod.worker(pid_fn=pid_fn)


def _default_proxy(dl):
    try:
        t = dl.timeout(0.2)
    except agent_resources.DeadlineSkip:
        return {"up": False, "skipped": True, "error": "skipped: deadline"}
    return agents_mod.proxy(timeout=t)


def collect(*, home=None, telemetry=None, observe_dir=None, adapters=None,
            now: float | None = None, proxy_fn=None, worker_fn=None, resources_fn=None,
            deadline=None) -> dict:
    """The snapshot dict. ``home`` overrides ``~`` for agent discovery; the injectable
    ``proxy_fn``/``worker_fn``/``resources_fn`` exist for tests. Never raises; never writes.
    Every subprocess / network call (launchctl, ps, lsof, ioreg, ollama, /healthz) shares one
    ``agent_resources.Deadline``; a source that would start after it is skipped."""
    now = time.time() if now is None else now
    dl = deadline or agent_resources.Deadline()
    found = _safe(agents_mod.discover, home, now)
    found = found if isinstance(found, list) else [found]
    adapters_out = _safe(adapters_block, adapters, now)
    w = _safe(worker_fn) if worker_fn else _safe(_default_worker, dl)
    wpid = w.get("pid") if isinstance(w, dict) else None
    res = _safe(resources_fn or agent_resources.collect, found, home=home, telemetry=telemetry,
                now=now, worker_pid=wpid if isinstance(wpid, int) else None, deadline=dl)
    if isinstance(res, dict) and not res.get("error") and isinstance(res.get("agents"), list):
        found, system = res["agents"], res.get("system") or {}
        graph = res.get("graph") or {"nodes": [], "edges": []}
        if isinstance(w, dict) and res.get("worker"):
            w = dict(w, res=res["worker"])
    else:
        system = res if isinstance(res, dict) else {"error": "resources unavailable"}
        graph = {"nodes": [], "edges": []}
    return {
        "schema": SCHEMA,
        "ts": now,
        "pressure": _safe(pressure_block, telemetry, now),
        "errors15m": _safe(errors_block, telemetry, now),
        "measure": _safe(measure_block, observe_dir, now),
        "agents": found,
        "worker": w,
        "system": system,
        "graph": graph,
        "proxy": _safe(proxy_fn) if proxy_fn else _safe(_default_proxy, dl),
        "adapters": adapters_out if isinstance(adapters_out, list) else [adapters_out],
    }


# ---- menubar formatter ----------------------------------------------------------------------

def dot(snap) -> str:
    """Colour name for the bar dot (see module doc)."""
    p = snap.get("pressure") if isinstance(snap, dict) else None
    if not isinstance(p, dict) or p.get("error"):
        return "gray"
    level = p.get("level")
    # A family RED/AMBER by its own rate shows even when the overall level is calm.
    fams = [v for v in (p.get("families") or {}).values() if v in ("RED", "AMBER")]
    if "RED" in fams:
        return "red"
    if level != "RED" and "AMBER" in fams:
        return "orange"
    if level == "RED":
        return "red"
    if level == "AMBER":
        return "orange"
    if level == "GREEN" and p.get("insufficient_sample") is False:
        return "green"
    return "gray"


def esc(s) -> str:
    """Make user-derived text safe as one SwiftBar menu line: no '|' (the param separator), no
    newlines or other control characters, no leading '-' (submenu / separator markers), at most
    120 chars."""
    s = _clip(s).replace("|", "¦")
    stripped = s.lstrip("-")
    return ("–" * (len(s) - len(stripped)) + stripped) if stripped != s else s


def fmt_age(s) -> str:
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


def _active(snap) -> int:
    """Active agents: Claude's own busy status, the log-mtime state only without one."""
    return sum(1 for a in snap.get("agents") or []
               if isinstance(a, dict) and not a.get("error") and agent_resources.agent_active(a))


def _bar(color: str, text: str) -> str:
    return f"{text} | color={COLORS[color]}"


def _pressure_lines(p: dict, e: dict) -> list:
    if p.get("error"):
        lines = [f"UNKNOWN · {esc(p['error'])}"]
    else:
        level = p.get("level", "UNKNOWN")
        head = f"{level} · {p.get('n', 0)} requests / {p.get('window_min', 15):g} min"
        if p.get("insufficient_sample") and level != "UNKNOWN":
            head += f" · insufficient sample (n={p.get('n', 0)})"
        lines = [head]
        c = p.get("causes") or {}
        parts = [f"{label} {c[k]}" for k, label in (("rate_limited", "429"),
                                                    ("transport_errors", "transport"),
                                                    ("retried", "retried"), ("local", "local"))
                 if c.get(k)]
        if parts:
            lines.append("causes: " + ", ".join(parts))
        if p.get("retry_after_recent") and p.get("retry_after_s") is not None:
            lines.append(f"retry-after {p['retry_after_s']}s")
        fams = p.get("families") or {}
        if fams:
            lines.append("families: " + ", ".join(f"{esc(k)} {v}" for k, v in sorted(fams.items())))
    if isinstance(e, dict) and not e.get("error"):
        by = ", ".join(f"{esc(k)} {v}" for k, v in (e.get("by_cause") or {}).items())
        lines.append(f"errors 15m: {e.get('n', 0)}" + (f" ({by})" if by else ""))
    elif isinstance(e, dict):
        lines.append(f"errors 15m: ? ({esc(e['error'])})")
    return lines


def _measure_line(m: dict) -> str:
    if m.get("error"):
        return f"unavailable · {esc(m['error'])}"
    if m.get("missing"):
        return "no measure yet (needs a plugin session)"
    parts = []
    kind = m.get("limit_kind")
    if m.get("limit_pct") is not None:
        parts.append(f"{esc(_LIMIT_SHORT.get(kind, kind or 'limit'))} {m['limit_pct']:g}%")
    # cost_usd stays in --json only: the widget shows no dollar amounts (owner, 2026-10-06)
    parts.append(f"age {fmt_age(m.get('age_s'))}")
    return " · ".join(parts)


ACTIVE_MAX = agent_resources.SCAN_SESSIONS_MAX      # subagent logs are scanned for exactly these
IDLE_SHOWN_MAX = 8
MENU_LINES_MAX = 80
MENU_WIDTH = 110                                     # visible characters per menu line
LABEL_W = 22
UPARAMS = "emojize=false symbolize=false"     # on every line that carries user-derived text
MONO = "font=Menlo size=12"
_SUB_SYM = {"running": "▶", "quiet": "◦", "done": "✓", "?": "?",
            "active": "▶", "idle": "✓"}                 # the last two: pre-0.4.2 snapshots
_LEVELS = {2: {"subs": 8, "procs": 3, "models": 3}, 1: {"subs": 3, "procs": 1, "models": 1}}


def _tip(s) -> str:
    """A tooltip value: our own numbers plus esc()'d text; no quotes, no '|', one line."""
    return _clip(s, 240).replace('"', "'").replace("|", "¦")


def _u(text: str, mono: bool = False, tip: str | None = None) -> str:
    """One menu line that carries user-derived text (already esc()'d): emoji/SF-symbol
    substitution off, optional monospace columns and tooltip."""
    params = ([MONO] if mono else []) + [UPARAMS]
    if tip:
        params.append(f'tooltip="{_tip(tip)}"')
    return f"{text} | {' '.join(params)}"


def _col(s: str, w: int) -> str:
    s = str(s)
    return s.ljust(w) if len(s) <= w else s[:w - 1] + "…"


def _res(a) -> dict:
    return a.get("res") if isinstance(a, dict) and isinstance(a.get("res"), dict) else {}


def _agent_label(a: dict, width: int | None = None) -> str:
    """``repo session`` for Claude Code, ``kind:repo session`` for pi / Codex — esc()'d. With a
    ``width`` the repo part is shortened (``apex-r…/rsi`` keeps a worktree name), never the
    8-char session id."""
    kind, repo = str(a.get("kind", "?")), a.get("repo")
    head = (repo or "") if kind == "claude" else (f"{kind}:{repo}" if repo else kind)
    head, sess = esc(head), esc(a.get("session", ""))
    if width is not None and sess and len(head) + 1 + len(sess) > width:
        room = max(1, width - len(sess) - 1)
        base, slash, tail = head.rpartition("/")
        if slash and len(tail) + 3 <= room:
            keep = room - len(tail) - 2
            head = base[:keep] + "…/" + tail
        else:
            head = head[:room - 1] + "…"
    return f"{head} {sess}".strip()


def _tree_tip(tree: dict, extra=()) -> str:
    if not tree.get("alive"):
        return " · ".join(extra)
    bits = [f"pid {tree.get('pid')}"]
    if tree.get("uptime_s") is not None:
        bits.append(f"up {fmt_age(tree['uptime_s'])}")
    if tree.get("procs"):
        bits.append(f"{tree['procs']} procs")
    if isinstance(tree.get("cpu_s"), (int, float)):
        bits.append(f"cpu time {fmt_age(tree['cpu_s'])}")
    if isinstance(tree.get("read_mb"), (int, float)):
        bits.append(f"io life {tree['read_mb']:.0f}/{tree.get('write_mb') or 0:.0f}MB")
    if isinstance(tree.get("read_mbs"), (int, float)):
        bits.append(f"io now r {tree['read_mbs']:.2f} w {tree.get('write_mbs') or 0:.2f} MB/s")
    if isinstance(tree.get("rss_mb"), (int, float)):
        bits.append(f"rss {fmt_mb(tree['rss_mb'])}")
    bits.append("cpu sampled" if tree.get("cpu_src") == "sampled" else "cpu = ps average")
    return " · ".join(bits + list(extra))


def _status_word(a: dict) -> str:
    """Claude's own busy/idle when known; else the log-mtime state."""
    st = _res(a).get("status")
    if st:
        return esc(st)
    return esc(a.get("state", "?"))


def _fit(line: str, tail: str = "", width: int = MENU_WIDTH) -> str:
    """``line + tail`` within ``width`` visible characters; the line is cut, never the tail."""
    room = width - len(tail)
    return (line if len(line) <= room else line[:room - 1] + "…") + tail


def _last_text(ts, now) -> str:
    t = _epoch_s(ts)
    if t is None or not isinstance(now, (int, float)):
        return ""
    return f"last {fmt_age(max(0.0, now - t))}"


def _agent_row(a: dict, prefix: str = "", now: float | None = None) -> str:
    """Fixed-width monospace row for an active agent: label, status, footprint, cpu now, then
    traffic 60 min (main + all subagents), context of the latest main-thread request, the 5-min
    request rate and the age of the newest request. Disk io and lifetime numbers: tooltip."""
    if a.get("error"):
        return _u(f"{prefix}{esc(a.get('kind', '?'))} · error · {esc(a['error'])}")
    res = _res(a)
    tree = res.get("tree") or {}
    tot = agent_resources.session_totals(res)
    alive = tree.get("alive")
    mem = fmt_mb(tree.get("footprint_mb")) if alive else "—"
    cpu = f"{tree.get('cpu_pct') or 0:g}%" if alive else "—"
    head = (f"{_col(_agent_label(a, LABEL_W), LABEL_W)} {_col(_status_word(a), 6)} "
            f"{mem.rjust(6)} {cpu.rjust(5)}")
    bits = [f"{tot['requests']} req", f"out {agent_resources._k(tot['tokens_out'])}"]
    ctx = agent_resources.ctx_text(res.get("telemetry"))
    if ctx:
        bits.append(ctx)
    if tot.get("req_5m"):
        bits.append(agent_resources.fmt_rate_min(tot["req_5m"]))
    last = _last_text(tot.get("last_ts"), now) if tot.get("requests") else ""
    if last:
        bits.append(last)
    flag = " ⚠" if agent_resources.agent_flagged(a) else ""
    line = _fit(head + "  " + " · ".join(bits), flag)
    subs = res.get("subagents") if isinstance(res.get("subagents"), dict) else {}
    extra = []
    if subs.get("count"):
        extra.append(f"subagents 60m {subs['count']} ({subs.get('running', 0)} running)")
    if res.get("pid_source"):
        extra.append(f"matched by {res['pid_source']}")
    return _u(prefix + line, mono=True, tip=_tree_tip(tree, extra))


_FLAG_WHY = {"long": "running > 20 min", "errors": "an error in the last 5 min or rate ≥ 5%",
             "ctx": "context ≥ 85% of its window"}


def _sub_row(sa: dict, prefix: str) -> str:
    """``sym label  N req · out X · cache P% · ctx A[/W P%] · last Ns`` (+ the error count when
    flagged for errors). Input / cached / write tokens and run time are in the tooltip."""
    st = sa.get("telemetry") if isinstance(sa.get("telemetry"), dict) else {}
    flags = sa.get("flags") or []
    sym = "⚠" if flags else _SUB_SYM.get(sa.get("state"), "?")
    label = esc(sa.get("description") or sa.get("type") or "?")
    bits = [f"{st.get('requests', 0)} req", f"out {agent_resources._k(st.get('tokens_out', 0))}"]
    share = agent_resources.cache_share(st)
    if share is not None:
        bits.append(f"cache {agent_resources.fmt_pct(share)}")
    if "errors" in flags and agent_resources.err_text(st):
        bits.append(agent_resources.err_text(st))
    ctx = agent_resources.ctx_text(st)
    if ctx:
        bits.append(ctx)
    last = sa.get("last_s", sa.get("age_s"))
    if isinstance(last, (int, float)):
        bits.append(f"last {fmt_age(last)}")
    line = _fit(f"{sym} {_col(label, LABEL_W)}  " + " · ".join(bits))
    tip = [f"type {esc(sa.get('type', '?'))}", f"id {esc(sa.get('id', '?'))}",
           f"state {sa.get('state')}", agent_resources.tokens_text(st)]
    if st.get("errors"):
        tip.append(agent_resources.err_text(st))
    tip.append(agent_resources.lifecycle_text(sa))
    if sa.get("depth"):
        tip.append(f"depth {sa['depth']}")
    if flags:
        tip.append("flagged: " + ", ".join(_FLAG_WHY.get(f, str(f)) for f in flags))
    return _u(prefix + line, mono=True, tip=" · ".join(tip))


def _more_text(n: int, st: dict, what: str = "more") -> str:
    return (f"… {n} {what} ({st.get('requests', 0)} req, "
            f"out {agent_resources._k(st.get('tokens_out', 0))})")


def _brief(st: dict) -> str:
    """``N req · out X · cache P% · E err R% · ctx …`` — input / cached / write go to a tooltip."""
    bits = [f"{st.get('requests', 0)} req", f"out {agent_resources._k(st.get('tokens_out', 0))}"]
    share = agent_resources.cache_share(st)
    if share is not None:
        bits.append(f"cache {agent_resources.fmt_pct(share)}")
    e = agent_resources.err_text(st)
    if e:
        bits.append(e)
    c = agent_resources.ctx_text(st)
    if c:
        bits.append(c)
    return " · ".join(bits)


def _agent_submenu(a: dict, level: int = 2) -> list:
    """SwiftBar ``--`` lines under an active agent at a detail level (2 full, 1 compact, 0 only
    the totals line). Totals are summed over the main thread and ALL subagents before any cut.
    Every user-derived string goes through esc()."""
    res = _res(a)
    tot = agent_resources.session_totals(res)
    out = [_u("--Σ 60m  " + agent_resources.tel_text(tot), mono=True,
              tip="main thread + every subagent, last 60 min of proxy traffic")]
    if level <= 0:
        return out
    cap = _LEVELS[level]
    tel = res.get("telemetry")
    if isinstance(tel, dict):
        p50 = tel.get("p50_ttft_ms")
        out.append(_u("--" + _fit("main  " + _brief(tel)
                                  + (f" · ttft {p50 / 1000:.1f}s"
                                     if isinstance(p50, (int, float)) else "")),
                      mono=True, tip=agent_resources.tokens_text(tel)))
    subs = res.get("subagents") if isinstance(res.get("subagents"), dict) else {}
    lst = [s for s in subs.get("list") or [] if isinstance(s, dict)]
    if lst:
        n = subs.get("count", len(lst) + (subs.get("more") or 0))
        hdr = f"--Subagents 60m · {n} · {subs.get('running', 0)} running"
        if subs.get("flagged"):
            hdr += f" · {subs['flagged']} ⚠"
        out.append(hdr)
        shown, cut = lst[:cap["subs"]], lst[cap["subs"]:]
        out += [_sub_row(sa, "--") for sa in shown]
        hidden_n = len(cut) + (subs.get("more") or 0)
        if hidden_n:
            hid = agent_resources.merge_stats(subs.get("hidden"),
                                              *[s.get("telemetry") for s in cut])
            out.append("--" + _more_text(hidden_n, hid))
    tree = res.get("tree") or {}
    procs = [p for p in (tree.get("top") or [])[:cap["procs"]] if isinstance(p, dict)]
    if procs:
        fps = [p.get("footprint_mb") for p in procs]
        held = sum(fps) if all(isinstance(x, (int, float)) for x in fps) else None
        out.append(f"--Processes · {len(procs)} · {fmt_mb(held)} | size=12 tooltip=\""
                   + _tip(f"the listed child processes; the whole tree incl. the session "
                          f"process: {tree.get('procs', '?')} procs · "
                          f"{fmt_mb(tree.get('footprint_mb'))}") + '"')
        for p in procs:
            tip = [f"pid {p.get('pid')}"]
            if p.get("uptime_s") is not None:
                tip.append(f"up {fmt_age(p['uptime_s'])}")
            if isinstance(p.get("cpu_s"), (int, float)):
                tip.append(f"cpu time {fmt_age(p['cpu_s'])}")
            if isinstance(p.get("rss_mb"), (int, float)):
                tip.append(f"rss {fmt_mb(p['rss_mb'])}")
            out.append(_u(f"----{_col(esc(p.get('name', '?')), LABEL_W)}  "
                          f"{fmt_mb(p.get('footprint_mb')).rjust(6)}  "
                          f"{p.get('cpu_pct') or 0:g}%".rstrip(), mono=True, tip=" · ".join(tip)))
    if isinstance(res.get("unattributed"), dict):
        u = res["unattributed"]
        out.append(f"--process not attributed ({u.get('ambiguous', '?')} candidates share this cwd)")
    models = Counter(tot.get("models") or {})
    if models:
        out.append("--Models 60m")
        out += [_u(f"----{_col(esc(name), LABEL_W)}  {n} req", mono=True)
                for name, n in models.most_common(cap["models"])]
    return out


def _idle_row(a: dict) -> str:
    if a.get("error"):
        return _u(f"--{esc(a.get('kind', '?'))} · error · {esc(a['error'])}")
    tree = _res(a).get("tree") or {}
    mem = fmt_mb(tree.get("footprint_mb")) if tree.get("alive") else ""
    line = (f"--{_col(_agent_label(a, LABEL_W), LABEL_W)}  {_status_word(a)} {fmt_age(a.get('age_s'))}"
            f"  {mem.rjust(6)}").rstrip()
    return _u(line, mono=True, tip=_tree_tip(tree) or None)


def fmt_mb(mb) -> str:
    if not isinstance(mb, (int, float)) or not math.isfinite(mb):
        return "?"
    return f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb:.0f}MB"


def _rank(a: dict):
    return agent_resources.rank_key(a)                 # the same order the subagent scan used


def _agents_header(snap: dict, n_active: int, n_idle: int) -> str:
    s = snap.get("system") if isinstance(snap.get("system"), dict) else {}
    bits = [f"Agents · {n_active} active · {n_idle} idle"]
    if isinstance(s.get("agents_footprint_mb"), (int, float)):
        bits.append(fmt_mb(s["agents_footprint_mb"]))
    if isinstance(s.get("agents_cpu_pct"), (int, float)):
        bits.append(f"cpu {s['agents_cpu_pct']:g}%")
    tr = s.get("traffic_60m") if isinstance(s.get("traffic_60m"), dict) else None
    if tr:
        bits.append(f"{tr.get('requests', 0)} req/h")
        bits.append(f"out {agent_resources._k(tr.get('tokens_out', 0))}/h")
        share = agent_resources.cache_share(tr)
        if share is not None:
            bits.append(f"cache {agent_resources.fmt_pct(share)}")
    tip = ("memory = physical footprint of every agent process tree · cpu sampled over "
           f"{s.get('rate_window_s', '?')}s · req/out/cache = all proxy traffic, last 60 min · "
           "cache = cached / (input + cached + cache write)")
    return f"{' · '.join(bits)} | size=12 color={COLORS['gray']} tooltip=\"{_tip(tip)}\""


def _agents_body(snap: dict, budget: int) -> list:
    """Agent rows within a line budget: active sessions ranked by output tokens then cpu, at
    most ACTIVE_MAX; each gets the most detail that still fits; idle ones fold into a submenu.
    Active / idle is Claude's own status when known (``agent_resources.agent_active``)."""
    ags = [a for a in snap.get("agents") or [] if isinstance(a, dict)]
    now = _epoch_s(snap.get("ts"))
    active = sorted([a for a in ags if a.get("error") or agent_resources.agent_active(a)],
                    key=_rank)
    idlers = [a for a in ags if not a.get("error") and not agent_resources.agent_active(a)]
    shown, hidden = active[:ACTIVE_MAX], active[ACTIVE_MAX:]
    idle_lines = (1 + min(len(idlers), IDLE_SHOWN_MAX) + (len(idlers) > IDLE_SHOWN_MAX)) \
        if idlers else 0
    reserve_tail = idle_lines + (1 if hidden else 0)
    body, used = [], 0
    for i, a in enumerate(shown):
        rest = len(shown) - i - 1
        row = [_agent_row(a, now=now)]
        for level in (2, 1, 0):
            sub = _agent_submenu(a, level) if not a.get("error") else []
            if level == 0 or used + 1 + len(sub) + 2 * rest + reserve_tail <= budget:
                break
        body += row + sub
        used += 1 + len(sub)
    if hidden:
        tot = agent_resources.merge_stats(*[agent_resources.session_totals(_res(a)) for a in hidden])
        body.append(_more_text(len(hidden), tot, "more active"))
    if idlers:
        held = [(_res(a).get("tree") or {}).get("footprint_mb") for a in idlers]
        held = [m for m in held if isinstance(m, (int, float))]
        body.append(f"idle ({len(idlers)})" + (f" · {fmt_mb(sum(held))} held" if held else ""))
        body += [_idle_row(a) for a in idlers[:IDLE_SHOWN_MAX]]
        if len(idlers) > IDLE_SHOWN_MAX:
            body.append(f"--… {len(idlers) - IDLE_SHOWN_MAX} more")
    return body or ["none in the last hour"]


def _ollama_lines(s: dict, wres: dict) -> list:
    lines = []
    ot = agent_resources.metrics_text(wres.get("ollama_tree") or {})
    if ot:
        lines.append(f"ollama server · {ot}")
    ol = s.get("ollama")
    if isinstance(ol, list):
        if not ol:
            lines.append("ollama: no model loaded")
        for m in ol[:5]:
            if isinstance(m, dict):
                t = f"ollama {esc(m.get('name', '?'))} · {fmt_mb(m.get('vram_mb'))} VRAM"
                if isinstance(m.get("unloads_in_s"), (int, float)):
                    t += f" · unloads {fmt_age(m['unloads_in_s'])}"
                lines.append(_u(t))
    else:
        lines.append("ollama: unavailable")
    return lines


def _system_lines(s: dict, wres: dict | None = None) -> list:
    if s.get("error"):
        return [_u(f"unavailable · {esc(s['error'])}")]
    gpu = (f"GPU {s['gpu_util_pct']}%" if isinstance(s.get("gpu_util_pct"), int) else "GPU ?")
    if isinstance(s.get("gpu_mem_mb"), (int, float)):
        gpu += f" · {fmt_mb(s['gpu_mem_mb'])} in use"
    gpu += " (system-wide)"
    la = s.get("loadavg")
    if isinstance(la, list) and la:
        gpu += " · load " + " ".join(f"{x:.2f}" for x in la if isinstance(x, (int, float)))
    lines = [gpu] + _ollama_lines(s, wres or {})
    errs = s.get("errors")
    if isinstance(errs, dict) and errs:
        lines.append(_u("unavailable: " + esc(", ".join(sorted(str(k) for k in errs))),
                        tip=" · ".join(f"{esc(k)}: {esc(v)}" for k, v in sorted(errs.items()))))
    return lines


def _worker_line(w: dict) -> str:
    def n(k):
        return w[k] if isinstance(w.get(k), int) else "?"
    q = f"inbox {n('inbox')} · running {n('queue_running')}"
    if w.get("pid"):
        res = w.get("res") if isinstance(w.get("res"), dict) else {}
        m = agent_resources.metrics_text(res.get("tree") or {})
        return f"pid {w['pid']} · {q}" + (f" · {m}" if m else "")
    why = esc(w.get("error") or "not running")
    return f"not running ({why}) · {q}"


def _proxy_line(p: dict) -> str:
    if p.get("up"):
        bits = ["up"]
        if isinstance(p.get("port"), int):  # from the /healthz body: never interpolate raw text
            bits.append(f":{p['port']}")
        if p.get("version"):
            bits.append(f"v{esc(p['version'])}")
        return " ".join(bits)
    if p.get("skipped"):
        return "not checked (deadline)"
    return "down"


def menubar(snap: dict) -> str:
    """SwiftBar/xbar plugin output for a snapshot. The bar is ``● N`` (active agents) plus
    `` ⚠`` when any agent is stuck or erroring; the dot colour stays the pressure rule."""
    color = dot(snap)
    ags = [a for a in snap.get("agents") or [] if isinstance(a, dict)]
    flag = " ⚠" if any(agent_resources.agent_flagged(a) for a in ags) else ""
    head = [_bar(color, f"● {_active(snap)}{flag}"), "---"]
    sections: list = []

    def section(title, body, user_text=False):
        hdr = f"{title} | size=12 color={COLORS['gray']}" + (f" {UPARAMS}" if user_text else "")
        sections.append([hdr] + list(body) + ["---"])

    p = snap.get("pressure") if isinstance(snap.get("pressure"), dict) else {"error": "missing"}
    section("Pressure", [_u(x) if "families" in x or "errors 15m" in x or "UNKNOWN" in x else x
                         for x in _pressure_lines(p, snap.get("errors15m") or {})])
    m = snap.get("measure") if isinstance(snap.get("measure"), dict) else {"error": "missing"}
    section("Measure", [_u(_measure_line(m))])
    agents_at = len(sections)
    sections.append(None)                                   # filled once the budget is known
    w = snap.get("worker") if isinstance(snap.get("worker"), dict) else {}
    section(f"Worker · {esc(w.get('label', '?'))}", [_u(_worker_line(w))], user_text=True)
    wres = w.get("res") if isinstance(w.get("res"), dict) else {}
    sy = snap.get("system") if isinstance(snap.get("system"), dict) else {"error": "missing"}
    section("System", _system_lines(sy, wres))
    px = snap.get("proxy") if isinstance(snap.get("proxy"), dict) else {}
    section("Proxy", [_u(_proxy_line(px))])
    for ad in snap.get("adapters") or []:
        if not isinstance(ad, dict) or ad.get("error"):
            continue
        body = [f"{esc(r)} | {UPARAMS}" for r in ad.get("rows") or []]
        age = f"updated {fmt_age(ad.get('age_s'))} ago"
        body.append(f"stale · {age}" if ad.get("stale") else age)
        section(esc(ad.get("title", "?")), body, user_text=True)
    other = len(head) + 1 + sum(len(x) for x in sections if x)
    budget = max(30, MENU_LINES_MAX - other - 2)
    n_idle = sum(1 for a in ags if not a.get("error") and not agent_resources.agent_active(a))
    sections[agents_at] = ([_agents_header(snap, _active(snap), n_idle)]
                           + _agents_body(snap, budget) + ["---"])
    lines = head + [ln for sec in sections for ln in sec] + ["Refresh | refresh=true"]
    return "\n".join(_cap_width(ln) for ln in lines)


def visible(line: str) -> str:
    """A menu line's visible text: no SwiftBar parameters, no leading ``--`` submenu markers."""
    text = line.split(" | ", 1)[0]
    return text.lstrip("-")


def _cap_width(line: str, width: int = MENU_WIDTH) -> str:
    """Safety net: cut a line's visible text to ``width`` (rows are built to fit already)."""
    text, sep, params = line.partition(" | ")
    body = text.lstrip("-")
    if len(body) <= width:
        return line
    marks = text[:len(text) - len(body)]
    return marks + body[:width - 1] + "…" + sep + params


def menubar_error(msg: str) -> str:
    return "\n".join([_bar("gray", "●"), "---", f"snapshot error: {esc(msg)}",
                      "---", "Refresh | refresh=true"])


# ---- CLI ------------------------------------------------------------------------------------

def _emit(text: str) -> None:
    try:
        print(text)
    except (BrokenPipeError, OSError):
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="apex-router snapshot",
        description="Read-only snapshot for the menu-bar widget (pressure, agents, meters).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="print the snapshot as JSON (default)")
    g.add_argument("--menubar", action="store_true", help="print SwiftBar/xbar plugin output")
    g.add_argument("--graph", action="store_true",
                   help="print the agent call graph (session -> subagents/processes/models)")
    args = ap.parse_args(argv)
    try:
        snap = collect()
    except Exception as e:  # noqa: BLE001 — the widget must always draw something
        _emit(menubar_error(_err(e)) if args.menubar
              else f"snapshot error: {_err(e)}" if args.graph
              else json.dumps({"schema": SCHEMA, "error": _err(e)}))
        return 0
    try:
        if args.graph:
            out = agent_resources.graph_text(snap.get("graph") or {})
        else:
            out = menubar(snap) if args.menubar else json.dumps(snap, indent=2, sort_keys=True)
    except Exception as e:  # noqa: BLE001 — a bad field must not blank the widget
        out = (menubar_error(_err(e)) if args.menubar
               else f"snapshot error: {_err(e)}" if args.graph
               else json.dumps({"schema": SCHEMA, "error": _err(e)}))
    _emit(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
