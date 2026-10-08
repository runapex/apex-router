# P6 loop 2, iteration 1b — making C4 reachable from initialisation (train/val only)

**Date:** 2026-10-08. **Scope:** recipe options in `TrainConfig`, real data, **train/val only**.
`worldmodel evaluate` was not run and the test split was never scored. Gate G1 and the strict C4
reading are unchanged: every epoch's val z must have effective rank ≥ 8 and SIGReg ≤ 0.10.

## Result

One recipe keeps **every epoch within bounds** on the real data. It does so with wide margins:
SIGReg ≤ 0.075 and effective rank ≥ 25 in every epoch of every run. The recipe is
`z_norm="center"` plus a SIGReg anneal from 30 over 4 epochs. It held on seed 0 on CPU (12/12
epochs), on seed 1 (11/11) and on GPU (12/12), and at every final w_reg tried (1, 3, 10).

**The cost is about +5% val CE** with step 0 included (+4.8% to +6.6%). This is no worse than
the plainest in-bounds lever, constant w_reg 10, which costs +5.0% and still fails epochs 0–2.
But it is not free. Attempt 2's C1 deficit was −4% on test, so C1 should be expected to widen
under this recipe.

Nothing that leaves z un-normalised passes. The closest such run is constant w_reg 10: 9/12
epochs in bounds, failing epochs 0–2 at SIGReg 0.154 / 0.128 / 0.116.

## What the diagnosis showed (why the asked-for options alone do not work)

All numbers below are from val z on the real data, seed 0, CPU.

1. **The control's out-of-bounds SIGReg is a mean offset.** This is not a rank problem.
   - At the best checkpoint, val SIGReg is 0.191 raw, 0.053 once z is centred, and 0.049 when
     also standardised. The effective rank is 14.5 throughout.
   - The z mean has norm 2.8 (train) to 3.6 (val), and the mean cosine is 0.17–0.28 across epochs.
   - At w_reg 1, SIGReg does not remove the offset. At w_reg 10–100 it does, except in the
     first 1–3 epochs.
2. **At initialisation z is nearly constant.** Init val SIGReg is 0.39, which is the
   "constant z" value. The mean cosine is 0.61, while the effective rank is 46.
   - Orthogonal output rows alone do not change this (0.43).
   - Centring and scaling the orthogonal rows from train statistics gives SIGReg 0.034 at rank
     47. That puts z inside the bounds before the first update.
3. **The first ~10 updates throw the mean back out, even at a tiny learning rate.** In a
   step-level trace (lr 5e-6 → 1.4e-4), the val z mean norm went 1.8 → 9.6 in 12 Adam steps.
   Reverting parameter groups showed the move comes from the transformer layers, not `to_z`.
   This is why `rank_floor_init` alone gets epoch 0 to 0.56 despite starting at 0.04.
4. **A short LR warm-up collapses rank.** With `lr_warmup_epochs=0`, the effective rank falls
   to 3–5 in epoch 0 at every w_reg tried (1, 10, 100). The 40-step default warm-up protects
   rank. Attempt 2's note that high w_reg "fails the rank bound" does not hold at the default
   warm-up: w_reg 30/100 failed in epochs 0–2 on SIGReg while rank stayed ≥ 8.2. Rank < 8 only
   appears with a short or zero warm-up.
5. **Normalising z needs train/eval-consistent statistics.**
   - A batch-norm map (batch statistics in training, momentum buffers in evaluation) failed:
     the encoder's mean moves faster than a momentum-0.1 buffer can follow (lag ≈ 3 in norm
     through epoch 0). That stale buffer is itself an offset that SIGReg reads.
   - The fix is "precise BN": before every checkpoint, set the buffers to the exact train-set
     statistics (`refresh_z_stats`). This uses train windows only.
   - Plain centring (`center`) works. ZCA (`whiten`) does not: 5 Newton–Schulz steps leave
     about 12 unit-variance directions and about 52 near-zero ones, which SIGReg reads as a too
     small scale.

