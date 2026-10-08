# Outcome-label judge on held-out gold (2026-10-08): accuracy holds at 0.95, fail recall does not

Loop-2 iteration 2a. Iteration 1 adopted `ornith:35b · export · v2` at min confidence 0.9 (see
`2026-10-08-label-judge-bench.md`). One gold set did three jobs there: it picked the variant,
set the threshold and supplied the v2 rules, so the 0.90 was in-sample. This iteration labels
new gold and uses it only for measurement. **Defaults are unchanged.** The numbers below go to
the coordinator for a ruling.

## Holdout

The holdout is 82 tasks tagged `holdout-2026-10-08`. None had gold before, and nothing was tuned
on them. They came in two batches.

**Batch A (60 tasks)**, drawn by
`labels export-review --strata suspected-fail:25,test-split:12,random:23` (seed 7). Strata are
drawn in order, never from tasks with gold, and never twice:

- **suspected-fail (25 tasks, from a pool of 146).** The task has a rule fail signal
  (tail_errors, interrupted, next_negative, repeated, or tests failed after the last edit), or a
  judge vote of fail/partial at any confidence, or a judge confidence below 0.9.
- **test-split (12 tasks).** The task is in the P6 test split
  (`worldmodel/tasks.jsonl`, `split == "test"`). The join is `session:index`, the same key
  `worldmodel.steps` uses for `lid = _tid(path, i)`. The 532/532 tasks join. Unlabelled tasks
  are drawn first.
- **random (23 tasks).** Drawn uniformly from the rest.

**Batch B (22 tasks)** was added at the coordinator's ruling: at least 10 scorable test-split
tasks, with at least 3 bad ones if that many exist. It is every test-split task still without
gold, ordered unlabelled first, then rule-fail, then tool errors. Batch B takes every remaining
test task, so the choice of tasks had no freedom.

**Labelling.** Opus (`model:claude-opus-5-5`) labelled both batches with the same rules as the
first 100: strict; the next message is the strongest signal; a turn cut mid-work is unknown; an
API or connection error is fail. The context was the export-review context.

**Difference from iteration 1: the judge's verdict was hidden while labelling.** The labeller
saw the rule votes and `signals()`, but not the `judge` field. The gold is still Opus reading
the same context the judge reads. It measures agreement with Opus, not with the owner.

| | success | partial | fail | unknown |
|---|---|---|---|---|
| batch A (60) | 37 | 7 | 3 | 13 |
| batch B (22) | 20 | 1 | 0 | 1 |
| **holdout (82)** | **57** | **8** | **3** | **14** |
| by stratum: suspected-fail (25) | 11 | 4 | 1 | 9 |
| by stratum: test-split A (12) | 8 | 1 | 0 | 3 |
| by stratum: random (23) | 18 | 2 | 2 | 1 |

## Out-of-sample result

Command: `apex-router labels judge-bench --models ornith:35b,gpt-oss:20b --contexts export
--prompts v2 --gold-tag holdout-2026-10-08`. Results are in
`~/.apex-router/labels/judge_bench/results-holdout-2026-10-08.jsonl` and `table-…md`. The cache
is shared with the in-sample run.

| (82 holdout, votes at ≥ 0.9) | acc_vote (k/n) | 95% CI | always-success on the same voted tasks | acc4 | abstain | unknown | s/task |
|---|---|---|---|---|---|---|---|
| **ornith:35b · export · v2 (adopted)** | **0.95 (38/40)** | [0.83, 0.99] | 0.93 (37/40) [0.80, 0.97] | 0.79 | 0.51 | 0.11 | 1.5 |
| gpt-oss:20b · export · v2 (reference) | 0.77 (37/48) | [0.63, 0.87] | 0.94 (45/48) | 0.57 | 0.34 | 0.26 | 11.8 |
| batch A only, ornith | 0.95 (19/20) | [0.76, 0.99] | 0.90 (18/20) | 0.73 | 0.67 | 0.13 | |

**Comparison with in-sample:**

