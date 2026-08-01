#!/usr/bin/env python3
"""Record a de-identified, corrected answer as an approved training example.

Run it:  python -m apex_router.ornith.record_feedback --job J --corrected-answer F --deidentified

Importing this module has NO side effects (no argparse at import) — the CLI logic lives
in main() behind the __main__ guard so the package imports cleanly.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


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
    out = ROOT / 'feedback/approved.jsonl'
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('a') as f:
        f.write(json.dumps(record) + '\n')
    print(out)


if __name__ == "__main__":
    main()
