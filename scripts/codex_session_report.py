#!/usr/bin/env python3
"""Codex session cache-cost report — the Codex analog of cache_report.py's
per-session view.

Codex traffic can't be joined to ~/.apex/telemetry.jsonl (its proxy rows carry
session_id=null / cache_read=0), so this reads Codex's own rollout files under
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl instead. Each rollout carries
`token_count` records whose final `total_token_usage` is the session's cumulative
token mix — including `cached_input_tokens` (cache reads). That means we can price
a Codex session with the SAME schedule C1 uses, rather than guessing from bytes.

This is a measurement, not the C3 hook: Codex has no Stop-hook contract, so this
is a batch report you run (or cron), not a live per-turn nudge.

Run:
    python3 scripts/codex_session_report.py --days 7
    python3 scripts/codex_session_report.py --days 7 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Reuse the exact pricing from C1 so Codex and Claude Code numbers are comparable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from cache_report import (BASE_IN_PER_TOKEN, READ_MULT, WRITE_MULT,  # noqa: F401
                              FRESH_MULT, OUT_MULT, iter_records)
except Exception:  # pragma: no cover - fallback if run in isolation
    BASE_IN_PER_TOKEN = 5.0 / 1_000_000
    READ_MULT, WRITE_MULT, FRESH_MULT, OUT_MULT = 0.1, 1.25, 1.0, 5.0

    def iter_records(path: Path):
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
                    continue

DEFAULT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


def _num(v) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def summarize_session(path: Path) -> dict | None:
    """Extract one rollout's cumulative token mix from its LAST token_count
    record, plus session_meta (id, cwd, timestamp) and a turn proxy. Returns None
    if the file has no usable token_count."""
    meta = {}
    last_usage = None
    turns = 0  # user_message payloads = user turns; a cheap session-length proxy
    for rec in iter_records(path):
        if rec.get("type") == "session_meta":
            pl = rec.get("payload") or {}
            meta = {"session_id": pl.get("session_id"), "cwd": pl.get("cwd"),
                    "timestamp": pl.get("timestamp")}
        pl = rec.get("payload")
        if isinstance(pl, dict):
            if pl.get("type") == "token_count":
                info = pl.get("info") or {}
                tu = info.get("total_token_usage")
                if isinstance(tu, dict):
                    last_usage = tu  # cumulative; the last one wins
            if pl.get("type") == "user_message":
                turns += 1
    if last_usage is None:
        return None

    read = _num(last_usage.get("cached_input_tokens"))
    write = _num(last_usage.get("cache_write_input_tokens"))
    # `input_tokens` is the FULL inclusive input count (verified: input+output ==
    # total_tokens), so genuinely-fresh input = input - cached - cache_write.
    # Subtracting only `cached` would double-bill the cache_write tokens (once as
    # fresh @1x and once as write @1.25x).
    fresh = max(0.0, _num(last_usage.get("input_tokens")) - read - write)
    out = _num(last_usage.get("output_tokens"))

    modeled = (read * READ_MULT + write * WRITE_MULT + fresh * FRESH_MULT) * BASE_IN_PER_TOKEN \
        + out * OUT_MULT * BASE_IN_PER_TOKEN
    return {
        "file": path.name,
        "session_id": meta.get("session_id"),
        "cwd": meta.get("cwd"),
        "timestamp": meta.get("timestamp"),
        "turns": turns,
        "read_tokens": int(read),
        "write_tokens": int(write),
        "fresh_tokens": int(fresh),
        "out_tokens": int(out),
        "read_cost_usd": round(read * READ_MULT * BASE_IN_PER_TOKEN, 4),
        "modeled_cost_usd": round(modeled, 4),
        "read_cost_per_turn": round(read * READ_MULT * BASE_IN_PER_TOKEN / turns, 4) if turns else None,
    }


def iter_rollouts(sessions_dir: Path, *, mtime_after: float | None):
    for p in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            if mtime_after is not None and p.stat().st_mtime < mtime_after:
                continue
        except OSError:
            continue
        yield p


def build_report(*, sessions_dir: Path, now_ts: float, days: float, top_n: int = 15) -> dict:
    mtime_after = now_ts - days * 86400 if (now_ts and days) else None
    sessions = []
    for p in iter_rollouts(sessions_dir, mtime_after=mtime_after):
        s = summarize_session(p)
        if s is not None:
            sessions.append(s)
    sessions.sort(key=lambda s: s["read_tokens"], reverse=True)

    tot = {k: sum(s[k] for s in sessions) for k in
           ("read_tokens", "write_tokens", "fresh_tokens", "out_tokens")}
    tot_cost = round(sum(s["modeled_cost_usd"] for s in sessions), 2)
    tot_read_cost = round(sum(s["read_cost_usd"] for s in sessions), 2)
    return {
        "schema": "codex-session-report/1",
        "window_days": days,
        "sessions_counted": len(sessions),
        "total_read_tokens": tot["read_tokens"],
        "total_modeled_cost_usd": tot_cost,
        "total_read_cost_usd": tot_read_cost,
        "top_sessions": sessions[:top_n],
    }


def _fmt_text(rep: dict) -> str:
    lines = [f"=== CODEX SESSION CACHE COST — rollouts modified in last "
             f"{rep['window_days']:g}d ({rep['sessions_counted']} sessions) ==="]
    lines.append(f"  total cache-read={rep['total_read_tokens']:,} tokens  "
                 f"read cost=${rep['total_read_cost_usd']:,.2f}  "
                 f"modeled total=${rep['total_modeled_cost_usd']:,.2f}")
    lines.append("  NOTE: `codex exec` runs are single-turn, so turns≈1 and $/turn "
                 "is NOT a length signal — rank by read_tokens.")
    lines.append(f"  {'session':36} {'turns':>5} {'read_tok':>13} {'$/turn':>7} {'cwd'}")
    for s in rep["top_sessions"]:
        sid = (s["session_id"] or s["file"])[:36]
        cwd = (s["cwd"] or "").replace(str(Path.home()), "~")
        ppt = s["read_cost_per_turn"]
        lines.append(f"  {sid:36} {s['turns']:>5} {s['read_tokens']:>13,} "
                     f"{(('%.3f' % ppt) if ppt is not None else '-'):>7} {cwd}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Codex per-session cache-cost report")
    ap.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--now", type=float, default=None,
                    help="epoch 'now' (default: max rollout mtime, so no wall-clock dep)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    now_ts = args.now
    if now_ts is None:
        mx = 0.0
        for p in args.sessions_dir.rglob("rollout-*.jsonl"):
            try:
                mx = max(mx, p.stat().st_mtime)
            except OSError:
                continue
        now_ts = mx

    rep = build_report(sessions_dir=args.sessions_dir, now_ts=now_ts,
                       days=args.days, top_n=args.top_n)
    print(json.dumps(rep, indent=2) if args.json else _fmt_text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
