# RA: Sub-agent cost and size (Area A)

## 1. Measurements

Data: 346 rows in `agent_runs.jsonl`, 353 `meta.json` files. The brief gives 136 Opus workers and 36 cost stops. The data now has 138 and 38, because more jobs ran. [C2]

### 1.1 Opus workers by state [C2]

| State | Jobs | Cost USD |
|---|---|---|
| all | 138 | 1,782.07 |
| done | 86 | – |
| killed(cost) | 38 | 711.72 (39.9 %) |
| all not-done states | 52 | 874.41 |

| Metric | killed(cost) | done |
|---|---|---|
| median turns | 51 | 35.5 |
| median tool calls | 57 | 38 |
| median cost per turn | 0.238 | 0.212 |
| cost / max_cost | 1.00–1.12 | median 0.52 |
| result.md size, bytes | 483–5,221 (37 of 39 < 700) | – |

The cap is a hard stop with no warning (`integrations/pi/lib/agents.ts:485`). The result of a capped job is only a short stop notice. All of its spend gives no usable report. [C3, C8]

### 1.2 By agent and model [C1, C9]

| Agent / model | Jobs | Done rate | Median cost USD |
|---|---|---|---|
| worker / opus, high thinking | 100 | – (20 % capped) | mean 15.59 |
| worker / opus, medium thinking | 38 | – (47 % capped) | mean 5.87 |
| reviewer / sonnet | 28 | 1.000 | 0.35 |
| scout / haiku | 11 | 1.000 | 0.0096 |
| worker / sonnet-5-5, gpt-6.1-sol | 9 | 9/9 done | 0.24–3.28 |

### 1.3 Cache spend inside Opus jobs [C5, C10]

| Item | USD |
|---|---|
| Opus total (events) | 1,853.59 |
| cacheWrite | 551.80 |
| cacheRead | 857.94 |
| cacheWrite on cache-miss turns | 251.58 |

A cache-miss turn rewrites the full context (example: 185,163 tokens for $3.47, so $18.75 per 1M tokens). There were 137 miss turns. All 137 came after a gap of 300–3,600 s. None came after a gap ≤ 300 s. The 5-minute cache TTL causes them. `cacheWrite1h` tokens: 0 of 31,461,225. `PI_CACHE_RETENTION` does not occur in the repo. [C6, C7, C10]

## 2. Predictors of a cost-cap stop (before spawn) [C4, C9]

| Factor | Cap-stop rate |
|---|---|
| max_cost < 13.14 (done p75) | 21/43 = 0.49 |
| max_cost ≥ 13.14 | 17/73 = 0.23 |
| no max_cost | 0/22 (max cost 67.18) |
| medium thinking, max_cost ≤ 8 | 16/27 = 0.59 |
| high thinking, max_cost 8–20 | 7/38 = 0.18 |
| high thinking, max_cost > 20 | 13/47 = 0.28 |
| timeout_s ≤ 3,600 | 24/74 = 0.32 |
| timeout_s > 3,600 | 14/64 = 0.22 |
| task length < 3,000 chars | 23/74 |
| task length 3,000–6,000 chars | 13/62 |

Done-job cost: high thinking median 9.38, p75 15.23. Medium thinking median 4.94, p75 6.22.

Findings:
1. The best predictor is max_cost below the done-p75 of the same agent, model and thinking cell. The rate doubles (0.49 against 0.23).
2. Small caps cause many stops but little money: 16 stops below $8.28 cost $92.43.
3. Large caps (≥ $20) cause fewer stops but most money: 15 stops cost $533.97. No pre-spawn field predicts these. Task length does not (capped median 1,817 chars, done median 2,906).
4. Thinking level and timeout_s have a weak effect after you control for max_cost.

## 3. Proposals (ranked)

### P1 (rank 1): Soft cap with salvage, not a hard stop
- Problem: 38 capped Opus jobs cost $711.72 (39.9 % of Opus worker spend). Their result.md is a stop notice only (483–5,221 bytes). [C2, C8]
- Change: In `lib/agents.ts` tick, at 85 % of max_cost, send the child one "stop work, write the final report now" message. Give it a fixed salvage budget (for example 10 % of max_cost). Use a hard stop only above that budget. If an in-session message is not possible, stop and `--session-id` resume with that one instruction. Record the new state `done(capped)` in `agent_runs.py`.
- Expected effect: at least 30 of 38 capped jobs (≥ 79 %) give a usable report. Extra cost per job: about one turn. Median last turn = $0.33 (cache hit). A resume with a cache miss costs about $1.92 at the 102,361-token median end context. Total extra cost < 38 × $1.92 = $73. [C5]
- Metric: share of capped jobs with result.md > 1,500 bytes and a final assistant message. Falsification: if < 50 % of soft-capped jobs give a report over 20 jobs, reject P1.
- Effort: M (tick logic, state, tests with the fake child).
- Risk: the salvage turn can start a new tool call. Block tools in the salvage turn, or keep the hard stop at 110 %.

