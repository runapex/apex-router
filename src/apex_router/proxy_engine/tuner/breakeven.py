"""Break-even / minimum-value gate primitive — the counterfactual an optimization must clear.

Motivated by py-kvcache (Kanichai/De Matteis/Trivedi, 2026-09): *a cache hit is not a win.* External
KV reuse only pays when the reusable state can be placed on the request's critical path FASTER than
the GPU can recompute the prefix. Their admission gate is a counterfactual:

    T_load(tier, bytes, queue state)  <  T_recompute(model, GPU, prefix)

Apex is a measure-only proxy in front of provider APIs, so it does NOT do external NVMe KV caching —
the PROVIDER owns the KV cache. The *analogous* apex lever (the digest's own words) is:
**log and optimize saved recomputation, not hits.** The apex counterfactual is a CACHE-PRESERVATION
decision priced in the token-cost units apex actually has (`cachesim.Pricing`): an optimization that
BUSTS the provider's prompt cache (a re-render of a frozen prefix, a compression transform inside a
cached span) is only worth doing when its predicted saving EXCEEDS the cost of the bust it causes,
plus an uncertainty margin. SCOPE: `bust_cost_tokens` prices a ONE-TIME bust exactly and is only a
LOWER BOUND for a model switch (whose lost read-discount recurs over the rest of the session — a
multi-turn horizon this primitive deliberately does not model; see `bust_cost_tokens`). A
horizon-aware model-switch gate is future, must-be-calibrated work, not this primitive.

This module is the PRIMITIVE ONLY. It is deliberately NOT wired into `decide.py` or `cachegate.py`.
Per the K3 handoff guardrail #6 ("Cross-validate the DESIGN with Codex BEFORE building the gate"), the
live admission gate that consumes this must pass a Codex cross-validation pass before it can gate any
wire. Until then this is a tested, un-wired calibration surface: offline analysis can score the
counterfactual on the (shadow `bytes_by_class` + provider `usage.cache_read_tokens`) pairs already in
telemetry, and report whether a proposed optimization would have cleared its break-even.

Honesty line (matches cachesim): this prices COST/SAVING under counterfactual invariance. It cannot
score fidelity or behavior — those stay with the tripwires + the behavioral gate. And every rate here
is PER-NODE, PER-MODEL calibratable (py-kvcache §10.1: their thresholds were measured on 3B/4B
single-GPU FP16 — never port a number across hardware without re-measuring).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apex_router.proxy_engine.tuner.cachesim import Pricing


@dataclass(frozen=True)
class BreakEven:
    """The minimum-value gate model. Config, not code — every field is calibratable per node/model.

    `pricing` supplies the token-cost multipliers (read discount, write premium, base) apex already
    uses in cachesim. `uncertainty_margin` is the fraction by which a predicted saving must BEAT the
    predicted cost before an optimization is admitted (py-kvcache: "predicted savings must exceed
    transfer, queueing and uncertainty margins"). 0.15 = require a 15% cushion; it is a DEFAULT to
    calibrate, not a fitted constant.

    FAIL-CLOSED INVARIANT: a bad calibration value must only make the gate STRICTER, never fail-open.
    `__post_init__` clamps `uncertainty_margin` to >= 0 — a negative margin (e.g. a mis-entered -1.0)
    would zero the threshold and admit every optimization, silently defeating the gate. Clamped, the
    worst a bad margin can do is require a 0% cushion (the un-margined break-even), still fail-closed.
    """

    pricing: Pricing = field(default_factory=Pricing)
    uncertainty_margin: float = 0.15

    def __post_init__(self) -> None:
        # frozen dataclass — clamp via object.__setattr__. A negative margin can only ever weaken the
        # gate, so floor it at 0 (fail-closed): the gate may be stricter than configured, never looser.
        if self.uncertainty_margin < 0.0:
            object.__setattr__(self, "uncertainty_margin", 0.0)

    def bust_cost_tokens(self, reusable_tokens: int) -> float:
        """Cost (in priced token-units) of BUSTING a reusable cached prefix of `reusable_tokens`.

        SCOPE (one-time bust only): this prices the IMMEDIATE, single-turn delta of re-establishing a
        prefix that would otherwise have been re-read — the write-vs-read premium on the reusable span:
        `reusable_tokens × (p_write − p_read)`. It is EXACT for a re-render / recache bust where the same
        prefix is written once and then re-read normally on following turns (the premium is paid once).
        It is a LOWER BOUND, not the full cost, for a MODEL SWITCH: switching models abandons the old
        provider's cache for the remainder of the session, so the lost read-discount recurs over the
        remaining turns — a multi-turn horizon this single-turn primitive deliberately does NOT model
        (cachesim.py prices that recurrence as `tokens × p_read × remaining_requests`; a horizon-aware
        switch gate is future work, and must be calibrated, not assumed). Callers pricing a model switch
        must treat this as a floor. Fail-closed: cost is floored at 0 (a mis-calibrated `p_write < p_read`
        can only make the gate stricter, never negative-cost fail-open). Zero/negative reusable → 0.
        """
        if reusable_tokens <= 0:
            return 0.0
        p = self.pricing
        return max(0.0, reusable_tokens * (p.p_write - p.p_read))

    def saving_tokens(self, predicted_saved_tokens: int) -> float:
        """Priced value of an optimization's predicted token saving (fresh/base input avoided)."""
        if predicted_saved_tokens <= 0:
            return 0.0
        return predicted_saved_tokens * self.pricing.p_base

    def admit(
        self,
        *,
        predicted_saved_tokens: int,
        reusable_tokens: int,
        busts_cache: bool,
    ) -> Decision:
        """Should this optimization proceed? Fail-CLOSED under the margin (cachegate doctrine:
        the default answer to 'should I?' is NO unless it provably clears the counterfactual).

        `predicted_saved_tokens` — what the optimization is predicted to save (e.g. compression
            bytes converted to tokens by R1, or a cheaper model's input-cost delta).
        `reusable_tokens` — the cached prefix the optimization would put at risk (0 if none).
        `busts_cache` — whether this optimization actually invalidates the cached prefix. An
            optimization that saves tokens WITHOUT busting the cache (`busts_cache=False`) has no
            cache cost and is admitted whenever it saves anything positive.
        """
        saving = self.saving_tokens(predicted_saved_tokens)
        cost = self.bust_cost_tokens(reusable_tokens) if busts_cache else 0.0
        # Require the saving to beat the cost by the uncertainty margin. When cost is 0 (no bust),
        # any positive saving clears it; when cost > 0, saving must exceed cost × (1 + margin).
        threshold = cost * (1.0 + self.uncertainty_margin)
        admit = saving > threshold if cost > 0.0 else saving > 0.0
        return Decision(
            admit=admit,
            predicted_saving=saving,
            bust_cost=cost,
            margin=self.uncertainty_margin,
            net=saving - cost,
            reason=(
                "admit" if admit
                else ("below_margin" if cost > 0.0 else "no_saving")
            ),
        )


@dataclass(frozen=True)
class Decision:
    """The break-even verdict for one candidate optimization. All token-denominated (priced units)."""

    admit: bool
    predicted_saving: float
    bust_cost: float
    margin: float
    net: float              # predicted_saving − bust_cost (the counterfactual delta)
    reason: str             # admit | below_margin | no_saving

    def to_dict(self) -> dict:
        return {
            "admit": self.admit,
            "predicted_saving": self.predicted_saving,
            "bust_cost": self.bust_cost,
            "margin": self.margin,
            "net": self.net,
            "reason": self.reason,
        }
