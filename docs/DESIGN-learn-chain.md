# DESIGN — adaptive multi-model learning chains (slots, not tiers)

Status: **design accepted, not yet implemented.** Cross-validated by Kimi K3.

A "learning chain" runs an ordered set of models over a task the user is studying
(e.g. `retrieve → sonnet → opus → kimi`), each stage building on the previous. We
want it to be **adaptive**: learn per task-class which stages earn their cost, propose
a chain, let the human confirm/edit with a metrics-grounded rationale, then run and
measure — **without building a parallel system or breaking the FDR-corrected
promotion gate** that already governs routing.

## Core decision: chains are SLOTS, and a slot IS a bench cell

Model a chain as ordered **slots** — `retrieve → draft → deepen → synthesize` — not
as fixed tiers. A slot has an incumbent model, candidate models, and paired reward
rows, which is exactly a `bench.py` cell. So "does kimi pay off after opus?" is a
standard incumbent-vs-candidate **paired** test in the `synthesize` slot, consumed by
`deltas_from_rows` → `amr.gate` (the existing out-of-sample, FDR-corrected gate).
**Nothing new is built for the gate.**

## Reward: signed, position-swapped pairwise judge — never cosine-delta

- Marginal value of stage *k* = judge score of `output_k` vs `output_{k-1}` under a
  fixed rubric, **position-swapped** to cancel order bias, with a **pinned/frozen**
  cheap judge model. Rubric seeded from Ornith's existing validation criteria.
- Cosine (local nomic-embed) is **only** a cheap pre-gate: if `|cos| ≈ 1`, nothing
  changed → skip the judge call. It is NOT the reward (it measures change, not
  improvement, and is biased upward for longer higher-tier outputs).
- Reward is **computed post-hoc** into bench rows, not stored in the log — so the
  reward definition stays versionable and the log stays minimal.

## Log: two record types, fail-open JSONL (mirrors route_log/offload_telemetry)

```
{kind:"chain", chain_id, task_class, proposed:[slots], executed:[slots],
                edited:bool, shown_rationale:bool, ts}
{kind:"stage", chain_id, slot, model, prompt_tokens, completion_tokens,
                cached_tokens, cost_usd, wall_ms, ts}
```

Optional sparse `user_rating` on ~10% of chains as a judge-validation anchor.
Note: `cost_usd` is confounded with position (each stage ingests the prior output,
so tokens grow with slot index) — attribute cost to **slot**, never to "tier".

## Planner + human confirm (with anti-bias guardrails)

1. apex-router's request **classifier** → `task_class`.
2. Planner = per-slot policy: for this class, pick the incumbent per slot, or **SKIP**
   a slot whose gate shows Δ ≈ 0. Cold-start (no history) → labeled default prior.
3. **Before running**, show the proposed chain as a *default with uncertainty*, not a
   verdict:
   > Planned: retrieve→sonnet→opus (~$0.07, ~22s). Basis: 23 chains, opus +0.18
   > Δreward (CI 0.09–0.27); kimi +0.03, below gate — available if wanted.
4. User **confirms / edits / overrides**. Then run + log.

Keeping the adaptive signal unbiased under human edits:
- Log `proposed` vs `executed` separately; **estimate only from executed-as-proposed**.
- Edited chains are a **flagged exploration arm** (propensity-style), analyzed
  separately — never pooled into the estimate.
- Log `shown_rationale`; occasionally A/B chain-only vs chain+rationale to measure
  anchoring.
- **ε-exploration**: with small probability propose a dropped slot anyway, so its
  marginal-value estimate can recover instead of freezing.

## The three aggregates that drive a decision

1. Mean **paired Δreward per slot** vs prior-stage baseline, **clustered by chain_id**,
   with CI (from `deltas_from_rows`).
2. **Cost per unit Δ** ($/Δreward) per slot.
3. **Gate verdict per slot → ON / OFFERED / SKIP** (the same promotion gate).

These are exactly what the confirmation rationale renders.

## Live vs bench (resolving order↔tier confound)

- **Live chains are observational** and fixed-order — tier effect *is* position effect,
  so never claim tier effects from live data alone.
- **Bench is experimental**: every stage output is logged with its inputs, so tiers can
  be **replayed in swapped order or in parallel on the same input** offline (cheap) to
  break the confound. Live stays fixed-order; bench does the causal work.

## Statistical traps (guarded above)

- Non-independence within a chain → cluster-bootstrap by `chain_id`, never iid stages.
- Pseudo-replication (same topic re-chained) → dedupe/cluster by topic.
- Order↔tier confound → only offline swapped-order replay resolves it.
- Surrogate mismatch (judge reward ≠ user value) → spot-check judge vs sparse
  `user_rating`; if they diverge, the gate is optimizing the judge, not the user.

## Phased implementation (proposed)

- **P1 — instrument + log (no adaptivity):** extend `/learn` into a configurable chain
  (`retrieve → sonnet → opus → kimi`), capture the two record types to
  `~/.apex-router/learn_chain.jsonl` via pi's `after_provider_response` usage +
  wall-clock. Ship the confirm/edit prompt with a **cold-start default** rationale.
- **P2 — reward + bench rows:** position-swapped judge (pinned model, cosine pre-gate)
  computing bench-compatible reward rows keyed by `(slot, task_class)`.
- **P3 — planner from the gate:** per-slot ON/OFFERED/SKIP from `amr.gate`; rationale
  renders the three aggregates; add ε-exploration + proposed/executed split.
- **P4 — offline swapped-order replay** in `bench` to de-confound order vs tier.

## Provenance

Design cross-validated by Kimi K3 (`moonshotai/kimi-k3`), which redirected an initial
naive design (cosine-delta reward + parallel aggregates feeding the gate) to the
slots-as-bench-cells model above. Full evaluation retained in the PR discussion.