| threshold | holdout | in-sample |
|---|---|---|
| 0.6 (iteration 1's headline) | 0.91 (59/65) [0.81, 0.96] | 0.90 (64/71) [0.81, 0.95] |
| 0.9 | 0.95 (38/40) | 0.98 (55/56) |

Vote accuracy therefore **replicates out of sample**. The in-sample selection did not inflate it
in any way this data can detect.

**The base rate is the catch.**

- Always-success scores 57/68 = 0.84 [0.73, 0.91] over all decided holdout gold.
- On the 40 tasks where the judge votes, always-success scores 0.93 and the judge 0.95. At 0.9,
  the judge adds one correct label over always-success on the tasks it chooses to vote on.
- What the judge contributes at 0.9 is the choice of WHICH tasks to label. It labels 40 of 68
  decided tasks, and those are the easy ones.

gpt-oss stays below its own always-success bound. It casts 13 fail votes at ≥ 0.9 and none
lands on a gold fail. At any confidence, it calls 7 gold successes "fail" and finds 0 of 3 gold
fails.

### Per class (ornith, 82 tasks)

"Raw" counts every answer at any confidence. "Vote" counts answers at confidence ≥ 0.9.

| class | gold n | recall raw | precision raw (n) | recall vote | precision vote (n) |
|---|---|---|---|---|---|
| success | 57 | 0.93 | 0.90 (59) | 0.65 | 0.97 (38) |
| partial | 8 | 0.62 | 0.42 (12) | 0.00 | – (0) |
| **fail** | **3** | **0.33** | 0.50 (2) | **0.33 (1/3) [0.06, 0.79]** | 0.50 (2) |
| unknown | 14 | 0.43 | 0.67 (9) | – | – |

**Fail recall is 1/3.** The judge gets the API-connection-error turn right (fail, 0.9). It
calls the other two gold fails "partial" at 0.75:

- a requested check that was never performed;
- an answer to a different algorithm than the one asked about.

Fail and partial together make 11 bad tasks. The judge flags 9 of them as partial or fail at
some confidence (raw recall 0.82). At ≥ 0.9 it votes on only 2 of them (0.18 [0.05, 0.48]).
**It does see the bad tasks, but below the threshold:** 5 of the 8 gold partials are "partial"
at 0.85, and the 2 missed fails are "partial" at 0.75.

### Accuracy by confidence bin (decided gold, decided judge)

| bin | holdout | in-sample (iteration 1) |
|---|---|---|
| [0.7, 0.8) | 1/4 | 1/1 |
| [0.8, 0.9) | **20/21 = 0.95** | **8/14 = 0.57** |
| [0.9, 1.0] | 38/40 = 0.95 | 55/56 = 0.98 |

The [0.8, 0.9) bin was the whole reason for the 0.9 threshold, and it **does not replicate**:
it scores 0.57 in-sample and 0.95 on holdout, or 28/35 = 0.80 pooled. On the holdout alone, the
bench's calibration rule would pick 0.8. Two caveats keep this from being an adoption case:

- n is small;
- the in-sample bin was the one the v2 rules were written against.

It is reported here, not adopted.

### Fail/partial-only threshold sweep (success stays at 0.9; measured, not adopted)

Precision denominators include votes cast on gold-unknown tasks, because those votes would emit
a label.

| t | fail recall | fail precision (votes) | partial precision (votes) | bad recall | bad precision (votes) | acc_vote (n) |
|---|---|---|---|---|---|---|
| 0.9 (today) | 1/3 | 0.50 (2) | – (0) | 0.18 | 1.00 (2) | 0.95 (40) |
| 0.8 | 1/3 | 0.50 (2) | 0.71 (7) | 0.64 | 0.78 (9) | 0.93 (46) |
| 0.7 | 1/3 | 0.50 (2) | 0.42 (12) | 0.82 | 0.64 (14) | 0.88 (49) |
| 0.6 | 1/3 | 0.50 (2) | 0.42 (12) | 0.82 | 0.64 (14) | 0.88 (49) |

**A lower threshold does not raise fail recall.** The missed fails are "partial" calls, not
low-confidence "fail" calls. At t = 0.7 they become partial votes. Lowering the threshold raises
bad (partial or fail) recall instead:

- **0.8:** bad recall 0.18 → 0.64 at precision 0.78;
- **0.7:** bad recall 0.82, but precision drops to 0.64. It picks up partial votes on 2 gold
  successes and 3 gold unknowns.

If the coordinator wants more bad labels, the measured candidate is 0.8 for partial/fail votes.
Treating partial and fail as one "bad" class would also help, because the judge confuses them.
Neither change is adopted.

## Weak labels before this gold existed

The posterior is recomputed with only the 100 untagged gold labels, which is the state before
the import. On the 82 holdout tasks it emitted 39 weak labels: 38 success and 1 fail.

- **Correct:** 36 of the 37 that landed on decided gold, i.e. **precision 0.97 [0.86, 1.00]**.
- **Wrong:** one success on a gold partial.
- **Undecided gold:** 2 labels landed on gold unknown.
- **Bad recall of the weak labels:** **1 of 11 [0.02, 0.38]**. Seven gold partials and two gold
  fails were left "unknown".

The stored judge votes from `build --rejudge` agree with gold on 36/38 holdout votes, close to
the bench's 38/40.

## Is the success skew real or a judge artefact?

**Both, in different places.**

**The population is mostly success, and that part is real:**

- **Random stratum** (the closest thing to an unbiased draw from the no-gold pool): 18 of 22
  decided tasks are success, 0.82 [0.61, 0.93]. Bad tasks are 4/22 = 0.18.
- **Suspected-fail stratum:** even here, 11 of 16 decided tasks are success; bad is 5/16 = 0.31
  [0.14, 0.56]. The rule-and-judge doubt signals are weak predictors of failure.
- **Independent signal check.** On all decided gold:
  - a task with any tool error is bad 0.41 [0.28, 0.55] of the time (20/49);
  - a task with none is bad 0.09 [0.05, 0.16] of the time (9/104).

  30% of all tasks have a tool error, so the expected bad rate is about 0.30·0.41 + 0.70·0.09
  ≈ 0.19, which agrees with the random stratum. Gold is not a random sample, so this is a
  consistency check, not an estimate.

**The weak labels exaggerate the skew, and that part is an artefact:**

- Weak-emitted labels are 0.96 / 0.00 / 0.04 (s/p/f, n 234). The decided gold is
  0.81 / 0.09 / 0.10 (n 153), and that gold is itself enriched for failure by the
  suspected-fail stratum and the disagreement sampler.
- The cause is abstention, not mislabelling. Bad tasks get the judge's "partial" at 0.75–0.85,
  fall below 0.9 and become `unknown`. They are not labelled success. Of the 11 bad holdout
  tasks, the judge says success on one.
- `labels report` now prints this line:

  `class prior s/p/f: weak-emitted 0.96 / 0.00 / 0.04 (n 234) · gold 0.81 / 0.09 / 0.10 (n 153)
  · independent signals: rule-fail vote 0.04 of tasks, n_errors>0 0.30`

**Effect on P6.** A model trained on weak labels sees almost no bad tasks. That follows from
the threshold, not from what the data contains.

## P6 test split

| | before | after |
|---|---|---|
| tasks with gold | 10/48 | **48/48** (38 holdout-tagged) |

The rebuilt manifest has 49 test tasks. The 49th is this loop's own coordinator session,
created during the run, and has no gold.

Test-split gold: success 38, partial 2, fail 1, unknown 7. That is 41 scorable tasks, of which
**3 are bad (0.07 [0.03, 0.19])**.

The coordinator's bar was ≥ 10 scorable holdout gold on test with ≥ 3 bad if they exist.

- **Met:** 33 scorable holdout-tagged test tasks.
- **Fewer than 3 bad in the holdout:** the holdout has 2 bad test tasks, and the third is an
  in-sample fail. The test split as a whole contains exactly 3 bad tasks. **That is the
  finding:** any fail-recall or C5-style metric on this test split rests on 3 tasks.

## Correlated voters (coordinator ruling, Fable cross-check)

The export context shows the judge `signals()`, so the judge and the rule voters are not
independent. `posterior()` now combines them through `voter_groups()`:

- A judge whose context is in `SIGNAL_CONTEXTS` (`export`) forms one voter together with every
  rule that votes its label.
- That voter's accuracy is the best smoothed accuracy in the group (max, not product).
- Rules that disagree with the judge still count separately. So do all voters when the judge
  used `clip`, and stored votes from before the context field existed.

Effect, recomputed with the pre-holdout gold:

- 52 tasks had a grouped voter. Their p fell from a mean of 0.992 to 0.950.
- **0 of the 273 weak labels change.** The judge alone has smoothed accuracy 0.95, which already
  clears `EMIT_MIN` 0.80.
- With all 182 gold labels: 45 grouped tasks, and 0 of 234 change.

The fix matters as soon as the judge's measured accuracy drops or an LF-only task is close to
the threshold. Today it moves no label.

## After rebuild

`labels build --judge 0`:

```
tasks 533 · labeled 387 (73%; weak-emitted 234, from gold 153) · gold 182 / target 100
gold by tag: holdout-2026-10-08 82, untagged 100
labels: success 348, partial 14, fail 25, unknown 146
class prior s/p/f: weak-emitted 0.96 / 0.00 / 0.04 (n 234) · gold 0.81 / 0.09 / 0.10 (n 153) · independent signals: rule-fail vote 0.04 of tasks, n_errors>0 0.30
judge 329 votes (61.7%), gold n 94, accuracy 0.97 [0.91, 0.99]
emitted-label precision on gold (leave-one-out): 93/95 = 0.98 [0.93, 0.99]; success 88/89, fail 5/6
```

`worldmodel build`:

- `outcomes: gold 182 · weak 234 · none 117`
- `splits: train 52s/438t · val 7s/46t · test 17s/49t`
- The split is frozen and unchanged.

## What this does not establish

- **Gold is still Opus, not the owner.** It was made from the context the judge reads, now blind
  to the judge's verdict.
- **The fail counts are tiny.** n = 3 gold fails on holdout, and 3 bad tasks on the whole test
  split. The fail-recall CI runs from 0.06 to 0.79.
- **Holdout accuracy is pooled over strata of different difficulty.** Per stratum at 0.9:
  - suspected-fail 4/4 (16 decided);
  - test-split A 3/3;
  - test-split B 19/20;
  - random 12/13.
- **Batch B was drawn after batch A was scored.** It is all remaining test-split tasks, so the
  draw had no freedom, but the decision to draw it was made after seeing batch A.
