#!/usr/bin/env python3
"""Overnight retrain cycle — if enough approved de-identified examples exist, stop the
Ornith service, run the training script, and restart it (macOS launchctl).

Run it:  python -m apex_router.ornith.overnight_cycle

Importing this module has NO side effects (no counting, no launchctl, no training) — the
cycle logic lives in main() behind the __main__ guard so the package imports cleanly.
`count()` is import-safe and reusable.
"""
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

from .ornith_client import LOCK, MAINTENANCE, readiness

ROOT = Path(__file__).resolve().parent
FEEDBACK = ROOT / 'feedback/approved.jsonl'
TRAIN = ROOT / 'training/train.sh'
MIN = int(os.environ.get('ORNITH_MIN_TRAINING_EXAMPLES', '100'))


def count() -> int:
    if not FEEDBACK.exists():
        return 0
    n = 0
    for line in FEEDBACK.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        n += bool(r.get('approved_for_training') and r.get('deidentified'))
    return n


def main() -> None:
    service = f'gui/{os.getuid()}/com.ornith.server'
    plist = Path.home() / 'Library/LaunchAgents/com.ornith.server.plist'

    n = count()
    print(f'approved de-identified examples: {n}')
    if n < MIN:
        raise SystemExit(0)
    if not (TRAIN.exists() and os.access(TRAIN, os.X_OK)):
        print('training disabled: create verified executable training/train.sh')
        raise SystemExit(0)
    MAINTENANCE.parent.mkdir(parents=True, exist_ok=True)
    MAINTENANCE.touch()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    stopped = False
    try:
        with LOCK.open('a+') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            subprocess.run(['launchctl', 'bootout', service], check=False)
            stopped = True
            subprocess.run([str(TRAIN)], check=True)
            subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist)], check=True)
            stopped = False
        MAINTENANCE.unlink(missing_ok=True)
        for _ in range(60):
            if readiness():
                print('Ornith ready')
                raise SystemExit(0)
            time.sleep(5)
        raise RuntimeError('Ornith did not become ready')
    finally:
        if stopped:
            subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist)], check=False)
        MAINTENANCE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
