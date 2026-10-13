# Follow-up — full-context exchange round vs self-consistency: results

Pre-declaration: `2026-10-13-debate-vs-sc-predeclaration.md`. Harness `scripts/panel_debate.py`. Card:
`~/.apex-router/panel/card-debate-musique-356.json`. Model `it-entra-gpt-5.6-luna`, effort `low`.

**Outcome C (pre-declared): close the thread.** One exchange round, with or without peers, is no better
than three more independent samples. Self-consistency with document-order diversity is the only thing
that clearly helps (+0.061 Brier over a single call).

## Arms (n=356 of 360; 4 items dropped from every arm — Azure content-filter 400s, deterministic on retry)

| arm | Brier | acc | 2-hop | 3-hop | 4-hop | tok/item |
|---|---|---|---|---|---|---|
| single | 0.2028 | 77.8% | 0.1969 | 0.2021 | 0.2228 | 2,726 |
| sc6 | 0.1421 | 81.2% | 0.1217 | 0.1622 | 0.1694 | 16,369 |
| rho1 (own + peers) | 0.1422 | 83.4% | 0.1157 | 0.1474 | 0.2166 | 16,835 |
| reflect (own only) | 0.1415 | 82.3% | 0.1219 | 0.1552 | 0.1781 | 16,531 |

Diversity worked this time: sample range > 0.05 on 210/356 items (HotpotQA: 40/239), round-1 label
disagreement on 80.

## Pre-declared tests (paired bootstrap, BH across four)

| contrast | Brier gain | CI95 | p | status |
|---|---|---|---|---|
| C1 rho1 vs sc6 (primary) | −0.0001 | [−0.0168, +0.0167] | 0.968 | no detectable difference |
| C2 rho1 vs reflect | −0.0007 | [−0.0139, +0.0122] | 0.889 | no detectable difference |
| C3 reflect vs sc6 | +0.0006 | [−0.0116, +0.0128] | 0.896 | no detectable difference |
| C4 sc6 vs single | +0.0607 | [+0.0369, +0.0857] | 0.001 | **better** (BH reject) |

Gate `debate/musique/vs-sc6` (candidate `rho1` only, no cost): promo +0.0066, confirmation −0.0081 →
"winner's advantage evaporates out-of-sample", not promoted. The HotpotQA hint (+0.0107, exploratory)
did not replicate under a fair design.

## Descriptive (not pre-declared)

- On the 80 round-1-disagreement items, the exchange round helps: rho1 0.198, reflect 0.211, sc6 0.233
  (single 0.455). rho1 flipped 26 pooled labels vs its round 1 (16 to right, 10 to wrong); reflect 16 (9/7).
  Over all items those gains are cancelled: rho1 is worse on 4-hop (0.217 vs sc6 0.169).
- Three samples vs six: sc3 is 0.008 worse than sc6, CI [−0.020, +0.003] — most of the gain is in the
  first three samples.

## Limits

One model, one effort level, one benchmark; luna has no temperature, so document order is the diversity
source for every arm (by design, the same for all). 4/360 items dropped by the content filter, conditional
on every arm completing.

## What changes (per the pre-declared rule)

C → no skill change (`cross-validate-codex` keeps its single reviewer pass). The backlog records the
closure of the panel/deliberation line (`RESEARCH-FIT-BACKLOG.md`). The positive result is self-consistency
itself: if a judgment call ever needs a cheap calibration gain, three samples with permuted evidence order
is the measured lever — a candidate only, not built.
