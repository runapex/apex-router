#!/usr/bin/env python3
"""apex-router cache-cost report + offload ROI gate — offline, measure-only.

Reads the local telemetry the proxy already writes and produces (a) a cache-cost
decomposition per window and per session, and (b) an offload ROI gate per lane.
Ships nothing off-box; no proxy restart; no model call.

Two hard-won facts this tool encodes (verified against live telemetry, 2026-08-21):
  * Heartbeat rows carry `ev == "hb"` and ZERO cache-read tokens. Only rows with
    `ev` absent/null are real requests. Every sum filters heartbeats.
  * The proxy `session_id` is byte-identical to the Claude Code transcript id, so
    per-session numbers here join directly to a live session (used by C3).

Run:
    python3 scripts/cache_report.py --days 7            # text report
    python3 scripts/cache_report.py --days 7 --json     # machine-readable
    python3 scripts/cache_report.py --check             # exit 2 if a weekly claim
                                                        # lacks >=7d of data
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---- pricing (Opus-tier, relative to $5/1M input). Output = 5x input. ----------
BASE_IN_PER_TOKEN = 5.0 / 1_000_000
READ_MULT = 0.1        # cache read
WRITE_MULT = 1.25      # cache write, 5-minute TTL
FRESH_MULT = 1.0       # uncached input
OUT_MULT = 5.0         # output ($25/1M)

# Offload eras never overlap: ornith-35b (…08-05) then qwen3.8-27b (08-18…). A
# cutoff anywhere in the gap cleanly credits only the qwen era. Epoch seconds for
# 2026-08-10T00:00:00Z (between the two eras), passed in rather than computed so
# the module has no wall-clock dependency.
QWEN_CUTOVER_TS = 1786406400.0

DEFAULT_TELEMETRY = Path.home() / ".apex" / "telemetry.jsonl"
DEFAULT_OFFLOAD = Path.home() / ".apex" / "offload_telemetry.jsonl"


def _num(v) -> float:
    """A telemetry field that may be null/absent → 0.0. Null-safe by contract."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def iter_records(path: Path):
    """Yield parsed JSON objects from a JSONL file, tolerating a half-written
    trailing line (a live tail can always leave one) and blank lines."""
    try:
        fh = open(path, encoding="utf-8")
    except FileNotFoundError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # partial/corrupt line — skip, never crash the report
                continue


def is_request(rec: dict) -> bool:
    """A real proxy request, not a heartbeat. Heartbeats set ev=='hb' and carry
    no cache-read tokens, so excluding them never drops billable tokens."""
    return rec.get("ev") != "hb"


def cost_usd(read: float, write: float, fresh: float, out: float) -> float:
    """Modeled dollar cost of a token mix under the caching price schedule."""
    return (read * READ_MULT + write * WRITE_MULT + fresh * FRESH_MULT) * BASE_IN_PER_TOKEN \
        + out * OUT_MULT * BASE_IN_PER_TOKEN


def no_cache_cost_usd(read: float, write: float, fresh: float, out: float) -> float:
    """Counterfactual: with no cache, every read+write token is re-billed as
    fresh input at 1x. Output is unchanged."""
    return (read + write + fresh) * FRESH_MULT * BASE_IN_PER_TOKEN \
        + out * OUT_MULT * BASE_IN_PER_TOKEN


