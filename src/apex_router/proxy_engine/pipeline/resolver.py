"""Δ14 stub CCR resolver — serve elided originals back on retrieval, no store (roadmap §Δ14).

A lossy `ccr_retrieval` cell drops bytes behind a counted+located marker carrying a
`ccr://<hash>#<lo>-<hi>` ref. Emitting it is only safe if those bytes can be served back when an
agent retrieves — the Δ1 capability gate ships raw otherwise. The FULL store (Δ12: HMAC refs,
project scoping, content-dedup, retention) comes later; this STUB is the minimum that un-gates the
lossy cell so the Δ14 behavioral gate can run: it holds the (ref → elided fragment) pairs the
transform carried and serves them directly.

Fail-closed: an unknown ref resolves to None — the stub never fabricates bytes it was not given
(the retrieval contract is "serve the exact original or nothing"). Deterministic; no I/O.
"""
from __future__ import annotations

from apex_router.proxy_engine.pipeline.transforms import json_crush


class StubResolver:
    """In-memory ref → elided-fragment map, populated from a transform's own elision pairs.

    `register(content, knobs)` records the pairs a crush of `content` would produce (via
    `json_crush.elisions`, the pure byte-derived mirror of the emit path), so a ref appearing in the
    emitted wire markers resolves to exactly the dropped bytes. Registering an instance with
    `decide.register_resolver("json_crush", resolver)` satisfies the capability gate.
    """

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def register(self, content: str, knobs: dict | None = None) -> int:
        """Record the elisions a crush of `content` produces. Returns how many refs were stored.
        Idempotent per ref (same content+knobs → same refs → same bytes)."""
        pairs = json_crush.elisions(content, knobs or {})
        for ref, fragment in pairs:
            self._map[ref] = fragment
        return len(pairs)

    def resolve(self, ref: str) -> str | None:
        """The exact elided bytes for `ref`, or None if this resolver never stored it (fail-closed)."""
        return self._map.get(ref)

    def __len__(self) -> int:
        return len(self._map)
