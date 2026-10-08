# Outcome-label judge bench (2026-10-08): ornith:35b on the export context wins; judge 0.46 → 0.90

Loop-2 iteration 1. The judge was the weakest voter in `labels report`: accuracy 0.46 on gold
(n 54), against tests 0.85 and commit 0.81. Its votes are what can raise label coverage, so its
accuracy capped coverage and, through coverage, P6 C2/C3/C5 and the workflow chains.

Command: `apex-router labels judge-bench [--models a,b] [--contexts clip,export] [--prompts v1,v2]`
(`src/apex_router/label_judge_bench.py`). Every variant judges the same 100 tasks: each id with
gold in force (newest row per id; the 4 orphaned rows are skipped). Gold has success 67, fail 12,
partial 6, unknown 15. Calls run at temperature 0 with seed 0 and are cached per (variant, task,
hash of the rendered prompt) under `~/.apex-router/labels/judge_bench/`. Results hold ids, outcomes,
confidences and counts only, no transcript text.

**Read this first: the gold is model-made.** All 100 gold labels are `by=model:claude-opus-5-5`.
Opus made them by reading the `export-review` context (request 1500 / final 2000 / next 800
characters, the tool summary, `signals()`, and the votes). The `export` context gives the judge
that same view without the votes. So "accuracy on gold" means agreement with an Opus reading of
the same evidence. It does not measure agreement with the owner. A judge that reads the export
context is partly scored on how well it reproduces that reading.

## Variants

- **context** `clip`: the original judge context (request 700 / final 900 / next 400 + tool
  summary). Rendered with v1, it is byte-identical to the old prompt (a test checks this).
  `export`: the export-review clip sizes plus the tool summary and a `SIGNALS:` JSON line of
  `signals()`.
- **prompt** `v1`: the original instructions. `v2` adds seven ordered decision rules:
  - the next message moving on positively means success;
  - a turn interrupted mid-work is unknown, not fail;
  - declining an offered follow-up is not a negative;
  - tests after the last edit and a commit/push count as evidence, and an API error counts as fail;
  - a session that ended is judged from the final message;
  - an answered question is success.
- **model**: `gpt-oss:20b` (the model behind all 371 stored judge votes) on all four
  context × prompt cells. The best cell (`export|v2`) was then run with `ornith:35b` and
  `qwen3.6:27b-coding-nvfp4`.

## Results (votes scored at confidence ≥ 0.6, the threshold in force)

`acc_vote` is accuracy over the judge's votes: gold ∈ {success, partial, fail} and judge outcome ∈
the same set at confidence ≥ 0.6. It is the same quantity `labels report` shows (0.46 today).
`acc4` is exact 4-class agreement over all 100 tasks, with unknown counted as a class.

| variant | acc_vote (k/n) | 95% CI | acc4 | unknown | error | s/task | prompt / eval tok |
|---|---|---|---|---|---|---|---|
| gpt-oss:20b · clip · v1 (today) | 0.46 (28/61) | [0.34, 0.58] | 0.29 | 0.25 | 0.00 | 8.1 | 509 / 375 |
| gpt-oss:20b · clip · v2 | 0.63 (36/57) | [0.50, 0.74] | 0.43 | 0.33 | 0.02 | 13.4 | 836 / 591 |
| gpt-oss:20b · export · v1 | 0.70 (49/70) | [0.58, 0.80] | 0.51 | 0.17 | 0.00 | 7.8 | 752 / 324 |
| gpt-oss:20b · export · v2 | 0.71 (49/69) | [0.59, 0.80] | 0.55 | 0.22 | 0.00 | 15.3 | 1079 / 579 |
| **ornith:35b · export · v2** | **0.90 (64/71)** | **[0.81, 0.95]** | **0.73** | 0.23 | 0.00 | **1.3** | 1131 / 25 |
| qwen3.6:27b-coding-nvfp4 · export · v2 | skipped: the model loads, but ollama answers `structured output is unavailable` to `format: json` | – | – | – | – | – | – |

The baseline cell reproduces the live number: 28/61 = 0.46, against 0.46 (n 54) in
`labels report`. Its verdicts agree with the stored gpt-oss votes on 69/79 tasks. They are not
identical because the old calls had no fixed seed.