## Recipe table

Setup for every run:
- Real dataset, streams view, `--features a2f --regime`, 12 epochs, patience 3 (as attempt 2),
  seed 0, `--device cpu` (bit-exact).
- Start w_reg 1, as in the attempt-2 default, unless the run says otherwise.
- Training time is the pure training loop on CPU; the GPU run took 6.5 s.
- CE is the best checkpoint's val CE in nats/step: E3's readout (step 0 excluded) and the
  E4/baseline convention (step 0 from the train start prior, val sequences only).
- Δ is the step-0-included CE against the CPU control.

| run | recipe | epochs in bounds | first in | failing epochs | best val CE excl / incl step 0 | Δ | erank range | SIGReg range | train s |
|---|---|---|---|---|---|---|---|---|---|
| `c4-ctrl-cpu` | **control** (attempt-2 default) | 0/8 | — | all | 1.6858 / 1.7068 | 0 | 12.4–15.9 | 0.144–0.275 | 14.7 |
| `c4-ctrl` | control, GPU | 0/8 | — | all | 1.6820 / 1.7033 | −0.2% | 12.4–16.0 | 0.142–0.275 | 4.4 |
| `c4-ln` | z_norm layernorm | 0/8 | — | all | 1.6950 / 1.7153 | +0.5% | 9.6–11.8 | 0.137–0.220 | 18.0 |
| `c4-wh4` | z_norm whiten | 0/8 | — | all | 1.8618 / 1.8699 | +9.6% | 37.6–41.7 | 0.118–0.195 | 12.3 |
| `c4-ctr4` | z_norm center | 8/11 | 3 | 0, 1, 2 (0.117, 0.103, 0.101) | 1.8143 / 1.8258 | +7.0% | 11.3–17.9 | 0.067–0.117 | 16.6 |
| `c4-ann` | anneal 30→1 over 4 | 0/12 | — | all | 1.7147 / 1.7336 | +1.6% | 8.7–14.7 | 0.108–0.148 | 18.7 |
| `c4-orth` | rank_floor_init orthogonal | 0/8 | — | all | 1.6937 / 1.7141 | +0.4% | 12.7–17.2 | 0.151–0.341 | 19.8 |
| `c4-cinit` | rank_floor_init centered | 0/12 | — | all (init 0.039 in bounds) | 1.6843 / 1.7055 | −0.1% | 12.9–19.7 | 0.111–0.558 | 19.3 |
| `c4-lrw0` | lr_warmup_epochs 0 | 0/8 | — | all | 1.7412 / 1.7582 | +3.0% | **4.0**–10.4 | 0.125–0.335 | 23.8 |
| `c4-w3` | const w_reg 3 (reference) | 5/12 | 6 | 0–5, 7 | 1.7033 / 1.7231 | +1.0% | 11.8–16.9 | 0.083–0.207 | 18.3 |
| `c4-w10` | const w_reg 10 (reference) | 9/12 | 3 | 0, 1, 2 (0.154, 0.128, 0.116) | 1.7787 / 1.7929 | +5.0% | 9.2–13.2 | 0.061–0.154 | 18.0 |
| `c4-ctr-w3` | center + const 3 | **8/8** | 0 | — | 1.8230 / 1.8339 | +7.4% | 15.2–19.7 | 0.055–0.087 | 12.2 |
| `c4-ctr-a10to1` | center + anneal 10→1 | **9/9** | 0 | — | 1.7992 / 1.8119 | +6.2% | 19.2–23.8 | 0.059–0.076 | 13.7 |
| `c4-ctr-a30to1` | center + anneal 30→1 | **12/12** | 0 | — | 1.7835 / 1.7974 | +5.3% | 25.7–29.8 | 0.043–0.061 | 18.2 |
| `c4-ctr-a30to3` | center + anneal 30→3 | **12/12** | 0 | — | 1.7847 / 1.7984 | +5.4% | 26.7–32.4 | 0.034–0.055 | 18.3 |
| `c4-ctr-a30to10` | **center + anneal 30→10** | **12/12** | 0 | — | 1.7740 / 1.7885 | **+4.8%** | 27.7–36.4 | 0.027–0.055 | 18.3 |
| `c4-ctr-a30to1-cinit` | center + anneal 30→1 + centered init | **12/12** | 0 | — | 1.7839 / 1.7977 | +5.3% | 29.3–33.7 | 0.041–0.058 | 18.6 |
| `c4-ctr-lrw3` | center + lr_warmup_epochs 3 | 6/11 | 1 | 0, 2–5 | 1.8370 / 1.8468 | +8.2% | 14.0–20.4 | 0.065–0.123 | 16.7 |
| `c4-ctr-w03` | center + const 0.3 (diagnostic) | 6/12 | 6 | 0–5 | 1.8108 / 1.8226 | +6.8% | 9.4–16.2 | 0.087–0.153 | 18.2 |
| `c4-ctrR` | center, running stats in training | 6/11 | 0 | 2–6 | 1.8529 / 1.8616 | +9.1% | 12.3–15.7 | 0.088–0.123 | 16.8 |
| `c4-ctrR-a30to1` | ↑ + anneal 30→1 | 9/12 | 1 | 0, 3, 5 | 1.8309 / 1.8412 | +7.9% | 10.7–15.5 | 0.082–0.104 | 18.1 |
| `c4-ctrRN` | center, batch renorm in training | 5/12 | 4 | 0–3, 5, 7, 9 | 1.7858 / 1.7995 | +5.4% | 8.9–13.0 | 0.087–0.124 | 20.3 |
| `c4-ctrRN-a30to10` | ↑ + anneal 30→10 | 12/12 | 0 | — | 1.8035 / 1.8158 | +6.4% | **8.5**–12.7 | 0.066–0.097 | 18.2 |
| `c4-a100to10k3` | anneal 100→10 over 3 | 7/10 | 1 | 0, 2, 3 | 1.9037 / 1.9086 | +11.8% | 9.1–10.7 | 0.065–0.138 | 15.1 |
| `c4-a100to10k3-lrw05` | ↑ + lr_warmup 0.5 | 8/12 | 4 | 0–3 (rank 7.3–7.6) | 1.8578 / 1.8661 | +9.3% | 7.3–9.6 | 0.058–0.170 | 17.9 |
| `c4-a100to10k3-lrw0` | ↑ + lr_warmup 0 | 0/12 | — | all (rank 3–7) | 1.9951 / 1.9933 | +16.8% | 3.1–6.9 | 0.081–0.321 | 17.7 |
| `c4-ann-lrw0` | anneal 30→1 + lr_warmup 0 | 1/12 | 11 | 0–10 | 1.8415 / 1.8511 | +8.5% | 3.4–10.6 | 0.098–0.440 | 18.7 |
| `c4-w10-lrw0` | const 10 + lr_warmup 0 | 0/9 | — | all | 2.0266 / 2.0225 | +18.5% | 4.9–7.4 | 0.083–0.302 | 13.7 |
| `c4-cinit-w10` | centered init + const 10 | 7/12 | 4 | 0–3, 6 | 1.7661 / 1.7812 | +4.4% | 11.6–15.6 | 0.059–0.251 | 18.1 |
| `c4-cinit-a30to3` | centered init + anneal 30→3 | 3/10 | 7 | 0–6 | 1.7255 / 1.7436 | +2.2% | 11.5–16.6 | 0.084–0.295 | 15.2 |
| `c4-cinit-a100to10k3` | centered init + anneal 100→10/3 | 7/12 | 4 | 0–3, 6 | 1.7724 / 1.7870 | +4.7% | 10.9–15.3 | 0.066–0.286 | 17.9 |
| `c4-cinit-a100to10k3-lrw3` | ↑ + lr_warmup 3 | 8/12 | 4 | 0–3 (0.217, 0.142, 0.187, 0.101) | 1.7512 / 1.7674 | +3.6% | 16.3–27.8 | 0.068–0.217 | 18.2 |
| `c4-cinit-w10-lrw3` | centered init + const 10 + lr_warmup 3 | 6/12 | 5 | 0–4, 8 | 1.7553 / 1.7712 | +3.8% | 15.9–26.6 | 0.064–0.289 | 18.1 |
| `c4-ln-cinit-w10` | layernorm + centered init + const 10 | 4/12 | 7 | 0–6, 10 | 1.8129 / 1.8245 | +6.9% | 7.7–10.4 | 0.075–0.156 | 17.9 |
| `c4-ln-cinit-a30to3` | layernorm + centered init + anneal 30→3 | 1/12 | 8 | all but 8 | 1.7475 / 1.7640 | +3.3% | 7.8–11.7 | 0.096–0.131 | 17.9 |
| `c4-ln-a100to10k3` | layernorm + anneal 100→10/3 | 0/12 | — | all (rank 6–7.8) | 1.8628 / 1.8708 | +9.6% | 6.0–7.8 | 0.070–0.133 | 17.8 |

