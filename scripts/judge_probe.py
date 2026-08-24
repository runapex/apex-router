#!/usr/bin/env python3
"""Judge drift probe — detect a silently-upgraded or drifting chain judge.

The chain reward (chain_reward.judge_pair) is computed by a PINNED position-swapped judge.
Nothing otherwise notices when the judge model behind the endpoint is upgraded or its
scoring behavior drifts — and a drifting reward function silently corrupts every gate
decision downstream. This probe scores a FROZEN set of output pairs (clear ordering under
the chain rubric) exactly the way judge_pair does — two position-swapped calls,
reward = (s1 - s2)/2 — and compares against a recorded baseline:

  - judge MODEL ID differs from baseline  -> drift (silent upgrade)
  - a probe's reward flips sign vs its expected ordering -> drift
  - >20% of probes moved >0.3 from baseline -> drift

Endpoint config (same as codeqa.judge): CODEQA_JUDGE_BASE (+ optional CODEQA_JUDGE_AUTH /
CODEQA_JUDGE_APIM_KEY). With no endpoint configured the probe skips honestly (exit 0).

  judge_probe.py --record   # score the frozen set and write the baseline
  judge_probe.py            # compare vs baseline; exit 2 on drift
  judge_probe.py --json
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

BASELINE_PATH = Path(os.environ.get(
    "APEX_JUDGE_PROBE_BASELINE", Path.home() / ".apex-router" / "judge_probe_baseline.json"))

# Frozen probe pairs: (prev_output, cur_output, expected_sign). The ordering is unambiguous
# under the chain rubric (correctness / grounding / actionability, NOT length).
PROBES = [
    {
        "id": "grounded-vs-vague",
        "prev": "The routing probably works by sending tasks to models. "
                "You can check the config maybe.",
        "cur": "Routing resolves a task_type to a model via route_table.json "
               "(see route_table.py: read_route); unpromoted cells fall back to the "
               "parent task-type default.",
        "expected": 1,
    },
    {
        "id": "correct-vs-wrong",
        "prev": "Cache reads bill at 5x the base input rate, so caching is usually a loss.",
        "cur": "Cache reads bill at 0.1x the base input rate; a high read line at a high "
               "hit-rate means caching is working, not regressing.",
        "expected": 1,
    },
    {
        "id": "padded-vs-concise-correct",
        "prev": "Certainly! Great question. Let me walk you through everything I know about "
                "databases in general, starting from first principles, then touch on indexes, "
                "then storage engines, then query planning, then transactions... "
                "(10 paragraphs of generic background) ...so in short, use an index.",
        "cur": "Add a composite index on (session_id, turn): the query filters on both and "
               "the current table scan is the bottleneck.",
        "expected": 1,
    },
    {
        "id": "equal-quality-swap",
        "prev": "Use pytest -k to run a subset of tests by name.",
        "cur": "Run a subset of tests by name with pytest -k.",
        "expected": 0,  # near-zero reward either way; sign-free probe (magnitude check only)
    },
]

_RUBRIC = (
    "Compare two answers (A, B) to the same task. Judge B relative to A on correctness and "
    "freedom from unsupported claims, completeness for the task's actual need (not length), "
    "concrete grounding, and actionability. A confident wrong or padded answer scores WORSE."
)

DRIFT_REWARD_DELTA = 0.3      # per-probe movement tolerance vs baseline
DRIFT_FRACTION = 0.2          # fraction of probes allowed past tolerance
EXPECTED_SIGN_MIN = 0.1       # |reward| below this counts as sign failure for expected=±1


class ProbeError(Exception):
    pass


def _judge_config() -> tuple[str | None, str]:
    base = os.environ.get("CODEQA_JUDGE_BASE") or None
    model = os.environ.get("CODEQA_JUDGE_MODEL") or "claude-opus-4-8"
    return base, model


def _judge_prompt(a: str, b: str) -> str:
    return (f"{_RUBRIC}\n\nOUTPUT A:\n{a}\n\nOUTPUT B:\n{b}\n\n"
            "Return only a number in [-1,1]: how much better B is than A "
            "(positive = B better, negative = A better).")


def _call_judge(base: str, model: str, prompt: str, *, timeout: float = 60.0) -> float:
    """One raw judge call -> float in [-1,1]. Anthropic-messages shape, same conventions
    as codeqa.judge._call_opus (env credentials, no agentic CLI, ever)."""
    body = json.dumps({
        "model": model, "max_tokens": 64,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    auth = os.environ.get("CODEQA_JUDGE_AUTH")
    if auth:
        headers["Authorization"] = auth if auth.lower().startswith("bearer ") else f"Bearer {auth}"
    key = os.environ.get("CODEQA_JUDGE_APIM_KEY")
    if key:
        headers["Ocp-Apim-Subscription-Key"] = key
    req = urllib.request.Request(base.rstrip("/") + "/v1/messages", data=body,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise ProbeError(f"judge call failed: {e}") from e
    text = ""
    for block in payload.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text", "")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        raise ProbeError(f"judge returned no score: {text[:120]!r}")
    return max(-1.0, min(1.0, float(m.group(0))))


def judge_pair(base: str, model: str, prev: str, cur: str) -> float:
    """Position-swapped debiased reward, identical to chain_reward.judge_pair."""
    s1 = _call_judge(base, model, _judge_prompt(prev, cur))
    s2 = _call_judge(base, model, _judge_prompt(cur, prev))
    return max(-1.0, min(1.0, (s1 - s2) / 2.0))


def score_probes() -> dict:
    base, model = _judge_config()
    rewards = {}
    for p in PROBES:
        rewards[p["id"]] = judge_pair(base, model, p["prev"], p["cur"])
    return {"judge_model": model, "rewards": rewards}


def evaluate(current: dict, baseline: dict) -> list[str]:
    """The drift findings (empty = no drift)."""
    problems = []
    if current["judge_model"] != baseline.get("judge_model"):
        problems.append(f"judge model changed: {baseline.get('judge_model')!r} -> "
                        f"{current['judge_model']!r} (silent upgrade?)")
    moved = 0
    base_rewards = baseline.get("rewards", {})
    for p in PROBES:
        pid, expected = p["id"], p["expected"]
        r = current["rewards"][pid]
        if expected != 0 and (r * expected < EXPECTED_SIGN_MIN):
            problems.append(f"probe {pid}: reward {r:+.2f} violates expected ordering "
                            f"(sign {expected:+d})")
        b = base_rewards.get(pid)
        if isinstance(b, (int, float)) and abs(r - b) > DRIFT_REWARD_DELTA:
            moved += 1
    if len(PROBES) and moved / len(PROBES) > DRIFT_FRACTION:
        problems.append(f"{moved}/{len(PROBES)} probes moved >{DRIFT_REWARD_DELTA} vs baseline")
    return problems


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    record = "--record" in argv
    as_json = "--json" in argv
    base, _ = _judge_config()
    if base is None:
        print("judge probe: skipped (no CODEQA_JUDGE_BASE configured)")
        return 0
    try:
        current = score_probes()
    except ProbeError as e:
        print(f"judge probe: ERROR {e}", file=sys.stderr)
        return 2

    if record:
        current["recorded_at"] = __import__("time").time()
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(current, indent=2, sort_keys=True))
        print(f"judge probe: baseline recorded ({current['judge_model']}) "
              f"-> {BASELINE_PATH}")
        for pid, r in current["rewards"].items():
            print(f"  {pid}: {r:+.2f}")
        return 0

    if not BASELINE_PATH.is_file():
        print(f"judge probe: no baseline at {BASELINE_PATH} — run with --record first",
              file=sys.stderr)
        return 2
    try:
        baseline = json.loads(BASELINE_PATH.read_text())
    except (OSError, ValueError) as e:
        print(f"judge probe: unreadable baseline: {e}", file=sys.stderr)
        return 2

    problems = evaluate(current, baseline)
    report = {"judge_model": current["judge_model"], "rewards": current["rewards"],
              "baseline_model": baseline.get("judge_model"), "drift": bool(problems),
              "problems": problems}
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for pid, r in current["rewards"].items():
            print(f"  {pid}: {r:+.2f} (baseline {baseline.get('rewards', {}).get(pid)})")
        if problems:
            print("judge probe: DRIFT")
            for p in problems:
                print(f"  ! {p}")
        else:
            print("judge probe: OK (no drift)")
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
