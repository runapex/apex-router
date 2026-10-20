# Evidence-based proposals: 3 independent researchers, 9 proposals, majority vote (2026-10-20)

## Method

| Step | What |
|---|---|
| Areas | A sub-agent cost and size · B review loop effectiveness · C routing ledger and labels |
| Independence | Separate evidence fields per area (A: cost/state/turns, no verdicts; B: verdicts and review logs, no cost; C: route_log, conformance, labels, observe, no agent_runs). No agent read another's report before the vote. |
| Researchers | RA Opus 5.5 · RB GPT-6.1 · RC Sonnet 5.5 |
| Ranking | Orchestrator rubric: impact 0.35, evidence 0.25, effort 0.20, risk 0.20 (each 1-5). Key numbers re-run independently. |
| Vote | Each researcher votes APPROVE/REJECT on all 9 proposals and on the ranking. Pass = 2 of 3. |

## Final ranking (approved 3/3 in round 2)

| Rank | ID | Proposal | Votes | Score | Effort |
|---|---|---|---|---|---|
| 1 | C1 | Record the failure cause (`infra` / `capability`); keep provider errors out of the pass rate | 3/3 | 4.80 | S |
| 2 | A1 | Soft cost cap: at 85 % of `max_cost`, ask for the final report; hard stop only after a salvage budget | 3/3 | 3.95 | M |
| 3 | B1 | Accept a code review GO only with an independent cross-vendor reviewer with code access, test results and regression checks | 3/3 | 3.60 | M |
| 4 | C2 | Join key between the routing ledger and labels (`session_id`, `agent_id`, tier on label rows) | 3/3 | 3.60 | M |
| 5 | B3 | Reconciled usefulness fields for reviews (`loop_id`, confirmed and rejected findings, post-GO defects) | 3/3 | 3.25 | M |
| 6 | C3 | Recalibrate the weak labeller on gold, with a held-out and a fail-class gate | 3/3 | 3.20 | S |
| 7 | A2 | `max_cost` advice per thinking-level cell, advice only | 2/3 | 3.15 | S |
| — | A3 | Long cache retention and background long tools | 0/3 rejected | 2.85 | S |
| — | B2 | Batch defect discovery loop controller | 1/3 rejected | 2.60 | L |

## Key evidence (re-run by the orchestrator and by voters)

| Fact | Value |
|---|---|
| Escalations that are provider errors | 120 of 122. Without them, the escalation rate drops from 13.3 % to 0.25 %, and 3 cells pass the 0.9 Wilson gate (0 today). |
| Opus workers stopped at the cost cap | 36 of 136 jobs, $695.29 of $1,765.64. 37 of 39 capped results are under 700 bytes (stop notice only). The cap is a hard stop (`integrations/pi/lib/agents.ts:485`). |
| Labels | 802 of 818 `unknown`; label rows have no tier; 4 sessions join labels and route rows. |
| Review verdicts | 83 of 179 judge/reviewer rows have no verdict; 2 feedback rows in 262 xval runs. |

## What the vote changed

Round 1 rejected the ranking (1 approve, 2 reject):

1. VC showed that A2's pooled predictor (stop rate 0.49 vs 0.23) is a confound. Within each thinking level the effect is inconsistent: medium 9/14 vs 7/14, high 5/26 vs 15/60 (reversed). A2 evidence 4 → 2, rank 3 → 7, scope reduced to per-cell advice.
2. VB moved B1 above A2: B1 addresses reproduced false acceptance; A2 is an observational predictor.
3. A3 (0/3) and B2 (1/3) left the list.

Round 2 approved the revised ranking 3/3. Every voter re-ran the A2 cell numbers.

## Cost and turns

| Phase | Agent | Model | Turns | Tool calls | USD | Wall s |
|---|---|---|---|---|---|---|
| research | RA | Opus 5.5 | 16 | 16 | 2.57 | 190 |
| research | RB | GPT-6.1 | 12 | 18 | 1.27 | 331 |
| research | RC | Sonnet 5.5 | 21 | 24 | 0.88 | 222 |
| vote 1 | VA / VB / VC | same | 4 / 3 / 5 | 3 / 5 / 6 | 0.82 / 0.10 / 0.20 | 36 / 46 / 41 |
| vote 2 | VA / VB / VC | same | 4 / 5 / 4 | 3 / 4 / 4 | 0.25 / 0.11 / 0.06 | 19 / 44 / 19 |

Total USD 6.26. The cheapest researcher (Sonnet, $0.88) found the strongest proposal (C1) and the
confound that changed the ranking. Raw rows: `metrics.jsonl`. Reports: `RA.md`, `RB.md`, `RC.md`.
Ballots: `BALLOT.md`, `BALLOT2.md`.
