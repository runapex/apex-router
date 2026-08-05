"""Divergence-signature classification (Spec 1) — per-event structural diagnosis of a cache break.

Analytics plane ONLY: this module never imports `apex_router.proxy_engine.pipeline` / `apex_router.proxy_engine.proxy` and never touches the
wire. It consumes a divergence event (a `cache_read` drop on an append-shaped turn, from the WP3
detector) plus the frontier bytes captured around the divergence point (#14), localizes the changed
region, extracts structural features, and classifies the change into one of a small set of causes,
each with a verbatim one-line fix for the doctor report.

Pipeline position:
    divergence event → frontier-byte context (#14) → localize → features → classify → doctor line

Graceful degradation is part of the contract: if the byte capture is absent (event predates #14, was
oversize-skipped, or the capture gapped), the classifier returns `UNATTRIBUTED_NO_BYTES` — never a
guessed class. No inference without its population, applied per-event.

On the Anthropic wire this runs today (sessions group via headers). On the Codex wire it activates
only after the suffix matcher (#13) lands with controls green; the module takes already-grouped
events and does not care who grouped them, but the doctor footer must state which wire's events are
classified vs pending grouping.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from apex_router.proxy_engine.policy import classify as classify_content

# 8 MB comparison bound. Derivation (bound, not policy): max observed context ~512k tokens; at the
# measured 3.2–4.06 bytes/token (apex_router.proxy_engine.policy canonical byte strata) that is ~1.6–2.1 MB, so 8 MB is
# ~4× the largest real prefix — the reference proxy without materializing MB-scale spliced copies.
_MAX_COMPARE_BYTES = 8 * 1024 * 1024

# difflib region-splitting runs on only the first _DIFFLIB_WINDOW bytes of each bracket. difflib is
# O(n²), and a bracket can contain a huge COMMON middle (an unchanged 90k-byte blob between a front
# reorder and an end append — the prefix/suffix scan can't remove an interior match), so diffing the
# whole bracket is catastrophic (measured: 90k bracket ≈ 7 min). The FIRST changed region — the one
# v1 reports — is at the front by construction (offset = first differing byte), so a 4 KB front
# window captures a reorder/value-swap in full while keeping the diff O(window²) = fast. A bracket
# longer than the window → `coarse_regions=True` (n_regions is a floor); first region stays exact.
_DIFFLIB_WINDOW = 4096

# Two changed runs separated by fewer than this many matching bytes are COALESCED into one region —
# the diff-hunk grouping rule (git does the same with context). Without it, incidental byte matches
# (aligned spaces, shared punctuation) shatter one logical change into many, inflating n_regions and
# firing a false HISTORY_RERENDER. 5 sits above incidental (1–3 char) matches and below structural
# scaffolding (`turn2 ` etc. ≥6), separating a value swap from a genuine multi-turn re-render.
_MIN_MATCH_GAP = 5

# Padding (bytes) around the changed span for PATTERN matching + JSON-key extraction. The minimal
# diff span often clips the signal (a timestamp's `2026-` year sits in the common prefix), so
# volatile patterns and key-sets read from a window, not the bare span. 64B covers a timestamp/key.
_CTX_PAD = 64

# bytes-per-token estimate for the offset→tokens sanity check ONLY (not billing). Lower bound of the
# measured 3.2–4.06 range: using the SMALLEST bytes/token maps a byte offset to the LARGEST token
# estimate, so the "offset lands at/below divergence_point" invariant is checked conservatively (we
# won't wrongly flag INCONSISTENT_SIGNAL by under-estimating where the change sits).
_BYTES_PER_TOKEN_LB = 3.2

# A changed region whose first byte, converted to tokens, exceeds the claimed divergence point by
# more than this factor is not a prefix change — flag INCONSISTENT_SIGNAL rather than classify.
_INCONSISTENT_OVER_FACTOR = 3.0


# ---------- Stage 1: diff localization ----------

@dataclass(frozen=True)
class ChangedRegion:
    offset: int | None          # first differing byte; None if identical
    old_span: bytes             # the FIRST changed region in `prior` (v1 reports the first only)
    new_span: bytes             # the first changed region in `current`
    n_regions: int              # disjoint changed regions after gap-coalescing (diagnostic itself)
    truncated: bool = False     # comparison hit the _MAX_COMPARE_BYTES bound
    coarse_regions: bool = False  # bracket exceeded the difflib cap → n_regions is a floor


def _common_prefix_len(a: memoryview, b: memoryview) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix_len(a: memoryview, b: memoryview, floor: int) -> int:
    # common suffix on the remainder, not crossing `floor` bytes already consumed by the prefix.
    n = min(len(a), len(b)) - floor
    i = 0
    while i < n and a[len(a) - 1 - i] == b[len(b) - 1 - i]:
        i += 1
    return i


def _regions_from_bracket(old: bytes, new: bytes) -> tuple[bytes, bytes, int]:
    """Split the changed bracket into logical regions via difflib, COALESCING changes separated by a
    matching run shorter than _MIN_MATCH_GAP. Returns (first_old, first_new, region_count)."""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    # collect non-equal opcodes and the equal gaps between them
    groups: list[tuple[int, int, int, int]] = []  # (i1,i2,j1,j2) merged change spans
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # a short equal run does NOT close the current group (coalesce across it)
            if groups and (i2 - i1) < _MIN_MATCH_GAP:
                continue
            continue
        if groups:
            gi1, gi2, gj1, gj2 = groups[-1]
            # find the equal-run length immediately before this change to decide coalescing
            if (i1 - gi2) < _MIN_MATCH_GAP and (j1 - gj2) < _MIN_MATCH_GAP:
                groups[-1] = (gi1, i2, gj1, j2)  # merge into the previous group
                continue
        groups.append((i1, i2, j1, j2))
    if not groups:
        return b"", b"", 0
    fi1, fi2, fj1, fj2 = groups[0]
    return old[fi1:fi2], new[fj1:fj2], len(groups)


def localize(prior: bytes, current: bytes) -> ChangedRegion:
    """First changed region between two byte prefixes. Byte-scans the common prefix/suffix on
    memoryviews (no spliced copies, MB-safe), then splits the residual BRACKET into logical regions
    with difflib + gap-coalescing. `offset` = first differing byte; `old_span`/`new_span` = FIRST
    region only (v1); `n_regions` = coalesced region count (multi-region is the history-rerender
    signal). Comparison capped at _MAX_COMPARE_BYTES (`truncated`); an oversized bracket falls back
    to a coarse single-region reading (`coarse_regions`) rather than a slow diff."""
    truncated = len(prior) > _MAX_COMPARE_BYTES or len(current) > _MAX_COMPARE_BYTES
    pa = memoryview(prior)[:_MAX_COMPARE_BYTES]
    pb = memoryview(current)[:_MAX_COMPARE_BYTES]

    pre = _common_prefix_len(pa, pb)
    if pre == len(pa) == len(pb):
        return ChangedRegion(None, b"", b"", n_regions=0, truncated=truncated)

    suf = _common_suffix_len(pa, pb, pre)
    bracket_len = max(len(pa) - suf - pre, len(pb) - suf - pre)
    # diff only the FRONT window of the bracket (O(window²), fast); the first region lives here.
    win_old = bytes(pa[pre:min(pre + _DIFFLIB_WINDOW, len(pa) - suf)])
    win_new = bytes(pb[pre:min(pre + _DIFFLIB_WINDOW, len(pb) - suf)])
    coarse = bracket_len > _DIFFLIB_WINDOW  # changes may exist past window → n_regions is a floor

    first_old, first_new, n_regions = _regions_from_bracket(win_old, win_new)
    return ChangedRegion(offset=pre, old_span=first_old, new_span=first_new,
                         n_regions=n_regions, truncated=truncated, coarse_regions=coarse)


# ---------- input contract ----------

@dataclass(frozen=True)
class DivergenceContext:
    session_id: str
    turn: int
    prior_cache_read: int
    new_cache_read: int
    divergence_point_tokens: int
    prior_prefix_bytes: bytes | None
    current_prefix_bytes: bytes | None
    endpoint_id: str


# ---------- Stage 2: feature extraction ----------

# volatile-value patterns — each a NAMED, individually tested regex (the protected-leaf predicate
# discipline from json_crush: a leaf pattern gets its own test so a false-positive is caught at the
# pattern, not the rule). Order matters: first hit wins in `_volatile_hit`.
_VOLATILE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("iso_timestamp", re.compile(rb"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")),
    ("uuid", re.compile(rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")),
    ("request_id", re.compile(rb"(?:req_|msg_|chatcmpl-)[0-9A-Za-z]{6,}")),
    ("unix_epoch", re.compile(rb"\b\d{10,13}\b")),
    ("counter_like", re.compile(rb"^\d{1,9}$")),
)


def _volatile_hit(old_span: bytes, new_span: bytes, old_win: bytes, new_win: bytes) -> str | None:
    """First volatile pattern matching the changed region. Non-counter patterns match the WINDOW
    (context carries the signal — the year of a timestamp, the `req_` prefix), but ONLY when the
    matched value actually DIFFERS between old and new windows (a value in unchanged surrounding
    context is not the cause). counter_like is special: BOTH bare spans must be short integers (a
    value swap, not incidental digits)."""
    for name, pat in _VOLATILE_PATTERNS:
        if name == "counter_like":
            if pat.match(old_span.strip()) and pat.match(new_span.strip()):
                return name
            continue
        om = pat.search(old_win)
        nm = pat.search(new_win)
        # require the pattern in BOTH windows but with a CHANGED value — a stable timestamp in both
        # is not the divergence; a substituted one is.
        if om and nm and om.group(0) != nm.group(0):
            return name
        # or present in exactly one side (added/removed volatile token in the changed span itself)
        if bool(om) != bool(nm) and (pat.search(old_span) or pat.search(new_span)):
            return name
    return None


@dataclass(frozen=True)
class SignatureFeatures:
    offset: int
    offset_fraction: float
    old_len: int
    new_len: int
    len_delta: int
    n_regions: int
    same_length_substitution: bool
    volatile_token_hit: str | None
    region_class: str
    boundary_aligned: bool
    current_is_shorter: bool
    current_is_prefix_of_prior: bool
    prior_is_prefix_of_current: bool  # pure append (prior survives intact) — the healthy case


def _window(buf: bytes, offset: int, span_len: int) -> bytes:
    """A padded byte window around the changed span — pattern/key matching needs surrounding context
    (a timestamp's year, a JSON key's quotes) that the minimal diff span clips off."""
    lo = max(0, offset - _CTX_PAD)
    hi = min(len(buf), offset + span_len + _CTX_PAD)
    return buf[lo:hi]


def _extract_features(ctx: DivergenceContext, region: ChangedRegion) -> SignatureFeatures:
    prior = ctx.prior_prefix_bytes or b""
    current = ctx.current_prefix_bytes or b""
    offset = region.offset or 0
    old_len, new_len = len(region.old_span), len(region.new_span)
    # Read patterns from a WINDOW around the change, not the bare span (context carries the signal).
    old_win = _window(prior, offset, old_len)
    new_win = _window(current, offset, new_len)
    # region_class: decode the changed-span window leniently and reuse the plane-neutral classifier.
    region_class = classify_content(new_win.decode("utf-8", "replace"))
    current_is_prefix = len(current) < len(prior) and prior.startswith(current)
    # pure append: prior is a CLEAN prefix of current (nothing below the cache point changed) — the
    # HEALTHY append-caching case, not a divergence. The changed region starts at len(prior).
    prior_is_prefix = len(current) > len(prior) and current.startswith(prior)
    return SignatureFeatures(
        offset=offset,
        offset_fraction=(offset / len(prior)) if prior else 0.0,
        old_len=old_len,
        new_len=new_len,
        len_delta=new_len - old_len,
        n_regions=region.n_regions,
        same_length_substitution=(old_len == new_len and old_len > 0),
        volatile_token_hit=_volatile_hit(region.old_span, region.new_span, old_win, new_win),
        region_class=region_class,
        boundary_aligned=False,  # message_boundaries not wired yet (#14); explicit, not a guess
        current_is_shorter=len(current) < len(prior),
        current_is_prefix_of_prior=current_is_prefix,
        prior_is_prefix_of_current=prior_is_prefix,
    )


# ---------- Stage 3: classification (rules v1; clustering slot behind the `classify` seam)
# ----------

_FIX_TEXT = {
    "VOLATILE_VALUE": ("A timestamp/ID in your prompt changes every request. Pin or remove it — "
                       "everything after byte {byte} re-bills at full price."),
    "TOOLDEF_RESERIALIZATION": ("Your tool definitions re-serialize in unstable order. "
                                "Sort keys / pin serialization."),
    "HISTORY_RERENDER": ("The client re-rendered conversation history. Check for message edits or "
                         "unstable history reconstruction."),
    "PREFIX_TRUNCATION": "Context was truncated/compacted below the cache point.",
    "APPEND_ONLY": ("Healthy: only new bytes appended; the cached prefix is intact. Not a "
                    "divergence (no fix needed)."),
    "UNCLASSIFIED": ("Prefix changed at byte {byte} ({span} bytes). Unrecognized pattern — "
                     "inspect the diff."),
    "UNATTRIBUTED_NO_BYTES": ("cache broke at ~{tokens} tokens; byte capture unavailable for this "
                              "event (cause not classified)."),
    "INCONSISTENT_SIGNAL": ("Reported cache break at ~{tokens} tokens but the byte change is far "
                            "past that point — not a prefix change; not classified."),
    "NO_CHANGE": "No prefix change detected (prior and current are identical).",
}


# JSON key at the start of a `"key":` construct — extracts a key MULTISET from a fragment that may
# not parse (the changed bracket is usually an object *interior*, not a whole value).
_JSON_KEY_RE = re.compile(rb'"([A-Za-z_][\w-]*)"\s*:')


def _json_key_multiset(span: bytes):
    """Ordered list of JSON key names found in a (possibly-fragment) span. A MULTISET/order pair: a
    reserialization keeps the same key SET but changes ORDER, so equal-set + different-order is the
    signature (equal-set + equal-order = not a reorder; different-set = a semantic edit)."""
    return _JSON_KEY_RE.findall(span)


def _is_tooldef_reserialization(f: SignatureFeatures, old_win: bytes, new_win: bytes) -> bool:
    # near the front of the prefix, JSON-structured context, and the changed window's key SET is
    # unchanged while its ORDER changed — a re-serialization (reorder/whitespace), not a semantic
    # edit. Keys are extracted by regex from the WINDOW (an object-INTERIOR fragment won't
    # json.loads,
    # so we can't rely on region_class=='json'; ≥2 `"key":` constructs is the JSON-structured
    # signal).
    if f.offset_fraction >= 0.3:
        return False
    ka, kb = _json_key_multiset(old_win), _json_key_multiset(new_win)
    if len(ka) < 2 or len(kb) < 2:
        return False
    return sorted(ka) == sorted(kb) and ka != kb  # same key set, different order


def classify(features: SignatureFeatures, old_win: bytes = b"", new_win: bytes = b"") -> str:
    """The classification seam: features → class string. v1 binds ordered rules (first match wins);
    the clustering slot (HDBSCAN over feature vectors) would bind here instead once the event
    population earns it. Do not add the clusterer without its admission evidence.
    `old_win`/`new_win` are padded windows the JSON-reorder rule reads (empty in a feature call)."""
    f = features
    # APPEND_ONLY — prior survives intact, only new bytes appended below the cache point. The
    # healthy append-caching case; label it benign so it never reads as an unexplained break.
    if f.prior_is_prefix_of_current:
        return "APPEND_ONLY"
    # PREFIX_TRUNCATION — current is a strict prefix of prior (compacted below the cache point).
    # Ahead of the region rules: a truncation IS one trailing region but a distinct, cleaner call.
    if f.current_is_prefix_of_prior:
        return "PREFIX_TRUNCATION"
    # VOLATILE_VALUE — a short value substitution matching a volatile pattern.
    if (f.volatile_token_hit is not None
            and (f.same_length_substitution or abs(f.len_delta) < 32)
            and f.n_regions <= 2):
        return "VOLATILE_VALUE"
    # TOOLDEF_RESERIALIZATION — front-of-prefix JSON, same key set, reordered.
    if _is_tooldef_reserialization(f, old_win, new_win):
        return "TOOLDEF_RESERIALIZATION"
    # HISTORY_RERENDER — many regions, or a boundary-aligned large shift.
    if f.n_regions >= 3 or (f.boundary_aligned and abs(f.len_delta) > 256):
        return "HISTORY_RERENDER"
    return "UNCLASSIFIED"


# ---------- result + top-level entry ----------

@dataclass(frozen=True)
class DivergenceSignature:
    klass: str
    fix_text: str
    features: SignatureFeatures | None
    session_id: str
    turn: int
    endpoint_id: str
    divergence_point_tokens: int
    rendered_excerpt: str = ""   # redacted UNCLASSIFIED excerpt (only when include_spans)
    region: ChangedRegion | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        f = self.features
        return {
            "class": self.klass,
            "fix": self.fix_text,
            "session_id": self.session_id,
            "turn": self.turn,
            "endpoint_id": self.endpoint_id,
            "divergence_point_tokens": self.divergence_point_tokens,
            "rendered_excerpt": self.rendered_excerpt,
            "features": None if f is None else {
                "offset": f.offset, "offset_fraction": round(f.offset_fraction, 4),
                "old_len": f.old_len, "new_len": f.new_len, "len_delta": f.len_delta,
                "n_regions": f.n_regions,
                "same_length_substitution": f.same_length_substitution,
                "volatile_token_hit": f.volatile_token_hit,
                "region_class": f.region_class,
                "boundary_aligned": f.boundary_aligned,
            },
        }


# A high-entropy token: a long run (≥20) of base62/JWT-ish chars. Catches a bare credential VALUE
# whose adjacent keyword ("Bearer", "x-api-key") sits OUTSIDE the changed span (in the common
# prefix) — a marker-substring check alone would miss it (self-adversarial finding). Length 20 is
# above ordinary identifiers and at/below real key lengths (sk- keys, JWTs, dashless UUIDs).
_HIGH_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9_\-]{20,}")
_CRED_MARKERS = ("sentinel-secret", "bearer ", "sk-", "authorization", "x-api-key", "api_key",
                 "apikey", "-apikey", "token", "secret", "password")


def _looks_high_entropy(tok: str) -> bool:
    """A long token that mixes character CLASSES (both letters and digits, or is very long) — the
    shape of a credential, not an English word. Plain long words (all-alpha) are NOT flagged so
    ordinary prose diffs aren't over-redacted."""
    if len(tok) < 20:
        return False
    has_alpha = any(c.isalpha() for c in tok)
    has_digit = any(c.isdigit() for c in tok)
    return (has_alpha and has_digit) or len(tok) >= 40


