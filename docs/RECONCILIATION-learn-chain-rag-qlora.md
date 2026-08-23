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

## Follow-ups — status

**DONE (shipped this pass):**
- First-class CLI subcommands wired in `cli.py` and live on the machine
  (`uv tool install --force --editable`): `apex-router chain-planner|chain-bench|rag-nightly`.
  `chain_bench` moved into the package so it ships with the installed tool.
- Full machine setup refreshed (uv tool + pi extensions apex-route/learn/booksearch + pypdf).
- **Full end-to-end regression:** repo suite **1162 passed / 4 skipped / 0 failed**; 12/12
  system smoke checks (verify, tiers, all 3 chain CLIs, train.sh dry-run exit-code paths,
  3 pi extensions load, proxy health, ollama, live booksearch query). One real bug found
  and fixed by the E2E (chain-bench CLI `n_chains`→`n_topics` KeyError) + regression test added.

**REMAINING for the sonnet-4.6 agents (non-blocking):**
- Wire the pi `/learn` extension to call `apex-router chain-planner` for the metrics-
  grounded proposed chain + confirm/edit, and emit SC1 chain/stage records during the run
  (WP1/WP4 CLI seams exist).
- Real QLoRA run needs MLX base weights + `mlx-lm`; `train.sh --dry-run` validates
  everything else (exit 3 = missing data, exit 1 = missing mlx-lm — both verified).
- `Stream ended without finish_reason` is an upstream/model transient (pi retries); the
  byte-pure proxy is intentionally untouched (streamed the identical 66 KB request cleanly).
