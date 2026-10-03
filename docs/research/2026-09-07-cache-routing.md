# Cache-value routing: evidence and implementation priorities

**As of 2026-09-07, 06:23:56 UTC · checkout `45a8722` · local-machine study**

**Status: source-verified draft; independent adversarial review incomplete.** This study is not authorization to promote a production routing policy.

## Decision

**Build measurement and identity first; do not enable sticky routing yet.** The digest identifies a useful design direction, but the available evidence does not establish a material routing-induced cache loss. The strongest immediate finding is **accounting/schema drift**, including a newly documented OpenAI cache-write field that Apex currently ignores.

Recommended order:

| Priority | Change | Benefit / cost | Decision |
|---|---|---|---|
| P0 | Provider/model-version-aware usage normalization and one rate-card implementation | Reliable economics; small–medium | Do first |
| P0 | Request-kind, dispatch/attempt/session lineage, revision provenance, cache scope, and real token timing | Makes the hypothesis identifiable; medium | Instrument before scoring |
| P1 | Conservative cache-affinity compatibility envelope | Prevents stale routing assumptions; small schema, medium integration | Add metadata/checks, default-off authority |
| P1 | Profile proxy pre-forward work and restore outcome collection | Existing measurable gaps; small investigation | Parallel work |
| P2 | Offline/cache-value shadow scorer at the model-selection boundary | Potential value, presently unproven; medium | Only after measurement coverage improves |
| P2 | Narrow, quality-gated local extraction/synthesis at fresh task boundaries | Plausible independent savings; medium | Benchmark, not general routing |
| Defer | Cross-model KV translation, KV paging/compression, declarative attention | Engine/model work, not transparent routing | No proxy implementation |

No production code, live settings, model residency, or routing policy was changed for this study. Existing uncommitted documents were left untouched.

## 1. What the metrics actually say

Source: a fixed copy of local `~/.apex/telemetry.jsonl`, cut off before study-generated inference. Companion: [`2026-09-07-cache-routing-metrics.json`](2026-09-07-cache-routing-metrics.json). No prompts, raw session IDs, or credentials are in that aggregate.

### Coverage and cache use

| Metric | Last 7 calendar days | Available data within last 30 days |
|---|---:|---:|
| Request rows, excluding heartbeats | 42 | 1,694 |
| Usage captured | 35 | 1,497 |
| Requests with session ID | 39 | 1,128 |
| Active UTC dates | 2 | 12 |
| Cached-read / all input tokens | 96.70% | 97.03% |
| Observed multi-request **generation** groups | 1 | 35 |
| Generation groups with model-label changes | 0 | 2 |
| Adjacent generation model-label changes | 0 | 2 |

The longer slice runs **2026-08-15 01:42:23 through 2026-09-07 06:22:30 UTC**, not a complete 30-day observation period. In the latest seven days, request rows occur only on August 31 and September 7. This is local proxy coverage, **not all Apex/Pi/Codex activity and not fleet metrics**. Missing usage may represent auxiliary requests, unsupported capture, or failures; it is not measured zero spend.

Normalization for the observed historical wires:

- Anthropic: total input = fresh `input_tokens` + cache reads + cache creation.
- OpenAI-compatible **Kimi rows in this snapshot**: total input = `input_tokens`; reads are a subset, so fresh = total − reads. No separate writes were captured for this cohort.
- Count only supported-schema rows with `usage.captured=true` in token denominators. All captured rows here are unflagged for error.
- Report token-weighted read fraction separately from “a request had any cache read.” The latter is 1,434/1,497 across the longer slice and is not the same metric.

The existing `cache_report.py` reports **49.16%** for this same seven-day snapshot because it counts the cached OpenAI-compatible input twice; the corrected fraction is **96.70%**. This is a reporting error, not a measured cache regression.

The longer-slice read fractions are **96.63% Anthropic wire** and **97.52% Kimi/OpenAI-compatible wire**. Thus “roughly 98%” is a reasonable historical order of magnitude, not a sufficient economic argument. A hit statistic excluding cache creation would flatter the Anthropic result.

