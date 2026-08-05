"""gutter_floor_v1 — the Δ13 entity-floor validator for file_read gutter-strip.

A pure function `(original, rendering_text) -> (ok, reason)` that structurally verifies the strip
transform preserved fidelity, so a lossy file_read cell can only be SIGNED with a validator that
catches a broken strip. Properties (the gutter-strip analog of json_entity_floor's list):
  - CONTENT retained verbatim — every stripped line's content survives on the wire;
  - COUNT survives — the marker announces the true number of stripped gutters;
  - LOCATOR present — a ccr:// ref is on the wire (so the guttered original is retrievable).

Adversarial requirement (roadmap §Δ13): the validator must catch a DELIBERATELY BROKEN strip variant,
not just bless the real one — mutation-test the validator.
"""
from __future__ import annotations

from apex_router.proxy_engine.pipeline.transforms import file_read_strip
from apex_router.proxy_engine.pipeline.transforms.base import Block
from apex_router.proxy_engine.pipeline.validators import gutter_floor_v1


def _guttered(n):
    return "\n".join(f"{i}\tdef func_{i}(x): return x * {i}" for i in range(1, n + 1))


def test_passes_the_real_strip():
    """The genuine gutter-strip rendering passes all three properties."""
    content = _guttered(30)
    r = file_read_strip.run(Block(content=content, tool_name="tool_result"), {})
    ok, reason = gutter_floor_v1(content, r.text)
    assert ok, reason


def test_catches_dropped_content_line():
    """A broken strip that DROPS a content line (not just the gutter) fails verbatim-retention."""
    content = _guttered(30)
    r = file_read_strip.run(Block(content=content, tool_name="tool_result"), {})
    # mutate: delete a content line from the rendering
    broken = "\n".join(ln for ln in r.text.split("\n") if "func_15(" not in ln)
    ok, reason = gutter_floor_v1(content, broken)
    assert not ok
    assert "verbatim" in reason.lower() or "content" in reason.lower()


def test_catches_wrong_count():
    """A marker announcing the wrong stripped-line count fails the count property."""
    content = _guttered(30)
    r = file_read_strip.run(Block(content=content, tool_name="tool_result"), {})
    broken = r.text.replace("stripped: 30 lines", "stripped: 3 lines")
    ok, reason = gutter_floor_v1(content, broken)
    assert not ok
    assert "count" in reason.lower()


def test_catches_missing_locator():
    """A rendering with no ccr:// ref fails the locator property (the original can't be retrieved)."""
    content = _guttered(30)
    r = file_read_strip.run(Block(content=content, tool_name="tool_result"), {})
    import re
    broken = re.sub(r"ccr://[0-9a-f]+", "REDACTED", r.text)
    ok, reason = gutter_floor_v1(content, broken)
    assert not ok
    assert "locator" in reason.lower() or "ccr" in reason.lower()


def test_fails_open_on_garbage():
    """Given nonsense (not a strip rendering at all), the validator returns not-ok rather than raising
    — the caller ships raw on a failed floor, never crashes."""
    ok, _reason = gutter_floor_v1("original", "totally unrelated text with no marker")
    assert not ok
