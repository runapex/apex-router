"""Δ6 + Δ7 — fidelity taxonomy v2 + JSON lexical hardening (roadmap §1).

Δ6: rename the fidelity enum to the honest taxonomy —
    lossless    → wire_canonicalization   (bytes change; defined semantic equivalence; no inverse)
    recoverable → external_retrieval       (astgrep: recovery needs a VERSIONED external object —
                                            the repo file at the ref state — NOT self-contained)
    lossy_ccr   → ccr_retrieval            (dropped from wire; original persisted in CCR)
    (self_contained is reserved — no transform is truly self-contained today.)
    Old strings are rejected at bundle load (one binning migration while no G baseline exists).

Δ7: JSON lexical detectors route hazards to RAW (fail-open) instead of trusting the round-trip
    oracle — duplicate keys (V3 hole), exotic number lexemes (1 vs 1.0 vs 1e0 vs -0); and marker
    offsets are UTF-8 BYTE offsets over the original wire bytes (v1 used code-point len(s)).
"""

from __future__ import annotations

import json

import pytest

from apex_router.proxy_engine.pipeline.transforms import astgrep, compaction, json_crush, terminal
from apex_router.proxy_engine.pipeline.transforms.base import Block

# ── Δ6: taxonomy v2 ───────────────────────────────────────────────────────────────────────────────


def test_fidelity_taxonomy_v2_names():
    assert compaction.fidelity == "wire_canonicalization"
    assert terminal.fidelity == "wire_canonicalization"
    assert astgrep.fidelity == "external_retrieval"  # recovery needs the repo file @ ref state
    assert json_crush.fidelity == "ccr_retrieval"


def test_fidelity_literal_is_the_v2_set():
    import typing

    from apex_router.proxy_engine.pipeline.transforms.base import Fidelity

    args = set(typing.get_args(Fidelity))
    assert args == {
        "wire_canonicalization",
        "self_contained",
        "external_retrieval",
        "ccr_retrieval",
    }


def test_bundle_rejects_legacy_fidelity_string():
    """A rule sealed with an old fidelity_class ('lossy_ccr') must fail load — the taxonomy bump is
    a binning migration, so a stale artifact is incomparable and refused."""
    from apex_router.proxy_engine.policy import (
        CONTENT_CLASSES,
        ClassRule,
        ExpectedReport,
        InvalidPolicy,
        PolicyVersion,
        T2Policy,
        transform_digest,
    )

    strata = ("xs", "s", "m", "l", "xl")
    raw = ClassRule(transform=None, enabled=False, min_bytes=1 << 30, ratio_floor=0.0)
    rules = {c: {st: raw for st in strata} for c in CONTENT_CLASSES}
    rules["json"]["xl"] = ClassRule(
        transform="json_crush",
        enabled=True,
        min_bytes=200,
        ratio_floor=0.1,
        transform_version=transform_digest("json_crush"),
        validator_id="v",
        validator_version="1",
        fidelity_class="lossy_ccr",
    )  # ← legacy string
    pol = PolicyVersion(
        version=1,
        compiled_at=1.0,
        compiler_hash="h",
        corpus_hash="c",
        band=(6.0, 30.0),
        rules=rules,
        t2=T2Policy(consolidate_on=("ttl",), min_turn_count=5),
        expected=ExpectedReport(0.0, {}),
    ).sealed()
    with pytest.raises(InvalidPolicy):
        PolicyVersion.load_verified(pol.to_dict())


# ── Δ7: JSON lexical detectors → raw (fail-open) ──────────────────────────────────────────


def _big(obj_str_inner: str) -> str:
    # a JSON object big enough to clear json_crush's min-array / compaction min_bytes gates
    return '{"data": [' + obj_str_inner + '], "pad":"' + "x" * 300 + '"}'


def test_duplicate_keys_route_raw_not_compacted():
    """The V3 hole, closed: duplicate-key JSON must be detected and shipped RAW — the round-trip
    oracle can't see the loss, so we refuse to transform rather than trust it."""
    dup = '{"a": 1, "a": 2, "pad": "' + "y" * 300 + '"}'
    assert compaction.applies(Block(content=dup, tool_name="Read")) is False
    assert json_crush.applies(Block(content=dup, tool_name="Read")) is False


def test_exotic_number_lexemes_route_raw():
    """LEXEME-CHANGING numbers (`1e0`→1.0, `-0`→0, `1E5`→100000.0, `2.0e-3`→0.002) must route raw:
    the value is preserved but the BYTES the model reads change, a fidelity risk (Δ7)."""
    for lexeme in ("1e0", "-0", "1E5", "2.0e-3"):
        blob = '{"n": ' + lexeme + ', "pad":"' + "z" * 300 + '"}'
        assert compaction.applies(Block(content=blob, tool_name="Read")) is False, (
            f"lexeme-changing number {lexeme!r} should route raw (re-serialization normalizes it)"
        )


def test_lexeme_stable_numbers_still_compact():
    """Control (negative): numbers that re-emit BYTE-IDENTICALLY (`1`, `1.0`, `42`, `3.14`) must
    still compact — the detector keys on lexeme CHANGE, not on 'is a number', so the common case is
    untouched."""
    for stable in ("1", "1.0", "42", "3.14", "-5", "100"):
        blob = '{"n": ' + stable + ', "s": "hi", "pad":  "' + "q" * 300 + '"}'
        assert compaction.applies(Block(content=blob, tool_name="Read")) is True, (
            f"lexeme-stable number {stable!r} re-emits identically and should still compact"
        )


# ── Δ7: marker offsets are UTF-8 byte offsets over the original ────────────────────────────


def test_leaf_marker_offsets_are_utf8_bytes_for_multibyte_leaf():
    """A leaf with multibyte chars (emoji/CJK) truncated → the marker's count and #range must be
    UTF-8 BYTE offsets into the original, so a resolver fetching original[lo:hi] gets the span."""
    # 400 CJK chars = 1200 UTF-8 bytes; each char is 3 bytes. Over the 200-byte-ish leaf budget.
    leaf = "中" * 400
    blob = json.dumps([{"id": i, "t": leaf} for i in range(60)], ensure_ascii=False)
    r = json_crush.run(Block(content=blob, tool_name="Read"), {})
    import re

    m = re.search(r"elided (\d+) of (\d+) bytes · ccr://[0-9a-f]+#(\d+)-(\d+)", r.text)
    assert m, f"no leaf marker found in: {r.text[:200]}"
    n_elided, m_total, lo, hi = (int(m.group(i)) for i in range(1, 5))
    # M must be the original leaf's UTF-8 BYTE length (1200), not its code-point length (400)
    assert m_total == len(leaf.encode("utf-8")), (
        f"marker total {m_total} != UTF-8 byte length {len(leaf.encode('utf-8'))} — offsets "
        "are still code points (Δ7 not applied)"
    )
    assert hi == m_total and lo < hi


def test_leaf_truncation_cuts_on_a_valid_char_boundary():
    """Even with byte offsets, the emitted prefix must be a valid string (never a split multibyte
    char) — the visible truncation cuts on a char boundary, the OFFSETS are bytes."""
    leaf = "世界" * 300  # 6 bytes per repeat
    blob = json.dumps([{"id": i, "t": leaf} for i in range(60)], ensure_ascii=False)
    r = json_crush.run(Block(content=blob, tool_name="Read"), {})
    json.loads(r.text)  # must still parse → no broken char emitted
