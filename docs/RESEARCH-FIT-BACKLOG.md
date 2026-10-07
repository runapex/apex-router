# Research-fit backlog — telemetry-gated candidate experiments

**Status:** BACKLOG (hypotheses, not commitments). Each entry is an *untrusted experiment
proposal* per `DESIGN-whitepaper-research-loop.md` — it may only ever become a **registered,
falsifiable experiment scored on an EXISTING metric**, never an instruction to implement. Nothing
here is built, gated, or promoted.

## Why this file exists
A 5-paper KV-caching / LLM-serving digest was assessed against apex-router and cross-validated with
`it-entra-gpt-6-astra` (verdict: OVER-CLAIMS-FIT — the papers describe **self-hosted serving-engine
internals**; apex-router is a **client-side router over hosted APIs + a single-GPU local offload**,
so cache *mechanisms* do not transfer). What DOES transfer is **methodology + telemetry**. Those
survivors are parked here because **telemetry will be enriched** (Phase-1 F5 accumulation, the
`route_join` + `telemetry.jsonl` third join, the shadow-A/B labeler) and a real use case may then
appear. Revisit each entry when the metric it names actually has paired rows.

Full assessment + astra reconciliation: this repo's fit-assessment reconciliation (2026-09).
Governance caveat (astra, grounded): the loop's **read-only slice forbids ledger writes**
(`DESIGN-whitepaper-research-loop.md:133`), so any entry needing a NEW field/write is **out of that
slice** and needs its own gate — it is not free-riding on the read-only slice.

---

## P3 — Calibrate, Then Route (Tumkur et al., Vizuara) → outcome-router methodology
**Fit:** best of the five, but **methodological inspiration, NOT empirical confirmation.** apex has
confirmed nothing — no gate has run, and `gate.run_gate` trusts its inputs
(`DESIGN-whitepaper-research-loop.md:29`).
**What transfers (already aligned with `PHASE1-outcome-router-research.md`):**
- coarse + **calibrated** route-cost model beats a sophisticated predictor;
- output-length only needs **broad buckets** (paper: ≤150% error tolerated);
- **refuse a score outside its validated range** = apex CANNOT-DECIDE (`PHASE1…` §2);
- **hardware evidence, not simulation** = apex out-of-sample gate + §4 replay;
- complexity activates **only under heterogeneity** (paper's learned router LOST on homogeneous
  chat 0.760 vs 0.855) = static-default-is-the-floor.
**Telemetry that unlocks it:** paired `route_log`/route-advise rows once F5 accumulates + `route_join`
is promoted. Then a **registered read-only experiment** can test whether a calibrated coarse
cost-model beats the context-size venue rule out-of-sample.
**Guardrail:** the prefill-vs-decode *instance* selection has NO apex referent; the measured paper
win is modest (0.864 vs 0.847). Do not sell this as more than a design confirmation.
**NOT a target:** never let calibration constants become a knob the loop can autonomously tune
without the per-knob capability manifest (loop prerequisite #3).

## P4 — AgentKV (Liu et al., Imperial/Columbia) → phase-as-routing-state telemetry
**Fit:** the KV-eviction *mechanism* = NO FIT. The transferable idea is **logging agent phase
(think | act | tool | other) as routing state** — a telemetry hypothesis, **NOT partly built**
(corrected by astra): the shadow-A/B labeler is `SPEC… Not built`
(`DESIGN-shadow-ab-labeler.md:3`), and neither `route_conformance.py:31` nor `route_log.py:94`
carries a phase field today.
**Hypothesis to test (when instrumented):** do cold-cache penalties and the route-switch cohort
**concentrate at tool/reasoning transitions**? If so, phase is a cheaper routing boundary than topic
classification.
**Cost to unlock:** a NEW `phase` field + a ledger write on `route_log`/`conformance` →
**out-of-slice** for the current read-only research slice; needs its own additive-schema gate (like
Δ2 `reusable_tokens`/`cache_compat_id`).
**Load-bearing guardrail:** grade phase from **pre-routing** signal only. Deriving phase from the
*completed answer* and using it to route that same turn is **hindsight leakage** (astra). Phase
markers are also model/template-dependent → a **provenance-tagged feature, never a target**.

## P2 — PrefixBench-H100 (Shewale et al.) → local prefix/capacity micro-benchmark
**Fit:** the core estimand (working_set / effective_kv_capacity) is **unobservable for hosted
providers** — apex sees only `cache_read`/`tokens_in` from usage headers, never provider KV
capacity. So on ~78.6%-of-spend it stays a **reporting nuance** (report p50 AND p95 separately;
treat **arrival variance**, not bare concurrency, as a route-advise feature).
**Where it IS testable (corrected by astra — I was too dismissive):** the **single-GPU ollama
offload** path. `model_router.py:129` already advertises shared-prefix reuse for multi-item work;
`offload_telemetry.py:70` already records cached tokens + latency. A **local prefix-working-set
sweep** (vary prefix length / concurrency / cache pressure, watch hit-rate collapse past capacity)
is a plausible benchmark under the loop's serving-experiment track
(`DESIGN-whitepaper-research-loop.md:85`, which adds memory/cold-start/peak-memory instrumentation).
**Prerequisite:** that local KV-pressure instrumentation must exist first — it is a prerequisite,
not proof of impossibility. Provider-wide capacity remains permanently out of reach.

---

## P6 — JEPA latent world model → workflow planner value (registered 2026-10-07)
**Fit:** narrow and **not yet**. As a text representation learner it is the wrong tool; as a small
action-conditioned world model for `PLAN-workflow-graph-optimizer.md` P2 (rank workflows by
P(success) from task state) it fits the research's most active thread (small plannable JEPAs,
e.g. LeWorldModel, 15M params on one GPU). Literature points are as supplied in the 2026-10-07
assessment (LeJEPA/SIGReg, SPHERE-JEPA, Klindt et al. identifiability, LeWM and H-JEPA, the
image-vs-text JEPA instability paper); not independently re-read.

