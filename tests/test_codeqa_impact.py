"""codeqa Phase 0 (rev 2) — verify the citations the MODEL EMITTED, and log citation validity.

rev 1 was REJECTED by Codex xval: it verified `answer.chunks` (everything retrieved), so 'current'
was near-predetermined and digest drift could never move the metric. rev 2 parses the file:line
citations the model emitted in `answer.text` and classifies each grounded / stale / hallucinated
(hallucinated = the model cited a location it was never given — the failure the old design missed).
"""
from __future__ import annotations

import json

from apex_router.codeqa.impact import (
    EmittedCite,
    ImpactRecord,
    aggregate_grounding,
    parse_emitted_citations,
    verify_emitted_citation,
    write_impact,
)
from apex_router.codeqa.retriever import Chunk


def _chunk(file, start, end):
    return Chunk(file=file, start=start, end=end, text="", why="")


# ---------- parsing the citations the model actually emitted ----------

def test_parse_emitted_citations_pulls_file_line_refs():
    text = ("The floor is derived in apex/readout/doctor.py:34, used by "
            "prefix_instability_alarm (apex/readout/doctor.py:378-405). See also tests/t.py:9.")
    cites = parse_emitted_citations(text)
    assert EmittedCite("apex/readout/doctor.py", 34, 34) in cites
    assert EmittedCite("apex/readout/doctor.py", 378, 405) in cites
    assert EmittedCite("tests/t.py", 9, 9) in cites


def test_parse_ignores_prose_that_is_not_a_file_cite():
    # "line 5", a "3:1 ratio", and a host:port must NOT be scooped up as file:line citations
    # (Codex xval F5: "127.0.0.1:9000" was parsed — the final segment must be an alpha-initial ext).
    cites = parse_emitted_citations("a 3:1 ratio on line 5; the proxy is at 127.0.0.1:9000")
    assert cites == []


def test_parse_handles_en_dash_ranges(tmp_path):
    # Codex xval F5: "a.py:10–20" (en-dash) was silently truncated to line 10.
    cites = parse_emitted_citations("see a.py:10–20 for detail")
    assert cites == [EmittedCite("a.py", 10, 20)]


# ---------- classifying an emitted cite: grounded / stale / hallucinated ----------

def test_emitted_cite_grounded_when_supplied_and_line_exists(tmp_path):
    (tmp_path / "a.py").write_text("l1\nl2\nl3\nl4\nl5\n")
    chunks = [_chunk("a.py", 1, 5)]
    cite = EmittedCite("a.py", 3, 3)
    assert verify_emitted_citation(tmp_path, cite, chunks) == "grounded"


def test_emitted_cite_hallucinated_when_not_in_any_retrieved_chunk(tmp_path):
    # The model cited a file:line that retrieval NEVER supplied — invented. This is the failure the
    # rev-1 (verify-retrieval) design structurally could not catch.
    (tmp_path / "a.py").write_text("l1\nl2\n")
    chunks = [_chunk("a.py", 1, 2)]
    cite = EmittedCite("made_up.py", 10, 10)
    assert verify_emitted_citation(tmp_path, cite, chunks) == "hallucinated"


def test_emitted_cite_stale_when_line_past_eof(tmp_path):
    # Supplied by retrieval, but the cited line no longer exists (file shrank in the live tree).
    (tmp_path / "a.py").write_text("l1\nl2\n")  # only 2 lines now
    chunks = [_chunk("a.py", 1, 40)]           # retrieval thought it had 40
    cite = EmittedCite("a.py", 30, 30)
    assert verify_emitted_citation(tmp_path, cite, chunks) == "stale"


def test_emitted_cite_stale_when_file_gone(tmp_path):
    chunks = [_chunk("gone.py", 1, 5)]
    cite = EmittedCite("gone.py", 2, 2)
    assert verify_emitted_citation(tmp_path, cite, chunks) == "stale"