def summarize_cache(records, *, now_ts: float, days: float, top_n: int = 10) -> dict:
    """Cache-cost decomposition over the last `days`, plus per-session ranking.

    `now_ts` is passed in (no wall-clock read) so the summary is deterministic
    and testable. `span_days_present` reports the ACTUAL span of in-window data
    so a short window is never silently annualized as a full week.
    """
    lo = now_ts - days * 86400
    tot = {"read": 0.0, "write": 0.0, "fresh": 0.0, "out": 0.0, "requests": 0, "busts": 0}
    per_session: dict[str, dict] = {}
    tmin = tmax = None
    for rec in records:
        if not is_request(rec):
            continue
        ts = rec.get("ts")
        if isinstance(ts, (int, float)) and ts < lo:
            continue
        if isinstance(ts, (int, float)):
            tmin = ts if tmin is None else min(tmin, ts)
            tmax = ts if tmax is None else max(tmax, ts)
        read = _num(rec.get("cache_read_tokens"))
        write = _num(rec.get("cache_write_tokens"))
        fresh = _num(rec.get("tokens_in"))
        out = _num(rec.get("tokens_out"))
        tot["read"] += read
        tot["write"] += write
        tot["fresh"] += fresh
        tot["out"] += out
        tot["requests"] += 1
        if rec.get("bust"):
            tot["busts"] += 1
        sid = rec.get("session_id") or "?"
        s = per_session.setdefault(sid, {"read": 0.0, "write": 0.0, "requests": 0})
        s["read"] += read
        s["write"] += write
        s["requests"] += 1

    denom_in = tot["read"] + tot["write"] + tot["fresh"]
    hit_rate = tot["read"] / denom_in if denom_in else None
    rw_ratio = tot["read"] / tot["write"] if tot["write"] else None
    modeled = cost_usd(tot["read"], tot["write"], tot["fresh"], tot["out"])
    no_cache = no_cache_cost_usd(tot["read"], tot["write"], tot["fresh"], tot["out"])
    span_present = ((tmax - tmin) / 86400) if (tmin is not None and tmax is not None) else 0.0

    top = sorted(per_session.items(), key=lambda kv: kv[1]["read"], reverse=True)[:top_n]
    top_sessions = [
        {
            "session_id": sid,
            "requests": s["requests"],
            "read_tokens": int(s["read"]),
            "read_usd": round(s["read"] * READ_MULT * BASE_IN_PER_TOKEN, 4),
            "read_usd_per_req": round(
                s["read"] * READ_MULT * BASE_IN_PER_TOKEN / s["requests"], 4
            ) if s["requests"] else 0.0,
            "rw_ratio": round(s["read"] / s["write"], 1) if s["write"] else None,
        }
        for sid, s in top
    ]

    return {
        "window_days": days,
        "span_days_present": round(span_present, 2),
        "requests": tot["requests"],
        "read_tokens": int(tot["read"]),
        "write_tokens": int(tot["write"]),
        "fresh_tokens": int(tot["fresh"]),
        "out_tokens": int(tot["out"]),
        "input_hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "read_write_ratio": round(rw_ratio, 1) if rw_ratio is not None else None,
        "busts": tot["busts"],
        "modeled_cost_usd": round(modeled, 2),
        "read_cost_usd": round(tot["read"] * READ_MULT * BASE_IN_PER_TOKEN, 2),
        "no_cache_cost_usd": round(no_cache, 2),
        "caching_saves_usd": round(no_cache - modeled, 2),
        "top_sessions": top_sessions,
    }


def summarize_offload_roi(records, *, now_ts: float, days: float,
                          qwen_cutover_ts: float = QWEN_CUTOVER_TS) -> dict:
    """Per-lane offload ROI over the window. A lane is offload-positive only when
    net (frontier tokens saved − escalated completion tokens) > 0 AND its rows are
    from the qwen era — so qwen is never credited with ornith-era history."""
    lo = now_ts - days * 86400
    lanes: dict[str, dict] = {}
    pre_cutover_rows = 0
    for rec in records:
        ts = rec.get("ts")
        if isinstance(ts, (int, float)) and ts < lo:
            continue
        qwen_era = isinstance(ts, (int, float)) and ts >= qwen_cutover_ts
        if not qwen_era:
            pre_cutover_rows += 1
            continue
        lane = rec.get("lane") or "?"
        L = lanes.setdefault(lane, {
            "n": 0, "ok": 0, "escalated": 0, "gated": 0,
            "saved": 0.0, "escalated_completion": 0.0, "null_token_rows": 0,
        })
        L["n"] += 1
        if rec.get("ok"):
            L["ok"] += 1
        if rec.get("escalated"):
            L["escalated"] += 1
            L["escalated_completion"] += _num(rec.get("completion_tokens"))
        if rec.get("gated"):
            L["gated"] += 1
        L["saved"] += _num(rec.get("frontier_completion_tokens_saved"))
        if rec.get("prompt_tokens") is None:
            L["null_token_rows"] += 1

    out = {}
    for lane, L in sorted(lanes.items()):
        net = L["saved"] - L["escalated_completion"]
        out[lane] = {
            "n": L["n"],
            "ok_rate": round(L["ok"] / L["n"], 3) if L["n"] else None,
            "escalation_rate": round(L["escalated"] / L["n"], 3) if L["n"] else None,
            "gated": L["gated"],
            "frontier_tokens_saved": int(L["saved"]),
            "escalated_completion_tokens": int(L["escalated_completion"]),
            "net_tokens": int(net),
            "offload_positive": net > 0,
            "null_token_rows": L["null_token_rows"],
        }
    return {"qwen_era_only": True, "pre_cutover_rows_excluded": pre_cutover_rows, "by_lane": out}


