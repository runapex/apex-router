"""Benchmark readout over ~/.codeqa/validate_metrics.jsonl — differentiate codeqa validation runs.

Run: python -m codeqa.metrics_report   (or with a path arg)
Shows, across all recorded runs: how many stale claims were caught, the verifier cost split
(free local vs paid frontier, with est tokens), the routing saving, and a per-run table so you can
see WHICH digest drifted WHEN and at what token cost.
"""
import json
import os
import sys


def main(path=None):
    path = path or os.path.expanduser("~/.codeqa/validate_metrics.jsonl")
    if not os.path.exists(path):
        print(f"(no metrics yet at {path} — run `codeqa validate ... ` first)")
        return 0
    recs = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass  # skip a corrupt/partial line
    if not recs:
        print("(metrics file is empty)")
        return 0

    n = len(recs)
    struck = sum(r.get("n_struck", 0) for r in recs)
    local = sum(r.get("n_local", 0) for r in recs)
    frontier = sum(r.get("n_frontier", 0) for r in recs)
    tokens = sum(r.get("est_frontier_tokens", 0) for r in recs)
    calls = local + frontier
    saved = (100 * local // calls) if calls else 0

    print(f"codeqa validation — {n} run(s) over {path}\n")
    print(f"  stale claims caught : {struck}")
    print(f"  verifier calls      : {local} local (free) + {frontier} frontier (paid)")
    print(f"  est. frontier tokens: ~{tokens:,}  (≈ ${tokens * 15 / 1_000_000:.4f} at Opus list in-rate)")
    print(f"  routing saved       : {saved}% of calls kept off the paid tier")

    # SECOND axis — which frontier tier decided each paid claim (the model-picker split). Per-tier
    # input list-rate ($/MTok) so the cost readout reflects that a haiku call ≠ an opus call.
    from collections import Counter as _Counter
    tier_calls = _Counter()
    for r in recs:
        tc = r.get("tier_calls")
        if isinstance(tc, dict):
            tier_calls.update({k: v for k, v in tc.items() if isinstance(v, int)})
    if tier_calls:
        _RATE = {"haiku": 1.0, "sonnet": 3.0, "opus": 5.0}   # input $/MTok; unknown/fixed → opus rate
        _TOK = 276                                            # ≈ tokens per frontier call (see freshness)
        total_cost = 0.0
        print("\n  frontier by tier (model picker):")
        for tier, c in tier_calls.most_common():
            cost = c * _TOK * _RATE.get(tier, 5.0) / 1_000_000
            total_cost += cost
            print(f"    {tier:8} {c:>5}  ≈ ${cost:.4f}")
        print(f"    {'total':8} {sum(tier_calls.values()):>5}  ≈ ${total_cost:.4f}")

    # which digests drift most
    from collections import Counter
    by_repo = Counter()
    for r in recs:
        by_repo[r.get("repo", "?")] += r.get("n_struck", 0)
    if any(by_repo.values()):
        print("\n  stale claims by repo:")
        for repo, s in by_repo.most_common():
            print(f"    {repo:10} {s}")

    print("\n  per run:")
    print(f"    {'when':21} {'repo':9} {'struck':>6} {'local':>5} {'front':>5} {'mode':>8}")
    for r in recs[-20:]:
        mode = "local" if r.get("local_only") else ("routed" if r.get("routed") else "frontier")
        cached = " (cached)" if r.get("cached") else ""
        print(f"    {r.get('ts',''):21} {r.get('repo','?'):9} {r.get('n_struck',0):>6} "
              f"{r.get('n_local',0):>5} {r.get('n_frontier',0):>5} {mode:>8}{cached}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