def test_grounded_verdict_is_a_location_check_not_a_semantic_one(tmp_path):
    # Honesty limit (documented): 'grounded' means the LOCATION is real + was supplied — NOT that the
    # code there still means what the answer claims. A changed body at the same line is still grounded.
    (tmp_path / "a.py").write_text("def totally_different():\n    pass\n")
    chunks = [_chunk("a.py", 1, 2)]
    assert verify_emitted_citation(tmp_path, EmittedCite("a.py", 1, 1), chunks) == "grounded"


def test_basename_collision_is_not_grounded(tmp_path):
    # Codex xval F2: a chunk for src/x.py must NOT ground a cite to other/x.py (same basename, diff
    # file). _same_file requires a trailing-path-segment match, not a bare basename.
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "x.py").write_text("l1\nl2\n")
    chunks = [_chunk("src/x.py", 1, 5)]
    assert verify_emitted_citation(tmp_path, EmittedCite("other/x.py", 1, 1), chunks) == "hallucinated"


def test_over_wide_span_is_not_grounded(tmp_path):
    # Codex xval F2: a cite spanning far past the chunk (a.py:1-999999 over a 5-line chunk) must NOT
    # be grounded — full containment is required, not mere overlap.
    (tmp_path / "a.py").write_text("l1\nl2\nl3\nl4\nl5\n")
    chunks = [_chunk("a.py", 1, 5)]
    assert verify_emitted_citation(tmp_path, EmittedCite("a.py", 1, 999999), chunks) == "hallucinated"


def test_reversed_range_is_not_grounded(tmp_path):
    # Codex xval F2: a malformed reversed range (20-10) must not pass as supplied.
    (tmp_path / "a.py").write_text("l1\nl2\nl3\n")
    chunks = [_chunk("a.py", 1, 30)]
    assert verify_emitted_citation(tmp_path, EmittedCite("a.py", 20, 10), chunks) == "hallucinated"


# ---------- impact record + NO content (a REAL canary this time) ----------

def test_impact_record_tallies_the_three_verdicts():
    rec = ImpactRecord(
        ts=1.0, repo="apex", git_head="abc", question_len=1, n_chunks=3,
        citations=[{"cite": "a.py:1", "verdict": "grounded"},
                   {"cite": "b.py:2", "verdict": "stale"},
                   {"cite": "c.py:3", "verdict": "hallucinated"}],
        cached_tokens=1, prompt_tokens=1, latency_ms=1, digest_commits_behind=0)
    assert rec.grounding() == {"grounded": 1, "stale": 1, "hallucinated": 1}


def test_deliver_impact_record_leaks_no_question_or_source_content(tmp_path):
    # REAL canary (Codex xval twice-flagged the vacuous version): drive the actual deliver() path
    # with a canary QUESTION and a canary SOURCE line, then assert the emitted record contains
    # NEITHER anywhere — this exercises the real content boundary, not a hand-built record.
    from apex_router.codeqa.deliver import deliver
    from apex_router.codeqa.retriever import RepoConfig

    Q_CANARY = "SECRETQUESTION_zzz"
    SRC_CANARY = "SECRETSOURCE_yyy"
    (tmp_path / "a.py").write_text(f"# {SRC_CANARY}\nl2\nl3\n")
    chunks = [_chunk("a.py", 1, 3)]
    # the answer text embeds the source canary (as a real answer would quote code) and cites a.py:1
    answer = _FakeAnswer(f"the code says {SRC_CANARY}, see a.py:1", chunks)

    cfg = RepoConfig(name="apex", root=tmp_path, language="python", digest=None,
                     index={"kind": "none"}, search_globs=["**"], exclude_globs=[],
                     code_exts=[".py"], definition_patterns=[])
    log = tmp_path / "impact.jsonl"
    orig = RepoConfig.load
    RepoConfig.load = staticmethod(lambda name: cfg)
    try:
        deliver("apex", Q_CANARY, impact_log=log, ask_fn=lambda r, q, **k: answer,
                githead_fn=lambda root: "h", behind_fn=lambda c: 0, clock=lambda: 1.0)
    finally:
        RepoConfig.load = orig

    raw = log.read_text()
    assert Q_CANARY not in raw, "the question string must never reach the impact log"
    assert SRC_CANARY not in raw, "source/answer text must never reach the impact log"
    obj = json.loads(raw.strip())
    assert obj["question_len"] == len(Q_CANARY)  # only the LENGTH is recorded
    assert obj["ts_iso"] == ""  # the deliver() path never populates the free string field


