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
             ollama models, total memory of all agent process trees; each agent gains ``res``
             (pid, memory, cpu, disk io, top child processes, subagents, proxy traffic 60 min)
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
AGENTS_IDLE_MAX = 30

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
    s = " ".join(str(s).split())
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


def collect(*, home=None, telemetry=None, observe_dir=None, adapters=None,
            now: float | None = None, proxy_fn=None, worker_fn=None, resources_fn=None) -> dict:
    """The snapshot dict. ``home`` overrides ``~`` for agent discovery; the injectable
    ``proxy_fn``/``worker_fn``/``resources_fn`` exist for tests. Never raises; never writes."""
    now = time.time() if now is None else now
    found = _safe(agents_mod.discover, home, now)
    found = found if isinstance(found, list) else [found]
    adapters_out = _safe(adapters_block, adapters, now)
    w = _safe(worker_fn or agents_mod.worker)
    wpid = w.get("pid") if isinstance(w, dict) else None
    res = _safe(resources_fn or agent_resources.collect, found, home=home, telemetry=telemetry,
                now=now, worker_pid=wpid if isinstance(wpid, int) else None)
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
        "proxy": _safe(proxy_fn or agents_mod.proxy),
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
    newlines, no leading '-' (submenu / separator markers), at most 120 chars."""
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
    return sum(1 for a in snap.get("agents") or []
               if isinstance(a, dict) and a.get("state") == "active")


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
    if m.get("cost_usd") is not None:
        c = m["cost_usd"]
        parts.append("<$0.01" if 0 < c < 0.01 else f"${c:.2f}")
    parts.append(f"age {fmt_age(m.get('age_s'))}")
    return " · ".join(parts)


def _agent_line(a: dict) -> str:
    if a.get("error"):
        return f"{esc(a.get('kind', '?'))} · error · {esc(a['error'])}"
    parts = [esc(a.get("kind", "?"))]
    if a.get("repo"):
        parts.append(esc(a["repo"]))
    parts += [esc(a.get("state", "?")), fmt_age(a.get("age_s")), esc(a.get("session", ""))]
    if a.get("subagents"):
        parts.append(f"{a['subagents']} subagents")
    res = a.get("res") if isinstance(a.get("res"), dict) else {}
    if res.get("status") and res["status"] != a.get("state"):  # Claude's own busy/idle
        parts.append(esc(res["status"]))
    m = agent_resources.metrics_text(res.get("tree") or {})
    if m:
        parts.append(m)
    return " · ".join(parts)


def fmt_mb(mb) -> str:
    if not isinstance(mb, (int, float)) or not math.isfinite(mb):
        return "?"
    return f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb:.0f}MB"


def _agent_submenu(a: dict) -> list:
    """SwiftBar ``--`` lines under an active agent: main-thread traffic, subagents, child
    processes, models called. Every user-derived string goes through esc()."""
    res = a.get("res") if isinstance(a.get("res"), dict) else {}
    out = []
    tel = res.get("telemetry")
    if isinstance(tel, dict):
        p50 = tel.get("p50_ttft_ms")
        out.append("--main thread 60m · " + agent_resources.tel_text(tel)
                   + (f" · p50 ttft {p50 / 1000:.1f}s" if isinstance(p50, (int, float)) else ""))
    subs = res.get("subagents") if isinstance(res.get("subagents"), dict) else {}
    lst = [s for s in subs.get("list") or [] if isinstance(s, dict)]
    models: Counter = Counter((tel or {}).get("models") or {})
    if lst:
        more = subs.get("more") or 0
        out.append(f"--subagents ({len(lst)}{f' +{more}' if more else ''})")
        for sa in lst:
            st = sa.get("telemetry") if isinstance(sa.get("telemetry"), dict) else {}
            models.update(st.get("models") or {})
            desc = str(sa.get("description") or "")[:40]
            out.append("--" + esc(f"{sa.get('type', '?')} · {desc} · "
                                  f"{agent_resources.tel_text(st)} · {sa.get('state', '?')}"))
    procs = [p for p in ((res.get("tree") or {}).get("top") or [])[:agent_resources.PROCS_TOP]
             if isinstance(p, dict)]
    if procs:
        out.append("--processes")
        out += ["--" + esc(f"{p.get('name', '?')} · {fmt_mb(p.get('rss_mb'))} · "
                           f"{p.get('cpu_pct', 0):g}% · pid {p.get('pid')}") for p in procs]
    if isinstance(res.get("unattributed"), dict):
        u = res["unattributed"]
        out.append(f"--process not attributed ({u.get('ambiguous', '?')} candidates share this cwd)")
    if models:
        out.append("--models 60m")
        out += ["--" + esc(f"{name} · {n} req") for name, n in models.most_common(
            agent_resources.GRAPH_MODELS_MAX)]
    return out


def _system_lines(s: dict) -> list:
    if s.get("error"):
        return [f"unavailable · {esc(s['error'])}"]
    gpu = (f"GPU {s['gpu_util_pct']}% (system-wide)" if isinstance(s.get("gpu_util_pct"), int)
           else "GPU ? (system-wide)")
    if isinstance(s.get("gpu_mem_mb"), (int, float)):
        gpu += f" · {fmt_mb(s['gpu_mem_mb'])} in use"
    lines = [gpu]
    la = s.get("loadavg")
    if isinstance(la, list) and la:
        lines.append("load " + " ".join(f"{x:.2f}" for x in la if isinstance(x, (int, float))))
    if isinstance(s.get("agents_rss_mb"), (int, float)):
        lines.append(f"agents {fmt_mb(s['agents_rss_mb'])} in {s.get('agents_procs', '?')} processes")
    ol = s.get("ollama")
    if isinstance(ol, list):
        if not ol:
            lines.append("ollama: no model loaded")
        for m in ol[:5]:
            if isinstance(m, dict):
                lines.append("ollama: " + esc(f"{m.get('name', '?')} · {fmt_mb(m.get('vram_mb'))} VRAM"))
    else:
        lines.append("ollama: unavailable")
    errs = s.get("errors")
    if isinstance(errs, dict) and errs:
        lines.append("unavailable: " + esc(", ".join(sorted(str(k) for k in errs))))
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
    return "down"


def menubar(snap: dict) -> str:
    """SwiftBar/xbar plugin output for a snapshot."""
    color = dot(snap)
    lines = [_bar(color, f"● {_active(snap)}"), "---"]

    def section(title, body):
        lines.append(f"{title} | size=12 color={COLORS['gray']}")
        lines.extend(body)
        lines.append("---")

    p = snap.get("pressure") if isinstance(snap.get("pressure"), dict) else {"error": "missing"}
    section("Pressure", _pressure_lines(p, snap.get("errors15m") or {}))
    m = snap.get("measure") if isinstance(snap.get("measure"), dict) else {"error": "missing"}
    section("Measure", [_measure_line(m)])
    ags = [a for a in snap.get("agents") or [] if isinstance(a, dict)]
    idle = sum(1 for a in ags if a.get("state") == "idle")
    body = []
    for a in ags:
        if a.get("state") != "idle":
            body.append(_agent_line(a))
            body += _agent_submenu(a)
    idlers = [a for a in ags if a.get("state") == "idle"]
    if idlers:                                          # idle sessions fold into a submenu
        body.append(f"idle ({len(idlers)})")
        body += ["--" + _agent_line(a) for a in idlers[:AGENTS_IDLE_MAX]]
        if len(idlers) > AGENTS_IDLE_MAX:
            body.append(f"--… {len(idlers) - AGENTS_IDLE_MAX} more")
    section(f"Agents ({_active(snap)} active, {idle} idle)", body or ["none in the last hour"])
    w = snap.get("worker") if isinstance(snap.get("worker"), dict) else {}
    wbody = [_worker_line(w)]
    wres = w.get("res") if isinstance(w.get("res"), dict) else {}
    ot = agent_resources.metrics_text(wres.get("ollama_tree") or {})
    if ot:
        wbody.append(f"ollama server · {ot}")
    for mdl in (wres.get("ollama_models") or [])[:5]:
        if isinstance(mdl, dict):
            wbody.append("ollama · " + esc(f"{mdl.get('name', '?')} · {fmt_mb(mdl.get('vram_mb'))} VRAM"))
    section(f"Worker · {esc(w.get('label', '?'))}", wbody)
    sy = snap.get("system") if isinstance(snap.get("system"), dict) else {"error": "missing"}
    section("System", _system_lines(sy))
    px = snap.get("proxy") if isinstance(snap.get("proxy"), dict) else {}
    section("Proxy", [_proxy_line(px)])
    for ad in snap.get("adapters") or []:
        if not isinstance(ad, dict) or ad.get("error"):
            continue
        body = [f"{esc(r)} | emojize=false" for r in ad.get("rows") or []]
        age = f"updated {fmt_age(ad.get('age_s'))} ago"
        body.append(f"stale · {age}" if ad.get("stale") else age)
        section(esc(ad.get("title", "?")), body)
    lines.append("Refresh | refresh=true")
    return "\n".join(lines)


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
