"""Enforcement plane — the hot path. §3.

The dumb runtime: given a compiled, signed `PolicyVersion` (loaded once at session epoch through
the operator gate), `decide()` does nothing but table lookup + a pure transform + structural floors
+ freeze. No economics, no estimation, no tokenizer — all of that happened offline in the compiler
(`apex_router.proxy_engine.tuner.compiler`), which is why THIS module may not import `apex_router.proxy_engine.tuner` (plane separation,
`tests/test_plane_separation.py`). The whole behavioural surface is enumerable from the table.

Mapping to the §3 pseudocode:

    def decide(block, session):
        if frozen(block):            return frozen_bytes(block)      # freeze wins, once per block
        rule = policy[route(block)]                                   # (class × stratum) lookup
        if rule.enabled and len(block) >= rule.min_bytes:
            out = rule.transform.apply(block)                        # pure fn of block bytes
            if floors_pass(...) and saved_fraction(block, out) >= rule.ratio_floor:
                return emit_and_freeze(out)
        return emit_and_freeze(RAW)                                  # per-block fail-open

`saved_fraction` is a **byte** fraction here on purpose: `rule.ratio_floor` is the compiled byte
floor, so the hot path stays byte-only and tokenizer-free. Its token-safety is **not a theorem** —
BPE token count is NON-monotone under deletion (deleting a byte can break a merge and fragment the
remainder into MORE tokens; verified on cl100k). It is a per-cell property established by
MEASUREMENT: the compiler admits a cell only after asserting that no corpus block clearing the byte
floor is token-negative (`_byte_floor_is_token_safe`), and shadow mode async-token-checks real
traffic off the hot path to alarm on any wild `token_red < 0`. Byte floor is token-safe per-cell by
measurement, monitored in shadow — not by theorem. `route` needs the block's content class and the
CURRENT context size (the stratum) — both cheap.
"""

from __future__ import annotations

from dataclasses import dataclass

from apex_router.proxy_engine.pipeline.transforms import astgrep, compaction, file_read_strip, json_crush, terminal
from apex_router.proxy_engine.pipeline.transforms.base import Block
from apex_router.proxy_engine.policy import (
    PolicyVersion,
    classify,
    size_stratum_bytes,  # the canonical plane-neutral size-binning contract (Δ2 revised)
)

# Runtime transform registry: name → module. Mirrors the compiler's `_TRANSFORMS` but lives on the
# hot-path side (no apex_router.proxy_engine.tuner import). A rule naming a transform not here fails open (ships raw). A
# test pins that this covers every transform the compiler can emit (extra runtime entries are fine).
_BY_NAME = {
    "compaction": compaction,
    "terminal": terminal,
    "astgrep": astgrep,
    "json_crush": json_crush,
    "file_read_strip": file_read_strip,
}

# Δ1 capability gate: a LOSSY (ccr-retrieval) cell drops bytes from the wire, so it is only safe to
# emit if a resolver can serve the elided original back on retrieval. `_RESOLVERS` maps transform
# name → a registered resolver; until the CCR store (Δ12) registers one, a lossy rule is INERT —
# decide() ships raw with reason `capability_missing`. Lossless/self-contained cells need no
# resolver (nothing was dropped). The compiler independently refuses to SIGN a lossy rule without
# capabilities (`_LOSSY_CAPABILITIES`), so a lossy cell is unrepresentable AND unreachable w/o them.
_RESOLVERS: dict[str, object] = {}

# The fidelity classes that drop bytes from the wire and therefore need a retrieval resolver. Covers
# both the current string (`lossy_ccr`) and the taxonomy-v2 name (`ccr_retrieval`, Δ6).
_LOSSY_FIDELITY = frozenset({"lossy_ccr", "ccr_retrieval"})


def register_resolver(transform_name: str, resolver: object) -> None:
    """Register a CCR resolver for a lossy transform (called by the store, Δ12). Idempotent."""
    _RESOLVERS[transform_name] = resolver


# Size binning is `size_stratum_bytes` (imported from apex_router.proxy_engine.policy) — the SAME function both planes
# use, on the SAME observable (context bytes). No token→byte conversion exists anymore (Δ2 revised):
# the compiler bins its cells by this identical function, so a routed block's cell key == the
# compiled cell key by construction (lesson #7 — share the observable, never bridge with a ratio).


