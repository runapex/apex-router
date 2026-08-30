"""Structured continuation state for session handoffs (SKILL.state, arXiv:2608.26263).

The cache-handoff-nudge hook fires when a session's prefix has grown expensive: today it writes
a PROSE handoff doc ("skim the last few exchanges, paste the summary you need") — a textual
reconstruction of history, the exact artifact the paper shows causes re-anchoring (history-
based runtimes hallucinated 5–8 recovery turns after state drift; structured state: 0). This
module is the source of truth for the STRUCTURED state block that replaces prose as the
handoff payload: the session projects its transient reasoning into six persistent fields, the
fresh session starts from state, not summary.

The hook (hooks/cache-handoff-nudge.sh) EMBEDS this template in a heredoc (it runs under the
system python, no package import guaranteed). tests/test_handoff_state.py pins the two in
sync — edit the template HERE, then re-embed.

CLI:
  python -m apex_router.handoff_state template            # print the block (for re-embedding)
  python -m apex_router.handoff_state validate FILE       # check a filled handoff; exit 2 on problems
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# (field, guidance) — field names are the contract; the hook embed and validate() both key off them.
FIELDS: list[tuple[str, str]] = [
    ("goal", "one sentence: what this session is trying to achieve"),
    ("constraints", "hard requirements and the do-not-touch list"),
    ("decisions", "decisions ALREADY made, each with its reason — do not re-litigate"),
    ("files_touched", "paths created/modified, one per line, with what changed"),
    ("open_issues", "unresolved problems, failing tests, blockers"),
    ("next_action", "the single step the fresh session should take FIRST"),
]

PLACEHOLDER = "_…_"


def render_template() -> str:
    """The state block as embedded in the handoff doc. Ends with a newline."""
    lines = [
        "## Structured continuation state",
        "",
        "Fill this in BEFORE stopping — once FILLED, it is the only context the fresh session",
        "needs. Record current state, not history: discard reasoning, keep decisions and facts.",
        "If any field is still " + PLACEHOLDER + " the handoff is INCOMPLETE — do not paste it; fill it",
        "first. Check with: python -m apex_router.handoff_state validate <this file>.",
        "(SKILL.state handoff — schema: apex_router.handoff_state.FIELDS)",
        "",
    ]
    lines += [f"- **{name}**: {PLACEHOLDER} {guidance}" for name, guidance in FIELDS]
    return "\n".join(lines) + "\n"


def validate(text: str) -> list[str]:
    """Check a FILLED handoff for the state contract. Returns a list of problems ([] = valid).

    A field line must match `- **<field>**: <value>` at line start (modulo indent) — a
    mention like `note: **goal** unavailable` is NOT a field line. Every occurrence of a field
    line is checked (a duplicated placeholder can't hide behind a filled one). Problems: field
    missing entirely, value still the placeholder, or value empty. Advisory (exit-2 CLI) — a
    handoff with problems is still a handoff, just a degraded one.
    """
    problems = []
    for name, _ in FIELDS:
        pat = re.compile(rf"^\s*-\s*\*\*{re.escape(name)}\*\*\s*:\s*(.*)$")
        values = [m.group(1).strip() for ln in text.splitlines() if (m := pat.match(ln))]
        if not values:
            problems.append(f"missing field: {name}")
            continue
        if any(not v for v in values):
            problems.append(f"empty field: {name}")
        if any(v.startswith(PLACEHOLDER) for v in values):
            problems.append(f"unfilled placeholder: {name}")
    return problems


def _cli(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] == "template":
        sys.stdout.write(render_template())
        return 0
    if args[0] == "validate" and len(args) == 2:
        problems = validate(Path(args[1]).read_text())
        for p in problems:
            print(f"PROBLEM: {p}")
        if not problems:
            print("handoff state: valid")
        return 2 if problems else 0
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
