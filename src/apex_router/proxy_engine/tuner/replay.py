"""Replay optimizer — the offline objective. §8.1.

`score(corpus, knob_vector) -> blended_cost`: push every captured session through the PURE
pipeline (transforms + freeze semantics + the cache simulator) and price the result. Search a
coarse grid over the byte-affecting knobs, scored per (model_family × stratum) cell and
volume-weighted (xl dominates, P0.1). Output a PROPOSAL RECORD — nothing is applied (v1 is
observe-only, §1). This is the objective half of the tuner; the constraint half is the live
tripwires (§8.3, M7).

Honesty (round 2): this prices COST under counterfactual invariance — it says what a knob
vector WOULD have cost on the corpus. It cannot score fidelity/behavior; those stay with the
tripwires and the §9 gate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from apex_router.proxy_engine.tuner.cachesim import CacheSimulator, Pricing
from apex_router.proxy_engine.tuner.stratify import Stratum, size_stratum


@dataclass(frozen=True)
class Request:
    """One replayable request: the session it belongs to, its full content bytes, token count,
    timestamp, and model. `content` is what the client sent (pre-transform); the pipeline applies
    the knob vector to produce the emitted bytes the cache sim prices.

    `message_boundaries` (Δ9): the cumulative UTF-8 byte offsets at which each wire message ENDS in
    `content` (so `content[b[i-1]:b[i]]` is message i). When present, `session_frontiers` extracts
    the frontier as the WHOLE messages appended since the previous turn (never slicing mid-message
    on an edited/reordered history); when None, it falls back to byte subtraction. Kept out of value
    identity the freeze pipeline uses (which keys on session_id+ts+content), so adding it does not
    change existing dedup/keying — it's an extraction hint, not a new identity component."""

    session_id: str
    content: bytes
    tokens: int
    ts: float
    model: str
    message_boundaries: tuple[int, ...] | None = None
    # Evidence-slice labels (corpus v2, lesson #9): the session's measured REGIME (single/shallow/
    # conversational, band-tied — see fixtures.build_replay_corpus.session_regime) and its PROJECT.
    # Both ride at the row so admission evidence can be conditioned per regime/project without
    # re-deriving from raw transcripts. Kept OUT of value identity (like message_boundaries) — a pure
    # label, so adding it changes no existing dedup/keying (session_id+ts+content is unchanged).
    regime: str = "unknown"
    project: str | None = None
    # Streaming-compiler representation. When `frontier_block` is present, `content` need not hold
    # the full growing request prefix: the compiler consumes this already-decomposed frontier plus
    # the request's context byte length and previous-prefix token count. This turns a long session
    # from O(sum of growing prefixes) memory into O(final transcript bytes).
    frontier_block: bytes | None = None
    context_bytes: int | None = None
    prefix_tokens_hint: int | None = None
    diverged_hint: bool = False


def request_context_bytes(req: Request) -> int:
    """Canonical request-context byte length for batch and compact frontier corpora."""
    return int(req.context_bytes) if req.context_bytes is not None else len(req.content)


# A pipeline function: (request, knobs) → (emitted_bytes, emitted_tokens, diverged, cause).
# Injected so replay stays decoupled from the concrete transform wiring (and testable).
PipelineFn = Callable[[Request, dict], tuple[bytes, int, bool, str]]


@dataclass
class CellScore:
    n: int = 0
    cost: float = 0.0
    read: int = 0
    write: int = 0
    orig_tokens: int = 0
    out_tokens: int = 0
    transform_busts: int = 0


@dataclass
class ScoreResult:
    blended_cost: float
    total_cost: float
    per_cell: dict[tuple[str, Stratum], CellScore] = field(default_factory=dict)
    transform_busts: int = 0

    def reduction_pct(self, stratum: Stratum | None = None) -> float:
        """Token reduction % over the whole corpus or one stratum."""
        orig = out = 0
        for (_fam, strat), c in self.per_cell.items():
            if stratum is None or strat == stratum:
                orig += c.orig_tokens
                out += c.out_tokens
        return 100.0 * (orig - out) / orig if orig else 0.0


# xl dominates real traffic (P0.1: 57.6% of volume). The blended score is a VOLUME-WEIGHTED MEAN
# of each stratum's per-request MEAN cost (a cost RATE, not a total): weight_s * mean_cost_s,
# renormalized over observed strata. This makes the objective a fair cost-per-request that a knob
# helping small contexts but hurting xl scores as the net loss it is — without letting the raw xl
# token volume swamp the comparison. It is a relative objective for RANKING knob vectors on one
# corpus, not an absolute $ figure (that's total_cost). (Codex #5: it is a mean-of-means by
# design; the weights are volume shares, applied to per-request means.)
_STRATUM_WEIGHT = {"xs": 0.013, "s": 0.013, "m": 0.141, "l": 0.257, "xl": 0.576}


