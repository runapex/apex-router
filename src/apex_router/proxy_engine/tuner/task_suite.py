"""Δ14 task suite — build array-middle-dependent probes from real JSON (roadmap §Δ14).

The behavioral gate needs tasks whose answer provably lives in content the crusher ELIDES, so a
correct answer requires retrieval (or a richer marker). This generates them mechanically: parse a
crushable JSON array, find the records the crusher drops (indices strictly inside [keep_head,
len-keep_tail)), pick one with a scalar field, and ask for that field's value — the ground truth is
read from the ORIGINAL, so the gate scores against truth, not the crushed wire.

Deterministic (no RNG — pick by a fixed stride so a fixed corpus → a fixed suite). Read-only.
"""

from __future__ import annotations

import json
import re

from apex_router.proxy_engine.pipeline.transforms.json_crush import (
    DEFAULT_KEEP_HEAD,
    DEFAULT_KEEP_TAIL,
    _try_load,
)
from apex_router.proxy_engine.tuner.behavioral_gate import AnswerType, GateTask

# Fields we know how to ask about: a scalar (int/str) keyed by a stable id-like field. Kept simple —
# the probe just needs a deterministic (locator, answer) pair inside an elided record.
_ID_KEYS = ("id", "uuid", "key", "name")
_ANSWER_KEYS = ("score", "value", "count", "status", "size", "total")


def _scalar(v) -> bool:
    return isinstance(v, (int, float, str)) and not isinstance(v, bool)


