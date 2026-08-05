"""JSON deletion-crusher — the first tenant of the lossy_ccr machinery (M5b).

Deletion-only on the JSON grammar: for arrays longer than the retain budget, keep the head + tail
elements verbatim and replace the middle with a COUNTED, LOCATED marker; truncate over-long string
leaves. Everything retained is byte-identical; nothing is re-encoded (the reference proxy's smart_crusher does
a lossy schema+CSV rewrite — the documented counterexample, deliberately NOT copied). The original
is carried for the CCR store so an agent can retrieve what was elided.

Fidelity floor (internal review — three mechanical, pure-function-checkable properties):
  1. verbatim-of-retained — a kept value is never rewritten;
  2. counted + located markers — `[… elided N of M elements (idx a–b) · ccr://<hash>#<range>]`, so
     cardinality and position survive in the emitted bytes (answerable without retrieval);
  3. one intact exemplar per elided array — the first element survives whole, so schema shape is
     inferable from the wire.
F6: markers are token-costly, so they only appear where a real elision happened, one per array, and
`emit_decision` measures the saving on the FINAL bytes including markers.

Deterministic, pure fn of block bytes (T1 tier), registry-blind, no store access. `run` raising on
non-JSON is the fail-open signal (§6). Token-monotone is NOT assumed (BPE non-monotonicity, F1); the
compiler's per-cell `_byte_floor_is_token_safe` gate admits it only where measured safe.
"""

from __future__ import annotations

import hashlib
import json
import re

from apex_router.proxy_engine.pipeline.transforms.base import Block, Rendering

name = "json_crush"
fidelity = "ccr_retrieval"
knobs: list[str] = ["json_keep_head", "json_keep_tail", "json_max_leaf"]

# Retain-budget defaults — knob-registry entries the compiler grid-searches per cell (internal review):
# keep head to carry schema + leading rows, a little tail for recency, cap leaf blobs.
DEFAULT_KEEP_HEAD = 5
DEFAULT_KEEP_TAIL = 2
DEFAULT_MAX_LEAF = 200
MIN_ARRAY_TO_ELIDE = DEFAULT_KEEP_HEAD + DEFAULT_KEEP_TAIL + 1  # smaller arrays: nothing to drop


def _ccr_ref(original_fragment: str, lo: int, hi: int) -> str:
    """A stable CCR reference for an elided span: hash of the elided bytes + the index range. The
    hash lets the retrieval path fetch exactly these bytes; the range makes position explicit."""
    h = hashlib.sha256(original_fragment.encode("utf-8")).hexdigest()[:12]
    return f"ccr://{h}#{lo}-{hi}"


# An atomic-locator leaf is a single opaque token — a url / path / hash / uuid / id — where a prefix
# is NOT a valid subsequence of the whole: a truncated signed URL, path, or hash reads as a complete
# value and gets used as if whole (worse than eliding it). This is the entity floor's protected
# class (internal review). Free text (contains whitespace) is NOT protected: it may be truncated with a
# counted marker, because a prose prefix is self-evidently partial once the marker is on the wire.
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_HEX_RE = re.compile(r"\A[0-9a-fA-F]{32,}\Z")  # long hex hash / oid (sha, git oid, …)


def _is_protected_leaf(s: str) -> bool:
    """True when `s` is an atomic locator/identifier that must never be prefix-truncated. Whitespace
    ⇒ free text ⇒ not protected. Otherwise: url scheme, absolute/deep path, uuid, long hex, or an
    opaque single token (no spaces) carrying a locator separator (`/ : ? # = _ - . +`)."""
    if not s or any(c.isspace() for c in s):
        return False
    low = s.lower()
    # data: URIs are NOT atomic locators — a small prefix + a bulk (base64) payload, the most
    # truncatable content in JSON. They get their own payload-eliding path in `_elide`, not blanket
    # protection (internal review: protecting them whole ships multi-MB blobs the crusher exists to shed).
    if low.startswith("data:"):
        return False
    if low.startswith(("http://", "https://", "ftp://", "s3://", "file://", "ccr://")):
        return True
    if s.startswith("/") or s.startswith("~/") or ("/" in s and "." in s.rsplit("/", 1)[-1]):
        return True  # abs path, home path, or path-with-extension
    if _UUID_RE.match(s) or _HEX_RE.match(s):
        return True
    # opaque token: no whitespace + a locator/token separator + long enough to be an id, not a plain
    # word. `- . + _` cover api keys (`sk-proj-…`), JWTs (`eyJ….….…`), and prefixed ids (`req_…`).
    return len(s) > 24 and any(c in s for c in "/:?#=_-.+")


def _reject_duplicate_keys(pairs):
    """object_pairs_hook: raise on any duplicate key. json_crush re-serializes via json.dumps, which
    silently keeps the last of a duplicate pair — so a duplicate-key object would drop a key the
    model saw (a fidelity loss the round-trip oracle can't detect, V3); such blocks route raw."""
    seen = set()
    for k, _v in pairs:
        if k in seen:
            raise ValueError("duplicate JSON key")
        seen.add(k)
    return dict(pairs)