@dataclass(frozen=True)
class Emission:
    """One block's enforcement outcome. `text` is what ships; `transformed` is False when the block
    shipped raw (frozen, not addressable, disabled cell, floor miss, or fail-open). `reason` is a
    short tag for telemetry / shadow-mode diffing."""

    text: str
    transformed: bool
    reason: str


def _utf8_len(s: str) -> int:
    """UTF-8 byte length, tolerant of lone surrogates (a transform emitting `\\ud800` from a
    surrogate in the source must not crash the hot path — cross-validation.1: `surrogatepass` so the
    encode can't raise UnicodeEncodeError; fail-open depends on this never throwing)."""
    return len(s.encode("utf-8", "surrogatepass"))


def _saved_fraction(original: str, out: str) -> float:
    """Byte reduction of `out` vs `original` (UTF-8) — the same unit `rule.ratio_floor` is compiled
    in. 0 when `original` is empty. Surrogate-safe (never raises)."""
    ob = _utf8_len(original)
    return 1.0 - (_utf8_len(out) / ob) if ob else 0.0


def _floors_pass(rendering) -> bool:
    """Structural fidelity floors that gate an emission (§3 `floors_pass`). Deliberately minimal for
    M5a.1: the lossless/recoverable transforms carry their own byte-exact or reconstructable
    guarantee, and the entity/fence floor lands with the prose extractor (its signature will grow a
    transform + block arg then). Here: a rendering that produced nothing is a fail-open, not an
    emission."""
    return bool(rendering is not None and rendering.text)


def decide(
    content: str,
    policy: PolicyVersion,
    *,
    context_bytes: int,
    tool_name: str | None = None,
    frozen: bool = False,
    frozen_text: str | None = None,
    block_meta: dict | None = None,
) -> Emission:
    """Decide one block's emission under a compiled policy (§3). Pure and deterministic: same
    (content, policy, context_bytes, tool_name) → same Emission. Fails open (ships raw) on any
    ambiguity — a decision happens ONCE per block and freeze wins.

    Routing is BY CONTEXT BYTES (Δ2 revised): `context_bytes` is the current cached-context byte
    length (the ledger's committed_wire_length) — the SAME observable the compiler binned cells on,
    so the routed cell key == the compiled cell key by construction. No token count is involved on
    the hot path (a tokenizer is forbidden here, and a provider token count may not exist yet).
    `frozen`/`frozen_text`: the block is behind the frontier (already shipped) → serve the stored
    bytes verbatim, never recompute (the freeze/guard invariant).
    """
    # 1. freeze wins — a frozen block emits its stored bytes, no decision (§3, §5.1).
    if frozen:
        return Emission(frozen_text if frozen_text is not None else content, False, "frozen")

    # 2. route BY CONTEXT BYTES: content class × byte-stratum → the compiled cell rule.
    cls = classify(content, tool_name or "")
    rule = policy.rule_for(cls, size_stratum_bytes(context_bytes))

    # 3. the step-function gate (all compiled; no estimation on the hot path).
    if not (rule.enabled and rule.transform and _utf8_len(content) >= rule.min_bytes):
        return Emission(content, False, "not_addressable")

    module = _BY_NAME.get(rule.transform)
    if module is None:
        return Emission(content, False, "unknown_transform")  # fail-open

    # Δ1 capability gate: a lossy (ccr) cell is inert without a registered resolver — the elided
    # bytes couldn't be served back, so emitting them would be an unrecoverable fidelity loss. Ship
    # raw. (Lossless cells drop nothing → no resolver needed.)
    if rule.fidelity_class in _LOSSY_FIDELITY and rule.transform not in _RESOLVERS:
        return Emission(content, False, "capability_missing")

    # The whole transform+floor sequence is inside fail-open (§6 step 7): ANY exception — the
    # transform raising, or a byte-length compare on surrogate content — ships RAW, never crashes
    # the hot path (cross-validation.1: a lone surrogate must not take down the proxy).
    block = Block(content=content, tool_name=tool_name, meta=block_meta or {})
    try:
        if not module.applies(block):
            return Emission(content, False, "not_applicable")
        rendering = module.run(block, rule.knobs)  # Δ3: sealed compile-time knobs, never {}
        if _floors_pass(rendering) and _saved_fraction(content, rendering.text) >= rule.ratio_floor:
            return Emission(rendering.text, True, "emit")
    except Exception:  # noqa: BLE001 - fail-open is the contract
        return Emission(content, False, "fail_open")

    return Emission(content, False, "below_floor")  # per-block fail-open
