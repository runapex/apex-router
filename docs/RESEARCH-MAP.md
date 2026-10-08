# Research map: every experiment, its evidence, and the layer it belongs to

**Status:** living index. **Date:** 2026-10-08 (L1 regime model + retry A/B instrument). Cross-validated by `it-entra-gpt-6-astra` (xval
`68db2eb9274f`, NO-GO → findings applied; see §7). Numbers are measured on this workstation unless
marked otherwise; each row links to the document that owns the detail.
**Companions:** `RESEARCH-FIT-BACKLOG.md` (P2–P6), `RESEARCH-difficulty-teacher-router.md`,
`PLAN-workflow-graph-optimizer.md`, `PLAN-system-design-optimizations.md`,
`PHASE1-outcome-router-research.md`, `DESIGN-shadow-ab-labeler.md`.

## 1. The layers

A method belongs on the lowest layer where **its signal is measured on the same timescale as the
decision it informs**. Putting a slow signal on a fast decision (or the reverse) is how a correct
method ends up used for the wrong case — which is what happened to Zeno (§3).

| Layer | Decides | Timescale | Today's owner |
|---|---|---|---|
| **L0 Serving & wire** | where bytes go, caching, telemetry | per request (ms) | proxy, `pricing.py`, widget network |
| **L1 Call reliability** | retry, wait, switch endpoint | seconds | `pressure.py`, retries, `zeno report` §1/1b/1c, `retry-ab report` |
| **L2 Dispatch routing** | which model tier starts a task / subagent / pi turn | per task start | `classify.py`, `route_resolve`, venue rule, `route_advise` |
| **L3 Task control** | continue, switch workflow, escalate, stop | minutes, within a task | — (nothing yet) |
| **L4 Planning & learning** | which workflow / parameters to *learn* for which kind of task | days–weeks (fitting) | xval bandit, learn chain |
| **L5 Labels & verification** (cross-cutting) | what actually happened, and was it right | offline or per task | `labels.py`, codeqa grounding, xval reviews, skill bench |

Learning and acting are on different layers: a model is *fitted* at L4 but *acts* at the layer of
its decision — the xval bandit is fitted on past runs (L4) yet picks arms before each review (an L2
dispatch decision); a JEPA world model is trained at L4, and its switch/stop output is an L3 action.

## 2. Evidence levels

E0 idea · E1 designed · E2 pilot measured · E3 held-out validated for the decision it drives ·
E4 shipped and monitored. E4 for code means the instrument runs; it says nothing about whether the
idea it measures works.

## 3. The experiments

### 3.1 Zeno — two different things under one name

| | Zeno p^n (shipped `zeno report`) | Zeno progress detector (P6) |
|---|---|---|
| Question | How do per-call failures compound over a session? | Is the agent closing a shrinking share of the remaining distance, so steps-to-goal → ∞? |
| Layer | **L1** (reliability readout) | **L3** (stop / switch / escalate trigger) |
| Evidence | **E4** as a readout. 8,464 calls, p = 97.4% overall but 99.7% within sessions (the headline mixes in session-less Codex outage traffic); 201+-call sessions: 38% clean observed vs 42% p^n | **E1** + a day-0 proxy: of 25 tasks with ≥ 3 parsed test runs, 21 reached 0 failing, 3 flat/rising, 1 decreasing but stuck above 0 |
| Strength | honest SLO horizon ("≥ 90% clean needs ≤ 4 calls at this p"); failure-kind discovery (Chao1 ≈ 9 kinds) | the right question for agents; closed form (v_∞ = v_t + Δ_t/(1−r), with Δ_t = v_{t+1} − v_t); no frontier calls needed |
| Weakness | not a decision tool; treats calls as independent | needs a progress signal per step; rare at today's volume |
| Next | keep as readout; drop the misleading pooled headline | E2: proxy detector (failing tests, open errors) vs a step/time cutoff baseline (P6 G1 criterion 5) |

### 3.2 Markov — three uses at three layers

