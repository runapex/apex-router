#!/usr/bin/env python3
"""sd_baseline — the measured baseline behind docs/PLAN-system-design-optimizations.md.

    python3 scripts/sd_baseline.py [--telemetry FILE ...] [--json]

Re-run before and after each plan item; every item names the line here that should move.
Read-only over the proxy telemetry (live file + its .1 rotation by default). Pure stdlib.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from apex_router.core.stats import nearest_rank  # noqa: E402
from apex_router.telemetry_path import telemetry_path  # noqa: E402


def load(paths):
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
                sh = r.pop("shadow", None) or {}
                r["_ctx"] = sh.get("context_bytes") or 0
                r["_row_bytes"] = len(line)
                rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows


def q(v, p):
    v = sorted(x for x in v if x is not None)
    return nearest_rank(v, p) if v else None


def baseline(rows):
    ok = [r for r in rows if not r["is_error"]]
    out = {"calls": len(rows), "failed": sum(r["is_error"] for r in rows)}
    out["completion"] = 1 - out["failed"] / len(rows) if rows else None

    # B1 pre-forward latency (apex_added_ms) vs the 20 ms budget, by client and matcher path
    aa = [r["apex_added_ms"] for r in ok if r.get("apex_added_ms")]
    out["apex_added"] = {"p50": q(aa, .5), "p95": q(aa, .95), "p99": q(aa, .99),
                         "over_20ms": sum(x > 20 for x in aa) / len(aa) if aa else None}
    by = collections.defaultdict(list)
    for r in ok:
        if r.get("apex_added_ms"):
            by[f"{r.get('client')}/{r.get('matcher_event')}"].append(r["apex_added_ms"])
    out["apex_added_by_path"] = {k: {"n": len(v), "p50": q(v, .5), "p95": q(v, .95)}
                                 for k, v in sorted(by.items()) if len(v) >= 30}

    # B2 failure cost: time to fail per cause, connect retries that rescued a call
    fails = collections.defaultdict(list)
    for r in rows:
        if r["is_error"] and r.get("error_cause"):
            fails[r["error_cause"]].append((r.get("upstream_error_wait_ms") or 0)
                                           + (r.get("connect_backoff_ms") or 0))
    out["time_to_fail_s"] = {c: {"n": len(v), "p50": q(v, .5) / 1000, "p95": q(v, .95) / 1000}
                             for c, v in fails.items() if v and q(v, .5) is not None}
    # connect_retries counts BOTH pre-write (connect) and fast-fail body-sent retries (upstream.py);
    # the row does not say which class rescued it, so this is "any transport retry that rescued".
    out["transport_retry_rescues"] = sum(1 for r in ok if (r.get("connect_retries") or 0) > 0)
    runs, k = [], 0
    for r in (x for x in rows if x.get("client") == "codex"):
        if r["is_error"]:
            k += 1
        elif k:
            runs.append(k)
            k = 0
    if k:
        runs.append(k)
    out["codex_longest_failure_run"] = max(runs) if runs else 0

    # B3 failure burstiness: index of dispersion of errors per active minute (Poisson = 1)
    act = collections.Counter(int(r["ts"] // 60) for r in rows)
    err = collections.Counter(int(r["ts"] // 60) for r in rows if r["is_error"])
    e = [err.get(m, 0) for m in act]
    out["error_dispersion_index"] = (st.pvariance(e) / st.fmean(e)) if e and st.fmean(e) else None
    # exposure-adjusted: under independent failures at one rate p, minute i has variance n_i p (1-p).
    # Observed sum of squared deviations from n_i p over that expectation; 1 = no extra clustering.
    p_fail = out["failed"] / len(rows) if rows else 0
    exp_var = sum(n * p_fail * (1 - p_fail) for n in act.values())
    obs = sum((err.get(m, 0) - n * p_fail) ** 2 for m, n in act.items())
    out["error_dispersion_exposure_adjusted"] = obs / exp_var if exp_var else None

    # B4 claude-code cold-cache tail: share of cost in cache writes, rewrites after 5-60 min idle
    cc = [r for r in ok if r.get("client") == "claude-code" and r.get("usage")]
    last, gap_writes, all_writes = {}, 0, 0
    cost = write_cost = 0.0
    for r in cc:
        key = (r.get("session_id"), r.get("agent_id"))
        gap = r["ts"] - last[key] if key in last else None
        last[key] = r["ts"]
        cw = r.get("cache_write_tokens") or 0
        cost += ((r.get("tokens_in") or 0) + 0.1 * (r.get("cache_read_tokens") or 0)
                 + 1.25 * cw + 4 * (r.get("tokens_out") or 0))
        write_cost += 1.25 * cw
        if cw > 20000:
            all_writes += cw
            if gap is not None and 300 <= gap < 3600:
                gap_writes += cw
    out["claude_cache_write_cost_share"] = write_cost / cost if cost else None
    # token-weighted, among successful usage-bearing calls that WROTE > 20k tokens to the cache
    out["claude_big_writes_after_5_60min_idle_share"] = gap_writes / all_writes if all_writes else None
    c_all = [((r.get("tokens_in") or 0) + 0.1 * (r.get("cache_read_tokens") or 0)
              + 1.25 * (r.get("cache_write_tokens") or 0) + 4 * (r.get("tokens_out") or 0)) for r in cc]
    warm = [c for c, r in zip(c_all, cc) if (r.get("cache_write_tokens") or 0) <= 20000]
    cs2 = lambda v: st.pvariance(v) / st.fmean(v) ** 2 if len(v) > 1 and st.fmean(v) else None  # noqa: E731
    out["claude_cost"] = {"p99_over_mean": q(c_all, .99) / st.fmean(c_all) if c_all else None,
                          "cs2_all": cs2(c_all), "cs2_without_big_writes": cs2(warm),
                          "big_write_calls_share": 1 - len(warm) / len(cc) if cc else None}

    # B1 context: isolated vs busy (other requests within +-60 s of the emit time)
    import bisect
    ts = [r["ts"] for r in rows]
    iso, busy = [], []
    for r in ok:
        if r.get("client") == "claude-code" and r.get("matcher_event") == "client_edit" and r.get("apex_added_ms"):
            nb = bisect.bisect_right(ts, r["ts"] + 60) - bisect.bisect_left(ts, r["ts"] - 60) - 1
            (iso if nb == 0 else busy if nb >= 20 else []).append(r["apex_added_ms"])
    out["client_edit_isolated_vs_busy_p50"] = (q(iso, .5), len(iso), q(busy, .5), len(busy))

    # sessions (rows with a session id)
    sess = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        if r.get("session_id"):
            sess[r["session_id"]][0] += 1
            sess[r["session_id"]][1] += r["is_error"]
    calls = [c for c, _ in sess.values()]
    sc, se = sum(calls), sum(e for _, e in sess.values())
    out["sessions"] = {"n": len(sess), "calls_mean": st.fmean(calls) if calls else None,
                       "calls_median": st.median(calls) if calls else None,
                       "completion": 1 - se / sc if sc else None,
                       "failed_calls_with_session_id": se}
    out["http_429"] = sum(1 for r in rows if r.get("error_cause") == "http_429")
    days = (rows[-1]["ts"] - rows[0]["ts"]) / 86400 if len(rows) > 1 else 0
    out["rows_per_day"] = len(rows) / days if days else None

    # B5 telemetry weight
    out["telemetry_row_bytes"] = {"mean": st.fmean(r["_row_bytes"] for r in rows),
                                  "p99": q([r["_row_bytes"] for r in rows], .99)}
    # B6 what the rows cannot answer yet
    keys = set().union(*(r.keys() for r in rows[-200:])) if rows else set()
    out["missing_fields"] = [f for f in ("stage_ms", "t_total_ms", "max_chunk_gap_ms", "attempt_of")
                             if f not in keys]
    return out


def _f(x, spec=".1f", scale=1.0):
    return "—" if x is None else format(x * scale, spec)


def render(b):
    L = [f"calls {b['calls']:,}  completion {_f(b['completion'], '.2f', 100)}%  failed {b['failed']}"]
    a = b["apex_added"]
    L.append(f"B1 apex_added_ms p50 {_f(a['p50'])}  p95 {_f(a['p95'])}  p99 {_f(a['p99'])}  "
             f"over 20 ms budget {_f(a['over_20ms'], '.1f', 100)}%")
    for k, v in b["apex_added_by_path"].items():
        L.append(f"     {k:28} n={v['n']:5}  p50 {_f(v['p50']):>6}  p95 {_f(v['p95']):>6}")
    i50, ni, b50, nb = b["client_edit_isolated_vs_busy_p50"]
    L.append(f"   client_edit apex_added p50: isolated (no call within ±60 s) {_f(i50)} ms n={ni}; "
             f"busy (20+) {_f(b50)} ms n={nb}")
    L.append("B2 time to fail (s): " + ", ".join(
        f"{c} n={v['n']} p50 {_f(v['p50'])} p95 {_f(v['p95'])}" for c, v in sorted(b["time_to_fail_s"].items())))
    L.append(f"   transport retries (connect or fast-fail body-sent) that rescued a call: "
             f"{b['transport_retry_rescues']}  longest codex failure run: {b['codex_longest_failure_run']}")
    L.append(f"B3 error dispersion per active minute: raw {_f(b['error_dispersion_index'])}, "
             f"exposure-adjusted {_f(b['error_dispersion_exposure_adjusted'])} (1 = independent at one rate)")
    L.append(f"B4 claude-code cost in cache writes: {_f(b['claude_cache_write_cost_share'], '.1f', 100)}%; "
             f"tokens in >20k-token writes that followed a 5-60 min idle gap: "
             f"{_f(b['claude_big_writes_after_5_60min_idle_share'], '.0f', 100)}%")
    c = b["claude_cost"]
    L.append(f"   claude-code cost p99/mean {_f(c['p99_over_mean'])}; Cs2 all {_f(c['cs2_all'], '.2f')}, "
             f"without the {_f(c['big_write_calls_share'], '.1f', 100)}% big-write calls "
             f"{_f(c['cs2_without_big_writes'], '.2f')}")
    t = b["telemetry_row_bytes"]
    L.append(f"B5 telemetry row bytes mean {_f(t['mean'], ',.0f')}  p99 {_f(t['p99'], ',.0f')}")
    L.append(f"B6 not recorded yet: {', '.join(b['missing_fields']) or 'none'}")
    s = b["sessions"]
    L.append(f"S  sessions {s['n']}: calls mean {_f(s['calls_mean'], '.0f')}, median {_f(s['calls_median'], '.0f')}; "
             f"completion {_f(s['completion'], '.2f', 100)}%; failed calls with a session id "
             f"{s['failed_calls_with_session_id']}")
    L.append(f"   http_429 rows {b['http_429']}; rows/day {_f(b['rows_per_day'], ',.0f')}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="baseline for the system-design optimization plan")
    ap.add_argument("--telemetry", type=Path, action="append")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    live = telemetry_path()
    rows = load(a.telemetry or [live.with_name(live.name + ".1"), live])
    if not rows:
        print("no proxy request rows", file=sys.stderr)
        return 1
    b = baseline(rows)
    print(json.dumps(b, indent=2) if a.json else render(b))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
