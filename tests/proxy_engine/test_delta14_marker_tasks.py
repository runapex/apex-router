"""Δ14 marker-answerable task class — the complement that measures retrieval PROPENSITY.

The scan-task suite (test_delta14_tasksuite) only asks about ELIDED records, so a correct answer
forces retrieval — retrieval_rate is 1.0 by construction and can't reveal over-retrieval. To measure
propensity ≠ necessity (roadmap §Δ14: the necessity≠propensity delta), the gate also needs tasks
whose answer is on the wire — in the marker's own text (the counted total M) or in a retained head
record (the verbatim exemplar). A model that RETRIEVES for these is OVER-retrieving; a model that
answers from the marker is behaving ideally. Together the two task classes bracket the propensity
measurement: scan tasks (retrieval necessary) + marker tasks (retrieval unnecessary).
"""
from __future__ import annotations

import json

from apex_router.proxy_engine.tuner.behavioral_gate import GateTask, run_gate
from apex_router.proxy_engine.tuner.task_suite import build_marker_tasks


def _records(n):
    return json.dumps([{"id": i, "name": f"item-{i}", "score": i * 7} for i in range(n)])


def test_total_count_task_is_answerable_from_the_marker():
    """A 'how many total elements' task's answer (the M in 'elided N of M') is present verbatim in the
    crushed wire — no retrieval needed."""
    tasks = build_marker_tasks(_records(300), max_tasks=2)
    total_task = next((t for t in tasks if "how many" in t.question.lower()), None)
    assert total_task is not None
    assert total_task.correct_answer == "300"
    from apex_router.proxy_engine.pipeline.transforms import json_crush
    from apex_router.proxy_engine.pipeline.transforms.base import Block
    crushed = json_crush.run(Block(content=total_task.content, tool_name="tool_result"), {}).text
    assert "300" in crushed  # the answer is on the wire


def test_head_exemplar_task_is_answerable_from_retained_record():
    """A task about a RETAINED head record (the intact exemplar at index 0) is answerable from the
    crushed wire — that record survives verbatim."""
    tasks = build_marker_tasks(_records(300), max_tasks=3)
    head_task = next((t for t in tasks if "first" in t.question.lower()
                      or "id 0" in t.question.lower() or "`id` is 0" in t.question), None)
    assert head_task is not None
    from apex_router.proxy_engine.pipeline.transforms import json_crush
    from apex_router.proxy_engine.pipeline.transforms.base import Block
    crushed = json_crush.run(Block(content=head_task.content, tool_name="tool_result"), {}).text
    assert head_task.correct_answer in crushed


def test_marker_task_correct_without_retrieval_is_ideal():
    """A model that answers a marker task correctly WITHOUT retrieving is classified
    correct_without_retrieval — the ideal outcome the scan suite could never produce."""
    from apex_router.proxy_engine.tuner.behavioral_gate import Outcome
    tasks = build_marker_tasks(_records(300), max_tasks=1)

    def fake_model(prompt, tools):
        return {"answer": tasks[0].correct_answer, "retrieved_refs": []}

    result = run_gate(tasks, ask_model=fake_model)
    assert result.outcomes[0] == Outcome.CORRECT_WITHOUT_RETRIEVAL


def test_marker_task_retrieval_signals_over_retrieval():
    """A model that RETRIEVES to answer a marker task (answer was already on the wire) still lands
    correct_with_retrieval — the gate's retrieval_rate on marker tasks IS the over-retrieval rate."""
    tasks = build_marker_tasks(_records(300), max_tasks=1)

    def fake_model(prompt, tools):
        return {"answer": tasks[0].correct_answer, "retrieved_refs": ["ccr://x#0-0"]}

    result = run_gate(tasks, ask_model=fake_model)
    assert result.retrieval_rate == 1.0  # over-retrieved: the answer needed no retrieval


def test_no_marker_tasks_when_nothing_elides():
    """A small array (no elision, no marker) yields no marker tasks."""
    assert build_marker_tasks(_records(3), max_tasks=5) == []
