# RC: routing ledger and label quality (Area C)

Question: can the ledger and labels support a promoted tier advice?
Answer: not today. Three blockers exist. Three proposals follow.

## 1. Measurements

All numbers come from the commands in the appendix. Window: route_log 2026-10-05 to 2026-10-20.

### 1.1 route_log.jsonl (933 rows)

| Item | Value |
|---|---|
| Rows with `passed` set | 920 (798 true, 122 false) |
| Rows with `passed` null | 13 (all `label_pending`, claude-code dispatch) |
| Task types | debug 555, review 328, explore 24, generate 6, status 5, other 15 |
| debug + review share | 883 of 933 (94.6%) |
| Rows without `session_id` | 627 of 933 (67.2%) |
| Rows without `context_size` | 39 of 933 |
| Rows with `family` | 575 of 933 |

Rows per task type and tier (`passed` true / false = pass / escalated):

| task_type | tier | n | pass | esc | Wilson low (raw) |
|---|---|---|---|---|---|
| debug | opus | 411 | 348 | 63 | 0.809 |
| review | opus | 276 | 233 | 42 | 0.800 |
| debug | sonnet | 69 | 61 | 8 | 0.788 |
| debug | gpt | 44 | 41 | 3 | 0.818 |
| debug | haiku | 31 | 31 | 0 | 0.890 |
| review | gpt | 31 | 30 | 1 | 0.838 |
| review | sonnet | 17 | 14 | 3 | 0.590 |
| explore | opus | 9 | 8 | 1 | 0.565 |
| explore | sonnet | 5 | 5 | 0 | 0.566 |
| status | haiku | 5 | 5 | 0 | 0.566 |

Cheap tiers (haiku + sonnet) hold 140 of 933 rows (15.0%). Opus holds 699 rows (74.9%).

### 1.2 What "escalated" means

| Note | n | passed | escalated |
|---|---|---|---|
| `turn` | 760 | true | false |
| `turn: provider error` | 120 | false | true |
| empty or `auto` | 40 | 38 true, 2 false | 2 true |
| `agent:*` (dispatch) | 13 | null | false |

- 120 of 122 escalations (98.4%) are provider errors. They are infra failures, not capability failures.
- Only 2 rows are quality escalations. Both are `debug` on sonnet. Of 800 non-provider rows, that is 0.25%.
- Raw escalation rate: 122 of 920 (13.3%). Without provider errors: 2 of 800 (0.25%).
- Provider errors rise with context size (opus, error rate):

| Context | n | provider errors | rate |
|---|---|---|---|
| < 50k | 184 | 14 | 7.6% |
| 50k to 150k | 187 | 31 | 16.6% |
| >= 150k | 328 | 62 | 18.9% |

- Median `context_size`: 184,435 on error rows, 78,885 on `turn` rows.

### 1.3 Cells with n >= 30 (the plugin `MIN_N`) and Wilson low >= 0.9 (`TARGET`)

| Count method | Cells that pass |
|---|---|
| Raw `passed` | 0 |
| Provider-error rows removed, Wilson low >= 0.9 | 3: review/opus 233 of 233, debug/opus 348 of 348, debug/gpt 41 of 41 |
| Provider-error rows removed, near miss | 3: debug/sonnet 61 of 63 (low 0.891), debug/haiku 31 of 31 (0.890), review/gpt 30 of 30 (0.886) |

The cheap-tier cells (debug/sonnet, debug/haiku) miss 0.9 by about 0.01.
All-pass needs n = 35. Two failures need n = 69.

### 1.4 conformance.jsonl (976 rows)

| Item | Value |
|---|---|
| matched true / false | 908 / 68 |
| All 68 mismatches | one day, 2026-08-24, surface `resolve` |
| Rows since 2026-10-05 | 908, mismatches 0 |

Conformance is not a blocker. Tier requests map to the right model today.

### 1.5 Labels