| Use | Layer | Evidence | Strength / weakness |
|---|---|---|---|
| **Burst chain** over call outcomes (`markov.py`, `zeno report` 1b) | **L1** retry policy | **E2** (both). Claude sessions: r1 0.271 from only 3 fail→fail pairs; held-out Brier 0.081 vs p^n 0.098, paired diff +0.018 [−0.002, +0.040] (not significant). Codex, ad-hoc pseudo-sessions (10-min gap split; session ids missing until today; one-off numbers, not reproducible until the split is stored as a script — owed): r1 0.375, P(fail \| fail) 0.40 vs 0.039 stationary, runs up to 16; held-out Brier 0.398 vs 0.511 | Observed: right after a failure the next call failed 30–40% of the time vs < 4% overall. That is a **hypothesis for the retry policy** (wait or switch rather than retry at once), not a validated policy: the held-out check scores session cleanliness, not competing retry actions; that needs an A/B on the retry action. Both models are badly miscalibrated on Codex because failures cluster by **day** (69 on 10-02, 1 on 10-07). **2026-10-08:** the A/B is now instrumented — **E1** (telemetry v11 `retry_arm`, opt-in `APEX_RETRY_AB=1`, readout `apex-router retry-ab report`; no randomised rows yet). Pre-A/B baseline: 374 retried requests, 73.3% [68.6%, 77.5%] recovered; ~53 retried requests/day → 30/arm in ~1–2 days, but 30/arm only detects a ~32-pt difference (10 pts needs ~308/arm, ~12 days) |
| **Regime model** — 2-state HMM over the time-ordered call stream, plain and with a per-state burst chain; EM-free day mixture as baseline (`regime.py`, `zeno report` 1c) | **L1** | **E2** (2026-10-08; `docs/research/2026-10-08-l1-regime-and-retry-ab.md`). 28,234 proxy calls: the plain HMM only re-finds the bursts (degraded state 83% fail, mean dwell 3.2 calls → "bursts only"). With a burst chain per state, a slow regime appears: base fail 0.13% vs 2.91% after an ok call, dwell ~340 / ~426 calls, stationary 55.6% degraded; Viterbi marks 08-24 → 10-02 degraded and 10-03 on normal. Held out (cut 10-02 17:57; 94 train / 41 test sessions): Brier iid 0.162, chain 0.116, HMM+burst 0.112, filtered 0.110 — vs chain +0.006 [+0.002, +0.011] (session bootstrap) | The switch on 10-02/03 is the **v8 transport-retry deploy** (SSLError/ReadError now retried), not an upstream recovery: on the post-v8 window (11.6k calls) both HMMs say "bursts only" and no day decodes degraded. So on proxy data the day-level effect was the instrument changing; the small held-out gain is adapting to that level shift. The Codex day clustering (pseudo-sessions, not proxy rows) is untested here |
| **Absorbing chain** over action classes / latent states | **L3** (expected steps, ρ(Q) → divergence) and **L4** (workflow value) | **E2** prototype: 13,034 tool calls; expected 167 calls START→END; next-action perplexity 6.92 → 5.34 (order 1) → 5.22 (order 2) → 5.63 (order 3 overfits) | Interpretable, closed form (N = (I−Q)⁻¹, P(success) = N·R). Needs absorbing outcome states, i.e. labels |
| **Markov inside JEPA** (k-means on latents → chain) | **L3/L4** | **E1** (P6) | Turns a learned representation into expected steps, absorption odds and the ρ(Q) divergence test |

### 3.3 JEPA world model (P6) — L4

**E2 (2026-10-07): first G1 attempt FAIL** on 23.6k steps / 536 tasks — JEPA 1.777 vs logistic
hand features 1.673 nats/step (rel −6.2% [−7.8%, −3.2%]); beats prior and Markov; early epochs
outside the SIGReg bound; outcome criteria inconclusive without gold labels
(`docs/research/2026-10-07-p6-g1-attempt-1.md`). Earlier, **E1 with a negative baseline probe** (not a tested JEPA): frozen request embeddings predict the task's action mix only ~1.5% better than the
prior (1.798–1.801 vs 1.825 nats/call); action history saturates at order 1–2. Compute is not the
limit (MLX on M4 Pro: 11.8M params at 1,535 samples/s). Data is: ~13k main-session steps, ~475/day.
Strength: the right architecture for L4 *if* targets are structured task state (not text), with
SIGReg, k-step + value head, macro-steps, and the H-JEPA perceptual/control split. Gate G1 (5
criteria) is pre-registered; first attempt expected to fail; drop at 50k steps if it still does.

