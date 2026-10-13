# Panel step 2 — pre-declaration (written before any panel arm was run)

**Status:** pre-declared 2026-10-13. Only the `single` arm had been run (40-item feasibility pilot, below), plus a 6-item all-arm smoke test of the plumbing. Results: `2026-10-13-panel-hotpot-results.md`.

## Why this is not the InfoDelphi reproduction

InfoDelphi (arXiv 2607.01661) ran on POLYGYM. Its evidence corpus (Tavily snippets) is explicitly
"not independently redistributed", no code link exists, and its own case studies show post-event evidence
(a resolved Polymarket page; the in-window ETH high). So the paper's numbers (0.206 / 0.178 / 0.193 Brier)
cannot be reproduced here. This run is an **engine demo** on a public fixed-evidence set; it tests the
same mechanism, not the same numbers.

## Step 1 context

The proxy-arm framing is dropped: panel-candidate turns are 0.5% of requests (strict, since 09-30) and
~90% of candidates are `codex_exec` review sessions. The transfer set (step 3), if any, is those sessions.

## Benchmark

HotpotQA distractor dev, 240 binary items (seed 7), 10 fixed paragraphs each, 2 gold:
- 120 `cmp`: native yes/no comparison questions, 60 yes / 60 no.
- 120 `brg`: bridge question + proposed answer; 60 gold answers, 60 hard negatives (title of a non-gold
  BM25-distractor paragraph, or another year from the paragraphs).
Split promo/confirm by sha256(id) parity; 4 capture windows by hash (synthetic: the gate's replication
layer is exercised mechanically, not as real fresh-data replication).

Known limits: HotpotQA is likely partly memorized; the prompt says "only the documents", which does not
prevent prior knowledge. That affects all arms equally (paired design), but compresses headroom.

## Arms (same model `it-entra-gpt-5.6-luna`, reasoning effort `low`, no temperature control — unsupported)

| arm | calls/item | what it isolates |
|---|---|---|
| `single` | 1 | incumbent baseline |
| `sc6` | 6 | self-consistency, call-matched to a J=3 R=2 panel (Smit control) |
| `rho1` | 6 | J=3 R=2, all evidence shared, rationale exchange (asymmetry removed) |
| `asym_r1` / `asym_rat` | 3 / 6 | rho=0.5 BM25 routing (InfoDelphi-faithful), round 1 only / + rationale exchange |
| `rand_r1` / `rand_rat` / `rand_ev` | 3 / 6 / 6 | rho=0.5 random routing; + rationale exchange; + **evidence** exchange |

`rand_*` was added after a pre-run diagnostic, not after seeing any panel result: on this set BM25 puts the
gold paragraphs in the public pool (73/480 gold paragraphs private, 2/240 items with the two gold
paragraphs on different agents), so BM25 routing creates almost no asymmetry on the evidence that matters.
Random routing: 250/480 private, 39/240 split across agents. The evidence-exchange arm therefore runs on
random routing, where there is private evidence to exchange. Evidence exchange passes each peer's private
paragraphs truncated to the character length of that peer's own round-1 rationale, plus its p.

Primary aggregator: uniform logit pool (p clipped to [0.01, 0.99]). Extremity (CW) weighting is reported
secondary only; it is the paper's choice and the plan rejected it for agent traffic.

## Pre-declared tests

Brier, paired per item. Six contrasts, paired bootstrap CI (4000 resamples), two-sided p, BH at 0.05:
`asym_rat` vs `sc6`, `rand_rat` vs `sc6`, `asym_rat` vs `rho1`, `rand_rat` vs `rho1`, `rand_ev` vs `rand_rat`,
`asym_rat` vs `single`.

Gate (`gate.run_gate`, k=30, m_windows=2, objective provenance, cost = tokens/item): two cells, incumbent
`sc6` and incumbent `single`; candidates = every other arm; winner picked on promo, confirmed on held-out.

## Kill criteria

- **K2** panel does not beat self-consistency: fires unless the `vs-sc6` gate cell promotes a panel arm.
- **K3** evidence vs rationale: CI of `rand_ev` − `rand_rat` inside ±0.01 Brier → indistinguishable, build
  the simpler evidence arm; CI excluding 0 → that side wins; otherwise "undecided" (reported, not a pass).
- **K4** cost: fires if `asym_rat` tokens/item > 2× `single` AND its Brier gain over `single` < 0.01.

## Feasibility pilot (single arm only, 40 items)

Brier 0.116, accuracy 87.5% (cmp 0.084, brg 0.148), 1,512 tokens/item, 0 parse failures. Not at ceiling.
