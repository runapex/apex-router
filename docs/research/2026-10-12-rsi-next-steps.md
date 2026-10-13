# RSI next steps (2026-10-12): P6 curve verdict, literature synthesis, outcome-ensemble pre-declaration

Written after the 2026-10-12 readout (retry A/B + refusals now in the digest; writer-parity cause
found; compaction threshold set to 40%; P6 learning curve run). The owner's framing for this note:
apex-router is an optimisation tool for workflows run in a recursive-self-improvement (RSI) loop,
so the target is output quality (decomposition, finding the best solution, reaching significant
rather than average results), not cost alone. Open questions are not blocked on: options are
listed, the cheapest one is implemented, and the comparison is recorded.

Evidence labels: **[measured]** = a number this session produced from the repo or the data home;
**[lit]** = a number from the four literature scans (`2026-10-12-lit-*.md`; spot-check table at
the end of `-lit-routing.md`); **[inference]** = reasoning from those.

## 1. P6 world model: the learning curve says the JEPA is data-bound, not close

`worldmodel curve` (new, val-only; test split stripped before any training; nothing written under
the data home; 14 tests) subsamples train **sessions** at fractions 0.25/0.5/1.0 × seeds 0/1/2 and
fits the same attempt-3 recipe per cell. [measured], val CE nats/step, mean over seeds:

| fraction | train steps | logistic | JEPA | raw-input probe | gap JEPA − logistic |
|---|---|---|---|---|---|
| 0.25 (13 sessions) | 3,289 | 1.770 | 2.034 | 1.871 | 0.264 [0.221, 0.322] |
| 0.5 (26) | 4,497 | 1.771 | 1.996 | 1.818 | 0.225 [0.165, 0.284] |
| 1.0 (52) | 14,477 | 1.687 | 1.819 | 1.695 | 0.132 [0.119, 0.149] |

- The gap's val-session bootstrap CI excludes 0 in all 9 cells. Logistic is the best baseline in
  every cell. The raw-input linear probe sits ≤ JEPA everywhere and equals logistic at full data.
- Slope of the gap against ln(train steps): **−0.089 nats per e-fold** pooled over 9 cells
  (measured range 2,672–14,477 train steps, ~1.7 e-folds); per seed −0.077 / −0.122 / −0.046.
  The pooled line crosses 0 at ≈ 6×10⁴ train steps, 1.4 e-folds past the data (per-seed lines:
  ≈ 4×10⁴ to 3.6×10⁵), and would reach the G1-C1 pass line (5% below logistic ≈ −0.084) at
  ≈ 1.5×10⁵ train steps (≈ 10× today). Extrapolations, not forecasts. [measured + inference]
- Caveats from the run: session sizes vary, so f = 0.5 holds only 31% of the steps (the slope uses
  actual steps); val is 7 sessions, so CIs are coarse; 4 of 6 sub-full JEPA runs had epochs
  outside the collapse bounds (recorded per cell in the JSON); raw-probe is scored on val windows
  (excludes step 0), a slightly different step set.

**Kill-criterion ruling.** Three options were on the table: (A) keep the pre-registered "drop P6
if G1 still fails at 50k main-session steps" (main steps today 7,344 of 25,311 total [measured];
at the last-14-day rate ≈ 1,650 total steps/day that is ≈ early 2027); (B) read "50k" as all
steps (≈ 2026-10-27), which changes the declared unit; (C) rule from the curve now.

Ruling: **A stays as the hard kill, unchanged (no loosening); C is added as an earlier, stricter
check.** The lines predict a full-data gap at the next doubling (≈ 29k train steps) of 0.07
(pooled) to 0.10 (slowest seed). When the train split reaches 29k steps, re-run
`worldmodel curve` (val-only, same recipe, seeds 0–2) and read the mean full-data gap:
**> 0.10** → every line is violated, the curve has flattened, P6 is dropped then, before the 50k
point; **≤ 0.07** → on the pooled line, continue to the 50k decision; between → ambiguous,
continue to 50k as pre-registered. No G1 attempt (test scoring) happens before either point; the
ledger stays at 16 lines. B is rejected: it would re-define the unit of a pre-registered rule
after seeing the data.