# ---------- aggregation: citation validity + per-record drift pairing ----------

def test_aggregate_pairs_validity_with_drift_for_every_record(tmp_path):
    log = tmp_path / "impact.jsonl"
    # record A: fresh digest, all grounded; record B: 8 behind, one hallucinated
    for behind, verdicts in [(0, ["grounded", "grounded"]), (8, ["grounded", "hallucinated"])]:
        write_impact(log, ImpactRecord(
            ts=1.0, repo="apex", git_head="h", question_len=1, n_chunks=len(verdicts),
            citations=[{"cite": f"f{i}.py:1", "verdict": v} for i, v in enumerate(verdicts)],
            cached_tokens=0, prompt_tokens=0, latency_ms=0, digest_commits_behind=behind))
    agg = aggregate_grounding(log)
    assert agg["total_citations"] == 4
    assert agg["grounded"] == 3 and agg["hallucinated"] == 1 and agg["stale"] == 0
    assert abs(agg["citation_validity"] - 0.75) < 1e-9
    # EVERY record contributes a (drift, validity) pair — so a correlation is computable (Codex #5).
    pairs = {(r["digest_commits_behind"], r["validity"]) for r in agg["per_record"]}
    assert (0, 1.0) in pairs and (8, 0.5) in pairs


# ---------- delivery orchestration: verify EMITTED cites, seams injected ----------

class _FakeAnswer:
    def __init__(self, text, chunks, cached=8000, prompt=10000):
        self.text = text
        self.chunks = chunks
        self.cached_tokens = cached
        self.prompt_tokens = prompt


def test_deliver_verifies_emitted_cites_not_retrieved_chunks(tmp_path):
    from apex_router.codeqa.deliver import deliver
    from apex_router.codeqa.retriever import RepoConfig

    (tmp_path / "a.py").write_text("l1\nl2\nl3\n")
    # retrieval supplied a.py:1-3 ; the ANSWER cites a.py:2 (grounded) and invented.py:9 (hallucinated).
    chunks = [_chunk("a.py", 1, 3)]
    answer = _FakeAnswer("see a.py:2 and also invented.py:9", chunks)

    cfg = RepoConfig(name="apex", root=tmp_path, language="python", digest=None,
                     index={"kind": "none"}, search_globs=["**"], exclude_globs=[],
                     code_exts=[".py"], definition_patterns=[])
    log = tmp_path / "impact.jsonl"
    orig = RepoConfig.load
    RepoConfig.load = staticmethod(lambda name: cfg)
    try:
        d = deliver("apex", "where is X?", impact_log=log,
                    ask_fn=lambda r, q, **k: answer,
                    githead_fn=lambda root: "h", behind_fn=lambda c: 3, clock=lambda: 1.0)
    finally:
        RepoConfig.load = orig

    verdicts = {c.cite: c.verdict for c in d.citations}
    assert verdicts == {"a.py:2": "grounded", "invented.py:9": "hallucinated"}
    assert d.citation_validity() == 0.5
    assert d.has_problem() is True
    # the deliver() path never sets ts_iso to content
    rec = json.loads(log.read_text().strip())
    assert rec["ts_iso"] == ""
    assert rec["question_len"] == len("where is X?")
    assert "where is X" not in log.read_text()  # question string never logged


def test_resolve_repo_from_cwd_matches_registered_root(tmp_path):
    from apex_router.codeqa.deliver import resolve_repo_from_cwd
    from apex_router.codeqa.retriever import RepoConfig
    try:
        apex = RepoConfig.load("apex")
    except Exception:
        import pytest
        pytest.skip("apex repo config not present")
    assert resolve_repo_from_cwd(apex.root) == "apex"
    assert resolve_repo_from_cwd(apex.root / "apex" / "readout") == "apex"
    assert resolve_repo_from_cwd(tmp_path) is None