### P2 (rank 2): Cell-based max_cost advice and capped waste in the report
- Problem: max_cost below the done-p75 of the cell gives a 0.49 stop rate, against 0.23 above it. Medium thinking with max_cost ≤ $8 stops 59 % of jobs. The report has no max_cost, no capped-cost sum, and no done-p75. [C4, C9]
- Change: Add `capped_cost_sum`, `done_cost_p75`, `done_cost_p90` per (role, model, thinking) to `agent_runs.report()`. In `agent_spawn`, warn when max_cost < done-p75 of the cell, and show the p75. Write the table into the model-routing advice.
- Expected effect: stop rate for jobs below p75 drops from 0.49 to ≤ 0.25. Direct waste target: $92.43 in sub-$8.28 stops.
- Metric: cap-stop rate per cell over the next 40 Opus worker jobs. Falsification: if the rate for warned-then-raised jobs is not lower than 0.40, reject P2.
- Effort: S.
- Risk: higher caps raise spend on jobs that fail anyway. Use p75, not p90, and keep the cap.

### P3 (rank 3): Stop cache misses from long tool waits
- Problem: 137 miss turns cost $251.58 in cacheWrite. All follow a 300–3,600 s gap. 1-hour cache is never used (0 tokens). [C6, C10]
- Change: Set `PI_CACHE_RETENTION=long` in `childEnv` for Opus jobs only when the job has long tool calls (for example timeout_s > 1,800). Also advise child workers to run commands that take > 240 s in the background and poll with short calls.
- Expected effect: a model with 1 h write = 2.0× and 5 min write = 1.25× base input gives $239.00 saved and $180.13 extra write cost. Net: $58.87 (3.2 % of $1,853.59). The background-command advice can save up to $251.58 with no extra write cost. [C10]
- Metric: cacheWrite USD on miss turns per $100 of Opus spend. Baseline: 13.6. Falsification: if it does not fall below 7 over 20 jobs, or total cacheWrite USD per turn rises, reject P3.
- Effort: S (env flag) plus S (advice).
- Risk: the price multipliers are an assumption. Provider support for `long` on foundry is not confirmed. Measure before you keep it.

## 4. Reason for the order
1. P1 targets the largest pool: $711.72, and it gives value from spend that is already made.
2. P2 is cheap and predictive. But it targets a small direct pool ($92.43), and it does not help large-cap stops ($533.97).
3. P3 has the smallest confirmed net effect ($58.87). It rests on an assumed price ratio.

## Appendix: commands (all run in this session)
- C1: python over `agent_runs.jsonl`: group by (agent, model, thinking, max_cost), count states, sum cost, median turns.
- C2: python: Opus workers, state counts, capped cost and share, cost/max_cost list, median turns, tool calls, cost per turn, done cost quantiles, thinking and max_cost buckets.
- C3: `sed -n 470,490p integrations/pi/lib/agents.ts` (hard stop at line 485).
- C4: python: stop rate by max_cost vs done-p75, thinking × max_cost bands, timeout_s bands, capped cost below $8.28 and ≥ $20.
- C5: python over `pi-jobs/*/events.jsonl` message_end usage: 2nd-half cost share, end context (median 102,361 capped, 77,197 done), first/last turn cost (0.156/0.332 capped).
- C6: python: miss turns (prior context > 30k, cacheWrite > 80 % of context) by state, gap before miss (median 605 s).
- C7: `grep -c PI_CACHE_RETENTION -r integrations src docs` (no matches), and the sum of cacheWrite1h tokens = 0.
- C8: python over `pi-jobs/*/meta.json`: states, reasons, last_tool type, result.md size of capped jobs.
- C9: `apex-router agent-runs report --json` (reviewer/sonnet n 28, done 1.0, cost_med 0.355; scout/haiku n 11, done 1.0, cost_med 0.0096).
- C10: python: Opus cacheWrite/cacheRead/miss USD, gap bands, long-retention net estimate.
- Task length: python `len(meta.task)` only. No task text was read into the report.
