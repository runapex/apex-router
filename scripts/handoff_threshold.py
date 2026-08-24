#!/usr/bin/env python3
"""Compute an adaptive cache-handoff threshold from proxy telemetry."""
import json
import math
import sys
import argparse
import time
import pathlib
from collections import defaultdict


HOME = pathlib.Path.home()
TELEMETRY_PATH = HOME / ".apex" / "telemetry.jsonl"
OUTPUT_DIR = HOME / ".apex-router"
OUTPUT_PATH = OUTPUT_DIR / "handoff_threshold.json"

FLOOR = 25_000_000
CAP = 500_000_000
MIN_SESSIONS = 5
STALE_SECONDS = 2 * 24 * 60 * 60


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Compute an adaptive cache-handoff threshold from proxy telemetry."
    )
    parser.add_argument(
        "--telemetry",
        default=str(TELEMETRY_PATH),
        help="Path to the telemetry JSONL file.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Lookback window in days (default: 14).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print the current stored threshold and exit 2 if missing/stale.",
    )
    return parser.parse_args(argv)


def load_sessions(path, days):
    """Aggregate cache_read_tokens per session over the last `days` days.

    Returns a tuple of (session_totals, unattributed_total).
    """
    path = pathlib.Path(path)
    session_totals = defaultdict(int)
    unattributed_total = 0

    if not path.exists():
        print(f"error: telemetry file not found: {path}", file=sys.stderr)
        sys.exit(2)

    now = time.time()
    cutoff = now - days * 24 * 60 * 60

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            if row.get("ev") == "hb":
                continue

            ts = row.get("ts")
            if not isinstance(ts, (int, float)) or ts <= 0:
                # Rows without a usable ts are skipped.
                continue

            if ts < cutoff:
                continue

            session_id = row.get("session_id")
            read_tokens = row.get("cache_read_tokens")

            if session_id is None:
                unattributed_total += read_tokens if isinstance(read_tokens, int) else 0
                continue

            if isinstance(read_tokens, int):
                session_totals[session_id] += read_tokens

    return session_totals, unattributed_total


def nearest_rank_percentile(sorted_values, pct):
    """Compute the nearest-rank percentile of a sorted list of numbers."""
    if not sorted_values:
        return None
    rank = math.ceil((pct / 100.0) * len(sorted_values))
    rank = max(1, min(rank, len(sorted_values)))
    return sorted_values[rank - 1]


def compute_threshold(session_totals, days):
    """Compute the threshold and its basis from per-session totals."""
    totals = list(session_totals.values())
    n = len(totals)
    sorted_totals = sorted(totals)

    p50 = nearest_rank_percentile(sorted_totals, 50)
    p80 = nearest_rank_percentile(sorted_totals, 80)
    max_total = sorted_totals[-1] if sorted_totals else 0

    if n < MIN_SESSIONS:
        threshold = FLOOR
        basis = "insufficient-data"
    else:
        threshold = p80
        threshold = max(threshold, FLOOR)
        threshold = min(threshold, CAP)
        basis = f"p80 of {n} sessions over {days}d"

    return threshold, basis, p50, p80, max_total, n


def write_output(result):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
        fh.write("\n")


def print_summary(result):
    print(f"handoff_threshold: {result['threshold_tokens']} tokens")
    print(f"basis: {result['basis']}")
    print(
        f"sessions={result['sessions']} p50={result['p50']} "
        f"p80={result['p80']} max={result['max']} "
        f"unattributed={result['unattributed_read_tokens']}"
    )


def do_check():
    if not OUTPUT_PATH.exists():
        print(
            f"error: stored threshold not found: {OUTPUT_PATH}",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        with OUTPUT_PATH.open("r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"error: cannot read stored threshold: {exc}", file=sys.stderr)
        sys.exit(2)

    computed_at = stored.get("computed_at")
    if not isinstance(computed_at, (int, float)) or computed_at <= 0:
        print("error: stored threshold has no valid computed_at", file=sys.stderr)
        sys.exit(2)

    age_seconds = time.time() - computed_at
    if age_seconds > STALE_SECONDS:
        print(
            f"error: stored threshold is stale ({age_seconds / 86400:.1f} days old)",
            file=sys.stderr,
        )
        sys.exit(2)

    print(json.dumps(stored, indent=2, sort_keys=True))
    sys.exit(0)


def main(argv=None):
    args = parse_args(argv)

    if args.check:
        do_check()

    session_totals, unattributed_total = load_sessions(args.telemetry, args.days)
    threshold, basis, p50, p80, max_total, n = compute_threshold(
        session_totals, args.days
    )

    result = {
        "threshold_tokens": threshold,
        "basis": basis,
        "computed_at": int(time.time()),
        "sessions": n,
        "span_days": args.days,
        "p50": p50,
        "p80": p80,
        "max": max_total,
        "unattributed_read_tokens": unattributed_total,
    }
    write_output(result)
    print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