### 3.4 Difficulty teacher → student router — L2 feature

**E2.** 60-task pilot: test–retest ρ 0.98, opus vs gpt ρ 0.93 (tier κ 0.45), vs effort ρ 0.47
(length 0.28, embedding kNN 0.29), but the score tracks length (ρ 0.72); a student copies it at
ρ 0.71 — exactly as well as length. ≈ $0.007 per call. **Strength:** cheap and repeatable; its
correlation with effort is higher than the simple baselines' (directional — overlapping intervals,
no paired test). **Gap:** measures "looks hard", not "cheap tier fails"; zero cheap-tier outcomes to
calibrate against (7 d: opus 3,493 requests, sonnet 39, haiku 9). Two-stage design (distill, then
calibrate on outcomes) and gates G-A/G-B/G-C in the research note.

### 3.5 Routing baseline and gates — L2

| Item | Evidence | Note |
|---|---|---|
| Context-size venue rule (Kimi k3 / k2.7-code) | **E4.** Corrected eligible-slice price ratio 1.88×; latest watch: 4,197/4,197 Codex requests fit the 250k downshift ceiling | **The baseline every learned router must beat** (`PHASE1` kill criterion) |
| `route_advise` cost gate | **E4** code, thin data: debug n 52 (4% escalated), review n 18 (11%); 24 h escalations 4/4 provider errors | Threshold is on the *escalation* rate (0.80 at ratio 5), not on P(kept) — corrected 2026-10-07 |
| Task classifier (`classify.py`) | **E4** | 5 task types; conservative heavy default |
| Phase-as-routing-state (P4, AgentKV) | **E0** | no `phase` field yet |

### 3.6 Labels and verification — L5 (the bottleneck under everything)

| Item | Evidence | Note |
|---|---|---|
| Outcome labels (`labels.py`) | **E4** pipeline, **E1** data: 818 tasks; judge results kept for 781; **16 emitted** (posterior ≥ 0.8), **gold 0** / target 100 | `apex-router labels review` is the single highest-leverage action in this map |
| Shadow A/B labeler | **E1** spec, not built | turn-level grades ≠ task-level routing outcomes (xval finding) — screening signal only |
| codeqa grounding oracle | **E4**: ledger (458 asks) 1,687 grounded / 0 stale / 136 hallucinated citations (7.5%) | a fact check, not a quality score |
| xval reviews (Codex) | **E4**: 22 runs in 24 h, 19 reached a verdict | its bandit is §3.7 |
| Skill-quality bench | **E4** built, measure-only; ledger 5 rows; corpus exhausted at window 2026-10-07 | needs more tasks to keep accruing |

### 3.7 Planning and learning — L4

| Item | Evidence | Note |
|---|---|---|
| Workflow graph optimizer | **E1**/E2 day-0 (classifier fixed 44% → 2% "shell"; uniform PageRank ≈ visit frequency; no bridges in the action graph) | P0 labels gate everything |
| xval P2C bandit (output cap × scope arms) | **E4** code; `zeno report` verdict **insufficient** (needs ≥ 3 arms with ≥ 20 runs and ≥ 1 failure) | the one live learning loop |
| Learn chain (WP1–WP9) / RAG–QLoRA | code shipped (`chain_*`, `learn_chain`, `qlora_serve`; reconciliation "SHIP"); **no chain run data found** in `~/.apex-router` | status lines in `DESIGN-learn-chain.md` are stale ("not yet implemented") |

### 3.8 Serving, cost, observability — L0

