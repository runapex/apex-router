"""M5b-S verify-first pins (roadmap §0) — V1/V2/V3 outcomes, committed as regressions.

These three facts were established before any Δ work and are pinned so a later change can't silently
flip them without a test failing. See docs/the reference window-apex-roadmap-m5bS-m7.md §0.
"""

from __future__ import annotations

import json

from apex_router.proxy_engine.pipeline.transforms import compaction
from apex_router.proxy_engine.pipeline.transforms.base import Block

# ── V1 — the compiler cannot currently emit a json_crush rule ────────────────────────────────────
# _TRANSFORMS["json"] = (compaction, None); json_crush is NOT in the compiler's registry, so a
# signed policy can never name it. Therefore Δ1 (capability-gated lossy) lands as a STRUCTURAL
# hardening, not a hotfix for a live emission path. If this ever changes, Δ1's refusal must already
# be in place — this test is the tripwire.


def test_v1_compiler_cannot_emit_json_crush_rule():
    from apex_router.proxy_engine.tuner import compiler

    names = {entry[0].name for entry in compiler._TRANSFORMS.values() if entry[0] is not None}
    assert "json_crush" not in names, (
        "json_crush is now in the compiler's _TRANSFORMS — Δ1 capability-gating must gate its "
        "signing BEFORE this is allowed (roadmap V1)."
    )
    # the json cell maps to lossless compaction, not the lossy crusher
    assert compiler._TRANSFORMS["json"][0] is compaction


def test_v1_json_crush_is_runnable_but_only_via_explicit_rule():
    # the RUNTIME registry does know json_crush (decide can execute a rule that names it) — the gap
    # is purely that the compiler won't SIGN such a rule. Pin both halves so the asymmetry is clear.
    from apex_router.proxy_engine.pipeline import decide

    assert "json_crush" in decide._BY_NAME


# ── V2 — no code path applies a rendering (offloaded or inline) after first emission ─────────────
# The live handler (apex/proxy/handlers/passthrough.py) forwards raw bytes and emits telemetry; it
# does NOT call decide / offload / freeze / guard, so no path can apply an offloaded rendering to an
# already-emitted block. Δ5 is therefore spec + this pin, not a bugfix.
#
# NARROWED the reference window: the pin forbids the byte-MUTATING rendering/emit machinery
# (decide/OffloadPool/guard/freeze), NOT all pipeline imports. Active mode now runs the COMPUTE-ONLY
# `run_shadow` side-read (bytes_by_class = R1's X, measurement-always-on — telemetry contract); that
# is a prediction over a COPY, never mutates forwarded bytes, so it does not implicate Δ5. A real
# transform being wired in (decide/offload/guard/freeze) still fails this pin.


def test_v2_passthrough_handler_does_not_apply_renderings():
    import inspect

    from apex_router.proxy_engine.proxy.handlers import passthrough

    src = inspect.getsource(passthrough)
    # The byte-mutating rendering/emit machinery — wiring ANY of these means transforms are on the
    # emit path, and Δ5's offload-deadline rule must land WITH that wiring.
    for wired in ("decide(", "OffloadPool", "guard(", "freeze(", "from apex_router.proxy_engine.pipeline.transforms"):
        assert wired not in src, (
            f"passthrough handler now references {wired!r} — the transform pipeline is being wired "
            "in; Δ5's offload-deadline rule (rendering selectable only before first emission) must "
            "land WITH that wiring (roadmap V2)."
        )
    # Compute-only measurement IS allowed (and required by the telemetry contract) — but ONLY
    # run_shadow (byte-only prediction). Pin that the import is specifically the compute entry, so a
    # future broad `from apex_router.proxy_engine.pipeline import *` that could pull renderings is not silently allowed.
    if "from apex_router.proxy_engine.pipeline" in src:
        assert "from apex_router.proxy_engine.pipeline.shadow import run_shadow" in src, (
            "passthrough imports from apex_router.proxy_engine.pipeline but not the compute-only run_shadow — only the "
            "byte-only shadow compute is allowed on the emit path (no rendering imports)."
        )


# ── V3 — json.loads(emitted) == json.loads(original) is blind to duplicate keys ──────────────────
# The oracle used by lossless tests collapses duplicate keys on BOTH sides, so it cannot detect a
# transform that drops a duplicate key. Compaction already rejects duplicate keys at PARSE (safe),
# but the TEST ORACLE is blind — which is why Δ7 needs a lexical detector + a raw-route assertion,
# not a reliance on the round-trip oracle.


def test_v3_roundtrip_oracle_is_blind_to_duplicate_keys():
    dup = '{"a": 1, "a": 2}'
    # a hypothetical lossy emission that dropped the first "a" — the oracle CANNOT tell:
    lossy_emission = '{"a": 2}'
    assert json.loads(dup) == json.loads(lossy_emission), (
        "the round-trip oracle is supposed to be blind here — if this fails, json changed its "
        "last-key-wins semantics and the V3 hole no longer exists."
    )
    # ...yet the two byte strings are obviously different — the property the oracle should protect.
    assert dup != lossy_emission


def test_v3_compaction_rejects_duplicate_keys_at_parse():
    # the transform itself is safe today (fail-open): duplicate-key JSON does not compact; it raises
    # and the pipeline ships raw. Δ7 makes this an explicit, tested detector rather than an
    # incidental parse rejection.
    dup = '{"a": 1, "a": 2, "padding": "' + "x" * 300 + '"}'
    assert compaction.applies(Block(content=dup, tool_name="Read")) is False
