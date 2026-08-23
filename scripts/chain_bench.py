#!/usr/bin/env python3
"""WP3 — bench-cell integration for learning chains.

Maps SC2 reward rows (WP2) into the EXISTING promotion gate (gate.run_gate) without
changing gate/bench signatures. A chain reward is already a paired delta (stage k vs
stage k-1's output as the incumbent baseline), so each cell = f"{slot}:{task_class}"
feeds the gate directly:
  - candidate model = the stage's model; incumbent = the pseudo "prior-stage" baseline;
  - each chain_id is a capture WINDOW (replication);
  - chains are split deterministically into promotion vs confirmation (out-of-sample);
  - provenance="judge" (2x floor), FDR applied across all cells JOINTLY by run_gate.

Aggregates rendered for the planner rationale (WP4):
  (1) mean paired Δreward + cluster-bootstrap CI (clustered by chain_id),
  (2) cost per unit Δ, (3) gate verdict -> ON | OFFERED | SKIP.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from apex_router.gate import CellEvidence, run_gate  # noqa: E402

INCUMBENT = "prior-stage"
SKIP_EPS = 0.02          # |mean| below this with a tight CI => the slot doesn't earn its cost
MIN_SKIP_TOPICS = 2      # never SKIP on fewer than this many distinct confirm topics (Kimi reconcile)
NO_TOPIC = "__no_topic__"  # missing topic_id collapses to ONE conservative cluster, never per-chain


def _split(chain_id: str) -> str:
    """Deterministic disjoint promotion/confirmation split by chain_id (out-of-sample)."""
    h = int(hashlib.sha256(str(chain_id).encode()).hexdigest(), 16)
    return "promo" if h % 2 == 0 else "confirm"


def cluster_bootstrap(values_by_chain: dict, *, n: int = 2000, seed: int = 7):
    """Cluster (block) bootstrap by chain_id: resample WHOLE chains, so rows sharing a
    chain_id always move together — never resampled independently. Returns (mean, lo, hi)."""
    chains = list(values_by_chain)
    flat = [v for vs in values_by_chain.values() for v in vs]
    if not flat:
        return 0.0, 0.0, 0.0
    mean = sum(flat) / len(flat)
    if len(chains) < 2:
        return mean, mean, mean
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        pick = [values_by_chain[chains[rng.randrange(len(chains))]] for _ in chains]
        vals = [v for vs in pick for v in vs]
        means.append(sum(vals) / len(vals))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    return mean, lo, hi


def _cell_evidence(cell_id: str, rows: list[dict]) -> CellEvidence:
    task_class = cell_id.split(":", 1)[-1]
    promo, confirm = defaultdict(list), defaultdict(list)
    cwin = defaultdict(set)
    cost_by_model, n_by_model = defaultdict(float), defaultdict(int)
    for r in rows:
        model = r.get("model") or "unknown"
        reward = float(r.get("reward", 0.0))
        chain_id = r.get("chain_id") or "c?"
        if _split(chain_id) == "promo":
            promo[model].append(reward)
        else:
            confirm[model].append(reward)
            cwin[model].add(str(chain_id))
        cost_by_model[model] += float(r.get("cost_usd", 0.0))
        n_by_model[model] += 1
    cost = {m: (cost_by_model[m] / n_by_model[m]) for m in n_by_model if n_by_model[m]}
    return CellEvidence(
        cell_id=cell_id, parent_task_type=task_class, incumbent_model=INCUMBENT,
        promo_deltas=dict(promo), confirm_deltas=dict(confirm),
        confirm_windows={m: set(cwin[m]) for m in cwin}, provenance="judge", cost=cost,
    )


def analyze(rows: list[dict], *, k: int = 2, m_windows: int = 2, alpha: float = 0.05,
            previously_promoted: dict | None = None) -> list[dict]:
    by_cell: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cell[r["cell_id"]].append(r)

    cells = [_cell_evidence(cid, rws) for cid, rws in by_cell.items()]
    results = {g.cell_id: g for g in run_gate(cells, k=k, m_windows=m_windows, alpha=alpha,
                                              previously_promoted=previously_promoted)}
    out = []
    for cid, rws in sorted(by_cell.items()):
        # DEFECT 2: the rendered CI and the SKIP decision must use the SAME out-of-sample
        # estimand as the gate -> CONFIRM-split rows only (pooling promo inflates precision).
        # DEFECT 3: cluster by topic_id (not chain_id) so re-chaining the same topic can't
        # masquerade as independent replication (pseudo-replication).
        vbc = defaultdict(list)
        cost_sum = n = 0.0
        for r in rws:
            if _split(r.get("chain_id") or "c?") != "confirm":
                continue
            # Missing topic_id must NOT fall back to chain_id (that reopens pseudo-
            # replication); collapse all such rows into ONE conservative cluster.
            key = r.get("topic_id") or NO_TOPIC
            vbc[key].append(float(r.get("reward", 0.0)))
            cost_sum += float(r.get("cost_usd", 0.0)); n += 1
        mean, lo, hi = cluster_bootstrap(vbc)
        gr = results.get(cid)
        promoted = bool(gr and gr.promoted)
        if promoted:
            verdict = "ON"
        elif len(vbc) < MIN_SKIP_TOPICS:  # no / too-few out-of-sample topics -> can't SKIP
            verdict = "OFFERED"           # (a 1-topic CI is degenerate; never SKIP on it)
        elif hi <= SKIP_EPS:              # tight confirm-only CI at/near zero -> doesn't pay off
            verdict = "SKIP"
        else:
            verdict = "OFFERED"           # positive but not FDR-confirmed out-of-sample
        # NOTE: confirm-PHASE cost per delta (excludes promo/exploration spend).
        cost_per_delta_confirm = (cost_sum / n / mean) if (n and mean > 1e-9) else None
        out.append({"cell_id": cid, "mean_delta": round(mean, 4),
                    "ci": [round(lo, 4), round(hi, 4)], "cost_per_delta_confirm": cost_per_delta_confirm,
                    "verdict": verdict, "n": int(n), "n_topics": len(vbc)})
    return out


def _load_rows(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="chain_bench")
    p.add_argument("--rows", required=True, help="SC2 reward rows JSONL")
    p.add_argument("--task-class")
    p.add_argument("--slot")
    p.add_argument("-k", type=int, default=2)
    p.add_argument("--m-windows", type=int, default=2)
    a = p.parse_args(argv)
    rows = _load_rows(Path(a.rows))
    if a.task_class:
        rows = [r for r in rows if str(r.get("cell_id", "")).endswith(f":{a.task_class}")]
    if a.slot:
        rows = [r for r in rows if str(r.get("cell_id", "")).startswith(f"{a.slot}:")]
    for res in analyze(rows, k=a.k, m_windows=a.m_windows):
        ci = res["ci"]
        print(f"{res['verdict']:8} {res['cell_id']:30} Δ={res['mean_delta']:+.3f} "
              f"CI[{ci[0]:+.3f},{ci[1]:+.3f}] n={res['n']} chains={res['n_chains']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
