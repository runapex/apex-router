"""Gutter-strip transform — lossy file_read compression (F2 split, roadmap T1-P).

Measured on 3,014 real file_read blocks: lossless gutter-CANONICALIZE saves ~0% (the Read-tool
gutter is already minimal `N\\t`), while gutter-STRIP (dropping the `<ws><number><sep>` gutter) saves
a stable ~9-10% in tokens. Strip is LOSSY — line numbers are functional entities (view_range /
grep -n / the model reasoning in "line N"), so the transform:
  - keeps every line's CONTENT verbatim (only the gutter is dropped);
  - carries the ORIGINAL for CCR retrieval (fidelity = ccr_retrieval);
  - emits ONE counted+located marker announcing the strip (entity floor: cardinality + a ccr ref
    survive on the wire), so the model knows numbers were removed and can retrieve the guttered
    original.

Because it's ccr_retrieval, decide()'s Δ1 capability gate keeps it INERT until a resolver is
registered, and the compiler refuses to SIGN it without behavioral evidence (the Δ14 gate). This
tests the pure transform; admission is priced separately.
"""
from __future__ import annotations

from apex_router.proxy_engine.pipeline.transforms import file_read_strip
from apex_router.proxy_engine.pipeline.transforms.base import Block


def _guttered(n_lines: int) -> str:
    """A cat -n / Read-tool style block: `<num>\\t<code>` per line."""
    return "\n".join(f"{i}\tdef func_{i}(x):  # line {i}" for i in range(1, n_lines + 1))


def test_applies_to_gutter_blocks_only():
    """Fires on a line-number-guttered block; not on plain prose or already-gutterless text."""
    assert file_read_strip.applies(Block(content=_guttered(20), tool_name="tool_result"))
    assert not file_read_strip.applies(
        Block(content="just some prose\nwith no line numbers\nat all", tool_name="tool_result"))


def test_strips_the_gutter_keeps_content_verbatim():
    """Every line's CONTENT survives byte-identical; only the `<num><sep>` gutter is gone."""
    content = _guttered(20)
    r = file_read_strip.run(Block(content=content, tool_name="tool_result"), {})
    assert r.fidelity == "ccr_retrieval"
    for i in range(1, 21):
        assert f"def func_{i}(x):  # line {i}" in r.text   # content verbatim
    # the leading "N\t" gutter is gone from the body (no "\n<digit>\t" line starts remain)
    import re
    assert not re.search(r"(?m)^\d+\t", r.text)


def test_emits_a_counted_ccr_marker():
    """One marker carries the stripped-line count + a ccr:// ref (entity floor: cardinality + locator
    on the wire), so the model knows the gutter was removed and can retrieve the original."""
    content = _guttered(50)
    r = file_read_strip.run(Block(content=content, tool_name="tool_result"), {})
    assert "ccr://" in r.text
    assert "50" in r.text          # the stripped-line count is announced
    assert r.original == content    # the guttered original is carried for retrieval


def test_reduces_tokens():
    """The stripped rendering is byte-shorter than the original (the point of the transform)."""
    content = _guttered(60)
    r = file_read_strip.run(Block(content=content, tool_name="tool_result"), {})
    assert len(r.text) < len(content)


def test_run_raises_when_not_guttered_fail_open():
    """A block with no gutter raises (the pipeline fail-open ships the original) — never a silent
    no-op emission that claims a lossy transform on unstrippable content."""
    import pytest
    with pytest.raises(Exception):
        file_read_strip.run(Block(content="no gutters here at all", tool_name="tool_result"), {})


def test_resolver_reconstructs_the_gutter():
    """The stub resolver serves the carried original back, so an agent can recover exact line numbers —
    the ccr_retrieval contract (the transform is only safe because this reconstruction exists)."""
    content = _guttered(30)
    r = file_read_strip.run(Block(content=content, tool_name="tool_result"), {})
    ref = file_read_strip.ccr_ref(content)
    assert ref in r.text
    # the resolver maps that ref back to the guttered original
    served = file_read_strip.resolve_original(content)
    assert served == content
    assert "1\tdef func_1" in served  # line numbers are back
