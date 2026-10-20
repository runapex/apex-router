# Implementation of the 7 approved proposals (2026-10-20)

## Work packages

| Package | Items | Engineer model | Worktree branch | Cross-family review | Rounds to GO |
|---|---|---|---|---|---|
| E1 ledger | C1, C2 (route side) | gpt-6.1-sol | apexjob/mvh5k2nj | claude-opus-5-5 reviewer | 2 (3 MUST-FIX) |
| E2 labels | C3, C2 (label side) | claude-sonnet-5-5 | apexjob/mvh5k2x8 | gpt-6-astra xval | 2 (2 MUST-FIX) |
| E3 cost | A1, A2 | claude-opus-5-5 | apexjob/mvh5k34w | gpt-6-astra xval | 2 (1 MUST-FIX; 1 idle timeout) |
| E4 review | B1, B3 | claude-sonnet-5-5 | apexjob/mvh5k3c7 | gpt-6-astra xval | 2 (2 MUST-FIX; 1 idle timeout) |

Each package owned a separate file set. The four branches merged with no conflicts.

## Review findings that changed the code

| Package | Finding | Fix |
|---|---|---|
| E1 | One label was claimed by two dispatches in one session. | One-to-one greedy claim; exact tool_use_id first. |
| E1 | A route with no tool_use_id joined a label with another id. | A label with a tool_use_id joins only the same id. |
| E1 | The pi extension read the APEX_BIN shebang and ran `python -m`; it failed with no error. | Keep the CLI call; add `route-log --cause`. |
| E2 | A tiny emitted set passed the gate on point accuracy. | Gate on the Wilson low bound >= 0.85. |
| E2 | Recall "n/a" hid a missed fail row. | Fewer than 2 held-out fails gives "insufficient", never pass. |
| E3 | The collector dropped `capped`, so capped jobs formed an advice cell. | `agent_runs` keeps `capped`; report shows `soft_capped`. |
| E4 | An unknown author vendor gave ACCEPT. | Unknown author vendor counts as no author. |
| E4 | A finding confirmed on the latest revision needed no regression test. | Regression check includes the current revision. |
| Integration | A text test of the pi call sites failed after the `cause` argument was added. | Test updated. |

## Results on real data (read-only)

| Measure | Before | After |
|---|---|---|
| Headline escalation rate | 13.7 % (119/869) | 0.27 % (2/752), infra 121 counted apart |
| Cells above the 0.9 Wilson gate | 0 | 3 (review/opus, debug/opus, debug/gpt-sol-6.1) |
| `labels calibrate` (84 gold rows) | no gate | FAIL: accuracy 26/28, Wilson low 77.4 %; guarded relabel stays off |
| Cost advice worker/opus/high | none | p75 $15.06, p90 $22.60 (n=66) |
| Label match rate in route-join | not measured | 0/966; old rows have no join keys |

## Tests on the merged tree

| Suite | Result |
|---|---|
| Python | 3506 passed, 6 skipped |
| pi (`node --test integrations/pi/tests/*.test.ts`) | 90 passed |
| plugin (`claude plugin test .`) | 267 passed |
| UX scorer | 99.6 |

## Cost

15 jobs, $18.47 (xval jobs report no USD). Per-job rows: `impl-metrics.jsonl`.
Two xval jobs stopped on the 700 s idle limit during a reconnect; a 1000 s or more idle limit worked.
