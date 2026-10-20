# Ballot: 9 proposals, orchestrator ranking

Reports: /tmp/rsch/RA.md (area A, cost and size), /tmp/rsch/RB.md (area B, review loops), /tmp/rsch/RC.md (area C, ledger and labels).

## Orchestrator check of key numbers (re-run independently)

| Claim | Report | Re-run |
|---|---|---|
| Opus workers capped | RA 38 jobs, $711.72 | 36 jobs, $695.29 (rows differ by refresh time; same order) |
| Escalations that are provider errors | RC 120 of 122 | 120 of 122 |
| Labels unknown | RC 802 of 818 | 802 of 818 |
| Judge/reviewer rows without verdict | RB 83 of 179 | 83 of 179 |
| xval feedback rows | RB 2 | 2 of 262 |
| Cost cap is a hard stop | RA (agents.ts) | integrations/pi/lib/agents.ts:485 `cancel(..., "killed(cost)")` |

## Rubric

Score 1-5 each. Weights: impact 0.35, evidence 0.25, effort 0.20 (5 = small), risk 0.20 (5 = low).

## Ranking

| Rank | ID | Proposal | Impact | Evidence | Effort | Risk | Score |
|---|---|---|---|---|---|---|---|
| 1 | C1 | Record failure cause; keep infra failures out of the pass rate | 5 | 5 | 5 | 4 | 4.80 |
| 2 | A1 | Soft cost cap with a salvage report | 5 | 4 | 3 | 3 | 3.95 |
| 3 | A2 | Cell-based max_cost advice at spawn (p75 of done jobs) | 3 | 4 | 5 | 3 | 3.65 |
| 4 | C2 | Join key between routing ledger and labels | 4 | 4 | 3 | 3 | 3.60 |
| 5 | B1 | Code evidence and invariant checks before a review GO is accepted | 4 | 4 | 3 | 3 | 3.60 |
| 6 | B3 | Reconciled usefulness fields for reviews (loop_id, confirmed findings) | 3 | 4 | 3 | 3 | 3.25 |
| 7 | C3 | Recalibrate the weak labeller on gold | 3 | 3 | 4 | 3 | 3.20 |
| 8 | A3 | Long cache retention and background long tools | 2 | 3 | 5 | 2 | 2.85 |
| 9 | B2 | Batch defect discovery loop controller | 3 | 3 | 1 | 3 | 2.60 |

Tie C2 / B1 (3.60): C2 ranks first because C3 depends on it.

## Reasons for the main calls

1. C1 first: smallest change, no new data needed, it alone moves passing cells from 0 to 3. Every later quality number depends on a clean pass rate.
2. A1 second: the largest money pool ($695-712 of spend with no usable report). Effort M because the child must get a "wrap up now" message before the hard stop.
3. A2 above B1: same expected direction, effort S and a measured predictor (stop rate 0.49 vs 0.23).
4. A3 low: net effect $58.87 (3.2 %) on assumed price ratios; provider support not confirmed.
5. B2 last: effort L, and RB itself says the NO-GO rounds found real defect families.
