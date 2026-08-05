"""M4 — replay optimizer determinism + proposal emission + A/B. §8.1.

Gate: replay is DETERMINISTic (same corpus+knobs → same score, §11 tuner-isolation), a grid
search emits a real proposal with per-stratum breakdown, and the A/B vs the P0.1 baseline shows
no xl regression for the shared transform set.
"""
from __future__ import annotations

import json

from apex_router.proxy_engine.pipeline.transforms import compaction
from apex_router.proxy_engine.pipeline.transforms.base import Block
from apex_router.proxy_engine.tuner.replay import Request, ScoreResult, grid_search, score


def _real_pipeline(req: Request, knobs: dict):
    """Apply the v1 transforms to a request's content, returning (emitted_bytes, out_tokens,
    diverged, cause). A faithful stand-in for the M3 pipeline over a single block: compaction on
    JSON, else passthrough. Never diverges (lossless) → no transform bust."""
    text = req.content.decode("utf-8", "replace")
    block = Block(content=text, tool_name="Read")
    if compaction.applies(block):
        rendering = compaction.run(block, knobs)
        emitted = rendering.text.encode("utf-8")
        # token count scales with byte reduction (chars/4 estimate)
        out_tokens = max(1, int(req.tokens * len(rendering.text) / max(1, len(text))))
        return emitted, out_tokens, False, "none"
    return req.content, req.tokens, False, "none"


def _corpus() -> list[Request]:
    """A small synthetic corpus: two sessions, each a growing chain of JSON tool results."""
    corpus = []
    for sess in ("A", "B"):
        content = b""
        toks = 0
        for turn in range(4):
            block = json.dumps([{"id": i, "name": f"item{i}", "v": [1, 2, 3]}
                                for i in range(50)], indent=2)
            content += block.encode()
            toks += 1500
            corpus.append(Request(sess, content, toks, ts=1000.0 + turn, model="opus-4-8"))
    return corpus


def test_replay_is_deterministic():
    """Same corpus + knobs → identical score (tuner-isolation, §11)."""
    corpus = _corpus()
    s1 = score(corpus, {}, _real_pipeline)
    s2 = score(corpus, {}, _real_pipeline)
    assert s1.blended_cost == s2.blended_cost
    assert s1.total_cost == s2.total_cost
    assert s1.transform_busts == s2.transform_busts


def _fresh_corpus() -> list[Request]:
    """One request per session (no growing prefix chain) — isolates compression savings from the
    cache interaction."""
    corpus = []
    for i in range(6):
        blk = json.dumps([{"id": j, "name": f"x{j}", "v": [1, 2, 3]} for j in range(50)],
                         indent=2)
        corpus.append(Request(f"sess{i}", blk.encode(), 1500, ts=1000.0, model="opus"))
    return corpus


def test_replay_prices_compression_win_on_fresh_content():
    """On FRESH content (no prefix chain), compaction reduces cost — fewer write tokens."""
    corpus = _fresh_corpus()
    compacted = score(corpus, {}, _real_pipeline)

    def _raw_pipeline(req, knobs):
        return req.content, req.tokens, False, "none"

    raw = score(corpus, {}, _raw_pipeline)
    assert compacted.total_cost < raw.total_cost   # compaction saved money on writes
    assert compacted.reduction_pct() > 0


def test_cachesim_prices_recompaction_break_even():
    """The M4 insight, CORRECTED (Fable review): re-compacting a behind-frontier block in a
    growing prefix changes cached bytes → costs cache reads. Whether it PAYS depends on session
    depth: it loses on a SHALLOW session (few remaining reads to amortize the re-write) but wins
    on a DEEP one (break-even k* ≈ 1.25/(0.1·f) remaining turns). The cachesim prices this
    correctly — so the optimizer's re-compaction decision is calibration-sensitive, not a
    structural 'never'."""
    def _raw_pipeline(req, knobs):
        return req.content, req.tokens, False, "none"

    # SHALLOW growing session: re-compacting every turn costs >= raw (too few reads to amortize)
    shallow = _corpus()  # 4 turns/session
    shallow_compact = score(shallow, {}, _real_pipeline).total_cost
    shallow_raw = score(shallow, {}, _raw_pipeline).total_cost
    assert shallow_compact >= shallow_raw

    # DEEP session: enough remaining reads that a one-time re-compaction amortizes. Model it
    # directly through the sim to prove the crossover exists.
    from apex_router.proxy_engine.tuner.cachesim import CacheSimulator

    def deep_cost(recompact: bool, turns: int = 60) -> float:
        sim = CacheSimulator()
        L = 20000
        sim.request("s", b"X" * (L * 4), L, ts=1000.0)  # establish a big cached prefix
        total = 0.0
        if recompact:  # one re-compaction now: busts, prefix shrinks by f≈0.365
            comp = int(L * 0.635)
            content = b"Z" * (comp * 4)
            total += sim.request("s", content, comp, ts=1001.0,
                                 prev_cached_diverged=True, diverge_cause="transform").cost
            base = comp
        else:
            content = b"X" * (L * 4)
            base = L
        for k in range(turns):
            content += b"Y" * (500 * 4)
            total += sim.request("s", content, base + (k + 1) * 500, ts=1002.0 + k).cost
        return total

    # deep session: the one-time re-compaction amortizes → cheaper than never compacting
    assert deep_cost(recompact=True) < deep_cost(recompact=False)


