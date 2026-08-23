"""WP4 — classifier-suggested chain + adaptive confirm.

plan(task_class) turns the gate verdicts (WP3) into a proposed chain: ON slots are
included, SKIP slots dropped, OFFERED slots listed-not-run. Cold-start (no history)
falls back to a labeled default prior. ε-exploration occasionally re-includes ONE
dropped slot so its estimate can recover (marked exploration:true).

render_rationale() emits the design's exact "default with uncertainty" string, using
only the three aggregates — informs without commanding.

Guardrails: ε only ever RE-ADDS a SKIP slot, never removes an ON slot; edited chains
are excluded from estimates (enforced upstream via the proposed-only estimand).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

# Default chain (slot -> model). Retrieve is local + free and always included.
DEFAULT_CHAIN = [
    ("retrieve", "local/nomic-embed"),
    ("validate", "anthropic/claude-sonnet-4-6"),
    ("deepen", "anthropic/claude-opus-4-8"),
    ("synthesize", "moonshotai/kimi-k3"),
]
ALWAYS_ON = {"retrieve"}


@dataclass
class SlotDecision:
    slot: str
    model: str
    action: str                 # include | offer | drop
    verdict: str = "PRIOR"      # ON | OFFERED | SKIP | PRIOR (cold-start)
    mean_delta: float | None = None
    ci: list | None = None
    n_chains: int = 0
    exploration: bool = False
    note: str = ""


def plan(task_class: str, analyze_by_cell: dict, *, chain=DEFAULT_CHAIN, eps: float = 0.05,
         rng: random.Random | None = None) -> list[SlotDecision]:
    """analyze_by_cell: {cell_id -> analyze() result dict} from WP3. Returns ordered
    SlotDecisions. Only slots with action in {include, offer} are shown; include ones run."""
    rng = rng or random.Random()
    decisions: list[SlotDecision] = []
    for slot, model in chain:
        cell = f"{slot}:{task_class}"
        res = analyze_by_cell.get(cell)
        if slot in ALWAYS_ON:
            decisions.append(SlotDecision(slot, model, "include", "ON", note="grounding (always on)"))
            continue
        if res is None:
            decisions.append(SlotDecision(slot, model, "include", "PRIOR", note="no history yet (prior)"))
            continue
        v = res.get("verdict", "OFFERED")
        common = dict(verdict=v, mean_delta=res.get("mean_delta"), ci=res.get("ci"),
                      n_chains=res.get("n_chains", 0))
        if v == "ON":
            decisions.append(SlotDecision(slot, model, "include", **common))
        elif v == "SKIP":
            decisions.append(SlotDecision(slot, model, "drop", **common))
        else:  # OFFERED
            decisions.append(SlotDecision(slot, model, "offer", **common))

    # ε-exploration: with prob eps, re-include ONE dropped slot (never touch ON slots).
    dropped = [d for d in decisions if d.action == "drop"]
    if dropped and rng.random() < eps:
        pick = rng.choice(dropped)
        pick.action = "include"
        pick.exploration = True
        pick.note = "ε-exploration (re-testing a skipped slot)"
    return decisions


def proposed_slots(decisions: list[SlotDecision]) -> list[str]:
    return [d.slot for d in decisions if d.action == "include"]


def render_rationale(task_class: str, decisions: list[SlotDecision], *,
                     est_cost: float | None = None, est_latency_s: float | None = None) -> str:
    run = [d for d in decisions if d.action == "include"]
    plan_str = "→".join(d.slot for d in run)
    head = f"Planned: {plan_str}"
    if est_cost is not None or est_latency_s is not None:
        head += f" (~${est_cost:.2f}, ~{est_latency_s:.0f}s)" if est_cost is not None else ""
    bits = []
    for d in decisions:
        if d.verdict == "PRIOR":
            bits.append(f"{d.slot}: no history yet (prior)")
        elif d.slot in ALWAYS_ON:
            continue
        elif d.mean_delta is not None and d.ci:
            tag = {"ON": "", "OFFERED": " (offered)", "SKIP": " below gate"}.get(d.verdict, "")
            expl = " [ε-exploration]" if d.exploration else ""
            bits.append(f"{d.slot} {d.mean_delta:+.2f} Δreward (CI {d.ci[0]:+.2f}–{d.ci[1]:+.2f})"
                        f"{tag}{expl} — n={d.n_chains}")
    offered = [d.slot for d in decisions if d.action == "offer"]
    basis = "Basis: " + "; ".join(bits) if bits else "Basis: cold-start default"
    tail = f" {', '.join(offered)} available if wanted." if offered else ""
    return f"{head}. {basis}.{tail}"


def _cli(argv=None) -> int:
    import argparse, json, sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
    from chain_bench import analyze, _load_rows  # noqa

    p = argparse.ArgumentParser(prog="chain-planner")
    p.add_argument("--task-class", required=True)
    p.add_argument("--rows", help="SC2 reward rows JSONL (omit for cold-start)")
    p.add_argument("--eps", type=float, default=0.05)
    a = p.parse_args(argv)
    by_cell = {}
    if a.rows and Path(a.rows).exists():
        by_cell = {r["cell_id"]: r for r in analyze(_load_rows(Path(a.rows)))}
    decisions = plan(a.task_class, by_cell, eps=a.eps)
    print(render_rationale(a.task_class, decisions))
    print(json.dumps({"proposed": proposed_slots(decisions),
                      "decisions": [d.__dict__ for d in decisions]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
