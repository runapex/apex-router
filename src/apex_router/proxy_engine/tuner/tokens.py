"""Class-conditional byte→token estimation (internal review item 4).

The cachesim and A/B need a token count per content blob. A uniform `chars/4` (4.0 bytes/token)
biases the tuner's rankings: BPE token density is NOT uniform over bytes — terminal output is
byte-cheap/token-cheap (ANSI, whitespace, repetition), code is token-dense (identifiers fragment).
Measured on real transcript blobs (cl100k_base):

    terminal 3.51 · prose 3.76 · code 4.06 · json 3.20   bytes/token (medians)

So proportional-uniform conversion over-ranks terminal-class transforms and under-ranks
code/astgrep-class ones in TRUE token savings. This module fits per-class coefficients offline
and converts class-conditionally; where a real tokenizer is available (tiktoken), it is used
directly (exact), and the coefficients are the fallback.
"""

from __future__ import annotations

# `classify` is the plane-neutral content-class contract — it lives in apex_router.proxy_engine.policy (both the offline
# compiler and the hot-path runtime must agree on it). Re-exported here so the many offline callers
# (`from apex_router.proxy_engine.tuner.tokens import classify`) keep working unchanged.
from apex_router.proxy_engine.policy import classify as classify  # noqa: F401

# Measured medians (bytes per token) by content class, cl100k_base over real transcript blobs.
# Fallback when no tokenizer is installed; refit with `fit_coefficients()` on a fresh corpus.
BYTES_PER_TOKEN = {
    "terminal": 3.51,
    "prose": 3.76,
    "code": 4.06,
    "file_read": 4.0,  # mostly source under a line-number gutter → near code density (F3)
    "diff": 3.9,  # source ± markers → between code and terminal (F3)
    "json": 3.20,
    "unknown": 3.8,  # overall-ish default, still better than a flat 4.0
}

# --- exact offline tokenizer (the compiler's oracle) ------------------------------------------
# The class-conditional coefficients above are for ABSOLUTE conversion; they are designed to be
# right on average per class. But in a RATIO of two same-class blobs (orig vs compressed) the
# coefficient CANCELS, collapsing the ratio to a byte-ratio that is blind to token-density change
# — the exact bias that corrupts the compiler's Δ$ target (v2.1 §4 / M5a.1 review). At compile
# time we hold both byte strings, so we MEASURE, never estimate: `true_token_count` is the exact
# tokenizer, memoized by content hash (the replay corpus is finite → a one-time cost). tiktoken
# (cl100k_base) is exact for the Codex/OpenAI wire and a close offline proxy for the Claude wire
# (the wire's own `usage` is the eventual Claude-side oracle — a live calibration hook, not needed
# for the offline compile). A missing tiktoken degrades to the class estimate, but a RATIO of two
# blobs must never be built from that estimate (see `apex_router.proxy_engine.tuner.compiler`).
_TOKENIZER_CACHE: dict[str, int] = {}
_ENCODER = None
_ENCODER_TRIED = False


def _encoder():
    """Lazily load the tiktoken cl100k_base encoder once; None if tiktoken is unavailable."""
    global _ENCODER, _ENCODER_TRIED
    if not _ENCODER_TRIED:
        _ENCODER_TRIED = True
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001 - any failure → fall back to the estimate
            _ENCODER = None
    return _ENCODER


def has_true_tokenizer() -> bool:
    """True iff an exact tokenizer is available — the compiler asserts this before signing a
    policy whose Δ$ came from measured (not estimated) token ratios."""
    return _encoder() is not None


def tokenizer_identity() -> dict:
    """Stable identity of the exact tokenizer used by an evidence-grade compile.

    Package version alone is not enough: an encoding table can be republished or selected
    differently. Hash the loaded pattern, mergeable ranks, and special-token table so a signed
    evidence manifest binds the exact token-counting oracle that produced its economics.
    """
    enc = _encoder()
    if enc is None:
        raise RuntimeError("exact tokenizer unavailable")
    import hashlib
    import importlib.metadata
    import json

    h = hashlib.sha256()
    h.update(getattr(enc, "name", "cl100k_base").encode("utf-8"))
    h.update(getattr(enc, "_pat_str", "").encode("utf-8"))
    for token, rank in sorted(getattr(enc, "_mergeable_ranks", {}).items(), key=lambda x: x[1]):
        h.update(len(token).to_bytes(4, "big"))
        h.update(token)
        h.update(int(rank).to_bytes(8, "big", signed=False))
    h.update(
        json.dumps(
            getattr(enc, "_special_tokens", {}), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    try:
        package_version = importlib.metadata.version("tiktoken")
    except importlib.metadata.PackageNotFoundError:
        package_version = "unknown"
    return {
        "implementation": "tiktoken",
        "package_version": package_version,
        "encoding": getattr(enc, "name", "cl100k_base"),
        "encoding_sha256": h.hexdigest(),
    }


def true_token_count(text: str) -> int:
    """Exact token count via tiktoken, memoized by content hash (finite corpus → one-time cost).
    Falls back to the class estimate only when tiktoken is absent — callers that need a RATIO must
    gate on `has_true_tokenizer()` and refuse to sign policy without it."""
    if not text:
        return 0
    enc = _encoder()
    if enc is None:
        return estimate_tokens(text)
    import hashlib

    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    hit = _TOKENIZER_CACHE.get(key)
    if hit is None:
        # `disallowed_special=()` — a special-token STRING in corpus content (e.g. the literal
        # `<|endoftext|>`, which appears in any transcript discussing tokenizers) is ordinary text to
        # be counted, NOT a control token. tiktoken defaults to raising on such strings; here that
        # would crash a signed-policy compile on benign content (it did, mid-v2a). Count as text.
        hit = len(enc.encode(text, disallowed_special=()))
        _TOKENIZER_CACHE[key] = hit
    return hit


def estimate_tokens(text: str, file_path: str = "", *, tokenizer=None) -> int:
    """Token count for `text`. Uses `tokenizer` (a callable str→int, e.g. tiktoken) when given —
    that is exact; otherwise a class-conditional bytes/token estimate (better than uniform 4.0)."""
    if not text:
        return 0
    if tokenizer is not None:
        return tokenizer(text)
    cls = classify(text, file_path)
    return max(1, int(round(len(text) / BYTES_PER_TOKEN[cls])))


def fit_coefficients(samples: list[tuple[str, str]], tokenizer) -> dict[str, float]:
    """Refit BYTES_PER_TOKEN from (text, file_path) samples using a real tokenizer. Returns
    class → median bytes/token. Used offline to refresh the fallback constants."""
    import statistics

    by_class: dict[str, list[float]] = {}
    for text, fp in samples:
        if len(text) < 200:
            continue
        n = tokenizer(text)
        if n <= 0:
            continue
        by_class.setdefault(classify(text, fp), []).append(len(text) / n)
    return {cls: round(statistics.median(v), 2) for cls, v in by_class.items() if v}
