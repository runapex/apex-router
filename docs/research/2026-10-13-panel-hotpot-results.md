# Panel step 2 — results (HotpotQA engine demo)

Pre-declaration: `2026-10-13-panel-hotpot-predeclaration.md`. Harness: `scripts/panel.py`. Card:
`~/.apex-router/panel/card-…-239.json`. Model `it-entra-gpt-5.6-luna`, effort `low`, 6,369 calls.

**Verdict: no promotion. K2 fires (the gate promoted no panel arm over self-consistency). K3: evidence
exchange beats rationale exchange. The paper's mechanism does not reproduce on this set: real
evidence asymmetry *hurts*, and rationale exchange as implemented does not repair it.**

Revised after Codex review (pass 1, 10 findings; see "Review reconciliation"). Scores recomputed from the
call cache with `single` clipped like every pooled arm (Brier 0.0518 → 0.0517; nothing else moved).

## Arms (n=239; one item dropped from every arm — Azure content-filter 400 on its random-routing prompts)

| arm | Brier | acc | tok/item | note |
|---|---|---|---|---|
| single | 0.0517 | 94.6% | 1,481 | baseline |
| sc6 | 0.0423 | 95.8% | 8,890 | sample range > 0.05 on 40/239 items, any range on 155 (no temperature control) |
| rho1 | **0.0316** | 96.7% | 9,366 | everyone sees everything, rationale exchange |
| asym_r1 | 0.0473 | 93.7% | 3,194 | BM25 rho=.5, round 1 |
| asym_rat | 0.0367 | 95.0% | 6,734 | InfoDelphi-faithful |
| rand_r1 | 0.0687 | 92.1% | 3,383 | random rho=.5, round 1 |
| rand_rat | 0.0686 | 93.3% | 7,150 | + rationale exchange |
| rand_ev | 0.0395 | 95.4% | 7,259 | + evidence exchange (capped at rationale length) |

Primary aggregator uniform logit pool; CW (extremity) changes no ordering.

## Pre-declared contrasts (paired bootstrap, BH across six)

| contrast | Brier gain | CI95 | BH |
|---|---|---|---|
| asym_rat vs sc6 | +0.0056 | [−0.0032, +0.0154] | – |
| rand_rat vs sc6 | −0.0263 | [−0.0434, −0.0090] | **reject (worse)** |
| asym_rat vs rho1 (asymmetry, BM25) | −0.0051 | [−0.0165, +0.0046] | – |
| rand_rat vs rho1 (asymmetry, random) | −0.0370 | [−0.0522, −0.0217] | **reject (worse)** |
| rand_ev vs rand_rat | +0.0291 | [+0.0166, +0.0420] | **reject (evidence better)** |
| asym_rat vs single | +0.0150 | [−0.0033, +0.0353] | – |

## Gate (`run_gate`, k=30, m=2)

- `vs-sc6`: promo-split winner `rand_ev` (+0.0122, just above `rho1` +0.0115); its confirm-split gain is
  −0.0065 → "advantage evaporates out-of-sample", not promoted. `rho1` would have confirmed (+0.0099) but
  was not the promo winner; the gate does not shop for a second candidate. This is the winner's-curse guard
  working as designed.
- `vs-single`: tested, p=0.244, not promoted. The pre-declared `cost = tokens/item` switches on the gate's §5.4
  cost tiebreak: after the promo winner (`rand_ev`) is fixed, the cheapest candidate that independently
  passes on its OWN confirmation evidence (`asym_r1`) replaces it, and FDR then applies to that candidate.
  The pre-declaration's "winner picked on promo" did not describe this; the tiebreak reads confirmation
  means, so it is not a pure promo-only selection. It did not change the outcome (no promotion either way).
  The follow-up study runs the gate without cost.

## Kills

- **K2 fires**: the gate promoted no panel arm over sc6. That is a statement about the gate's selected
  candidate, not that every panel arm failed out-of-sample: `rho1` had +0.0099 on the confirmation split.
