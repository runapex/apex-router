"""§7 per-venue route-table emitter + reader — the only artifact the consumers read.

Regenerated from gate results + per-cell rankings after each gated bench. The table maps
each cell to a chosen model; unpromoted / uncertain cells fall back to the parent
task-type's safe/heavy default (CANNOT-DECIDE, §6).

The §5.4 quality+cost objective is NOT re-derived here. The GATE owns it (that's where
the PAIRED per-step deltas live, so the cost tiebreak among quality-tied candidates is a
sound paired comparison — see amr.gate). A prior version re-derived a cost choice from
MARGINAL Wilson CIs, which could route to a cheaper-but-credibly-worse model (Codex
route-table #2). This module now just EMITS the gate's decision and validates it is
supported by the ranking.

Consumers only READ this table (they never write reward or mutate it, finding #16).
When the table is missing / malformed / a cell is absent / a cell is unpromoted /
ambiguous, the reader returns the parent task-type default — a strict superset of today's
static behavior, and it FAILS TO THE DEFAULT on any surprise (never routes on a wrong or
stale value).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import stats

SCHEMA_VERSION = 1


def build_ranking(model_stats: dict) -> list:
    """Build ranked ranking rows from per-model stats.

    model_stats: {model: (passes, n, cost_usd, latency)}. Quality is the pass-rate with a
    Wilson CI (§5.0). A model with n <= 0 has no evidence and is EXCLUDED (a zero-sample
    row must not crash wilson_ci or masquerade as a rankable model, Codex #9). Rows are
    sorted by quality descending with a deterministic tiebreak (cost, latency, name).
    """
    rows = []
    for model, (passes, n, cost_usd, latency) in model_stats.items():
        if n <= 0:
            continue                       # no evidence -> not rankable (Codex #9)
        lo, hi = stats.wilson_ci(passes, n)
        rows.append({
            "model": model,
            "quality": passes / n,
            "quality_ci": (lo, hi),
            "cost_usd": cost_usd,
            "latency": latency,
            "provenance": "objective",
            "n": n,
        })
    # NaN-safe sort key: non-finite quality/cost sink to the bottom deterministically.
    def _key(r):
        q = r["quality"]
        c = r["cost_usd"]
        q = q if isinstance(q, (int, float)) and math.isfinite(q) else -math.inf
        c = c if isinstance(c, (int, float)) and math.isfinite(c) else math.inf
        lat = r["latency"] if isinstance(r["latency"], (int, float)) and math.isfinite(r["latency"]) else math.inf
        return (-q, c, lat, str(r["model"]))
    rows.sort(key=_key)
    return rows


def emit_route_table(gate_results, rankings: dict, *, venue: str, generated_from: dict,
                     path=None) -> dict:
    """Assemble the per-venue route table from gate results + per-cell rankings.

    A promoted cell routes to the gate's chosen model — but ONLY if that model appears in
    the cell's ranking (an unsupported route is demoted to the parent default, Codex #5).
    An unpromoted / healed / CANNOT-DECIDE cell routes to its parent task-type default.
    The parent default must be a non-empty string (a healed row with an empty parent is
    rejected, Codex #5). If `path` is given the table is written as JSON.
    """
    cells = []
    dropped_routes = []
    for gr in gate_results:
        parent_default = gr.parent_task_type
        healed = bool(getattr(gr, "healed", False))

        # An absent-cell heal (run_gate emits these with an empty parent) has no cell to
        # route — it only signals "tear down this stale route". Record it as a dropped
        # route and DON'T reject the whole batch (Codex pass2 #4).
        if healed and not (isinstance(parent_default, str) and parent_default.strip()):
            dropped_routes.append(gr.cell_id)
            continue

        if not (isinstance(parent_default, str) and parent_default.strip()):
            raise ValueError(
                f"cell {gr.cell_id!r} has an empty/invalid parent_task_type "
                f"(fallback would be unroutable): {parent_default!r}")

        ranking = rankings.get(gr.cell_id, [])
        # Only NON-EMPTY model names count as supporting a route (Codex pass2 #5).
        ranked_models = {r["model"] for r in ranking
                         if isinstance(r.get("model"), str) and r["model"].strip()}

        promoted = gr.promoted is True     # strict: only a real bool True promotes
        chosen_ok = (isinstance(gr.chosen_model, str) and gr.chosen_model.strip()
                     and gr.chosen_model in ranked_models)
        if promoted and chosen_ok:
            chosen = gr.chosen_model
        else:
            promoted = False
            chosen = parent_default        # CANNOT-DECIDE / unsupported -> safe default

        cells.append({
            "cell_id": gr.cell_id,
            "parent_task_type": parent_default,
            "promoted": promoted,
            "healed": healed,
            "ranking": [_ranking_row(r) for r in ranking],
            "chosen_model": chosen,
            "confidence": _confidence(gr) if promoted else 0.0,
            "fallback_model": parent_default,
        })

    table = {
        "schema_version": SCHEMA_VERSION,
        "venue": venue,
        "generated_from": dict(generated_from),
        "cells": cells,
        "dropped_routes": dropped_routes,
    }
    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(table, indent=2, sort_keys=True))
    return table


def read_route(path, *, cell_id: str, parent_task_type: str) -> str:
    """Consumer read: return the chosen model for `cell_id`, else the parent task-type
    default (CANNOT-DECIDE). FAILS TO THE DEFAULT on any surprise — missing table,
    malformed shape, absent cell, unpromoted cell, non-bool `promoted`, empty chosen
    model, or a DUPLICATE cell id (ambiguous -> safe). A strict superset of today's
    static behavior.
    """
    try:
        p = Path(path)
    except TypeError:
        return parent_task_type            # None / invalid path type -> default (Codex pass2 #5)
    if not p.is_file():
        return parent_task_type
    try:
        table = json.loads(p.read_text())
    except (OSError, ValueError):
        return parent_task_type
    if not isinstance(table, dict):
        return parent_task_type
    cells = table.get("cells")
    if not isinstance(cells, list):
        return parent_task_type

    matches = [c for c in cells if isinstance(c, dict) and c.get("cell_id") == cell_id]
    if len(matches) != 1:
        return parent_task_type            # absent OR ambiguous duplicate -> safe default
    cell = matches[0]
    # Strict bool: "false"/1/None must NOT count as promoted (Codex #6).
    if cell.get("promoted") is True:
        chosen = cell.get("chosen_model")
        # The chosen model must be a non-empty string AND supported by the persisted
        # ranking (Codex pass2 #5) — a promoted-but-unranked route is not trusted.
        ranked = {r.get("model") for r in cell.get("ranking", [])
                  if isinstance(r, dict) and isinstance(r.get("model"), str) and r["model"].strip()}
        if isinstance(chosen, str) and chosen.strip() and chosen in ranked:
            return chosen
    return parent_task_type


def _ranking_row(r: dict) -> dict:
    # Persist CI as a list (JSON has no tuple); keep the field set the design names.
    lo, hi = r["quality_ci"]
    return {
        "model": r["model"],
        "quality": r["quality"],
        "quality_ci": [lo, hi],
        "cost_usd": r["cost_usd"],
        "latency": r["latency"],
        "provenance": r.get("provenance", "objective"),
        "n": r["n"],
    }


def _confidence(gate_result) -> float:
    """A promoted cell's confidence is 1 - p, bounded to [0,1]. A non-finite/invalid
    p-value yields 0.0 confidence (never max), so bad evidence can't read as certain
    (Codex #8)."""
    if not gate_result.promoted:
        return 0.0
    p = gate_result.pvalue
    if not isinstance(p, (int, float)) or not math.isfinite(p) or p < 0.0 or p > 1.0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - p))