def build_scan_tasks(
    content: str, *, max_tasks: int = 5, knobs: dict | None = None
) -> list[GateTask]:
    """Build up to `max_tasks` GateTasks whose answers live in elided array-middle records of
    `content`. Empty when `content` isn't a crushable JSON array or nothing is elided."""
    knobs = knobs or {}
    obj = _try_load(content)
    if not isinstance(obj, list):
        return []
    keep_head = int(knobs.get("json_keep_head", DEFAULT_KEEP_HEAD))
    keep_tail = int(knobs.get("json_keep_tail", DEFAULT_KEEP_TAIL))
    lo, hi = keep_head, len(obj) - keep_tail  # elided indices are [lo, hi)
    if hi - lo <= 0:
        return []
    # candidate elided records that are objects with an id-like key AND a scalar answer key
    candidates = []
    for idx in range(lo, hi):
        rec = obj[idx]
        if not isinstance(rec, dict):
            continue
        id_key = next((k for k in _ID_KEYS if k in rec and _scalar(rec[k])), None)
        ans_key = next((k for k in _ANSWER_KEYS if k in rec and _scalar(rec[k])), None)
        if id_key and ans_key and id_key != ans_key:
            candidates.append((idx, id_key, ans_key, rec))
    if not candidates:
        return []
    # deterministic spread across the elided middle: evenly-strided picks, not the first N
    stride = max(1, len(candidates) // max_tasks)
    picks = candidates[::stride][:max_tasks]
    tasks: list[GateTask] = []
    for _idx, id_key, ans_key, rec in picks:
        tasks.append(
            GateTask(
                content=content,
                question=f"What is the `{ans_key}` of the record whose `{id_key}` is {rec[id_key]!r}?",
                correct_answer=str(rec[ans_key]),
                knobs=knobs,
                answer_type=(
                    AnswerType.INTEGER if isinstance(rec[ans_key], int) else AnswerType.EXACT
                ),
                expected_retrieval=True,
            )
        )
    return tasks


def build_marker_tasks(
    content: str, *, max_tasks: int = 3, knobs: dict | None = None
) -> list[GateTask]:
    """Build tasks whose answer is ON THE WIRE — in the crush marker's counted total, or in a retained
    head record (the verbatim exemplar). A model that answers these WITHOUT retrieving is ideal; one
    that retrieves is over-retrieving. The complement to `build_scan_tasks` (answer needs retrieval);
    together they bracket the propensity≠necessity measurement (roadmap §Δ14). Empty when nothing
    elides (no marker, no over-retrieval to measure)."""
    knobs = knobs or {}
    obj = _try_load(content)
    if not isinstance(obj, list):
        return []
    keep_head = int(knobs.get("json_keep_head", DEFAULT_KEEP_HEAD))
    keep_tail = int(knobs.get("json_keep_tail", DEFAULT_KEEP_TAIL))
    if len(obj) - (keep_head + keep_tail) <= 0:
        return []  # nothing elided → no marker
    tasks: list[GateTask] = []
    # (1) counted total — the M in "elided N of M elements", present verbatim in the marker text.
    tasks.append(
        GateTask(
            content=content,
            question="How many total elements are in this array (including any elided)? Answer the count.",
            correct_answer=str(len(obj)),
            knobs=knobs,
            answer_type=AnswerType.INTEGER,
            expected_retrieval=False,
            kind="marker",
        )
    )
    # (2) retained head-record field — the intact exemplar at index 0 survives verbatim on the wire.
    head = obj[0] if obj else None
    if isinstance(head, dict):
        id_key = next((k for k in _ID_KEYS if k in head and _scalar(head[k])), None)
        ans_key = next((k for k in _ANSWER_KEYS if k in head and _scalar(head[k])), None)
        if id_key and ans_key and id_key != ans_key:
            tasks.append(
                GateTask(
                    content=content,
                    question=f"What is the `{ans_key}` of the FIRST record (its `{id_key}` is "
                    f"{head[id_key]!r})?",
                    correct_answer=str(head[ans_key]),
                    knobs=knobs,
                    answer_type=(
                        AnswerType.INTEGER if isinstance(head[ans_key], int) else AnswerType.EXACT
                    ),
                    expected_retrieval=False,
                    kind="marker",
                )
            )
        elif ans_key:
            tasks.append(
                GateTask(
                    content=content,
                    question=f"What is the `{ans_key}` of the first record in the array?",
                    correct_answer=str(head[ans_key]),
                    knobs=knobs,
                    answer_type=(
                        AnswerType.INTEGER if isinstance(head[ans_key], int) else AnswerType.EXACT
                    ),
                    expected_retrieval=False,
                    kind="marker",
                )
            )
    return tasks[:max_tasks]


_GUTTER_LINE = re.compile(r"^(\s*)(\d+)([\t:|])(.*)$")


def _infer_file_kind(lines: list[str]) -> str:
    text = "\n".join(lines)
    if re.search(r"\b(def|class|function|func|return|const|let|var|import|package)\b", text):
        return "code"
    if re.search(r"^\s*#{1,6}\s+", text, re.M):
        return "markdown"
    if re.search(r"^\s*[A-Za-z_][\w.-]*\s*[:=]\s*\S+", text, re.M):
        return "config"
    if re.search(r"\b(INFO|WARN|ERROR|DEBUG|TRACE)\b", text):
        return "log"
    return "text"


def _distinctive_token(text: str) -> str | None:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{4,}", text)
    return tokens[0] if tokens else None


def _content_relation_task(
    content: str, rows: list[tuple[int, str]], knobs: dict
) -> GateTask | None:
    """Create a non-tautological question whose answer survives gutter stripping.

    Unlike the old "does token X appear?" probe, these tasks require reading a relation in the
    retained content: function→return value, key→value, heading→next heading, or log token→level.
    """
    kind = _infer_file_kind([text for _num, text in rows])
    if kind == "code":
        for _num, text in rows:
            m = re.search(
                r"\b(?:def|func|function)\s+([A-Za-z_]\w*)[^\n]*?\breturn\s+([-+]?\d+|[A-Za-z_]\w*)",
                text,
            )
            if m:
                return GateTask(
                    content=content,
                    question=f"What exact value does `{m.group(1)}` return?",
                    correct_answer=m.group(2),
                    knobs=knobs,
                    kind="content_only",
                    answer_type=(
                        AnswerType.INTEGER
                        if re.fullmatch(r"[-+]?\d+", m.group(2))
                        else AnswerType.EXACT
                    ),
                    expected_retrieval=False,
                    stratum=kind,
                )
    if kind == "config":
        for _num, text in rows:
            m = re.match(r"\s*([A-Za-z_][\w.-]*)\s*[:=]\s*([^#;]+?)\s*$", text)
            if m:
                return GateTask(
                    content=content,
                    question=f"What exact value is assigned to `{m.group(1)}`?",
                    correct_answer=m.group(2).strip(),
                    knobs=knobs,
                    kind="content_only",
                    answer_type=AnswerType.NORMALIZED_TEXT,
                    expected_retrieval=False,
                    stratum=kind,
                )
    if kind == "markdown":
        headings = []
        for _num, text in rows:
            m = re.match(r"\s*#{1,6}\s+(.+?)\s*$", text)
            if m:
                headings.append(m.group(1))
        if len(headings) >= 2:
            return GateTask(
                content=content,
                question=f"Which heading comes immediately after `{headings[0]}`?",
                correct_answer=headings[1],
                knobs=knobs,
                kind="content_only",
                answer_type=AnswerType.NORMALIZED_TEXT,
                expected_retrieval=False,
                stratum=kind,
            )
    if kind == "log":
        for _num, text in rows:
            level = re.search(r"\b(INFO|WARN|ERROR|DEBUG|TRACE)\b", text)
            token = _distinctive_token(text)
            if level and token and token != level.group(1):
                return GateTask(
                    content=content,
                    question=f"What log level appears in the entry containing `{token}`?",
                    correct_answer=level.group(1),
                    knobs=knobs,
                    kind="content_only",
                    answer_type=AnswerType.EXACT,
                    expected_retrieval=False,
                    stratum=kind,
                )
    return None


def build_gutter_tasks(
    content: str, *, max_tasks: int = 4, knobs: dict | None = None
) -> list[GateTask]:
    """Build stratified behavioral probes for the file_read gutter-strip transform.

    A line-number task is marked retrieval-dependent only when the original numbering is NOT
    derivable from the stripped line ordinal (non-1 start or a gap). For ordinary 1..N numbering,
    the model can count retained lines, so calling retrieval "necessary" would be false evidence.
    Content-preserving tasks ask a relation (function→value, key→value, heading order, log level),
    never the old tautology "does a token mined from this block appear?".

    Kinds:
      - gutter_dependent: recover an original, non-derivable line number;
      - content_only: answer a non-tautological relation preserved verbatim, without retrieval;
      - absent_fact: reject a fact not present in the preserved content.
    Empty when the block isn't a line-number-guttered file read."""
    knobs = knobs or {}
    lines = content.split("\n")
    guttered = [(int(m.group(2)), m.group(4)) for ln in lines if (m := _GUTTER_LINE.match(ln))]
    if len(guttered) < 4:
        return []
    tasks: list[GateTask] = []
    file_kind = _infer_file_kind([text for _num, text in guttered])

    # Original-line-number lookup. It is a genuine lossy dependency only if line numbers are not
    # equivalent to 1-based ordinals in the stripped block.
    non_derivable = [
        (ordinal, num, text)
        for ordinal, (num, text) in enumerate(guttered, start=1)
        if num != ordinal
    ]
    for _ordinal, num, text in non_derivable[: max(1, max_tasks // 3)]:
        token = _distinctive_token(text)
        if not token:
            continue
        tasks.append(
            GateTask(
                content=content,
                question=f"What is the original line number containing the exact token `{token}`?",
                correct_answer=str(num),
                knobs=knobs,
                kind="gutter_dependent",
                answer_type=AnswerType.INTEGER,
                expected_retrieval=True,
                stratum=file_kind,
            )
        )

    relation = _content_relation_task(content, guttered, knobs)
    if relation:
        tasks.append(relation)

    # Negative/absence control: use a deterministic identifier proven absent from the original.
    # This catches hallucinated facts and the exact negation/substr bug without exposing content.
    content_text = "\n".join(text for _num, text in guttered)
    suffix = 0
    absent = "apex_absent_identifier"
    while absent in content_text:
        suffix += 1
        absent = f"apex_absent_identifier_{suffix}"
    tasks.append(
        GateTask(
            content=content,
            question=f"Does the exact identifier `{absent}` occur in this file? Answer yes or no.",
            correct_answer="no",
            knobs=knobs,
            kind="absent_fact",
            answer_type=AnswerType.BOOLEAN,
            expected_retrieval=False,
            stratum=file_kind,
        )
    )
    return tasks[:max_tasks]


def build_suite_from_corpus(
    blocks: list[str], *, per_block: int = 2, cap: int = 40
) -> list[GateTask]:
    """Build a behavioral-gate suite from a list of raw JSON tool-result blocks (e.g. the
    conversational-regime frontier blocks). Skips non-crushable blocks; caps the total so a live run
    stays inside budget. Deterministic given the block order."""
    out: list[GateTask] = []
    for block in blocks:
        if len(out) >= cap:
            break
        out.extend(build_scan_tasks(block, max_tasks=per_block))
    return out[:cap]


def _load_json_blocks_from_jsonl(path: str) -> list[str]:
    """Pull candidate JSON array strings from a jsonl of frontier blocks (one {"text": ...} per line
    or a bare string). Helper for the live driver script; tolerant of shape."""
    blocks: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = d.get("text") if isinstance(d, dict) else (d if isinstance(d, str) else None)
            if isinstance(text, str) and text.lstrip()[:1] in ("[", "{"):
                blocks.append(text)
    return blocks
