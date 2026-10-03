#!/usr/bin/env python3
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone

LOG_PATH = os.path.expanduser("~/.apex/telemetry.jsonl")
OUT_PATH = "./telemetry_analysis.json"

RATES = {
    "kimi-k3": (3.00, 0.30, 15.00, 0.0),
    "kimi-k2.7-code": (0.95, 0.19, 4.00, 0.0),
    "kimi-k2.6": (0.95, 0.16, 4.00, 0.0),
    "claude-sonnet-5": (3.00, 0.30, 15.00, 3.75),
    "claude-opus-4-8": (15.00, 1.50, 75.00, 0.0),  # cache-write rate not supplied; treated as 0
}


def get_num(row, key):
    v = row.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    return 0


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_vals) else f
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def main():
    total = 0
    heartbeats = 0
    malformed = 0
    null_model_rows = 0

    request_rows = []

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            total += 1
            line = line.strip()
            if not line:
                malformed += 1
                continue
            try:
                row = json.loads(line)
            except Exception:
                malformed += 1
                continue

            if row.get("ev") == "hb":
                heartbeats += 1
                continue

            if row.get("model_resolved") is None:
                null_model_rows += 1

            request_rows.append(row)

    # 2. Per (client, model_resolved)
    per_client_model = defaultdict(lambda: {
        "requests": 0,
        "distinct_sessions": set(),
        "tokens_in": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "tokens_out": 0,
    })

    # 3. Codex context
    codex_contexts = []
    codex_sessions = set()
    codex_count = 0

    # 5. Claude-code cache hit / bust
    claude_cache_read = 0
    claude_tokens_in = 0
    claude_bust_count = 0

    # 6. Time / daily
    ts_list = []
    daily_counts = defaultdict(lambda: defaultdict(int))

    for row in request_rows:
        client = row.get("client")
        model = row.get("model_resolved")
        session_id = row.get("session_id")

        bucket = per_client_model[(client, model)]
        bucket["requests"] += 1
        if session_id is not None:
            bucket["distinct_sessions"].add(session_id)
        bucket["tokens_in"] += get_num(row, "tokens_in")
        bucket["cache_read_tokens"] += get_num(row, "cache_read_tokens")
        bucket["cache_write_tokens"] += get_num(row, "cache_write_tokens")
        bucket["tokens_out"] += get_num(row, "tokens_out")

        ts = row.get("ts")
        if isinstance(ts, (int, float)):
            ts_list.append(ts)
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            day = dt.strftime("%Y-%m-%d")
            daily_counts[client][day] += 1

        if client == "codex":
            codex_count += 1
            if session_id is not None:
                codex_sessions.add(session_id)
            ctx = get_num(row, "tokens_in") + get_num(row, "cache_read_tokens")
            codex_contexts.append(ctx)

        if client == "claude-code":
            claude_cache_read += get_num(row, "cache_read_tokens")
            claude_tokens_in += get_num(row, "tokens_in")
            if row.get("bust") is True:
                claude_bust_count += 1

    # Convert sets to counts
    per_client_model_out = {}
    for (client, model), data in sorted(per_client_model.items(), key=lambda x: (-x[1]["requests"], x[0])):
        per_client_model_out[f"{client}|{model}"] = {
            "requests": data["requests"],
            "distinct_sessions": len(data["distinct_sessions"]),
            "tokens_in": data["tokens_in"],
            "cache_read_tokens": data["cache_read_tokens"],
            "cache_write_tokens": data["cache_write_tokens"],
            "tokens_out": data["tokens_out"],
        }

    # Codex context stats
    codex_contexts_sorted = sorted(codex_contexts)
    p50 = percentile(codex_contexts_sorted, 50)
    p95 = percentile(codex_contexts_sorted, 95)
    max_ctx = max(codex_contexts) if codex_contexts else 0
    pct_over_250k = (sum(1 for c in codex_contexts if c > 250000) / len(codex_contexts) * 100.0) if codex_contexts else 0.0

    # 4. Cost
    def cost_for(tokens_in, cache_read, cache_write, tokens_out, model):
        rates = RATES.get(model, (0.0, 0.0, 0.0, 0.0))
        in_rate, cr_rate, cw_rate, out_rate = rates[0], rates[1], rates[3], rates[2]
        cost = (
            tokens_in / 1e6 * in_rate
            + cache_read / 1e6 * cr_rate
            + cache_write / 1e6 * cw_rate
            + tokens_out / 1e6 * out_rate
        )
        return cost

    actual_spend = defaultdict(float)
    for row in request_rows:
        client = row.get("client")
        model = row.get("model_resolved")
        spend = cost_for(
            get_num(row, "tokens_in"),
            get_num(row, "cache_read_tokens"),
            get_num(row, "cache_write_tokens"),
            get_num(row, "tokens_out"),
            model,
        )
        actual_spend[(client, model)] += spend

    actual_spend_out = {
        f"{client}|{model}": round(v, 6)
        for (client, model), v in sorted(actual_spend.items(), key=lambda x: -x[1])
    }

    # Counterfactual: all codex-client requests priced as kimi-k2.7-code, writes free
    counterfactual = 0.0
    for row in request_rows:
        if row.get("client") == "codex":
            counterfactual += cost_for(
                get_num(row, "tokens_in"),
                get_num(row, "cache_read_tokens"),
                0,  # writes free
                get_num(row, "tokens_out"),
                "kimi-k2.7-code",
            )

    # 5. Claude cache hit
    claude_cache_denom = claude_cache_read + claude_tokens_in
    claude_cache_hit_rate = (claude_cache_read / claude_cache_denom * 100.0) if claude_cache_denom else 0.0

    # 6. Time range
    min_ts = min(ts_list) if ts_list else None
    max_ts = max(ts_list) if ts_list else None

    result = {
        "row_counts": {
            "total": total,
            "heartbeats": heartbeats,
            "request_rows": len(request_rows),
            "malformed": malformed,
            "null_model_resolved": null_model_rows,
        },
        "per_client_model": per_client_model_out,
        "codex_context": {
            "total_requests": codex_count,
            "distinct_sessions": len(codex_sessions),
            "p50": p50,
            "p95": p95,
            "max": max_ctx,
            "pct_over_250k": pct_over_250k,
        },
        "cost": {
            "actual_spend_per_client_model": actual_spend_out,
            "counterfactual_codex_as_kimi_k2_7_code": counterfactual,
        },
        "claude_code": {
            "cache_hit_rate_pct": claude_cache_hit_rate,
            "bust_true_count": claude_bust_count,
        },
        "time_range": {
            "min_ts": min_ts,
            "max_ts": max_ts,
            "min_ts_iso": datetime.fromtimestamp(min_ts, tz=timezone.utc).isoformat() if min_ts else None,
            "max_ts_iso": datetime.fromtimestamp(max_ts, tz=timezone.utc).isoformat() if max_ts else None,
        },
        "requests_per_client_per_day": {
            client: dict(sorted(days.items()))
            for client, days in sorted(daily_counts.items())
        },
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    # Human summary
    print("=== Telemetry Analysis ===")
    print(f"Total lines: {total}")
    print(f"  Heartbeats: {heartbeats}")
    print(f"  Request rows: {len(request_rows)}")
    print(f"  Malformed: {malformed}")
    print(f"  Null model_resolved: {null_model_rows}")
    print()
    print(f"Request time range: {result['time_range']['min_ts_iso']} -> {result['time_range']['max_ts_iso']}")
    print("Requests per client per day:")
    for client, days in result["requests_per_client_per_day"].items():
        print(f"  {client}: {days}")
    print()
    print("Codex context (tokens_in + cache_read_tokens):")
    print(f"  Requests: {codex_count}, Distinct sessions: {len(codex_sessions)}")
    print(f"  p50: {p50:,.0f}, p95: {p95:,.0f}, max: {max_ctx:,.0f}, >250k: {pct_over_250k:.2f}%")
    print()
    print("Top (client, model) spend (actual):")
    for k, v in list(actual_spend_out.items())[:8]:
        print(f"  {k}: ${v:,.4f}")
    print(f"Counterfactual (codex as kimi-k2.7-code): ${counterfactual:,.4f}")
    print()
    print("Claude-code cache hit rate: {:.2f}%".format(claude_cache_hit_rate))
    print(f"Claude-code bust==true rows: {claude_bust_count}")
    print(f"\nFull JSON written to {OUT_PATH}")


if __name__ == "__main__":
    main()