def _redact(text: str) -> str:
    """Run a candidate excerpt through credential detectors — a keyword MARKER anywhere, OR a bare
    high-entropy TOKEN (a credential value whose keyword sits outside the changed span). If either
    hits, replace the WHOLE excerpt with its length+shape only. The doctor must never become the
    leak path the secrets canary guards elsewhere."""
    lowered = text.lower()
    if any(m in lowered for m in _CRED_MARKERS):
        return f"<redacted: {len(text)} bytes, credential marker present>"
    if any(_looks_high_entropy(t) for t in _HIGH_ENTROPY_TOKEN.findall(text)):
        return f"<redacted: {len(text)} bytes, high-entropy token present>"
    return text


def _fix(klass: str, *, byte: int = 0, span: int = 0, tokens: int = 0) -> str:
    return _FIX_TEXT[klass].format(byte=f"{byte:,}", span=f"{span:,}", tokens=f"{tokens:,}")


def classify_divergence(ctx: DivergenceContext, *,
                        include_spans: bool = False) -> DivergenceSignature:
    """Full pipeline for one divergence event: degrade → localize → features → classify → fix text.
    Returns a `DivergenceSignature`. Never raises on content; graceful on absent capture."""
    def sig(klass, features=None, region=None, excerpt="", **fmt):
        return DivergenceSignature(
            klass=klass, fix_text=_fix(klass, **fmt), features=features,
            session_id=ctx.session_id, turn=ctx.turn, endpoint_id=ctx.endpoint_id,
            divergence_point_tokens=ctx.divergence_point_tokens,
            rendered_excerpt=excerpt, region=region,
        )

    # degradation contract: no bytes → no guess.
    if ctx.prior_prefix_bytes is None or ctx.current_prefix_bytes is None:
        return sig("UNATTRIBUTED_NO_BYTES", tokens=ctx.divergence_point_tokens)

    region = localize(ctx.prior_prefix_bytes, ctx.current_prefix_bytes)
    if region.offset is None:
        return sig("NO_CHANGE", region=region)

    # sanity invariant: the change must sit at/below the claimed divergence point. If the first
    # differing byte, in tokens (conservative LB bytes/token), is far ABOVE it, this is not a prefix
    # change — flag rather than classify (INCONSISTENT_SIGNAL).
    offset_tokens = region.offset / _BYTES_PER_TOKEN_LB
    if (ctx.divergence_point_tokens > 0
            and offset_tokens > ctx.divergence_point_tokens * _INCONSISTENT_OVER_FACTOR):
        return sig("INCONSISTENT_SIGNAL", region=region, tokens=ctx.divergence_point_tokens)

    features = _extract_features(ctx, region)
    old_win = _window(ctx.prior_prefix_bytes, region.offset, len(region.old_span))
    new_win = _window(ctx.current_prefix_bytes, region.offset, len(region.new_span))
    klass = classify(features, old_win, new_win)

    excerpt = ""
    if include_spans:
        raw = f"old[:256]={region.old_span[:256]!r} new[:256]={region.new_span[:256]!r}"
        excerpt = _redact(raw)

    return sig(klass, features=features, region=region, excerpt=excerpt,
               byte=region.offset, span=len(region.old_span),
               tokens=ctx.divergence_point_tokens)


