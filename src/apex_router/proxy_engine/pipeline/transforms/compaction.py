"""Compaction — lossless JSON minification with an exact inverse. §7 (M3, inline <25ms).

Tool results frequently embed pretty-printed JSON (arrays of records, nested objects) whose
insignificant whitespace is pure token drag: the model reads the same structure whether it is
indented or minified. Compaction re-serializes JSON compactly and is LOSSLESS because it carries
the exact inverse:

  - the SEMANTIC content is preserved (json.loads(original) == json.loads(rendering)); the model
    sees identical data.
  - a BYTE-EXACT inverse is available via `recover`: we store the original only when it does not
    round-trip to itself under compact re-emit (i.e. it had significant formatting). For the
    common case (already-parseable JSON that re-emits identically up to whitespace), the inverse
    is structural and needs no stored original — the "lossless, no CCR" property the M3 set
    requires.

Only fires on blocks whose content is a single JSON value (array/object) large enough to matter.
A block that is not valid JSON, or is already compact, does not apply (returns applies()=False),
so plain prose/logs fall through to other transforms untouched.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from apex_router.proxy_engine.pipeline.transforms.base import Block, Rendering, Snapshot

MIN_CHARS = 200  # below this, minification saves little; skip
name = "compaction"
fidelity = "wire_canonicalization"
knobs: list[str] = []  # no tunables (§7 table: compaction has no v1 knobs)

# Number literals in the source JSON (used to verify value-preservation). Negative lookbehind
# avoids matching digits inside strings/identifiers adjacent to word chars or dots.
_NUM_RE = re.compile(r"(?<![\w.])-?\d+\.?\d*(?:[eE][+-]?\d+)?")


def _reject_duplicate_keys(pairs):
    """json object_pairs_hook: raise if any object has duplicate keys. Minifying such JSON
    would DROP a key on the wire (the model would read different content), so compaction must
    not apply — otherwise 'lossless' would be a lie (cross-validation review). Reconstructs a normal
    dict when keys are unique."""
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise ValueError("duplicate JSON key")
        seen.add(k)
    return dict(pairs)


def _try_parse(content: str):
    stripped = content.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    # Reject any TRAILING data after the JSON value (json.loads tolerates leading/trailing
    # whitespace only; extra text would be silently truncated → not lossless).
    try:
        return json.loads(stripped, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError):
        return None


def _value_preserving(original: str) -> bool:
    """True iff compacting preserves every number's exact VALUE **and its LEXEME** and the
    model-visible content. Two failure modes, both → skip (ship raw):

    (a) VALUE drift at PARSE time: 9007199254740993 → …992 (past 2^53), 0.1000…005 → 0.1. Compared
        as exact Decimals (cross-validation).
    (b) LEXEME drift under re-serialization (Δ7): `1e0` → `1.0`, `-0` → `0`, `1E5` → `100000.0`,
        `2.0e-3` → `0.002`. The VALUE is preserved but the BYTES the model reads change — a
        scientific-notation measurement or a `-0` sentinel is a token the model may reason about, so
        a byte change is a fidelity risk even at equal value. If any number token does not re-emit
        byte-identically, compaction is not a pure canonicalization → route raw.
    """
    for tok in _NUM_RE.findall(original):
        try:
            parsed = json.loads(tok)
            exact = Decimal(tok)  # exact value of the original literal
            roundtrip = Decimal(str(parsed))  # value after json parse+repr
        except (InvalidOperation, ValueError):
            return False  # unparseable as a clean number → be conservative
        if exact != roundtrip:
            return False  # (a) value drift
        if json.dumps(parsed) != tok:
            return False  # (b) lexeme drift — Δ7
    return True


def _compact_or_none(content: str) -> str | None:
    """The single parse+serialize+value-check pass, or None if compaction doesn't apply.
    Shared by applies() and run() so the JSON is parsed and serialized ONCE per site instead
    of twice (review finding: applies+run each did a full parse+dumps)."""
    if len(content) < MIN_CHARS:
        return None
    obj = _try_parse(content)  # rejects non-JSON, trailing data, duplicate keys
    if obj is None:
        return None
    compact = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    if len(compact) >= len(content):  # already minified / no gain
        return None
    if not _value_preserving(content):  # no float-precision / -0 / exponent value loss
        return None
    return compact


def applies(block: Block) -> bool:
    # Cheap structural pre-check first (avoids a full json parse for the common non-JSON block),
    # then the real check. run() repeats _compact_or_none once — the pipeline calls applies()
    # then run(); a single full pass per method is the floor without threading state between them.
    stripped = block.content.lstrip()
    if len(block.content) < MIN_CHARS or not stripped or stripped[0] not in "[{":
        return False
    return _compact_or_none(block.content) is not None


def run(block: Block, knobs: Snapshot) -> Rendering:
    compact = _compact_or_none(block.content)
    if compact is None:
        # applies() gates this, but run() must be safe if called anyway → fail-open (§6)
        raise ValueError("compaction: block is not compactable JSON")
    # Byte-exact inverse: minification drops whitespace, so re-emit cannot recover the ORIGINAL
    # spacing. We record the original iff the compact form is not already byte-identical to it —
    # that lets `inverse()` reproduce the exact original bytes on demand. (With dup keys rejected
    # by _try_parse, the compact WIRE text is also model-equivalent to the original.)
    recover = {} if compact == block.content else {"original": block.content}
    return Rendering(
        text=compact,
        fidelity="wire_canonicalization",
        recover=recover,
        meta={"orig_chars": len(block.content), "out_chars": len(compact)},
    )


def inverse(rendering: Rendering) -> str:
    """Reconstruct the ORIGINAL bytes. If the original was carried (had significant
    formatting), return it verbatim; otherwise the compact form IS the canonical original."""
    if "original" in rendering.recover:
        return rendering.recover["original"]
    return rendering.text
