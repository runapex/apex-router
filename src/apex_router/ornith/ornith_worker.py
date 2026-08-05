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

from .dispatch import run_job
from .offload_telemetry import OffloadRecord, usage_tokens, write_offload

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / 'jobs/inbox'
RUNNING = ROOT / 'jobs/running'
DONE = ROOT / 'jobs/done'
FAILED = ROOT / 'jobs/failed'
MAINT = ROOT / 'state/maintenance'


def _process_one(run: Path) -> None:
    """Dispatch one job by lane, record its earned verdict, route to done/failed.

    Control-flow invariants (hardened via Codex cross-validation):
      - parse INSIDE try: a poison (malformed) job goes to FAILED, never kills the loop.
      - mark done BEFORE unlink: a failing unlink must not let the except-handler ALSO write FAILED.
      - telemetry carries the LANE's verdict (gated codegen that passed is the only thing that can
        count as frontier work saved); a failed job records an ungated escalation.
    """
    t0 = time.time()
    lane = 'adhoc'
    done = False
    try:
        job = json.loads(run.read_text())
        lane = job.get('lane', 'adhoc')
        res = run_job(job)
        job.update(completed_at=datetime.now(timezone.utc).isoformat(), lane=res.lane,
                   ok=res.ok, gated=res.gated, escalate=res.escalate,
                   answer=res.output, detail=res.detail, usage=res.usage)
        (DONE / run.name).write_text(json.dumps(job, indent=2))
        done = True
        run.unlink(missing_ok=True)
        p, c, cached = usage_tokens(res.usage)
        write_offload(OffloadRecord(ts=time.time(), lane=res.lane, model='ornith-35b',
            ok=res.ok, gated=res.gated, escalated=res.escalate, prompt_tokens=p,
            completion_tokens=c, cached_tokens=cached, latency_ms=int((time.time() - t0) * 1000)))
    except Exception as e:  # noqa: BLE001
        if not done:
            try:
                (FAILED / run.name).write_text(json.dumps(
                    {'error': str(e), 'traceback': traceback.format_exc(), 'src': run.name}, indent=2))
            except OSError:
                pass
            run.unlink(missing_ok=True)
            write_offload(OffloadRecord(ts=time.time(), lane=lane, model='ornith-35b', ok=False,
                gated=False, prompt_tokens=None, completion_tokens=None, cached_tokens=None,
                latency_ms=int((time.time() - t0) * 1000), escalated=True))


def main() -> None:
    for d in (INBOX, RUNNING, DONE, FAILED):
        d.mkdir(parents=True, exist_ok=True)
    for p in RUNNING.glob('*.json'):        # requeue anything left running from a prior run
        p.replace(INBOX / p.name)
    # Version-guard: snapshot our own source at startup. If the code on disk changes, exit cleanly so
    # the supervisor (launchd KeepAlive / systemd Restart) relaunches on fresh code — a long-lived
    # daemon must never keep running a stale bugfix (measured: a fix sat unused ~19h once).
    from .version_guard import Guard
    guard = Guard(ROOT)
    while True:
        if guard.is_stale():
            print("worker source changed on disk — exiting for supervisor to restart on fresh code")
            return
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
