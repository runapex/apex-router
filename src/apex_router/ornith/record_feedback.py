#!/usr/bin/env python3
"""Record a de-identified, corrected answer as an approved training example.

Run it:  python -m apex_router.ornith.record_feedback --job J --corrected-answer F --deidentified

Importing this module has NO side effects (no argparse at import) — the CLI logic lives
in main() behind the __main__ guard so the package imports cleanly.
"""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FEEDBACK_PATH = ROOT / 'feedback' / 'approved.jsonl'

_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
_HOME = re.compile(re.escape(str(Path.home())))
_TOKEN = re.compile(r'(sk|xox[baprs]|ghp|gho|AKIA)[-_A-Za-z0-9]{8,}')


def deidentify(text: str) -> str:
    """Minimal, deterministic redaction so harvested exemplars can be marked
    deidentified: emails, the user's home path, and obvious API tokens. Callers doing
    weight-training MUST still review; this is a floor, not a guarantee."""
    if not isinstance(text, str):
        return ''
    text = _EMAIL.sub('<email>', text)
    text = _HOME.sub('<home>', text)
    text = _TOKEN.sub('<token>', text)
    return text


def append_approved(record: dict, path: Path = FEEDBACK_PATH) -> Path:
    """Single writer for the approved-corrections corpus (WP6 reuses this)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record) + '\n')
    return path


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--job', required=True)
    p.add_argument('--corrected-answer', required=True)
    p.add_argument('--deidentified', action='store_true')
    p.add_argument('--tags', default='')
    a = p.parse_args(argv)
    if not a.deidentified:
        raise SystemExit('shared LoRA examples must be de-identified')
    job = json.loads(Path(a.job).read_text())
    corrected = Path(a.corrected_answer).read_text()
    record = {'created_at': datetime.now(timezone.utc).isoformat(), 'source_job_id': job['id'],
              'messages': job['messages'], 'original_answer': job.get('answer'),
              'corrected_answer': corrected,
              'tags': [x.strip() for x in a.tags.split(',') if x.strip()],
              'approved_for_training': True, 'deidentified': True}
    print(append_approved(record))


if __name__ == "__main__":
    main()
