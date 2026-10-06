#!/usr/bin/env python3
"""telemetry_dashboard — one self-contained HTML page of the proxy's current stats and their
dynamics: mean/median AND the tails (p95, p99) kept apart, per client, per day.

    python3 scripts/telemetry_dashboard.py                      # -> ~/.apex-router/reports/telemetry-dashboard.html
    python3 scripts/telemetry_dashboard.py --days 7 --open
    python3 scripts/telemetry_dashboard.py --telemetry a.jsonl --telemetry b.jsonl -o out.html

Reads the proxy telemetry (live file + its rotation `.1` by default) and xval_runs.jsonl. Needs
matplotlib + numpy (not apex-router deps; system python usually has them). Read-only, offline;
the HTML embeds PNGs, so it opens anywhere and holds no prompt text or ids.

Conventions: percentiles are nearest-rank (apex_router.core.stats.nearest_rank). A day's p95 is
drawn hollow when it rests on < 20 calls, a p99 when < 100 — at that n it is one or two calls,
not a tail. Latency/token/cost stats use successful calls only (an errored call's ttft is the
failure's timing); error panels use every call. Prompt tokens are normalised across wires:
OpenAI input_tokens already include cached tokens; Anthropic's do not (+cache read +cache write).
Cost units = uncached input + 0.1·cache read + 1.25·cache write + 4·output (input-token
equivalents, no prices) — relative cost, not dollars.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import io
import json
import math
import os
import random
import sys
import webbrowser
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from apex_router import zeno  # noqa: E402
from apex_router.core.stats import nearest_rank, wilson_ci  # noqa: E402
from apex_router.telemetry_path import telemetry_path  # noqa: E402

CLIENTS = ("codex", "claude-code")
COLOR = {"codex": "#1f77b4", "claude-code": "#d62728", "all": "#444444"}
MIN_P95, MIN_P99 = 20, 100

plt.rcParams.update({"figure.dpi": 110, "font.size": 9, "axes.grid": True, "grid.alpha": 0.3,
                     "axes.spines.top": False, "axes.spines.right": False,
                     "legend.fontsize": 8, "legend.frameon": False})


# ---- data ------------------------------------------------------------------------------------

def load(paths, since_ts):
    rows = []
    for p in paths:
        try:
            f = open(p)
        except FileNotFoundError:
            continue
        with f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get("ev") or r.get("is_error") is None:
                    continue
                ts = r.get("ts")
                if not isinstance(ts, (int, float)) or ts < since_ts:
                    continue
                rows.append(_slim(r))
    rows.sort(key=lambda r: r["ts"])
    return rows


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0


def _slim(r):
    client = r.get("client") or "unknown"
    tin, tout = _num(r.get("tokens_in")), _num(r.get("tokens_out"))
    cr, cw = _num(r.get("cache_read_tokens")), _num(r.get("cache_write_tokens"))
    if client == "codex":          # OpenAI: input_tokens INCLUDES cached
        prompt, uncached = tin, max(0.0, tin - cr)
    else:                           # Anthropic: input_tokens excludes cache read/write
        prompt, uncached = tin + cr + cw, tin
    captured = bool(r.get("usage")) or tout > 0 or prompt > 0
    return {
        "ts": float(r["ts"]), "day": dt.date.fromtimestamp(r["ts"]), "client": client,
        "err": bool(r.get("is_error")), "cause": r.get("error_cause"),
        "stratum": r.get("stratum") or "unknown", "session": r.get("session_id"),
        "ttft": _num(r.get("ttft_ms")) or None,
        "ttfb": _num(r.get("t_upstream_ttfb_ms")) or None,
        "apex": _num(r.get("apex_added_ms")) or None,
        "out": tout if captured else None, "prompt": prompt if captured else None,
        "cached_share": (cr / prompt) if captured and prompt > 0 else None,
        "cost": (uncached + 0.1 * cr + 1.25 * cw + 4 * tout) if captured else None,
    }


def stats(v):
    v = sorted(x for x in v if x is not None)
    n = len(v)
    if not n:
        return {"n": 0}
    return {"n": n, "mean": sum(v) / n, "p50": nearest_rank(v, 0.50),
            "p95": nearest_rank(v, 0.95), "p99": nearest_rank(v, 0.99), "max": v[-1]}


def boot_ci(v, q, n_boot=300, seed=0):
    v = [x for x in v if x is not None]
    if len(v) < MIN_P95:
        return (None, None)
    rng = random.Random(seed)
    est = sorted(nearest_rank(sorted(rng.choices(v, k=len(v))), q) for _ in range(n_boot))
    return est[int(0.025 * n_boot)], est[int(0.975 * n_boot) - 1]


def by_day(rows, key, client=None, ok_only=True):
    d = defaultdict(list)
    for r in rows:
        if (client is None or r["client"] == client) and not (ok_only and r["err"]):
            if r[key] is not None:
                d[r["day"]].append(r[key])
    return {day: stats(v) for day, v in sorted(d.items())}


# ---- figure helpers --------------------------------------------------------------------------

def png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _dates(days):
    return [dt.datetime.combine(d, dt.time(12)) for d in days]


THIN_DAY = 100          # calls/day below which a day is shaded: a different regime, not a trend point
_DAY_N: dict = {}      # day -> calls, filled once in build()


def _fmt_x(ax):
    """One tick per data day at noon (where every series is placed); shade thin days."""
    days = sorted(_DAY_N)
    if days:
        ax.set_xticks(_dates(days), [f"{d:%m-%d}\n{_DAY_N[d]}" for d in days])
        for d in days:
            if _DAY_N[d] < THIN_DAY:
                x = dt.datetime.combine(d, dt.time(0))
                ax.axvspan(x, x + dt.timedelta(days=1), color="#f2c94c", alpha=0.18, lw=0)
    for lab in ax.get_xticklabels():
        lab.set_fontsize(7)


def _thin_aware(ax, xs, ys, ns, floor, color, ls, label):
    ax.plot(xs, ys, ls, color=color, lw=1.4, label=label)
    for x, y, n in zip(xs, ys, ns):
        ax.plot([x], [y], "o", ms=4.5, color=color,
                mfc=color if n >= floor else "white", mew=1.2)


def metric_figure(rows, key, title, unit, log_dist=True):
    """Three panels: center over time | tails over time | whole-window distribution."""
    fig, (a1, a2, a3) = plt.subplots(1, 3, figsize=(15, 3.6))
    for c in CLIENTS:
        days = by_day(rows, key, c)
        if not days:
            continue
        xs = _dates(days)
        s = list(days.values())
        ns = [x["n"] for x in s]
        _thin_aware(a1, xs, [x["mean"] for x in s], ns, MIN_P95, COLOR[c], "-", f"{c} mean")
        a1.plot(xs, [x["p50"] for x in s], "--", color=COLOR[c], lw=1, alpha=0.8, label=f"{c} median")
        _thin_aware(a2, xs, [x["p95"] for x in s], ns, MIN_P95, COLOR[c], "-", f"{c} p95")
        _thin_aware(a2, xs, [x["p99"] for x in s], ns, MIN_P99, COLOR[c], ":", f"{c} p99")
        v = np.sort(np.array([r[key] for r in rows if r["client"] == c and not r["err"]
                              and r[key] is not None and r[key] > 0]))
        if len(v):
            ccdf = 1.0 - np.arange(len(v)) / len(v)
            a3.plot(v, ccdf, color=COLOR[c], lw=1.4, label=c)
            for q, ls in ((0.95, "-"), (0.99, ":")):
                a3.axvline(nearest_rank(v.tolist(), q), color=COLOR[c], ls=ls, lw=0.9, alpha=0.7)
    a1.set_title(f"{title} — center (mean, median) per day")
    a2.set_title(f"{title} — tails (p95 solid, p99 dotted)")
    a3.set_title(f"{title} — whole window, P(X > x)")
    a1.set_ylabel(unit)
    a2.set_ylabel(unit)
    a2.set_yscale("log")
    a3.set_yscale("log")
    if log_dist:
        a3.set_xscale("log")
    a3.set_xlabel(unit)
    a3.axhline(0.05, color="grey", lw=0.6, ls="-")
    a3.axhline(0.01, color="grey", lw=0.6, ls=":")
    for a in (a1, a2):
        _fmt_x(a)
        a.legend(ncol=2)
    a3.legend()
    fig.tight_layout()
    return png(fig)


def reliability_figure(rows):
    fig, (a1, a2, a3) = plt.subplots(1, 3, figsize=(15, 3.6))
    for c in CLIENTS + ("all",):
        d = defaultdict(lambda: [0, 0])
        for r in rows:
            if c == "all" or r["client"] == c:
                d[r["day"]][0] += r["err"]
                d[r["day"]][1] += 1
        days = sorted(d)
        if not days:
            continue
        xs = _dates(days)
        rate = [d[x][0] / d[x][1] for x in days]
        ci = [wilson_ci(d[x][0], d[x][1]) for x in days]
        a1.plot(xs, [100 * x for x in rate], "-o", ms=3.5, color=COLOR[c], label=c,
                lw=2 if c == "all" else 1.3)
        if c != "all":
            band = [(x, lo, hi) for x, (lo, hi), day in zip(xs, ci, days) if d[day][1] >= MIN_P95]
            if band:
                a1.fill_between([b[0] for b in band], [100 * b[1] for b in band],
                                [100 * b[2] for b in band], color=COLOR[c], alpha=0.12)
    a1.set_title("error rate per day (Wilson 95% band where ≥20 calls)")
    a1.set_ylabel("% of calls failed")
    a1.legend()

    # burstiness: error rate in 10-minute windows (>= 5 calls), mean vs p95/p99 of windows per day
    win = defaultdict(lambda: [0, 0])
    for r in rows:
        k = int(r["ts"] // 600)
        win[k][0] += r["err"]
        win[k][1] += 1
    per_day = defaultdict(list)
    for k, (e, n) in win.items():
        if n >= 5:
            per_day[dt.date.fromtimestamp(k * 600)].append(e / n)
    days = sorted(per_day)
    if days:
        xs = _dates(days)
        st = [stats(per_day[x]) for x in days]
        ns = [x["n"] for x in st]
        a2.plot(xs, [100 * x["mean"] for x in st], "-o", ms=3.5, color="#444", label="mean window")
        _thin_aware(a2, xs, [100 * x["p95"] for x in st], ns, MIN_P95, "#ff7f0e", "-", "p95 window")
        _thin_aware(a2, xs, [100 * x["p99"] for x in st], ns, MIN_P99, "#9467bd", ":", "p99 window")
    a2.set_title("burstiness: error rate of 10-min windows (≥5 calls)")
    a2.set_ylabel("% failed in window")
    a2.legend()

    # causes per day, unlabeled stacked on top
    causes = Counter(r["cause"] or "no cause recorded" for r in rows if r["err"])
    order = [c for c, _ in causes.most_common() if c != "no cause recorded"] + ["no cause recorded"]
    days = sorted(_DAY_N)
    bottom = np.zeros(len(days))
    xs = _dates(days)
    palette = plt.get_cmap("tab10")
    for i, cause in enumerate(order):
        h = np.array([sum(1 for r in rows if r["err"] and r["day"] == d
                          and (r["cause"] or "no cause recorded") == cause) for d in days])
        a3.bar(xs, h, bottom=bottom, width=0.7, label=cause,
               color="#bbbbbb" if cause == "no cause recorded" else palette(i % 10),
               hatch="//" if cause == "no cause recorded" else None, edgecolor="white", lw=0.3)
        bottom += h
    a3.set_title("failed calls per day by cause")
    a3.set_ylabel("failed calls")
    a3.legend(fontsize=7)
    for a in (a1, a2, a3):
        _fmt_x(a)
    fig.tight_layout()
    return png(fig)


def tail_share_figure(rows):
    """How much of the total sits in the tail, per day: share of cost from the top 5% / 1% of
    calls, and the p99/mean ratio of each metric (a heavy tail pulls the ratio up)."""
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(15, 3.6))
    for c in CLIENTS:
        d = defaultdict(list)
        for r in rows:
            if r["client"] == c and not r["err"] and r["cost"]:
                d[r["day"]].append(r["cost"])
        days = [x for x in sorted(d) if len(d[x]) >= MIN_P95]
        xs = _dates(days)
        for frac, ls in ((0.05, "-"), (0.01, ":")):
            ys = []
            for x in days:
                v = sorted(d[x], reverse=True)
                k = max(1, math.ceil(frac * len(v)))
                ys.append(100 * sum(v[:k]) / sum(v))
            a1.plot(xs, ys, ls + "o", ms=3.5, color=COLOR[c], label=f"{c} top {int(frac * 100)}% of calls")
    a1.set_title("share of the day's cost carried by its costliest calls")
    a1.set_ylabel("% of day's cost units")
    a1.legend()
    markers = {"ttft": "o", "out": "s", "prompt": "^", "cost": "D"}
    for c in CLIENTS:
        for key, m in markers.items():
            days = by_day(rows, key, c)
            ok = [(x, s) for x, s in days.items() if s["n"] >= MIN_P99]
            if ok:
                a2.plot(_dates([x for x, _ in ok]), [s["p99"] / s["mean"] for _, s in ok],
                        "-" + m, ms=4, lw=1, color=COLOR[c], label=f"{c} {key}")
    a2.axhline(1, color="grey", lw=0.6)
    a2.set_title("tail weight: p99 / mean per day (days with ≥100 calls)")
    a2.set_ylabel("p99 ÷ mean")
    a2.legend(ncol=2, fontsize=7)
    for a in (a1, a2):
        _fmt_x(a)
    fig.tight_layout()
    return png(fig)


def sessions_figure(rows):
    sess = defaultdict(lambda: [0, 0])
    for r in rows:
        if r["session"]:
            sess[r["session"]][0] += 1
            sess[r["session"]][1] += r["err"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(15, 3.6))
    comp = zeno.compounding(sess.values())
    if comp["p"] is not None:
        p = comp["p"]
        n = np.arange(1, 1001)
        a1.plot(n, 100 * p ** n, color="#444", lw=1.5, label=f"p^n, p = {100 * p:.2f}% (session rows)")
        for pp, ls in ((0.99, "--"), (0.999, ":")):
            a1.plot(n, 100 * pp ** n, ls, color="grey", lw=1, label=f"p^n, p = {100 * pp:g}%")
        for b in comp["buckets"]:
            lo, hi = b["observed_ci"]
            mid = {"1-10": 5, "11-50": 30, "51-200": 120, "201+": 400}.get(b["calls"], 1)
            a1.errorbar([mid], [100 * b["observed_clean"]],
                        yerr=[[max(0.0, 100 * (b["observed_clean"] - lo))], [max(0.0, 100 * (hi - b["observed_clean"]))]],
                        fmt="o", color="#d62728", capsize=3)
            a1.annotate(f"{b['calls']} (n={b['sessions']})", (mid, 100 * b["observed_clean"]),
                        textcoords="offset points", xytext=(6, 4), fontsize=7)
        a1.set_xscale("log")
        a1.set_ylim(-3, 103)
        a1.set_title("horizon: P(session finishes with no failed call) — p^n vs observed (red, Wilson)")
        a1.set_xlabel("calls in the session")
        a1.set_ylabel("% clean")
        a1.legend()
    lengths = sorted(c for c, _ in sess.values())
    if lengths:
        a2.hist(lengths, bins=np.logspace(0, math.log10(max(lengths)) + 0.1, 25), color="#888")
        a2.set_xscale("log")
        st = stats(lengths)
        for q, ls in (("mean", "-"), ("p95", "--"), ("p99", ":")):
            a2.axvline(st[q], color="#d62728", ls=ls, lw=1, label=f"{q} {st[q]:.0f}")
        a2.set_title(f"calls per session ({len(lengths)} sessions with a session id)")
        a2.set_xlabel("calls")
        a2.legend()
    fig.tight_layout()
    return png(fig)


def coverage_figure(rows):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(15, 3.4))
    for ax, key, title in ((a1, "stratum", "context-size stratum"), (a2, "client", "client")):
        d = defaultdict(lambda: [0, 0])
        for r in rows:
            d[r[key]][0] += r["err"]
            d[r[key]][1] += 1
        cov = zeno.coverage({k: tuple(v) for k, v in d.items()})
        lo, hi = cov["aggregate_ci"]
        ax.axvspan(100 * lo, 100 * hi, color="grey", alpha=0.2, label="aggregate (Wilson)")
        ax.axvline(100 * cov["aggregate"], color="grey", lw=1)
        names = sorted(cov["strata"], key=lambda k: cov["strata"][k]["rate"] or 0)
        for i, name in enumerate(names):
            s = cov["strata"][name]
            col = {"worse": "#d62728", "thin": "#ff7f0e"}.get(s["status"], "#1f77b4")
            ax.errorbar([100 * s["rate"]], [i], xerr=[[max(0.0, 100 * (s["rate"] - s["ci"][0]))],
                                                       [max(0.0, 100 * (s["ci"][1] - s["rate"]))]],
                        fmt="o", color=col, capsize=3)
            tag = s["status"] + (" (thin)" if s["status"] == "worse" and s["trials"] < 30 else "")
            ax.annotate(f"{s['failures']}/{s['trials']}  {tag}", (100 * s["ci"][1], i),
                        textcoords="offset points", xytext=(6, -3), fontsize=7)
        ax.set_yticks(range(len(names)), names)
        ax.set_xlabel("% failed (Wilson 95%)")
        ax.set_title(f"coverage by {title}: what the aggregate hides")
        ax.legend(loc="lower right")
    fig.tight_layout()
    return png(fig)


def discovery_figure(rows, xval_path):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(15, 3.4))
    errs = [r for r in rows if r["err"]]
    if errs:
        xs = [dt.datetime.fromtimestamp(r["ts"]) for r in errs]
        kinds, seen, unl = [], set(), []
        for i, r in enumerate(errs, 1):
            if r["cause"]:
                seen.add(r["cause"])
            kinds.append(len(seen))
            unl.append(100 * sum(1 for x in errs[:i] if not x["cause"]) / i)
        a1.step(xs, kinds, where="post", color="#1f77b4", label="named failure kinds (cumulative)")
        a1.set_ylabel("distinct causes")
        ax2 = a1.twinx()
        ax2.plot(xs, unl, color="#888", lw=1, label="% of failures with no cause (cumulative)")
        ax2.set_ylabel("% unnamed")
        ax2.set_ylim(0, 100)
        a1.set_title("epistemic: failure kinds discovered vs. failures we can't name")
        h1, l1 = a1.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        a1.legend(h1 + h2, l1 + l2, loc="center right")
        _fmt_x(a1)
    runs = [r for r in zeno._jsonl(xval_path) if r.get("arm")]
    if runs:
        arms = sorted({r["arm"] for r in runs})
        for r in runs:
            y = arms.index(r["arm"])
            if r.get("cost") is None:
                a2.plot([1], [y], "x", color="#d62728", ms=8)
            else:
                a2.plot([r["cost"]], [y], "o" if r.get("ok") else "x",
                        color="#2ca02c" if r.get("ok") else "#d62728", ms=6, alpha=0.8)
        a2.set_yticks(range(len(arms)), arms)
        a2.set_xscale("log")
        a2.set_xlabel("cost units per review (× at far left = failed with no cost)")
        a2.set_title(f"engineering: xval reviews per output-cap arm ({len(runs)} runs; ● ok, × failed)")
    fig.tight_layout()
    return png(fig)


# ---- page ------------------------------------------------------------------------------------

def _f(x, unit=""):
    if x is None:
        return "—"
    if abs(x) >= 1000:
        return f"{x:,.0f}{unit}"
    return f"{x:.1f}{unit}" if abs(x) >= 10 else f"{x:.2f}{unit}"


METRICS = [("ttft", "time to first token", "ms"),
           ("ttfb", "upstream first byte", "ms"),
           ("apex", "apex added latency", "ms"),
           ("out", "output tokens", "tokens"),
           ("prompt", "prompt tokens (incl. cached)", "tokens"),
           ("cost", "cost units per call", "units")]


def summary_table(rows):
    out = ["<table><tr><th>metric</th><th>client</th><th>n</th><th>mean</th><th>median</th>"
           "<th>p95 [95% CI]</th><th>p99 [95% CI]</th><th>max</th><th>p95÷mean</th><th>p99÷mean</th></tr>"]
    for key, label, unit in METRICS:
        for c in CLIENTS:
            v = [r[key] for r in rows if r["client"] == c and not r["err"] and r[key] is not None]
            s = stats(v)
            if not s["n"]:
                continue
            c95, c99 = boot_ci(v, 0.95), boot_ci(v, 0.99)
            out.append(
                f"<tr><td>{label}</td><td><span class='dot' style='background:{COLOR[c]}'></span>{c}</td>"
                f"<td>{s['n']:,}</td><td>{_f(s['mean'])}</td><td>{_f(s['p50'])}</td>"
                f"<td><b>{_f(s['p95'])}</b> <small>[{_f(c95[0])}, {_f(c95[1])}]</small></td>"
                f"<td><b>{_f(s['p99'])}</b> <small>[{_f(c99[0])}, {_f(c99[1])}]</small></td>"
                f"<td>{_f(s['max'])}</td><td>{s['p95'] / s['mean']:.1f}×</td>"
                f"<td>{s['p99'] / s['mean']:.1f}×</td></tr>")
    out.append("</table>")
    return "\n".join(out)


def kpis(rows):
    n = len(rows)
    k = sum(r["err"] for r in rows)
    p = 1 - k / n
    unl = sum(1 for r in rows if r["err"] and not r["cause"])
    ttft = stats([r["ttft"] for r in rows if not r["err"]])
    cards = [("calls", f"{n:,}", f"{rows[0]['day']} → {rows[-1]['day']}"),
             ("completed", f"{100 * p:.2f}%", f"{zeno.nines(p):.2f} nines · {k} failed"),
             ("100-call task clean", f"{100 * p ** 100:.1f}%", f"p^100; 90% needs p ≥ {100 * 0.9 ** 0.01:.3f}%"),
             ("failures with no cause", f"{100 * unl / k:.0f}%" if k else "—",
              f"{unl} of {k} — mid-stream breaks get midstream_* from the 0.4.2 proxy on"),
             ("ttft mean / p95 / p99", f"{ttft['mean'] / 1000:.1f} / {ttft['p95'] / 1000:.1f} / {ttft['p99'] / 1000:.1f} s",
              "successful calls, both clients")]
    return "".join(f"<div class='kpi'><div class='k'>{html.escape(a)}</div><div class='v'>{html.escape(b)}</div>"
                   f"<div class='s'>{html.escape(c)}</div></div>" for a, b, c in cards)


CSS = """
body{font:14px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;margin:24px auto;max-width:1500px;color:#222;padding:0 16px}
h1{margin:0 0 4px;font-size:22px} h2{margin:28px 0 6px;font-size:17px;border-bottom:1px solid #ddd;padding-bottom:4px}
.sub{color:#666;margin-bottom:14px} .kpis{display:flex;gap:12px;flex-wrap:wrap}
.kpi{border:1px solid #e3e3e3;border-radius:8px;padding:10px 14px;min-width:200px;background:#fafafa}
.kpi .k{color:#666;font-size:12px}.kpi .v{font-size:22px;font-weight:600}.kpi .s{color:#888;font-size:12px}
img{width:100%;border:1px solid #eee;border-radius:6px;margin:6px 0}
p.note{color:#555;margin:4px 0 2px;max-width:1150px}
table{border-collapse:collapse;font-size:13px;margin-top:8px}
th,td{border-bottom:1px solid #eee;padding:5px 10px;text-align:right}th{background:#f4f4f4}
td:first-child,td:nth-child(2),th:first-child,th:nth-child(2){text-align:left}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
small{color:#888} code{background:#f2f2f2;padding:1px 4px;border-radius:3px}
"""


def build(rows, xval_path, sources, generated):
    _DAY_N.clear()
    _DAY_N.update(Counter(r["day"] for r in rows))
    sec = []

    def add(title, note, img):
        sec.append(f"<h2>{html.escape(title)}</h2><p class='note'>{note}</p><img src='data:image/png;base64,{img}'>")

    add("Reliability — mean rate vs bursts",
        "Left: the day's failure rate with its Wilson 95% band. Middle: the same failures cut into "
        "10-minute windows — the mean window vs the worst 5% and 1% of windows each day. A flat mean with "
        "a jumping p99 means failures arrive in bursts. Right: what failed, with "
        "<i>no cause recorded</i> hatched (the blind spot the 0.4.2 proxy now labels <code>midstream_*</code>).",
        reliability_figure(rows))
    for key, label, unit in METRICS:
        note = {"ttft": "Client wait from request to first byte. Center (left) and tails (middle) are drawn "
                        "separately because they move separately. Hollow markers: too few calls that day for "
                        "that statistic (mean and p95 &lt; 20 calls, p99 &lt; 100) — one or two calls, not a trend. Right: whole-window survival curve; "
                        "vertical lines are p95 (solid) and p99 (dotted).",
                "ttfb": "Upstream share of ttft (ttft = apex added + upstream first byte).",
                "apex": "The proxy's own pre-forward work plus any connect-retry backoff it slept.",
                "out": "Output tokens per successful call.",
                "prompt": "Prompt size per call, normalised across wires (OpenAI counts cached tokens in "
                          "input; Anthropic doesn't, so cache read + write are added).",
                "cost": "Relative cost in input-token equivalents: uncached + 0.1·cache read + "
                        "1.25·cache write + 4·output. Not dollars."}[key]
        add(f"{label[0].upper()}{label[1:]}", note, metric_figure(rows, key, label, unit))
    add("Tail weight over time",
        "Left: how much of each day's cost the costliest 5% and 1% of calls carry. Right: p99 ÷ mean per "
        "metric per day — 1× would be no tail; the higher, the more the tail, not the average, decides "
        "what a long task experiences.", tail_share_figure(rows))
    add("Horizon — per-call reliability compounds",
        "Left: P(no failed call in an n-call session) under independence (p^n) against the clean-session "
        "rate actually observed (Wilson bars). A gap says one p with independent calls doesn't describe "
        "sessions (clustering, per-session p, or length that depends on outcome). Only rows carrying a "
        "session id (claude-code) enter. Right: how long sessions are.", sessions_figure(rows))
    add("Coverage — what the aggregate rate hides",
        "Each group's failure rate with its Wilson 95% interval against the aggregate band. "
        "<b style='color:#d62728'>worse</b> = the group's lower bound sits above the aggregate's upper bound.",
        coverage_figure(rows))
    add("Discovery and engineering",
        "Left: distinct named failure kinds over time and the running share of failures with no recorded "
        "cause. Right: each xval cross-validation review by arm and cost — too few runs per arm for a "
        "cost-per-nine verdict yet.", discovery_figure(rows, xval_path))

    src = ", ".join(html.escape(Path(s).name) for s in sources)  # names only: the page gets shared
    thin = [d for d in sorted(_DAY_N) if _DAY_N[d] < THIN_DAY]
    thin_note = ""
    if thin:
        tn = sum(_DAY_N[d] for d in thin)
        te = sum(r["err"] for r in rows if r["day"] in thin)
        thin_note = (f" Thin days {', '.join(f'{d:%m-%d}' for d in thin)}: {tn} calls, {te} failed "
                     f"({100 * te / tn:.0f}%).")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>apex proxy — stats &amp; tails</title><style>{CSS}</style></head><body>
<h1>apex proxy — current stats, dynamics, and tails</h1>
<div class="sub">Generated {generated} · {len(rows):,} calls · sources: {src}<br>
Measures whether calls <b>finished</b> (transport/upstream faults), timing, tokens and relative cost —
<b>not</b> whether answers were right.<br>Every x-axis tick is a day with its call count underneath;
<span style="background:#f2c94c55;padding:0 4px">shaded</span> days had &lt; {THIN_DAY} calls — read them as
events, not trend points.{thin_note}</div>
<div class="kpis">{kpis(rows)}</div>
<h2>Summary — center vs tails, whole window</h2>
<p class="note">Successful calls. p95/p99 with bootstrap 95% CIs (300 resamples). The ÷mean columns show how far
the tail sits from the average.</p>
{summary_table(rows)}
{''.join(sec)}
</body></html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--telemetry", type=Path, action="append",
                    help="telemetry jsonl (repeatable). Default: the live file and its .1 rotation")
    ap.add_argument("--xval-runs", type=Path,
                    default=Path(os.environ.get("APEX_ROUTER_HOME", Path.home() / ".apex-router")) / "xval_runs.jsonl")
    ap.add_argument("--days", type=float, default=30.0, help="window, days back from now (default 30)")
    ap.add_argument("-o", "--out", type=Path,
                    default=Path.home() / ".apex-router" / "reports" / "telemetry-dashboard.html")
    ap.add_argument("--open", action="store_true", help="open the page in the browser")
    a = ap.parse_args(argv)
    live = telemetry_path()
    sources = a.telemetry or [live.with_name(live.name + ".1"), live]
    now = dt.datetime.now()
    rows = load(sources, now.timestamp() - a.days * 86400)
    if not rows:
        print("no proxy request rows in the window", file=sys.stderr)
        return 1
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(build(rows, a.xval_runs, [s for s in sources if Path(s).exists()],
                           now.strftime("%Y-%m-%d %H:%M")))
    print(f"{a.out}  ({len(rows):,} calls, {a.out.stat().st_size / 1e6:.1f} MB)")
    if a.open:
        webbrowser.open(a.out.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