**Robustness of the chosen recipe** (center + anneal 30→10):

| check | in bounds | best val CE excl / incl | Δ vs that run's own control |
|---|---|---|---|
| seed 1, CPU (`c4-ctr-a30to10-seed1`) | 11/11 | 1.8073 / 1.8194 | +5.8% vs `c4-ctrl-seed1` (1.6996 / 1.7196, 0/8 in bounds) |
| seed 0, GPU (`c4-ctr-a30to10-gpu`) | 12/12 | 1.7851 / 1.7988 | +5.6% vs `c4-ctrl` (GPU) |

**Per-epoch erank / SIGReg for the key runs:**

- Control:
  - erank 13.3, 12.4, 12.9, 13.4, 14.5, 15.1, 15.2, 15.9
  - SIGReg .275 .236 .235 .186 .191 .180 .160 .144
- const w_reg 10:
  - erank 11.0 … 13.2
  - SIGReg **.154 .128 .116** .086 .080 .082 .075 .064 .066 .064 .073 .061
- center + anneal 30→10:
  - erank 29.1, 27.7, 28.0, 28.8, 30.4, 31.6, 31.8, 33.0, 34.0, 34.8, 35.6, 36.4
  - SIGReg .055 .050 .039 .034 .037 .037 .033 .036 .029 .032 .027 .027