### The switching cohort is much smaller than a naive query suggests

Grouping all identifiable rows by `(client, session_id, agent_id)` and sorting by arrival timestamp produces **179** model-label transitions in **7/41** multi-request groups. This is misleading: **98/99 requests asking for `claude-sonnet-5` have no captured usage**.

Restricting to usage-bearing, unflagged generation rows leaves:

- **2/35 groups (5.71%)** with a model-label change, **two transitions** total.
- One `claude-opus-4-8 → claude-opus-5`; one `claude-sonnet-5 → claude-opus-4-8`.
- The first generation after each transition has zero cache reads; together these two rows have **81,539 cache-write tokens and 4 fresh tokens**.
- No observed wire-label switch within these groups. **Provider switching cannot be measured**: `endpoint_id` identifies a wire family, not an authenticated provider/deployment/cache pool.

These remain **observed label changes, not confirmed router switches**. All request IDs and agent IDs are absent; arrival order is not necessarily causal order; session matching can split logical continuations; `model_resolved` can merely copy the requested alias. Generation-only filtering also skips unobserved intervening activity. Auxiliary traffic, child dispatches, model upgrades, TTL expiry, changed prefixes, and real route choices cannot be cleanly separated.

### Dollar ceiling: small in the visible cohort, unknown globally

Use these as **repository-rate scenarios, not bills or current verified list prices**. The pinned `readout/pricing.py` table prices Opus at $15/$1.50/$18.75/$75 per million fresh/read/write/output tokens. Another report uses $5/$0.50/$6.25/$25. This disagreement alone blocks a trustworthy dollar promotion gate.

Under the former scenario:

| Quantity | Modeled amount | Meaning |
|---|---:|---|
| Priced captured traffic, longer slice | $302.43 | One captured row is unpriced; uncaptured usage excluded |
| Latest seven-day captured traffic | $2.77 | Only 35 captured requests, all Kimi |
| Optimistic savings on first rows after the two generation changes | $1.41 | Pretend all their fresh/write input could have been read |
| Optimistic savings on **all** fresh/write input anywhere in the two changed groups | $3.76 | Deliberately over-generous same-model cache-recovery envelope |
| Same-model envelope across all priced captured rows | $65.08 | Includes starts, novel suffixes, TTL/edits; not route-attributable |

Formula for that deliberately optimistic cache-only envelope:

```text
fresh_tokens × (fresh_rate − read_rate)
+ write_tokens × (write_rate − read_rate)
```

These are **not counterfactual policy savings**: staying on a different model changes rates, output, quality, and later requests. The first-row envelope is 0.47% of the captured priced total; the whole visible changed-cohort envelope is 1.24%. Neither identifies avoidable losses.

Crucially, **517/1,497 captured generations lack session IDs**, representing **$206.76/$302.43 (68.37%)** of scenario cost. Session attribution covers only 31.63% of priced captured cost. **We cannot conclude that total route-induced loss is below 5%.** The correct result is “visible opportunity small; global ceiling unknown,” not “proven uneconomic.” Five percent is an engineering-budget heuristic, not a research-derived constant; absolute dollars and latency SLO value also matter.

### Timing and health

- Latest seven-day captured, unflagged requests: client first-byte p50 **12.81 s**, p95 **20.55 s**, n=35.
- In that same cohort, proxy pre-forward p95 **276.18 ms**; longer-slice p95 **73.74 ms**, n=1,497.
- `ttft_ms` is actually **arrival → first raw response byte**, not first generated/content token. `apex_added_ms` on successful rows is pre-forward time, not all proxy overhead. Neither isolates cold prefill or GPU queue time.
- There are 18 flagged errors in the longer slice. HTTP 4xx is not comprehensively represented by `is_error`; the handler flags transport/stream errors and HTTP 5xx. Do not call 18/1,694 an all-failures rate.
- All rows report `bust=false`. They carry transformation-shadow observations; the handler does not establish a cache-miss detector. **Zero bust flags is not evidence of zero cold starts.**