def test_grid_search_emits_proposal_with_per_stratum():
    """A grid search returns a Proposal with a per-stratum breakdown and zero transform busts
    (observe-only — nothing applied)."""
    corpus = _corpus()
    byte_knobs = {"relevance_threshold": (0.1, 0.5, 0.9)}  # a representative byte-affecting knob
    prop = grid_search(corpus, byte_knobs, _real_pipeline, baseline_knobs={})
    assert isinstance(prop.per_stratum_reduction, dict)
    assert set(prop.per_stratum_reduction) == {"xs", "s", "m", "l", "xl"}
    assert prop.transform_busts == 0  # a proposal that busts the cache is inadmissible
    assert prop.knob_vector is not None


def test_stratum_leverage_flags_uninformative_strata():
    """Fable round-2 diagnostic: a stratum where NO knob moves the objective is flagged
    uninformative — so a 'no regression' result there is inertness, not validation. Prevents
    citing the tuner for xl safety when it has no xl leverage (the exact M4-report error)."""
    # corpus with a compressible m-stratum (JSON) + an xs-stratum with nothing addressable
    corpus = []
    for i in range(3):
        big = json.dumps([{"id": j, "v": [1, 2, 3]} for j in range(400)], indent=2)
        corpus.append(Request(f"m{i}", big.encode(), 10000, ts=1000.0, model="opus"))
        corpus.append(Request(f"xs{i}", b"tiny prose, no structure", 500, ts=1000.0, model="opus"))

    def _knob_pipeline(req, knobs):
        # responds to the `compress` knob so the grid actually varies the objective on m
        if knobs.get("compress", 0) == 1:
            b = Block(content=req.content.decode("utf-8", "replace"), tool_name="Read")
            if compaction.applies(b):
                r = compaction.run(b, knobs)
                out = max(1, int(req.tokens * len(r.text) / max(1, len(req.content))))
                return r.text.encode(), out, False, "none"
        return req.content, req.tokens, False, "none"

    prop = grid_search(corpus, {"compress": (0, 1, 1)}, _knob_pipeline,
                       baseline_knobs={"compress": 0})
    # m has leverage (JSON compacts under the knob); xs does not (nothing to compress)
    assert prop.stratum_leverage["m"] > 0
    assert "xs" in prop.uninformative_strata()


def test_transform_bust_makes_proposal_inadmissible():
    """A pipeline that diverges (transform bust) must be rejected by grid_search — cache safety
    is a wall, not a weight."""
    corpus = _corpus()

    def _busting_pipeline(req, knobs):
        # pretend a knob value causes a divergence within the cached span
        if knobs.get("k", 0) == 9:
            return req.content, req.tokens, True, "transform"
        return req.content, req.tokens, False, "none"

    prop = grid_search(corpus, {"k": (0, 5, 9)}, _busting_pipeline, baseline_knobs={"k": 0})
    # the busting value (9) must not be chosen
    assert prop.knob_vector.get("k") != 9
    assert prop.transform_busts == 0
    assert prop.admissible is True


def test_all_busting_returns_inadmissible_not_silent_baseline():
    """xval #6: when EVERY candidate (incl. baseline) busts, grid_search must flag the proposal
    inadmissible with its real bust count — not silently return the busting baseline as clean."""
    corpus = _corpus()  # warm sessions (turn 2+ have a cached prefix to bust)

    def _always_bust(req, knobs):
        return req.content, req.tokens, True, "transform"

    prop = grid_search(corpus, {"k": (0, 5, 9)}, _always_bust, baseline_knobs={"k": 0})
    assert prop.admissible is False
    assert prop.transform_busts > 0  # surfaced, not hidden


def test_score_result_reduction_by_stratum():
    corpus = _corpus()
    s: ScoreResult = score(corpus, {}, _real_pipeline)
    # our corpus is all ~1500-6000 token requests → s/m strata; xl should be empty (0%)
    assert s.reduction_pct("xl") == 0.0
    assert s.reduction_pct() >= 0.0
