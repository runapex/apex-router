# Research: a frontier "difficulty teacher" distilled into a small routing model

**Status:** research note with a measured pilot; nothing built. **Date:** 2026-10-07.
Cross-validated by `it-entra-gpt-6-astra` (xval run `ee71133bfe48`): every §2 number it could
recompute from the raw pilot file matched; its design findings are folded in and listed in §9.
**Question:** if a top frontier model, given the right prompt, scores how hard a request is, and we
collect those scores at scale, can we train a small specialist model to route each request or turn
to the right-sized model?
**Related:** `PHASE1-outcome-router-research.md` (estimand, missing-data problem, offline eval),
`DESIGN-shadow-ab-labeler.md` (counterfactual labels), `phase0-outcome-router/REPORT.md` (NO-GO,
no labels), `PLAN-workflow-graph-optimizer.md` (outcome labels, P0), `src/apex_router/labels.py`.

## 1. The idea, stated precisely

1. **Teacher:** a frontier model reads a request (before any work is done) and returns a structured
   score — difficulty, reasoning depth, ambiguity, expected steps, cheapest adequate tier.
2. **Scale:** run the teacher over every logged request (cheap: §5).
3. **Student:** distill the teacher into a small, local model that scores in milliseconds.
4. **Route:** map the student's score (plus context features) to a model tier.

