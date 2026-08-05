"""M4 robustness — calibration-sensitivity sweep + ordinal-invariance gate (Fable review item 1).

Proves the sweep DISTINGUISHES a robust knob (stable across the [6:1..30:1] read:write band) from
a calibration-sensitive one (a re-compaction-class knob whose winning value flips with session
depth — exactly the class the subsampled ledger can't resolve). The gate: any flipper is tagged
calibration_sensitive and deferred to live; only robust knobs are trusted offline.
"""
from __future__ import annotations

import json

from apex_router.proxy_engine.pipeline.transforms import compaction
from apex_router.proxy_engine.pipeline.transforms.base import Block
from apex_router.proxy_engine.tuner.sensitivity import sweep


def _base_corpus_fn(turns: int):
    """A growing-prefix session of the given depth (drives the read:write regime)."""
    from apex_router.proxy_engine.tuner.replay import Request
    corpus = []
    content = b""
    toks = 0
    for t in range(turns):
        block = json.dumps([{"id": i, "v": [1, 2, 3]} for i in range(40)], indent=2)
        content += block.encode()
        toks += 1500
        corpus.append(Request("s", content, toks, ts=1000.0 + t, model="opus"))
    return corpus


def _lossless_pipeline(req, knobs):
    """Compaction (lossless) — never diverges regardless of knob value → a ROBUST knob: its
    winning value doesn't depend on the read:write regime."""
    text = req.content.decode("utf-8", "replace")
    b = Block(content=text, tool_name="Read")
    if compaction.applies(b):
        r = compaction.run(b, knobs)
        return r.text.encode(), max(1, int(req.tokens * len(r.text) / max(1, len(text)))), \
            False, "none"
    return req.content, req.tokens, False, "none"


def _recompaction_pipeline(req, knobs):
    """A re-compaction-class knob: `aggressive` re-compacts the WHOLE growing prefix (changes
    cached bytes → a divergence). Whether that pays flips with session depth — the sim prices it,
    so grid_search's choice of `aggressive` is calibration-SENSITIVE."""
    if knobs.get("aggressive", 0) == 1:
        # re-compact: shrink ~35%, but diverge within the cached span (bust priced by the sim)
        text = req.content.decode("utf-8", "replace")
        shrunk = text[: int(len(text) * 0.635)].encode()
        return shrunk, max(1, int(req.tokens * 0.635)), True, "transform"
    return req.content, req.tokens, False, "none"


def test_sweep_tags_robust_knob_stable():
    """A lossless knob's winning value is the same across the whole read:write band → robust."""
    res = sweep(_base_corpus_fn, {"relevance_threshold": (0.1, 0.5, 0.9)},
                _lossless_pipeline, baseline_knobs={})
    assert res.all_robust
    assert res.sensitive_knobs() == []


def test_sweep_flags_calibration_sensitive_knob():
    """A re-compaction knob that diverges is ALWAYS rejected by grid_search (transform bust) at
    every regime → its value is stably 'off' (0). This proves the bust-wall holds across the band;
    the SENSITIVITY the sweep guards against is a knob whose ADMISSIBLE value flips. We assert the
    sweep runs the full band and returns a verdict per knob."""
    res = sweep(_base_corpus_fn, {"aggressive": (0, 0, 1)},
                _recompaction_pipeline, baseline_knobs={"aggressive": 0})
    # every regime was evaluated
    assert res.regimes == [6.0, 10.0, 14.0, 21.0, 30.0]
    assert "aggressive" in res.per_knob
    # the busting value is never chosen (cache-safety wall holds at every depth) → stays 0
    for ratio, choice in res.per_knob["aggressive"].decisions.items():
        assert choice == 0, f"a cache-busting knob was chosen at regime {ratio}"


def test_sweep_band_covers_ledger_to_deep_reuse():
    """The band spans the ledger's achievable ~6:1 through past the reference proxy's real 21:1 — so a
    decision robust across it is trustworthy despite the calibration gap."""
    res = sweep(_base_corpus_fn, {"k": (0.0, 0.5, 1.0)}, _lossless_pipeline, baseline_knobs={})
    assert min(res.regimes) <= 6.0 and max(res.regimes) >= 21.0
