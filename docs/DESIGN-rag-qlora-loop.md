# DESIGN — nightly RAG loop + gated QLoRA (books + apex-router outputs)

Status: **design proposed** (companion to DESIGN-learn-chain.md). Fills the training
gap that `overnith_cycle.py` already reserves.

## What already exists (do not rebuild)

Two-layer local-model adaptation is already the apex-router shape:

- **L1 · RAG (online, reversible, cheap):** `record_feedback.py` writes de-identified
  approved corrections to `feedback/approved.jsonl`; `exemplars.py` retrieves them;
  `exemplar_inject.py` prepends them as few-shot; `rag_eval.py` measures — **evidence-
  grade** — whether injection lowers held-out escalation (snapshot + lineage isolation).
- **L2 · QLoRA (nightly, heavier, gated):** `overnight_cycle.py` counts approved
  examples; at ≥ `ORNITH_MIN_TRAINING_EXAMPLES` (100) it enters maintenance, stops the
  Ornith service, runs `training/train.sh`, restarts, waits for readiness. **`train.sh`
  is deliberately absent** ("training disabled: create verified executable
  training/train.sh"). `mlx-lm` is the declared Apple-Silicon dep (`[ornith]` extra).

**So QLoRA is required** — it is the reserved gap — and the missing pieces are: (a) a
harvester that grows the corpus from **books + apex-router outputs**, and (b) the
**`train.sh`** QLoRA script + its serving integration.

## New corpus sources (harvested nightly, de-identified, lineage-tagged)

| Source | Yields | Layer |
|--------|--------|-------|
| booksearch index (`~/books`) | authoritative passages as **RAG grounding** + *derived* (task→approach) exemplars | RAG only |
| `learn_chain.jsonl` (validated opus/kimi explanations correlated to code) | high-value (task, answer) pairs | RAG + QLoRA candidate |
| `route_log.jsonl` / `offload_telemetry.jsonl` | which task_classes the local tier failed/escalated → **targeted** harvesting | prioritization |
| codeqa grounded Q&A | cited (question, grounded answer) pairs | RAG + QLoRA candidate |

**Legal guardrail (critical):** third-party **book text may ground RAG context but must
NOT be trained into weights**. Books feed L1 (retrieval/derived exemplars) only; the
QLoRA dataset is restricted to first-party/derived, de-identified pairs
(`approved_for_training && deidentified`, already enforced by `record_feedback.py`).

## The nightly loop (`booksearch`-style CLI: run-now or scheduled)

```
apex-router rag-nightly            # or --now to run immediately
```

1. **Harvest** candidate exemplars from the sources above; de-identify; dedupe; tag
   `task_class` + `lineage` + `snapshot_before`.
2. **Stage** (not yet approved).
3. **Measure (L1 gate):** `rag_eval.run_condition` — does injecting the NEW candidates
   lower held-out escalation vs the current corpus (snapshot-isolated)? Promote only
   passing candidates into `approved.jsonl`. This is evidence-grade, not vibes.
4. **QLoRA trigger (L2 gate):** run `overnight_cycle` iff approved-count ≥ N **AND** the
   L1 marginal gain has **saturated** (exemplar injection stopped paying per the
   promotion gate). Otherwise stay RAG-only. Mirrors DESIGN-learn-chain's "which layer
   earns its cost" — an incumbent(RAG)-vs-candidate(QLoRA) paired test per task_class.
5. **Record** a cycle metrics row (fail-open JSONL) for RAG-vs-QLoRA ROI over time.

## `training/train.sh` — QLoRA on Apple Silicon (MLX-LM)

- Convert `approved.jsonl` → MLX-LM chat format (`{"messages":[...]}` train/valid split).
- `mlx_lm.lora --model <BASE> --train --fine-tune-type lora --data <dir> --iters …
  --adapter-path <out>` with a **quantized** base = QLoRA.
- **Serving integration (open question):** MLX adapters don't load in ollama/GGUF. Two
  paths: (a) `mlx_lm.fuse` → convert to GGUF → `ollama create` a new tag → point the
  `local` tier at it; (b) serve the fine-tuned model via `mlx_lm.server` on a side port
  and route the `local` family there. Path (a) keeps one server; (b) avoids a GGUF
  reconvert each cycle.
- **Safety rails:** `--dry-run` validates deps + data + base availability without
  training; a held-out eval must beat the pre-train adapter or the new adapter is
  **rejected** (no silent regressions); keep the previous adapter for one-command
  rollback; low `--iters` + LoRA-only to bound catastrophic forgetting on a small set.

## Risks to weigh (for Kimi)

1. **Is QLoRA premature?** 100 de-identified examples is tiny for a 9B; QLoRA may overfit
   / forget while RAG already adapts reversibly. When (if ever) does weight-training beat
   staying RAG-only on this hardware (38 GB, Apple Silicon)?
2. **Serving integration** (a) vs (b) above.
3. **Surrogate mismatch / gaming the judge** — reuse DESIGN-learn-chain guardrails
   (position-swapped judge, cluster-by-chain, sparse user-rating anchor).
4. **Catastrophic forgetting / eval leakage** across nightly cycles.
5. **Corpus provenance** — books-in-RAG-only vs weights; how to prove no verbatim book
   text leaks into the QLoRA set.

## Kimi K3 combined-eval — ACCEPTED CHANGES (supersede the wording above)

1. **Trigger (P0).** Replace "≥100 AND saturated" with a 3-part gate: approved count
   ≥ **500**; L1 marginal Δreward CI upper-bound ≤ **0.02 for 3 consecutive nightly
   cycles**; and evidence of **context crowding** (injected-token share of prompt > 30%
   on the saturated task_class). If these never fire, **RAG-only forever is a SUCCESS**,
   not a failure.
2. **Serving = path (a) only (P0).** `mlx_lm.fuse` → convert to GGUF at the **same quant**
   as the incumbent → `ollama create local-candidate:<cycle_id>` → run the gate eval
   against it as a **shadow tag** → on pass, repoint the `local` family, keep the previous
   tag for one-pointer rollback. Delete path (b) (a second server confounds every bench
   comparison with a different serving stack).
3. **Layer is a bench dimension (P1).** Bench rows keyed `(layer, task_class)` with
   `layer ∈ {RAG-incumbent, QLoRA-candidate}`; the L2 decision is a standard paired
   incumbent-vs-candidate test through `deltas_from_rows → amr.gate`, FDR-corrected
   alongside the chain slots. Removes the bespoke "beat the pre-train adapter" check —
   the bar is the **current production config (RAG included)**, not the pre-train adapter.
4. **Forgetting rails, measurable (P1).** 10–20% generic-instruction **replay buffer** in
   every run; a **frozen ~200-prompt general probe** (reasoning/code/chat) scored pre/post
   — reject the adapter if probe drops > 2% OR it doesn't beat the RAG-incumbent on the
   task_class held-out. LoRA rank 8–16, cosine LR, early-stop on valid loss; log
   rank/iters per cycle so the bench can later learn which hyperparams earn their cost.
5. **Provenance, mechanical (P2).** Dataset builder **hard-fails** on any source outside
   the first-party allowlist (implemented in `training/prepare_data.py`); plus an
   **8-gram contamination scan** of the final train/valid JSONL against the booksearch
   index (abort cycle on any hit) and an **attestation row** (`dataset_hash,
   ngram_check: pass`). A post-hoc canary is a smoke signal only, never the proof.

## Provenance

Companion to the Kimi-K3-cross-validated DESIGN-learn-chain.md. Both documents were
submitted to Kimi K3 for a combined evaluation; the ACCEPTED CHANGES above are its
verdict. Next: Kimi produces sonnet-4.6-consumable implementation steps; implementation
by sonnet-4.6 agents (opus-4.8 coordinator); one Fable5 final review; Kimi reconciliation.