# Exotic number lexemes (`1e0`, `-0`, `1E5`, `2.0e-3`) survive json parse by VALUE but change under
# re-serialization — a byte change to a token the model may reason about. json.dumps in `_crush`
# would normalize them, so a block containing one routes raw (Δ7). A number whose lexeme re-emits
# identically (`1`, `1.0`, `42`) is safe. Same numeric-token regex as compaction (kept local so the
# transform stays self-contained).
_NUM_RE = re.compile(r"(?<![\w.])-?\d+\.?\d*(?:[eE][+-]?\d+)?")


def _has_lexeme_unstable_number(content: str) -> bool:
    for tok in _NUM_RE.findall(content):
        try:
            if json.dumps(json.loads(tok)) != tok:
                return True
        except (json.JSONDecodeError, ValueError):
            return True  # unparseable numeric-looking token → be conservative
    return False


def _try_load(content: str):
    stripped = content.lstrip()
    if not stripped or stripped[0] not in "[{":
        return None
    if _has_lexeme_unstable_number(stripped):
        return None  # Δ7: a re-serialization would change a number's bytes
    try:
        return json.loads(stripped, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError):
        return None  # Δ7: duplicate keys / malformed → route raw (fail-open)


def _elide(obj, keep_head: int, keep_tail: int, max_leaf: int, counter: list[int]):
    """Recursively delete array middles and truncate long leaves. Returns the crushed structure with
    string sentinels standing in for elided spans; `counter` accumulates the elision count."""
    if isinstance(obj, dict):
        return {k: _elide(v, keep_head, keep_tail, max_leaf, counter) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > keep_head + keep_tail:
            dropped = obj[keep_head : len(obj) - keep_tail]
            lo, hi = keep_head, len(obj) - keep_tail - 1
            ref = _ccr_ref(json.dumps(dropped, separators=(",", ":"), ensure_ascii=False), lo, hi)
            counter[0] += 1
            # keep_head elements (incl. the intact EXEMPLAR at index 0) + one marker + keep_tail
            head = [_elide(x, keep_head, keep_tail, max_leaf, counter) for x in obj[:keep_head]]
            tail_src = obj[len(obj) - keep_tail :]
            tail = [_elide(x, keep_head, keep_tail, max_leaf, counter) for x in tail_src]
            marker = f"[… elided {len(dropped)} of {len(obj)} elements (idx {lo}–{hi}) · {ref}]"
            return head + [marker] + tail
        return [_elide(x, keep_head, keep_tail, max_leaf, counter) for x in obj]
    if isinstance(obj, str) and len(obj) > max_leaf:
        return _elide_leaf(obj, max_leaf, counter)
    return obj


def _elide_leaf(s: str, max_leaf: int, counter: list[int]) -> str:
    """Degrade one over-budget string leaf. THREE modes, so that 'any locator on the wire is whole'
    holds unconditionally (internal review):
      - data: URI  → keep the `data:<mime>,` prefix (schema signal), elide the bulk payload;
      - atomic locator (url/path/hash/uuid/id) → replace the WHOLE value with a marker (never a
        prefix — a partial locator wears completeness, used as if whole, worse than absence);
      - free text → truncate to max_leaf chars + a counted marker (prose prefix is self-evident).

    Marker offsets are UTF-8 BYTE offsets over the ORIGINAL wire bytes (Δ7): `M` is the leaf's byte
    length and `#lo-hi` is the elided byte span, so a resolver fetching original.encode()[lo:hi]
    gets the dropped bytes exactly. Truncation still cuts on a CHAR boundary (`s[:max_leaf]` on the
    str), so the emitted prefix is never a split multibyte char — only the OFFSETS are bytes."""
    counter[0] += 1
    m = len(s.encode("utf-8"))  # M = total leaf length in UTF-8 bytes
    # data: URI — protect only the prefix up to and including the first comma, elide the payload.
    if s.lower().startswith("data:") and "," in s:
        head = s[: s.index(",") + 1]
        lo = len(head.encode("utf-8"))
        payload = s[len(head) :]
        return head + f"[… elided {m - lo} of {m} bytes · {_ccr_ref(payload, lo, m)}]"
    # atomic locator over budget → whole-value marker (no prefix leaks).
    if _is_protected_leaf(s):
        return f"[… elided {m} of {m} bytes · {_ccr_ref(s, 0, m)}]"
    # free text → prefix (cut on a char boundary) + counted marker over BYTE offsets.
    prefix = s[:max_leaf]
    lo = len(prefix.encode("utf-8"))
    return prefix + f"[… elided {m - lo} of {m} bytes · {_ccr_ref(s[max_leaf:], lo, m)}]"


def _crush(content: str, knobs) -> tuple[str | None, int]:
    """Parse → elide → compact re-serialize. Returns (crushed_text, n_elisions), or (None, 0) if not
    a crushable JSON value. Whitespace removal is the lossless compaction we already do; deletion
    is what makes this lossy."""
    obj = _try_load(content)
    if obj is None:
        return None, 0
    keep_head = int(knobs.get("json_keep_head", DEFAULT_KEEP_HEAD))
    keep_tail = int(knobs.get("json_keep_tail", DEFAULT_KEEP_TAIL))
    max_leaf = int(knobs.get("json_max_leaf", DEFAULT_MAX_LEAF))
    counter = [0]
    crushed = _elide(obj, keep_head, keep_tail, max_leaf, counter)
    if counter[0] == 0:
        return None, 0  # nothing elided → not a crush (applies() gates this)
    out = json.dumps(crushed, separators=(",", ":"), ensure_ascii=False)
    return out, counter[0]


def _collect_elisions(obj, keep_head: int, keep_tail: int, max_leaf: int,
                      out: list[tuple[str, str]]) -> None:
    """Walk the SAME structure `_elide` walks and record every (ccr_ref, elided_fragment) pair —
    the bytes that would be dropped, keyed by the ref the emitted marker carries. Read-only mirror
    of `_elide`/`_elide_leaf`: same recursion, same ref construction (`_ccr_ref` over the same
    fragment + offsets), so a ref in the wire marker resolves to exactly these bytes. This is the
    Δ14 resolver's source of truth — no store, served straight from the original (roadmap §Δ14)."""
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_elisions(v, keep_head, keep_tail, max_leaf, out)
        return
    if isinstance(obj, list):
        if len(obj) > keep_head + keep_tail:
            dropped = obj[keep_head : len(obj) - keep_tail]
            lo, hi = keep_head, len(obj) - keep_tail - 1
            fragment = json.dumps(dropped, separators=(",", ":"), ensure_ascii=False)
            out.append((_ccr_ref(fragment, lo, hi), fragment))
            for x in obj[:keep_head]:
                _collect_elisions(x, keep_head, keep_tail, max_leaf, out)
            for x in obj[len(obj) - keep_tail :]:
                _collect_elisions(x, keep_head, keep_tail, max_leaf, out)
        else:
            for x in obj:
                _collect_elisions(x, keep_head, keep_tail, max_leaf, out)
        return
    if isinstance(obj, str) and len(obj) > max_leaf:
        m = len(obj.encode("utf-8"))
        if obj.lower().startswith("data:") and "," in obj:
            head = obj[: obj.index(",") + 1]
            lo = len(head.encode("utf-8"))
            payload = obj[len(head) :]
            out.append((_ccr_ref(payload, lo, m), payload))
        elif _is_protected_leaf(obj):
            out.append((_ccr_ref(obj, 0, m), obj))
        else:
            prefix = obj[:max_leaf]
            lo = len(prefix.encode("utf-8"))
            tail = obj[max_leaf:]
            out.append((_ccr_ref(tail, lo, m), tail))


def elisions(content: str, knobs) -> list[tuple[str, str]]:
    """The (ccr_ref, elided_fragment) pairs a crush of `content` produces — pure, byte-derived from
    the same elision `run()` performs. Empty when the block is not crushable JSON or nothing elides
    (mirrors `_crush` returning None). The Δ14 stub resolver keys on these refs."""
    obj = _try_load(content)
    if obj is None:
        return []
    keep_head = int(knobs.get("json_keep_head", DEFAULT_KEEP_HEAD))
    keep_tail = int(knobs.get("json_keep_tail", DEFAULT_KEEP_TAIL))
    max_leaf = int(knobs.get("json_max_leaf", DEFAULT_MAX_LEAF))
    out: list[tuple[str, str]] = []
    _collect_elisions(obj, keep_head, keep_tail, max_leaf, out)
    return out


def applies(block: Block) -> bool:
    """True iff the block is JSON with something elidable — an array past the retain budget or
    an over-long string leaf. Cheap structural pre-check, then the real elision test."""
    stripped = block.content.lstrip()
    if not stripped or stripped[0] not in "[{":
        return False
    out, n = _crush(block.content, {})
    return out is not None and n > 0


def run(block: Block, knobs) -> Rendering:
    """Pure (block, knobs) → Rendering. Raising is the fail-open signal (§6): non-JSON or a value
    with nothing to elide raises, and the pipeline ships the original."""
    out, n = _crush(block.content, knobs)
    if out is None:
        raise ValueError("json_crush: block is not a crushable JSON value")
    return Rendering(
        text=out,
        fidelity="ccr_retrieval",
        original=block.content,  # carried for the CCR store (retrieval path)
        meta={"elisions": n, "orig_chars": len(block.content), "out_chars": len(out)},
    )