**Measured on this machine (2026-10-07):**
- Data: ~13k main-session tool steps (pi + Claude Code), growing ~475/day (~26 tasks/day); Codex
  ~2,973 steps/day but 1,287 of 1,440 rollouts in 14 d are the `vuln_triage` pipeline (a different
  task distribution).
- Next-action-class predictability on held-out sessions: perplexity 6.92 (prior) -> 5.34 (order-1
  Markov) -> 5.22 (order 2) -> 5.63 (order 3 overfits). History of action classes saturates fast.
- Frozen nomic-embed of the request -> kNN prediction of the task's action mix: 1.798-1.801
  nats/call vs 1.825 prior (~1.5%), no gain on task length. The request text barely predicts the
  work — consistent with the text-JEPA conditional-concentration result.
- MLX on M4 Pro, batch 256 x 32 steps x 768-d inputs: 0.6M params 18,084 samples/s; 3.8M 4,377/s;
  11.8M 1,535/s (33 s per 50k-sample epoch). Compute is not the constraint.

**Design constraints taken from the research:**
- **Never predict text.** Text embeddings are frozen *inputs*; targets are structured task state
  (next action class, error / test status, size buckets, workflow phase).
- **SIGReg (isotropic Gaussian), no EMA teacher**; SPHERE-JEPA's hypersphere target as an ablation;
  linear probes as the evaluation (identifiability motivates it, the data is too discrete to assume it).
- **Train for the planner, not only the next step:** k-step (1-4) latent loss plus a value / ranking
  head on outcomes; plan over workflow *macro-steps* (3-6 per task), not tool calls, to bound
  rollout drift.
- **H-JEPA split:** perceptual code (request, tool output) vs control state (workflow, phase,
  budget, pressure); the planner reads only the control state.

**Work (12-16 dev-days):** E0 prerequisites (>= 100 gold labels from `apex-router labels review`,
300-call action-classifier validation; in flight) · E1 step dataset, session/time-split, local only
(2-3 d) · E2 baselines: Markov order 1-2 + logistic / GBM on hand features (2 d) · E3 MLX model
(4-6 d) · E4 evaluation + ablations + collapse diagnostics (2-3 d) · E5 shadow in the planner only
after G1 (2 d). E0-E2 are needed by the workflow plan anyway.

**Pre-registered gate G1** (held-out sessions, later in time than training):
1. next-state cross-entropy >= 5% below the best baseline;
2. outcome-probe Brier score below the baseline's, 95% CI of the difference excluding 0, on gold;
3. workflow-ranking accuracy >= the Markov chain's;
4. no collapse: effective rank and the SIGReg normality statistic within preset bounds on every
   evaluation checkpoint.

**Schedule and kill criterion:** first G1 attempt on the current ~13k steps (~1-2 weeks of work;
expected to FAIL — a useful negative that validates the baselines). Second attempt at 50k
main-session steps (~11 weeks at today's rate) or after Codex pretraining + main-session
fine-tuning (~2-3 weeks; the distribution shift must be measured). **Drop P6 if G1 still fails at
50k main-session steps.** Before building an encoder, check whether AMI Labs has open-sourced one.

---

## Entries deliberately NOT parked (rejected as implementation candidates)
- **P1 (Shared KV / LMCache, 2 vLLM replicas)** — apex owns no replica fleet, no shared KV pool, no
  byte-level KV transfer. Its layered-validation *discipline* is at most a conceptual scaffold for a
  shadow record whose **integrity layer** (KV-transfer ordering/allocation) has no apex referent.
  Note: apex DOES have a **byte-prefix** integrity layer (`freeze.py:65-87`: hash-at-length,
  fallback on apex-caused divergence, invalidate on client edit) — but that is not KV-transfer
  validation. The synthesis's "shared-cache recovery for failover" is **not apex's to build**.
- **P5 (Contiguity / stale-KV repair, CCR)** — apex's compression pipeline is measure-only + un-wired;
  `replay.py:10` prices **cost** and explicitly "cannot score fidelity/behavior", and CCR restores
  **elided text, not KV tensors** (`resolver.py:3`). The paper's deterministic-reuse-beats-clever-
  scoring lesson only *reinforces* the existing absolute bust-prohibition wall (`replay.py:216`); it
  adds no capability and validates nothing.

## Cross-cutting note — cache-value-aware affinity
The digest's affinity hypothesis is sound and **partly already encoded** (K4: heavy-reuse Claude
multi-turn → sonnet where provider caching works). Adopt the **affinity** half. **Drop** the
"validated shared-cache recovery for failover/load-rebalancing" half — apex owns no replicas to
recover across.