| Item | Value |
|---|---|
| `labels.jsonl` rows | 818, all `from: weak` |
| Classes | unknown 802 (98.0%), success 16 (2.0%), fail 0, partial 0 |
| Sessions | 80 (pi 713 rows, claude 105 rows) |
| Task dates | 2026-07-31 to 2026-10-07 |
| Judge on `tasks.jsonl` (781 judged) | success 434, unknown 237, fail 64, partial 46 |
| Judge model stored | qwen3.8:27b-mlx. Code default `JUDGE_MODEL` is ornith:35b. Labels are stale. |
| Route_log sessions also in label sessions | 4 (of 17 route sessions, 80 label sessions) |
| Label rows with tier or model field | 0 (`tasks.jsonl` keys: id, source, session, index, ts, n_calls, n_errors, has_next, votes, judge) |

Why 98% unknown: `labels.py` needs posterior >= `EMIT_MIN` 0.80. With no gold-calibrated voter accuracy, one judge vote gives posterior 0.5. Simulation with `posterior(r, {})` emits 16 labels.

Gold (`labels/gold/consensus.csv`): 100 rows, 84 with an outcome: success 73, partial 8, fail 3. 16 rows are open.

| Weak source vs gold (100 rows) | Result |
|---|---|
| Stored `weak_label` | 3 of 100 match (95% CI 0.010 to 0.085); recall on gold success 3 of 73 |
| Raw judge outcome | 57 of 100 match (95% CI 0.472 to 0.663); judge success precision 54 of 63; caught 3 of 3 gold fails |
| `voter_accuracy` on gold | judge 57 of 63 (90.5%), tests 5 of 9, commit 6 of 8 |
| Simulated relabel with gold accuracies | 528 of 818 emit (64.5%): success 431, fail 58, partial 39 (in-sample) |

Pilot teacher (`labels/pilot/teacher.jsonl`): 60 tasks, 2 teachers (opus, gpt). Both give the same `min_tier` on 39 of 60 (65%).

### 1.6 observe/*.jsonl (951 rows)

| ev | n |
|---|---|
| step | 804 |
| measure | 106 |
| spawn | 13 |
| complete | 13 (`ok` true 13 of 13) |

`ok` is completion only. It has no signal about quality. `QUALITY_LABELS = false` in `plugin/hooks/evidence.ts` states the same.

## 2. What blocks a promoted tier advice

| # | Blocker | Evidence |
|---|---|---|
| B1 | Failure label mixes infra and capability | 120 of 122 escalations are provider errors |
| B2 | No answer-quality label reaches a cell | 16 of 818 labels emitted; plugin `QUALITY_LABELS = false` |
| B3 | Labels and routing rows do not join | 0 tier field on labels; 4 shared sessions; 627 of 933 route rows lack `session_id` |
| B4 | Sample size on cheap tiers | Cheap rows 140 of 933; sonnet/debug 63 clean rows, needs 69 with 2 fails |
| B5 | Task-type skew | debug + review 94.6%; other types have n <= 24 |
| B6 | Gold is small | 84 labelled rows, 3 fail rows; fail class not measurable |

## 3. Proposals (ranked)

### Rank 1: Record the failure cause; keep infra failures out of the pass rate

- **Problem.** 120 of 122 escalations are provider errors (appendix A2). Raw cell Wilson low is 0.809 for debug/opus. No cell with n >= 30 reaches 0.9 (A3). The note text `turn: provider error` is the only cause marker. `read_rates` counts those rows as escalations.
- **Change.** In `route_log.py`, add an optional `cause` field (`infra` or `capability`). Set `cause=infra` when the pi hook sees `stopReason == "error"` (`integrations/pi/apex-route.ts`). Make `read_rates` and the plugin cell counter skip `cause=infra` rows from pass/escalated. Count them in a separate `infra` counter per cell. Back-fill old rows from the note text. `quality.py` already counts `escalation_causes` for display; reuse its rule.
- **Expected effect.** Escalation rate drops from 13.3% (122 of 920) to 0.25% (2 of 800). Cells that pass the n >= 30 and Wilson low >= 0.9 test rise from 0 to 3, with 3 more cells within 0.015 of 0.9. The change also exposes the context effect: opus infra rate 7.6% below 50k against 18.9% at 150k and above.
- **Metric and falsification.** Metric: count of cells that pass the test, and the infra share per context bucket. Falsify: if a later sample shows capability escalations above 5% in debug/opus clean rows (n >= 100 new rows), the clean rate of 100% is a short-window artifact. Also falsify if the back-fill changes more than 120 rows.
- **Effort.** S (one field, one filter, one back-fill, tests).
- **Risk.** Low. A clean pass is still a completion signal, not a quality signal (`QUALITY_LABELS = false`). This change removes noise. It does not prove quality. Keep `QUALITY_LABELS = false` until Rank 3 has data.

