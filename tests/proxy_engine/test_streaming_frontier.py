"""Streaming frontier extractor — memory-bounded corpus profiling (a Ruby service OOM fix).

The batch path `diagnose(build_corpus(...))` holds every request's full growing-prefix `content` in
memory at once: a 6818-turn session sums to ~76 GB of prefix bytes (O(n²)) and OOM-kills the build.
But frontier extraction only needs prev-vs-current, and an append-only transcript is already in ts
order — so frontiers can be streamed with O(final_prefix) memory (~20 MB), never materializing all
prefixes. This extractor MUST be behaviorally identical to the batch path; the contract is pinned by
equivalence against `diagnose` on a fixture, then used where batch OOMs.
"""

from __future__ import annotations

import json

from apex_router.proxy_engine.tuner.composition import diagnose, session_frontiers
from fixtures.build_replay_corpus import (
    build_corpus,
    build_streaming_corpus,
    stream_composition,
    stream_transcript_frontiers,
    transcript_to_frontier_requests,
    transcript_to_requests,
)


def _write(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _session(n, *, kinds):
    """n user→assistant turns; `kinds[i]` picks the user block class for turn i (cycled)."""
    recs = []
    for i in range(n):
        k = kinds[i % len(kinds)]
        if k == "prose":
            block = {"type": "tool_result", "content": f"the analysis shows {i} " * 40}
        elif k == "file_read":
            block = {
                "type": "tool_result",
                "content": "\n".join(f"{j:5d}\tcode line {i}" for j in range(60)),
            }
        else:  # terminal — classify() keys terminal on ANSI escape codes, not "$" prompts
            block = {
                "type": "tool_result",
                "content": f"\x1b[32m$\x1b[0m run {i}\n" + "\x1b[1moutput\x1b[0m\n" * 50,
            }
        recs.append(
            {
                "type": "user",
                "timestamp": f"2026-07-13T10:{i:02d}:00Z",
                "message": {"content": [block]},
            }
        )
        recs.append(
            {
                "type": "assistant",
                "timestamp": f"2026-07-13T10:{i:02d}:30Z",
                "message": {
                    "model": "opus",
                    "usage": {"input_tokens": 100},
                    "content": [{"type": "text", "text": f"reply {i}"}],
                },
            }
        )
    return recs


def test_streaming_frontiers_match_batch_block_for_block(tmp_path):
    """The within-session streaming extractor emits the SAME frontier blocks (bytes) and the SAME
    prefix strata as the batch `session_frontiers` — proven block-for-block, so it can replace the
    O(n²)-memory batch path without changing any downstream tally. This is the fix for the 6818-turn
    session that materialized ~76 GB of growing prefixes and OOM-killed the build."""
    from apex_router.proxy_engine.policy import size_stratum_bytes

    proj = tmp_path / "-Users-x-dev-demo"
    proj.mkdir()
    _write(proj / "a.jsonl", _session(25, kinds=["prose", "file_read", "terminal"]))

    reqs = transcript_to_requests(str(proj / "a.jsonl"))
    batch = [
        (fr.block, size_stratum_bytes(len(fr.req.content)))
        for fr in session_frontiers(reqs)
        if fr.block
    ]
    stream = [
        (block, size_stratum_bytes(prefix_len))
        for block, prefix_len, _regime in stream_transcript_frontiers(str(proj / "a.jsonl"))
        if block
    ]
    assert stream == batch, "streaming frontier extraction diverged from batch session_frontiers"


def test_streaming_composition_matches_batch(tmp_path):
    """The streaming profiler's per-(class×stratum) block counts and bytes match the batch
    `diagnose(build_corpus(...))` exactly on the same transcripts."""
    proj = tmp_path / "-Users-x-dev-demo"
    proj.mkdir()
    _write(proj / "a.jsonl", _session(20, kinds=["prose", "file_read", "terminal"]))
    _write(proj / "b.jsonl", _session(15, kinds=["file_read", "prose"]))

    corpus, _ = build_corpus("-Users-x-dev-demo", projects_root=str(tmp_path), min_turns=1)
    batch = diagnose(corpus).snapshot()["cells"]

    stream = stream_composition("-Users-x-dev-demo", projects_root=str(tmp_path), min_turns=1)
    stream_cells = stream["cells"]

    assert stream_cells == batch, f"streaming != batch\n stream={stream_cells}\n batch={batch}"


def test_compact_frontier_corpus_matches_batch_composition_without_growing_prefixes(tmp_path):
    proj = tmp_path / "-Users-x-dev-demo"
    proj.mkdir()
    path = proj / "deep.jsonl"
    _write(path, _session(50, kinds=["prose", "file_read", "terminal"]))

    batch, _ = build_corpus("-Users-x-dev-demo", projects_root=str(tmp_path), min_turns=1)
    compact, _ = build_streaming_corpus(
        "-Users-x-dev-demo", projects_root=str(tmp_path), min_turns=1
    )
    assert diagnose(compact).snapshot() == diagnose(batch).snapshot()
    assert all(r.content == b"" and r.frontier_block is not None for r in compact)
    # Batch retains every growing prefix; compact retains each frontier once.
    assert sum(len(r.frontier_block or b"") for r in compact) < sum(len(r.content) for r in batch)


def test_compact_frontier_rows_match_batch_frontiers_and_prefix_tokens(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, _session(12, kinds=["prose", "file_read"]))
    batch = session_frontiers(transcript_to_requests(str(path)))
    compact = session_frontiers(transcript_to_frontier_requests(str(path)))
    assert [f.block for f in compact] == [f.block for f in batch]
    assert [f.prefix_tokens for f in compact] == [f.prefix_tokens for f in batch]


def test_streaming_carries_regime_slices(tmp_path):
    """The streaming profiler buckets composition BY REGIME (the evidence-slice the recompile needs),
    and regime is the same band-tied per-session classification as the batch labels."""
    proj = tmp_path / "-Users-x-dev-demo"
    proj.mkdir()
    _write(proj / "deep.jsonl", _session(30, kinds=["prose", "file_read"]))  # conversational
    _write(proj / "shallow.jsonl", _session(4, kinds=["terminal"]))  # shallow

    stream = stream_composition("-Users-x-dev-demo", projects_root=str(tmp_path), min_turns=1)
    by_regime = stream["by_regime"]

    assert set(by_regime) == {"conversational", "shallow"}
    # the deep session is prose+file_read; the shallow one is terminal — regimes don't bleed
    assert "prose" in by_regime["conversational"]["classes"]
    assert by_regime["shallow"]["classes"].get("terminal", 0) > 0
    assert "terminal" not in by_regime["conversational"]["classes"]
