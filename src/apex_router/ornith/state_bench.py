"""A/B bench: one-shot codegen lane vs SKILL.state codegen lane — measured, not asserted.

Runs a task suite (JSONL: {"id", "spec", "tests"}) through both arms and writes one JSONL row
per (task, arm) plus a summary report. The question it answers, per apex doctrine ("adaptive is
earned from evidence"): does the state loop BUY enough extra gated passes to pay for its extra
local calls — or is it a degradation (more local tokens, same escalations)?

METRICS (per arm):
  pass_rate + Wilson 95% CI   — gated ok / tasks. THE quality signal.
  escalation_rate             — handed to frontier anyway (the one-shot lane's floor).
  mean tokens / job           — the state loop's known cost driver (prompt + completion).
  tokens / pass               — total tokens / passes: the honest efficiency number. A state
                                loop that doubles passes at 3x tokens is still a win here.
  taxonomy totals             — json_syntax / schema_type / premature_overwrite counts (paper
                                §5.7). High json_syntax = this local model can't hold the JSON
                                contract → the degradation to watch for.
  wall ms                     — serialized single-GPU seconds are the real budget.

SIGNALS (printed explicitly): PASS_DELTA (state − oneshot pass rate), EFFICIENCY (tokens/pass
ratio), ESCALATION delta, STRUCTURED-OUTPUT health. The bench reports; the promotion decision
stays with the human + gate, same as any other apex evidence.

Model is INJECTABLE: tests pass a scripted fake; live runs use ornith_client (default).
NOTE the oneshot arm re-implements codegen_lane's flow with an injectable chat (production
codegen_lane imports ornith_client internally) — kept deliberately in sync with it.

CLI:
  python -m apex_router.ornith.state_bench [--suite suite.jsonl] [--attempts 3]
      [--out rows.jsonl] [--report report.json] [--fake]
  --suite omitted -> BUILTIN_SUITE (smoke only; real evidence needs a captured corpus suite).
  --fake          -> scripted model (buggy first attempt, repaired on second) for offline
                     pipeline verification. --fake rows are NOT evidence.
"""
from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .offload_lanes import LaneResult, run_python_tests
from .ornith_code import extract_code
from .state_codegen import state_codegen_lane


@dataclass
class Task:
    id: str
    spec: str
    tests: str


BUILTIN_SUITE: list[Task] = [
    Task("add", "Write a function add(a, b) that returns their sum.",
         "def test_add():\n    assert add(2, 3) == 5\n    assert add(-1, 1) == 0\n"),
    Task("is_even", "Write a function is_even(n) -> bool.",
         "def test_even():\n    assert is_even(4) is True\n    assert is_even(3) is False\n"),
    Task("clamp", "Write clamp(x, lo, hi) that bounds x to [lo, hi].",
         "def test_clamp():\n    assert clamp(5, 0, 3) == 3\n    assert clamp(-1, 0, 3) == 0\n"
         "    assert clamp(2, 0, 3) == 2\n"),
    Task("reverse_words",
         "Write reverse_words(s) that reverses the order of space-separated words, "
         "collapsing multiple spaces. '  a  b ' -> 'b a'.",
         "def test_rw():\n    assert reverse_words('hello world') == 'world hello'\n"
         "    assert reverse_words('  a  b ') == 'b a'\n"),
    Task("dedupe", "Write dedupe(xs) that removes duplicates preserving first-seen order.",
         "def test_dd():\n    assert dedupe([1, 2, 1, 3, 2]) == [1, 2, 3]\n"
         "    assert dedupe([]) == []\n"),
    Task("fib", "Write fib(n) returning the n-th Fibonacci number, fib(0)==0, fib(1)==1.",
         "def test_fib():\n    assert fib(0) == 0\n    assert fib(1) == 1\n"
         "    assert fib(10) == 55\n"),
]


# --------------------------------------------------------------------------------------------------
# Arms. Both return LaneResult so rows are uniform; both take the injected chat.
# --------------------------------------------------------------------------------------------------

def oneshot_arm(spec: str, tests: str, *, max_tokens: int, timeout_s: int, chat) -> LaneResult:
    """Mirror of offload_lanes.codegen_lane with injectable chat (see module docstring)."""
    prompt = ("Write python code for this task. Return ONLY the code in a ```python block, "
              f"no explanation.\n\nTASK:\n{spec}")
    result = chat([{"role": "user", "content": prompt}],
                  max_tokens=max_tokens, enable_thinking=False)
    code = extract_code(result.answer)
    passed, detail = run_python_tests(code, tests, timeout_s=timeout_s)
    return LaneResult("codegen", ok=passed, escalate=not passed, output=code,
                      usage=getattr(result, "usage", None), detail=detail, gated=True,
                      _extra={"attempts": 1, "calls": 1, "rejected": 0, "taxonomy": {}})


ARMS = {"oneshot": oneshot_arm, "state": state_codegen_lane}


