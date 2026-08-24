#!/usr/bin/env python3
"""Overnight retrain cycle — if enough approved de-identified examples exist, unload the
resident Ornith tier (frees the weights for training), run the training script, and re-warm it.

Model serving is ollama; the retired MLX launchd unit is gone, so "stop the service" here
means `ollama stop <active-tier>` and "restart" means a warm call against the active tier
from ornith.env — never a launchctl bootout/bootstrap.

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
def _min_examples() -> int:
    try:
        return int(os.environ.get('ORNITH_MIN_TRAINING_EXAMPLES', '100'))
    except (TypeError, ValueError):
        return 100


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
    from . import local_tier
    tier = local_tier.resolve()

    n = count()
    print(f'approved de-identified examples: {n}')
    if n < _min_examples():
        raise SystemExit(0)
    if not (TRAIN.exists() and os.access(TRAIN, os.X_OK)):
        print('training disabled: create verified executable training/train.sh')
        raise SystemExit(0)
    MAINTENANCE.parent.mkdir(parents=True, exist_ok=True)
    MAINTENANCE.touch()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    unloaded = False
    try:
        with LOCK.open('a+') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            # Free the resident weights for training; a missing ollama or model is not fatal
            # (check=False) — train.sh --dry-run validates the environment itself.
            subprocess.run(['ollama', 'stop', tier.api_model], check=False)
            unloaded = True
            subprocess.run([str(TRAIN)], check=True)
            _warm(tier)
            unloaded = False
        MAINTENANCE.unlink(missing_ok=True)
        for _ in range(60):
            if readiness():
                print('Ornith ready')
                raise SystemExit(0)
            time.sleep(5)
        raise RuntimeError('Ornith did not become ready')
    finally:
        if unloaded:
            _warm(tier)
        MAINTENANCE.unlink(missing_ok=True)


def _warm(tier) -> None:
    """Reload the active tier after training (best-effort; readiness() is the real check)."""
    try:
        subprocess.run(['ollama', 'run', tier.api_model, 'warmup'],
                       check=False, capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        pass


if __name__ == "__main__":
    main()
