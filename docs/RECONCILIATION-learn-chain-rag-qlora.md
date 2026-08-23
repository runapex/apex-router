# RECONCILIATION — learn-chain + rag-qlora-loop (final)

Status: **SHIP** (Kimi K3 final reconciliation). 9 work packages implemented; 32 WP
tests + full repo suite (1159 passed / 4 skipped / 0 failed) green.

## Pipeline provenance

1. Kimi K3 — combined design eval → 5 accepted changes folded into the designs.
2. Kimi K3 — 9-work-package implementation plan (`IMPL-plan-…`).
3. Implementation — all 9 WPs, per-WP tested, committed.
4. Final review — **Fable5 returned empty** (an upstream/`claude-fable-5` transient,
   NOT a proxy defect: the measuring proxy streamed the identical 66 KB extended-
   reasoning request cleanly). **sonnet-4-6 stood in** and produced the rigorous review.
5. Kimi K3 — final reconciliation of the fixes → **SHIP**.

## Final review (sonnet-4-6): 3 must-fix defects — all FIXED

| # | Severity | Defect | Fix |
|---|----------|--------|-----|
| 1 | CRITICAL | `judge_pair` used `(s1+s2)/2` = the position **bias**; for an antisymmetric judge it **zeros** the reward | `(s1−s2)/2` (debias); test 0.1→0.3 |
| 2 | major | SKIP + rendered CI pooled promo+confirm, bypassing the gate estimand | CI/SKIP from **confirm-split rows only**; no-confirm ⇒ OFFERED |
| 3 | major | `cluster_bootstrap` keyed `chain_id` → pseudo-replication unguarded | cluster by **`topic_id`**; guard test added |

## Kimi reconciliation — verdict SHIP, with two conditions (both applied)

- **NO-SHIP seam closed:** a missing `topic_id` no longer falls back to `chain_id`
  (which would reopen pseudo-replication). It now collapses to ONE conservative cluster.
- **Degenerate-CI invariant enforced:** a cell with `< 2` distinct confirm topics is
  never SKIP'd (a 1-topic bootstrap CI is zero-width) — it is OFFERED.
- **Honesty:** `cost_per_delta` renamed `cost_per_delta_confirm` (confirm-phase only).

## Handoff to sonnet-4.6 implementation agents (opus-4.8 coordinator)

1. **Preserve** the two-call position-swapped judge and `(s1 − s2)/2`; never collapse
   it to a one-sided judge call.
2. **Keep green** the confirm-only CI/SKIP, `topic_id` clustering, min-topic,
   missing-`topic_id`, and empty-confirm/OFFERED regression tests.
3. Treat **OFFERED as exploration-only (never ON)**; label cost as confirm-phase unless
   lifetime (promo+exploration) cost is added separately.

## Open (non-blocking) follow-ups

- Wire the pi `/learn` extension to emit SC1 records + call the planner (WP1/WP4 CLI
  seams exist); add `apex-router rag-nightly` / `chain-bench` subcommands to `cli.py`
  and `uv tool install --force` (coordinator note in IMPL-plan).
- Real QLoRA run needs the MLX base weights + `mlx-lm`; `train.sh --dry-run` validates
  everything else. `Stream ended without finish_reason` is an upstream/model transient
  (pi retries); the byte-pure proxy is intentionally untouched.