# --------------------------------------------------------------------------------------------------
# Stats — pure stdlib, Wilson score interval (same shape the core gate's reporting uses).
# --------------------------------------------------------------------------------------------------

def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% interval for a binomial proportion. n=0 -> (0.0, 1.0) (no information)."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _mean(xs: list[float]) -> float | None:
    return (sum(xs) / len(xs)) if xs else None


def _tok(usage: dict | None) -> tuple[int, int, int]:
    from .offload_telemetry import usage_tokens
    return usage_tokens(usage)


# --------------------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------------------

def run_bench(tasks: list[Task], *, arms: tuple[str, ...] = ("oneshot", "state"),
              chat=None, max_tokens: int = 1200, timeout_s: int = 30,
              max_attempts: int = 3, out_path: str | Path | None = None) -> list[dict]:
    """Run every task through every arm; return (and optionally append-write) metric rows.

    chat: injectable model. None = live ornith_client (thinking-OFF, temp 0.0 — the measured
    codegen settings). One shared callable across arms so both see identical model conditions.
    """
    if chat is None:
        from . import ornith_client as oc

        def chat(messages, *, max_tokens, enable_thinking):  # noqa: A002
            return oc.chat_messages(messages, max_tokens=max_tokens,
                                    enable_thinking=enable_thinking, temperature=0.0,
                                    raise_on_truncation=False)

    rows: list[dict] = []
    for task in tasks:
        for arm in arms:
            runner = ARMS[arm]
            t0 = time.monotonic()
            try:
                if arm == "state":
                    res = runner(task.spec, task.tests, max_tokens=max_tokens,
                                 timeout_s=timeout_s, max_attempts=max_attempts, chat=chat)
                else:
                    res = runner(task.spec, task.tests, max_tokens=max_tokens,
                                 timeout_s=timeout_s, chat=chat)
                err = ""
            except Exception as e:  # noqa: BLE001 — a crashed arm is a row, not an abort
                res = LaneResult("codegen", ok=False, escalate=True, output="",
                                 usage=None, detail=f"arm_error: {e!r}", gated=False,
                                 _extra={"attempts": 0, "calls": 0, "rejected": 0,
                                         "taxonomy": {}})
                err = repr(e)
            wall_ms = int((time.monotonic() - t0) * 1000)
            p, c, k = _tok(res.usage)
            ex = res._extra or {}
            rows.append({
                "task_id": task.id, "arm": arm,
                "ok": bool(res.ok), "gated": bool(res.gated), "escalated": bool(res.escalate),
                "attempts": ex.get("attempts", 0), "calls": ex.get("calls", 0),
                "rejected": ex.get("rejected", 0), "taxonomy": ex.get("taxonomy", {}),
                "prompt_tokens": p, "completion_tokens": c, "cached_tokens": k,
                "wall_ms": wall_ms, "error": err,
            })
    if out_path:
        op = Path(out_path)
        op.parent.mkdir(parents=True, exist_ok=True)
        with open(op, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
    return rows


def summarize(rows: list[dict]) -> dict:
    """Aggregate rows into the per-arm report + explicit positive/degradation signals."""
    by_arm: dict[str, list[dict]] = {}
    for r in rows:
        by_arm.setdefault(r["arm"], []).append(r)

    report: dict = {"arms": {}, "signals": {}}
    for arm, rs in by_arm.items():
        n = len(rs)
        passes = sum(1 for r in rs if r["ok"])
        esc = sum(1 for r in rs if r["escalated"])
        tot_p = sum(r["prompt_tokens"] for r in rs)
        tot_c = sum(r["completion_tokens"] for r in rs)
        tot = tot_p + tot_c
        tax: dict[str, int] = {}
        for r in rs:
            for k, v in r["taxonomy"].items():
                tax[k] = tax.get(k, 0) + v
        lo, hi = wilson_ci(passes, n)
        report["arms"][arm] = {
            "n": n, "passes": passes, "pass_rate": passes / n if n else None,
            "pass_rate_ci95": [round(lo, 3), round(hi, 3)],
            "escalated": esc, "escalation_rate": esc / n if n else None,
            "total_tokens": tot, "prompt_tokens": tot_p, "completion_tokens": tot_c,
            "cached_tokens": sum(r["cached_tokens"] for r in rs),
            "mean_tokens_per_job": round(_mean([r["prompt_tokens"] + r["completion_tokens"]
                                                for r in rs]) or 0, 1),
            "tokens_per_pass": round(tot / passes, 1) if passes else None,
            "mean_attempts": round(_mean([r["attempts"] for r in rs]) or 0, 2),
            "mean_wall_ms": round(_mean([r["wall_ms"] for r in rs]) or 0, 1),
            "errors": sum(1 for r in rs if r["error"]),
            "taxonomy": tax,
        }

    a, b = report["arms"].get("oneshot"), report["arms"].get("state")
    if a and b:
        sig = report["signals"]
        sig["pass_delta"] = round((b["pass_rate"] or 0) - (a["pass_rate"] or 0), 3)
        sig["escalation_delta"] = round((b["escalation_rate"] or 0)
                                        - (a["escalation_rate"] or 0), 3)
        if a["tokens_per_pass"] and b["tokens_per_pass"]:
            sig["efficiency_ratio"] = round(b["tokens_per_pass"] / a["tokens_per_pass"], 3)
        else:
            sig["efficiency_ratio"] = None
        # Explicit verdicts, so the report reads as signal-vs-degradation at a glance.
        verdicts = []
        if sig["pass_delta"] > 0:
            verdicts.append(f"POSITIVE: pass rate +{sig['pass_delta']:.1%} "
                            f"(state {b['pass_rate']:.1%} CI{b['pass_rate_ci95']} vs "
                            f"oneshot {a['pass_rate']:.1%} CI{a['pass_rate_ci95']})")
        elif sig["pass_delta"] < 0:
            verdicts.append(f"DEGRADATION: pass rate {sig['pass_delta']:+.1%}")
        else:
            verdicts.append("NEUTRAL: pass rate unchanged")
        er = sig["efficiency_ratio"]
        if er is not None:
            verdicts.append(("POSITIVE" if er <= 1.0 else "COST") +
                            f": tokens/pass ratio {er:.2f}x vs oneshot")
        json_syntax = b["taxonomy"].get("json_syntax", 0)
        if b["n"] and json_syntax > b["n"] * 0.5:
            verdicts.append(f"DEGRADATION RISK: json_syntax salvage on {json_syntax} steps — "
                            "model is not holding the JSON contract (paper §5.7: constrained "
                            "decoding territory)")
        sig["verdicts"] = verdicts
    return report


def render_report(report: dict) -> str:
    lines = ["state-lane A/B report", "===================="]
    for arm, a in report["arms"].items():
        pr = f"{a['pass_rate']:.1%}" if a["pass_rate"] is not None else "n/a"
        tpp = f"{a['tokens_per_pass']:.0f}" if a["tokens_per_pass"] else "n/a (0 passes)"
        lines.append(
            f"[{arm}] n={a['n']} pass={pr} CI95={a['pass_rate_ci95']} "
            f"escalated={a['escalation_rate']:.1%} tok/job={a['mean_tokens_per_job']:.0f} "
            f"tok/pass={tpp} attempts={a['mean_attempts']} wall={a['mean_wall_ms']:.0f}ms "
            f"errors={a['errors']} taxonomy={a['taxonomy']}")
    for v in report.get("signals", {}).get("verdicts", []):
        lines.append("  " + v)
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------
# Offline verification model — NOT evidence, just proves the pipeline end to end.
# --------------------------------------------------------------------------------------------------

class FakeModel:
    """Scripted chat: buggy code first; when the observation says TESTS FAILED, emits a valid
    JSON patch with corrected code. Per-task answer scripts keyed by spec substring."""

    def __init__(self, scripts: dict[str, tuple[str, str]] | None = None):
        # spec-substring -> (buggy_code, fixed_code)
        self.scripts = scripts or {}
        self.calls = 0

    class _R:
        def __init__(self, answer):
            self.answer = answer
            self.usage = {"prompt_tokens": 100, "completion_tokens": 50,
                          "prompt_tokens_details": {"cached_tokens": 20}}

    def __call__(self, messages, *, max_tokens, enable_thinking):
        self.calls += 1
        user = messages[-1]["content"]
        for key, (buggy, fixed) in self.scripts.items():
            if key in user:
                if "TESTS FAILED" in user:
                    return self._R(json.dumps({"code": fixed, "fix_summary": "repaired"}))
                if "=== CURRENT STATE" in user:  # state arm, first call
                    return self._R(json.dumps({"code": buggy, "fix_summary": "first draft"}))
                return self._R(f"```python\n{buggy}\n```")  # oneshot arm
        return self._R("```python\npass\n```")


def load_suite(path: str | Path) -> list[Task]:
    tasks = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        tasks.append(Task(id=str(d["id"]), spec=d["spec"], tests=d["tests"]))
    return tasks


def _cli(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="state-bench",
                                description="A/B one-shot vs SKILL.state codegen lane")
    p.add_argument("--suite", help="JSONL of {id, spec, tests}; default: builtin smoke suite")
    p.add_argument("--arms", default="oneshot,state")
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--out", help="append metric rows to this JSONL")
    p.add_argument("--report", help="write summary report JSON here")
    p.add_argument("--fake", action="store_true",
                   help="scripted offline model (pipeline check only — NOT evidence)")
    args = p.parse_args(argv)

    tasks = load_suite(args.suite) if args.suite else BUILTIN_SUITE
    chat = None
    if args.fake:
        chat = FakeModel({
            "add": ("def add(a, b):\n    return a - b\n", "def add(a, b):\n    return a + b\n"),
        })
    rows = run_bench(tasks, arms=tuple(args.arms.split(",")), chat=chat,
                     max_attempts=args.attempts, out_path=args.out)
    report = summarize(rows)
    print(render_report(report))
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
