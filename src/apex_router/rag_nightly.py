"""WP6/WP7 — nightly RAG harvester + L1 gate + QLoRA trigger.

Harvests candidate exemplars from apex-router outputs (learn_chain, route/offload
failures, codeqa) and the booksearch index, de-identifies + dedupes + tags them,
stages to feedback/staged.jsonl, then promotes those that pass an evidence-grade L1
gate (rag_eval.run_condition, snapshot-isolated) into feedback/approved.jsonl.

GUARDRAIL: book-derived candidates are RAG-only — approved_for_training is forced
False, so training/prepare_data.py's allowlist refuses them from weights (WP8).

SC4 cycle record (frozen), fail-open to ~/.apex-router/rag_cycle.jsonl:
  {"kind":"cycle", cycle_id, l1_delta, l1_ci, crowding, approved_count,
                   qlora_fired, verdict, dataset_hash, ngram_check, ts}
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .ornith.record_feedback import append_approved, deidentify, FEEDBACK_PATH

STAGED_PATH = FEEDBACK_PATH.parent / "staged.jsonl"
FIRST_PARTY_SOURCES = {"learn_chain", "route_log", "offload", "codeqa", "manual"}
BOOK_SOURCES = {"book", "booksearch"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_cycle_log() -> Path:
    env = os.environ.get("APEX_RAG_CYCLE_LOG")
    return Path(env) if env else Path.home() / ".apex-router" / "rag_cycle.jsonl"


@dataclass
class Candidate:
    messages: list
    corrected_answer: str
    task_class: str
    source: str                       # one of FIRST_PARTY_SOURCES | BOOK_SOURCES
    tags: list = field(default_factory=list)
    lineage: str = ""
    created_at: str = field(default_factory=_now_iso)

    def content_hash(self) -> str:
        blob = json.dumps([self.messages, self.corrected_answer], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:16]

    def to_record(self) -> dict:
        book = self.source in BOOK_SOURCES
        tags = list(self.tags) + ([self.source] if self.source in BOOK_SOURCES else [])
        return {
            "created_at": self.created_at,
            "messages": self.messages,
            "corrected_answer": deidentify(self.corrected_answer),
            "task_class": self.task_class,
            "source": self.source,
            "tags": tags,
            "lineage": self.lineage or self.content_hash(),
            # GUARDRAIL: books never enter the weight-training set.
            "approved_for_training": (not book),
            "deidentified": True,
        }


def harvest(source_loaders: list) -> list[Candidate]:
    """Collect candidates from injectable source loaders (each returns list[Candidate]),
    de-dupe by content hash. Loaders are injected so the harvest is testable and so new
    sources (booksearch, learn_chain, codeqa) plug in without touching the gate."""
    seen: set[str] = set()
    out: list[Candidate] = []
    for load in source_loaders:
        try:
            cands = load() or []
        except Exception as exc:  # noqa: BLE001 — one bad source must not sink the harvest
            print(f"rag_nightly: source failed (skipped): {exc}", file=sys.stderr)
            continue
        for c in cands:
            h = c.content_hash()
            if h in seen:
                continue
            seen.add(h)
            out.append(c)
    return out


def stage(candidates: list[Candidate], path: Path = STAGED_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in candidates:
            f.write(json.dumps(c.to_record()) + "\n")
    return path


def l1_gate(candidates: list[Candidate], *, measure_fn, threshold: float = 0.0,
            approved_path: Path = FEEDBACK_PATH) -> list[dict]:
    """Promote candidates whose injection improves the held-out signal.

    `measure_fn(candidate_record) -> improvement` wraps rag_eval.run_condition
    (inject vs baseline escalation) so this stays transport-agnostic and testable.
    A candidate is promoted (appended to approved.jsonl) iff improvement > threshold.
    Book-derived records are still written (RAG corpus) but keep approved_for_training
    False from to_record(), so they never reach weights.
    """
    promoted = []
    for c in candidates:
        rec = c.to_record()
        try:
            improvement = float(measure_fn(rec))
        except Exception as exc:  # noqa: BLE001
            print(f"rag_nightly: measure failed for one candidate (skipped): {exc}", file=sys.stderr)
            continue
        if improvement > threshold:
            append_approved(rec, approved_path)
            promoted.append(rec)
    return promoted


def approved_count(path: Path = FEEDBACK_PATH) -> int:
    if not Path(path).exists():
        return 0
    n = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        n += bool(r.get("approved_for_training") and r.get("deidentified"))
    return n


def should_train(history: list[dict], *, min_examples: int = 500, sat_cycles: int = 3,
                 eps: float = 0.02, crowding_min: float = 0.30,
                 approved: int | None = None) -> bool:
    """WP7 — the 3-part QLoRA trigger. ALL must hold:
      1. approved_for_training count >= min_examples (default 500);
      2. L1 marginal Δreward CI-upper <= eps for `sat_cycles` consecutive cycles;
      3. context crowding (injected-token share) > crowding_min on the saturated class.
    RAG-only forever (never firing) is a SUCCESS, not an error.
    """
    n = approved if approved is not None else approved_count()
    if n < min_examples:
        return False
    recent = [h for h in history if h.get("kind") == "cycle"][-sat_cycles:]
    if len(recent) < sat_cycles:
        return False
    saturated = all(isinstance(h.get("l1_ci"), (list, tuple)) and len(h["l1_ci"]) == 2
                    and float(h["l1_ci"][1]) <= eps for h in recent)
    crowded = any(isinstance(h.get("crowding"), (int, float)) and float(h["crowding"]) > crowding_min
                  for h in recent)
    return bool(saturated and crowded)


def log_cycle(record: dict, path: Path | None = None) -> bool:
    path = Path(path) if path else default_cycle_log()
    record = {"kind": "cycle", "ts": _now_iso(), **record}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, allow_nan=False) + "\n")
        return True
    except (OSError, TypeError, ValueError) as exc:
        print(f"rag_nightly: cycle log failed (non-fatal): {exc}", file=sys.stderr)
        return False


def read_cycle_history(path: Path | None = None) -> list[dict]:
    path = Path(path) if path else default_cycle_log()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="rag-nightly", description="nightly RAG harvest + L1 gate + QLoRA trigger")
    p.add_argument("--now", action="store_true", help="run a cycle immediately")
    p.add_argument("--dry-run", action="store_true", help="harvest+stage only; no promotion, no training")
    a = p.parse_args(argv)
    if not (a.now or a.dry_run):
        print("nothing to do (pass --now or --dry-run)")
        return 0
    # Live source loaders are wired in WP8/integration; here we run an empty, safe cycle
    # that still records SC4 so the schedule is exercisable end-to-end.
    cands = harvest([])
    stage(cands)
    n = approved_count()
    fired = should_train(read_cycle_history(), approved=n) if a.now else False
    log_cycle({"cycle_id": f"cyc-{int(time.time())}", "l1_delta": None, "l1_ci": None,
               "crowding": None, "approved_count": n, "qlora_fired": fired,
               "verdict": "SKIP", "dataset_hash": None, "ngram_check": None})
    print(f"rag-nightly: staged {len(cands)} candidates, approved_count={n}, qlora_fired={fired}")
    if fired and not a.dry_run:
        print("QLoRA trigger met -> would invoke overnight_cycle (see training/train.sh)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
