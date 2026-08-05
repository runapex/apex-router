"""Δ14 gutter task class — behavioral probes for the file_read gutter-strip transform.

The scan/marker task classes are JSON-array shaped; gutter-strip is a DIFFERENT lossy transform (drops
line-number gutters), so it needs its own probes. Two shapes, mirroring scan/marker:
  - GUTTER-DEPENDENT (retrieval necessary): "what is on line N?" — after strip, line numbers are gone
    from the wire, so answering needs retrieval of the guttered original. The safety probe:
    wrong_without_retrieving = the model guessed a line's content without the numbers.
  - CONTENT-ONLY (answer on wire): a question about the code content that survives verbatim (strip
    keeps content) — answerable without retrieval; retrieving is over-retrieval.

Uses the same GateTask contract, but the block is a file_read (guttered code), so the gate runs
gutter-strip instead of json_crush. The gate is transform-parameterized for this.
"""

from __future__ import annotations

from apex_router.proxy_engine.tuner.behavioral_gate import GateTask, Outcome, run_gate
from apex_router.proxy_engine.tuner.task_suite import build_gutter_tasks


def _code(n, *, start=1):
    # line i defines func_i returning a distinctive constant, so "line N" has a checkable answer
    return "\n".join(f"{i}\tdef func_{i}(): return {i * 100}" for i in range(start, start + n))


def test_gutter_dependent_task_answer_needs_the_line_number():
    """A 'what does the function on line N return' task: the answer is a specific line's content, and
    after strip the line NUMBERS are gone — so locating line N needs the guttered original."""
    tasks = build_gutter_tasks(_code(40, start=101), max_tasks=4)
    dep = [t for t in tasks if t.kind == "gutter_dependent"]
    assert dep, "expected gutter-dependent tasks"
    t = dep[0]
    # the correct answer is a real line's returned constant
    assert t.correct_answer.isdigit()


def test_contiguous_one_based_gutter_is_not_falsely_called_retrieval_necessary():
    """After stripping 1..N gutters, line N is still derivable by counting retained lines. The
    harness must not manufacture a retrieval-necessity claim for that population."""
    tasks = build_gutter_tasks(_code(40), max_tasks=6)
    assert not [t for t in tasks if t.kind == "gutter_dependent"]


def test_content_only_task_answerable_without_line_numbers():
    """A content-only task (a fact about the code that survives strip verbatim) is answerable from the
    stripped wire — retrieving for it is over-retrieval."""
    tasks = build_gutter_tasks(_code(40), max_tasks=6)
    content = [t for t in tasks if t.kind == "content_only"]
    assert content
    assert "appear anywhere" not in content[0].question  # old tautological probe is gone


def test_gutter_task_runs_through_the_gate_with_strip_transform():
    """The gate runs gutter-strip (not json_crush) on a file_read block and classifies the outcome —
    proving the gate is transform-parameterized for file_read probes."""
    tasks = build_gutter_tasks(_code(40), max_tasks=2)

    def fake_model(prompt, tools, resolver=None):
        # a model that retrieves and answers correctly
        return {"answer": tasks[0].correct_answer, "retrieved_refs": ["x"]}

    result = run_gate(tasks, ask_model=fake_model, transform="file_read_strip")
    assert result.outcomes[0] in (Outcome.CORRECT_WITH_RETRIEVAL, Outcome.CORRECT_WITHOUT_RETRIEVAL)


def test_no_gutter_tasks_when_not_guttered():
    """Plain prose (no gutter) yields no gutter tasks — the probes only apply to file reads."""
    assert (
        build_gutter_tasks("just prose\nno line numbers here\nnothing to strip", max_tasks=4) == []
    )
