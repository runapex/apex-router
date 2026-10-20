# datapce band and /apex pane: UX RSI loop results (2026-10-20)

## Result

| Item | Value |
|---|---|
| Baseline composite (`scripts/ux_score.py`) | 62.9 |
| Final composite | 98.9 (fact recall 0.963, action 1.0, band fit 1.0, jargon 0, pane cut 0/157) |
| Stop reason | 3 of 3 judges GO; composite gain < 1.0 in 3 rounds |
| Final commit | `ec781c4` |
| Plugin tests | 262 pass, 0 fail |
| Spend (sub-agents with a price) | USD 29.27; xval (astra) runs show no price |

The one missed fact is the backend conformance figure in the advice-differs band. The band drops it
for room on purpose (spec B13). The pane Now section names it.

## Bands: before and after

| Scenario | Before | After |
|---|---|---|
| fresh | `apex ●GREEN 5h:3%  ctx ▇░░░░░░░ 10%  $0.12` | `apex GREEN · 5h 3% · context 10% · $0.12` |
| advice-differs | `apex ●AMBER 5h:62%  ctx ▇▇▇▇▇░░░ 61%  $4.12/10  routes 7 (local 4 ✓3 ↑1) ▲2  conf 86%` (85 cols) | `apex AMBER · 5h 62% · context 61% · $4.12/$10 · 2 of 7 differ from advice` |
| red-limit | `apex ●RED 5h:95%  ctx ▇▇▇▇▇░░░ 61%  $4.12  routes 3` | `apex RED · 5h limit 95% · pause new subagents · context 61% · $4.12` |
| context-full | `apex ●GREEN 5h:40%  ctx ▇▇▇▇▇▇▇░ 88%  $6.40  routes 1` | `apex pressure GREEN · context 88% full · run /compact · 5h 40% · $6.40` |
| budget-burn | `apex ●AMBER 5h:50%  ctx ▇▇▇▇░░░░ 45%  $9.10/10  routes 2 (local 4 ✓3 ↑1)  conf 86%` | `apex AMBER · $9.10/$10, empty ≈18 min · pause new subagents · context 45%` |

Pane: sections are now Now · Limits and budget · Subagents · Local models (24 h) · Track record ·
Session. Now gives the band words, the figures the band dropped, and "advice only: datapce never
changes the model". Rows fit 72 columns. Jargon (`▲ ↑ μ tok(all) conf routes breakers`) is gone.

## Loop

| Round | Who | What | Outcome |
|---|---|---|---|
| 0 | main | Frozen scorer + 6 fixed scenarios | baseline 62.9 |
| 1 research | R1 opus, R2 sonnet, R3 gpt-6.1 (independent) | 7 competitors each; baseline pick | Codex CLI / VS Code+k9s / Helicone; all 3 converge on `LEVEL · cause · action · figures` |
| 1 build | E1 opus, E2 sonnet, E3 gpt-6.1 (independent worktrees) | implement spec, measured loop | 97.8 / 98.9 / 97.8 |
| 1 judge | J1 opus, J2 sonnet, J3 astra (blind A/B/C) | vote | 3/3 for E1 (not the top scorer), GO-WITH-CHANGES |
| 2 | E1b sonnet + main | 12 merged MUST-FIX | 1 GO, 2 NO-GO (contradicting advice, fixture pace, quota cause, width guard) |
| 3 | E1c gpt-6.1 + main | 8 MUST-FIX + 3 xval follow-ups | 3/3 GO |

## Findings for model routing

| Finding | Evidence (metrics.jsonl) |
|---|---|
| The scorer could not pick the winner. Judges picked E1 (97.8) over E2 (98.9) 3/3. | A frozen proxy plateaus near 98; after that only judges separate variants. |
| Cheap judges are fast and lenient. Sonnet said GO in round 2 and missed 5 MUST-FIX that opus and astra found. | J2: 2 turns, USD 0.09; J1: 5 turns, USD 0.62. |
| The cross-vendor judge (astra) found every code-level bug (width guard, UTF-16 vs columns, 1e15 label). | 4 runs; each later fix was confirmed by it. |
| Opus researchers and engineers hit the cost cap (47 turns each). | R1 USD 6.23, E1 USD 10.20. |
| Sonnet did a fixed fix-list well and cheaply. | E1b 17 turns, USD 0.81, 12 fixes, 0 test fails. |
| Sonnet research was shallow (3 sources grep-only, 2 UNVERIFIED). | R2 6 turns, USD 0.15. |

Routing rule this suggests (hypothesis, n = 1 task): research and open design → opus or gpt-6.1;
fix lists → sonnet; judge panel → at least one cross-vendor judge with code access; never accept a
single cheap GO.

## Files

`R1.md R2.md R3.md` research · `SPEC-round1.md` reconciled spec · `eng-E*.jsonl` per-iteration
engineer logs · `metrics.jsonl` per-agent turns, cost, time, votes · `scripts/ux_score.py` scorer.
