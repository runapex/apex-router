"""WP2 — post-hoc reward for learning chains (SC2 bench rows).

Marginal value of a stage = a signed, POSITION-SWAPPED pairwise judge of its output
vs the previous stage's output. Cosine (embed.py) is only a cheap "did anything
change" pre-gate — never the reward (it measures change, not improvement, and is
biased toward longer higher-tier outputs).

Reward is computed here, post-hoc, and never stored in the chain log — so the reward
definition stays versionable and the log minimal.

SC2 bench row (frozen; WP3 adapts these into bench.py's paired-replay rows):
  {cell_id, model, arm, reward, prompt_tokens, completion_tokens, cached_tokens,
   cost_usd, wall_ms, chain_id, topic_id, ts}
  cell_id = f"{slot}:{task_class}";  arm ∈ {"incumbent","candidate"}.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .embed import embed as _embed, cosine as _cosine

# Pinned, frozen judge — one constant, overridable only by env for ops, never per-call.
PINNED_JUDGE = os.environ.get("CHAIN_JUDGE_MODEL", "anthropic/claude-haiku-4-5")
COSINE_SKIP = float(os.environ.get("CHAIN_COSINE_SKIP", "0.995"))
_RUBRIC_PATH = Path(__file__).resolve().parent / "prompts" / "judge_rubric.md"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def topic_id(task_text: str) -> str:
    """Stable id for pseudo-replication clustering: normalize whitespace/case, hash."""
    norm = re.sub(r"\s+", " ", (task_text or "").strip().lower())
    return hashlib.sha256(norm.encode("utf-8", "ignore")).hexdigest()[:16]


def load_rubric() -> str:
    try:
        return _RUBRIC_PATH.read_text(encoding="utf-8")
    except OSError:
        return "Judge whether OUTPUT B is a better answer than OUTPUT A for the task."


def cosine_pregate(prev: str, cur: str, *, embed_fn=None) -> bool:
    """True => skip the judge (outputs are ~identical). embed_fn injectable for tests."""
    if not prev or not cur:
        return False
    embed_fn = embed_fn or _embed
    try:
        va, vb = embed_fn(prev[:1900]), embed_fn(cur[:1900])
        return abs(_cosine(va, vb)) >= COSINE_SKIP
    except Exception:  # noqa: BLE001 — a pre-gate failure must not block reward
        return False


def _judge_prompt(rubric: str, a: str, b: str) -> str:
    return (f"{rubric}\n\nOUTPUT A:\n{a[:4000]}\n\nOUTPUT B:\n{b[:4000]}\n\n"
            "Return only a number in [-1,1]: how much better B is than A "
            "(positive = B better, negative = A better).")


def judge_pair(prev_out: str, cur_out: str, *, judge_fn, rubric: str | None = None,
               judge_model: str = PINNED_JUDGE) -> float:
    """Signed marginal value of cur over prev, POSITION-SWAPPED to cancel order bias.

    judge_fn(model, prompt) -> float in [-1,1] (cur-vs-prev oriented). Called twice with
    A/B swapped; reward = (s1 - s2)/2 so the shared B-position bias cancels and the true
    cur-over-prev advantage survives. Acceptance: (+0.4,-0.2)->0.3.
    """
    rubric = rubric or load_rubric()
    s1 = float(judge_fn(judge_model, _judge_prompt(rubric, prev_out, cur_out)))   # A=prev,B=cur => truth+bias
    s2 = float(judge_fn(judge_model, _judge_prompt(rubric, cur_out, prev_out)))   # A=cur, B=prev => -truth+bias
    # DEBIAS: (s1 - s2)/2 = truth; (s1 + s2)/2 would return the position BIAS and, for an
    # antisymmetric judge, ZERO the signal (Fable/sonnet review DEFECT 1).
    return max(-1.0, min(1.0, (s1 - s2) / 2.0))


def _stage_row(stage: dict, task_class: str, reward: float) -> dict:
    return {
        "cell_id": f"{stage.get('slot')}:{task_class}",
        "model": stage.get("model"),
        "arm": "candidate",
        "reward": float(reward),
        "prompt_tokens": stage.get("prompt_tokens", 0),
        "completion_tokens": stage.get("completion_tokens", 0),
        "cached_tokens": stage.get("cached_tokens", 0),
        "cost_usd": stage.get("cost_usd", 0.0),
        "wall_ms": stage.get("wall_ms", 0),
        "chain_id": stage.get("chain_id"),
        "topic_id": stage.get("topic_id", stage.get("chain_id")),
        "ts": _now_iso(),
    }


def compute_rewards(records: list[dict], outputs: dict, *, judge_fn, embed_fn=None,
                    topics: dict | None = None) -> list[dict]:
    """Join chain/stage records + a (chain_id, slot)->output_text lookup into SC2 rows.

    Each stage k (k>=1) is the CANDIDATE; the baseline is stage k-1's output within the
    same chain. cosine_pregate skips the judge for unchanged outputs (reward 0.0).
    `topics` maps chain_id -> topic_id (from WP5 payloads / the task text); defaults to
    chain_id.
    """
    topics = topics or {}
    task_class = {}
    stages_by_chain: dict[str, list[dict]] = {}
    for r in records:
        if r.get("kind") == "chain":
            task_class[r["chain_id"]] = r.get("task_class", "unknown")
        elif r.get("kind") == "stage":
            stages_by_chain.setdefault(r["chain_id"], []).append(r)

    rows: list[dict] = []
    for chain_id, stages in stages_by_chain.items():
        cls = task_class.get(chain_id, "unknown")
        for i in range(1, len(stages)):
            prev, cur = stages[i - 1], stages[i]
            prev_out = outputs.get((chain_id, prev.get("slot")), "")
            cur_out = outputs.get((chain_id, cur.get("slot")), "")
            enriched = {**cur, "chain_id": chain_id, "topic_id": topics.get(chain_id, chain_id)}
            if cosine_pregate(prev_out, cur_out, embed_fn=embed_fn):
                rows.append(_stage_row(enriched, cls, 0.0))          # unchanged -> no judge call
                continue
            reward = judge_pair(prev_out, cur_out, judge_fn=judge_fn)
            rows.append(_stage_row(enriched, cls, reward))
    return rows