Profile request-body acquisition, shadow work, hashing/matching, and store operations separately before changing routing. The latency measurements motivate investigation, not a claim that any particular component is the root cause.

### Outcome/local evidence

Readouts collected in this session:

- `route-advise`: debug n=5, generate n=1; both **INCONCLUSIVE** under its n=30 floor.
- `route-join --json`: 6 outcome rows × 4 conformance rows, **0 joined labels**.
- Aggregate exporter: skill/proxy route tables each have **0 cells / 0 promotions**.
- Local offload log: **37 rows**, all labeled `ornith-35b`, latest **2026-08-24**. Review: **35/35 escalated**, zero saved completion tokens. Codegen: one gated success, **25 completion tokens** credited. No current-revision general-quality conclusion is possible from this.
- Live SQLite aggregate inspection: sessions/chain populated; **freeze, prefix_hashes, epochs, and ccr each have zero rows**. The existing cache-safety machinery is not evidence of deployed KV-cache management.

## 2. Required code changes—not a greenfield router

### A. Fix accounting before optimizing it

1. **Unify report normalization and pricing.** `scripts/cache_report.py:28-33` has one Opus-like rate for every model; its `summarize_cache` treats `tokens_in` as fresh on both wires. In contrast, `src/apex_router/proxy_engine/readout/doctor.py:237-257` has wire-aware subtraction and `readout/pricing.py` uses model-family scenario rates. Reuse a single pure normalization/rate-card layer; pin provider, exact SKU/API era, currency and effective dates; unknown prices remain unpriced, never silent zero.
2. **Support current OpenAI cache-write accounting.** The official [Prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching), retrieved September 7, documents GPT-5.6+ `input_tokens_details.cache_write_tokens`, write/read multipliers 1.25×/0.1×, `prompt_cache_options`, and model-dependent cache behavior. `src/apex_router/proxy_engine/proxy/usage.py:197-206` currently reads only cached input on Responses; `readout/pricing.py` assumes zero writes for the `gpt-5` substring family. A synthetic reproduction using the guide's 15,000-total / 12,000-read / 3,000-write usage shape yields **zero captured creation tokens** today. Preserve raw provider counts and normalize disjoint fresh/read/write pools by explicit adapter version; do not apply new OpenAI rules to Kimi merely because the wire is compatible.
3. **Capture write TTL partitions and accounting completeness.** Anthropic 5-minute versus 1-hour writes cannot be faithfully priced from a single undifferentiated creation total. Distinguish partial input capture from terminal output completion, absent usage from zero, and requested from provider-confirmed model. Historical lost fields cannot be reconstructed by a reporting fix.
4. **Fix window semantics.** `cache_report.py` defaults its clock to the newest log timestamp, which can present old traffic as a recent week; it also does not exclude future/null-timestamp rows. Emit fixed wall-clock boundaries, latest observation, coverage gaps, and explicit exclusion counts. Never extrapolate this sparse week into spend claims.

### B. Correct the outcome estimand before training

`docs/PHASE1-outcome-router-research.md` defines τ as cheap-model **success** probability, but then refers to 0.80 at cost ratio 5 as its success threshold. `src/apex_router/route_advise.py:12-23` correctly defines 0.80 as the **escalation** break-even. Under that simplified cost-only model:

```text
C_cheap + (1 − τ) × C_heavy < C_heavy
⇔ τ > C_cheap / C_heavy = 0.20 when the ratio is 5
```

Fix the document/learning-target orientation before turning it into a router. This is **not** permission to accept 20% quality: actual quality eligibility is a separate hard gate, and cache/validation/recovery costs change the break-even. Likewise, an approximate exploration convention is not randomized assignment with an auditable propensity.

### C. Put routing in the correct layer

