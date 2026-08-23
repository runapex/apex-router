"""WP5 — offline swapped-order replay (de-confound order vs tier).

Live chains are observational and FIXED-order, so tier effect is confounded with
position. This module replays recorded stage inputs through tiers in SWAPPED order
(or parallel) on identical inputs — the experimental arm — and emits SC2 rows tagged
`replay:true` + `order`, which the gate consumes like any cell. Only replay rows may
back a tier-effect claim; live rows never can (enforced by the `replay` flag).

Payloads (inputs/outputs) are logged separately from the minimal SC1 chain log.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .chain_reward import judge_pair, cosine_pregate, topic_id


def default_payload_log() -> Path:
    env = os.environ.get("APEX_CHAIN_PAYLOADS")
    return Path(env) if env else Path.home() / ".apex-router" / "chain_payloads.jsonl"


class PayloadLogger:
    """Records stage input/output text so tiers can be replayed offline. Fail-open."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else default_payload_log()

    def log(self, chain_id: str, slot: str, input_text: str, output_text: str) -> bool:
        rec = {"chain_id": chain_id, "slot": slot, "input": input_text, "output": output_text,
               "ts": datetime.now(timezone.utc).isoformat()}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            return True
        except OSError as exc:
            print(f"chain_replay: payload log failed (non-fatal): {exc}", file=sys.stderr)
            return False


def replay_order(base_input: str, ordered_stages: list, task_class: str, *, call_fn, judge_fn,
                 embed_fn=None, order_label: str | None = None) -> list[dict]:
    """Re-run `ordered_stages` (list of (slot, model)) on `base_input`, feeding each
    output into the next, and emit SC2 rows (arm=candidate vs prior stage) tagged
    replay:true + order. call_fn(model, prompt) -> {"output","usage","cost_usd"}.
    """
    order_label = order_label or ">".join(s for s, _ in ordered_stages)
    tid = topic_id(base_input)
    prior_out, rows = "", []
    for i, (slot, model) in enumerate(ordered_stages):
        prompt = base_input if i == 0 else f"{base_input}\n\nPrevious:\n{prior_out}"
        res = call_fn(model, prompt)
        out = res.get("output", "") if isinstance(res, dict) else ""
        if i >= 1:
            reward = 0.0 if cosine_pregate(prior_out, out, embed_fn=embed_fn) \
                else judge_pair(prior_out, out, judge_fn=judge_fn)
            rows.append({
                "cell_id": f"{slot}:{task_class}", "model": model, "arm": "candidate",
                "reward": float(reward),
                "prompt_tokens": (res.get("usage", {}) or {}).get("prompt_tokens", 0) if isinstance(res, dict) else 0,
                "completion_tokens": (res.get("usage", {}) or {}).get("completion_tokens", 0) if isinstance(res, dict) else 0,
                "cost_usd": res.get("cost_usd", 0.0) if isinstance(res, dict) else 0.0,
                "wall_ms": 0, "chain_id": f"replay:{order_label}:{tid}", "topic_id": tid,
                "replay": True, "order": order_label,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        prior_out = out
    return rows


def replay_both_orders(base_input: str, stage_a, stage_b, task_class: str, *, call_fn, judge_fn,
                       embed_fn=None) -> list[dict]:
    """Run (A→B) and (B→A) on the SAME input to break the order↔tier confound.
    stage_a/stage_b are (slot, model) tuples."""
    rows = replay_order(base_input, [stage_a, stage_b], task_class, call_fn=call_fn,
                        judge_fn=judge_fn, embed_fn=embed_fn, order_label="AB")
    rows += replay_order(base_input, [stage_b, stage_a], task_class, call_fn=call_fn,
                        judge_fn=judge_fn, embed_fn=embed_fn, order_label="BA")
    return rows
