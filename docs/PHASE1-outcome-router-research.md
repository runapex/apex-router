# Phase 1 — agentic-turn routing: positioning, estimand, offline eval design

**Status:** research design, pre-implementation. No gate ships from this document without a
cross-family review pass (handoff guardrail 6). Companion to
`docs/phase0-outcome-router/REPORT.md` (Phase 0: NO-GO pending labels; instrumentation shipped).

---

## 1. Positioning — what's known, what's ours

**The recipe exists.** RouteLLM (Ong et al., LMSYS 2024) trains a small router
(BERT-class / matrix factorization) on *preference* labels — GPT-4-as-judge deciding
which of two models wins — and shows most traffic can downshift at small quality cost.
Hybrid LLM (Ding et al. 2024) routes to small-or-large with a learned DeBERTa scorer.
FrugalGPT (Chen et al. 2023) cascades models of rising cost with a learned accept gate.
AutoMix uses self-verification to decide escalation. Products (Martian, Not Diamond,
Unify, OpenRouter auto-router, NVIDIA's router) ship variants of the same shape.

**What none of them route: agentic API turns.** Their unit is the standalone prompt.
The workload apex-router already serves is different on every axis that matters for routing:

| Axis | Standalone-prompt routing | Agentic-turn routing (measured here) |
|---|---|---|
| Context | ~10²–10⁴ tokens | p50 102k, max 289k per turn (592 context-bearing rows, corrected OpenAI-wire semantics, 2026-08-30 readout) |
| Economics | output-dominated | cache-read + fresh-input dominated (99.97% cache-hit on the Claude side) |
| Decision unit | one request | one turn inside a session with a *shared cached prefix* |
| Price spread | across vendors | 1.88× *within one vendor family* on the corrected 2026-08-30 eligible slice (k3 vs k2.7-code; token-mix dependent) |
| Failure cost | one bad answer | a bounced turn + a retry + the failed attempt's tokens in the shared prefix |

The honest framing (unchanged from the handoff): **the RouteLLM recipe, correctly
targeted — measured-outcome labels instead of judge preferences — on the agentic-turn
workload nobody else routes.** The moat is the label stream and the workload, not the
algorithm. RouteLLM's arena data remains useful as a *capability-gap prior* for cold
cells (Phase 2), never as training labels.

## 2. The estimand, stated formally

For dispatch *i* with pre-routing features *xᵢ* = (task_type, context_size, venue,
surface, session-era), define potential outcomes *Yᵢ(cheap), Yᵢ(heavy)* ∈ {0,1}
(1 = the dispatch's output was kept; 0 = it escalated). The routing question is:

> **τ(x) = P(Y(cheap) = 1 | X = x)** — the cheap model's success probability at *this* x.

Route cheap where τ̂(x) clears the cost-aware threshold (route_advise's break-even,
currently 0.80 at cost-ratio 5); route heavy otherwise; return CANNOT-DECIDE (heavy
default) where τ̂ has no out-of-sample support.

**The label is 1 − escalated, with two named caveats** (from Phase 0, restated because
every downstream claim inherits them): escalation embeds the dispatcher's judgment (not
a pure correctness oracle), and Y(cheap) is observed **only where a cheap start was
attempted**.

## 3. The missing-data mechanism — the problem prior designs ignored

Let *S* ∈ {0,1} mark "cheap start attempted." We observe Y(cheap) only when S=1.
If S depends on X (it does — the skill's routing table tells the orchestrator to start
heavy on hard-looking work) **and** on unobserved difficulty U (it plausibly does — an
orchestrator reads the task), then per-cell rates over S=1 rows are biased estimates of
τ(x): cells look better than they are precisely where dispatchers pre-filter hard cases
to heavy. Three identification routes, in order of preference:

1. **Exploration (chosen).** Make S independent of U given X by design: with fixed
   probability ε, cheap-start cells whose default is heavy (and log the propensity).
   ε = 0.1 to start, capped so expected exploration cost ≤ a set budget per week;
   exploration dispatches are marked `note=explore` so analysis can separate them.
   This is the only route that needs no untestable assumption, and it doubles as the
   remedy for selection lock-in (heavy-default cells would otherwise *never* acquire labels).
2. **Propensity weighting.** Where propensity π(x) = P(S=1|X=x) is known from the
   exploration log, IPW corrects the naive rate. Valid only with overlap (π bounded
   away from 0) — which exploration guarantees for explored cells.
3. **Ignorability assumption** (S ⊥ U | X) — untestable; usable only as a sensitivity
   analysis, never as the primary claim.

### 3.1 Operational layer — exploration without a programmatic dispatcher

There is no routing daemon to inject ε into: today the "router" is an orchestrator
agent (Pi/Claude) reading the model-routing skill and choosing a tier per subtask.
Exploration therefore lives at the **skill + logging layer**, not in code:

- **Convention:** when a task-type cell's route-advise verdict is a significant
  START-HEAVY (or the orchestrator's judgment defaults heavy) and the task is
  cheap-start *eligible* (read-only / trivially re-runnable — the skill's existing
  eligibility rule), the orchestrator cheap-starts anyway at rate ε ≈ 0.1 and logs
  `--note explore` (skill convention shipped in apex-router-skills). Eligibility is
  the safety bound: exploration never touches mutating/irreversible work, so a failed
  exploration costs a retry, never corrupted state.
- **Measurement:** exploration rows are ordinary route_log rows with note=explore;
  the exploration rate per cell is auditable from the log itself (explore-tagged n /
  total n), and the ε budget is enforced by reading the log, not by trusting the
  convention. route-advise's null_ts/provenance warnings apply unchanged.
- **Estimand impact:** explore-tagged rows give S ⊥ U | X by construction *within
  eligible work* — the identified region is eligible dispatches, and τ̂ for
  never-eligible work (mutating, irreversible) stays CANNOT-DECIDE permanently.
  That restriction is a feature: those are exactly the dispatches where a wrong
  cheap-start is most expensive.

## 4. Offline evaluation — counterfactual held out, or it didn't happen

**Split:** session-level, time-ordered (train = earlier sessions, test = later). No
random row shuffle: turns within a session share a cached prefix and are strongly
correlated; row-level splits leak.

**Policy replay against the logged policy π₀ (the current venue rule + judgment):**
for each held-out session, price π₀ and the learned π₁ with the frozen rate card.
Report three regions separately — collapsing them is how self-confirming evals happen:

- **Agreement region** (π₁ = π₀): realized cost, no model dependence.
- **π₁ cheaper, log ran cheap:** realized savings — the gold region, counterfactually clean.
- **π₁ cheaper, log ran heavy:** *model-dependent* savings — computable only through
  τ̂(x). Report as an upper bound, and report what fraction of claimed savings lives here.
  If the headline number needs this region to clear the gate, the answer is INCONCLUSIVE,
  not a pass.

**Metrics:** out-of-sample AUC per task-type cell; per-cell decisions BH-FDR-corrected
across the family of tested cells; cost-savings as a session-level bootstrap CI whose
lower bound must clear 0 *and* whose model-dependent share is disclosed; promotion only
on an out-of-sample beat over π₀ above the noise floor. **Kill criterion (armed):** if
π₁ cannot beat the context-size venue rule out-of-sample, keep the rule — its corrected
1.88× current-slice counterfactual price ratio already captures a material cheap win, and a negative
result is a valid, publishable outcome.

## 5. Feature and label spec (already instrumented — F1/F4 shipped @ 7fef50b/7cbd2d4)

- **Labels** `route_log.jsonl`: {ts, task_type, model, escalated, context_size?,
  session_id?, note}. `null_ts` provenance is counted and warned in route-advise;
  out-of-band/synthetic rows are quarantined, never deleted.
- **Features** `conformance.jsonl`: {ts, surface, task_type, requested_tier,
  resolved_model, matched, context_size?, session_id?}. Honesty invariant: agent-surface
  intent-only rows (matched=null) never enter a denominator.
- **Join** (Phase 0 pipeline, to be promoted to `route_join` + CLI): task_type equal,
  |Δts| ≤ 300 s, nearest match; session_id becomes the primary key once emitters
  populate it (the >>cue extension does as of 7cbd2d4).
- **Enrichment (third join):** `telemetry.jsonl` supplies cache_read/tokens_in per
  turn via session_id + ts-window — enables the context-size feature at full fidelity
  and the per-turn cost model for §4 replay.

## 6. Blind spots this design cannot see (stated now, guardrail 4)

- τ̂ is only identified where exploration or cheap-starts reach; unexplored heavy-default
  regions stay at CANNOT-DECIDE forever unless ε is raised.
- The dispatcher-judgment label means τ measures "kept by the dispatcher," not
  "correct" — a drift in dispatcher strictness looks like a drift in model capability.
  Era-slicing (now possible with clean ts) is the detection tool.
- Replay can't price *quality regret* on kept cheap output that was mediocre but not
  escalated; the escalation label is a floor on failure, not a ceiling on quality.
- Frontier difficulty scores and arena priors (Phase 2) answer a different question
  ("does this look hard") and must stay provenance-tagged features/priors, never targets.

## 7. Build order (each step gated)

1. F5 data accumulation (organic, exploration policy needs a design + review first).
2. `route_join` promotion + labeled table regeneration (mechanical).
3. Baseline logistic/GBDT router + §4 replay eval (build cheap, statistics xval'd
   cross-family before any gate).
4. Phase 2: cold-start prior/score for n<floor cells, prospective logging only.
5. Phase 3: distill + gated promotion through route_table/gate machinery.
