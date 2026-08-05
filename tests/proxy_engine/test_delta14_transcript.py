"""Δ14 transcript persistence — auditable per-task evidence (closes the 'not re-verifiable' gap).

The first live run classified 6/6 but persisted no raw model answers, so the result trusted the
tested pipeline rather than an auditable record. run_gate now returns per-task records (question,
ground-truth answer, the model's answer, retrieved refs, the outcome) so a reviewer can re-check the
classification against what the model actually said — the verify-claims discipline applied to the
gate's own output.
"""

from __future__ import annotations

import json

from apex_router.proxy_engine.tuner.behavioral_gate import GateTask, Outcome, run_gate, verify_gate_report


def _records(n):
    return json.dumps([{"id": i, "name": f"item-{i}", "score": i * 7} for i in range(n)])


def _elided_task():
    return GateTask(content=_records(300), question="score of id 150?", correct_answer="1050")


def test_result_carries_per_task_records():
    """Each task produces a record with the fields needed to re-verify its classification."""

    def fake_model(prompt, tools):
        return {"answer": "1050", "retrieved_refs": ["ccr://x#5-297"]}

    result = run_gate([_elided_task()], ask_model=fake_model)
    assert len(result.records) == 1
    rec = result.records[0]
    assert rec["question"] == "score of id 150?"
    assert rec["correct_answer"] == "1050"
    assert rec["model_answer"] == "1050"
    assert rec["retrieved_refs"] == ["ccr://x#5-297"]
    assert rec["outcome"] == Outcome.CORRECT_WITH_RETRIEVAL.value


def test_records_let_a_reviewer_recompute_the_verdict():
    """The record is self-contained: outcome is derivable from (correct_answer, model_answer,
    retrieved_refs) alone — so a reviewer never has to trust the classifier's label blind."""

    def wrong_no_retrieval(prompt, tools):
        return {"answer": "999", "retrieved_refs": []}

    result = run_gate([_elided_task()], ask_model=wrong_no_retrieval)
    rec = result.records[0]
    # a reviewer recomputes: answer != correct AND no retrieval → wrong_without_retrieving
    correct = rec["correct_answer"] == rec["model_answer"]
    retrieved = bool(rec["retrieved_refs"])
    assert not correct and not retrieved
    assert rec["outcome"] == Outcome.WRONG_WITHOUT_RETRIEVING.value


def test_to_dict_includes_records():
    """The serialized result carries the transcript so a run's JSON output is self-auditing."""

    def fake_model(prompt, tools):
        return {"answer": "1050", "retrieved_refs": []}

    d = run_gate([_elided_task()], ask_model=fake_model).to_dict()
    assert "records" in d and len(d["records"]) == 1
    assert d["records"][0]["model_answer"] == "1050"


def test_typed_oracle_rejects_negated_substring_false_positive():
    def negated(prompt, tools):
        return {"answer": "there is no such value 1050", "retrieved_refs": []}

    result = run_gate([_elided_task()], ask_model=negated)
    assert result.outcomes == [Outcome.WRONG_WITHOUT_RETRIEVING]
    assert verify_gate_report(result.to_dict()).ok


def test_verifier_rejects_a_clean_looking_tampered_summary():
    def wrong(prompt, tools):
        return {"answer": "999", "retrieved_refs": []}

    report = run_gate([_elided_task()], ask_model=wrong).to_dict()
    report["by_outcome"][Outcome.WRONG_WITHOUT_RETRIEVING.value] = 0
    report["by_outcome"][Outcome.CORRECT_WITHOUT_RETRIEVAL.value] = 1
    verification = verify_gate_report(report)
    assert not verification.ok
    assert any("by_outcome mismatch" in e for e in verification.errors)