### Rank 2: Add a join key between the routing ledger and the labels

- **Problem.** Labels carry no tier. 627 of 933 route rows (67.2%) have no `session_id`. Only 4 sessions appear in both sets (A5). The label window ends 2026-10-07. The route window ends 2026-10-20. No cell can receive a quality label today.
- **Change.** (a) Make every `log_outcome` caller write `session_id` and `agent_id` (the pi hook already has `PI_SESSION_ID`; the claude-code hook has `session_id` in its payload). (b) In `labels.py` `_row`, write `model` and `tier` from the transcript's first assistant turn. (c) Add a `route-join` step that joins on `session_id` plus nearest `ts` and writes `label` to the labeled table. (d) Resolve the 13 `label_pending` rows with the same step.
- **Expected effect.** Joinable route rows rise from 306 of 933 (32.8%, rows with `session_id`) to at least 90% for new rows. Labels per tier cell become countable. Target: sonnet/debug quality-labelled n from 0 to 69 or more.
- **Metric and falsification.** Metric: share of route rows with a label match, per tier. Falsify: after 7 days of new rows, if the join rate is below 90%, the key is wrong. Also falsify if joined sessions show a tier different from `model` in route_log for more than 5% of rows.
- **Effort.** M (two writers, one reader, one join, tests).
- **Risk.** Medium. Time-nearest joins can attach a label to the wrong task. Mitigation: join on `tool_use_id` for dispatch rows and keep the session-plus-ts join only as a fallback.

### Rank 3: Recalibrate the weak labeller on gold and re-judge with the default judge

- **Problem.** 802 of 818 labels (98.0%) are `unknown`. `posterior` gets no gold accuracies, so one judge vote stays below `EMIT_MIN` 0.80 (A4). The stored judge is qwen3.8:27b-mlx. The default is ornith:35b. Gold already exists (84 labelled rows) and gives judge accuracy 57 of 63 (90.5%).
- **Change.** Import `labels/gold/consensus.csv` with `import_gold`, run `relabel` so `voter_accuracy` uses gold, and re-judge with `JUDGE_MODEL`. Add a held-out check: fit voter accuracy on a random 50% of gold, score the other 50%. Gate the label feed on a fail-class check. Fail gold has 3 rows; ask for 20 more fail-suspect rows (`suspected_fail`) in the next gold batch.
- **Expected effect.** Emitted labels rise from 16 (2.0%) to 528 (64.5%, in-sample). Expect a held-out value lower than in-sample. Target held-out accuracy on emitted labels at or above 0.85.
- **Metric and falsification.** Metric: held-out accuracy on emitted labels, and fail-class recall. Falsify: if held-out accuracy on emitted labels is below 0.85, or if fail recall is below 2 of 3 gold fails (judge caught 3 of 3 raw), do not feed labels to cells. Also falsify if the ornith re-judge disagrees with the stored qwen judge on more than 25% of rows.
- **Effort.** S for the relabel; M for the extra gold rows (owner time).
- **Risk.** Medium. In-sample 64.5% is optimistic. Gold has 73 of 84 success rows (87%), so a label of "success" is near the base rate. Accuracy alone can hide a weak fail class. Pilot teachers agree on `min_tier` for only 39 of 60 tasks (65%), so label noise on tier advice stays high.

### Order and reason

