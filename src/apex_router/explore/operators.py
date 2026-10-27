"""The 6 island operators and the prompt builders for `apex-router explore`.

Isolation rule: `diverge_prompt` takes ONLY goal, constraints, facts, negative evidence and the operator.
It has no parameter for another candidate, so it cannot contain another candidate's text.
Only `evolve_prompt` (the evolve phase) shows parent texts, and only the champions it mutates or combines.
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

OPERATORS: dict[str, str] = {
    "first-principles": (
        "Reason from first principles. Name the basic quantities and the true cost of the work. "
        "Remove every step that the goal does not need, then build the solution from those parts."),
    "inversion": (
        "Invert the problem. Ask what makes the result bad or slow, and design to remove that cause. "
        "Consider computing the complement, working backwards, or swapping the roles of input and output."),
    "cross-domain": (
        "Take a mechanism from a different field (for example physics, logistics, biology, databases, "
        "signal processing) and adapt it to this goal. Name the field in your tags."),
    "constraint-mutation": (
        "Change one assumption on purpose: drop a constraint, add a constraint, or change a bound. "
        "Solve the changed problem, then make the solution meet the real constraints again."),
    "adversarial": (
        "Think like an attacker of the obvious solution. Find the input that breaks it or makes it slow, "
        "and design a solution that stays correct and fast on that input."),
    "simplification": (
        "Find the simplest mechanism that can meet the goal. Prefer fewer moving parts and a known "
        "standard technique over a clever one."),
}

FORMAT = (
    "Reply in this format and nothing else:\n"
    "MECHANISM: <3 to 6 short comma-separated tags that name the core technique, not the wording>\n"
    "CANDIDATE:\n"
    "```\n<the full candidate>\n```\n")


def _block(title: str, items: Sequence[str] | str | None) -> str:
    if not items:
        return ""
    if isinstance(items, str):
        body = items.strip()
    else:
        body = "\n".join(f"- {x}" for x in items)
    return f"## {title}\n{body}\n\n"


def diverge_prompt(operator: str, goal: str, constraints: Sequence[str] = (), facts: Sequence[str] = (),
                   negative: Sequence[str] = ()) -> str:
    """Diverge-phase prompt: goal + constraints + facts + negative evidence + operator. No other candidate."""
    if operator not in OPERATORS:
        raise KeyError(f"unknown operator {operator!r}")
    return (
        f"You are one independent explorer. Operator: {operator}.\n{OPERATORS[operator]}\n\n"
        + _block("Goal", goal)
        + _block("Constraints", list(constraints))
        + _block("Facts", list(facts))
        + _block("Negative evidence (mechanisms already rejected; do not repeat them)", list(negative))
        + FORMAT)


def baseline_prompt(goal: str, constraints: Sequence[str] = (), facts: Sequence[str] = ()) -> str:
    """The plain prompt of the baseline arm: no operator, no evidence, same output format."""
    return (_block("Goal", goal) + _block("Constraints", list(constraints)) + _block("Facts", list(facts))
            + FORMAT)


def evolve_prompt(mode: str, goal: str, parents: Sequence[tuple[str, str, str]],
                  constraints: Sequence[str] = (), negative: Sequence[str] = ()) -> str:
    """Evolve-phase prompt. parents = [(text, tags, oracle detail)]. mode 'mutate' (1 parent) or 'combine' (2)."""
    if mode == "mutate":
        if len(parents) != 1:
            raise ValueError("mutate needs 1 parent")
        head = ("Improve this champion. Change ONE mechanism at a time. Keep what the oracle accepted.\n\n")
    elif mode == "combine":
        if len(parents) != 2:
            raise ValueError("combine needs 2 parents")
        head = ("Combine these two champions from different niches into one candidate that keeps the "
                "strength of each.\n\n")
    else:
        raise ValueError(f"unknown evolve mode {mode!r}")
    pblocks = ""
    for i, (text, tags, detail) in enumerate(parents, 1):
        pblocks += (f"## Parent {i} (mechanism: {tags})\nOracle: {detail}\n```\n{text.strip()}\n```\n\n")
    return (head + _block("Goal", goal) + _block("Constraints", list(constraints)) + pblocks
            + _block("Negative evidence (mechanisms already rejected; do not repeat them)", list(negative))
            + FORMAT)


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_MECH = re.compile(r"^\s*\**\s*MECHANIS[MT]\w*\s*\**\s*:\s*(.+)$", re.M | re.I)   # "MECHANISTIC:" seen live
_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)


def parse_reply(reply: str) -> tuple[list[str], str]:
    """-> (tags, candidate text). Tags from the MECHANISM line; text from the first fenced block after
    'CANDIDATE', else the first fenced block, else the text after the MECHANISM line."""
    reply = _THINK.sub("", reply or "")
    tags: list[str] = []
    m = _MECH.search(reply)
    if m:
        raw = m.group(1).strip().lstrip("*").strip()
        tags = [t.strip(" *`") for t in re.split(r"[,;]", raw) if t.strip(" *`")]
    tail = reply
    k = re.search(r"CANDIDATE\s*\**\s*:?", reply, re.I)
    if k:
        tail = reply[k.end():]
    f = _FENCE.search(tail) or _FENCE.search(reply)
    if f:
        text = f.group(1)
    else:
        text = tail if k else (reply[m.end():] if m else reply)
    return tags, text.strip("\n")


def tags_text(tags: Iterable[str]) -> str:
    return ", ".join(tags) or "untagged"
