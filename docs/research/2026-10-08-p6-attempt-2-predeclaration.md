# P6 attempt 2 — pre-declaration (written before any training or scoring)

**Date:** 2026-10-08. **Status:** declared, not run. Attempt 1 (`2026-10-07-p6-g1-attempt-1.md`)
failed G1 as pre-registered: C1 −6.2% vs the logistic hand-feature baseline, C4 on early
epochs, C2/C3/C5 inconclusive (0 gold). The cross-check located the deficit: a linear probe on
the JEPA's own inputs beats the JEPA head, the loss sits after `run`/`test`/`vcs` steps, and
longer or larger training overfits. This file fixes what attempt 2 changes and how it is
scored, so nothing is tuned against the test split.

## Changes to the registered model (each a named ablation; the gate text is unchanged)

| id | change | why (evidence from attempt 1) |
|---|---|---|
| A2-F | JEPA inputs gain the cross-step state the hand features carry: f_ref-normalised test progress, cumulative error share, tests-ran-so-far, spawned-so-far, log1p(stream position) | the −10–14% deficit after run/test/vcs steps; these are structured task state, never text |
| A2-E | absorbing chain gets an error-conditioned state (`<class>!err` for steps with `err=1`) | retries are state-dependent (P(fail\|fail) 46% vs 3%); `chains.py` states were classes only |
| A2-D | a day-regime feature (errors in the session's last 15 min; the day's error rate so far) for the logistic baseline **and** the JEPA | per-day CE moves together across models; the regime is unmodelled |
| A2-W | w_reg sweep bounds checked on **every** epoch (as C4 reads), so the sweep cannot choose a run that fails C4 by construction | sweep/gate mismatch found in review; C4 stays strict by owner decision |

Baselines receive A2-D too, so the comparison stays fair; A2-F only gives the JEPA what the
logistic model already has.

## Data and scoring

- Dataset: the frozen split bounds in `manifest.json` (val from 2026-10-03T19:41Z, test from
  2026-10-04T21:59Z); a rebuild after gold labels and the transcript mirror is allowed (it adds
  sessions to the *test* side by time and labels to all sides); no `--resplit`.
- Gold: labels marked `by=user` or `by=model:*` count for C2/C3/C5; the card states the split.
  If gold on the test split is < 10 tasks, C2/C5 stay INCONCLUSIVE by rule.
- **One scoring of the test split** for attempt 2: `worldmodel evaluate --train --seeds 2 --sweep`
  once, after the ablations are chosen on val only; the ledger line is the record. No re-runs
  "to check". A second scoring needs a new pre-declaration.
- Same gate G1 (5 criteria), same C4 strict reading, same owner-signed C5 rule, same session-
  cluster bootstrap, same start-prior convention, streams view.

## Hypotheses (stated before running)

- H1: with A2-F the JEPA matches the logistic baseline within its CI on C1 but does not beat it
  by 5% — the data (≈15k train steps) is the limit, not the inputs. Expected: C1 FAIL, narrower.
- H2: A2-E lowers the chain's expected-steps variance toward the observed task-length variance
  (attempt 1: model sd ≈ 2× observed).
- H3: C4 PASS under A2-W at w_reg 10, at a C1 cost ≤ 1%.
- H4: C2/C3/C5 become scorable; the untrained-value-head caveat disappears once ≥ 1 labelled
  train task exists; no prediction on their verdicts.

## Kill criterion (unchanged)

Drop P6 if G1 still fails at 50k main-session steps. Attempt 2 is the "current data +
declared fixes" run; it does not count as the 50k attempt.