# ---------- doctor integration: per-event line + summary block ----------

# Short class labels for the report (the fix_text carries the actionable sentence).
_CLASS_LABEL = {
    "VOLATILE_VALUE": "VOLATILE_VALUE — a timestamp/ID in the prompt changed",
    "TOOLDEF_RESERIALIZATION": "TOOLDEF_RESERIALIZATION — tool definitions re-serialized",
    "HISTORY_RERENDER": "HISTORY_RERENDER — conversation history re-rendered",
    "PREFIX_TRUNCATION": "PREFIX_TRUNCATION — context truncated below the cache point",
    "APPEND_ONLY": "APPEND_ONLY — healthy append, cached prefix intact (not a divergence)",
    "UNCLASSIFIED": "UNCLASSIFIED — unrecognized prefix change",
    "UNATTRIBUTED_NO_BYTES": "UNATTRIBUTED — byte capture unavailable",
    "INCONSISTENT_SIGNAL": "INCONSISTENT_SIGNAL — change is not at the cache point",
    "NO_CHANGE": "NO_CHANGE — no prefix divergence",
}


def format_event(sig: DivergenceSignature) -> str:
    """The per-event doctor line (Spec 1 shape): what broke, where, the class, and the fix."""
    f = sig.features
    at = ""
    if f is not None:
        at = f" (change at byte {f.offset:,})"
    lines = [
        f"⚠ Turn {sig.turn} broke your cache at ~{sig.divergence_point_tokens:,} tokens "
        f"(endpoint: {sig.endpoint_id})",
        f"  Cause: {_CLASS_LABEL.get(sig.klass, sig.klass)}{at}",
        f"  Fix: {sig.fix_text}",
    ]
    if sig.rendered_excerpt:
        lines.append(f"  Diff: {sig.rendered_excerpt}")
    return "\n".join(lines)


