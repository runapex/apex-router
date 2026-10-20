# UX scorer v2: judge findings as invariant checks (2026-10-20)

v1 (`scripts/ux_score.py`, format only) stopped at 98.9 while judges still found MUST-FIX defects.
v2 adds `plugin/tests/ux_invariants.test.tsx`: 12 fixed edge cases plus 400 seeded fuzz cases
(seed 20261020). The test file holds an oracle written from the spec, not from `band.tsx`.

## Checks

| Check | Rule | Critical |
|---|---|---|
| `action_whole` | The first required action is in the band, whole | yes |
| `band_width` | Band ≤ 74 terminal columns (wide characters count 2) | yes |
| `no_contradiction` | No "at most N subagents" advice while the band says pause | yes |
| `pane_width` | Every pane row ≤ 72 terminal columns | |
| `status_action` | The status entry (no-band surfaces) carries the action | yes |
| `now_actions` | The pane Now section lists every required action | yes |
| `green_not_all_clear` | GREEN with an action does not read `apex GREEN ·` | |
| `cause_matches` | RED names the quota only at ≥ 80 %, else `pressure RED` | |
| `calm_no_action` | No action words when the user need not act | |
| `no_hidden_fact` | Each figure is in the band or the pane Now section, as a whole cell with its label (`context 61%`, `5h 62%`, `$4.12` not `$4.12M`); wrapped rows are joined first | |
| `no_junk` | No `undefined`, `NaN`, `null`, `unknownx`, `>1e`, and no exponent notation | |
| `renders` | Rendering does not throw | yes |

Composite = 0.4 × v1 + 60 × mean check pass rate. A failed critical check caps it at 60. If the
fixture prints fewer than 412 cases (`UXINV_META`), the scorer exits 2 with composite 0.

## Validation: v2 re-scores the history

(Run with 10 edge cases; the 11th, `tiny-budget`, came from the last review.)

The new checks and the scenario fixture were copied into each old commit. Each NO-GO commit is
capped by the defect that the judges found by hand.

| Commit | Judges | v1 | v2 | Critical failures |
|---|---|---|---|---|
| `8f6fa01` baseline | — | 62.9 | 47.3 | action, width, contradiction, Now, status |
| `b05b6bd` round 1 | GO-WITH-CHANGES | 97.8 | 60.0 | action, width, contradiction, Now, status |
| `5856164` round 2 | — | 97.8 | 60.0 | width, contradiction, Now |
| `15ffeb9` round 2 | 1 GO / 2 NO-GO | 98.9 | 60.0 | width, contradiction, Now |
| `1ca2ab1` round 3 | 2 GO / 1 NO-GO | 98.9 | 60.0 | width, Now |
| `fcb7273` | NO-GO (action cut) | 98.9 | 60.0 | action, width, Now |
| `1a56fb1` | NO-GO (columns) | 98.9 | 60.0 | width, Now |
| `ec781c4` | GO | 98.9 | 96.5 | — |
| this change | — | 98.9 | 99.6 | — |

v2 also found two defects that no judge reported on `ec781c4`. Both are fixed:

1. `pane_width` 245/410: `wrapRow` counted UTF-16 units, so wide-character rows were up to 2× too wide.
2. `no_hidden_fact` 321/410: at RED below 80 % quota, the band dropped the quota figure, and the pane did not name it.

The cross-vendor review then found 7 ways to pass a wrong figure. Each is now a case in
`tests/test_ux_score.py`, and every figure uses a closed grammar. It also found one plugin defect:
a budget of `1e-7` printed `$1e-7`, and huge sums printed `$1e+21T` or `$1e+21`. The band now prints
`$0` and caps money at `$999T+`.

## Mutation check: the score cannot be gamed by these

| Mutation | v2 |
|---|---|
| none | 99.6 |
| invariant test file removed | 0.0, exit 2 |
| band never shows context | 94.0 (`no_hidden_fact` 137/410) |
| pane Now lists only the first action | 60.0 (critical `now_actions`) |
| band labels the quota as `budget` | 95.1 (`no_hidden_fact` 117/410) |

## Data collection on this machine

`apex-router agent-runs collect` runs every 30 min (`~/Library/LaunchAgents/com.apex-router.collect.plist`)
and in the daily run. It copies no brief text, answer text, paths or descriptions.

| File (`~/.apex-router/labels/`) | Row | Source |
|---|---|---|
| `agent_runs.jsonl` | one per finished sub-agent job: role, model, thinking, state, turns, tool calls, tokens, cost, wall time, verdict | `pi-jobs/*/meta.json`, `events.jsonl`, `xval_runs.jsonl` |
| `band_alerts.jsonl` | one per context ≥ 80 % alert, with `acted` = context < 50 % within 30 min (proxy) | plugin `observe/*.jsonl` measure rows with `sid` |

Turn rule: pi jobs count `turn_end` events. xval jobs count codex model calls when exactly one
`xval_runs` row matches the job window; else the pi usage count. The `turns_source` field says which.
Read with `apex-router agent-runs report [--since DAYS] [--json]`.
