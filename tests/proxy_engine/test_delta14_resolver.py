"""Δ14 stub resolver — serve elided originals back on retrieval (M6a Stage, roadmap §Δ14).

A lossy `ccr_retrieval` cell drops bytes from the wire behind a counted+located marker
(`ccr://<hash>#<lo>-<hi>`). It is only SAFE to emit if a resolver can serve the elided original
back when an agent needs it (the Δ1 capability gate: no resolver ⇒ decide() ships raw). The FULL
CCR store is Δ12 (HMAC refs, project scoping, dedup); the Δ14 STUB serves retrievals directly from
the transform's carried `original` — enough to run the behavioral gate before the store exists.

Contract:
  - `json_crush.elisions(content, knobs)` returns the (ref, elided_fragment) pairs a crush produced —
    pure, read-only, byte-derived from the SAME elision `run()` performs (so a ref in the emitted
    marker resolves to exactly the bytes that were dropped).
  - `StubResolver.register(content, knobs)` stores those pairs; `.resolve(ref)` returns the elided
    fragment for a ref the emitted bytes carry, or None for an unknown ref (fail-closed: never invent
    bytes). Registering the resolver with `decide.register_resolver` un-gates the lossy cell.
"""
from __future__ import annotations

import json

from apex_router.proxy_engine.pipeline.resolver import StubResolver
from apex_router.proxy_engine.pipeline.transforms import json_crush
from apex_router.proxy_engine.pipeline.transforms.base import Block


def _big_array_json() -> str:
    # 300 records → past the retain budget, so json_crush elides the middle behind a ccr marker.
    return json.dumps([{"id": i, "name": f"item-{i}", "score": i * 7} for i in range(300)])


def test_elisions_refs_match_the_emitted_markers():
    """Every ref `elisions()` reports is present verbatim in the crushed output's markers — the
    resolver's keys are exactly the refs on the wire, so a retrieval can't ask for a ref that
    doesn't exist and can't miss one that does."""
    content = _big_array_json()
    rendering = json_crush.run(Block(content=content, tool_name="tool_result"), {})
    pairs = json_crush.elisions(content, {})
    assert pairs, "expected at least one elision on a 300-element array"
    for ref, _fragment in pairs:
        assert ref in rendering.text, f"ref {ref} not found in emitted markers"


def test_resolver_serves_the_exact_elided_bytes():
    """A registered resolver returns, for each ref, the exact JSON fragment that was dropped —
    parseable and equal to the corresponding slice of the original array."""
    content = _big_array_json()
    resolver = StubResolver()
    resolver.register(content, {})
    pairs = json_crush.elisions(content, {})
    ref, fragment = pairs[0]
    served = resolver.resolve(ref)
    assert served == fragment
    # the served bytes are the real dropped records, not a summary
    recovered = json.loads(served)
    assert isinstance(recovered, list) and recovered
    assert recovered[0] == {"id": 5, "name": "item-5", "score": 35}  # first elided (keep_head=5)


def test_resolver_fail_closed_on_unknown_ref():
    """An unknown ref resolves to None — the stub never fabricates bytes it wasn't given."""
    resolver = StubResolver()
    resolver.register(_big_array_json(), {})
    assert resolver.resolve("ccr://deadbeef#0-0") is None


def test_registering_resolver_ungates_the_lossy_cell():
    """With a resolver registered under the transform name, decide()'s Δ1 capability gate no longer
    ships raw with `capability_missing` — the lossy cell becomes reachable."""
    from apex_router.proxy_engine.pipeline import decide as decide_mod

    resolver = StubResolver()
    decide_mod.register_resolver("json_crush", resolver)
    try:
        assert "json_crush" in decide_mod._RESOLVERS
    finally:
        decide_mod._RESOLVERS.pop("json_crush", None)  # keep global registry clean for other tests
