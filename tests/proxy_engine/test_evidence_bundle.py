from __future__ import annotations

import json

import pytest

from apex_router.proxy_engine.policy import (
    CONTENT_CLASSES,
    ClassRule,
    EvidenceBundle,
    ExpectedReport,
    InvalidPolicy,
    PolicyVersion,
    T2Policy,
)
from apex_router.proxy_engine.tuner.behavioral_gate import GateTask, run_gate
from apex_router.proxy_engine.tuner.evidence import corpus_content_hash, source_tree_hash
from apex_router.proxy_engine.tuner.replay import Request

STRATA = ("xs", "s", "m", "l", "xl")


def _policy(manifest_hash: str) -> PolicyVersion:
    rules = {
        cls: {st: ClassRule(None, False, 1 << 30, 0.0) for st in STRATA} for cls in CONTENT_CLASSES
    }
    return PolicyVersion(
        version=1,
        compiled_at=123.0,
        compiler_hash="compiler-1",
        corpus_hash="composition-1",
        band=(6.0, 30.0),
        rules=rules,
        t2=T2Policy(("ttl",), 3),
        expected=ExpectedReport(0.0, {}),
        evidence_manifest_hash=manifest_hash,
    ).sealed()


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "compiled_at": 123.0,
        "source_revision": "abc",
        "source_tree_clean": True,
        "source_tree_sha256": "tree",
        "corpus_content_sha256": "content",
        "policy_corpus_hash": "composition-1",
        "corpus_n_requests": 1,
        "corpus_n_sessions": 1,
        "projects": ["p"],
        "models": ["m"],
        "tokenizer": {"encoding": "cl100k_base", "encoding_sha256": "tok"},
        "compiler_hash": "compiler-1",
        "validators": {},
        "artifacts": [],
    }


def test_production_bundle_binds_manifest_into_policy_seal():
    manifest = _manifest()
    digest = EvidenceBundle._manifest_digest(manifest)
    bundle = EvidenceBundle(_policy(digest), manifest, {"enabled_cells": []})
    loaded = EvidenceBundle.load_verified(bundle.to_dict())
    assert loaded.policy.evidence_manifest_hash == digest


def test_manifest_tamper_is_rejected_even_when_policy_json_is_untouched():
    manifest = _manifest()
    bundle = EvidenceBundle(
        _policy(EvidenceBundle._manifest_digest(manifest)), manifest, {"enabled_cells": []}
    ).to_dict()
    bundle["manifest"]["models"].append("different-model")
    with pytest.raises(InvalidPolicy, match="manifest digest"):
        EvidenceBundle.load_verified(bundle)


def test_bundle_rejects_manifest_policy_timestamp_disagreement():
    manifest = _manifest()
    manifest["compiled_at"] = 124.0
    digest = EvidenceBundle._manifest_digest(manifest)
    bundle = EvidenceBundle(_policy(digest), manifest, {"enabled_cells": []})
    with pytest.raises(InvalidPolicy, match="compiled_at"):
        EvidenceBundle.load_verified(bundle.to_dict())


def test_bare_policy_is_not_a_production_bundle():
    with pytest.raises(InvalidPolicy, match="evidence bundle"):
        EvidenceBundle.load_verified(_policy("x").to_dict())


def test_corpus_content_hash_binds_rows_not_only_aggregate_counts():
    a = [Request("s", b"alpha", 1, 1.0, "m")]
    b = [Request("s", b"bravo", 1, 1.0, "m")]
    assert corpus_content_hash(a) != corpus_content_hash(b)


def test_source_tree_hash_ignores_mtime_but_changes_with_bytes(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("x = 1\n")
    first = source_tree_hash(tmp_path)
    p.touch()
    assert source_tree_hash(tmp_path) == first
    p.write_text("x = 2\n")
    assert source_tree_hash(tmp_path) != first


def test_gate_report_hash_can_be_bound_as_verified_json(tmp_path):
    task = GateTask(
        content=json.dumps([{"id": i, "value": i} for i in range(300)]),
        question="answer?",
        correct_answer="7",
    )

    def model(prompt, tools):
        return {"answer": "7", "retrieved_refs": []}

    report = run_gate([task], ask_model=model).to_dict()
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(report))
    # The report is self-verifying and remains usable as an evidence artifact.
    from apex_router.proxy_engine.tuner.behavioral_gate import load_and_verify_gate_report

    loaded, verification = load_and_verify_gate_report(str(path))
    assert loaded["n"] == 1
    assert verification.ok