**What is new, and what is not.** Routers trained from frontier-model judgments exist: RouteLLM
(LMSYS 2024) trains small routers on preference labels where GPT-4 judges which of two models won;
Hybrid LLM, FrugalGPT, AutoMix and products (Martian, Not Diamond, OpenRouter's auto-router) ship
variants (from memory; not each verified to use frontier judgments or the same routing unit).
Asking a model to rate a prompt's difficulty is not new either. What *may* be uncommon is doing it on
agentic tasks (100k+ token contexts, cached prefixes, multi-step work) and calibrating the score
against measured outcomes on one's own workload. That is a hypothesis, not a finding: `PHASE1`'s
"nobody routes agentic turns" is itself unsourced, and a 2026 literature search is owed before any
novelty claim.

## 2. Pilot (2026-10-07): what a teacher score is actually worth

**Setup.** 60 real tasks (user turns that ran tools, from pi and Claude Code transcripts), stratified
into quartiles by tool calls. Request text (≤ 1,500 chars) sent to `claude-opus-5-5` **twice** and
to `gpt-6.1-sol` once; JSON-only system prompt; 180 calls, 2 parse failures, 130 s at concurrency 6.
Stored: task ids and scores only (`~/.apex-router/labels/pilot/teacher.jsonl`, 0600). Spearman ρ
on average ranks; 95% percentile bootstrap over **tasks** (2,000 resamples). Tasks from the same
session are not independent, so these intervals are optimistic; a session-cluster bootstrap is owed.
n = 59 (one task lost both opus parses) unless stated; gpt-only rows use n = 60 (on the common 59,
gpt vs effort is ρ 0.45).

| Check | Result | Reading |
|---|---|---|
| Test–retest (opus vs opus): difficulty | ρ **0.98** [0.96, 0.99]; exact 86%, ±1 100% | the teacher is stable |
| Test–retest: cheapest tier | agreement 88%, κ **0.80** | stable |
| Cross-model (opus vs gpt): difficulty | ρ **0.93** [0.88, 0.95]; exact 44%, ±1 90% | two vendors rank alike, scale differently |
| Cross-model: reasoning depth / ambiguity | ρ 0.89 / **0.60** | ambiguity is the noisy dimension |
| Cross-model: cheapest tier | agreement 68%, κ **0.45** | the tier call is much less agreed than the rank |
| Teacher difficulty vs observed effort, log(tool calls) | ρ **0.47** [0.23, 0.68] (gpt 0.43) | real but moderate signal |
| Baselines vs the same effort | request length ρ 0.28 [0.03, 0.50]; embedding kNN ρ 0.29 [0.01, 0.52] | the teacher beats both |
| Teacher vs request length | ρ **0.72** | most of the score is length |
| Teacher's signal beyond length (partial ρ) | **0.40**; beyond length + kNN **0.37** | there is a non-length part |
| Expected-steps bucket = observed bucket | opus 59%, gpt 43% | rough |
| Student: ridge on nomic embeddings → teacher mean score (LOO over tasks, α = 1) | ρ 0.71, MAE 1.33 | no better than length … |
| Student: request length alone → teacher mean score (LOO) | ρ 0.71, MAE 1.20 (predict-the-training-mean, LOO: 1.69) | … so nothing shows the student learns more than length |
| Cheapest tier the teacher would pick | opus 17/59 (29%), sonnet 31 (53%), haiku 11 (19%) | the teacher would downshift most work |

**Effort is a proxy, not the target.** "Tool calls in the task" mixes difficulty with chore size,
the executing model (always the heavy tier here), failures and workflow. It is not P(cheap tier
fails). It is used only because it is the one outcome-shaped signal with variance: of the sampled
tasks, 33 are judged *success*, 3 *partial*, 23 *unknown*.

**Method notes.** The student targets and the predict-the-mean baseline use the **mean of the three
ratings** per task (two opus, one gpt), not opus rep 0 alone. Baseline MAE is 1.69 under the same
LOO protocol as the students (1.66 in-sample; reviewer-recomputed). Ridge α was not tuned on held-out data; three values were tried (1, 10, 100) and the
best is shown, which flatters the student. kNN used the other 700+ tasks as neighbours (pilot tasks
excluded). No uncertainty is reported for the partial correlations or for the *differences* between
teacher and baselines.

## 3. Where the concept is now stronger

1. **The teacher is repeatable.** Test–retest ρ 0.98 under one prompt and two vendors at ρ 0.93: the
   score is not sampling noise. Repeatability is necessary for distillation, not evidence that the
   score measures "difficulty" — two vendors can share the same length bias.
2. **It may carry signal beyond the cheap features.** Its point correlation with effort is higher than
   length or embedding neighbours (0.47 vs ~0.29) and stays ≈ 0.4 after controlling for both, but the
   intervals overlap and no paired test of the difference was run. Directional, not established.
3. **It is cheap.** Pilot calls through the proxy: prompt p50 240 tokens (opus) / 157 (gpt), output
   p50 49 / 75. At the stale July list prices that is ≈ $0.007 per opus call — ≈ $70 to score
   10,000 requests once, ≈ $210 for a three-rater ensemble (2× opus + 1× gpt; the gpt price is a placeholder). Latency is irrelevant offline.
4. **Parts of the pipeline exist; others are designs.** Built: outcome labels (`labels.py`: 818
   tasks, rules + local judge + gold workflow), classifier + routing table (`classify.py`,
   `route_resolve`), escalation logging (`route_log`), the significance gate (`route_advise`: Wilson
   CI, n ≥ 30, break-even *escalation* rate 1 − 1/cost_ratio = 0.80 at ratio 5), the widget's Quality
   section. **Designs only:** exploration with propensities (`PHASE1` §3) and the shadow A/B labeler.

## 4. Gaps — and why the teacher score cannot be the routing target on its own

| Gap | Evidence | Consequence |
|---|---|---|
| **The score answers "does it look hard", not "will the cheap tier fail"** | ρ 0.47 with effort; length explains most of the score (ρ 0.72); tier κ 0.45 across vendors | routing on it directly would mostly route on length; `PHASE1` §6 already rules it a feature/prior, never a target |
| **No cheap-tier outcomes to calibrate against** | 7 d of proxy traffic: opus 3,493 requests, sonnet 39, haiku 9 | P(success \| cheap tier) is unobserved where it matters (positivity fails); the teacher's "53% sonnet" is untestable today |
| **Outcome labels barely exist** | labels: 16 emitted of 818, gold 0; old labeled table 66 rows, 61 "easy" | nothing to measure the teacher's precision against |
| **The unit of decision is unclear** | Claude-side traffic is 95% cache reads | switching model mid-session forfeits the cached prefix; routing must happen at task / session / subagent start, not per turn |
| **The teacher sees only the request** | it cannot see repo state, prior turns, tool results | same blind spot as the JEPA probe (request embeddings barely predicted the action mix) |
| **Escalation is not yet a capability signal** | 24 h: 4/4 escalations caused by provider errors | labels must separate infra failure from capability failure (P0 of the workflow plan) |
| **Selection bias** | dispatchers send hard-looking work to heavy tiers | naive per-cell rates flatter cheap tiers; needs exploration or shadow replay (`PHASE1` §3) |
| **Teachers disagree on scale** | exact difficulty match 44% across vendors | use ranks / per-teacher calibration, or an ensemble mean, not raw points |

## 5. What fills the gaps

1. **Two-stage target (keeps the guardrail and scales the idea).**
   - Stage A — *distill the teacher*: student ŝ(x) ≈ teacher score. No outcome labels needed; this is
     where scale comes from (§3.3). Target: the mean of ≥ 2 teacher ratings (cross-vendor ensemble),
     rank-normalized per teacher.
   - Stage B — *calibrate to outcomes*: P(success | tier, ŝ(x), context features) fitted on outcome
     labels only; the teacher score is a feature, never the label. Decision rule, kept separate:
     **economic** — cheap-first is cheaper iff P(escalate) < 1 − C_cheap/C_heavy (0.80 at ratio 5,
     i.e. P(not escalated) > 0.20; `PHASE1` §2 states the bar as 0.80 on the kept rate, which
     conflates it with a quality bar); **quality** — a separate minimum P(acceptable outcome) (e.g.
     0.8), where "acceptable" is a different event from "not escalated": a kept-but-mediocre cheap
     answer is not escalated yet may be unacceptable, and the escalation model does not price it.
     Stage B therefore fits two probabilities. The route is the cheapest tier passing both.
2. **Cheap-tier counterfactuals — with a unit mismatch to solve.** The shadow A/B labeler (spec'd,
   not built) grades a cheap model *continuing one turn* of a frontier-generated history ("same tool
   call", "grounded citations"). Routing happens at task / subagent start (§5.6). A cheap
   continuation of a frontier trajectory does not show a cheap model would complete the task from
   the start; matching tool calls measures imitation (a different call can also be right); grounded
   citations show the locations exist, not that the answer is right. Usable as a *screening* signal;
   task-level labels need whole-task cheap runs (exploration, or offline re-runs of re-runnable tasks).
   Replay seeded where the teacher says "cheap is enough" gives selective coverage — record the
   selection so it can be weighted, and keep a random replay share.
3. **Exploration on eligible work.** ε ≈ 0.1 cheap-starts on read-only / re-runnable dispatches with
   logged propensity (`PHASE1` §3.1). This identifies Stage B **only within eligible, explored work**;
   mutating or irreversible work stays CANNOT-DECIDE, as `PHASE1` states.
4. **Gold outcomes.** 100+ hand labels via `apex-router labels review`, infra vs capability failures
   split. These label what *happened* (almost always on the heavy tier): they make the outcome voters'
   precision measurable, but say nothing about cheap-tier outcomes or the teacher's tier call.
5. **A richer teacher input.** Score the task with its first turn's context summary (repo, files
   named, size of the change) — not just the request — and log which input the score saw.
6. **Decision points that do not bust the cache.** Session start (main model), subagent dispatch,
   pi `>>auto`, xval reviews. Not mid-session turns.

## 6. Gates (pre-registered)

Common protocol for all gates: train on earlier sessions, test on later ones (split by session,
never by task row); session-cluster bootstrap (≥ 2,000 resamples) for every interval; all tuning
inside the training split; the test split is scored once.
- **G-A (student):** Spearman(student, teacher-ensemble mean) ≥ 0.8 on the test split, **and** the
  paired difference Spearman(student) − Spearman(length-only) has a 95% interval above 0.
  *Today (n = 59, task-level LOO, untuned split):* ρ 0.71, equal to length → not met.
- **G-B (calibration):** at ≥ 300 task-level outcome labels, with **≥ 30 cheap-tier outcomes and ≥ 10
  cheap-tier failures in the test split itself** (else INCONCLUSIVE, not fail). Population: eligible
  (read-only / re-runnable) tasks only, explored or replayed whole-task. Pass if adding ŝ lowers
  test-split Brier score *and* lowers expected decision cost, both with paired session-bootstrap
  intervals excluding 0. Cost estimator: per task, C_cheap + P(escalate)·C_heavy vs C_heavy, using
  that task's measured token mix priced with the current rate table, plus the cache prefix forfeited
  at the decision point (cachesim), as `DESIGN-shadow-ab-labeler` guardrail 5 requires.
- **G-C (policy):** offline replay per `PHASE1` §4 — savings lower bound > 0 with the
  model-dependent share disclosed; no gate on the "π₁ cheaper, log ran heavy" region alone.
- **Kill:** if G-B fails (not INCONCLUSIVE) at 300 labels, drop the teacher as a router input. It
  may still be *logged* prospectively for empty cells, never used to route there (`PHASE1` §7.4).

## 7. Next steps, in order

1. Gold labels (`labels review -n 100`) — makes outcome-voter precision measurable (not cheap-tier
   outcomes; those need step 3 and exploration).
2. Score all 818 tasks with a two-vendor teacher ensemble (≈ $12 at list prices), store ids + scores only.
3. Shadow A/B labeler MVP, replaying teacher-says-cheap tasks first.
4. Stage A student (embedding + length + simple features), check G-A.
5. Stage B calibration once labels allow; then G-C replay.

## 8. Caveats on the pilot

n = 59–60 from one user's sessions over ~6 weeks; task-level intervals ignore session clustering;
effort is a proxy; outcome-labeled variance is tiny (33 success / 3 partial / 23 unknown); costs
use median tokens (not means) and the July list table, with a placeholder gpt-6 price; the prompt is
one wording (rating prompts are sensitive to phrasing and to anchoring the scale with examples — not
yet tested); the length / embedding / partial-correlation / ridge numbers were computed in ad-hoc
scripts not stored with the pilot data, so they are not independently reproducible yet.

## 9. Cross-validation record (astra: pass 1 `ee71133bfe48` NO-GO → pass 2 `041cf9c78c19` GO-WITH-CHANGES → fixed)

| Finding | Triage | Change |
|---|---|---|
| Break-even compared to P(success) instead of P(escalation) (also in `PHASE1` §2) | confirmed against `route_advise.py` | §5.1 separates the economic and quality thresholds; `PHASE1` flagged, not edited |
| Turn-level shadow grades do not identify task-level routing outcomes | confirmed | §5.2 rewritten as a screening signal with the mismatch stated |
| "Identified without untestable assumptions" too broad; gold labels do not reveal cheap-tier outcomes | confirmed | §5.3, §5.4, §7.1 scoped |
| Repeatability promoted to validity; no paired test vs baselines; row bootstrap ignores sessions | confirmed | §2 method, §3.1–2 softened, §6 protocol |
| Student result overstated; mean-predictor MAE target unstated | confirmed (target was the 3-rating mean) | §2 rows + method notes |
| Gates not fully specified; kill rule could route on the prior | confirmed | §6 rewritten; pass 2: G-B population, test-split support and cost estimator added |
| Pass 2 (xval `041cf9c78c19`, GO-WITH-CHANGES): "P(success) > 0.20" conflated not-escalated with acceptable; baseline MAE protocol mismatch | confirmed | §5.1 two events; §2 MAE 1.69 under LOO |
| Novelty / prior art unsourced | confirmed | §1 hedged |
| "Pipeline already exists" includes designs | confirmed | §3.4 split built vs designed |
| All recomputable §2 numbers and the cost arithmetic | verified by the reviewer | none |
