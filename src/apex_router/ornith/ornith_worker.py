#!/usr/bin/env python3
"""Ornith job worker — polls jobs/inbox and answers each via the local Ornith server.

Run it:  python -m apex_router.ornith.ornith_worker

Importing this module has NO side effects (no mkdir, no polling loop) — the daemon logic
lives in main() behind the __main__ guard so the package imports cleanly.
"""
import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .ornith_client import chat_messages

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / 'jobs/inbox'
RUNNING = ROOT / 'jobs/running'
DONE = ROOT / 'jobs/done'
FAILED = ROOT / 'jobs/failed'
MAINT = ROOT / 'state/maintenance'


def _process_one(run: Path) -> None:
    job = json.loads(run.read_text())
    try:
        r = chat_messages(job['messages'], max_tokens=job.get('max_tokens', 4096))
        job.update(completed_at=datetime.now(timezone.utc).isoformat(), answer=r.answer,
                   reasoning=r.reasoning, finish_reason=r.finish_reason, usage=r.usage)
        (DONE / run.name).write_text(json.dumps(job, indent=2))
        run.unlink()
    except Exception as e:
        job.update(error=str(e), traceback=traceback.format_exc())
        (FAILED / run.name).write_text(json.dumps(job, indent=2))
        run.unlink(missing_ok=True)


def main() -> None:
    for d in (INBOX, RUNNING, DONE, FAILED):
        d.mkdir(parents=True, exist_ok=True)
    for p in RUNNING.glob('*.json'):        # requeue anything left running from a prior run
        p.replace(INBOX / p.name)
    while True:
        if MAINT.exists():
            time.sleep(5)
            continue
        jobs = sorted(INBOX.glob('*.json'))
        if not jobs:
            time.sleep(5)
            continue
        src = jobs[0]
        run = RUNNING / src.name
        src.replace(run)
        _process_one(run)


if __name__ == "__main__":
    main()
