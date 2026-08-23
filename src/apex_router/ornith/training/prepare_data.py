#!/usr/bin/env python3
"""Turn approved, de-identified corrections into an MLX-LM chat dataset for QLoRA.

Reads feedback/approved.jsonl (written by record_feedback.py) and emits
  <out>/train.jsonl and <out>/valid.jsonl
in MLX-LM's chat format: one object per line, {"messages": [ {role, content}, ... ]}.

LEGAL GUARD: only first-party, de-identified corrections are eligible
(`approved_for_training && deidentified`). Records tagged as book-derived
(`source == "book"` or tag "book") are REFUSED from the weight-training set — book
text may ground RAG context but must never be trained into weights. Import-safe:
all logic is under main().
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

BOOK_TAINT = {"book", "books", "booksearch"}


def eligible(rec: dict) -> tuple[bool, str]:
    if not (rec.get("approved_for_training") and rec.get("deidentified")):
        return False, "not approved+deidentified"
    tags = {str(t).lower() for t in rec.get("tags", [])}
    if rec.get("source") == "book" or (tags & BOOK_TAINT):
        return False, "book-derived (RAG-only, excluded from weights)"
    if not isinstance(rec.get("messages"), list) or not rec.get("corrected_answer"):
        return False, "missing messages/corrected_answer"
    return True, ""


def to_example(rec: dict) -> dict:
    # the training target is the CORRECTED answer as the final assistant turn.
    msgs = [m for m in rec["messages"] if isinstance(m, dict) and m.get("role") != "assistant"]
    msgs.append({"role": "assistant", "content": rec["corrected_answer"]})
    return {"messages": msgs}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="prepare_data")
    p.add_argument("--feedback", default=str(Path(__file__).resolve().parent.parent / "feedback" / "approved.jsonl"))
    p.add_argument("--out", required=True, help="output dir for train.jsonl/valid.jsonl")
    p.add_argument("--valid-frac", type=float, default=0.1)
    p.add_argument("--seed", type=int, default=13)
    p.add_argument("--min-examples", type=int, default=1)
    a = p.parse_args(argv)

    fb = Path(a.feedback)
    if not fb.exists():
        print(f"no feedback corpus at {fb}", file=sys.stderr)
        return 2
    kept, refused = [], 0
    for line in fb.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ok, _why = eligible(rec)
        if ok:
            kept.append(to_example(rec))
        else:
            refused += 1
    if len(kept) < a.min_examples:
        print(f"only {len(kept)} eligible examples (< {a.min_examples}); refused {refused}", file=sys.stderr)
        return 3

    random.Random(a.seed).shuffle(kept)
    n_valid = max(1, int(len(kept) * a.valid_frac)) if len(kept) > 1 else 0
    valid, train = kept[:n_valid], kept[n_valid:]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "train.jsonl").write_text("".join(json.dumps(e) + "\n" for e in train), encoding="utf-8")
    (out / "valid.jsonl").write_text("".join(json.dumps(e) + "\n" for e in valid), encoding="utf-8")
    print(f"prepared {len(train)} train + {len(valid)} valid (refused {refused}) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
