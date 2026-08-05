"""Canonicalization + hashing — §3.2 LOCKED.

ONE canonical_json, ONE hash function, imported everywhere a hash is computed. A mismatch
between the session matcher and the prefix guard is a silent catastrophic bug class
(a frozen block would never match, or a diverged prefix would falsely match), so this is
the single source of truth. The test matrix pins it with a golden-vector test.

TWO DISTINCT HASH PURPOSES — DO NOT CONFLATE (cross-validation):

  1. SESSION / CHAIN IDENTITY (§4 matcher, this module's `hash_obj`): "is this the same
     logical message as the one the client resent last turn?" canonical_json is CORRECT
     here — key order is normalized so a message matches its own re-serialization. It
     matches a message against ITS OWN prior resend, not against an adversarial edit.

  2. PREFIX-GUARD CACHE SAFETY (§5.2, M2 — NOT in this module yet): "do the exact bytes we
     are about to emit still byte-prefix-match what we shipped last turn?" That MUST hash
     the RAW EMITTED BYTES (incremental sha256 over the output stream, hash-at-length), NEVER
     canonical_json — because Anthropic caches on the literal byte prefix, and canonical_json
     would bless a byte change that busts the cache. When M2 lands, its hash lives in
     freeze.py over raw bytes; it must not import canonical_json for the guard.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """LOCKED (§3.2): stable, compact, unicode-preserving JSON.

    sort_keys → order-independent; compact separators → no incidental whitespace;
    ensure_ascii=False → hash the real UTF-8 bytes, not \\u escapes.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj: Any) -> str:
    """sha256 of the canonical JSON of an object (a message, a block)."""
    return sha256_hex(canonical_json(obj))
