# datapce band and /apex pane: reconciled UX spec (round 1)

Sources: three independent research reports (R1, R2, R3 in this folder). Each rule shows its vote.
A rule needs 2 of 3 votes. The score is `scripts/ux_score.py` (frozen). Baseline composite: 62.9.

## Baseline pattern (3/3)

The three reports named different products (R1 Codex CLI, R2 VS Code status bar + k9s hint,
R3 Helicone alert). All three converge on one structure. We use that structure as the baseline:

`apex LEVEL[: action] · figures`  — the level first, then the cause and action when the user must act,
then the figures with unit words.

Codex CLI is the nearest product (a coding-agent status line, plain words, ` · `, fixed warning
points that name a real command, the user decides). Use it as the tie-break reference.

## Band rules

| # | Rule | Votes |
|---|---|---|
| B1 | When the user must act, put cause + action right after the level. Name a real command when one exists. | 3/3 |
| B2 | Show no action text when the user need not act (calm, advice differs). | 3/3 (fixtures) |
| B3 | Fit 80 columns, including the Hide button (about 6 columns): target 74 or less. | 3/3 |
| B4 | Give each number a unit word. No bare glyphs: drop `▲ ↑ ✓ μ conf routes tok(all)`. | 2/3 (R1, R3) |
| B5 | Keep `GREEN/AMBER/RED` as the level word (it is the pressure state, not a new scale). | 2/3 (R1, R3) |
| B6 | Write `context` in full; the 8-cell meter is optional (drop it first when space is short). | 2/3 |
| B7 | Separator ` · ` (Codex) — `|` and double space lost. | 1/3 each; tie-break Codex |
| B8 | Move local-lane counts out of the band into the pane. | 2/3 |
| B9 | Context ≥ 80%: `context 88% full: run /compact`. | 2/3 (R1, R2; R3 said /apex handoff) |
| B10 | Limit RED: `5h limit 95%: pause new subagents`. | 2/3 ("pause") |
| B11 | Budget burn: `$9.10/$10, empty in 18 min: slow down` (or pause new subagents). | 2/3 mention pause/slow |
| B12 | Advice differs: a count with a word (`2 differ from advice`), no action. | 3/3 |
| B13 | Drop order when long: meter, conformance `match`, `5h` below 80%. Never drop the action. | 2/3 |
| B14 | Never change the model. Hints are advice. | 3/3 |

## Pane rules

| # | Rule | Votes |
|---|---|---|
| P1 | First section "Now": one summary row (level, cause, action) — same words as the band. | 3/3 |
| P2 | Plain section titles. Order: Now · Limits and budget · Subagents · Local models (24 h) · Track record · Session. | 3/3 (names differ; this set is the merge) |
| P3 | Each row 72 columns or less. Split dispatch rows; cut descriptions at a word. | 3/3 |
| P4 | Replace jargon: `tok(all) μ` → `avg … tokens`, ` n 25 ` → `25 runs`, `▲` → `advice:`, breakers/heavy spawns/P(any slow)/settling/injection → plain words. | 3/3 |
| P5 | Keep the "completion is not answer quality" note, shorter. | 3/3 |
| P6 | Unknown values show `unknown`, not 0. | 1/3 (R3) — optional |

`paneSections` may read optional `m: Measure | null` and `budgetUsd: number` (the fixture passes them).

## Research cost (round 1)

| Agent | Model | Turns | Cost USD | Time s | Competitors verified | Note |
|---|---|---|---|---|---|---|
| R1 | claude-opus-5-5 | 47 | 6.23 | 241 | 7 (1 UNVERIFIED) | hit cost cap after writing; tested bands with a scorer copy |
| R2 | claude-sonnet-5-5 | 6 | 0.15 | 356 | 7 (2 UNVERIFIED, 3 grep-only) | cheapest; shallowest sources |
| R3 | gpt-6.1-sol | 28 | 2.30 | 405 | 7 (several UNVERIFIED) | most complete pane proposal |
