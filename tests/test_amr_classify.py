"""Tests for amr.classify — the §11 Tier-2 task-type classifier.

Task-types: debug · explore · review · refactor · generate.
Two fused cheap signals (design §11):
  1. request-signal (free, 0ms): tool-set + system-prompt markers — the PRIOR.
  2. embedding signal: nomic vs per-class exemplars — REFINES only above a margin.
Conservative on ambiguity -> the safe/heavy default ('debug').

Fusion tests inject a fake embed_fn so they stay hermetic.
"""
import os

import pytest

from apex_router import classify


# --------------------------------------------------------------------------- #
# request-signal classifier (pure, 0ms) — the prior
# --------------------------------------------------------------------------- #
def test_report_findings_tool_means_review():
    r = classify.classify_request(tools=["Read", "Grep", "ReportFindings"])
    assert r.task_type == "review"


def test_edit_write_readonly_split_mutation_vs_explore():
    mut = classify.classify_request(tools=["Read", "Edit", "Write"])
    exp = classify.classify_request(tools=["Read", "Grep", "Glob"])
    assert mut.task_type in ("refactor", "generate")   # mutation present
    assert exp.task_type == "explore"                   # read-only


def test_system_marker_debug_beats_toolset():
    # An explicit 'debug' system marker should classify as debug even with edit tools.
    r = classify.classify_request(tools=["Read", "Edit"], sys_markers=["debug"])
    assert r.task_type == "debug"


def test_review_marker_classifies_review():
    r = classify.classify_request(tools=["Read"], sys_markers=["review"])
    assert r.task_type == "review"


def test_no_signal_defaults_to_safe_heavy():
    # No tools, no markers -> conservative default (heavy/safe), never a random pick.
    r = classify.classify_request(tools=[], sys_markers=[])
    assert r.task_type == "debug"        # the §11 safe/heavy default
    assert r.confidence <= 0.5           # low confidence when nothing discriminates


def test_request_result_reports_confidence_and_signal():
    r = classify.classify_request(tools=["ReportFindings"])
    assert 0.0 <= r.confidence <= 1.0
    assert r.source == "request"


# --------------------------------------------------------------------------- #
# fusion: request prior + embedding refine-above-margin (injected embed_fn)
# --------------------------------------------------------------------------- #
def _embed_fn_matching(exemplar_class, exemplars):
    """Fake embed_fn: returns the exemplar vector of `exemplar_class` for the query,
    and canonical basis vectors for each class's exemplars, so cosine picks that class."""
    classes = list(exemplars.keys())
    basis = {c: [1.0 if i == n else 0.0 for i in range(len(classes))]
             for n, c in enumerate(classes)}
    def embed_fn(text):
        # exemplar texts are "<class>::..." ; the query is "QUERY"
        if text == "QUERY":
            return basis[exemplar_class]
        cls = text.split("::", 1)[0]
        return basis[cls]
    return embed_fn


def test_fusion_low_request_confidence_lets_embedding_decide():
    # Ambiguous tools (low request confidence) -> embedding refinement takes over.
    exemplars = {"debug": ["debug::x"], "explore": ["explore::y"], "review": ["review::z"],
                 "refactor": ["refactor::w"], "generate": ["generate::v"]}
    embed_fn = _embed_fn_matching("refactor", exemplars)
    r = classify.classify("QUERY", tools=["Read"], sys_markers=[],
                          embed_fn=embed_fn, exemplars=exemplars, margin=0.05)
    assert r.task_type == "refactor"
    assert r.source in ("embedding", "fusion")


def test_fusion_strong_request_signal_not_overridden_below_margin():
    # A confident request signal (ReportFindings -> review) must NOT be flipped by a
    # weak embedding lead that doesn't clear the margin.
    exemplars = {"debug": ["debug::x"], "explore": ["explore::y"], "review": ["review::z"],
                 "refactor": ["refactor::w"], "generate": ["generate::v"]}
    # embedding weakly points at 'explore' but margin is huge -> no override.
    embed_fn = _embed_fn_matching("explore", exemplars)
    r = classify.classify("QUERY", tools=["ReportFindings"], sys_markers=[],
                          embed_fn=embed_fn, exemplars=exemplars, margin=0.99)
    assert r.task_type == "review"


def test_fusion_conservative_default_when_all_ambiguous():
    # No request signal AND no exemplars -> safe/heavy default, never a coin flip.
    r = classify.classify("QUERY", tools=[], sys_markers=[], embed_fn=None,
                          exemplars=None, margin=0.05)
    assert r.task_type == "debug"


