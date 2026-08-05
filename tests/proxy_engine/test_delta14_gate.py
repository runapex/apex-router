"""Δ14 behavioral gate — counterfactual replay + three-outcome classification (roadmap §Δ14).

The gate answers the question the fidelity floor cannot: does compressing a block change what the
AGENT does? It gives a model a task whose answer depends on the block, feeds it the CRUSHED block
(markers + a retrieval tool backed by the stub resolver), and classifies the run into three
outcomes tracked SEPARATELY (decision-log Q5):
  - correct_without_retrieval — answered correctly from the markers alone (the elision was safe AND
    the agent didn't even need to retrieve — the ideal case);
  - correct_with_retrieval    — retrieved the elided bytes, then answered correctly (safe, but the
    elision cost a retrieval — feeds the retrieval-propensity ≠ necessity measurement);
  - wrong_without_retrieving  — answered WRONG without retrieving (the ONLY failure that triggers the
    digest-marker escalation: the marker lacked what the task needed and the agent didn't know to ask).

The harness is model-injected: `run_gate(tasks, ask_model=...)` calls `ask_model(prompt, tools)` and
never imports a live client, so the classification logic is fully tested with a deterministic fake
model. A separate thin driver (built later) supplies the real model.

Instrument controls (roadmap §Δ14 "tests of the harness itself"): a positive control (a task that
provably needs the elided content → the harness detects divergence or retrieval) and a negative
control (a task untouched by elision → no divergence).
"""
from __future__ import annotations

import json

from apex_router.proxy_engine.tuner.behavioral_gate import GateTask, Outcome, run_gate


def _records_json(n: int) -> str:
    return json.dumps([{"id": i, "name": f"item-{i}", "score": i * 7} for i in range(n)])


def _task_on_elided_middle() -> GateTask:
    """A task whose answer lives in an ELIDED middle record (id=150 of 300) — needs retrieval or a
    marker rich enough to answer without it."""
    return GateTask(
        content=_records_json(300),
        question="What is the `score` of the record with id 150?",
        correct_answer="1050",  # 150 * 7
    )


def _task_on_retained_head() -> GateTask:
    """A task whose answer lives in a RETAINED head record (id=2, within keep_head) — the negative
    control: elision doesn't touch it, so a correct answer needs no retrieval."""
    return GateTask(
        content=_records_json(300),
        question="What is the `score` of the record with id 2?",
        correct_answer="14",  # 2 * 7
    )


def test_correct_without_retrieval_when_answer_in_retained_head():
    """Negative control: the answer is in a retained record; a model that answers correctly without
    retrieving is classified correct_without_retrieval (no divergence, no retrieval)."""
    task = _task_on_retained_head()

    def fake_model(prompt, tools):
        return {"answer": "14", "retrieved_refs": []}

    result = run_gate([task], ask_model=fake_model)
    assert result.outcomes[0] == Outcome.CORRECT_WITHOUT_RETRIEVAL


def test_correct_with_retrieval_when_model_retrieves_then_answers():
    """A model that retrieves the elided middle and then answers correctly is
    correct_with_retrieval — safe, but it spent a retrieval (propensity signal)."""
    task = _task_on_elided_middle()

    def fake_model(prompt, tools):
        # simulate the model calling the retrieval tool, then answering from the served bytes
        return {"answer": "1050", "retrieved_refs": ["<any>"]}

    result = run_gate([task], ask_model=fake_model)
    assert result.outcomes[0] == Outcome.CORRECT_WITH_RETRIEVAL


def test_wrong_without_retrieving_is_the_escalation_trigger():
    """Positive control: the answer is in an elided record; a model that answers WRONG without
    retrieving is wrong_without_retrieving — the only outcome that trips the digest-marker escalation."""
    task = _task_on_elided_middle()

    def fake_model(prompt, tools):
        return {"answer": "999", "retrieved_refs": []}  # wrong, didn't retrieve

    result = run_gate([task], ask_model=fake_model)
    assert result.outcomes[0] == Outcome.WRONG_WITHOUT_RETRIEVING
    assert result.wrong_without_retrieving_rate == 1.0
    assert result.should_escalate is True  # >0 at any measurable rate → digest-marker design activates


def test_rates_aggregate_across_tasks():
    """The gate reports per-outcome rates + the retrieval-propensity rate over a task batch."""
    tasks = [_task_on_retained_head(), _task_on_elided_middle(), _task_on_elided_middle()]
    answers = iter([
        {"answer": "14", "retrieved_refs": []},        # correct_without_retrieval
        {"answer": "1050", "retrieved_refs": ["x"]},    # correct_with_retrieval
        {"answer": "1050", "retrieved_refs": []},        # correct_without_retrieval (rich marker case)
    ])

    def fake_model(prompt, tools):
        return next(answers)

    result = run_gate(tasks, ask_model=fake_model)
    assert result.n == 3
    assert result.wrong_without_retrieving_rate == 0.0
    assert result.retrieval_rate == 1 / 3  # one of three runs retrieved
    assert result.should_escalate is False


def test_gate_prompt_carries_crushed_bytes_not_original():
    """The prompt the model sees contains the CRUSHED block (with markers), never the full original —
    otherwise the gate would measure the model on uncompressed input and prove nothing."""
    task = _task_on_elided_middle()
    seen = {}

    def fake_model(prompt, tools):
        seen["prompt"] = prompt
        return {"answer": "1050", "retrieved_refs": ["x"]}

    run_gate([task], ask_model=fake_model)
    assert "elided" in seen["prompt"]           # a crush marker is present
    assert '"id":150' not in seen["prompt"]      # the elided record is NOT in the prompt verbatim
