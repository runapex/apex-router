"""``apex-router snapshot --detail <session id | subagent id | all> [--no-open]``.

Writes ONE self-contained HTML page to ``~/.apex-router/widget/detail-<id8>.html`` (atomic: temp
file + ``os.replace``) and runs ``open`` on it (``--no-open`` skips that). The page has no
external resources at all — no scripts, stylesheets, fonts or images from anywhere, no network:
charts are inline SVG drawn here, hover detail is the SVG ``<title>`` tooltip (no JavaScript),
light/dark follows ``prefers-color-scheme``.

Sources (read-only, like the menu): ``snapshot.collect()`` for the current state, the last 6 h of
proxy telemetry (5-min buckets, per-request context sizes, errors and their cause), the widget
history (``widget_history``: cpu / memory / disk / context per refresh) and, for one session, its
subagent sidecars over 6 h (spawn = ``meta.json`` mtime, last write = log mtime).

Every string that comes from outside (repo, descriptions, process names, model ids, error causes)
goes through ``clean_text`` and ``html.escape``. No dollar amounts. A subagent target renders its
session's page with that subagent first and anchored (``#a…``).
"""
from __future__ import annotations

import html
import math
import os
import subprocess
import time
from collections import Counter
from pathlib import Path

from . import agent_resources as ar
from . import agents as agents_mod
from . import pressure, widget_history

WINDOW_S = 6 * 3600
BUCKET_S = 300
OPEN_TIMEOUT_S = 5.0
DETAIL_DEADLINE_S = 1.5
GRAPH_SUBS_MAX = 20
GANTT_MAX = 60
SCATTER_POINTS_MAX = 3000

_run = subprocess.run            # tests fake this


def esc(s) -> str:
    return html.escape(ar.clean_text("" if s is None else s), quote=True)


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return v


def _hm(ts) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _hms(ts) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _cut(s: str, n: int) -> str:
    s = ar.clean_text(s)
    return s if len(s) <= n else s[:n - 1] + "…"


# ---- SVG charts ------------------------------------------------------------------------------

W, H = 760, 170
PL, PR, PT, PB = 58, 14, 12, 24
SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"]
OTHER = "var(--muted)"


def _nice(v: float) -> float:
    if v <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * e:
            return m * e
    return 10 * e


def _time_ticks(t0: float, t1: float) -> list:
    span = max(1.0, t1 - t0)
    for step in (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800):
        if span / step <= 7:
            break
    first = math.ceil(t0 / step) * step
    return [first + i * step for i in range(int((t1 - first) // step) + 1)]


class _Frame:
    """Coordinate frame of one chart: time on x, one linear y axis from 0 (or ``y0``)."""

    def __init__(self, t0, t1, ymax, y0=0.0, w=W, h=H):
        self.t0, self.t1 = t0, max(t1, t0 + 1)
        self.y0, self.ymax = y0, (ymax if ymax > y0 else y0 + 1)
        self.w, self.h = w, h

    def x(self, t):
        return PL + (t - self.t0) / (self.t1 - self.t0) * (self.w - PL - PR)

    def y(self, v):
        return self.h - PB - (v - self.y0) / (self.ymax - self.y0) * (self.h - PT - PB)

    def axes(self, fmt) -> list:
        out = []
        for i in range(5):
            v = self.y0 + (self.ymax - self.y0) * i / 4
            y = self.y(v)
            out.append(f'<line class="grid" x1="{PL}" x2="{self.w - PR}" y1="{y:.1f}" y2="{y:.1f}"/>')
            out.append(f'<text class="tick" x="{PL - 6}" y="{y + 4:.1f}" text-anchor="end">'
                       f'{esc(fmt(v))}</text>')
        for t in _time_ticks(self.t0, self.t1):
            x = self.x(t)
            out.append(f'<text class="tick" x="{x:.1f}" y="{self.h - 6}" text-anchor="middle">'
                       f'{_hm(t)}</text>')
        out.append(f'<line class="axis" x1="{PL}" x2="{self.w - PR}" y1="{self.y(self.y0):.1f}" '
                   f'y2="{self.y(self.y0):.1f}"/>')
        return out


def _svg(body: list, w=W, h=H, label="") -> str:
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{esc(label)}">'
            + "".join(body) + "</svg>")


def _legend(items) -> str:
    if len(items) < 2:
        return ""
    return '<div class="legend">' + "".join(
        f'<span><i style="background:{c}"></i>{esc(n)}</span>' for n, c in items) + "</div>"


def _figure(title: str, svg: str, legend: str = "", note: str = "", anchor: str = "") -> str:
    aid = f' id="{esc(anchor)}"' if anchor else ""
    n = f'<div class="note">{esc(note)}</div>' if note else ""
    return f'<figure{aid}><figcaption>{esc(title)}</figcaption>{legend}{svg}{n}</figure>'


def _empty(title: str, why: str) -> str:
    return _figure(title, f'<div class="empty">{esc(why)}</div>')


def line_chart(title: str, series: list, t0: float, t1: float, fmt, ymax=None, hlines=(),
               zero=True, note="", dots=False) -> str:
    """``series`` = [(name, colour, [(ts, value|None)])]. One y axis; gaps where a value is None.
    Each point carries a hover ``<title>``."""
    pts = [(t, v) for _, _, s in series for t, v in s if _num(v) is not None]
    if not pts:
        return _empty(title, "no samples yet")
    lo = 0.0 if zero else min(v for _, v in pts)
    hi = max([v for _, v in pts] + [v for v, _ in hlines])
    if ymax is not None:
        hi = max(hi, ymax)
    if not zero:                                   # min..max, but never a span < 5% of the max
        span = max(hi - lo, abs(hi) * 0.05, 1e-9)
        mid = (hi + lo) / 2
        lo, hi = max(0.0, mid - span * 0.6), mid + span * 0.6
    f = _Frame(t0, t1, _nice(hi) if zero else hi, y0=lo)
    body = f.axes(fmt)
    for v, label in hlines:
        y = f.y(v)
        body.append(f'<line class="limit" x1="{PL}" x2="{W - PR}" y1="{y:.1f}" y2="{y:.1f}"/>'
                    f'<text class="tick" x="{W - PR - 4}" y="{y - 4:.1f}" text-anchor="end">'
                    f'{esc(label)}</text>')
    for name, color, s in series:
        seg, segs = [], []
        for t, v in s:
            if _num(v) is None or t < t0:
                if seg:
                    segs.append(seg)
                seg = []
                continue
            seg.append((f.x(t), f.y(v), t, v))
        if seg:
            segs.append(seg)
        for sg in segs:
            if len(sg) > 1 and not dots:
                d = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in sg)
                body.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" '
                            f'stroke-linejoin="round" points="{d}"/>')
            for x, y, t, v in sg:
                r = 3 if dots or len(sg) == 1 else 2
                body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}">'
                            f'<title>{esc(name)} · {_hms(t)} · {esc(fmt(v))}</title></circle>')
    leg = _legend([(n, c) for n, c, _ in series])
    return _figure(title, _svg(body, label=title), leg, note)


