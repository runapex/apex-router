"""Per-cell cost advice for pi sub-agent jobs (A2, advice only).

Reads ``~/.apex-router/labels/agent_runs.jsonl`` and gives the p75 and p90 of the cost of DONE jobs for
one (agent, model, thinking) cell, with n. A cell with n < MIN_N gives no advice.

There is no pooled threshold on purpose: the pooled predictor is a confound (the high-thinking cell goes
the other way). Advice never changes max_cost or the model; the caller only shows it.

Usage:  python -m apex_router.cost_advice --agent worker --model M --thinking T [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

MIN_N = 8


def runs_path() -> Path:
    home = Path(os.environ.get("APEX_HOME", Path.home() / ".apex-router"))
    return home / "labels" / "agent_runs.jsonl"


def _pct(xs: list[float], q: float) -> float:
    """Linear-interpolation percentile (numpy 'linear'); xs is sorted and not empty."""
    k = (len(xs) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def done_costs(rows, agent: str, model: str, thinking: str) -> list[float]:
    """Costs of done jobs in exactly this cell. A done job flagged capped is left out (its cost is cut by the cap)."""
    out = []
    for r in rows:
        if not isinstance(r, dict) or r.get("state") != "done" or r.get("capped") is True:
            continue
        if (r.get("agent"), r.get("model"), r.get("thinking")) != (agent, model, thinking):
            continue
        c = r.get("cost_usd")
        if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0:
            out.append(float(c))
    return sorted(out)


def read_rows(path: Path | None = None) -> list[dict]:
    p = path or runs_path()
    rows = []
    try:
        with p.open() as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def advice(agent: str, model: str, thinking: str, rows=None, max_cost: float | None = None) -> dict:
    costs = done_costs(read_rows() if rows is None else rows, agent, model, thinking)
    out: dict = {"agent": agent, "model": model, "thinking": thinking, "n": len(costs), "min_n": MIN_N}
    if len(costs) < MIN_N:
        out.update(advice=False, p75=None, p90=None)
        return out
    out.update(advice=True, p75=round(_pct(costs, 0.75), 4), p90=round(_pct(costs, 0.90), 4))
    if max_cost is not None:
        out["below_p75"] = max_cost < out["p75"]
    return out


def line(a: dict) -> str:
    cell = f"{a['agent']}/{a['model']}/{a['thinking']}"
    if not a["advice"]:
        return f"cost advice {cell}: no advice (n={a['n']} < {a['min_n']})"
    return f"cost advice {cell}: done-cost p75 ${a['p75']:.2f}, p90 ${a['p90']:.2f} (n={a['n']})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m apex_router.cost_advice", description="Per-cell done-cost p75/p90 (advice only).")
    ap.add_argument("--agent", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--thinking", required=True)
    ap.add_argument("--max-cost", type=float, default=None, help="flag when this budget is below the cell p75")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)
    a = advice(ns.agent, ns.model, ns.thinking, max_cost=ns.max_cost)
    print(json.dumps(a, sort_keys=True) if ns.json else line(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
