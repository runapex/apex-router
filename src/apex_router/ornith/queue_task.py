#!/usr/bin/env python3
"""Enqueue a job for the Ornith worker.

Run it:  python -m apex_router.ornith.queue_task --task "..." [--context FILE ...]

Importing this module has NO side effects (no argparse at import) — the CLI logic lives
in main() behind the __main__ guard so the package imports cleanly.
"""
import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--task", required=True)
    p.add_argument("--context", action="append", default=[])
    p.add_argument("--customer")
    p.add_argument("--max-tokens", type=int, default=4096)
    a = p.parse_args(argv)
    if a.customer and "," in a.customer:
        raise SystemExit("one customer per job")
    context = ""
    for raw in a.context:
        path = Path(raw).expanduser().resolve()
        data = path.read_text(errors="replace")
        context += f"\n\n--- BEGIN {path} ---\n{data}\n--- END {path} ---"
    job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    job = {"id": job_id, "customer": a.customer, "messages": [
        {"role": "system",
         "content": "Use only supplied evidence. State uncertainty. Do not invent numeric claims."},
        {"role": "user", "content": a.task + context}], "max_tokens": a.max_tokens}
    out = ROOT / "jobs/inbox" / f"{job_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(job, indent=2))
    print(out)


if __name__ == "__main__":
    main()