- center + anneal 30→1:
  - erank 29.1 … 29.8
  - SIGReg .055 .048 .061 .053 .052 .060 .053 .050 .052 .052 .048 .043

**Earlier variants replaced in the code** (logs only; their weights do not load under the final
code):
- `c4-wh`: batch-statistics IterNorm averaging per-batch matrices; SIGReg 0.21–0.59.
- `c4-wh2`: running-statistics ZCA in training and evaluation; 0.26–0.33.
- `c4-ctr`: running-statistics center without the exact refresh; 0.17–0.26.
- `c4-ctr3`: batch center without the exact refresh; 0.22–0.46.
- `c4-diag-meanonly`: a temporary mean-only center; CE 1.81, the same cost as with the scale.

The directories `c4-ctr-a30to10-s1`, `c4-ctr-a30to1-s1` and `c4-ctrl-s1` are mislabelled
**seed-0** duplicates: the runner's `--seed 0` overrode the config's seed. The real seed-1 runs
are the `-seed1` directories.

## Decision

All epochs within bounds at the smallest val-CE cost: **`z_norm="center"`, `w_reg_schedule=
"anneal"`, `w_reg_start=30`, `w_reg_anneal_epochs=4`, final `w_reg` 10.**
- Cost: +4.8% val CE (step 0 included) on seed 0, +5.8% on seed 1, +5.6% on GPU.
- The final w_reg barely matters: 1 / 3 / 10 give +5.3 / +5.4 / +4.8% and all pass with
  margin. The 0.5-point differences are inside the run-to-run spread, so the grid should be
  declared rather than the single value.
