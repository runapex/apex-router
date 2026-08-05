"""Δ14 behavioral gate — the model-level fidelity check the byte floor can't do (roadmap §Δ14).

The entity floor (Δ13) is a STRUCTURAL guarantee: it checks the crushed bytes still carry counts,
locators, and an exemplar. It cannot tell you whether an AGENT, reading those bytes, still does the
right thing. The behavioral gate measures exactly that: give a model a task whose answer depends on
a block, feed it the CRUSHED block plus a retrieval tool (backed by the Δ14 stub resolver), and
classify the run into three outcomes tracked SEPARATELY (decision-log Q5) —

    correct_without_retrieval  the marker sufficed; the agent answered right, no retrieval
    correct_with_retrieval     the agent retrieved the elided bytes, then answered right
    wrong_without_retrieving   the agent answered WRONG without retrieving — the marker lacked what
                               the task needed AND the agent didn't know to ask. This is the ONLY
                               outcome that trips the digest-marker escalation (roadmap §Δ14 / R6):
                               >0 at any measurable rate → the digest-marker design activates.

The gate is MODEL-INJECTED: `run_gate(tasks, ask_model=…)` calls `ask_model(prompt, tools)` and
imports no client, so the classification logic is verifiable with a deterministic fake model and the
real-dollar driver (a thin `ask_model` that streams a live Claude request) plugs in unchanged. This
is ANALYTICS-plane code (apex_router.proxy_engine.tuner) — it never runs on the hot path; its output is evidence that
feeds the compiler's `_LOSSY_CAPABILITIES` behavioral-evidence reference, not a runtime decision.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from apex_router.proxy_engine.pipeline.resolver import StubResolver
from apex_router.proxy_engine.pipeline.transforms import json_crush
from apex_router.proxy_engine.pipeline.transforms.base import Block


class Outcome(enum.Enum):
    CORRECT_WITHOUT_RETRIEVAL = "correct_without_retrieval"
    CORRECT_WITH_RETRIEVAL = "correct_with_retrieval"
    WRONG_WITHOUT_RETRIEVING = "wrong_without_retrieving"
    # a run that retrieved and STILL got it wrong: the retrieval worked but the model erred anyway —
    # NOT an elision-fidelity failure (the bytes were served), so it does not trip the escalation.
    WRONG_WITH_RETRIEVAL = "wrong_with_retrieval"


# The model answer contract:
#   `ask_model(prompt, tools, resolver) -> {"answer": str, "retrieved_refs": [str]}`.
# `retrieved_refs` is the list of ccr refs the model asked the retrieval tool to resolve during the
# run (empty if it never retrieved). `resolver` is the per-task StubResolver the driver serves the
# retrieval tool from — the real driver calls `resolver.resolve(ref)` inside its tool loop; a fake
# model ignores it and returns the trace directly. Passed as a keyword so a 2-arg fake still works
# only if it opts in; run_gate always supplies it.
AskModel = Callable[..., dict]


class AnswerType(enum.Enum):
    """Machine-checkable answer oracles for behavioral evidence.

    The old substring matcher could score a negated answer as correct (for example, ground truth
    ``42`` versus ``there is no such value 42``). Evidence-grade tasks therefore declare a typed
    oracle and the persisted transcript records that oracle so a reviewer can re-run the exact
    classification without the original Python objects.
    """

    EXACT = "exact"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    NORMALIZED_TEXT = "normalized_text"


def _normalize_text(value: str) -> str:
    return " ".join((value or "").strip().split())


def answer_matches(answer_type: str | AnswerType, expected: str, actual: str) -> bool:
    """Evaluate a persisted typed oracle. Deliberately never uses substring containment."""
    try:
        kind = answer_type if isinstance(answer_type, AnswerType) else AnswerType(answer_type)
    except ValueError:
        return False
    expected_n = _normalize_text(expected)
    actual_n = _normalize_text(actual)
    if kind is AnswerType.EXACT:
        return actual_n == expected_n
    if kind is AnswerType.INTEGER:
        # Require the entire answer to be one integer. This rejects both prose-wrapped ambiguity and
        # negation such as "there is no value 42".
        if not re.fullmatch(r"[-+]?\d+", actual_n):
            return False
        try:
            return int(actual_n) == int(expected_n)
        except ValueError:
            return False
    if kind is AnswerType.BOOLEAN:
        aliases = {
            "yes": True,
            "true": True,
            "1": True,
            "no": False,
            "false": False,
            "0": False,
        }
        return aliases.get(actual_n.lower()) is aliases.get(expected_n.lower()) and (
            actual_n.lower() in aliases and expected_n.lower() in aliases
        )
    return actual_n.casefold() == expected_n.casefold()


@dataclass(frozen=True)
class GateTask:
    """One behavioral probe: a block, a question whose answer depends on it, and the ground-truth
    answer. `answer_type` selects a typed, exact oracle; substring containment is intentionally
    unsupported because it made a negated answer look correct. `kind` labels the probe class
    (scan / marker / gutter_dependent / content_only) so a run can slice rates by class."""

    content: str
    question: str
    correct_answer: str
    knobs: dict = field(default_factory=dict)
    kind: str = "scan"
    answer_type: AnswerType = AnswerType.EXACT
    expected_retrieval: bool | None = None
    stratum: str = "unknown"

    def answer_matches(self, model_answer: str) -> bool:
        return answer_matches(self.answer_type, self.correct_answer, model_answer)

    @property
    def task_id(self) -> str:
        body = {
            "content_hash": hashlib.sha256(self.content.encode("utf-8")).hexdigest(),
            "question": self.question,
            "correct_answer": self.correct_answer,
            "answer_type": self.answer_type.value,
            "kind": self.kind,
            "expected_retrieval": self.expected_retrieval,
            "stratum": self.stratum,
            "knobs": self.knobs,
        }
        blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:20]


@dataclass
class GateResult:
    outcomes: list[Outcome]
    n: int
    # per-task transcript: {question, correct_answer, model_answer, retrieved_refs, outcome} — the
    # auditable record a reviewer re-checks the classification against (verify-claims on the gate's
    # own output; the first live run lacked this and could only be trusted, not re-verified).
    records: list[dict] = field(default_factory=list)
    transform: str = "json_crush"

    def _count(self, o: Outcome) -> int:
        return sum(1 for x in self.outcomes if x == o)

    @property
    def wrong_without_retrieving_rate(self) -> float:
        return self._count(Outcome.WRONG_WITHOUT_RETRIEVING) / self.n if self.n else 0.0

    @property
    def retrieval_rate(self) -> float:
        """Fraction of runs that retrieved at least once — the retrieval-PROPENSITY observation
        (roadmap §Δ14): compared against the mined necessity rate, propensity ≠ necessity is the
        signal that markers are richer (or poorer) than agents treat them."""
        retrieved = self._count(Outcome.CORRECT_WITH_RETRIEVAL) + self._count(
            Outcome.WRONG_WITH_RETRIEVAL
        )
        return retrieved / self.n if self.n else 0.0

    @property
    def should_escalate(self) -> bool:
        """The digest-marker escalation trigger: any wrong_without_retrieving at all (roadmap §Δ14 —
        '>0 at any measurable rate → digest-marker design activates')."""
        return self._count(Outcome.WRONG_WITHOUT_RETRIEVING) > 0

    def to_dict(self) -> dict:
        return {
            "schema_version": 2,
            "transform": self.transform,
            "n": self.n,
            "by_outcome": {o.value: self._count(o) for o in Outcome},
            "wrong_without_retrieving_rate": round(self.wrong_without_retrieving_rate, 4),
            "retrieval_rate": round(self.retrieval_rate, 4),
            "should_escalate": self.should_escalate,
            "records": self.records,
        }


@dataclass(frozen=True)
class GateVerification:
    ok: bool
    errors: tuple[str, ...]
    report_sha256: str


def verify_gate_report(report: dict) -> GateVerification:
    """Re-derive every persisted outcome and summary from transcript records.

    A report cannot become signing evidence merely because its top-level counters look clean. The
    records are the authority; malformed/missing oracles, mismatched labels, summary drift, or a
    missing task identity make the report invalid.
    """
    errors: list[str] = []
    records = report.get("records")
    if not isinstance(records, list):
        errors.append("report.records is missing or not a list")
        records = []
    derived: list[Outcome] = []
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            errors.append(f"record[{i}] is not an object")
            continue
        for key in (
            "task_id",
            "correct_answer",
            "answer_type",
            "model_answer",
            "retrieved_refs",
            "outcome",
        ):
            if key not in rec:
                errors.append(f"record[{i}] missing {key}")
        if any(
            key not in rec
            for key in ("correct_answer", "answer_type", "model_answer", "retrieved_refs")
        ):
            continue
        correct = answer_matches(
            rec["answer_type"], str(rec["correct_answer"]), str(rec["model_answer"])
        )
        retrieved = bool(rec["retrieved_refs"])
        if correct and retrieved:
            outcome = Outcome.CORRECT_WITH_RETRIEVAL
        elif correct:
            outcome = Outcome.CORRECT_WITHOUT_RETRIEVAL
        elif retrieved:
            outcome = Outcome.WRONG_WITH_RETRIEVAL
        else:
            outcome = Outcome.WRONG_WITHOUT_RETRIEVING
        derived.append(outcome)
        if rec.get("outcome") != outcome.value:
            errors.append(
                f"record[{i}] outcome mismatch: stored={rec.get('outcome')!r} derived={outcome.value!r}"
            )
    if report.get("n") != len(records):
        errors.append(f"n mismatch: stored={report.get('n')!r} records={len(records)}")
    stored_counts = report.get("by_outcome")
    derived_counts = {o.value: sum(1 for x in derived if x is o) for o in Outcome}
    if stored_counts != derived_counts:
        errors.append(f"by_outcome mismatch: stored={stored_counts!r} derived={derived_counts!r}")
    blob = json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return GateVerification(not errors, tuple(errors), hashlib.sha256(blob).hexdigest())


def load_and_verify_gate_report(path: str) -> tuple[dict, GateVerification]:
    with open(path, encoding="utf-8") as f:
        report = json.load(f)
    verification = verify_gate_document(report)
    if not verification.ok:
        raise ValueError("invalid behavioral gate report: " + "; ".join(verification.errors))
    return report, verification


def verify_gate_document(document: dict) -> GateVerification:
    """Verify either one GateResult document or a multi-section live-run document.

    JSON-array runs persist ``scan`` and ``marker`` sections, while gutter runs persist a direct
    result plus metadata. Every non-null result section must independently re-derive cleanly.
    """
    if isinstance(document.get("records"), list):
        return verify_gate_report(document)
    sections = [name for name in ("scan", "marker") if document.get(name) is not None]
    errors: list[str] = []
    if not sections:
        errors.append("no verifiable gate-result section found")
    for name in sections:
        section = document.get(name)
        if not isinstance(section, dict):
            errors.append(f"{name} section is not an object")
            continue
        result = verify_gate_report(section)
        errors.extend(f"{name}: {err}" for err in result.errors)
    blob = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return GateVerification(not errors, tuple(errors), hashlib.sha256(blob).hexdigest())


_RETRIEVAL_TOOL = {
    "name": "retrieve_elided",
    "description": (
        "Retrieve the original bytes elided behind a ccr:// marker. Pass the exact ref string from a "
        "'[… elided … · ccr://<hash>#<lo>-<hi>]' marker; returns the dropped JSON fragment verbatim."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"ref": {"type": "string", "description": "the ccr:// ref to resolve"}},
        "required": ["ref"],
    },
}


def _invoke(ask_model: AskModel, prompt: str, tools: list, resolver: StubResolver) -> dict:
    """Call the injected model, passing `resolver` as a keyword only if it accepts one — a plain
    `(prompt, tools)` fake in a test still works; the real driver takes `resolver=` to serve the
    retrieval tool. Keeps the injection contract permissive without forcing every fake to thread it."""
    import inspect

    try:
        params = inspect.signature(ask_model).parameters
        takes_resolver = "resolver" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        takes_resolver = False
    if takes_resolver:
        return ask_model(prompt, tools, resolver=resolver)
    return ask_model(prompt, tools)


def _classify(task: GateTask, model_out: dict) -> Outcome:
    correct = task.answer_matches(model_out.get("answer", ""))
    retrieved = bool(model_out.get("retrieved_refs"))
    if correct and not retrieved:
        return Outcome.CORRECT_WITHOUT_RETRIEVAL
    if correct and retrieved:
        return Outcome.CORRECT_WITH_RETRIEVAL
    if not correct and retrieved:
        return Outcome.WRONG_WITH_RETRIEVAL
    return Outcome.WRONG_WITHOUT_RETRIEVING


def _build_prompt(task: GateTask, crushed: str, marker_hint: str) -> str:
    return (
        "You are answering a question about a tool result. Parts of it may have been elided behind "
        f"markers like {marker_hint}. If the answer is behind such a marker, call `retrieve_elided` "
        "with the marker's `ccr://` ref to get the original bytes; otherwise answer directly.\n\n"
        f"Tool result:\n{crushed}\n\n"
        f"Question: {task.question}\n"
        "Answer with just the value."
    )


# Per-transform gate wiring: how to render a block, what marker hint to show the model, and how to
# build the resolver that serves retrievals. Keeps run_gate transform-agnostic.
_MARKER_HINTS = {
    "json_crush": "`[… elided N of M elements (idx a–b) · ccr://<hash>#<lo>-<hi>]`",
    "file_read_strip": "`[… line-number gutter stripped: N lines · ccr://<hash>]`",
}


def _render_and_resolver(transform: str, content: str, knobs: dict):
    """Render `content` under `transform` and build a resolver that serves its retrievals. Returns
    (rendering_text, resolver). Each ccr_retrieval transform carries its original; the stub resolver
    serves it back keyed on the ref the marker puts on the wire."""
    if transform == "file_read_strip":
        from apex_router.proxy_engine.pipeline.transforms import file_read_strip

        rendering = file_read_strip.run(Block(content=content, tool_name="tool_result"), knobs)
        resolver = StubResolver()
        # gutter-strip carries ONE ref for the whole block → the guttered original
        resolver._map[file_read_strip.ccr_ref(content)] = file_read_strip.resolve_original(content)
        return rendering.text, resolver
    # default: json_crush
    rendering = json_crush.run(Block(content=content, tool_name="tool_result"), knobs)
    resolver = StubResolver()
    resolver.register(content, knobs)
    return rendering.text, resolver


def run_gate(
    tasks: list[GateTask], *, ask_model: AskModel, transform: str = "json_crush"
) -> GateResult:
    """Run the behavioral gate over `tasks` with an injected model, using `transform` to render each
    block (json_crush for array probes, file_read_strip for gutter probes). For each task: render the
    block, build a stub resolver, prompt the model with the rendered bytes + the retrieval tool, and
    classify the outcome. Pure w.r.t. `ask_model` — same model → same result."""
    outcomes: list[Outcome] = []
    records: list[dict] = []
    hint = _MARKER_HINTS.get(transform, _MARKER_HINTS["json_crush"])
    for task in tasks:
        rendered, resolver = _render_and_resolver(transform, task.content, task.knobs)
        prompt = _build_prompt(task, rendered, hint)
        model_out = _invoke(ask_model, prompt, [_RETRIEVAL_TOOL], resolver)
        outcome = _classify(task, model_out)
        outcomes.append(outcome)
        records.append(
            {
                "task_id": task.task_id,
                "content_hash": hashlib.sha256(task.content.encode("utf-8")).hexdigest(),
                "kind": task.kind,
                "stratum": task.stratum,
                "question": task.question,
                "correct_answer": task.correct_answer,
                "answer_type": task.answer_type.value,
                "expected_retrieval": task.expected_retrieval,
                "model_answer": model_out.get("answer", ""),
                "retrieved_refs": list(model_out.get("retrieved_refs") or []),
                "outcome": outcome.value,
            }
        )
    return GateResult(outcomes=outcomes, n=len(tasks), records=records, transform=transform)
