"""Regime + project labeling on replay Requests (corpus v2, post-reconciliation).

Lesson #9 first-build enforcement: a corpus is a SAMPLE OF A REGIME, and every number inherits the
regime of the corpus that produced it. The day-1 zero came from compiling on ONE project whose
canonical corpus turned out to be deep-session traffic, while the runtime meets a mix of regimes.
So evidence must be regime-LABELED at the row, and admission conditioned per regime/project (the
minimal M7 pulled forward), rather than a runtime regime classifier (regime is unknowable at turn 1).

Regime is a MEASURED session property tied to the admission band, NOT a project-name assumption
(measurement refuted "the reference proxy=burst, ml=conversational": the reference proxy is deep, ml is bimodal):
  - single         : 1 turn        — R=0, compression can never pay.
  - shallow        : 2..12 turns   — below the band's turn-depth (band [6,30] r:w -> 2r+1 = 13..61).
  - conversational : >=13 turns    — within/above the admitted regime; compression economics apply.

`project` is a SEPARATE label carried end-to-end (Δ10 partition identity), so evidence can be sliced
by either axis without conflating them.
"""
from __future__ import annotations

import json

from fixtures.build_replay_corpus import build_corpus, session_regime, transcript_to_requests


def _write(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _turns(n):
    """n user→assistant exchanges as raw transcript records."""
    recs = []
    for i in range(n):
        recs.append({"type": "user", "timestamp": f"2026-07-13T10:{i:02d}:00Z",
                     "message": {"content": [{"type": "tool_result", "content": f"u{i} " * 50}]}})
        recs.append({"type": "assistant", "timestamp": f"2026-07-13T10:{i:02d}:30Z",
                     "message": {"model": "opus", "usage": {"input_tokens": 100},
                                 "content": [{"type": "text", "text": f"a{i}"}]}})
    return recs


def test_session_regime_thresholds():
    """The band-tied classifier: 1 turn=single, 2..12=shallow, >=13=conversational."""
    assert session_regime(1) == "single"
    assert session_regime(2) == "shallow"
    assert session_regime(12) == "shallow"
    assert session_regime(13) == "conversational"
    assert session_regime(2895) == "conversational"


def test_requests_carry_regime_and_project(tmp_path):
    """A built Request is tagged with its session's regime and its project — both at the row, so a
    downstream slice can condition on either without re-deriving from raw transcripts."""
    proj = tmp_path / "-Users-x-dev-demo"
    proj.mkdir()
    _write(proj / "deep.jsonl", _turns(20))     # 20 turns -> conversational
    reqs = transcript_to_requests(str(proj / "deep.jsonl"), project="-Users-x-dev-demo")
    assert reqs, "expected requests from a 20-turn transcript"
    assert all(r.project == "-Users-x-dev-demo" for r in reqs)
    assert all(r.regime == "conversational" for r in reqs)


def test_regime_is_per_session_not_per_project(tmp_path):
    """A single project with a shallow AND a deep session yields BOTH regimes — regime is a session
    property, not a project constant (the ml-is-bimodal finding)."""
    proj = tmp_path / "-Users-x-dev-bimodal"
    proj.mkdir()
    _write(proj / "burst.jsonl", _turns(3))      # 3 turns -> shallow
    _write(proj / "deep.jsonl", _turns(40))      # 40 turns -> conversational
    corpus, _stats = build_corpus("-Users-x-dev-bimodal", projects_root=str(tmp_path),
                                  min_turns=1)
    regimes = {r.regime for r in corpus}
    assert regimes == {"shallow", "conversational"}
    # and every row still knows its project
    assert {r.project for r in corpus} == {"-Users-x-dev-bimodal"}
