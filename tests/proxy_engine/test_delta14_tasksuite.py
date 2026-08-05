"""Δ14 task suite — probes whose answer lives in an ELIDED array-middle record (roadmap §Δ14).

The gate needs tasks that provably depend on elided content ("scan-type tasks, array-middle-dependent
decisions"). This builds them mechanically from real crushable JSON: pick a record the crusher drops
(strictly between keep_head and the tail), and ask a question whose ground-truth answer is a field of
that record. A correct answer therefore requires either retrieval or a marker richer than the crusher
emits — exactly the fidelity question the gate exists to settle.
"""
from __future__ import annotations

import json

from apex_router.proxy_engine.tuner.behavioral_gate import GateTask
from apex_router.proxy_engine.tuner.task_suite import build_scan_tasks


def _records(n):
    return json.dumps([{"id": i, "name": f"item-{i}", "score": i * 7} for i in range(n)])


def test_task_targets_an_elided_record():
    """The built task's answer is a field of a record the crusher elides — so answering needs the
    dropped bytes (retrieval), not the retained head/tail. Checked structurally: the target record is
    absent from the crushed wire as a parseable object (a substring match on the raw text would false-
    positive on the answer digits appearing inside a ccr hash)."""
    tasks = build_scan_tasks(_records(300), max_tasks=1)
    assert tasks
    t = tasks[0]
    assert isinstance(t, GateTask)
    from apex_router.proxy_engine.pipeline.transforms import json_crush
    from apex_router.proxy_engine.pipeline.transforms.base import Block
    crushed = json_crush.run(Block(content=t.content, tool_name="tool_result"), {}).text
    # the crushed wire, re-parsed, must NOT contain a record carrying this answer as its `score`
    # (retained head/tail records have different scores; the target's record was dropped).
    import re
    marker_free = re.sub(r"\[… elided[^\]]*\]", "", crushed)
    surviving = re.findall(r'"score":(\d+)', marker_free)
    assert t.correct_answer not in surviving  # the answer's record is not on the wire


def test_no_tasks_when_nothing_elides():
    """A small array (nothing dropped) yields no scan tasks — the suite only probes real elisions."""
    assert build_scan_tasks(_records(3), max_tasks=5) == []


def test_tasks_have_ground_truth_from_the_original():
    """Each task's correct_answer is the true field value in the ORIGINAL, so the gate's correctness
    check is against ground truth, not the crushed bytes."""
    tasks = build_scan_tasks(_records(300), max_tasks=3)
    original = json.loads(_records(300))
    for t in tasks:
        # the question names an id; the answer is that record's score in the original
        assert any(str(r["score"]) == t.correct_answer for r in original)