# Benign classes are NOT cache-break events: NO_CHANGE (identical) and APPEND_ONLY (healthy append,
# cached prefix intact). Excluded from the events roll-up so the summary counts only real breaks.
_BENIGN_CLASSES = ("NO_CHANGE", "APPEND_ONLY")


def summarize(signatures: list[DivergenceSignature]) -> dict:
    """Summary block: events by class, per session — the roll-up the doctor prints above per-event
    detail. Counts only real events (benign classes are not events)."""
    by_class: dict[str, int] = {}
    by_session: dict[str, dict[str, int]] = {}
    for s in signatures:
        if s.klass in _BENIGN_CLASSES:
            continue
        by_class[s.klass] = by_class.get(s.klass, 0) + 1
        by_session.setdefault(s.session_id, {})
        by_session[s.session_id][s.klass] = by_session[s.session_id].get(s.klass, 0) + 1
    return {
        "n_events": sum(by_class.values()),
        "by_class": dict(sorted(by_class.items(), key=lambda kv: -kv[1])),
        "by_session": by_session,
    }


def format_divergence_report(signatures: list[DivergenceSignature],
                             *, classified_wires: tuple[str, ...] = ("anthropic",),
                             pending_wires: tuple[str, ...] = ()) -> str:
    """The full `apex doctor --divergence` text: header (which wires classified vs pending grouping)
    → summary by class → per-event detail. States the wire provenance so a reader knows the Codex
    events are pending the suffix matcher (#13) when it hasn't landed."""
    out = ["divergence signatures — what broke your cache, per event", "=" * 60]
    out.append(f"classified wires: {', '.join(classified_wires) or 'none'}")
    if pending_wires:
        out.append(f"pending session grouping (not yet classified): {', '.join(pending_wires)} "
                   "— needs the suffix matcher (#13)")
    summary = summarize(signatures)
    out.append("")
    if not summary["n_events"]:
        out.append("no divergence events in this window.")
        return "\n".join(out)
    out.append(f"{summary['n_events']} event(s) by class:")
    for klass, n in summary["by_class"].items():
        out.append(f"  {n:>4}  {_CLASS_LABEL.get(klass, klass)}")
    out.append("")
    for s in signatures:
        if s.klass in _BENIGN_CLASSES:
            continue
        out.append(format_event(s))
        out.append("")
    return "\n".join(out)
