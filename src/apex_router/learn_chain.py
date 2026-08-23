"""WP1 — learning-chain instrumentation (SC1).

Fail-open JSONL logger + a transport-agnostic chain orchestrator for the
retrieve→sonnet→opus→kimi study chain. Mirrors route_log.py's discipline: a write
error logs to stderr and returns, NEVER raises — an instrument must not break the tool.

SC1 record schema (FROZEN — all writers go through ChainLogger):
  {"kind":"chain", chain_id, task_class, proposed:[slot...], executed:[slot...],
                    edited:bool, shown_rationale:bool, exploration:bool, ts}
  {"kind":"stage", chain_id, slot, model, prompt_tokens, completion_tokens,
                    cached_tokens, cost_usd, wall_ms, input_hash, output_hash, ts}
Reward is NEVER written here (WP2 computes it post-hoc).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def default_log_path() -> Path:
    env = os.environ.get("APEX_LEARN_CHAIN_LOG")
    if env:
        return Path(env)
    return Path.home() / ".apex-router" / "learn_chain.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]


@dataclass(frozen=True)
class Slot:
    """One ordered stage: a named slot bound to a concrete model id."""
    slot: str          # retrieve | draft | deepen | synthesize (or the family name)
    model: str         # e.g. "anthropic/claude-sonnet-4-6", "local/…", "moonshotai/kimi-k3"


class ChainLogger:
    """Fail-open writer for the two SC1 record kinds."""

    def __init__(self, log_path: Path | None = None):
        self.log_path = Path(log_path) if log_path else default_log_path()

    def _append(self, record: dict) -> bool:
        try:
            line = json.dumps(record, allow_nan=False) + "\n"
        except (TypeError, ValueError) as exc:
            print(f"learn_chain: undumpable record dropped: {exc}", file=sys.stderr)
            return False
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line)
            return True
        except OSError as exc:
            print(f"learn_chain: log append failed (non-fatal): {exc}", file=sys.stderr)
            return False

    def log_chain(self, chain_id, task_class, proposed, executed, *,
                  edited=False, shown_rationale=False, exploration=False, ts=None) -> bool:
        return self._append({
            "kind": "chain", "chain_id": chain_id, "task_class": task_class,
            "proposed": [s.slot if isinstance(s, Slot) else s for s in proposed],
            "executed": [s.slot if isinstance(s, Slot) else s for s in executed],
            "edited": bool(edited), "shown_rationale": bool(shown_rationale),
            "exploration": bool(exploration), "ts": ts or _now_iso(),
        })

    def log_stage(self, chain_id, slot, model, usage: dict, wall_ms, cost_usd, *,
                  input_text="", output_text="", ts=None) -> bool:
        def _int(v):
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
        usage = usage or {}
        return self._append({
            "kind": "stage", "chain_id": chain_id, "slot": slot, "model": model,
            "prompt_tokens": _int(usage.get("prompt_tokens")),
            "completion_tokens": _int(usage.get("completion_tokens")),
            "cached_tokens": _int(usage.get("cached_tokens")),
            "cost_usd": float(cost_usd) if isinstance(cost_usd, (int, float)) else 0.0,
            "wall_ms": int(wall_ms) if isinstance(wall_ms, (int, float)) else 0,
            "input_hash": _hash(input_text), "output_hash": _hash(output_text),
            "ts": ts or _now_iso(),
        })


def new_chain_id() -> str:
    return f"ch-{int(time.time()*1000):x}-{os.getpid():x}"


def run_chain(task: str, task_class: str, slots: list[Slot], call_fn, *,
              proposed: list[Slot] | None = None, edited: bool = False,
              shown_rationale: bool = False, exploration: bool = False,
              logger: ChainLogger | None = None, chain_id: str | None = None) -> str:
    """Execute slots in fixed order, feeding each stage's output into the next.

    `call_fn(model, prompt) -> dict` must return {"output": str, "usage": {...},
    "cost_usd": float}. A raising call_fn (e.g. network death) stops the chain but the
    stages already completed remain logged (fail-open, per the acceptance test).
    `proposed` defaults to `slots` (nothing edited).
    """
    logger = logger or ChainLogger()
    chain_id = chain_id or new_chain_id()
    proposed = proposed if proposed is not None else list(slots)
    executed: list[Slot] = []
    prior_output = ""
    try:
        for s in slots:
            prompt = _build_prompt(task, s.slot, prior_output)
            t0 = time.perf_counter()
            res = call_fn(s.model, prompt)  # may raise — we log completed stages in finally
            wall_ms = (time.perf_counter() - t0) * 1000.0
            output = res.get("output", "") if isinstance(res, dict) else ""
            logger.log_stage(chain_id, s.slot, s.model, res.get("usage", {}) if isinstance(res, dict) else {},
                             wall_ms, res.get("cost_usd", 0.0) if isinstance(res, dict) else 0.0,
                             input_text=prompt, output_text=output)
            executed.append(s)
            prior_output = output
    finally:
        logger.log_chain(chain_id, task_class, proposed, executed,
                         edited=edited, shown_rationale=shown_rationale, exploration=exploration)
    return chain_id


def _build_prompt(task: str, slot: str, prior_output: str) -> str:
    if not prior_output:
        return f"[{slot}] Task: {task}"
    return f"[{slot}] Task: {task}\n\nBuild on the previous stage's output:\n{prior_output}"
