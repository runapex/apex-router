"""offload_report — read the offload telemetry log and print per-lane economics.

Answers the only question that justifies local offload: is each lane NET-POSITIVE — i.e. is it
actually moving frontier work off the paid tier, and at what correctness rate? Reads
~/.apex/offload_telemetry.jsonl (or a given path) and prints a per-lane table.

Honesty rules baked in (measurement-over-attribution):
  - `frontier_completion_tokens_saved` counts ONLY gated+ok+not-escalated calls (aggregate_offload
    enforces this). Ungated worker completions and always-escalated reviews contribute 0.
  - ok_rate is over GATED calls only — an ungated completion has no earned verdict.
  - A lane with gated=0 is reported as MEASURE-ONLY (no correctness signal yet), never as "saving".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .offload_telemetry import DEFAULT_OFFLOAD_LOG, aggregate_offload

# codeqa keeps its own two intact logs (different schemas); the weekly report READS them so all
# local-model activity shows in one place, without rewiring codeqa's runtime.
CODEQA_IMPACT_LOG = Path.home() / ".apex" / "codeqa_impact.jsonl"
CODEQA_VALIDATE_LOG = Path.home() / ".codeqa" / "validate_metrics.jsonl"


def _iter_jsonl(path: Path):
    try:
        text = Path(path).read_text(errors="replace")
    except Exception:  # noqa: BLE001
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(d, dict):
            yield d


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def summarize_codeqa_impact(path: Path = CODEQA_IMPACT_LOG) -> dict:
    """codeqa `ask` grounding + token summary (from codeqa_impact.jsonl)."""
    n = grounded = stale = hallucinated = pt = ct = 0
    for d in _iter_jsonl(path):
        n += 1
        g = d.get("grounding") or {}
        grounded += _int(g.get("grounded"))
        stale += _int(g.get("stale"))
        hallucinated += _int(g.get("hallucinated"))
        pt += _int(d.get("prompt_tokens"))
        ct += _int(d.get("cached_tokens"))
    return {"n_questions": n, "grounded": grounded, "stale": stale,
            "hallucinated": hallucinated, "prompt_tokens": pt, "cached_tokens": ct}


def summarize_codeqa_validate(path: Path = CODEQA_VALIDATE_LOG) -> dict:
    """codeqa `validate` local-vs-frontier routing summary (from validate_metrics.jsonl).

    local_share = fraction of routed verifier calls kept on the FREE local model (the frontier
    tokens codeqa's routing avoided)."""
    n_local = n_frontier = n_struck = est_frontier = 0
    for d in _iter_jsonl(path):
        n_local += _int(d.get("n_local"))
        n_frontier += _int(d.get("n_frontier"))
        n_struck += _int(d.get("n_struck"))
        est_frontier += _int(d.get("est_frontier_tokens"))
    routed = n_local + n_frontier
    return {"n_local": n_local, "n_frontier": n_frontier, "n_struck": n_struck,
            "est_frontier_tokens": est_frontier,
            "local_share": (n_local / routed) if routed else None}


def format_lane_verdict(lane: dict) -> str:
    """Honest per-lane verdict (spec F2). The `frontier_completion_tokens_saved` field is a GROSS
    proxy — the LOCAL model's completion tokens on gated+ok calls — NOT a net saving: it does not
    subtract escalation waste (local tokens spent on calls that still escalated), the frontier prompt
    overhead of consuming a failed attempt, verifier/cross-validation tokens, or latency. So we NEVER
    label it "NET-POSITIVE"; a positive gross with an unknown net is "GROSS-POSITIVE (net unknown)",
    and the escalation waste is surfaced alongside it, not hidden."""
    gross = lane.get("frontier_completion_tokens_saved", 0)
    waste = lane.get("escalated_completion_tokens", 0)
    rate = lane.get("ok_rate")
    rate_s = "n/a" if rate is None else f"{rate*100:.0f}%"
    if lane.get("gated", 0) == 0:
        label = "MEASURE-ONLY (no gate)"
    elif gross <= 0:
        label = "NO-GROSS-GAIN"
    elif (rate or 0) >= 0.5:
        label = "GROSS-POSITIVE (net unknown — no matched frontier baseline)"
    else:
        label = "GROSS-POSITIVE but WEAK (low ok_rate)"
    return (f"{label} — gross_avoided={gross} tok, escalation_waste={waste} tok, ok_rate={rate_s}")


def format_report(agg: dict) -> str:
    o = agg["overall"]
    lines = [
        "OFFLOAD ECONOMICS (measure-first; 'saved' is GROSS, not net — see per-lane note)",
        f"  total calls={o['n']}  gated={o.get('gated', 0)}  ok={o['ok']}  escalated={o['escalated']}",
        "",
        f"  {'lane':12s} {'n':>5s} {'gated':>6s} {'gross_tok':>10s} {'waste_tok':>10s}  verdict",
    ]
    for lane, L in sorted(agg["by_lane"].items()):
        gross = L["frontier_completion_tokens_saved"]
        waste = L.get("escalated_completion_tokens", 0)
        lines.append(
            f"  {lane:12s} {L['n']:>5d} {L.get('gated', 0):>6d} "
            f"{gross:>10d} {waste:>10d}  {format_lane_verdict(L)}")

    # codeqa's own two subsystems, read in (not rewired).
    imp = summarize_codeqa_impact()
    val = summarize_codeqa_validate()
    lines += [
        "",
        "CODEQA (separate subsystem, read-in)",
        f"  ask:      {imp['n_questions']} questions  grounded={imp['grounded']} "
        f"stale={imp['stale']} hallucinated={imp['hallucinated']}  "
        f"prompt_tok={imp['prompt_tokens']} cached={imp['cached_tokens']}",
    ]
    ls = val["local_share"]
    ls_s = "n/a" if ls is None else f"{ls*100:.0f}%"
    lines.append(
        f"  validate: {val['n_local']} local + {val['n_frontier']} frontier verifier calls  "
        f"(local_share={ls_s})  struck={val['n_struck']}  est_frontier_tok={val['est_frontier_tokens']}")
    if imp["n_questions"] == 0 and val["n_local"] == 0:
        lines.append("  (codeqa idle — no `apex ask`/`validate` activity in the logs)")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_OFFLOAD_LOG
    agg = aggregate_offload(path)
    print(format_report(agg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
