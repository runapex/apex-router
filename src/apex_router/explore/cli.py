"""CLI: `apex-router explore run|table`.

  apex-router explore run --task bench/explore/speedup-lis --arm explore --budget 12
  apex-router explore run --task DIR --arm baseline --backend cmd --cmd 'my-agent --stdin'
  apex-router explore table results.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .backends import DEFAULT_OLLAMA_MODEL, make_backend
from .run import Task, report_markdown, run_task
from .scheduler import DEFAULT_FLOOR


def _default_run_dir(task: str, arm: str) -> Path:
    home = Path(os.environ.get("APEX_ROUTER_HOME") or Path.home() / ".apex-router")
    return home / "explore" / f"{time.strftime('%Y%m%d-%H%M%S')}-{task}-{arm}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router explore",
                                 description="discovery harness: explore vs baseline at equal call budget")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one arm on one task; print the report JSON")
    r.add_argument("--task", required=True, help="task dir with goal.md, task.json, hidden oracle dir")
    r.add_argument("--arm", choices=("explore", "baseline"), default="explore")
    r.add_argument("--budget", type=int, default=12, help="model-call budget B (default 12)")
    r.add_argument("--backend", choices=("ollama", "cmd"), default="ollama")
    r.add_argument("--model", default=DEFAULT_OLLAMA_MODEL, help="ollama model")
    r.add_argument("--cmd", dest="shell_cmd", default=None, help="cmd backend: prompt on stdin, reply on stdout")
    r.add_argument("--temperature", type=float, default=0.8)
    r.add_argument("--num-predict", type=int, default=6144)
    r.add_argument("--floor", type=float, default=DEFAULT_FLOOR, help="exploration floor per round")
    r.add_argument("--round-size", type=int, default=6)
    r.add_argument("--oracle-per-round", type=int, default=None,
                   help="max oracle tests per round (default: all; the first Pareto front always)")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--max-minutes", type=float, default=None, help="stop making calls after this time")
    r.add_argument("--run-dir", type=Path, default=None, help="default ~/.apex-router/explore/<ts>-<task>-<arm>")
    r.add_argument("--out", type=Path, default=None, help="append the report row to this JSONL")
    t = sub.add_parser("table", help="markdown table from result JSONL")
    t.add_argument("results", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "table":
        rows = [json.loads(x) for x in a.results.read_text().splitlines() if x.strip()]
        print(report_markdown(rows), end="")
        return 0

    if a.budget < 1:
        ap.error("--budget must be >= 1")
    task = Task.load(a.task)
    backend = make_backend(a.backend, model=a.model, cmd=a.shell_cmd, temperature=a.temperature,
                           num_predict=a.num_predict)
    run_dir = a.run_dir or _default_run_dir(task.name, a.arm)
    deadline = time.time() + 60 * a.max_minutes if a.max_minutes else None
    kw = {}
    if a.arm == "explore":
        kw = {"floor": a.floor, "round_size": a.round_size, "oracle_per_round": a.oracle_per_round}
    rep = run_task(task, a.arm, a.budget, backend, run_dir, seed=a.seed, deadline=deadline, **kw)
    rep["run_dir"] = str(run_dir)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rep) + "\n")
    print(json.dumps(rep, indent=2))
    return 0