- `src/apex_router/route_resolve.py:136-224` and `consumer.resolve` implement **model selection** against task/venue/default evidence. Add cache-aware state as an explicit input to this boundary and its actual dispatch callers.
- `src/apex_router/proxy_engine/pipeline/decide.py:1-35` selects **block transformations**, not backends. Its contract forbids economics, tokenizers, and online estimation on the hot path. Do not insert a learned backend scorer here.
- `src/apex_router/proxy_engine/proxy/upstream.py:93-94` selects a configured upstream by client wire. It is not a multi-provider load balancer. A transparent proxy must not silently replace the client's requested model/protocol.
- `policy.py` already seals transformation policy artifacts. Reuse its control/enforcement separation, but introduce a versioned **routing-policy artifact** rather than pretending block-transform rules already encode route utility.
- `src/apex_router/ornith/model_router.py:53-131` supplies capability/tier hints and `needs_switch`, not measured quality probability. `reuse_cache = items > 1` is an intent hint, not confirmation that the items share a prefix or that a cache is resident.

### D. Add a conservative affinity envelope, not fictional KV ownership

The proxy stores hashes/renderings, **not model KV tensors**. The immediate safety invariant is “never credit a route with cache value from an incompatible or unknown identity,” not “the router now safely transfers KV.” Provider-owned cache correctness remains provider-owned.

Maintain logical session/branch identity separately from a bounded map of per-route cache observations:

```text
logical_session_id, branch_id, parent_dispatch_id
provider_id, deployment_id, processing_region, opaque_auth_scope_id
requested_model, resolved_model, model_revision, revision_source
cache_namespace, cache_protocol_version, canonicalization_version
prefix_fingerprint, prefix_length, fingerprint_kind
last_read_tokens, last_write_tokens, usage_complete
observed_at, ttl_or_expiry, residency_evidence_source
routing_policy_version, route_epoch, state_generation
```

