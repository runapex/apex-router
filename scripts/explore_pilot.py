#!/usr/bin/env python3
"""Pilot bench for `apex-router explore` (docs/research/2026-10-27-p99-discovery-harness.md, sec. 3).

Runs both arms (explore, baseline) on each task at the same model-call budget B, writes one JSONL row per
(task, arm) and prints a markdown table. A smoke pilot checks the plumbing; it is not evidence of a gain.

  scripts/explore_pilot.py                                   # all tasks in bench/explore, B=12, ollama
  scripts/explore_pilot.py --tasks speedup-lis --budget 6 --max-minutes 20

The backend is local ollama by default (free). `--max-minutes` is one wall-clock limit for the whole
pilot; an arm that hits it stops making calls and its row says truncated.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from apex_router.explore.backends import DEFAULT_OLLAMA_MODEL, make_backend  # noqa: E402
from apex_router.explore.run import Task, report_markdown, run_task  # noqa: E402

ARMS = ("explore", "baseline")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bench", type=Path, default=ROOT / "bench" / "explore")
    ap.add_argument("--tasks", default="", help="comma list of task dir names (default: all)")
    ap.add_argument("--budget", type=int, default=12, help="model-call budget B per arm (default 12)")
    ap.add_argument("--backend", choices=("ollama", "cmd"), default="ollama")
    ap.add_argument("--model", default=DEFAULT_OLLAMA_MODEL)
    ap.add_argument("--cmd", dest="shell_cmd", default=None)
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--num-predict", type=int, default=2048)
    ap.add_argument("--floor", type=float, default=0.3)
    ap.add_argument("--round-size", type=int, default=6)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-minutes", type=float, default=None, help="wall-clock limit for the whole pilot")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="default ~/.apex-router/explore/pilot-<ts>")
    a = ap.parse_args(argv)

    names = [x for x in a.tasks.split(",") if x] or sorted(
        p.name for p in a.bench.iterdir() if (p / "task.json").exists())
    out = a.out_dir or Path.home() / ".apex-router" / "explore" / f"pilot-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    results = out / "results.jsonl"
    deadline = time.time() + 60 * a.max_minutes if a.max_minutes else None
    backend = make_backend(a.backend, model=a.model, cmd=a.shell_cmd, temperature=a.temperature,
                           num_predict=a.num_predict)
    rows = []
    for name in names:
        task = Task.load(a.bench / name)
        for arm in ARMS:
            kw = {"floor": a.floor, "round_size": a.round_size} if arm == "explore" else {}
            t0 = time.time()
            rep = run_task(task, arm, a.budget, backend, out / f"{name}-{arm}", seed=a.seed,
                           deadline=deadline, **kw)
            rows.append(rep)
            with open(results, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rep) + "\n")
            print(f"[pilot] {name} {arm}: calls={rep['calls']} best={rep['best_score']} hit={rep['hit']} "
                  f"{time.time() - t0:.0f}s", file=sys.stderr)
    md = report_markdown(rows)
    (out / "table.md").write_text(md)
    print(md, end="")
    print(f"\nresults: {results}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