Why not drop now: the gap is shrinking monotonically and the raw-input probe reaching logistic
shows the inputs carry the signal; what is missing is data, which arrives for free. Why not
invest more now: three attempts and the curve all say the hand-feature logistic is the model to
build on, and the literature agrees (next section).

## 2. What the literature says about this programme (four scans, 2026-10-12)

Each scan is a model-produced read of abstracts/HTML; ten of the numbers were spot-checked against
the arXiv abstracts (4 confirmed, 2 partial, 4 plausible-in-body, 0 contradicted; one flag: the
Automata paper's headline AUROC is 0.94, the scan quoted a per-dataset 0.799). Treat the rest as
quoted, not verified.

1. **Trajectory outcome prediction.** No 2025–26 paper shows learned latents beating hand or
   n-gram features at hundreds of labelled trajectories; learned trace readers that win use
   10k–250k (SWE-Critix 2610.13356). GBM/logistic on prefix structure features is the working
   method (EarlyEval 2609.02783, 89–97% accuracy, confirmed; Automata 2608.23670, LR ≈ ESN;
   Behavioral Drivers 2604.02547: step of first edit ρ +0.68, early-patch share ρ −0.78).
   Surprise under a cheap next-action model is a useful feature. **This is the P6 result
   re-derived externally.** [lit]
2. **Prediction is not intervention.** A critic with AUROC 0.94 cost 26 points of success when
   used to abort/redirect (2602.03338, confirmed). Any auto-abandon or auto-escalate needs a
   ~50-task pilot measuring harm before it is on by default. [lit]
3. **Routing.** Prompt-only learned routers plateau; kNN with history is within 0.1 pp of the best
   trained router and all sit 10–30 pp below oracle (Routing Plateau 2606.07587, +1.24 pp from
   13× more data confirmed). Partial-trajectory routing (route after 1–2 cheap turns,
   SWE-Router 2607.00053) is the design that moves AUC. Single-sample labels flip the winning
   model on most queries (DARS 2606.06924). [lit]
4. **Fan-out.** Multi-agent gains are mostly extra tokens; at matched budget a single agent
   matches or beats sequential/debate (2604.02460). Gains range +80.8% to −70.0% by task
   structure (2512.08296, confirmed); independent parallel agents amplify errors, a central
   verifier contains them. Diversity beats headcount (2602.03794, 2609.31571). Significant
   results are a max@k / selected@k quantity: SWE-Master 61.4 → 70.8 at k = 8, realised only
   through the selector. Cross-family review catches different bugs but can hurt the stronger
   drafter (2607.21656). [lit]
5. **RSI loops in practice.** Under matched budget harness evolution fails to beat test-time
   scaling on Terminal-Bench 2.1 (2607.12227, confirmed). Held-out gains are a few points
   (RRSI ≤ 4.3, VERSE +3.1); ~1 accepted change/day; what worked: hidden scorer, disjoint
   splits + OOD set, strict held-out accept gate, bounded edits, lineage audit, separate
   reward-hacking probe. SkillOpt's gated single-file skill edits gave +19–25 points on GPT-5.5
   (confirmed), but self-generated skills were net negative in SkillsBench. [lit]

**Synthesis [inference].** The repo already has the shape the literature converged on (gold
labels with cross-family check, pre-declared gates, held-out slices, a ledger that counts test
scorings). What it lacks is (a) an outcome predictor that is actually used, built on the features
that win at this scale; (b) a matched-budget control for every nightly/skill edit; (c) a
quality-per-token readout for W2 fan-out that is token-matched; (d) a selector-based best-of-k
lane on the hard stratum, because "significant results" live in max@k, not in the mean.

## 3. Pre-declared experiments (ordered; each has a kill rule)

| # | experiment | what is measured | pass / kill | effort |
|---|---|---|---|---|
| E-A | **Outcome ensemble** (§4): logistic/GBM on prefix structure + Markov surprise + judge/rule votes, nested 5-fold × 3 on the 222 gold tasks | AUROC/Brier vs class prior, length-only, judge-only, at prefix 0.25/0.5/1.0; paired ΔAUROC CI | adopt as the P6 "value head" if ΔAUROC vs judge-only CI > 0 at prefix 1.0 **and** AUROC ≥ 0.70 at prefix 0.5; otherwise the judge stays the only outcome signal | built this session |
| E-B | **Label-noise re-run**: 60 tasks × 3 repeats per tier | how often the per-task winner flips | if > 30% flip, every tier comparison must use ≥ 3 samples | 1 night, local tiers |
| E-C | **Matched-budget control** for nightly skill edits: frozen ~150-task slice, same tokens for the control arm (best-of-N/retry) | held-out delta of edit vs control | accept an edit only if it beats the control, not the baseline | 2 days |
| E-D | **Token-matched W0 vs W2** (randomised within task type) | success, tokens, retry cost, context fill at dispatch; A_e per task type | keep W2 only on task types with A_e > 1 at matched tokens | 100–150 tasks of traffic |
| E-E | **Diverse-strategy best-of-k on the hard stratum** (k = 4, strategies not samples) with a cross-family selector | oracle pass@k, selected@k, max − mean | lane goes default-on if selected@4 − single ≥ +5 pts on ≥ 40 tasks | after E-A (needs the hard-stratum predictor) |
| E-F | **Intervention pilot** (50 tasks): use E-A's predictor to suggest (not force) escalate/abandon | success and tokens with vs without the suggestion | no auto-action until harm is measured ≤ 0 | after E-A |

## 4. E-A: outcome ensemble — pre-declaration and first run

Pre-declared before the first run (this section was written before results were pasted in):
binary target bad = {fail, partial} vs success, unknown dropped; features computed on the prefix
only; Markov surprise fit on the other folds inside the CV loop; nested stratified 5-fold × 3
repeats, seed 0; task bootstrap 1000 draws; models A prior, B length-only, C judge-only,
D structure, E structure+surprise, F stacked (+judge/rules), G GBM if sklearn is present.
Hypotheses: H1 D beats B (structure > length) ΔAUROC > 0.05; H2 F beats C (adding structure to
the judge helps) ΔAUROC CI > 0; H3 AUROC at prefix 0.5 ≥ 0.8 × AUROC at 1.0 (Automata reports 92%).

**Run** (`PYTHONPATH=src .venv/bin/python -m apex_router.worldmodel.outcome --prefixes
0.25,0.5,1.0,8,16,32`, 3.4 s; card `/tmp/outcome-card-abs.json`; reproduced twice with
identical numbers). Data: 226 gold ids → **n = 147** (bad 39: fail 15, partial 24; success 108);
excluded 40 in the frozen P6 test split (kept untouched), 35 unknown, 4 without a labels task.
scikit-learn is not in the venv, so G (GBM) was skipped and the logistic is a numpy L2 fit.

[measured], AUROC with task-bootstrap 95% CI; "k" = first k steps (the online quantity), "f" =
fraction of the task's own length (leaks total length through `n_steps`, optimistic):

| model | prefix | AUROC [CI] | Brier | Δ vs judge-only | Δ vs length-only |
|---|---|---|---|---|---|
| A class prior | – | 0.465 [0.355, 0.575] | 0.195 | | |
| B length-only | k = 8 | 0.457 [0.351, 0.556] | 0.195 | −0.365 | |
| C judge-only rule | whole task | **0.821 [0.745, 0.889]** | 0.143 | | |
| D structure | k = 8 | **0.682 [0.570, 0.784]** | 0.181 | −0.139 [−0.279, +0.004] | +0.225 [+0.097, +0.352] |
| D structure | k = 16 / 32 | 0.633 / 0.659 | 0.187 / 0.182 | −0.188 / −0.162 (CI < 0) | +0.189 / +0.225 (CI > 0) |
| D structure | f = 1.0 | 0.636 [0.527, 0.740] | 0.187 | −0.185 [−0.315, −0.045] | +0.202 [+0.061, +0.341] |
| E + Markov surprise | k = 8 | 0.681 [0.572, 0.785] | 0.182 | −0.140 | +0.225 |
| F stack (+ judge/rules) | k = 8 | 0.864 [0.790, 0.922] | 0.131 | +0.043 [−0.028, +0.120] | +0.407 |
| F stack | f = 1.0 | 0.866 [0.788, 0.929] | 0.128 | +0.045 [−0.027, +0.124] | +0.432 |

ECE (10 bins) of F at f = 1.0: 0.092. Top F coefficients (standardised, sign agreement over 15
fits): judge_bad +0.94, n_err +0.29, judge_unknown +0.28, act_other −0.28, tail_errors +0.20,
act_edit +0.18, act_build +0.18, act_write −0.17.

**Verdict against the pre-declaration.** H1 holds (structure beats length by +0.20 at full,
+0.23 at 8 steps, CIs exclude 0). H2 **fails**: the stack's +0.045 over the judge has a CI that
includes 0, so by the declared rule the ensemble is **not adopted** as a value head; the
`ornith:35b` judge stays the outcome instrument. H3 is moot on the fraction prefixes (they leak
length); on honest prefixes the 8-step AUROC (0.68) is the best structure number, not the worst.
Surprise features add nothing (E ≤ D everywhere), which is the P6 result again at the outcome
level.

**What is real here [inference].** An 8-step structure-only predictor at AUROC 0.68 is the first
early-warning signal this repo has that does not need the transcript; the judge (0.82) needs the
whole transcript and the task to be over. The two are complementary in time, not competitors.
The gold labellers never saw the judge (the 2026-10-08 blind re-label stripped it), so C's number
is not contaminated by construction. With 39 positives the AUROC SE is ≈ 0.05: a +0.045 lift
cannot be resolved at this n; resolving it needs ≈ 4× the labels (E-B's label expansion), which
is the same gate the P6 curve points at. Next use of E-A: feed D@8 into E-F (suggest-only pilot)
and into the hard-stratum selection for E-E; do not auto-act on it (lit item 2).

## 5. Independent review

Fresh Opus reviewer (read-only, prompted to refute) on the curve diff. Findings and what was done:

| # | finding | triage | change |
|---|---|---|---|
| 1 | `curve_slope` fits a slope from float noise when every cell has the same train size (several seeds at fraction 1) | CONFIRMED (reviewer ran it) | requires ≥ 2 distinct sizes (rounded); test added |
| 2 | zero-gap point reported even when it lies inside/before the measured range (gap already negative), and a real future crossing from below is never reported | CONFIRMED | crossing reported only beyond the largest measured size, with direction and e-folds past the data; tests added |
| 3 | a NaN gap (diverged JEPA) poisons the whole fit silently | CONFIRMED | non-finite gaps dropped and counted; test added |
| 4 | `raw_probe` is fit on ≤ 20,000 sampled train rows and scored on ≤ 20,000 val rows (train.cap), so it stops growing with data past 20k and its val subset differs per seed | CONFIRMED by reading; no effect on this run (14.5k train / 3.5k val) | recorded in the JSON notes |
| 5 | every val CE is on the split that chose the JEPA epoch and the logistic penalty: optimistic, unequally | CONFIRMED | stated in the render and notes; the G1 test split is the unbiased number |
| 6 | test hook `__never__` asserted nothing; percentile-CI containment could flake | CONFIRMED | hook now records scored ids and the test asserts they are val only; containment assertion replaced |
| 7 | one seed drives subset and init; no slope CI | CONFIRMED | per-seed slopes added to JSON and render (−0.077 / −0.122 / −0.046) |
| 8 | a mid-run exception loses every finished cell | SUSPECTED, plausible | finished cells are checkpointed to `<out>.partial.json` after each cell, removed on success |
| 9 | "never loaded into the curve" is loose (rows are loaded, then dropped) | CONFIRMED | wording fixed |
| 10 | test split cannot reach training, scoring or the ledger; subsampling nested and non-mutating; bootstrap pairs steps and resamples val sessions | holds | none |

Pass 2 was not spent: every fix is local and covered by a unit test (18 curve tests pass).
The outcome prototype (§4) was reproduced by re-running it; it has not had an independent
reviewer yet and is labelled a prototype in the code.

## 6. Owner-owed items

- Restart the Claude Code process in `~/src/apex-router` (session chain started 2026-10-03,
  before the datapce plugin install); until then the parity gate cannot open.
- Decide whether E-D's randomisation (W0 vs W2 per task) is acceptable on live work; it costs
  nothing in tokens but changes which workflow runs.
- Retry A/B and refusals: re-check the digest ≈ 2026-10-26.