Confusion matrices (rows are gold, columns are the judge's raw outcome at any confidence):

| gpt-oss · clip · v1 | success | partial | fail | unknown |
|---|---|---|---|---|
| success | 17 | 14 | 15 | 21 |
| partial | 0 | 2 | 3 | 1 |
| fail | 0 | 1 | 9 | 2 |
| unknown | 0 | 1 | 13 | 1 |

| ornith:35b · export · v2 | success | partial | fail | unknown |
|---|---|---|---|---|
| success | 58 | 2 | 1 | 6 |
| partial | 1 | 2 | 1 | 2 |
| fail | 0 | 2 | 4 | 6 |
| unknown | 3 | 3 | 0 | 9 |

Reading:

- **The old judge was pessimistic, not noisy.** It called 29 of 67 gold successes partial or
  fail, and 13 of 15 gold-unknown tasks fail.
- **Context matters most:** clip → export adds 0.24 for gpt-oss. The v2 rules add 0.17 on clip
  and about 0 on export, and they double gpt-oss latency because gpt-oss reasons longer. Two v2
  runs ran away to more than 7k tokens and returned invalid JSON (2% error).
- **The model is the second large step.** ornith:35b (MoE, about 3B active) does not reason
  aloud: 25 eval tokens and 1.3 s/task against 15 s for gpt-oss. On the same cell it scores
  0.90 against 0.71.
- **Base rate.** A judge that always says "success" would score 67/85 = 0.79 on `acc_vote`.
  ornith beats that bound (CI lower bound 0.81). gpt-oss at 0.70–0.71 does not.
- **Weak spot: fail recall.** ornith calls 4 of 12 gold fails fail and 6 of them unknown. At
  any confidence it called partial 9 times, and only 2 of those are gold partial.

## Winner and threshold

Rule: highest `acc_vote` (ties at 2 dp go to fewer "unknown" answers). The winner needs ≥ 0.70
with a Wilson lower bound > 0.46.

**Winner: `ornith:35b` · `export` · `v2`**, 0.90 [0.81, 0.95]. It is now the default:

- `JUDGE_MODEL = "ornith:35b"` (env `APEX_LABEL_JUDGE`)
- `JUDGE_CONTEXT = "export"` (env `APEX_LABEL_JUDGE_CONTEXT`)
- `JUDGE_PROMPT_VERSION = "v2"` (env `APEX_LABEL_JUDGE_PROMPT`)

The old code default was `qwen3.8:27b-mlx`, which is not installed; the stored votes came from
gpt-oss via the env override.

**Calibration** (winner; gold decided, judge decided, any confidence):

| confidence | [0.7, 0.8) | [0.8, 0.9) | [0.9, 1.0] |
|---|---|---|---|
| correct / n | 1/1 | 8/14 = 0.57 | 55/56 = 0.98 |

No votes fell below 0.7. The literal "lowest bin with accuracy ≥ 0.75" is [0.7, 0.8), but that
bin holds one vote and the bin above it is at 0.57. The bench therefore uses the lowest bin
from which **every** non-empty bin above is ≥ 0.75. **`JUDGE_MIN_CONF` = 0.9** (env
`APEX_LABEL_JUDGE_MIN_CONF`), up from 0.6.

At 0.9 the judge votes on 56 of the 85 decided gold tasks:

- success 51/51;
- fail 4/5;
- partial never voted.

It is a high-precision success/fail detector, and partial stays with the rules and gold.

## `labels build --judge 600 --rejudge`

`--rejudge` drops every judge vote the current config (model, context, prompt) did not make,
then judges up to N tasks, newest first. A rerun resumes and keeps votes the current config
already made. It judged all 532 tasks in 619 s (1.2 s/task). Five new tasks had appeared since
the 527-task report.

| | before (gpt-oss · clip · v1 @ 0.6) | after (ornith · export · v2 @ 0.9) |
|---|---|---|
| tasks | 527 | 532 |
| labeled (coverage) | 104 (20%) | 358 (67%) |
| weak-emitted | 19 | 273 |
| labels s / p / f / unknown | 86 / 6 / 12 / 423 | 329 / 6 / 23 / 174 |
| judge votes (coverage) | 257 (48.8%) | 329 (61.8%) |
| judge accuracy on gold | 0.46 (n 54) [0.34, 0.59] | 0.98 (n 56) [0.91, 1.00] |
| tests / commit accuracy | 0.85 (13) / 0.81 (26) | 0.85 (13) / 0.81 (26) |
| emitted-label precision on gold (LOO) | 10/10 = 1.00 [0.72, 1.00] | 57/58 = 0.98 [0.91, 1.00] |
| … success / fail | 9/9, 1/1 | 53/53 = 1.00 [0.93, 1.00], 4/5 = 0.80 [0.38, 0.96] |

`worldmodel build` manifest: `outcomes: gold 100 · weak 273 · none 159`; before it was
gold 100 · weak 19.

## What this does not establish

- **In-sample.** One gold set did three jobs: it selected the variant, set the threshold, and
  supplied the v2 rules, which were written from the decision patterns in the gold reasons.
  The 0.90 and the post-build 0.98 are optimistic, and no held-out gold exists. The leave-one-out
  precision in `labels report` holds out the label, not the configuration choice.
- **Agreement with Opus, not with the owner.** See the caveat at the top. The winner sees the
  same context the gold labeller saw.
- **Label skew.** The new weak labels are 96% success (262 of 273), following the gold base rate and the
  judge's precision profile. Fail and partial labels stay scarce: 23 fail, 6 partial, all
  partial from gold. The judge does not fix C5's shortage of bad tasks.
- **qwen3.6:27b-coding-nvfp4 was not measured.** Its ollama backend rejects `format: json`. A
  free-form JSON fallback would allow it, but that is a separate change.
- **Untested:** ornith:35b on clip or v1, and gpt-oss at a calibrated threshold. The judging
  budget was 90 min, and about 87 min were used: 74 gpt-oss bench, 2 ornith bench, 11 build.
- `worldmodel evaluate` was not run, so there is no test-split scoring in this iteration.