| Item | Evidence | Note |
|---|---|---|
| Widget: pressure, quality, network, GPU clients | **E4**, numbers verified against raw sources | |
| Cache handoff threshold | **E4**: 25M tokens — the configured lower clamp; the measured 14-d median over 81 sessions is 1,190 tokens (p80 3.7M), so the clamp, not the data, sets it | |
| Cold cache on hosted APIs | measured: 7 d Claude 836M prompt tokens, 32.8M cache writes (3.9%); **79%** of tokens in > 20k writes follow a 5–60 min idle gap; ≈ $615 at the stale list table, of which ≈ $123 is the write premium over plain input | input to §5 |
| System-design plan (stage timings, retry linkage, total duration) | **E1** | total duration is still missing from telemetry |
| Meter-calibrated cost (SemiAnalysis method) | **E0**; Foundry has no subscription meter (29 `measure` events, none with a limit %) | only on a subscription setup |
| Local prefix-capacity sweep (P2) | **E1**; prerequisite KV-pressure instrumentation missing | becomes central under §5 |

## 4. What was missing from the list (added here)

1. **Regime model for L1** (normal vs outage, e.g. a 2-state hidden Markov model or a per-day mix):
   the Codex miscalibration is a day-level effect neither chain can fit. ~~E0~~ **E2** (2026-10-08,
   §3.2 row, `zeno report` 1c): on proxy data the only slow regime is the v8 retry deploy (degraded
   08-24 → 10-02, normal since); post-v8 the stream is "bursts only". Re-run 1c after a real upstream
   incident; the Codex day effect needs Codex session ids in proxy rows first.
2. **One shared evaluation protocol**: session-level time split, session-cluster bootstrap, tuning
   inside train, test scored once. Every doc above restates it differently. E0.
3. **One task table** joining transcripts, telemetry, labels, teacher scores and phase, keyed by
   task id — today each experiment re-extracts. E0.
4. **Calibration layer (Stage B)** that turns any score (teacher, JEPA value, Markov P(success)) into
   P(acceptable | tier) and P(escalate | tier) on outcomes. Shared by L2, L3 and L4. E0.
5. **Infra vs capability failure split** in labels (all 4 recent escalations were infra). E1.

## 5. Shared, open-weight models: what they unlock for KV cache, research and implementation

### 5.1 The change

Today apex-router is a client of hosted APIs: it sees `cache_read` / `cache_write` counts in usage
headers and nothing else — KV capacity, eviction and the model's internals are unobservable for
hosted traffic (P2). A **shared open-weight server** (team box on-prem, or GPU VMs in our tenant;
meeting brief options C/D) running an engine with automatic prefix caching and KV offload (vLLM's
prefix caching, SGLang's radix cache, LMCache-style CPU/SSD tiers — named from general knowledge,
versions and features not checked) makes the cache of *that* server observable and tunable. What it
changes in the backlog, precisely:
- **P2** (prefix working set / capacity): already testable locally on ollama; a server makes it
  testable at realistic concurrency.
- **P1** (shared KV across replicas, transfer integrity): stays out of scope with one server; it only
  becomes relevant with ≥ 2 replicas and KV transfer between them — not proposed here.
- **P4** (AgentKV): its accepted hypothesis is phase as *pre-routing telemetry*; that needs no server.
  Phase-aware KV eviction would be a new, separate experiment.

### 5.2 KV-cache benefits, grounded in today's numbers

| Benefit | Today | With a shared server |
|---|---|---|
| **Local reuse across consumers** | codeqa: cached tokens recorded on only 110 of 458 asks; on those, **26%** (266,513 / 1,018,013) — the rest unknown. Review offload: 119 calls, **0.1%** (120 / 93,546). Eviction between consumers sharing one ollama model is plausible, not shown | a prefix/radix cache over many slots + a shared corpus prefix (tool schemas, judge prompt) — a hit rate we can measure and tune (P2 sweep) |
| **Cold rewrites after idle** | hosted: 79% of large cache-write tokens follow a 5–60 min idle gap; TTL is the provider's | *possibly* KV kept across idle gaps (CPU/SSD tiers, our own retention) — only for work moved to the open model, and only if the chosen engine and configuration support it; to be shown by a retention test at S1 |
| **Observability** | capacity, evictions, working set: unknown for hosted traffic | measured directly on our server → P2 estimand becomes real |
| **Eviction policy experiments** | no KV access | possible in principle (e.g. keep shared prefixes, drop per-turn scratch); safety and benefit unshown |
| **Batching / cost** | per-token billing, provider meters that change silently | fixed hardware cost; offline jobs (judge, teacher, replays) batched when the box is idle |