- `rank_floor_init="centered"` adds nothing on top (+5.3%).
- Every un-normalised recipe tried fails at least epoch 0.

## Caveats

1. **What "center" does to the C4 reading.**
   - The SIGReg bound now reads z after a fixed affine map, estimated on train: centring plus
     one global scale.
   - The encoder output under that map stays far outside the bound: pre-norm SIGReg is
     ~0.76–0.84 at the best checkpoint (`recipe.pre_norm_collapse`).
   - Effective rank is invariant to the map, since it is centred and scale-free. The rank gain
     is therefore genuine: 25–36 against the control's 12–16.
   - The map is part of the model: the heads, predictor, value head and SIGReg all read the
     normalised z, and inference applies it. The gate text is untouched. But in practice C4's
     SIGReg half now tests the shape of z, not its mean or scale. **The owner should decide
     whether that is the intended reading** before declaring.
2. **CE cost.** About 5% on val. This comes from the centring itself: w_reg 0.3, mean-only,
   running-stats and renorm variants all cost 5–9%. It is not from batch-statistics noise
   (renorm removes that noise and still costs). With C1 at −4% in attempt 2, a rough expectation is C1 around
   −9% under this recipe.
3. **Coverage.**
   - One seed per cell, plus one seed-1 and one GPU replication of the winner.
   - Val only. C4 also reads the scored checkpoint's test z, which was not measured here.
   - 12 epochs: attempt 2 used 20, which gives a 68-step LR warm-up instead of 40.
4. **Live-install guard.** The full suite's isolation guard flagged
   `~/.apex-router/labels/judge_bench/cache/gpt-oss_20b_export_v1.jsonl`. The concurrent
   judging agent keeps writing that file (its mtime advanced during an idle 20 s with no tests
   running); these changes do not touch it.

## Proposed attempt-3 pre-declaration paragraph — *proposed, not declared*

> **A3-C (collapse recipe).** Attempt 3 changes only the collapse recipe. Inputs and every
> other attempt-2 setting stay as declared: A2-F, A2-D, A2-E, the A2-W every-epoch sweep rule,
> streams view, frozen split, one scoring, and the same five G1 rules, including the strict
> reading of C4.
>
> - The JEPA's latent gets `z_norm="center"`. Training uses the batch statistics of the real
>   positions. Before every checkpoint, the map's statistics are set to the exact train-set
>   statistics, so val/test z is a fixed affine map of the encoder output estimated on train.
> - The SIGReg weight anneals geometrically from 30 to the final w_reg over the first 4 epochs.
> - Grid for the final w_reg: **{1, 3, 10}**, every-epoch rule. 12 epochs, patience 3, default
>   LR warm-up.
> - C4 is read exactly as before: every epoch's val z plus the scored checkpoint's test z. The
>   scorecard also reports the pre-normalisation SIGReg and rank beside them, labelled
>   informational.
> - **H3′:** C4 PASS (val: 12/12 epochs within bounds at every grid point on seed 0;
>   SIGReg ≤ 0.075, rank ≥ 25).
> - **H5:** the recipe costs about 5% next-action CE, so C1 FAILs wider than attempt 2's −4%.
>
> Prerequisite: `worldmodel evaluate --train` passes only `--features/--regime/--err-states/
> --w-reg-grid` into `TrainConfig`. A recipe override must be added, and tested on synthetic
> data, before the declaration can be run.

## Reproduce (train/val only)

```
apex-router worldmodel train --features a2f --regime --epochs 12 --seed 0 --device cpu \
  --run-id c4-ctr-a30to10 \
  --config '{"z_norm":"center","w_reg":10,"w_reg_schedule":"anneal","w_reg_start":30}'
```

Every run records its recipe in `summary.json` → `recipe`:
- the recipe fields and `default`;
- `lr_warmup_steps` and `w_reg_per_epoch`;
- `init_collapse` (val z before the first update);
- `pre_norm_collapse` (with `z_norm`) and `centered_init` (with that init).
