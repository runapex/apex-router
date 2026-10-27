# p99 discovery harness: measurement, plan, status

Source: the essay "Designing a p99 Creativity Harness for AI Agents" (diverge → challenge → evolve → prove,
Quality–Diversity archive, evidence over consensus). This note checks its premise on our own data, then
fixes the scope of `apex-router explore`.

## 1. Measurement on our data (no new model calls)

Data: `~/.apex-router/panel/calls.jsonl`, MuSiQue-Ans dev, binarised, n = 356, `it-entra-gpt-5.6-luna`
low effort (pre-declared run, `2026-10-13-debate-vs-sc-*`). `sc6`, `rho1` and `reflect` share the round-1
calls `s0–s2`. `rho1` = one round where each agent sees the two peers' answers. `reflect` = one round
where each agent sees only its own answer.

| Pool of 3 answers | Mean p range | Items with split verdict |
|---|---|---|
| round 1 (`s0–s2`, independent) | 0.227 | 74 (20.7 %) |
| after `reflect` (own answer only) | 0.199 | 70 (19.7 %) |
| after `rho1` (peers visible) | 0.077 | 23 (6.5 %) |

Items where round 1 was split, by outcome after round 2:

| Round-1 state | Arm | All right | Still split | All wrong |
|---|---|---|---|---|
| right answer in the minority (1 of 3), 32 items | `rho1` | 10 | 11 | **11** |
| | `reflect` | 2 | 28 | 2 |
| right answer in the majority (2 of 3), 42 items | `rho1` | 29 | 5 | 8 |
| | `reflect` | 10 | 30 | 2 |

Findings:
1. Peer visibility, not a second look, removes the disagreement: split verdicts fall from 20.7 % to 6.5 %
   with peers and to 19.7 % without.
2. When the right answer is in the minority, one exchange round removes it completely in 11 of 32 items
   (34 %). A second look alone does that in 2 of 32.
3. Accuracy and Brier did not improve (C1 `rho1` vs `sc6`: −0.0001 Brier, CI [−0.017, +0.017]). The
   exchange converts diversity into agreement, and the agreement is not more correct.

This supports the essay's principle 3 ("evidence, not consensus; a right minority must survive") on a
closed-answer task. It says nothing yet about open-ended "p99 breakthroughs".

## 2. Scope of `apex-router explore`

The harness owns exploration, memory, evaluation and selection. Models are replaceable workers.

| Part | Rule |
|---|---|
| Islands | 6 operators: first-principles, inversion, cross-domain, constraint-mutation, adversarial, simplification. Diverge-phase prompt = goal + constraints + facts + negative evidence. It never contains another candidate. |
| Archive | MAP-Elites style. Niche = (operator, mechanism fingerprint). One champion per niche. Candidates are immutable; an evolution makes a child with `parent` ids. |
| Evidence | Append-only JSONL. Keeps rejections and counterexamples, so a disproven mechanism is not proposed again. |
| Selection | Hard gates (oracle `valid`), then a Pareto front over (impact, novelty, evidence). No vote count removes a candidate. |
| Scheduler | Exploration floor: a fixed share of each round goes to empty or untested niches. |
| Oracle | An external command. The generator never sees its files. Only the oracle sets `valid` and `score`. |
| Metrics | `HitRate_τ@B` with a Wilson CI, time to first hit, niches covered, cost. p99 impact is printed only when n ≥ 100; else "n too small". |

The harness does not keep or merge its own output. A human adopts a candidate. Thus R1–R3 (outside
score) apply to any later loop that auto-adopts candidates, and the oracle is the outside score.

## 3. Evaluation plan (pre-declared)

- Arms at equal model-call budget B: `explore` vs `baseline` (B independent samples of the same plain
  prompt, best oracle score wins).
- Primary metric: `HitRate_τ@B`, τ = the task's breakthrough threshold, fixed before the run.
- Ablations: no archive (greedy), shared context (islands see each other), no oracle (model self-score).
- A smoke pilot on a few tasks checks the plumbing only. It is not evidence of a p99 gain.

## 4. Status

See the commit log for the implementation and the pilot result table.
