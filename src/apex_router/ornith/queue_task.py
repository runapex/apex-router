#!/usr/bin/env python3
"""Enqueue a job for the Ornith worker.

Lanes (the worker dispatches on `lane`):
  adhoc   (default)  raw thinking-OFF chat over --task (+ --context files)
  codegen            --spec + --tests-file  -> worker runs the tests, GATED (counts as savings)
  review             --diff-file            -> worker runs a review pre-filter, always escalates

Run it:  python -m apex_router.ornith.queue_task --task "..." [--context FILE ...]
         python -m apex_router.ornith.queue_task --lane codegen --spec "..." --tests-file t.py
         python -m apex_router.ornith.queue_task --lane review --diff-file change.diff

Importing this module has NO side effects (argparse lives in main() behind the __main__ guard).
"""
import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--lane", choices=["adhoc", "codegen", "review"], default="adhoc")
    p.add_argument("--task")                       # adhoc
    p.add_argument("--context", action="append", default=[])
    p.add_argument("--spec")                       # codegen
    p.add_argument("--tests-file")                 # codegen: a python file of test_* functions
    p.add_argument("--diff-file")                  # review: a diff/patch
    p.add_argument("--customer")
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--enable-thinking", action="store_true")  # adhoc opt-in (hazard on codegen)
    a = p.parse_args(argv)
    if a.customer and "," in a.customer:
        raise SystemExit("one customer per job")

    job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    job = {"id": job_id, "lane": a.lane, "customer": a.customer, "max_tokens": a.max_tokens}

    if a.lane == "codegen":
        if not a.spec or not a.tests_file:
            raise SystemExit("codegen lane requires --spec and --tests-file")
        job["spec"] = a.spec
        job["tests"] = Path(a.tests_file).expanduser().resolve().read_text(errors="replace")
    elif a.lane == "review":
        if not a.diff_file:
            raise SystemExit("review lane requires --diff-file")
        job["diff"] = Path(a.diff_file).expanduser().resolve().read_text(errors="replace")
    else:  # adhoc
        if not a.task:
            raise SystemExit("adhoc lane requires --task")
        context = ""
        for raw in a.context:
            path = Path(raw).expanduser().resolve()
            context += f"\n\n--- BEGIN {path} ---\n{path.read_text(errors='replace')}\n--- END {path} ---"
        job["messages"] = [
            {"role": "system",
             "content": "Use only supplied evidence. State uncertainty. Do not invent numeric claims."},
            {"role": "user", "content": a.task + context}]
        if a.enable_thinking:
            job["enable_thinking"] = True

    out = ROOT / "jobs/inbox" / f"{job_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(job, indent=2))
    print(out)


if __name__ == "__main__":
    main()