def build_report(*, telemetry: Path, offload: Path, now_ts: float, days: float,
                 top_n: int = 10) -> dict:
    return {
        "schema": "cache-report/1",
        "window_days": days,
        "cache": summarize_cache(iter_records(telemetry), now_ts=now_ts, days=days, top_n=top_n),
        "offload_roi": summarize_offload_roi(iter_records(offload), now_ts=now_ts, days=days),
    }


def _fmt_text(rep: dict) -> str:
    c = rep["cache"]
    lines = []
    lines.append(f"=== CACHE COST — last {c['window_days']:g}d "
                 f"(actual data span: {c['span_days_present']:g}d) ===")
    if c["span_days_present"] < c["window_days"] - 0.5:
        lines.append(f"  ⚠ only {c['span_days_present']:g}d of data — weekly figures are PROJECTIONS, not measured")
    lines.append(f"  requests={c['requests']}  busts={c['busts']}  "
                 f"hit_rate={c['input_hit_rate']}  r:w={c['read_write_ratio']}")
    lines.append(f"  read={c['read_tokens']:,}  write={c['write_tokens']:,}  "
                 f"fresh={c['fresh_tokens']:,}  out={c['out_tokens']:,}")
    lines.append(f"  modeled_cost=${c['modeled_cost_usd']:,.2f}  "
                 f"(read portion=${c['read_cost_usd']:,.2f})")
    lines.append(f"  no-cache counterfactual=${c['no_cache_cost_usd']:,.2f}  "
                 f"→ caching saves ${c['caching_saves_usd']:,.2f}")
    if c["span_days_present"] > 0:
        wk = c["read_cost_usd"] / c["span_days_present"] * 7
        lines.append(f"  cache-read projected to 7d ≈ ${wk:,.2f}/wk")
    lines.append("  top sessions by cache-read:")
    lines.append(f"    {'session':36} {'reqs':>5} {'read_tok':>13} {'$/req':>7} {'r:w':>6}")
    for s in c["top_sessions"]:
        lines.append(f"    {s['session_id']:36} {s['requests']:>5} "
                     f"{s['read_tokens']:>13,} {s['read_usd_per_req']:>7.3f} "
                     f"{str(s['rw_ratio']):>6}")
    r = rep["offload_roi"]
    lines.append(f"\n=== OFFLOAD ROI GATE — qwen-era only "
                 f"(excluded {r['pre_cutover_rows_excluded']} pre-cutover rows) ===")
    if not r["by_lane"]:
        lines.append("  (no qwen-era offload rows in window)")
    for lane, L in r["by_lane"].items():
        verdict = "OFFLOAD-POSITIVE" if L["offload_positive"] else "not worth offloading"
        lines.append(f"  {lane:8} n={L['n']:>3} ok={L['ok_rate']} esc={L['escalation_rate']} "
                     f"net={L['net_tokens']:>+8} → {verdict}"
                     + (f"  (⚠ {L['null_token_rows']} null-token rows)" if L["null_token_rows"] else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="apex cache-cost report + offload ROI gate")
    ap.add_argument("--telemetry", type=Path, default=DEFAULT_TELEMETRY)
    ap.add_argument("--offload", type=Path, default=DEFAULT_OFFLOAD)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--now", type=float, default=None,
                    help="epoch seconds for 'now' (default: max ts in telemetry, "
                         "so the report has no wall-clock dependency)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="exit 2 if the data span is < the requested window "
                         "(a weekly claim can't be supported by a short window)")
    args = ap.parse_args(argv)

    # Anchor 'now' to the newest telemetry ts unless overridden — avoids a
    # wall-clock read and keeps runs reproducible.
    now_ts = args.now
    if now_ts is None:
        mx = None
        for rec in iter_records(args.telemetry):
            ts = rec.get("ts")
            if isinstance(ts, (int, float)):
                mx = ts if mx is None else max(mx, ts)
        now_ts = mx if mx is not None else 0.0

    rep = build_report(telemetry=args.telemetry, offload=args.offload,
                       now_ts=now_ts, days=args.days, top_n=args.top_n)

    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(_fmt_text(rep))

    if args.check and rep["cache"]["span_days_present"] < args.days - 0.5:
        print(f"\ncache_report: FAIL — only {rep['cache']['span_days_present']:g}d of data "
              f"for a {args.days:g}d claim", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