def bar_chart(title: str, starts: list, stacks: list, fmt, size_s=BUCKET_S, marks=None,
              note="") -> str:
    """Stacked bars per bucket: ``stacks`` = [(name, colour, [value per bucket])]. ``marks`` =
    {bucket index: tooltip} draws a critical ▲ above the bar (errors with their cause)."""
    n = len(starts)
    if not n or not any(any(v for v in vals) for _, _, vals in stacks):
        return _empty(title, "no requests in the window")
    tot = [sum(vals[i] for _, _, vals in stacks) for i in range(n)]
    f = _Frame(starts[0], starts[-1] + size_s, _nice(max(tot) * (1.15 if marks else 1)))
    body = f.axes(fmt)
    bw = max(1.0, f.x(starts[0] + size_s) - f.x(starts[0]) - 2)
    for i, t in enumerate(starts):
        base = 0.0
        x = f.x(t) + 1
        for name, color, vals in stacks:
            v = vals[i]
            if not v:
                continue
            y1, y2 = f.y(base), f.y(base + v)
            hgt = max(1.0, y1 - y2 - (1 if base else 0))
            body.append(f'<rect x="{x:.1f}" y="{y2:.1f}" width="{bw:.1f}" height="{hgt:.1f}" '
                        f'rx="1" fill="{color}"><title>{_hm(t)}–{_hm(t + size_s)} · '
                        f'{esc(name)} {esc(fmt(v))}</title></rect>')
            base += v
        if marks and i in marks:
            y = f.y(tot[i]) - 6
            cx = x + bw / 2
            body.append(f'<path class="errmark" d="M{cx - 5:.1f},{y:.1f} L{cx + 5:.1f},{y:.1f} '
                        f'L{cx:.1f},{y - 8:.1f} Z"><title>{_hm(t)} · {esc(marks[i])}</title></path>')
    leg = _legend([(n_, c) for n_, c, _ in stacks]
                  + ([("errors (▲, hover for cause)", "var(--crit)")] if marks else []))
    return _figure(title, _svg(body, label=title), leg, note)


def mini_spark(values, color="var(--s1)", w=150, h=30, zero=True) -> str:
    """A tiny inline SVG polyline for small multiples; '' when nothing to draw."""
    vals = [_num(v) for v in values or []]
    have = [v for v in vals if v is not None]
    if not have:
        return '<span class="muted">—</span>'
    lo = 0.0 if zero else min(have)
    hi = max(have)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        if v is None:
            continue
        x = 2 + (i / max(1, n - 1)) * (w - 4)
        y = h - 2 - (v - lo) / span * (h - 4)
        pts.append(f"{x:.1f},{y:.1f}")
    if len(pts) == 1:
        x, y = pts[0].split(",")
        shape = f'<circle cx="{x}" cy="{y}" r="2" fill="{color}"/>'
    else:
        shape = (f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
                 f'points="{" ".join(pts)}"/>')
    return f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" class="spark">{shape}</svg>'


# ---- data -----------------------------------------------------------------------------------

