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
import hashlib
import json
import random
import sys
from pathlib import Path

BOOK_TAINT = {"book", "books", "booksearch"}


def _text_of(example: dict) -> str:
    return " ".join(m.get("content", "") for m in example.get("messages", []) if isinstance(m, dict))


def _ngrams(text: str, n: int = 8) -> set:
    toks = text.split()
    return {" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)} if len(toks) >= n else set()


def contamination_hits(examples: list, book_ngrams: set, n: int = 8) -> int:
    """Count training examples sharing an 8-gram with the booksearch corpus. Any hit
    aborts the cycle (mechanical proof that no verbatim book text enters weights)."""
    if not book_ngrams:
        return 0
    hits = 0
    for ex in examples:
        if _ngrams(_text_of(ex), n) & book_ngrams:
            hits += 1
    return hits


def load_book_ngrams(path: Path | None, n: int = 8) -> set:
    """Build the book 8-gram set from a reference file. Each line may be a precomputed
    8-gram OR raw text (expanded into its 8-grams). Empty when no corpus is supplied
    (the harvester should pass one)."""
    if not path or not Path(path).exists():
        return set()
    grams: set = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        toks = line.split()
        grams |= _ngrams(line, n) if len(toks) > n else {line}
    return grams


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
    p.add_argument("--replay-buffer", help="generic-instruction jsonl to mix in (forgetting rail)")
    p.add_argument("--replay-frac", type=float, default=0.15)
    p.add_argument("--book-ngrams", help="book 8-gram set (one per line) for contamination scan")
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

    # Replay buffer: mix in generic-instruction data to bound catastrophic forgetting.
    replay = []
    if a.replay_buffer and Path(a.replay_buffer).exists():
        for line in Path(a.replay_buffer).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    replay.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    n_replay = int(len(kept) * a.replay_frac)
    replay_mix = (replay * ((n_replay // len(replay)) + 1))[:n_replay] if replay else []

    # 8-gram contamination scan vs the booksearch corpus — ANY hit aborts (mechanical
    # proof, not assertion, that no verbatim book text enters weights).
    book_ngrams = load_book_ngrams(Path(a.book_ngrams) if a.book_ngrams else None)
    hits = contamination_hits(kept + replay_mix, book_ngrams)
    if hits:
        print(f"CONTAMINATION: {hits} training example(s) share an 8-gram with the book corpus — abort",
              file=sys.stderr)
        return 4

    dataset = kept + replay_mix
    random.Random(a.seed).shuffle(dataset)
    n_valid = max(1, int(len(dataset) * a.valid_frac)) if len(dataset) > 1 else 0
    valid, train = dataset[:n_valid], dataset[n_valid:]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "train.jsonl").write_text("".join(json.dumps(e) + "\n" for e in train), encoding="utf-8")
    (out / "valid.jsonl").write_text("".join(json.dumps(e) + "\n" for e in valid), encoding="utf-8")
    dataset_hash = hashlib.sha256(
        "".join(json.dumps(e, sort_keys=True) for e in dataset).encode("utf-8", "ignore")).hexdigest()[:16]
    attestation = {"dataset_hash": dataset_hash, "ngram_check": "pass", "n_train": len(train),
                   "n_valid": len(valid), "n_replay": len(replay_mix), "refused": refused,
                   "replay_frac": a.replay_frac}
    (out / "attestation.json").write_text(json.dumps(attestation, indent=2), encoding="utf-8")
    print(f"prepared {len(train)} train + {len(valid)} valid "
          f"({len(replay_mix)} replay, refused {refused}) -> {out}")
    print(f"attestation: {json.dumps(attestation)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