What it does **not** do: it does not cache Opus. The hosted cold-cache cost (~$123/week write
premium at stale list prices) only moves to the extent work moves to the open model.

### 5.3 Research it unlocks (each tied to an experiment above)

All of these are **hypotheses to test**, not established benefits.

| Capability (needs weights) | Could feed | Caveat |
|---|---|---|
| **Token log-probabilities / entropy** while the model works | L3 Zeno (uncertainty trend as one progress signal); L5 labels (confidence feature) | arrives *after* generation starts, so it cannot inform the initial L2 dispatch of the same task; its link to error rate here is unmeasured |
| **Prompt-side signals** (e.g. prompt perplexity under the open model) | L2 difficulty feature without a frontier teacher | available before dispatch; value unmeasured |
| **Hidden states** | L4 JEPA context encoder | a richer input, but JEPA's blocker is structured targets and data volume, which hidden states do not supply |
| **Reproducible replay** (pinned weights, seed, engine, batch settings) | L5 re-scoring, held-out evals | fixed seed alone does not guarantee determinism on GPU engines, and tool state must be replayed too |
| **Cheap whole-task runs on the open model** | L2 exploration *for the open model's own tier* | outcomes for an open model are not outcomes for Sonnet / Haiku; the hosted-tier positivity gap stays |
| **Fine-tuning** (QLoRA; `qlora_serve` exists) | L2 student router, L5 local judge, L3 detectors | each distilled model needs its own gate |
| **Prefix-sharing experiments** | P2, §5.2 | measurable on one server |

### 5.4 Costs and risks

- **Quality:** local models are helpers, not replacements, today — review offload 40/119 ok,
  ~8% hallucinated codeqa citations. Every lane moved needs its own gate.
- **Two different side channels, two different controls:**
  - *Prefix-cache timing* (a hit is faster, so a user can learn whether someone else sent the same
    prefix): per-tenant cache salt / namespace, and share only prefixes cleared as non-sensitive —
    system prompts and repo digests are not automatically public.
  - *CPU-side detokenization leakage* (`SECURITY-local-serving.md`): a co-resident attacker on the same
    CPU; separate UIDs and authentication are explicitly **not** sufficient. Controls: no untrusted
    co-tenants on the host, VM-level isolation, SMT/cache partitioning where warranted.
  - Authentication in front of the server is needed regardless (ollama today has none).
- **Operations, hardware, model provenance and license** (meeting brief points 5–6).
- **On a Mac:** the mainstream prefix-caching engines target CUDA; the realistic test bed is a Linux
  GPU host or in-tenant GPU VM. The laptop can still run P2 on ollama / MLX.

### 5.5 Implementation path (each step gated)

| Step | What | Gate to continue |
|---|---|---|
| S0 | Record cached tokens on every local call (codeqa logs it on 110 of 458 asks); instrument KV pressure; run the P2 prefix-working-set sweep on ollama | a complete local hit-rate baseline + the curve vs prefix length × concurrency |
| S1 | One open model behind a prefix-caching engine (in-tenant GPU VM or team box); apex proxy routes the offline lanes there (labels judge, difficulty teacher, codeqa, replays) with cache-hit telemetry | lane quality ≥ today's local lanes; hit rate above the S0 baseline |
| S2 | Shared-prefix registry for prefixes cleared as non-sensitive, with per-tenant salt | cache-timing test across tenants shows no hits on private content; separate isolation review for CPU side channels; hit-rate gain measured |
| S3 | Research hooks behind a flag: log-prob / entropy and hidden-state export, deterministic replay | feeds §3.1–3.4 experiments; privacy review of what is stored |
| S4 | Move a production lane only where its gate (Stage B calibration, G-B) passes on that lane | per-lane, never wholesale |