For local engines, include tokenizer/chat-template, adapter/LoRA revision, runtime/cache layout and dtype, and multimodal preprocessing identity where relevant. [vLLM's primary design documentation](https://github.com/vllm-project/vllm/blob/main/docs/design/prefix_caching.md) explicitly hashes parent/block tokens and extra identifiers including LoRA, multimodal content and cache salts.

Rules:

- Compatibility is **necessary, not sufficient** for a cache hit: valid prefix extent, expiry, eviction, and provider-side routing also matter.
- Unknown immutable revision means unknown compatibility. Do not equate two null revisions or promote a client alias to a confirmed model revision. Default forwarding still succeeds; only optimization authority/cache credit is withheld.
- Keep existing logical-message canonical hashes separate from the raw-emission prefix guard (`src/apex_router/proxy_engine/session/identity.py:1-25`, `src/apex_router/proxy_engine/pipeline/freeze.py:36-129`). A full growing-prefix hash need not equal the preceding full hash; compare the previous extent. Neither hash reveals a proprietary provider's exact cache key.
- A switch **does not prove eviction of the old route's cache**. A return may still hit within that route's lifetime. Model per-route observations; do not charge a full rewrite on every A→B→A transition automatically.
- Cache scope must distinguish tenant/account/region without logging credentials. A namespace is not permission to reuse state across tenants.
- Separate affinity-policy invalidation from content/KV incompatibility. Updating a routing threshold need not flush a still-compatible cache. Use an atomic route epoch/state-generation check for concurrent requests and stale completions.
- Stable cache hints at the **client adapter** may help when supported. OpenAI explicitly says `prompt_cache_key` influences routing but does **not pin a machine or guarantee a hit**. Do not mutate request bytes in the measuring proxy to add one.

Rejection taxonomy should cover revision/model/scope mismatch, prefix divergence, expired evidence, missing identity, and policy incompatibility. Log `capacity_pressure` only with engine evidence; otherwise residency cause is unknown. Rejection falls back deterministically without resurrecting deleted content.

### E. Make causality and quality observable

Add schema-versioned fields for request kind (generation/count-tokens/probe), API path, response status, dispatch and attempt IDs, explicit parent/branch IDs, identity provenance, selected/candidate routes, route reason, compatibility verdict, cache-blind versus cache-aware decision, predicted warm/cold cost and uncertainty, and outcome linkage.

Record response headers/first byte/first semantic token/completion/cancellation separately. Model-reported reasoning tokens are not necessarily visible-answer tokens. For local serving, add actual prefix-match/residency, prefill duration, queue wait and load when available. Cloud absence remains unknown—do not infer cold-prefill milliseconds from first-byte delay alone.

## 3. What the recent research changes

Primary sources were retrieved on **2026-09-07**. This is a targeted arXiv search, not a claim to exhaustive literature coverage. Dates below identify first submission of the cited version family; these are preprints and author-reported experiments, not reproduced Apex gains. Full-text sections were inspected for the cache/serving papers; LLMRouter's positioning here is abstract-level only.

| Source | Date | Relevant evidence | Apex implication |
|---|---|---|---|
| [Adaptive Context Parallelism for Production LLM Serving (Vertumnus), 2609.04774v1](https://arxiv.org/abs/2609.04774v1) | Sep 4 | Joint queue-delay, cache-aware prefill and GPU-time placement; experiments on 64 GPUs | Include load and prefill, not affinity alone. CP worker split/merge and KV replication stay below Apex. |
| [GrowPage: On-Demand KV Budgeting for Efficient LLM Reasoning Serving, 2609.03494v1](https://arxiv.org/abs/2609.03494v1) | Sep 3 | Dynamic page budgeting/compression during decoding | Engine integration, not a routing feature. No cloud-cache ownership implied. |
| [Language Models Can Control Their Own Attention, 2609.02737v1](https://arxiv.org/abs/2609.02737v1) | Sep 2 | **Declarative Attention** is the method name; global/focus/local declarations are parsed by the runtime | Requires prompt/protocol and runtime cooperation, **not necessarily weight training**. Paper uses off-the-shelf models and reports accuracy drops; skip transparent deployment. |
| [HeadWiseKV: Budgeted Per-Head Cache Residency for Hybrid Long-Context Language Models, 2609.02029v1](https://arxiv.org/abs/2609.02029v1) | Sep 2 | Calibrated per-head residency; quality across four hybrid models, serving study on one | Defer to a local-runtime project with revision-specific quality/physical-memory tests. |
| [CacheBridge: Efficient Cross-Model KV Cache Transfer, 2609.00891v1](https://arxiv.org/abs/2609.00891v1) | Sep 1 | Matched-head affine mapping, attention-weighted calibration, fused construction; three transfer directions | Confirms model-specific translation is a calibrated runtime operation, not transparent cache compatibility. Defer. |
| [TOPAS: Workflow-Aware Prefix-State Scheduling for Multi-Agent LLM Serving, 2608.25523v1](https://arxiv.org/abs/2608.25523v1) | Aug 26 | Joint residency/admission and remaining workflow path; synthetic DAGs and MetaGPT workflows in SGLang | Add whole-task completion and starvation/queue constraints. Do not optimize cache hit rate at the expense of workflow progress. |
| [CacheRoute: Planned Prefix-Affinity Routing for Large-Scale LLM Serving, 2608.19677v1](https://arxiv.org/abs/2608.19677v1) | Aug 20 | Periodic affinity/load plans, measured positive **and negative** workloads; recommends shadow replay | Closest architecture match. Its supplement proposes placement hysteresis but explicitly **does not evaluate a churn-aware algorithm**. Hysteresis is still Apex's testable hypothesis, not a proven imported result. |
| [LLMRouter: Unified Infrastructure for Developing, Evaluating, and Deploying LLM Routers, 2608.06867v1](https://arxiv.org/abs/2608.06867v1) | Aug 7 | Formulates single-turn, multi-turn and personalized routing | The existing Phase-1 document's broad “nobody routes agentic/multi-turn work” positioning is too strong. Evaluate exact workload/labels, not novelty by exclusion. |
| [SMetric: Rethink LLM Scheduling for Serving Agents with Balanced Session-centric Scheduling, 2607.08565v1](https://arxiv.org/abs/2607.08565v1) | Jul 9 | Balance the first session request, cache-aware continuation; global KV store assumptions | Supports evaluating session-boundary decisions; its storage assumptions do not hold for arbitrary cloud providers. |
| [Lodestar: An Online-Learning LLM Inference Router, 2606.00946v1](https://arxiv.org/abs/2606.00946v1) | May 31 | Per-request cluster snapshots and online latency learning; policy-induced distribution shift | Counterargument to blind faith in fixed thresholds. Keep Apex deterministic, but monitor drift and refresh evidence offline; replay alone is not proof of online performance. |
| [Leyline: KV Cache Directives for Agentic Inference, 2606.01065v1](https://arxiv.org/abs/2606.01065v1) | May 31 | Explicit edit directives; distinguishes amortized splice from forgetting/re-prefill | Borrow explicit governance. Do **not** treat position-correct KV surgery as true semantic deletion or full substituted-prompt equivalence. Out of proxy scope. |

Most important counterexample: CacheRoute reports a 32B workload where affinity raises reuse but capacity falls to **0.50–0.67×** cache-blind balancing. Its own positive cluster results therefore do not justify enabling stickiness on this already-cache-heavy, sparsely observed workload.

Corrections to the digest's framing:

1. “Cached state should be governed” is sound; “Apex can govern provider KV residency” is not yet true.
2. Switching models changes the output distribution. **No quality loss cannot be promised from cache mechanics.** Even same-model backend equivalence needs checked settings/revisions.
3. Cache compatibility cannot be established by the suggested tuple alone, nor by canonical JSON equality.
4. Switching a live model does not inherently erase another model's cache; returning with a changed prefix or expired entry is a different issue.
5. The strongest directly applicable research is cache-aware placement with load constraints and negative-result gates—not cross-model transfer.

## 4. Shadow experiment and promotion gates

### Deterministic decision

First hard-filter candidates on policy authority, model/tool/context capability, safety/data residency, readiness, and **held-out quality eligibility**. An unhealthy or unsafe current route is not protected by economic hysteresis. Explicit user model selection remains authoritative.

Then use one unit system. Prefer cost minimization under quality/latency constraints over adding “quality points + milliseconds + dollars”:

```text
J(route) = expected total continuation dollars
          including fresh/read/write input, output, permitted recovery,
          router overhead, and local operating cost

switch only if:
  candidate passes all hard gates and its latency constraint
  AND lower_confidence_bound(J(stay) − J(candidate)) > hysteresis_dollars
```

Warm/cold input cost belongs **inside J once**. Do not subtract cache loss in a score and then charge it again as an external cold-start threshold. Begin with a one-step horizon plus a conservative, offline-calibrated continuation sensitivity analysis; no unsupported future-turn forecasts. Compile coefficients/buckets, evidence versions and hysteresis into a deterministic policy artifact. Stale evidence gives “cannot decide,” not a speculative warm-cache credit.

A compatibility miss removes cache credit; it does not necessarily prohibit an otherwise safe, quality-gated new-route cold start. Hysteresis needs a load/SLO escape hatch before dispatch, not a mechanism for trapping work on a failing backend.

### Run sequence

1. **Instrument and observe.** Require high session/attempt attribution by **cost as well as request count** before interpreting a 5% ceiling. Proposed study gate: at least 95% attributable priced cost, bounded unknown/unpriced exposure, actual sustained traffic across multiple capture windows. This is a proposed engineering threshold, not an existing earned result.
2. **Replay three baselines:** current route policy, session-boundary-only affinity, and cache-value policy with/without hysteresis. Stratify by TTL gaps, request kind, context, revision, branch and readiness. Log how often cache cost changes the cache-blind choice. Keep estimates separate from observed outcomes; replay does not reveal alternative-route latency or quality.
3. **Economic screen:** under a verified rate card, if a defensible *global upper bound* on avoidable cache loss is below the engineering-value threshold, stop cache-only production work. Otherwise retain uncertainty and obtain targeted evidence; today's missing-cost coverage fails this screen's identifiability requirement.
4. **Quality/latency evaluation:** frozen, held-out tasks and session-level/time-ordered splits. Prospective controlled assignments only for explicit safe/read-only tasks, with logged assignment probability and cluster/load effects considered. Do not turn a skill's approximate “10% exploration” convention into a claim of randomized identification.
5. **Canary only after evidence:** lower confidence bound on net benefit above zero, non-inferior accepted quality, bounded p95 semantic TTFT **and total task completion**, no reliability regression, multiple capture windows, deterministic rollback. Apply the existing multiple-testing/replication discipline where applicable.

No shadow policy makes a second production POST. Offline benchmark calls are separately authorized experiments. **Never replay after an ambiguous send/timeout/partial stream**. Readiness fallback happens before sending; any permissible post-response escalation must be explicit, new, and accounted for—not transparent retry of the original operation.

### Required regression matrix

Revision/alias changes; unknown identity; tenant/region mismatch; tokenizer/template/adapter changes; prefix edit/deletion/compaction; growing prefix; A→B→A with still-warm A; expiry/eviction; stale policy and stale completion; concurrent branches; restart and schema migration; lost state; unsupported usage versions; null/partial usage; new cache-write fields; request probes; queue overload; equal-score ties; all candidates unavailable; and ambiguous POST failure with **zero replay**. Preserve existing byte-identical shadow/raw fallback and plane-separation tests.

## 5. Narrow Ornith experiment

Start with **verbatim extraction or constrained synthesis with explicit source evidence**, not general log diagnosis or anomaly explanation where plausible mistakes are hard to grade. Use fresh child-task contexts so the orchestrator's expensive prefix is not rerouted. Keep the orchestrator's model fixed; use scripts for counting/searching and small contexts for lower-tier inference.

Pin model artifact digest, tokenizer/template/runtime, benchmark corpus and verifier version. Compare to the trusted route on held-out tasks with extraction oracles and blinded human/judge calibration for synthesis. The proposed 1-percentage-point quality margin must be a **non-inferiority confidence-bound test**, not point estimates within one point. Determine sample size from baseline error/disagreement rates; the current single codegen pass cannot certify it.

Measure accepted-task dollars, local energy/operational cost, queue/cold-load latency, frontier validation and full escalation cost. Keep review default-off until its net value is actually measured; a review that always escalates is not frontier work avoided. Do not load a different multi-GB tier as a side effect of asking for a route.

## Verification and limits

- Raw counts and monetary scenarios were computed deterministically from the frozen local snapshot; usage semantics checked against scanner code and the official OpenAI guide. The companion JSON preserves numerator/denominator and unknown-coverage details. Raw evidence and the analysis script remain local under `/tmp/apex-cache-study-20260907/`.
- **129 targeted existing tests passed**, one Starlette/httpx deprecation warning. These cover session wiring, freeze storage, passthrough/shadow, plane separation, pricing, route resolution/advice/join and offload accounting. They do not validate an unimplemented cache router.
- Research extraction was delegated to GPT-5.6 Terra at low effort with a bounded public-paper excerpt pack; claims used here were checked against primary text. Claude/Sonnet CLI delegation failed before inference on Azure authentication; one direct local-proxy attempt returned HTTP 401. No auth configuration was changed or ambiguous POST retried.
- Independent adversarial review **did not complete**: the fresh Opus attempt returned upstream HTTP 502 with no review; after resuming, a fresh GPT-5.6 Sol/high read-only reviewer produced no output for over six minutes and was stopped locally. There is no independent approval or refutation to report. Recommendations remain provisional; no routing promotion is authorized by this study.
- Deterministic reconciliation caught and corrected the seven-day generation-group denominator (one, not the two groups obtained before generation filtering) and a stale citation endpoint. A separate arithmetic check verified the success/escalation threshold distinction. The code-grounding oracle validates all 10 code citation spans; that validates locations, not reasoning.
- Authenticated billing, fleet-wide metrics, actual cloud cache identities, and a prospective quality/latency experiment remain unavailable.