- **K3**: evidence exchange beats rationale exchange (CI excludes 0). If anything is built, it's the
  simpler one.
- **K4** does not fire: asym_rat is 4.55× single tokens with a +0.015 point gain (≥0.01 bar), but that gain is
  not significant.

## What the mechanism actually did

Where random routing put the two gold paragraphs on different agents (n=39, the exact case InfoDelphi targets):

| single | rho1 | rand_r1 | rand_rat | rand_ev |
|---|---|---|---|---|
| 0.0900 | 0.0631 | 0.1319 | **0.1510** | 0.0762 |

Rationale exchange made those items *worse* than independent round 1. Passing the evidence itself restored
most of the loss on this subgroup (0.0762 vs 0.1510), still 0.0131 behind the everyone-sees-everything arm
(0.0631; n=39, no interval computed). Over all 239 items `rand_ev` vs `rho1` is −0.0079, CI [−0.0185, +0.0043]
(exploratory, not pre-declared). The evidence arm does not literally restore full context: each peer's
private paragraphs are truncated to that peer's rationale length in characters, so a long first paragraph
can use the whole budget. BM25 routing looked fine only because it rarely made anything private: 72/478
gold paragraphs private, 2/239 items split.

The arm that did best is the one with no asymmetry: `rho1` (full context + one round of exchange) vs sc6,
exploratory +0.0107, CI [−0.0000, +0.0237] — borderline and not pre-declared as a primary.

## Caveats

- Near ceiling (single 94.6% acc); HotpotQA is likely partly memorized. Paired design keeps the comparison
  fair but compresses headroom; effects here are small in absolute terms.
- No temperature control on this model: sc6 is a weak self-consistency (range > 0.05 on 40/239 items),
  which favours every panel arm.
- Capture windows are synthetic hash shards; the gate's replication layer was exercised mechanically only.
- Engine demo, not an InfoDelphi reproduction (PolyGym evidence unreleased).

## Review reconciliation (Codex `it-entra-gpt-6-astra`, pass 1)

| # | finding | triage | action |
|---|---|---|---|
| 1 | cost tiebreak reads confirmation outcomes, contradicting "winner picked on promo" | CONFIRMED (description) | documented above; follow-up runs gate without cost |
| 2 | round 2 omits the agent's own round-1 answer, so "reconsider" has nothing to reconsider | CONFIRMED | matches the paper's h over k≠j, but a design flaw; follow-up shows own + peers, and adds a self-only control |
| 3 | −0.0079 is the whole-set contrast, attached to the 39-item subgroup | CONFIRMED | corrected (subgroup gap is 0.0131) |
| 4 | controls get a neutral prompt, panels get panel framing | CONFIRMED (confound) | disclosed; follow-up uses one neutral prompt for every arm |
| 5 | evidence budget is char-truncated concatenation; "restores full context" unsupported | CONFIRMED | claim removed |
| 6 | `single` scored unclipped, ensembles clipped | CONFIRMED | fixed; 0.0518 → 0.0517 |
| 7 | paired deletion conditions on every arm completing | CONFIRMED (1 item, content filter) | disclosed |
| 8 | parse failures were cached | CONFIRMED (0 occurred) | fixed: parse failures are no longer cached |
| 9 | K2 wording overstated as "no panel arm beats sc6 out-of-sample" | CONFIRMED | reworded |
| 10 | "spread on 40" is range > 0.05 | CONFIRMED | reworded; any-range count added (155) |

Herding (#2/#4) remains a plausible explanation for rationale exchange hurting, not an established one.

## Implication for step 3

The panel as specified (partition + rationale exchange) is not carried into the review-session transfer
set. The one surviving arm (`rho1`, full context + one exchange round vs self-consistency) was re-tested
under a fair pre-declared design: `2026-10-13-debate-vs-sc-results.md` — outcome C, closed.

Correction to the step-1 framing: the proxy telemetry on disk is ~13.1k real request rows (09-30 → 10-13)
plus ~19.7k heartbeat rows, not a 112k-request log; turn shape came from local transcripts (13,040
requests in the same window, matching).