## 6. Where the strength is, in one table

| Experiment | Layer | Evidence | Strongest point | Blocking gap |
|---|---|---|---|---|
| Markov burst chain | L1 | E2 | after a failure, 30–40% next-call failure vs < 4% | retry-action A/B (instrumented, E1; needs `APEX_RETRY_AB=1` and ~300 retries/arm for a 10-pt effect) |
| Regime model (HMM) | L1 | E2 | finds a slow regime only with a per-state burst chain; filtered held-out Brier beats the chain by 0.006 [0.002, 0.011] | the one regime found is the v8 proxy change; needs a real upstream incident |
| Venue rule | L2 | E4 | 1.88× on the eligible slice; the baseline | — |
| Difficulty teacher | L2 | E2 | repeatable (ρ 0.98), cheap; directional edge over baselines | cheap-tier outcomes |
| Zeno progress | L3 | E2 (code + synthetic; real data unscored) | right question, closed form; plateau false positives removed | gold labels; only 16/43 test tasks carry parsed test runs |
| Markov absorbing chain | L3/L4 | E2 | interpretable expected steps / odds | absorbing outcome labels |
| Workflow graph | L4 | E1–E2 | design + day-0 baseline | labels |
| JEPA | L4 (fit) → L3 (act) | E2 (G1 attempts 1–2 FAIL) | beats prior and Markov on next action; C2 inconclusive (8 gold test tasks, below the declared floor) | loses to logistic by 4% (was 6%); C4 unreachable through warm-up; gold on test; data volume |
| xval bandit | L4 (fit) → L2 (pick arm) | E4 code, data insufficient | the only live learning loop | runs per arm |
| Zeno p^n | L1 (readout) | E4 | honest horizon readout | not a decision tool |
| Outcome labels | L5 | E4 / data E1 | pipeline + precision accounting | **100 gold labels** |
| Shared open weights | L0 → all | E1 | observable KV, model internals, cheap open-model runs (hypotheses) | quality gate per lane; isolation review |

**Order that unblocks the most:** gold labels → one task table + shared eval protocol → Stage B
calibration layer → L3 proxy Zeno detector (cheap, on data we have; the L1 regime model is done, E2) →
S0 local KV sweep → everything that needs scale (teacher at scale, JEPA, shared server).

## 7. Cross-validation record (xval `68db2eb9274f`)

| Finding | Triage | Change |
|---|---|---|
| codeqa 7% treated unknown `cached_tokens` as misses | confirmed: recorded on 110/458 asks; 26% on those | §5.2 row, S0/S1 gates |
| "Shared server reverses P1's rejection" | confirmed: P1 is about multi-replica KV transfer; P4's hypothesis is telemetry | §5.1 rewritten per item |
| Security controls conflated two side channels | confirmed against `SECURITY-local-serving.md` | §5.4, S2 gate |
| Research benefits overstated (entropy timing, hidden states, determinism, open ≠ hosted tiers) | confirmed | §5.3 rewritten as hypotheses with caveats |
| Retry recommendation presented as validated; Codex numbers not reproducible | confirmed | §3.2 E2, hypothesis + A/B; numbers marked one-off until the split is a stored script |
| Pass 2 (`93cd9f0d8f4a`, GO-WITH-CHANGES): idle-gap retention still promised; Codex numbers still unreproducible | confirmed | §5.2 retention is a hypothesis with an S1 test; §3.2 marked one-off |
| Zeno limit dropped one increment | confirmed (0.2 + 0.2/(1−0.5) = 0.6, not 0.4) | fixed here and in `RESEARCH-FIT-BACKLOG.md` P6 |
| Evidence levels inflated (E2⁻ for JEPA, "beats baselines", E4 = validated) | confirmed | §2 definition, §3.3, §3.4, §6 |
| Learning vs acting layers mixed | confirmed | §1 note, §6 rows |
| Stale / misread numbers (426/39 grounding; 781/818 judged; 25M clamp vs 1,190 median) | confirmed | §3.6, §3.8 |
