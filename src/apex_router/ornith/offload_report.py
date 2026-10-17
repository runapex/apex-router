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


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def dollar_summary(log_path: Path, verdict_path: Path) -> dict:
    """Per-backend $ view of `apex-router ask` calls (rows with a call_id), joined with verdicts.

    spent_usd              every call's cost, failures included (a billed failure is still spend)
    claude_equiv_used_usd  baseline_cost_usd of USED answers: what the Claude tier would have charged
                           to write that output
    gross_saving_used_usd  claude_equiv_used - the cost of those used calls
    waste_discarded_usd    cost of answers the role discarded (it did the work anyway)
    pending_usd            cost of answered calls with no verdict yet
    net_after_remote_usd   claude_equiv_used - spent (all remote spend charged against the used
                           answers). Still not a true net: the Claude tokens spent reading and
                           checking each answer are not measured, hence "net unknown".
    The last verdict booked for a call wins."""
    verdicts: dict[str, str] = {}
    for d in _iter_jsonl(verdict_path):
        cid, v = d.get("call_id"), d.get("verdict")
        if isinstance(cid, str) and v in ("used", "discarded"):
            verdicts[cid] = v
    out: dict[str, dict] = {}
    seen: set[str] = set()
    for d in _iter_jsonl(log_path):
        cid = d.get("call_id")
        if not isinstance(cid, str) or "verdict" in d:   # not an ask call (or a stray verdict row)
            continue
        backend = d.get("backend") if isinstance(d.get("backend"), str) else "unknown"
        S = out.setdefault(backend, {"calls": 0, "answered": 0, "refused": 0, "failed": 0,
                                     "used": 0, "discarded": 0, "pending": 0, "spent_usd": 0.0,
                                     "claude_equiv_used_usd": 0.0, "gross_saving_used_usd": 0.0,
                                     "waste_discarded_usd": 0.0, "pending_usd": 0.0,
                                     "unknown_cost": 0, "duplicates": 0})
        if cid in seen:                                   # a call counts once (first row wins)
            S["duplicates"] += 1
            continue
        seen.add(cid)
        S["calls"] += 1
        if d.get("cost_known") is False:
            S["unknown_cost"] += 1
        cost = _num(d.get("cost_usd"))
        S["spent_usd"] += cost
        if d.get("ok") is not True:
            S["refused" if d.get("refused") else "failed"] += 1
            continue
        S["answered"] += 1
        v = verdicts.get(cid)
        if v == "used":
            S["used"] += 1
            base = _num(d.get("baseline_cost_usd"))
            S["claude_equiv_used_usd"] += base
            S["gross_saving_used_usd"] += base - cost
        elif v == "discarded":
            S["discarded"] += 1
            S["waste_discarded_usd"] += cost
        else:
            S["pending"] += 1
            S["pending_usd"] += cost
    for S in out.values():
        S["net_after_remote_usd"] = S["claude_equiv_used_usd"] - S["spent_usd"]
    return out


def format_dollars(summary: dict) -> str:
    lines = ["REMOTE/LOCAL ASK ($; 'saving' is GROSS: Claude read/check tokens not counted -> net unknown)"]
    if not summary:
        lines.append("  (no `apex-router ask` calls in the log)")
        return "\n".join(lines)
    for backend, S in sorted(summary.items()):
        lines.append(
            f"  {backend:9s} calls={S['calls']} answered={S['answered']} refused={S['refused']} "
            f"failed={S['failed']}  used={S['used']} discarded={S['discarded']} pending={S['pending']}")
        lines.append(
            f"  {'':9s} spent=${S['spent_usd']:.4f}  claude_equiv(used)=${S['claude_equiv_used_usd']:.4f}  "
            f"gross_saving(used)=${S['gross_saving_used_usd']:.4f}  waste(discarded)=${S['waste_discarded_usd']:.4f}  "
            f"pending=${S['pending_usd']:.4f}  after_remote=${S['net_after_remote_usd']:.4f} (net unknown)")
        if S.get("unknown_cost"):
            lines.append(f"  {'':9s} spend incomplete: {S['unknown_cost']} call(s) may have billed with no usage "
                         "returned; spent and after_remote are lower bounds")
        if S.get("duplicates"):
            lines.append(f"  {'':9s} {S['duplicates']} duplicate call row(s) ignored")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_OFFLOAD_LOG
    agg = aggregate_offload(path)
    print(format_report(agg))
    return 0


def main_cli(argv: list[str]) -> int:
    """`apex-router offload-report [--log PATH] [--verdicts PATH] [--json]`: tokens, then $."""
    import argparse
    ap = argparse.ArgumentParser(prog="apex-router offload-report",
                                 description="Per-lane offload economics, then the $ view of `ask` calls.")
    ap.add_argument("--log", type=Path, default=None)
    ap.add_argument("--verdicts", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    log = a.log or Path.home() / ".apex" / "offload_telemetry.jsonl"
    verd = a.verdicts or Path.home() / ".apex" / "offload_verdicts.jsonl"
    if log.resolve() == verd.resolve():
        print("apex-router offload-report: the verdicts file must not be the offload log", file=sys.stderr)
        return 2
    agg, dollars = aggregate_offload(log), dollar_summary(log, verd)
    if a.json:
        print(json.dumps({"tokens": agg, "dollars": dollars}, indent=2))
    else:
        print(format_report(agg))
        print()
        print(format_dollars(dollars))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