def test_classify_task_types_are_the_canonical_five():
    assert set(classify.TASK_TYPES) == {"debug", "explore", "review", "refactor", "generate"}


# --------------------------------------------------------------------------- #
# Regression — confirmed by Codex cross-validation (the reference window)
# --------------------------------------------------------------------------- #
def test_fusion_lone_survivor_with_low_cosine_does_not_override():
    # BUG (Codex): when only ONE class's exemplars survive scoring, runner-up was
    # treated as -1.0, so any positive cosine "led" by >1.0 and overrode the prior
    # at 0.95 confidence — even a near-orthogonal (cos~0.02) match. An embedding that
    # matches nothing well must NOT override the request prior.
    def embed_fn(text):
        if text == "QUERY":
            return [1.0, 0.0]
        if text.startswith("refactor::"):
            return [0.02, 1.0]          # ~orthogonal to the query
        raise RuntimeError("other classes fail to embed")
    exemplars = {"debug": ["debug::x"], "explore": ["explore::y"], "review": ["review::z"],
                 "refactor": ["refactor::w"], "generate": ["generate::v"]}
    r = classify.classify("QUERY", tools=["Read"], sys_markers=[],
                          embed_fn=embed_fn, exemplars=exemplars, margin=0.05)
    assert r.task_type == "explore"     # prior stands; no noise override
    assert r.source in ("request", "default")


def test_fusion_high_cosine_lone_survivor_may_refine():
    # Complement: a lone survivor with a STRONG absolute cosine is a legitimate
    # refinement (there is genuinely only one plausible class) and may override a
    # weak (non-confident) prior.
    def embed_fn(text):
        if text == "QUERY":
            return [1.0, 0.0]
        if text.startswith("refactor::"):
            return [1.0, 0.0]           # near-identical to the query
        raise RuntimeError("other classes fail to embed")
    exemplars = {"debug": ["debug::x"], "refactor": ["refactor::w"]}
    r = classify.classify("QUERY", tools=["Read"], sys_markers=[],
                          embed_fn=embed_fn, exemplars=exemplars, margin=0.05)
    assert r.task_type == "refactor"


def test_fusion_decisive_embedding_does_not_override_confident_request_prior():
    # POLICY (Codex-flagged as untested, behavior intended): a confident request
    # signal (ReportFindings -> review, 0.85) is trusted even against a decisive,
    # margin-clearing embedding pointing elsewhere.
    def embed_fn(text):
        order = {"debug": 0, "explore": 1, "review": 2, "refactor": 3, "generate": 4}
        if text == "QUERY":
            return [1.0, 0.0, 0.0, 0.0, 0.0]     # points hard at 'debug'
        idx = order[text.split("::")[0]]
        return [1.0 if i == idx else 0.0 for i in range(5)]
    exemplars = {k: [f"{k}::e"] for k in classify.TASK_TYPES}
    r = classify.classify("QUERY", tools=["ReportFindings"], sys_markers=[],
                          embed_fn=embed_fn, exemplars=exemplars, margin=0.05)
    assert r.task_type == "review"
    assert r.source == "request"


# --------------------------------------------------------------------------- #
# cell parents: every task-type is a coarse parent cell (fallback before clustering)
# --------------------------------------------------------------------------- #
def test_parent_cell_is_the_task_type():
    assert classify.parent_cell("refactor") == "task:refactor"


def test_parent_cell_rejects_unknown_type():
    with pytest.raises(ValueError):
        classify.parent_cell("nonsense")


# --------------------------------------------------------------------------- #
# live fusion — real nomic embedder splits an ambiguous request (opt-in)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.environ.get("RUN_LIVE_EMBED") != "1",
                    reason="set RUN_LIVE_EMBED=1 to hit the live ollama server")
def test_live_fusion_splits_debug_from_explore_on_readonly_toolset():
    from apex_router import embed
    # Read-only tools -> request prior = 'explore' (0.7, not >=0.8 confident), so the
    # embedding is allowed to refine. A debug-flavored query should pull it to 'debug'.
    exemplars = {
        "debug":    ["diagnose why the failing test raises an exception and fix the defect"],
        "explore":  ["help me understand how the retrieval pipeline is structured"],
        "review":   ["review this diff and report concrete bugs with line refs"],
        "refactor": ["rename this function across the codebase and update call sites"],
        "generate": ["write a new function that formats a report from raw data"],
    }
    r = classify.classify(
        "the unit test is failing with a traceback, find the bug and fix it",
        tools=["Read", "Grep"], sys_markers=[],
        embed_fn=embed.embed, exemplars=exemplars, margin=0.05,
    )
    assert r.task_type == "debug"
    assert r.source in ("fusion", "embedding")
