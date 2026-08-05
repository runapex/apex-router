"""Policy compiler — where all the economics live (§2).

The offline pipeline (composition + replay + cachesim + calibration-band sweep + admission
tests) is a *compiler* whose output is a static, signed `PolicyVersion` (`apex_router.proxy_engine.policy`). The
runtime does none of this: it loads the table and enforces it with pure functions (§3). Every
past Apex failure was an economic mispricing, so this is the single component all correctness
now concentrates in (§10.1) — it earns the adversarial treatment (mutation tests, Codex
cross-validation) the guard got.

What the compiler decides, per content class, priced in DOLLARS only:
  1. **efficacy** — the reduction the class's transform ACTUALLY achieves on this deployment's
     frontier blocks (never applicability — the round-2 error). Zero shrink → inadmissible.
  2. **retrieval ceiling** — the max retrieval rate at which compression still nets positive,
     the dollar break-even converted offline (§6). Derived at the SHALLOWEST band regime, where
     break-even is smallest, so a single ceiling holds across the whole band (see below).
  3. **sign-stability** — Δ$ > 0 at *every* point of [6:1, 30:1] with retrieval priced at the
     class's ceiling (§2.3.1). Proven in closed form by the ceiling derivation, then confirmed
     numerically. It is a DIRECT dollar check — never the knob-robustness proxy, which an inert
     knob can fool (the local model review, finding 4).
  4. **admission** — addressable bytes × efficacy × dollar pricing (incl. retrieval + output)
     > 0, a precondition of emission (§2.3.2).

Why one ceiling covers the band. `break_even(R) = saving(R) / retrieval_cost(R)` where
`saving = (1−f)·B·p_read·R` grows linearly in R = remaining REQUESTS (a cached prefix is re-read
once per subsequent request, never per block — unit-audited 2026-07-13) and `retrieval_cost` grows
in R with an R-independent floor (context read + write + output). So break_even INCREASES
monotonically in R (→(1−f)/2 as R→∞, →0 as R→0); its minimum over the band is at the smallest R
(the 6:1 regime). Set ceiling = SAFETY · break_even(R_min): at any deeper regime R>R_min,
break_even(R) > ceiling, so Δ$ = saving − ceiling·retrieval_cost > 0 there too. Sign-stability
across the band is therefore a property of the ceiling, not a hope.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from apex_router.proxy_engine.pipeline.transforms import astgrep, compaction, file_read_strip, terminal
from apex_router.proxy_engine.pipeline.transforms.base import Block
from apex_router.proxy_engine.policy import (
    CONTENT_CLASSES,
    ClassRule,
    ExpectedReport,
    InvalidPolicy,
    PolicyVersion,
    T2Policy,
    size_stratum_bytes,  # canonical byte binning — the same fn the runtime routes on (Δ2 revised)
    transform_digest,
)
from apex_router.proxy_engine.tuner.cachesim import CacheSimulator, Pricing
from apex_router.proxy_engine.tuner.composition import Composition, composition_hash, diagnose, session_frontiers
from apex_router.proxy_engine.tuner.replay import Request, request_context_bytes
from apex_router.proxy_engine.tuner.sensitivity import DEFAULT_BAND
from apex_router.proxy_engine.tuner.tokens import classify, has_true_tokenizer, true_token_count

# --- compiler tunables (conservative by design: early versions under-compress, §6) ------------

COMPILER_VERSION = "apex-compiler-v2-realR"  # folds into compiler_hash; bump on logic change.
#   v2: per-block real-R pricing (was band-aggregate), jackknife admission (was pooled band-sign),
#   single per-block pricing source for the signed expected report (was gross-freeze minus spend).
SAFETY_MARGIN = 0.5  # ceiling = half the shallowest break-even
MIN_EFFICACY = 0.05  # a transform must shrink ≥5% to risk retrieval
MIN_TOKEN_REDUCTION = 0.10  # OFFLINE gate: a block compresses iff it sheds ≥10%
#                                                 of its TOKENS (the economics unit; emit_decision)
MIN_CELL_BLOCKS = 3  # a cell needs ≥N compressing blocks to admit — no
#                                                 fleet-wide rule from thin evidence (§6: early
#                                                 versions under-compress; cold start admits none)
BYTE_FLOOR_MARGIN = 0.01  # runtime byte floor sits this far above the worst
#                                                 token-negative block seen (a conservative cushion)
DEFAULT_OUTPUT_TOKENS = 500  # retrieval extra-turn output (matches cachesim)
_NEVER_MATCH = 1 << 30  # a min_bytes no real block reaches (cell disabled)
STRATA = ("xs", "s", "m", "l", "xl")  # context-size bins rules are conditioned on (F1)

# Δ1 capability gating for LOSSY (ccr) cells. The compiler will not SIGN a lossy rule unless its
# transform is in `_LOSSY_CAPABILITIES` with a validator_id + a behavioral-evidence reference. This
# is the compile-time half of "a lossy cell is unrepresentable without its capabilities" (the
# runtime half is decide()'s resolver check). Empty today: json_crush is not yet in `_TRANSFORMS`
# (V1) so no lossy cell can be admitted; the behavioral gate (Δ14) populates it once evidence lands.
_LOSSY_FIDELITY = frozenset({"lossy_ccr", "ccr_retrieval"})
_LOSSY_CAPABILITIES: dict[str, dict] = {}

# class → (transform module, tool_name to present on the Block so `applies()` gates correctly).
# prose maps to the T1-P extractor (a separate M5a deliverable, not built here) → None for now,
# an honest gap: prose ships raw until that transform is registered. opaque is always raw (§2.2).
# NOTE (cross-validation): a replay `Request` carries no file_path, so `classify(text)` almost never
# returns "code" (that needs a code file extension) — real source blocks classify as prose today.
# So the code/astgrep arm is effectively dormant until the corpus schema carries a path or MIME
# hint. This is an honest current-state gap, documented rather than faked; the wiring is correct
# and lights up as soon as the classifier can see "code". Same posture as prose (no transform).
_TRANSFORMS = {
    # json → lossless compaction is the M5a baseline (on main). The lossy json_crush (M5b) is a
    # SEPARATE, additive transform admission-tested on its own (not a swap), so M5a's lossless
    # guarantees stay intact. Selecting lossy per cell is a deliberate M5b gate (fork branch).
    "json": (compaction, None),
    "terminal": (terminal, "bash"),
    "code": (astgrep, "read"),
    # file_read → lossy gutter-strip (T1-P): drops the line-number gutter (~9-10% tok on real blocks),
    # ccr_retrieval fidelity. Priceable now; stays INERT at runtime (Δ1 resolver gate) and UNSIGNED
    # until behavioral evidence (Δ14 gate) — registering it makes the cell measurable, not live.
    "file_read": (file_read_strip, "tool_result"),
    "diff": (None, None),
    "prose": (None, None),
    "opaque": (None, None),
}


def _apply(content_class: str, text: str) -> str:
    """Route a frontier block to its class transform and return the emitted text. Fail-open: if
    the transform doesn't apply or raises, the original text ships (§3 per-block fail-open). Pure
    function of (class, text)."""
    entry = _TRANSFORMS.get(content_class)
    if not entry or entry[0] is None:
        return text
    module, tool_name = entry
    block = Block(content=text, tool_name=tool_name)
    try:
        if not module.applies(block):
            return text
        return module.run(block, {}).text
    except Exception:
        return text  # fail-open: ship the original


def emit_decision(
    content_class: str,
    text: str,
    block_bytes: int,
    min_bytes: int,
    token_floor: float = MIN_TOKEN_REDUCTION,
) -> tuple[bool, str]:
    """THE single COMPILE-TIME compression decision, shared by every offline path (Codex M5a.1:
    block_econs, the freeze pipeline, and retrieval-spend must decide "does this block compress"
    IDENTICALLY, or the compiler enables a cell one path won't emit → a dollar-negative signed
    policy). Returns (compresses, emitted_text).

    Decided in the ECONOMICS unit — TOKENS: a block compresses iff its UTF-8 size clears `min_bytes`
    AND its transform reduces MEASURED tokens by ≥ `token_floor` (never a byte/char ratio, which
    re-introduces the §4 estimator bias). This is the OFFLINE gate; the runtime `decide()` uses a
    cheaper byte gate (`ClassRule.ratio_floor`, a token-SAFE byte floor compiled from these blocks)
    so the hot path needs no tokenizer. `emitted_text` is the transformed text when it compresses,
    else the original — so callers never re-derive it and can't drift.
    """
    if block_bytes < min_bytes:
        return False, text
    cand = _apply(content_class, text)
    if cand == text:
        return False, text
    orig_tok = true_token_count(text)
    token_red = 1.0 - (true_token_count(cand) / orig_tok) if orig_tok else 0.0
    if token_red >= token_floor:
        return True, cand
    return False, text


# --- per-block economics (the unit the runtime actually meets) --------------------------------


def _requests_for_regime(ratio: float) -> int:
    """Map a read:write cache ratio to R = remaining REQUESTS (the unit `saving`/`retrieval_cost`
    consume). read:write ≈ (turns−1)/2 for a linear-growth session, so turns ≈ 2·ratio+1; one turn
    is one request event, so this depth IS the remaining-requests count a frontier block is re-read
    over (matches sensitivity._corpus_for_regime). Request-denominated — never a block count."""
    return max(2, int(round(2 * ratio + 1)))


# back-compat alias (the name carried "turns"; the value was always request-denominated — see the
# 2026-07-13 unit audit). New call sites use `_requests_for_regime`.
_turns_for_regime = _requests_for_regime


@dataclass
class BlockEcon:
    """The economics of ONE real frontier block — priced on its OWN tokens and its OWN cached
    context, never an aggregate (cross-validation/F2). `compresses` is the runtime's actual emit decision:
    the block is only transformed if the rule is enabled AND it clears `min_bytes` AND it achieves
    ≥ `ratio_floor` reduction (cross-validation). Only compressing blocks earn savings and carry retrieval
    risk (cross-validation)."""

    block_tokens: int
    prefix_tokens: int  # real cached context this block sits on (the retrieval driver)
    retain: float  # token fraction kept if compressed (1.0 if shipped raw)
    orig_bytes: int
    out_bytes: int
    compresses: bool
    stratum: str  # size stratum of the owning request (for $ attribution)
    # the block's REAL amortization horizon = requests remaining in its session after it enters
    # (per-entry-position R, not a session/band aggregate). Priced by `saving`/`retrieval_cost` when
    # the caller asks for the block's own horizon (stock-vs-flow, internal review). 0 for a terminal block.
    remaining_requests: int = 0
    # source project (the reference proxy/ml/a Ruby service/…) — carried so admission can check Δ$-sign across
    # per-project subpopulations (jackknife heterogeneity robustness). None if the corpus is unlabeled.
    project: str | None = None

    def retrieval_cost(self, remaining_requests: int, pricing: Pricing) -> float:
        """$ of one CCR retrieval of this block, at its REAL cached context (prefix + block). Same
        cost model as cachesim.retrieval() so compiler and runtime tripwire agree (the local model #6)."""
        context = self.prefix_tokens + self.block_tokens
        R = max(1, remaining_requests)
        return (
            context * pricing.p_read
            + self.block_tokens * pricing.p_write
            + self.block_tokens * pricing.p_read * R
            + DEFAULT_OUTPUT_TOKENS * pricing.p_output
        )

    def saving(self, remaining_requests: int, pricing: Pricing) -> float:
        """$ compressing this block saves over R re-reads (0 if it ships raw)."""
        if not self.compresses:
            return 0.0
        return (1.0 - self.retain) * self.block_tokens * pricing.p_read * max(1, remaining_requests)

    def break_even(self, remaining_requests: int, pricing: Pricing) -> float:
        """Retrieval probability at which this block's compression breaks even, at its real
        context. Monotone increasing in R (proof in module docstring), so its band-min is at the
        shallowest regime — the property that lets one ceiling cover the band."""
        context = self.prefix_tokens + self.block_tokens
        return CacheSimulator.retrieval_break_even_prob(
            self.block_tokens,
            self.retain,
            max(1, remaining_requests),
            context_tokens=context,
            pricing=pricing,
            output_tokens=DEFAULT_OUTPUT_TOKENS,
        )


@dataclass
class Efficacy:
    """Aggregate realized shrink for a class — evidence-pack reporting only (admission prices the
    per-block distribution, not this mean)."""

    content_class: str
    orig_bytes: int
    out_bytes: int
    n_blocks: int
    n_compressing: int

    @property
    def byte_reduction(self) -> float:
        return 1.0 - (self.out_bytes / self.orig_bytes) if self.orig_bytes else 0.0


def block_econs(
    corpus: list[Request],
    content_class: str,
    *,
    min_bytes: int,
    ratio_floor: float,
    enabled: bool = True,
) -> list[BlockEcon]:
    """Per-block economics for every frontier block of `content_class`, modeling the emitted rule.

    `enabled=False` (or a class with no transform) yields all-raw blocks — the reference arm.
    Deterministic; independent of object identity.
    """
    entry = _TRANSFORMS.get(content_class)
    has_transform = bool(entry and entry[0] is not None)
    out: list[BlockEcon] = []
    for fr in session_frontiers(corpus):
        if not fr.block:
            continue
        text = fr.block.decode("utf-8", "replace")
        if classify(text) != content_class:
            continue
        # THE single compression decision — the same one the freeze pipeline and retrieval-spend
        # use (Codex M5a.1: divergent gates → enable-but-emit-raw → dollar-negative policy). Decided
        # in tokens, min_bytes vs UTF-8 bytes. `emitted` is returned so no caller re-derives it.
        block_bytes = len(fr.block)
        if enabled and has_transform:
            compresses, emitted = emit_decision(
                content_class, text, block_bytes, min_bytes, ratio_floor
            )
        else:
            compresses, emitted = False, text
        bt = true_token_count(text)
        ot = true_token_count(emitted)
        out.append(
            BlockEcon(
                block_tokens=bt,
                prefix_tokens=fr.prefix_tokens,
                retain=(ot / bt if bt else 1.0) if compresses else 1.0,
                orig_bytes=block_bytes,
                out_bytes=(len(emitted.encode("utf-8")) if compresses else block_bytes),
                compresses=compresses,
                stratum=size_stratum_bytes(request_context_bytes(fr.req)),
                remaining_requests=fr.remaining_requests,
                project=fr.req.project,
            )
        )
    return out


def measure_efficacy(
    corpus: list[Request], content_class: str, *, min_bytes: int = 1, ratio_floor: float = 0.0
) -> Efficacy:
    """Aggregate realized reduction over a class's compressing frontier blocks (efficacy, not
    applicability). Used for the evidence pack and the coarse MIN_EFFICACY gate."""
    econs = block_econs(corpus, content_class, min_bytes=min_bytes, ratio_floor=ratio_floor)
    orig_b = sum(e.orig_bytes for e in econs)
    out_b = sum(e.out_bytes for e in econs)
    return Efficacy(
        content_class,
        orig_b,
        out_b,
        n_blocks=len(econs),
        n_compressing=sum(1 for e in econs if e.compresses),
    )


def _cell_byte_reduction(econs: list[BlockEcon]) -> float:
    """Aggregate byte reduction over a set of compressing blocks (a cell's efficacy)."""
    orig = sum(e.orig_bytes for e in econs)
    out = sum(e.out_bytes for e in econs)
    return 1.0 - (out / orig) if orig else 0.0


def compile_byte_floor(
    corpus: list[Request],
    content_class: str,
    stratum: str,
    min_bytes: int,
    token_floor: float = MIN_TOKEN_REDUCTION,
) -> float:
    """Compile a token-SAFE BYTE floor for the runtime `decide()` gate (the user's chosen design).

    The offline gate is token-based (`emit_decision`), but the hot path can't run a tokenizer. So
    per cell we emit the byte reduction above which, on THIS deployment's blocks, compression was
    always token-positive: `floor = max(byte_red over token-NEGATIVE blocks) + margin`, clamped to
    `[token_floor, 1)`. A runtime block whose byte saving clears this floor is — on the compiled
    evidence — at least as token-compressible as `token_floor` demands, so the cheap byte check is
    a conservative proxy for the true token economics. If no token-negative block exists, the floor
    is just `token_floor` (bytes reduce at least as fast as tokens for these transforms).
    """
    worst_negative = 0.0
    saw_block = False
    for fr in session_frontiers(corpus):
        if not fr.block:
            continue
        text = fr.block.decode("utf-8", "replace")
        if (
            classify(text) != content_class
            or size_stratum_bytes(request_context_bytes(fr.req)) != stratum
        ):
            continue
        if len(fr.block) < min_bytes:
            continue
        cand = _apply(content_class, text)
        if cand == text:
            continue
        orig_tok = true_token_count(text)
        token_red = 1.0 - (true_token_count(cand) / orig_tok) if orig_tok else 0.0
        byte_red = 1.0 - (len(cand.encode("utf-8")) / len(fr.block)) if fr.block else 0.0
        saw_block = True
        if token_red < token_floor:  # a block the token gate REJECTS
            worst_negative = max(worst_negative, byte_red)
    if not saw_block:
        return token_floor
    return min(0.999, max(token_floor, worst_negative + BYTE_FLOOR_MARGIN))


def _byte_floor_is_token_safe(
    corpus: list[Request], content_class: str, stratum: str, min_bytes: int, byte_floor: float
) -> bool:
    """The F1 admission assertion: no corpus block in this cell that CLEARS the runtime byte floor
    may be token-negative. Because BPE is non-monotone under deletion, the byte floor's token-safety
    is a measured per-cell property, not a theorem — a cell where any floor-clearing block loses
    tokens is refused (its byte gate would emit a token-negative block at runtime). Cheap: the
    compiler already holds exact tokens."""
    for fr in session_frontiers(corpus):
        if not fr.block:
            continue
        text = fr.block.decode("utf-8", "replace")
        if (
            classify(text) != content_class
            or size_stratum_bytes(request_context_bytes(fr.req)) != stratum
        ):
            continue
        if len(fr.block) < min_bytes:
            continue
        cand = _apply(content_class, text)
        if cand == text:
            continue
        byte_red = 1.0 - (len(cand.encode("utf-8")) / len(fr.block)) if fr.block else 0.0
        if byte_red < byte_floor:  # runtime ships this raw — not our concern
            continue
        orig_tok = true_token_count(text)
        token_red = 1.0 - (true_token_count(cand) / orig_tok) if orig_tok else 0.0
        if token_red < 0:  # clears byte floor but LOSES tokens → unsafe
            return False
    return True


# --- hindsight expected-value pricing over the REAL population (stock-vs-flow, internal review) -----------
# A cell fires as ONE unit under one policy, so its economics are the SUM over its compressing blocks,
# each priced at its OWN measured horizon R = `remaining_requests` (never a band/session aggregate —
# that proxy was the bug). Per block, hindsight net at retrieval rate p is `R_i·s_i − p·c_i`; summed,
# the cell breaks even at `p*_cell = Σ(R_i·s_i) / Σ(c_i)`. This is the SAME expected-value function
# admission prices, and the ceiling is its zero (×SAFETY) — one function, self-consistent by
# construction. See decision-log "Step-2 R-wiring DIRECTIVE" + "Ceiling form".


def _priced_blocks(econs: list[BlockEcon]) -> list[BlockEcon]:
    """The blocks that carry economics in the hindsight aggregation: compressing AND with retrieval
    opportunity (R>0). A terminal R=0 block earns no amortization AND has no future request in which a
    retrieval could occur → economically INERT → it washes out of BOTH the numerator and denominator,
    instead of `min` treating it as maximally binding (the censoring bug the arc set out to kill)."""
    return [e for e in econs if e.compresses and e.remaining_requests > 0]


def priced_block_count(econs: list[BlockEcon]) -> int:
    """How many blocks actually carry economics (compressing AND R>0) — the count the cold-start
    evidence floor must gate on. Terminal R=0 blocks are inert (0 saving, 0 exposure), so padding a
    cell with them must NOT satisfy MIN_CELL_BLOCKS: a cell priced from one real block is a one-block
    bet regardless of how many terminals share its stratum."""
    return len(_priced_blocks(econs))


def cell_expected_saving(econs: list[BlockEcon], pricing: Pricing) -> float:
    """Σ over priced blocks of `saving` at each block's OWN R (Σ R_i·s_i). R=0 terminal blocks are
    inert (saving(0)=0) — they earn no amortization they never occupy."""
    return sum(e.saving(e.remaining_requests, pricing) for e in _priced_blocks(econs))


def cell_retrieval_exposure(econs: list[BlockEcon], pricing: Pricing) -> float:
    """Σ retrieval cost over priced blocks (`c_i` weighted by retrieval opportunity, R>0 only). This
    is the denominator of the cell break-even; R=0 blocks are excluded, made explicit."""
    return sum(e.retrieval_cost(e.remaining_requests, pricing) for e in _priced_blocks(econs))


def cell_break_even(econs: list[BlockEcon], pricing: Pricing) -> float:
    """`p*_cell = Σ(R_i·s_i) / Σ(c_i over R>0)` — the portfolio break-even (ratio of SUMS, NOT mean of
    per-block ratios): the single probability at which the cell's TOTAL expected Δ$ crosses zero. 0 if
    no block has retrieval exposure (nothing to price)."""
    exposure = cell_retrieval_exposure(econs, pricing)
    return cell_expected_saving(econs, pricing) / exposure if exposure else 0.0


def cell_net_delta(econs: list[BlockEcon], ceiling: float, pricing: Pricing) -> float:
    """Δ$ of a (sub)population at a GIVEN retrieval ceiling: Σ(R_i·s_i) − ceiling·Σ(c_i). The single
    expected-value function admission and the ceiling both derive from — evaluated here at a ceiling
    supplied by the caller (the POOLED cell's ceiling, for subpopulation sign checks)."""
    return cell_expected_saving(econs, pricing) - ceiling * cell_retrieval_exposure(econs, pricing)


def cell_sign_stable_across_projects(
    econs: list[BlockEcon], ceiling: float, pricing: Pricing
) -> bool:
    """Heterogeneity robustness — the NON-TAUTOLOGICAL successor to band sign-stability. The pooled
    cell is Δ$-positive at its own ceiling BY CONSTRUCTION (the ceiling is the function's zero), so a
    pooled check has no teeth. One level down it does: require Δ$ > 0 within EVERY project
    subpopulation, each priced at the POOLED cell's `ceiling` (a price the subpopulation's own blocks
    did not set). Rejects a cell whose value concentrates in one deployment while another is underwater
    — the terminal/xl failure shape in the project dimension. With a single project (or unlabeled
    corpus) there is no heterogeneity and this reduces to the pooled positivity."""
    by_project: dict[str | None, list[BlockEcon]] = {}
    for e in econs:
        if e.compresses:
            by_project.setdefault(e.project, []).append(e)
    if not by_project:
        return False
    return all(cell_net_delta(group, ceiling, pricing) > 0 for group in by_project.values())


def retrieval_ceiling(econs: list[BlockEcon], band: tuple[float, ...], pricing: Pricing) -> float:
    """Ceiling = SAFETY · p*_cell, the cell's portfolio break-even on real per-block horizons (ratio of
    sums, R=0 blocks inert). NO `min` over blocks: a policy is an expected-value instrument over a
    population under censoring — `min` prices every cell on its inevitable terminal member and refuses
    every bet (min→0). Conservatism lives in SAFETY + the tail-exposure report, not the aggregation.
    `band` is retained in the signature (compiler-hash + call-site stability) but no longer selects an
    aggregate R — R is now each block's own. 0 if no compressing block has retrieval exposure."""
    return SAFETY_MARGIN * cell_break_even(econs, pricing)


def compile_min_bytes(
    econs: list[BlockEcon], floor_min_bytes: int, band: tuple[float, ...], pricing: Pricing
) -> tuple[int, float]:
    """COMPILE min_bytes from the per-block econ distribution instead of defaulting to the
    transform's static gate (M5a.1 review F1). Sweep candidate size thresholds (the compressing
    blocks' own byte sizes, ascending); for each, keep only blocks at/above it, price the ceiling
    over THAT survivor set, and check ADMISSIBILITY. Return the SMALLEST admissible threshold with
    its ceiling; a never-match sentinel if none is admissible.

    Admissibility is the per-project JACKKNIFE (heterogeneity robustness), NOT a pooled band-sign
    check. Under real-per-block-R pricing the ceiling is the zero of the pooled Δ$ function, so a
    pooled "Δ$>0 at the ceiling" check is a tautology (net = saving·(1−SAFETY) > 0 always) — the old
    band-sign teeth were an artifact of the ceiling/Δ using DIFFERENT R (min vs aggregate). Real
    teeth require checking sign across a population the ceiling did not price: every project
    subpopulation, at the pooled cell's ceiling. A survivor set whose exposure is 0 (all-terminal)
    yields ceiling 0 and cannot be admitted — the empty/degenerate cell excludes itself here rather
    than enabling with a zero ceiling."""
    compressing = sorted((e for e in econs if e.compresses), key=lambda e: e.orig_bytes)
    if not compressing:
        return _NEVER_MATCH, 0.0
    # candidate thresholds = each compressing block's size (dropping the smallest, weakest blocks
    # one at a time), plus the transform's own floor as the lower bound.
    candidates = sorted({max(floor_min_bytes, e.orig_bytes) for e in compressing})
    for thr in candidates:
        survivors = [e for e in compressing if e.orig_bytes >= thr]
        if not survivors:
            continue
        ceiling = retrieval_ceiling(survivors, band, pricing)
        if ceiling <= 0.0:
            continue  # no retrieval exposure to price (all-terminal survivor set) — not admissible
        if cell_sign_stable_across_projects(survivors, ceiling, pricing):
            return thr, ceiling
    return _NEVER_MATCH, 0.0


@dataclass
class BandPoint:
    regime: float
    remaining_requests: int  # mean real per-block R over the priced blocks (report-only)
    saving: float  # Σ $ compression saves on compressing blocks at their OWN R
    retrieval_cost: float  # Σ $ of retrieving those blocks at their OWN R
    expected_retrieval: float  # ceiling · retrieval_cost
    net_delta: float  # saving − expected_retrieval


def band_sign_stability(
    econs: list[BlockEcon], ceiling: float, band: tuple[float, ...], pricing: Pricing
) -> list[BandPoint]:
    """REPORTING ONLY (no longer the admission gate — that's `cell_sign_stable_across_projects`, the
    per-project jackknife). Reports the cell's Δ$ decomposition priced on each block's OWN real R (the
    same expected-value function the ceiling derives from), so the number is never mixed-unit. Because
    real R does not vary by band regime, every reported point carries the SAME consistent value; the
    `regime` column is retained for report-shape stability. At the cell's own ceiling the net is the
    correctly-labeled tautology Σ(R_i·s_i)·(1−SAFETY) — a self-consistency signal, not a filter (the
    teeth live in the jackknife + materiality floor + the online transfer-gap)."""
    priced = _priced_blocks(econs)
    saving = cell_expected_saving(econs, pricing)
    retr = cell_retrieval_exposure(econs, pricing)
    expected = ceiling * retr
    mean_R = round(sum(e.remaining_requests for e in priced) / len(priced)) if priced else 0
    pts: list[BandPoint] = []
    for r in band:
        pts.append(BandPoint(r, mean_R, saving, retr, expected, saving - expected))
    return pts


# --- freeze-aware replay pipeline -------------------------------------------------------------


def _block_key(req: Request) -> tuple:
    """Value identity for a request within a corpus — session + ts + content, NOT id() (Codex
    F7: id() is reused across object lifetimes and is not value-deterministic)."""
    return (
        req.session_id,
        req.ts,
        req.content,
        req.frontier_block,
        req.context_bytes,
    )


def build_freeze_pipeline(corpus: list[Request], rules: dict[str, dict[str, ClassRule]]):
    """A PipelineFn modeling prefix-freeze + the emitted rule. It transforms only the NEWEST block
    of each turn (iff the (class × stratum) rule is enabled AND the block clears min_bytes AND
    achieves ratio_floor — cross-validation), keeps the frozen prefix byte-identical, and concatenates. On
    a DIVERGED turn (client edit / compaction) it RESETS the prefix and reports a bust, rather than
    appending divergent bytes to a stale prefix (cross-validation). Token counts anchor on the captured
    `Request.tokens`, scaled by realized byte compression (cross-validation).

    The rule is looked up by (class, stratum) where stratum is the owning request's context size —
    the same key the runtime `decide()` uses (M5a.1 review F1). Deterministic: emission depends
    only on the corpus decomposition + rules, keyed by value (cross-validation), never on object identity.
    """
    by_session: dict[str, list] = {}
    for fr in session_frontiers(corpus):
        by_session.setdefault(fr.req.session_id, []).append(fr)

    # Per request: emitted bytes, divergence flag, and PRECOMPUTED out_tokens. Computing out_tokens
    # here (tokenizing each frontier delta ONCE, accumulated) is the fix for the O(turns*prefix)
    # blowup (M5b perf): the old closure re-tokenized the whole growing prefix (MBs) every request;
    # true_token_count's content-hash memo never hit (each turn's prefix is unique). Accumulating
    # orig/emit token counts as the prefix grows makes a 3MB/2895-turn session tokenize each small
    # frontier once, not the 3MB prefix 2895 times.
    emitted_by_key: dict[tuple, tuple[bytes, bool, int]] = {}
    for _sid, seq in by_session.items():
        prefix = b""
        orig_tok = 0  # cumulative exact tokens of the ORIGINAL growing prefix
        emit_tok = 0  # cumulative exact tokens of the EMITTED growing prefix
        for fr in seq:
            diverged = fr.diverged
            if fr.block:
                text = fr.block.decode("utf-8", "replace")
                cls = classify(text)
                st = size_stratum_bytes(request_context_bytes(fr.req))
                rule = rules.get(cls, {}).get(st)
                out = text
                if rule and rule.enabled and rule.transform:
                    # the SAME token-gated decision block_econs used — the OFFLINE token floor, NOT
                    # rule.ratio_floor (which is now the runtime's compiled BYTE floor). Passing the
                    # byte floor here would re-create the char-gate/token-gate drift (Codex M5a.1:
                    # admission enables a cell the replay emits raw → dollar-negative policy.
                    _c, out = emit_decision(cls, text, len(fr.block), rule.min_bytes)
                emitted_block = out.encode("utf-8")
                block_orig_tok = true_token_count(text)  # frontier delta, tokenized once
                block_emit_tok = true_token_count(out)
                if diverged:
                    prefix, orig_tok, emit_tok = emitted_block, block_orig_tok, block_emit_tok
                else:
                    prefix = prefix + emitted_block
                    orig_tok += block_orig_tok
                    emit_tok += block_emit_tok
            elif diverged:
                prefix, orig_tok, emit_tok = b"", 0, 0
            # out_tokens: captured req.tokens scaled by the measured whole-prefix ratio (F2/F4),
            # now from the incremental cumulative counts rather than a per-request full re-tokenize.
            ratio = (emit_tok / orig_tok) if orig_tok else 1.0
            out_tokens = max(1, int(round(fr.req.tokens * ratio)))
            emitted_by_key[_block_key(fr.req)] = (prefix, diverged, out_tokens)

    def pipeline(req: Request, _knobs: dict) -> tuple[bytes, int, bool, str]:
        # out_tokens is PRECOMPUTED above (captured req.tokens scaled by the measured whole-prefix
        # ratio — F2/F4), so the closure does no per-request tokenization of the growing prefix.
        emitted, diverged, out_tokens = emitted_by_key.get(
            _block_key(req), (req.content, False, req.tokens)
        )
        return emitted, out_tokens, diverged, ("client_edit" if diverged else "none")

    return pipeline


# --- expected-Δ$ report (extensive $, by stratum) ---------------------------------------------


# NOTE: the freeze-replay pricing path (`_stratum_costs`, `_streaming_freeze_costs`) and the separate
# `_expected_retrieval_spend` were DELETED (2026-07-15). They computed the signed by_stratum as a
# gross-freeze saving minus a separately-priced retrieval spend — two machineries with incommensurable
# saving definitions that produced the terminal/l = -3513 artifact (witness six). The signed report is
# now the SINGLE per-block pricing source `net_by_stratum` (Σ admission per-block net, in compile_policy).
# A dormant second pricing path is exactly how that divergence class re-enters; if a future diagnostic
# needs a per-stratum cost view, it re-aggregates the per-block BlockEcon terms — it does NOT keep its
# own economics.


@dataclass(frozen=True)
class CorpusProvenance:
    """Provenance of the corpus a compile is signing (internal review structural closure). `canonical` is the
    load-bearing bit: True iff the corpus is the FULL sorted population (freeze_corpus / a
    build_corpus with limit_sessions=None), False for a `limit_sessions=N` truncation. The sorted
    truncation produced two wrong standing conclusions (the mis-specified ceiling table AND
    "admitted: NONE") because a small limit biases toward the first-N-by-name — usually the small
    sessions, EXCLUDING the high-turn ones where xl blocks live. `compile_policy` in evidence mode
    refuses a non-canonical provenance, so `limit_sessions` cannot feed a signed bundle: the
    frozen-snapshot standing rule (F-ii) becomes MECHANISM, not discipline."""

    canonical: bool
    n_sessions: int
    source: str  # human/debug label of how the corpus was built

    def to_dict(self) -> dict:
        return {"canonical": self.canonical, "n_sessions": self.n_sessions, "source": self.source}

    @staticmethod
    def from_stats(stats) -> CorpusProvenance:
        """Derive provenance from a CorpusStats (or any object with `.canonical`/`.n_sessions`)."""
        return CorpusProvenance(
            canonical=bool(getattr(stats, "canonical", False)),
            n_sessions=int(getattr(stats, "n_sessions", 0)),
            source=getattr(stats, "source", "build_corpus"),
        )


@dataclass
class CompileResult:
    """The compiled policy plus the evidence that justified it (§2.3.3 evidence pack)."""

    policy: PolicyVersion
    composition: Composition
    efficacy: dict[str, Efficacy]
    band_points: dict[str, list[BandPoint]]
    evidence: dict


def compile_policy(
    corpus: list[Request],
    *,
    version: int = 1,
    compiled_at: float = 0.0,
    pricing: Pricing | None = None,
    band: tuple[float, ...] = DEFAULT_BAND,
    evidence_grade: bool = False,
    corpus_provenance: CorpusProvenance | None = None,
    evidence_manifest_hash: str = "",
) -> CompileResult:
    """Compile a signed `PolicyVersion` from a deployment's replay corpus. Deterministic:
    same corpus + version + compiled_at → byte-identical policy (§3 reproducibility). `compiled_at`
    is a caller-supplied input, never `now()`.

    EVIDENCE-GRADE gate (internal review structural closure). `evidence_grade=True` marks a compile whose
    output may back a signed bundle or an evidence pack — the real signing path (`apex compile`). In
    that mode the corpus MUST carry a CANONICAL `corpus_provenance` (the frozen snapshot / full
    population); a missing or truncated (`limit_sessions=N`) provenance raises `InvalidPolicy` at
    compile time, because the sorted-filename truncation is exactly the instrument that produced two
    wrong standing conclusions (the mis-specified ceiling table and "admitted: NONE"). Probes keep
    the default `evidence_grade=False`: they compile + seal as before but the evidence pack is
    stamped `evidence_grade: False`, so a debug compile can't be mistaken for a signed bundle.
    This turns the frozen-snapshot standing rule (F-ii) from discipline into mechanism.
    """
    if evidence_grade:
        if corpus_provenance is None:
            raise InvalidPolicy(
                "evidence-grade compile requires corpus_provenance — an unlabeled corpus cannot "
                "sign a bundle or back an evidence pack (frozen-snapshot rule, F-ii)."
            )
        if not corpus_provenance.canonical:
            raise InvalidPolicy(
                f"evidence-grade compile requires a CANONICAL corpus; got "
                f"{corpus_provenance.source!r} (canonical=False, n_sessions="
                f"{corpus_provenance.n_sessions}). A `limit_sessions` truncation is a probe "
                "debug parameter — it cannot feed a signed bundle (the sorted-filename bias that "
                "produced 'admitted: NONE' and the wrong ceiling table). Freeze the canonical "
                "corpus (`freeze_corpus.py`) or build with limit_sessions=None."
            )
        if not evidence_manifest_hash:
            raise InvalidPolicy(
                "evidence-grade compile requires evidence_manifest_hash — a production policy "
                "must bind source, full corpus content, tokenizer, model ids, validators, and "
                "verified gate transcripts before it can be signed"
            )
    # A signed policy's Δ$ must come from MEASURED token ratios, never an estimator that cancels
    # to a byte-ratio (M5a.1 review §4). Refuse to compile without the exact tokenizer rather than
    # silently sign a biased target the transfer gap G would then grade against.
    if not has_true_tokenizer():
        raise RuntimeError(
            "policy compilation requires an exact tokenizer (tiktoken) — refusing to sign a "
            "policy whose Δ$ target would be built from estimated token ratios (see §4)."
        )
    pricing = pricing or Pricing()
    comp = diagnose(corpus)
    n_sessions = len({r.session_id for r in corpus}) or 1

    # rules[class][stratum] → ClassRule; compiled per (class × context-size stratum) so the
    # retrieval ceiling and min_bytes are honest for THAT context regime (M5a.1 review F1).
    rules: dict[str, dict[str, ClassRule]] = {}
    efficacies: dict[str, Efficacy] = {}
    band_pts: dict[str, list[BandPoint]] = {}
    # Cells the economics admit but that cannot be SIGNED (a lossy transform with no registered
    # capability). NOT a crash: the cell ships disabled (deny-by-default) and its economic case is
    # recorded as a standing, dollar-quantified demand for behavioral evidence. Economics proposes;
    # evidence disposes (blocked_on_evidence — the P4/P5 inversion in the type system).
    blocked_records: list[dict] = []
    # SINGLE pricing source for the signed expected report: each ENABLED cell's admission per-block
    # net Δ$ (Σ saving(R) − ceiling·retrieval_cost(R) over its survivor blocks), keyed by byte stratum.
    # by_stratum is a re-aggregation of the SAME terms admission priced — never a second machinery
    # (freeze-replay gross minus expected-spend), which mixed incommensurable saving definitions.
    net_by_stratum: dict[str, float] = {}

    for cls in CONTENT_CLASSES:
        transform_name = _TRANSFORMS[cls][0].name if _TRANSFORMS[cls][0] else None
        floor_min_bytes = _min_bytes_for(cls)
        token_floor = MIN_TOKEN_REDUCTION  # the OFFLINE gate (emit_decision); not emitted
        rules[cls] = {}

        # All frontier blocks of this class, priced per-block (cross-validation/F2/F5), then partitioned by
        # the owning request's context-size stratum — the retrieval-cost driver L.
        all_econs = (
            block_econs(corpus, cls, min_bytes=floor_min_bytes, ratio_floor=token_floor)
            if transform_name
            else []
        )
        efficacies[cls] = measure_efficacy(
            corpus, cls, min_bytes=floor_min_bytes, ratio_floor=token_floor
        )

        by_stratum_econ: dict[str, list[BlockEcon]] = {}
        for e in all_econs:
            by_stratum_econ.setdefault(e.stratum, []).append(e)

        for st in STRATA:
            cell_econs = by_stratum_econ.get(st, [])
            if transform_name and cell_econs:
                # COMPILE min_bytes for this cell: the smallest size that is Δ$-positive across the
                # band at its ceiling, so a tiny-block outlier excludes itself (F1) instead of
                # dragging the ceiling to zero.
                min_bytes, ceiling = compile_min_bytes(cell_econs, floor_min_bytes, band, pricing)
                survivors = [e for e in cell_econs if e.compresses and e.orig_bytes >= min_bytes]
                pts = band_sign_stability(survivors, ceiling, band, pricing) if survivors else []
                byte_red = _cell_byte_reduction(survivors)
                # Admission teeth = the per-project JACKKNIFE at the compiled ceiling (heterogeneity
                # robustness), NOT the now-tautological pooled band-sign. `ceiling > 0` already means
                # compile_min_bytes found an admissible threshold; re-assert the jackknife here as
                # defence-in-depth so `enabled` and `min_bytes` can never disagree.
                sign_stable = ceiling > 0.0 and cell_sign_stable_across_projects(
                    survivors, ceiling, pricing
                )
                # Admission also needs a minimum-evidence floor: enough distinct PRICED (R>0) blocks so
                # the cell isn't a fleet-wide bet on one session's luck (§6, cold-start safe). Counting
                # terminal R=0 blocks here would let filler pad a one-block bet past the floor (Codex).
                enough_evidence = priced_block_count(survivors) >= MIN_CELL_BLOCKS
                enabled = bool(
                    survivors and enough_evidence and byte_red >= MIN_EFFICACY and sign_stable
                )
                if st == "xl" or (cls == "json" and st in ("s", "m", "l", "xl")):
                    band_pts.setdefault(cls, pts)  # a representative cell for the evidence pack
                byte_floor = 0.0
                if enabled:
                    # the runtime's cheap gate: a BYTE floor compiled from this cell's blocks so
                    # `decide()` never runs a tokenizer. Its token-safety is NOT a theorem — BPE
                    # token count is non-monotone under deletion (deleting a byte can split a merge
                    # and ADD tokens; verified). It is a per-cell PROPERTY, measured here and
                    # monitored in shadow: no corpus block that clears the byte floor may be
                    # token-negative (M5a.1 review F1). A cell that can't guarantee it is refused.
                    byte_floor = compile_byte_floor(corpus, cls, st, min_bytes, token_floor)
                    if not _byte_floor_is_token_safe(corpus, cls, st, min_bytes, byte_floor):
                        enabled = False
                if not enabled:
                    # a non-admitted cell carries no live thresholds: never-match + no ceiling,
                    # so a disabled rule can't leak a stale value onto the hot path.
                    min_bytes, ceiling, byte_floor = _NEVER_MATCH, 0.0, 0.0
            else:
                min_bytes, ceiling, enabled, byte_floor = _NEVER_MATCH, 0.0, False, 0.0
            # Δ3: seal the transform digest + fidelity onto an ENABLED rule, so the runtime rejects
            # a policy whose transform code changed since compile (the digest covers the module
            # source incl. its DEFAULT_ knob constants — a default change bumps it). `knobs` is {}
            # today (transforms use their sealed-by-digest defaults); the per-cell grid search (Q4)
            # fills searched values here without a schema change.
            xf = _TRANSFORMS[cls][0] if transform_name else None
            fidelity = getattr(xf, "fidelity", "") if (enabled and xf) else ""
            # Δ1 capability gate: a LOSSY (ccr) cell is only signable if its transform has a
            # registered capability (validator + behavioral evidence). WITHOUT one, the cell is
            # `blocked_on_evidence`: it does NOT sign (deny-by-default — ships DISABLED, nothing lossy
            # fires) and its economic case is recorded as a standing, dollar-quantified demand for a
            # behavioral campaign. This is NOT a crash: the economics proposing a cell and the policy
            # being allowed to sign it are different facts; conflating them (the old raise) blocked
            # every probe-compile the moment a lossy transform priced positive. Economics proposes;
            # evidence disposes.
            validator_id, validator_version = None, ""
            if enabled and fidelity in _LOSSY_FIDELITY:
                cap = _LOSSY_CAPABILITIES.get(transform_name)
                if not cap or not cap.get("validator_id") or not cap.get("evidence"):
                    blocked_records.append(
                        {
                            "content_class": cls,
                            "stratum": st,
                            "transform": transform_name,
                            "fidelity_class": fidelity,
                            "reason": "no_registered_capability",
                            "expected_delta": round(cell_net_delta(survivors, ceiling, pricing), 4),
                            "ceiling": round(ceiling, 6),
                            "n_blocks": len(survivors),
                            "mean_remaining_requests": (
                                round(sum(e.remaining_requests for e in survivors) / len(survivors))
                                if survivors
                                else 0
                            ),
                            # PROVENANCE of expected_delta: it is priced on POSITIONAL R (n−1−index),
                            # an UPPER BOUND on real amortization. The demand is real; the DOLLAR value
                            # is not signable until repriced on measured R_eff (the survival scan) —
                            # long sessions truncate via compaction/TTL. Carried so no reader quotes a
                            # positional-R figure as a verified one (no number without its population).
                            "r_basis": "positional_upper_bound",
                        }
                    )
                    # ship raw: disable the cell exactly like any non-admitted one (never-match, no
                    # thresholds, no lossy fidelity leaks onto the sealed rule).
                    enabled, fidelity = False, ""
                    min_bytes, ceiling, byte_floor = _NEVER_MATCH, 0.0, 0.0
                else:
                    validator_id = cap["validator_id"]
                    validator_version = cap.get("validator_version", "")
            rules[cls][st] = ClassRule(
                transform=transform_name,
                enabled=enabled,
                min_bytes=min_bytes,
                ratio_floor=byte_floor,
                retrieval_ceiling=ceiling,
                knobs={},
                transform_version=transform_digest(transform_name)
                if (enabled and transform_name)
                else "",
                validator_id=validator_id,
                validator_version=validator_version,
                fidelity_class=fidelity,
            )
            # SINGLE pricing source: an ENABLED cell contributes its admission per-block net Δ$ to the
            # signed by_stratum — the exact `Σ saving(R) − ceiling·retrieval_cost(R)` over its survivor
            # blocks that the jackknife admitted. A disabled/blocked cell contributes 0 (ships raw). So
            # the signed expected can never disagree with admission (was: a freeze-replay gross minus a
            # separate expected-spend, three incommensurable saving definitions → the -3513 residual).
            if enabled:
                net_by_stratum[st] = net_by_stratum.get(st, 0.0) + cell_net_delta(
                    survivors, ceiling, pricing
                )
        band_pts.setdefault(cls, [])

    # Expected-Δ$ report — the SINGLE pricing source. `by_stratum` is the admission per-block net,
    # re-aggregated by byte stratum: Σ over each enabled cell's survivor blocks of
    # `saving(R) − ceiling·retrieval_cost(R)`, priced at each block's OWN real R. This is the EXACT
    # quantity the jackknife admitted on, so the signed expected can never disagree with admission
    # (the -3513 residual came from a SECOND machinery — freeze-replay gross saving minus a separately
    # computed expected retrieval spend — whose three incommensurable saving definitions and (until
    # witness six) mismatched strata produced a number no admitted cell owned). Retrieval is priced,
    # never a free safety valve (§6): it is the `ceiling·retrieval_cost` term already inside each net.
    by_stratum = dict(net_by_stratum)
    total_delta = sum(by_stratum.values())
    delta_per_session = total_delta / n_sessions
    enabled_cells = {
        (c, st) for c, strata in rules.items() for st, r in strata.items() if r.enabled
    }

    t2 = T2Policy(
        consolidate_on=("ttl", "client_edit", "operator"), min_turn_count=_survival_p25(corpus)
    )

    expected = ExpectedReport(
        delta_dollars_per_session=delta_per_session,
        by_stratum={st: v / n_sessions for st, v in sorted(by_stratum.items())},
    )

    corpus_h = composition_hash(comp)
    policy = PolicyVersion(
        version=version,
        compiled_at=compiled_at,
        compiler_hash=_compiler_hash(pricing, band),
        corpus_hash=corpus_h,
        band=(float(band[0]), float(band[-1])),
        rules=rules,
        t2=t2,
        expected=expected,
        evidence_manifest_hash=evidence_manifest_hash,
    ).sealed()

    evidence = {
        "composition": comp.snapshot(),
        "n_sessions": n_sessions,
        # Provenance stamp (internal review structural closure): every evidence pack records whether it is
        # evidence-grade and the corpus it was compiled from, so a probe pack can never be quoted as
        # a signed one and a truncated corpus can never masquerade as canonical.
        "evidence_grade": evidence_grade,
        "corpus_provenance": corpus_provenance.to_dict() if corpus_provenance else None,
        "enabled_cells": sorted(f"{c}/{st}" for c, st in enabled_cells),
        "efficacy": {
            c: {
                "byte_reduction": round(e.byte_reduction, 4),
                "n_blocks": e.n_blocks,
                "n_compressing": e.n_compressing,
            }
            for c, e in efficacies.items()
        },
        # per-cell compiled thresholds — the F1-relevant view
        "cells": {
            f"{c}/{st}": {
                "enabled": r.enabled,
                "min_bytes": r.min_bytes,
                "ceiling": round(r.retrieval_ceiling, 4),
            }
            for c, strata in rules.items()
            for st, r in strata.items()
            if r.enabled
        },
        "sign_stability": {
            c: [{"regime": p.regime, "net_delta": round(p.net_delta, 2)} for p in band_pts[c]]
            for c in CONTENT_CLASSES
            if band_pts.get(c)
        },
        # cells the economics admit but that cannot sign without behavioral evidence — a standing,
        # dollar-quantified demand for a capability campaign (deterministic order for reproducibility).
        "blocked_on_evidence": sorted(
            blocked_records, key=lambda b: (b["content_class"], b["stratum"])
        ),
        "expected_delta_per_session": round(delta_per_session, 4),
        "expected_by_stratum": {st: round(v, 4) for st, v in expected.by_stratum.items()},
    }
    return CompileResult(
        policy=policy,
        composition=comp,
        efficacy=efficacies,
        band_points=band_pts,
        evidence=evidence,
    )


# --- small compiled inputs --------------------------------------------------------------------


def _min_bytes_for(content_class: str) -> int:
    """The transform's own min-char gate, lifted into the policy as a step function (§2.2)."""
    if content_class == "json":
        return compaction.MIN_CHARS
    if content_class == "code":
        return astgrep.DEFAULT_MIN_CHARS
    if content_class == "terminal":
        return 1  # terminal normalization is worth it on any real block
    if content_class == "file_read":
        return 1  # gutter-strip efficacy is measured per block; capability gate controls signing
    return 1 << 30  # prose/opaque: effectively never (no transform yet)


def _survival_p25(corpus: list[Request]) -> int:
    """25th-percentile session length (turn count) — the compiled T2 `min_turn_count` (§2.1).
    Consolidation waits until a session is at least this deep so it doesn't fire on the short
    sessions that dominate the head of the survival curve."""
    lengths: dict[str, int] = {}
    for r in corpus:
        lengths[r.session_id] = lengths.get(r.session_id, 0) + 1
    counts = sorted(lengths.values())
    if not counts:
        return 1
    # nearest-rank P25
    idx = max(0, (len(counts) - 1) // 4)
    return max(1, counts[idx])


def _compiler_hash(pricing: Pricing, band: tuple[float, ...]) -> str:
    """Identifies the compiler logic + pricing + band that produced a policy. A pricing or band
    change is a recompile with a new hash (§3: provider price change → recompile, not drift)."""
    import json

    from apex_router.proxy_engine.policy import CLASSIFIER_VERSION

    payload = {
        "compiler": COMPILER_VERSION,
        # the classifier keys the rule table — a taxonomy change makes policies incomparable (F3).
        "classifier": CLASSIFIER_VERSION,
        "safety_margin": SAFETY_MARGIN,
        "min_efficacy": MIN_EFFICACY,
        "min_token_reduction": MIN_TOKEN_REDUCTION,
        "byte_floor_margin": BYTE_FLOOR_MARGIN,
        "pricing": {
            "p_write": pricing.p_write,
            "p_read": pricing.p_read,
            "p_base": pricing.p_base,
            "p_output": pricing.p_output,
            "ttl_s": pricing.ttl_s,
            "min_cacheable_tokens": pricing.min_cacheable_tokens,
        },
        "band": list(band),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]