| Rank | Reason |
|---|---|
| 1 | Smallest effort. Largest number (98.4% of failures are infra). It needs no new data. It makes B1 and the cell count honest at once. |
| 2 | It is the only fix for B3. Without it, Rank 3 labels cannot reach a tier cell. It costs more and needs new rows to prove itself. |
| 3 | It has the best label coverage gain (2.0% to 64.5%). It depends on Rank 2 for tier use, and its gain is in-sample. |

## 4. Open items

1. `labeled_table.jsonl` is outside the allowed evidence. The 13 claude-code rows are not resolved here.
2. Gold has 3 fail rows. No fail-class precision can be stated.
3. Only 4 sessions join labels and route rows. No tier-level quality number exists.
4. Only 2 quality escalations exist. Capability failure rate on cheap tiers is not measurable yet.
5. Provider-error rows may stay in the cell if the cause is unknown. The note text is the only marker today.

## Appendix: commands

All commands ran in `~/dev/apex-router` or `~/.apex-router`. Python 3, standard library. Inline scripts used `json.loads` on each line. Wilson formula as in `labels/gold/score.py`.

| ID | Command (summary) | Result used |
|---|---|---|
| A1 | `wc -l route_log.jsonl conformance.jsonl labels/labels.jsonl labels/tasks.jsonl observe/*.jsonl` | 933, 976, 818, 818, 951 rows (observe sum of 6 files) |
| A2 | `python3 -` : load `route_log.jsonl`; `Counter` of `(task_type, tier)`, `(note, passed, escalated)`, context buckets, `family`, `session_id` | Sections 1.1, 1.2 |
| A3 | `python3 -` : group `passed` rows by `(task_type, tier)`; Wilson low per cell, raw and with `'provider error' in note` removed | Section 1.3 |
| A4 | `python3 -` : `Counter` over `labels.jsonl` (`label`, `from`, `p`, `voters`) and `tasks.jsonl` (`judge.outcome`, `judge.model`) | Section 1.5 |
| A5 | `python3 -` : set intersection of `route_log` `session_id` and `tasks.jsonl` `session`; keys of `tasks.jsonl` | 4 shared sessions; no tier field |
| A6 | `python3 -` : join `labels/gold/consensus.csv` with `labels/gold/weak_hidden.jsonl` on `task_id`; accuracy, Wilson interval, confusion `Counter` for `weak_label` and `judge.outcome` | Section 1.5 gold table |
| A7 | `cd ~/.apex-router/labels/gold && python3 score.py --sheet consensus.csv --source label` | Failed with `KeyError: 'project'` (consensus.csv has no project column). A6 replaced it. |
| A8 | `cd ~/dev/apex-router && PYTHONPATH=src python3 -` : `from apex_router import labels`; `voter_accuracy(tasks, gold)`; `posterior(r, acc)` for each task, with gold and with `{}` | 528 emit with gold; 16 emit without |
| A9 | `python3 -` : `Counter` over `conformance.jsonl` (`matched`, `surface`, date of `ts`) | Section 1.4 |
| A10 | `python3 -` : `Counter` over `labels/pilot/teacher.jsonl` (`teacher`, `min_tier`); per-id agreement | 39 of 60 agree |
| A11 | `python3 -` : `Counter` over `observe/*.jsonl` (`ev`, `ok`, `model_resolved`) | Section 1.6 |
| A12 | `python3 -` : Wilson low loop for n at all-pass and at 2 fails | n = 35, n = 69 |
| A13 | `grep -n "MIN_N\|TARGET" plugin/hooks/core/routing.ts`; `sed -n 1,30p plugin/hooks/evidence.ts` | `MIN_N=30`, `TARGET=0.9`, `QUALITY_LABELS=false` |
| A14 | `grep -n "EMIT_MIN\|JUDGE_MIN_CONF\|JUDGE_MODEL" src/apex_router/labels.py` | 0.80, 0.8, ornith:35b |
| A15 | `grep -rn "provider error" src tests docs integrations` | Existing docs already note provider errors (`docs/RESEARCH-MAP.md` line 84) and `src/apex_router/quality.py` has `escalation_causes` |
