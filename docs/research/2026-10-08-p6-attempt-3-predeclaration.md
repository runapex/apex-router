# P6 attempt 3 — pre-declaration (written before training or scoring)

**Date:** 2026-10-08 (after loop-2 iterations 1–2). **Status:** declared; one scoring follows.

## What changed since attempt 2 (evidence)
- Labels: judge `ornith:35b · export · v2` (held out 0.95 [0.83, 0.99]), min confidence **0.8**
  (pooled gold 28/35 in the [0.8, 0.9) bin), judge + rule votes combined as one voter when the
  judge saw the rule facts; gold 182 (all model-made, 82 held-out-tagged), weak 286, unknown 65.
  Gold covers the whole test split: 41 scorable, **3 bad** — any fail metric rests on 3 tasks.
- Collapse recipe A3-C (`docs/research/2026-10-08-p6-c4-warmup-recipe.md`, val only):
  `z_norm=center` + SIGReg anneal 30 → w_reg over 4 epochs keeps every epoch within bounds at
  ≈ +5% val CE. **Definitional note, stated up front:** with centring, C4's SIGReg reads the model
  latent z := center-map(encoder) — the latent every head and the planner read — so it tests
  the shape of z, not its position; the card prints pre-map values beside it; effective rank is
  map-invariant. C4 PASS under A3-C is expected from val and carries no new information; this
  attempt is scored for C1, C2, C3, C5 under corrected labels.
- Floors now coded: C2 INCONCLUSIVE below 10 gold test tasks; C5 below 10 gold or 3 bad.

## The one scoring (venv interpreter; the uv-tool one has no mlx)
```
PYTHONPATH=src .venv/bin/python -m apex_router.cli worldmodel evaluate --train --seeds 2 \
  --sweep --sweep-every-epoch --w-reg-grid 1,3,10 --features a2f --regime --err-states \
  --z-norm center --w-reg-schedule anneal --w-reg-start 30 --w-reg-anneal-epochs 4
```
Epochs: the default (20, as attempt 2; the recipe doc used 12). Frozen split; streams view;
one train start prior; session-cluster bootstrap; same gate G1, strict C4, owner-signed C5.
No re-runs; a second scoring needs a new pre-declaration.

## Hypotheses
- H1: C1 FAIL, rel ≈ −9% ± 3 (attempt 2 −4.0% plus the ≈ 5% centring tax on val).
- H2: C4 PASS on every checkpoint (val-known; the test z is the only unknown).
- H3: C2 scorable (≥ 10 gold); no prediction on its verdict — the test split is 93% success,
  so a win over the constant baseline is weak evidence either way.
- H4: C5 scorable at exactly 3 bad tasks; expected INCONCLUSIVE-level power even if it reads
  PASS/FAIL (wins/losses over 3 tasks cannot clear the Wilson rule).
- H5: C3 still INCONCLUSIVE (no workflow pair with different realized success among ≥ 5
  labelled test tasks each — realized success is ≈ 1.0 for both W0 and W2).

## Kill criterion (unchanged)
Drop P6 if G1 fails at 50k main-session steps. Stop iterating P6 earlier if, with corrected
labels, the raw-input linear probe beats the JEPA head twice in a row (attempt 2: 1.712 vs 1.742).
