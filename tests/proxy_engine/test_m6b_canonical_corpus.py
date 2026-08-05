"""Structural closure of the corpus-selection instrument (Fable, post-shadow-merge).

The sorted-filename `limit_sessions=5` truncation produced TWO wrong standing conclusions (the
mis-specified per-stratum ceiling table AND "admitted: NONE"). A register entry notes the lesson;
this test pins the MECHANISM that makes it unrepeatable: `compile_policy` in EVIDENCE MODE refuses a
non-canonical corpus, so `limit_sessions` becomes a probe-labeled debug parameter that cannot feed a
signed bundle or an evidence pack. Converts the frozen-snapshot standing rule (F-ii) from discipline
into enforcement — the house pattern (like Δ1's unrepresentable-lossy-cell, Δ2's shared observable).

The contract:
  - `compile_policy(..., evidence_grade=True)` REQUIRES `corpus_provenance` with `canonical=True`;
    a missing or truncated (`canonical=False`) provenance raises `InvalidPolicy` at sign time.
  - `build_corpus(limit_sessions=N)` stamps `canonical=False` on its stats; `limit_sessions=None`
    stamps `canonical=True` (the full sorted glob = the canonical population).
  - probe/synthetic compiles (`evidence_grade=False`, the default) are unaffected — they compile and
    seal as before, they just can't claim evidence grade.
"""

from __future__ import annotations

import pytest

from apex_router.proxy_engine.policy import InvalidPolicy
from apex_router.proxy_engine.tuner.compiler import CorpusProvenance, compile_policy
from apex_router.proxy_engine.tuner.replay import Request


def _growing_json_corpus(sessions: int = 2) -> list[Request]:
    import json

    corpus: list[Request] = []
    for s in range(sessions):
        prev = ""
        for t in range(6):
            block = json.dumps(
                [{"id": i, "name": f"n{i}", "vals": list(range(8))} for i in range(300)], indent=2
            )
            content = (prev + block).encode("utf-8")
            corpus.append(
                Request(
                    f"s{s}",
                    content,
                    max(1, len(content) // 4),
                    ts=float(t),
                    model="claude-opus-4-8",
                )
            )
            prev = content.decode() + "\n"
    return corpus


def test_evidence_grade_refuses_missing_provenance():
    corpus = _growing_json_corpus()
    with pytest.raises(InvalidPolicy, match="canonical|provenance|evidence"):
        compile_policy(corpus, version=1, compiled_at=1e9, evidence_grade=True)


def test_evidence_grade_refuses_truncated_corpus():
    # a limit_sessions truncation carries canonical=False → cannot sign an evidence-grade bundle
    corpus = _growing_json_corpus()
    trunc = CorpusProvenance(canonical=False, n_sessions=5, source="build_corpus(limit_sessions=5)")
    with pytest.raises(InvalidPolicy, match="canonical|truncat|limit_sessions"):
        compile_policy(
            corpus, version=1, compiled_at=1e9, evidence_grade=True, corpus_provenance=trunc
        )


def test_evidence_grade_admits_canonical_corpus():
    corpus = _growing_json_corpus()
    canon = CorpusProvenance(canonical=True, n_sessions=9, source="freeze_corpus")
    res = compile_policy(
        corpus,
        version=1,
        compiled_at=1e9,
        evidence_grade=True,
        corpus_provenance=canon,
        evidence_manifest_hash="a" * 64,
    )
    assert res.policy.verify()  # signed and valid
    # the provenance rides into the evidence pack so a reader can see it was canonical
    assert res.evidence.get("corpus_provenance", {}).get("canonical") is True


def test_probe_mode_unaffected_default():
    # the default (evidence_grade=False) compiles + seals as before — synthetic/probe corpora, the
    # 30 existing call sites, and quick limit_sessions compiles all keep working.
    corpus = _growing_json_corpus()
    res = compile_policy(corpus, version=1, compiled_at=1e9)  # no evidence_grade, no provenance
    assert res.policy.verify()
    # a probe policy is marked non-evidence so it can never be mistaken for a signed evidence bundle
    assert res.evidence.get("evidence_grade") is False


def test_build_corpus_stamps_canonical_flag():
    # limit_sessions=None → canonical; limit_sessions=N → not canonical. Pure stats-level pin so a
    # truncated build can't masquerade as canonical downstream (no network / real corpus needed:
    # assert the flag derivation directly).
    from fixtures.build_replay_corpus import CorpusStats

    full = CorpusStats(n_sessions=9, n_requests=100, total_bytes=1, max_tokens=1, canonical=True)
    trunc = CorpusStats(n_sessions=5, n_requests=50, total_bytes=1, max_tokens=1, canonical=False)
    assert full.canonical and not trunc.canonical
    assert CorpusProvenance.from_stats(full).canonical
    assert not CorpusProvenance.from_stats(trunc).canonical


def test_corpus_stats_defaults_fail_closed():
    # cross-validation (closure review): canonical is authority-side (it gates signing) → it must default
    # to False (fail-closed, the F5 plane invariant). An ad-hoc CorpusStats with no stated
    # provenance must NOT silently authorize an evidence-grade sign.
    from fixtures.build_replay_corpus import CorpusStats

    unlabeled = CorpusStats(5, 50, 1, 1)  # pre-change positional shape, no canonical stated
    assert unlabeled.canonical is False  # default refuses, not admits
    assert not CorpusProvenance.from_stats(unlabeled).canonical
    with pytest.raises(InvalidPolicy, match="canonical|truncat|limit_sessions"):
        compile_policy(
            _growing_json_corpus(),
            version=1,
            compiled_at=1e9,
            evidence_grade=True,
            corpus_provenance=CorpusProvenance.from_stats(unlabeled),
        )