def bucket_starts(now: float, window_s: float = WINDOW_S, size_s: float = BUCKET_S) -> list:
    n = int(window_s // size_s)
    end = math.floor(now / size_s) * size_s + size_s
    return [end - (n - i) * size_s for i in range(n)]


def bucketize(rows, starts: list, size_s: float = BUCKET_S) -> dict:
    """{"req","in","cached","write","out","err": [per bucket], "causes": {i: Counter}} for rows
    whose ts falls in the buckets."""
    n = len(starts)
    out = {k: [0] * n for k in ("req", "in", "cached", "write", "out", "err")}
    causes: dict = {}
    if not n:
        out["causes"] = causes
        return out
    t0 = starts[0]
    for r in rows:
        ts = _num(r.get("ts")) if isinstance(r, dict) else None
        if ts is None or ts < t0 or ts >= starts[-1] + size_s:
            continue
        i = int((ts - t0) // size_s)
        out["req"][i] += 1
        out["in"][i] += ar.fresh_input(r)
        out["cached"][i] += ar._cache_read(r)
        out["write"][i] += ar._cache_write(r)
        out["out"][i] += int(_num(r.get("tokens_out")) or 0)
        if r.get("is_error"):
            out["err"][i] += 1
            causes.setdefault(i, Counter())[str(r.get("error_cause") or "unlabeled")] += 1
    out["causes"] = causes
    return out


def _err_marks(b: dict) -> dict:
    return {i: f"{sum(c.values())} error(s): " + ", ".join(f"{_cut(k, 40)} ×{v}"
                                                           for k, v in c.most_common(4))
            for i, c in b["causes"].items()}


def _tel_rows(path, since):
    try:
        return list(pressure.tail_rows(Path(path) if path else pressure.default_telemetry_path(),
                                       since))
    except Exception:  # noqa: BLE001 — no telemetry: charts say so
        return []


def find_session_of_agent(aid: str, snap: dict, rows: list, home: Path):
    for a in snap.get("agents") or []:
        subs = ((a.get("res") or {}).get("subagents") or {}) if isinstance(a, dict) else {}
        if any(isinstance(s, dict) and s.get("id") == aid for s in subs.get("list") or []):
            return a.get("session_id")
    for r in rows:
        if r.get("agent_id") == aid and isinstance(r.get("session_id"), str):
            return r["session_id"]
    try:
        for p in (Path(home) / ".claude" / "projects").glob(f"*/*/subagents/agent-{aid}.jsonl"):
            return p.parent.parent.name
    except OSError:
        pass
    return None


def _find_agent(snap: dict, target: str):
    for a in snap.get("agents") or []:
        if isinstance(a, dict) and isinstance(a.get("session_id"), str) and (
                a["session_id"] == target or a["session_id"].startswith(target)):
            return a
    return None


# ---- page pieces ----------------------------------------------------------------------------

CSS = """
:root{color-scheme:light dark;--bg:#fcfcfb;--card:#ffffff;--fg:#0b0b0b;--fg2:#52514e;
--muted:#9a9890;--grid:#e6e5e0;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;
--good:#0ca30c;--warn:#fab219;--crit:#d03b3b;--done:#b5b3ab}
@media (prefers-color-scheme: dark){:root{--bg:#1a1a19;--card:#232321;--fg:#ffffff;
--fg2:#c3c2b7;--muted:#7d7b73;--grid:#383835;--s1:#3987e5;--s2:#d95926;--s3:#199e70;
--s4:#c98500;--done:#5c5b55}}
body{margin:0;padding:20px 28px;background:var(--bg);color:var(--fg);
font:13px/1.45 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 8px;color:var(--fg2)}
.sub{color:var(--fg2);margin-bottom:14px}.muted{color:var(--muted)}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}
.tile{background:var(--card);border:1px solid var(--grid);border-radius:8px;padding:8px 12px;
min-width:110px}.tile b{display:block;font-size:17px}.tile span{color:var(--fg2);font-size:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(520px,1fr));gap:14px}
figure{margin:0;background:var(--card);border:1px solid var(--grid);border-radius:8px;
padding:10px 12px}figcaption{font-weight:600;margin-bottom:4px}
.legend{display:flex;flex-wrap:wrap;gap:12px;color:var(--fg2);font-size:12px;margin:2px 0 4px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;
vertical-align:-1px}.note{color:var(--muted);font-size:11px;margin-top:2px}
.empty{color:var(--muted);padding:30px 0;text-align:center}
svg text{fill:var(--fg2);font-size:11px}svg .tick{fill:var(--muted);font-size:10px}
svg .grid{stroke:var(--grid);stroke-width:1}svg .axis{stroke:var(--muted);stroke-width:1}
svg .limit{stroke:var(--crit);stroke-width:1.5;stroke-dasharray:5 4}
svg .errmark{fill:var(--crit)}svg .edge{stroke:var(--muted);fill:none;opacity:.7}
svg .node{fill:var(--card);stroke:var(--fg2);stroke-width:1}
svg .node.session{stroke:var(--s1);stroke-width:2}svg .node.model{stroke:var(--s2)}
svg .node.proc{stroke:var(--s3);stroke-dasharray:3 2}svg .lbl{fill:var(--fg);font-size:11px}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--grid);
border-radius:8px;font-variant-numeric:tabular-nums}
th,td{padding:4px 8px;border-bottom:1px solid var(--grid);text-align:left;vertical-align:top}
th{color:var(--fg2);font-weight:600;font-size:12px}td.n{text-align:right}
tr:target,.focus{outline:2px solid var(--s1);outline-offset:-2px}
.st{display:inline-block;padding:0 6px;border-radius:9px;font-size:11px;color:#000}
.st.running{background:var(--good)}.st.quiet{background:var(--warn)}
.st.done{background:var(--done)}.st.flag{background:var(--crit);color:#fff}
.focus{background:var(--card);border-radius:8px;padding:10px 12px;margin:10px 0}
footer{margin-top:28px;color:var(--muted);font-size:11px}
"""


def _tile(value, label) -> str:
    return f'<div class="tile"><b>{esc(value)}</b><span>{esc(label)}</span></div>'


def _state_badge(state, flags=()) -> str:
    st = str(state or "?")
    cls = "flag" if flags else (st if st in ("running", "quiet", "done") else "done")
    text = f"⚠ {st}" if flags else st
    return f'<span class="st {cls}">{esc(text)}</span>'


def _status_badge(a: dict) -> str:
    st = ar.display_state(a)
    cls = "flag" if ar.agent_flagged(a) else ("running" if ar.agent_active(a) else "done")
    return f'<span class="st {cls}">{esc(("⚠ " if cls == "flag" else "") + st)}</span>'


def _mb_axis(v) -> str:
    return f"{v / 1024:.2f} GB" if v >= 10240 else f"{v:,.0f} MB"


def _page(title: str, body: list, now: float) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
            "style-src 'unsafe-inline'; img-src data:\">"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
            + "".join(body)
            + f"<footer>apex-router widget · generated {esc(_hms(now))} · local file, no network, "
              "no dollar amounts · hover a point or bar for its value</footer></body></html>")


def gantt(subs: list, t0: float, t1: float, focus: str | None = None) -> str:
    """Subagent timeline: spawn (meta.json mtime) -> last write / newest request."""
    items = [s for s in subs if _num(s.get("_start")) is not None][:GANTT_MAX]
    if not items:
        return _empty("Subagent timeline", "no subagent in the window")
    lw, rw, rh = 230, 200, 20
    w = W + 240
    h = PT + rh * len(items) + PB
    x0, x1 = lw, w - rw

    def x(t):
        return x0 + (min(max(t, t0), t1) - t0) / (t1 - t0) * (x1 - x0)
    body = []
    for t in _time_ticks(t0, t1):
        xx = x(t)
        body.append(f'<line class="grid" x1="{xx:.1f}" x2="{xx:.1f}" y1="{PT}" y2="{h - PB}"/>'
                    f'<text class="tick" x="{xx:.1f}" y="{h - 6}" text-anchor="middle">{_hm(t)}</text>')
    for i, s in enumerate(items):
        y = PT + i * rh
        st = s.get("state")
        color = ("var(--crit)" if s.get("flags") else {"running": "var(--good)",
                                                       "quiet": "var(--warn)"}.get(st, "var(--done)"))
        a, b = x(s["_start"]), x(max(s["_end"], s["_start"]))
        tel = s.get("telemetry") or {}
        label = _cut(f"{s.get('type') or '?'} · {s.get('description') or s.get('id')}", 36)
        info = f"{tel.get('requests', 0)} req · out {ar._k(tel.get('tokens_out', 0))}"
        c = ar.ctx_text(tel)
        if c:
            info += " · " + c
        tip = (f"{s.get('type') or '?'} · {s.get('description') or ''} · {st} · "
               f"{_hms(s['_start'])} → {_hms(s['_end'])} · {info}")
        cls = ' class="focus"' if focus and s.get("id") == focus else ""
        body.append(f'<g{cls}><text class="lbl" x="{lw - 8}" y="{y + 14}" text-anchor="end">'
                    f'{esc(label)}</text>'
                    f'<rect x="{a:.1f}" y="{y + 4}" width="{max(3.0, b - a):.1f}" height="{rh - 8}" '
                    f'rx="3" fill="{color}"><title>{esc(tip)}</title></rect>'
                    f'<text x="{x1 + 8}" y="{y + 14}">{esc(_cut(info, 34))}</text></g>')
    leg = _legend([("running", "var(--good)"), ("quiet < 5 min", "var(--warn)"),
                   ("done", "var(--done)"), ("flagged ⚠", "var(--crit)")])
    return _figure("Subagent timeline (spawn → last write)", _svg(body, w=w, h=h,
                                                                  label="subagent timeline"), leg,
                   note=f"{len(subs)} subagents in the window"
                        + (f", first {GANTT_MAX} shown" if len(subs) > GANTT_MAX else ""))


def call_graph(agent: dict, subs: list, main_models: dict, procs: list) -> str:
    """session -> main thread / subagents -> models (edge width ~ requests); session -> child
    processes (dashed, MB · %cpu)."""
    col1 = [("main", "main thread", main_models, sum(main_models.values()), "")]
    shown = subs[:GRAPH_SUBS_MAX]
    for s in shown:
        tel = s.get("telemetry") or {}
        col1.append((f"sub:{s['id']}", _cut(f"{s.get('type') or '?'} · {s.get('description') or s['id']}", 30),
                     tel.get("models") or {}, tel.get("requests", 0), s.get("state") or ""))
    rest = subs[GRAPH_SUBS_MAX:]
    if rest:
        m = ar.merge_stats(*[s.get("telemetry") for s in rest])
        col1.append(("more", f"{len(rest)} more subagents", m.get("models") or {},
                     m.get("requests", 0), ""))
    pcol = [(f"p:{p.get('pid')}", _cut(p.get("name") or "?", 22),
             f"{ar.fmt_mem(p.get('footprint_mb'))} · {p.get('cpu_pct') or 0:g}%")
            for p in procs]
    models = Counter()
    for _, _, mm, _, _ in col1:
        models.update(mm or {})
    mlist = [m for m, _ in models.most_common(12)]
    rows = max(len(col1) + len(pcol), len(mlist), 1)
    rh, nw, nh = 30, 210, 22
    w = W + 240
    h = 20 + rows * rh
    xs, x1, x2 = 16, 340, w - nw - 16
    sy = h / 2 - nh / 2
    body = []
    pos1 = {k: 14 + i * rh for i, (k, *_r) in enumerate(col1)}
    posp = {k: 14 + (len(col1) + i) * rh for i, (k, *_r) in enumerate(pcol)}
    posm = {m: 14 + i * rh * max(1, rows // max(1, len(mlist))) for i, m in enumerate(mlist)}
    maxreq = max([n for k, _, mm, _, _ in col1 for n in (mm or {}).values()] + [1])

    def curve(xa, ya, xb, yb, wdt=1.5, dash=""):
        mx = (xa + xb) / 2
        d = f' stroke-dasharray="{dash}"' if dash else ""
        return (f'<path class="edge" stroke-width="{wdt:.1f}"{d} '
                f'd="M{xa:.1f},{ya:.1f} C{mx:.1f},{ya:.1f} {mx:.1f},{yb:.1f} {xb:.1f},{yb:.1f}"/>')
    for k, label, mm, req, st in col1:
        y = pos1[k] + nh / 2
        body.append(curve(xs + nw, sy + nh / 2, x1, y))
        for m, n in (mm or {}).items():
            if m in posm:
                wdt = 1 + 7 * math.sqrt(n / maxreq)
                body.append(curve(x1 + nw, y, x2, posm[m] + nh / 2, wdt)[:-2]
                            + f'><title>{esc(label)} → {esc(m)} · {n} requests</title></path>')
    for k, label, info in pcol:
        body.append(curve(xs + nw, sy + nh / 2, x1, posp[k] + nh / 2, 1.2, "4 3"))
    sess = _cut(f"{agent.get('repo') or agent.get('kind') or '?'} {agent.get('session') or ''}", 30)
    body.append(f'<rect class="node session" x="{xs}" y="{sy:.1f}" width="{nw}" height="{nh}" '
                f'rx="5"/><text class="lbl" x="{xs + 8}" y="{sy + 15:.1f}">{esc(sess)}</text>')
    for k, label, mm, req, st in col1:
        y = pos1[k]
        body.append(f'<rect class="node" x="{x1}" y="{y}" width="{nw}" height="{nh}" rx="5">'
                    f'<title>{esc(label)} · {req} requests{(" · " + esc(st)) if st else ""}</title>'
                    f'</rect><text class="lbl" x="{x1 + 8}" y="{y + 15}">{esc(label)}</text>'
                    f'<text class="tick" x="{x1 + nw - 6}" y="{y + 15}" text-anchor="end">{req}</text>')
    for k, label, info in pcol:
        y = posp[k]
        body.append(f'<rect class="node proc" x="{x1}" y="{y}" width="{nw}" height="{nh}" rx="5"/>'
                    f'<text class="lbl" x="{x1 + 8}" y="{y + 15}">{esc(label)}</text>'
                    f'<text class="tick" x="{x1 + nw - 6}" y="{y + 15}" text-anchor="end">'
                    f'{esc(info)}</text>')
    for m in mlist:
        y = posm[m]
        body.append(f'<rect class="node model" x="{x2}" y="{y}" width="{nw}" height="{nh}" rx="5">'
                    f'<title>{esc(m)} · {models[m]} requests</title></rect>'
                    f'<text class="lbl" x="{x2 + 8}" y="{y + 15}">{esc(_cut(m, 26))}</text>'
                    f'<text class="tick" x="{x2 + nw - 6}" y="{y + 15}" text-anchor="end">'
                    f'{models[m]}</text>')
    leg = _legend([("session", "var(--s1)"), ("thread / subagent", "var(--fg2)"),
                   ("process (dashed)", "var(--s3)"), ("model", "var(--s2)")])
    return _figure("Call graph (6 h): session → threads → models; session → processes",
                   _svg(body, w=w, h=h, label="call graph"), leg,
                   note="edge width ~ requests; numbers = requests")


def _table(head: list, rows: list, row_ids=None) -> str:
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    out = []
    for i, r in enumerate(rows):
        rid = f' id="{esc(row_ids[i])}"' if row_ids and row_ids[i] else ""
        out.append(f"<tr{rid}>" + "".join(
            f'<td class="n">{esc(c)}</td>' if isinstance(c, (int, float)) and not isinstance(c, bool)
            else (f"<td>{c.html}</td>" if isinstance(c, _Raw) else f"<td>{esc(c)}</td>")
            for c in r) + "</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(out)}</tbody></table>"


class _Raw:
    """Pre-escaped HTML for a table cell (our own markup only)."""

    def __init__(self, s):
        self.html = s


def _fmt_ts(t):
    return _hms(t) if _num(t) is not None else "—"


# ---- session page ---------------------------------------------------------------------------

def session_page(sid: str, snap: dict, rows: list, hist: list, home: Path, now: float,
                 focus: str | None = None) -> str:
    agent = _find_agent(snap, sid) or {"session_id": sid, "session": agents_mod.short_id(sid),
                                       "kind": "?", "repo": None}
    sid = agent.get("session_id") or sid
    res = agent.get("res") if isinstance(agent.get("res"), dict) else {}
    tree = res.get("tree") if isinstance(res.get("tree"), dict) else {}
    srows = [r for r in rows if r.get("session_id") == sid]
    split = ar.telemetry_split(srows, now).get(sid) or {"main": ar._stats([], now), "subagents": {}}
    main = split["main"]
    try:
        subs = ar.subagents(home, sid, now, split["subagents"], keep=500, scan=True,
                            window_s=WINDOW_S, times=True)
    except Exception:  # noqa: BLE001
        subs = {"list": [], "count": 0}
    slist = [s for s in subs.get("list") or [] if isinstance(s, dict)]
    by_agent: dict = {}
    for r in srows:
        if r.get("agent_id"):
            by_agent.setdefault(r["agent_id"], []).append(r)
    for s in slist:
        ts = [t for t in (_num(r.get("ts")) for r in by_agent.get(s["id"], [])) if t is not None]
        start = _num(s.get("spawn_ts")) or (min(ts) if ts else None)
        end = max([x for x in (_num(s.get("mtime")), max(ts) if ts else None) if x is not None]
                  or [start or now])
        s["_start"], s["_end"] = start, end
    slist.sort(key=lambda s: (s.get("_start") or now))
    if focus:
        slist.sort(key=lambda s: s.get("id") != focus)
    tot = ar.merge_stats(main, *[s.get("telemetry") for s in slist])
    t0 = now - WINDOW_S
    title = f"{agent.get('repo') or agent.get('kind')} · {agent.get('session')}"
    body = [f"<h1>{esc(title)}</h1>",
            f'<div class="sub">session <code>{esc(sid)}</code> · {esc(agent.get("kind"))} · '
            f'status {esc(ar.display_state(agent))}'
            + (f" · pid {esc(tree.get('pid'))}" if tree.get("alive") else " · process not seen")
            + (f" · up {esc(ar.fmt_dur(tree.get('uptime_s')))}" if tree.get("uptime_s") else "")
            + f" · models {esc(', '.join(list(tot.get('models') or {})[:4]) or '—')}</div>"]
    io = ar.io_rate(tree) if tree.get("alive") else None
    share = ar.cache_share(tot)
    ctx = ar.ctx_text(main) or "—"
    body.append('<div class="tiles">' + "".join([
        _tile(ar.fmt_mem(tree.get("footprint_mb")) if tree.get("alive") else "—", "memory (footprint)"),
        _tile(f"{tree.get('cpu_pct') or 0:g}%" if tree.get("alive") else "—", "cpu now"),
        _tile(ar.fmt_rate(io) if io is not None else "—", "disk read+write"),
        _tile(ctx.replace("ctx ", ""), "context (latest main request)"),
        _tile(ar.fmt_pct(share) if share is not None else "—", "cache share 6 h"),
        _tile(f"{tot.get('requests', 0)}", "requests 6 h"),
        _tile(ar._k(tot.get("tokens_in", 0)), "input (uncached) 6 h"),
        _tile(ar._k(tot.get("cache_read", 0)), "cached read 6 h"),
        _tile(ar._k(tot.get("tokens_out", 0)), "output 6 h"),
        _tile(f"{len(slist)}", "subagents 6 h"),
    ]) + "</div>")
    if focus:
        fs = next((s for s in slist if s.get("id") == focus), None)
        if fs:
            tel = fs.get("telemetry") or {}
            body.append(f'<div class="focus"><b>Subagent {esc(focus)}</b> · '
                        f'{_state_badge(fs.get("state"), fs.get("flags"))} · '
                        f'{esc(fs.get("type"))} · {esc(fs.get("description"))}<br>'
                        f'{esc(ar.tel_text(tel))} · {esc(ar.lifecycle_text(fs))} · spawned '
                        f'{esc(_fmt_ts(fs.get("_start")))}</div>')
    # history time series
    hser = {k: widget_history.agent_series(hist, sid, k)
            for k in ("cpu_pct", "footprint_mb", "io_mbs", "ctx")}
    ht = [t for v in hser.values() for t, _ in v]
    h0 = max(t0, min(ht) - 60) if ht else t0
    hnote = (f"{len(hser['cpu_pct'])} widget refreshes since {_hm(h0)}" if ht
             else "history fills as the menu refreshes (once a minute)")
    body.append("<h2>Process load (widget history)</h2><div class=\"grid2\">")
    body.append(line_chart("CPU %", [("cpu", SERIES[0], hser["cpu_pct"])], h0, now,
                           lambda v: f"{v:g}%", note=hnote))
    body.append(line_chart("Memory (footprint)", [("memory", SERIES[0], hser["footprint_mb"])],
                           h0, now, _mb_axis, zero=False, note=hnote))
    body.append(line_chart("Disk read+write", [("disk", SERIES[0], hser["io_mbs"])], h0, now,
                           lambda v: f"{v:.2f} MB/s", note=hnote))
    body.append(line_chart("Context tokens (main thread)", [("context", SERIES[0], hser["ctx"])],
                           h0, now, ar._kc, note=hnote))
    body.append("</div>")
    # telemetry buckets
    starts = bucket_starts(now)
    b = bucketize(srows, starts)
    body.append("<h2>Traffic (proxy telemetry, last 6 h, 5-min buckets)</h2><div class=\"grid2\">")
    body.append(bar_chart("Requests per 5 min", starts, [("requests", SERIES[0], b["req"])],
                          lambda v: f"{v:g}", marks=_err_marks(b)))
    body.append(bar_chart("Tokens per 5 min (stacked)", starts,
                          [("input (uncached)", SERIES[0], b["in"]),
                           ("cached read", SERIES[1], b["cached"]),
                           ("cache write", SERIES[2], b["write"]),
                           ("output", SERIES[3], b["out"])], ar._k,
                          note="cached read dominates; output has its own chart"))
    body.append(bar_chart("Output tokens per 5 min", starts, [("output", SERIES[3], b["out"])],
                          ar._k))
    body.append(context_growth(srows, slist, main, t0, now))
    body.append("</div>")
    body.append("<h2>Subagents</h2>")
    body.append(gantt(slist, t0, now, focus))
    procs = [p for p in tree.get("top") or [] if isinstance(p, dict)]
    body.append("<h2>Call graph</h2>")
    body.append(call_graph(agent, slist, main.get("models") or {}, procs))
    body.append("<h2>Subagents table</h2>")
    if slist:
        rows_t, ids = [], []
        for s in slist:
            tel = s.get("telemetry") or {}
            sh = ar.cache_share(tel)
            rows_t.append([_Raw(_state_badge(s.get("state"), s.get("flags"))), s.get("type") or "?",
                           _cut(s.get("description") or "", 80), s["id"],
                           _fmt_ts(s.get("_start")), ar.fmt_dur(s.get("run_s")),
                           ar.fmt_dur(s.get("last_s")), tel.get("requests", 0),
                           ar._k(tel.get("tokens_in", 0)), ar._k(tel.get("cache_read", 0)),
                           ar._k(tel.get("cache_write", 0)), ar._k(tel.get("tokens_out", 0)),
                           ar.fmt_pct(sh) if sh is not None else "—",
                           (ar.ctx_text(tel) or "—").replace("ctx ", ""),
                           ar.err_text(tel) or "—"])
            ids.append(s["id"])                       # the #a… anchor of a subagent click
        body.append(_table(["state", "type", "description", "id", "spawned", "run", "last",
                            "req", "input", "cached", "write", "out", "cache", "ctx", "errors"],
                           rows_t, ids))
    else:
        body.append('<div class="empty">no subagents in the last 6 h</div>')
    body.append("<h2>Processes</h2>")
    if procs:
        body.append(_table(["pid", "name", "memory", "cpu %", "rss", "up", "cpu time"],
                           [[p.get("pid"), _cut(p.get("name") or "?", 40),
                             ar.fmt_mem(p.get("footprint_mb")), f"{p.get('cpu_pct') or 0:g}%",
                             ar.fmt_mem(p.get("rss_mb")), ar.fmt_dur(p.get("uptime_s")),
                             ar.fmt_dur(p.get("cpu_s"))] for p in procs]))
    else:
        body.append('<div class="empty">no child processes seen</div>')
    body.append("<h2>Models (6 h)</h2>")
    mrows = []
    for m, n in Counter(tot.get("models") or {}).most_common():
        mr = [r for r in srows if str(r.get("model_requested") or "?") == m]
        win = ar.context_window(m)
        mrows.append([_cut(m, 50), n, ar._k(sum(int(_num(r.get("tokens_out")) or 0) for r in mr)),
                      ar._kc(win) if win else "unknown",
                      sum(1 for r in mr if r.get("is_error"))])
    body.append(_table(["model", "requests", "output", "window", "errors"], mrows) if mrows
                else '<div class="empty">no proxy traffic in the last 6 h</div>')
    return _page(f"apex-router · {title}", body, now)


def context_growth(srows: list, slist: list, main: dict, t0: float, now: float) -> str:
    """Context size per request: main thread, the two busiest subagents, the rest together;
    a dashed line at the known window (1M / 200k)."""
    by: dict = {}
    for r in srows:
        n = ar.context_of(r)
        ts = _num(r.get("ts"))
        if n > 0 and ts is not None and ts >= t0:
            by.setdefault(r.get("agent_id") or None, []).append((ts, n))
    if not by:
        return _empty("Context size per request", "no request carried a prompt")
    names = {s["id"]: _cut(f"{s.get('type') or '?'} · {s.get('description') or s['id']}", 30)
             for s in slist}
    subs = sorted((k for k in by if k), key=lambda k: -len(by[k]))
    series = []
    if None in by:
        series.append(("main thread", SERIES[0], sorted(by[None])))
    for k, c in zip(subs[:2], SERIES[1:3]):
        series.append((names.get(k, k), c, sorted(by[k])))
    other = sorted(p for k in subs[2:] for p in by[k])
    if other:
        series.append((f"{len(subs) - 2} other subagents", OTHER, other))
    total = sum(len(s) for _, _, s in series)
    if total > SCATTER_POINTS_MAX:                    # keep the page small: thin evenly
        series = [(n, c, s[::math.ceil(total / SCATTER_POINTS_MAX)]) for n, c, s in series]
    wins = sorted({w for w in (ar.context_window(r.get("model_requested")) for r in srows) if w})
    win = main.get("ctx_window") or (wins[-1] if wins else None)
    hl = [(win, f"window {ar._kc(win)}")] if win else []
    return line_chart("Context size per request", series, t0, now, ar._kc, hlines=hl, dots=True,
                      note="prompt = uncached input + cache read + cache write; one dot per request")


# ---- overview page --------------------------------------------------------------------------

def overview_page(snap: dict, rows: list, hist: list, now: float) -> str:
    t0 = now - WINDOW_S
    ags = [a for a in snap.get("agents") or [] if isinstance(a, dict) and not a.get("error")]
    ags.sort(key=lambda a: (not ar.agent_active(a), ar.rank_key(a)))
    s = snap.get("system") if isinstance(snap.get("system"), dict) else {}
    body = ["<h1>apex-router · agents overview</h1>",
            (f'<div class="sub">{len(ags)} sessions seen in the last hour · '
             f'{sum(1 for a in ags if ar.agent_active(a))} active · GPU figures are system-wide '
             "(macOS reports per-process GPU only to root)</div>")]
    tr = s.get("traffic_60m") if isinstance(s.get("traffic_60m"), dict) else {}
    share = ar.cache_share(tr) if tr else None
    body.append('<div class="tiles">' + "".join([
        _tile(ar.fmt_mem(s.get("agents_footprint_mb")), "agents memory"),
        _tile(f"{s.get('agents_cpu_pct'):g}%" if _num(s.get("agents_cpu_pct")) is not None else "—",
              "agents cpu now"),
        _tile(f"{s['gpu_util_pct']}%" if _num(s.get("gpu_util_pct")) is not None else "—",
              "GPU (system)"),
        _tile(ar.fmt_mem(s.get("gpu_mem_mb")), "GPU memory in use"),
        _tile(" ".join(f"{x:.2f}" for x in s.get("loadavg") or []) or "—", "load 1/5/15"),
        _tile(str(tr.get("requests", 0)), "requests 60 min"),
        _tile(ar._k(tr.get("tokens_out", 0)), "output 60 min"),
        _tile(ar.fmt_pct(share) if share is not None else "—", "cache share 60 min"),
    ]) + "</div>")
    body.append("<h2>Sessions (small multiples: last 6 h)</h2>")
    starts = bucket_starts(now)
    by_sid: dict = {}
    for r in rows:
        if isinstance(r.get("session_id"), str):
            by_sid.setdefault(r["session_id"], []).append(r)
    trs = []
    for a in ags:
        sid = a.get("session_id") or ""
        res = a.get("res") if isinstance(a.get("res"), dict) else {}
        tree = res.get("tree") if isinstance(res.get("tree"), dict) else {}
        tot = ar.session_totals(res)
        b = bucketize(by_sid.get(sid, []), starts)
        cpu = [v for _, v in widget_history.agent_series(hist, sid, "cpu_pct")]
        mem = [v for _, v in widget_history.agent_series(hist, sid, "footprint_mb")]
        trs.append([f"{_cut(a.get('repo') or a.get('kind') or '?', 40)}",
                    a.get("session") or "", _Raw(_status_badge(a)),
                    ar.fmt_mem(tree.get("footprint_mb")) if tree.get("alive") else "—",
                    _Raw(mini_spark(cpu)), _Raw(mini_spark(mem, "var(--s3)", zero=False)),
                    _Raw(mini_spark(b["req"], "var(--s2)")), tot.get("requests", 0),
                    ar._k(tot.get("tokens_out", 0)), (ar.ctx_text(res.get("telemetry")) or "—")
                    .replace("ctx ", "")])
    body.append(_table(["repo", "session", "status", "memory", "cpu (history)",
                        "memory (history)", "requests / 5 min (6 h)", "req 60m", "out 60m", "ctx"],
                       trs) if trs else '<div class="empty">no sessions in the last hour</div>')
    body.append("<h2>System (widget history)</h2><div class=\"grid2\">")
    gt = [t for t, _ in widget_history.system_series(hist, "gpu_util_pct")]
    h0 = max(t0, min(gt) - 60) if gt else t0
    body.append(line_chart("GPU utilisation % (system-wide)",
                           [("gpu", SERIES[0], widget_history.system_series(hist, "gpu_util_pct"))],
                           h0, now, lambda v: f"{v:g}%"))
    body.append(line_chart("GPU memory in use (system-wide)",
                           [("gpu memory", SERIES[0],
                             widget_history.system_series(hist, "gpu_mem_mb"))],
                           h0, now, _mb_axis, zero=False))
    body.append(line_chart("Load average (1 min)",
                           [("load", SERIES[0], widget_history.system_series(hist, "load1"))],
                           h0, now, lambda v: f"{v:.2f}"))
    b = bucketize(rows, starts)
    body.append(bar_chart("All proxy requests per 5 min (6 h)", starts,
                          [("requests", SERIES[0], b["req"])], lambda v: f"{v:g}",
                          marks=_err_marks(b)))
    body.append("</div>")
    body.append("<h2>ollama</h2>")
    ol = s.get("ollama")
    if isinstance(ol, list) and ol:
        body.append(_table(["model", "VRAM", "size", "unloads in"],
                           [[_cut(m.get("name") or "?", 50), ar.fmt_mem(m.get("vram_mb")),
                             ar.fmt_mem(m.get("size_mb")), ar.fmt_dur(m.get("unloads_in_s"))]
                            for m in ol if isinstance(m, dict)]))
    else:
        body.append('<div class="empty">'
                    + ("no model loaded" if isinstance(ol, list) else "ollama unavailable")
                    + "</div>")
    return _page("apex-router · agents overview", body, now)


# ---- build / open / main --------------------------------------------------------------------

def page_path(target_key: str) -> Path:
    return widget_history.widget_dir() / f"detail-{target_key}.html"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                       encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def build(target: str, *, now: float | None = None, home=None, telemetry=None,
          history_path=None, collect_fn=None, snap: dict | None = None):
    """Write the page for ``target``; returns ``(path, anchor or None)``. Raises ValueError for
    an invalid target."""
    from . import snapshot
    if not snapshot.valid_target(target):
        raise ValueError("target must be 'all', a session id or a subagent id")
    now = time.time() if now is None else now
    home = Path(home).expanduser() if home else Path.home()
    if snap is None:
        fn = collect_fn or (lambda: snapshot.collect(
            home=home, telemetry=telemetry, now=now,
            deadline=ar.Deadline(DETAIL_DEADLINE_S)))
        snap = fn()
    rows = _tel_rows(telemetry, now - WINDOW_S)
    hist = widget_history.load(now - WINDOW_S, path=history_path)
    if target == "all":
        path = page_path("all")
        write_atomic(path, overview_page(snap, rows, hist, now))
        return path, None
    focus = None
    sid = target
    if snapshot.AGENT_ID_RE.match(target):
        focus = target
        sid = find_session_of_agent(target, snap, rows, home)
        if not sid:
            raise ValueError("subagent not found in the last 6 h")
    agent = _find_agent(snap, sid)
    if agent is None:                                    # a prefix of a session in telemetry?
        full = next((r["session_id"] for r in rows if isinstance(r.get("session_id"), str)
                     and r["session_id"].startswith(sid)), None)
        sid = full or sid
    else:
        sid = agent.get("session_id") or sid
    key = agents_mod.short_id(sid)
    if not snapshot.SESSION_ID_RE.match(key):
        key = "session"
    path = page_path(key)
    write_atomic(path, session_page(sid, snap, rows, hist, home, now, focus))
    return path, focus


def open_file(path: Path, anchor: str | None = None) -> bool:
    """``open <file>`` (``file://…#anchor`` for a subagent); False on any failure."""
    target = path.as_uri() + (f"#{anchor}" if anchor else "") if anchor else str(path)
    try:
        r = _run(["open", target], capture_output=True, timeout=OPEN_TIMEOUT_S, check=False)
        return getattr(r, "returncode", 1) == 0
    except Exception:  # noqa: BLE001 — fail open
        return False


def main(target: str, open_page: bool = True, emit=print) -> int:
    try:
        path, anchor = build(target)
    except ValueError as e:
        emit(f"detail: {e}")
        return 2
    except Exception as e:  # noqa: BLE001 — never a traceback from a menu click
        emit(f"detail error: {type(e).__name__}: {e}")
        return 0
    emit(str(path))
    if open_page:
        open_file(path, anchor)
    return 0