def score(
    corpus: list[Request], knobs: dict, pipeline: PipelineFn, pricing: Pricing | None = None
) -> ScoreResult:
    """Deterministic blended cost of `knobs` over `corpus`. Same corpus + knobs → same score."""
    sim = CacheSimulator(pricing)
    per_cell: dict[tuple[str, Stratum], CellScore] = {}
    for req in corpus:
        emitted, out_tokens, diverged, cause = pipeline(req, knobs)
        res = sim.request(
            req.session_id,
            emitted,
            out_tokens,
            req.ts,
            prev_cached_diverged=diverged,
            diverge_cause=cause,
        )
        fam = _family(req.model)
        key = (fam, size_stratum(req.tokens))
        c = per_cell.setdefault(key, CellScore())
        c.n += 1
        c.cost += res.cost
        c.read += res.cache_read_tokens
        c.write += res.cache_write_tokens
        c.orig_tokens += req.tokens
        c.out_tokens += out_tokens
        if res.bust_cause == "transform":
            c.transform_busts += 1

    total_cost = sum(c.cost for c in per_cell.values())
    # blended cost = volume-weighted mean of per-stratum mean cost (xl-dominant)
    blended = 0.0
    wsum = 0.0
    strat_cost: dict[str, tuple[float, int]] = {}
    for (_fam, strat), c in per_cell.items():
        prev = strat_cost.get(strat, (0.0, 0))
        strat_cost[strat] = (prev[0] + c.cost, prev[1] + c.n)
    for strat, (cost, n) in strat_cost.items():
        w = _STRATUM_WEIGHT.get(strat, 0.0)
        if n:
            blended += w * (cost / n)
            wsum += w
    blended = blended / wsum if wsum else 0.0
    return ScoreResult(
        blended_cost=blended,
        total_cost=total_cost,
        per_cell=per_cell,
        transform_busts=sum(c.transform_busts for c in per_cell.values()),
    )


def _family(model: str) -> str:
    from apex_router.proxy_engine.tuner.stratify import model_family

    return model_family(model)


@dataclass
class Proposal:
    """A tuner proposal — the audit-log record. NOTHING applies it (observe-only, v1)."""

    knob_vector: dict
    blended_cost: float
    baseline_cost: float
    expected_delta_pct: float  # negative = cheaper than baseline (a win)
    per_stratum_reduction: dict[str, float]
    transform_busts: int  # a proposal with ANY transform bust is inadmissible
    admissible: bool = True  # False when NO zero-bust vector exists (even baseline busts)
    # stratum-leverage diagnostic (internal review round-2): the spread of the objective across the knob
    # grid, per stratum. spread≈0 → NO KNOB HAS LEVERAGE here and the objective is uninformative
    # for that stratum — so a "no regression" result there is inertness, not validation. Prevents
    # citing the tuner for xl safety when it has no xl leverage.
    stratum_leverage: dict[str, float] = field(default_factory=dict)

    def uninformative_strata(self, eps: float = 1e-9) -> list[str]:
        """Strata where the knob grid produced ~no objective spread → tuner has no leverage."""
        return [s for s, spread in self.stratum_leverage.items() if spread <= eps]


def grid_search(
    corpus: list[Request],
    byte_knobs: dict[str, tuple[float, float, float]],
    pipeline: PipelineFn,
    baseline_knobs: dict,
    pricing: Pricing | None = None,
) -> Proposal:
    """Coarse grid over byte-affecting knobs: each knob at {min, default, max}, evaluate the
    product, keep the cheapest cell that introduces ZERO transform busts (cache-safety is a wall,
    not a weight — a knob vector that busts the cache is inadmissible no matter how cheap).

    `byte_knobs`: name → (min, default, max). `baseline_knobs`: the current epoch's vector (the
    A/B reference). Returns a Proposal; the caller writes it to the audit log and applies nothing.
    """
    baseline = score(corpus, baseline_knobs, pipeline, pricing)

    # candidate grid: cartesian product of {min, default, max} per knob
    import itertools

    names = list(byte_knobs)
    axes = [[byte_knobs[n][0], byte_knobs[n][1], byte_knobs[n][2]] for n in names]
    best: ScoreResult | None = None
    best_vec = baseline_knobs
    # per-stratum cost of every admissible grid point, to compute leverage (spread) afterward
    strata = ("xs", "s", "m", "l", "xl")
    stratum_costs: dict[str, list[float]] = {s: [] for s in strata}
    for combo in itertools.product(*axes):
        vec = dict(baseline_knobs)
        vec.update(dict(zip(names, combo, strict=True)))
        s = score(corpus, vec, pipeline, pricing)
        if s.transform_busts > 0:
            continue  # inadmissible — busts the cache
        for strat in strata:
            cost = sum(c.cost for (_f, st), c in s.per_cell.items() if st == strat)
            if any(st == strat for (_f, st) in s.per_cell):
                stratum_costs[strat].append(cost)
        if best is None or s.blended_cost < best.blended_cost:
            best, best_vec = s, vec

    # leverage per stratum = spread of admissible objective values (max-min). ~0 → no knob moves
    # this stratum's cost → the objective is uninformative there (internal review round-2 diagnostic).
    leverage = {s: (max(v) - min(v)) if len(v) >= 2 else 0.0 for s, v in stratum_costs.items()}

    if best is not None:
        # a genuinely admissible (zero-bust) proposal was found
        chosen, chosen_vec, admissible = best, best_vec, True
    else:
        # NO admissible candidate — even the baseline busts. Do NOT silently return it as a
        # clean proposal (Codex #6). Surface baseline with its real (nonzero) bust count so the
        # caller sees it is inadmissible and applies nothing.
        chosen, chosen_vec, admissible = baseline, baseline_knobs, False

    delta = (
        100.0 * (chosen.blended_cost - baseline.blended_cost) / baseline.blended_cost
        if baseline.blended_cost
        else 0.0
    )
    return Proposal(
        knob_vector=chosen_vec,
        blended_cost=chosen.blended_cost,
        baseline_cost=baseline.blended_cost,
        expected_delta_pct=delta,
        per_stratum_reduction={
            s: chosen.reduction_pct(s)  # type: ignore[arg-type]
            for s in ("xs", "s", "m", "l", "xl")
        },
        transform_busts=chosen.transform_busts,
        admissible=admissible,
        stratum_leverage=leverage,
    )
