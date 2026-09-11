# Design record — wiring the Δ1 break-even primitive into a live admission gate

**Status:** DECISION — Codex-cross-validated (K3 handoff guardrail #6). Codex rejected the first draft's
reasoning on 7 findings (all grounded 24/24 via codeqa); every finding was verified at ground truth
and the decision below is the corrected result. Outcome: **make no wiring change** — same action as the
draft proposed, but for corrected reasons. See "Codex reconciliation" at the end.

## TL;DR (corrected)

"Wire the break-even primitive into a live admission gate in `decide.py`/`cachegate.py`" cannot and
should not be done, for THREE independent reasons, any one of which is sufficient:
1. **Plane separation forbids the import** — `decide.py`/`cachegate.py` are hot-path; importing
   `...tuner.breakeven` fails `test_plane_separation.py` (structural `policy_provenance` invariant).
2. **There is no live enforcement path to wire INTO** — the compression pipeline is measure-only by
   doctrine: both proxy branches forward bytes verbatim; `decide()` never transforms the live wire and
   `cachegate.check` has ZERO production call sites (Codex F4/F5, verified). A "live admission gate" has
   no live wire to admit onto.
3. **The cache-bust question the primitive poses is already answered offline, more conservatively** —
   the replay optimizer rejects ANY transform that busts the cache as an absolute wall (Codex F7). The
   primitive's flat-margin "bust if saving is big enough" is strictly MORE permissive than the shipping
   "never bust" wall; wiring it would LOOSEN a safety rule, not add one.

## The request

"Wire the Δ1 `breakeven.py` primitive into a live admission gate in `decide.py` / `cachegate.py`."

## The blocking constraint (why the literal request cannot be built)

`decide.py` and `cachegate.py` are **enforcement-plane / hot-path** modules. The plane-separation
invariant (`tests/test_plane_separation.py`, the `policy_provenance` contract) forbids any hot-path
module from importing `apex_router.proxy_engine.tuner` — where `breakeven.py` lives. Two tests
enforce it structurally:

1. `test_hot_path_does_not_import_economics` — fails the moment `proxy/`, `pipeline/`, `session/`,
   or `telemetry/` imports `...tuner`.
2. `test_cachegate_is_purely_structural` — pins `cachegate.check`'s signature to exactly
   `{block_meta, ship_count, in_frozen_prefix}` and asserts no economic token (`cost`, `p_read`,
   `p_write`, `break_even`, `survival`, `retriev`) appears in its source.

Wiring `breakeven` (an economics module) into either would require **deleting or weakening these two
tests** — which is definitionally "a Δ$ gate creeping onto the request path," the exact regression
v2.1 was built to make unrepresentable. The runtime **cannot** price policy; it may only look up a
compiled rule.

## The compiler's economic gate (related, but NOT the same counterfactual — corrected)

The compiler DOES run an economic admission gate offline — but Codex F2 correctly refuted the draft's
claim that it is "the same counterfactual as `breakeven.py`, more rigorous." They price DIFFERENT
things:
- `breakeven.py` prices a **cache-BUST** (`reusable·(p_write−p_read)`): the cost of invalidating a
  cached prefix (`breakeven.py:9,64`).
- the compiler's `cell_break_even` prices **CCR-RETRIEVAL** (`Σ(Rᵢ·sᵢ)/Σ(cᵢ)`): the retrieval
  probability at which a lossy cell's total Δ$ crosses zero (`compiler.py:430`).

Both are break-even ratios in `cachesim.Pricing` units, and the compiler's is more rigorous on its OWN
question (per-block horizon R, portfolio ratio-of-sums, cross-project sign-stability). But they answer
different economic questions, so the draft's equivalence was wrong. The compiler side, for reference:

| `breakeven.py` (Δ1 primitive) | `compiler.py` (shipping gate) |
|---|---|
| `saving > cost·(1+margin)` | `cell_net_delta = Σ(Rᵢ·sᵢ) − ceiling·Σ(cᵢ) > 0` |
| single-turn `reusable·(p_write−p_read)` cost | per-block retrieval exposure at each block's **own horizon R** |
| flat `uncertainty_margin` (0.15) | `ceiling = SAFETY_MARGIN·cell_break_even`, `SAFETY_MARGIN=0.5` |
| single decision | **portfolio** break-even (ratio of sums, not mean of ratios) |
| no heterogeneity check | `cell_sign_stable_across_projects` — Δ$>0 in EVERY project subpopulation |
| fail-closed clamp (Δ1 Codex fix) | `MIN_CELL_BLOCKS`, `MIN_EFFICACY`, token-safety walls (filter-then-optimize, Δ3) |

The compiler admits a cell only if it clears this gate. But — corrected per F4 — `decide()` does NOT
then enforce it on the live wire: the proxy is measure-only (shadow computes + logs, passthrough
forwards verbatim; default is passthrough). So the counterfactual-value lesson is present as OFFLINE
cell-admission economics, but it is not "enforced live via the signed policy table" today — nothing in
the compression pipeline is live-enforcing. The three cache-bust/economic policies actually in the
repo are: (1) replay's ABSOLUTE bust-prohibition wall (`replay.py:193,216`), (2) the compiler's
CCR-retrieval admission economics (`compiler.py:430`), and (3) the un-wired `breakeven.py` bust
primitive. The draft's two-way framing missed (1).

## What `breakeven.py` uniquely provides (the real, narrow gap)

`breakeven.py` is a *simpler re-derivation* of the compiler's gate, with ONE thing the compiler does
not model: the **model-switch** counterfactual (`bust_cost` as the write-vs-read premium a switch
forfeits). The compiler prices **compression-cell** admission (transform vs raw within one model),
not **route-switch** admission (model A vs model B and the prompt-cache A would forfeit). That is a
different decision, made in a different place (the router / `route_conformance`, Δ2), and it is
exactly why Δ1's docstring marks the model-switch horizon as un-modeled future work.

## Options considered

- **A. Wire into `decide.py`/`cachegate.py` as asked.** REJECTED — structurally forbidden; requires
  deleting the plane-separation guards; duplicates a more rigorous existing gate.
- **B. Wire `breakeven` into the compiler as a second admission wall.** REJECTED — redundant with
  `cell_break_even`/`retrieval_ceiling`; a weaker single-turn model would only ever be more permissive
  or conflict with the portfolio gate; two economics gates on one decision is a maintenance hazard.
- **C. Leave `decide`/`cachegate` untouched; record that the live gate already exists in the
  compiler; keep `breakeven.py` as the un-wired primitive for the FUTURE route-switch (model A→B)
  gate, whose home is the router/Δ2 conformance plane, not the compression hot path.** PROPOSED.

## Decision (C, corrected)

1. Make NO change to `decide.py` or `cachegate.py`. Not because the gate is "already live there" (it
   isn't — F4/F5), but because (a) plane separation forbids the economics import, (b) there is no live
   compression-enforcement path to wire onto, and (c) the cache-bust concern is already handled offline
   more conservatively by replay's absolute bust-wall.
2. Keep `breakeven.py` un-wired. Its only content the other two policies lack is the single-turn
   MODEL-SWITCH bust lower bound — a ROUTE-plane question (model A→B forfeiting a prompt cache), not a
   compression-pipeline one. That plane (`route_conformance`/`route_resolve`) is itself measure-only
   and write-only today (F6: it cannot block a switch), so even there the primitive is a future seed,
   not a wire-able gate now.
3. Do NOT add `breakeven.py` as a second wall in the compiler (rejected option B): it prices a
   different, weaker counterfactual than `cell_break_even` and would only conflict with or loosen the
   existing gates.
4. This document is the audit trail. The Δ3 filter-then-optimize test already pins the compiler gate's
   ordering invariant.

## Codex reconciliation (7 findings, all verified at ground truth)

| # | Codex finding | Verdict | Effect on decision |
|---|---|---|---|
| F1 | plane-sep bans direct `tuner` imports but not all precompiled wiring | CONFIRMED (partial) | reinforces §reason-1; a precompiled value would be a DIFFERENT design, not "wire breakeven in" |
| F2 | breakeven prices cache-BUST; compiler prices CCR-RETRIEVAL — different counterfactuals | CONFIRMED | corrected the false equivalence claim |
| F3 | production `apex compile` builds a ONE-project corpus → cross-project sign-stability has no teeth in the common case | CONFIRMED | caveat noted; not load-bearing for the decision |
| F4 | the compiler gate is NOT live-enforced (proxy measure-only) | CONFIRMED | **decisive** — killed the draft's "already live, enforced by lookup" |
| F5 | `cachegate.check` has no production call site; `decide()` doesn't call it | CONFIRMED | reinforces F4 — no live path to wire onto |
| F6 | model-switch is only a lower bound; conformance is write-only (can't block) | CONFIRMED | the primitive's unique content targets a plane that also can't enforce yet |
| F7 | replay rejects EVERY transform bust as an absolute wall — a third, distinct policy | CONFIRMED | the bust question is already answered, more conservatively; wiring breakeven would loosen it |

Codex's own framing ("reject decision C as written") was adopted at the level of its FINDINGS: the
draft's *reasoning* was wrong (the "already live" and "same counterfactual" claims). The *action*
(make no wiring change) survives — now justified by measure-only doctrine + three-policy reality +
wrong-plane, not by the refuted "already live" claim. Fix authored from this analysis, not ported from
Codex.

## If a live break-even gate is genuinely wanted later (the real project)

That is not "wire in `breakeven.py`" — it is: (1) turn on an ACTIVE (non-measure-only) emission path
for the compression pipeline (a large, separate decision with its own risk gate), and/or (2) build a
ROUTE-switch gate in the router plane with a real reusable-token signal (Δ2 laid the schema; the
signal source is still absent). Both need their own design + Codex pass; neither is unblocked by
importing the current primitive onto the hot path.
