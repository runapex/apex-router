"""ASD-STE100 Simplified Technical English — apex-router's default format for instructions and briefs.

One source of truth: `data/ste_rules.json` (rules block, brief block, limits, word map). The pi extensions
(`integrations/pi/ste.ts`) read the same file. This module gives:

  rules_text()          the block apex-router injects into agent system prompts (pi) and CLAUDE.md
  brief_text()          the sub-agent brief template
  check(text, kind)     mechanical findings: sentence length, -ing forms, passive voice, non-STE words
  apex-router ste rules | brief | check FILE|- [--kind procedure|description] [--json] | install-claude

The checker covers the mechanical STE rules only. It does not hold the licensed STE dictionary, so a clean
result means "no mechanical findings", not "STE-compliant". Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent / "data" / "ste_rules.json"
CLAUDE_BEGIN = "<!-- apex-router:ste:begin -->"
CLAUDE_END = "<!-- apex-router:ste:end -->"


@lru_cache(maxsize=1)
def rules() -> dict:
    return json.loads(RULES_PATH.read_text())


def rules_text() -> str:
    return "\n".join(rules()["prompt_block"])


def brief_text() -> str:
    return "\n".join(rules()["brief_block"])


@dataclass
class Finding:
    line: int
    rule: str
    text: str
    hint: str


_CODE = re.compile(r"```.*?```", re.S)
_SPAN = re.compile(r"`[^`\n]*`")
_STEP = re.compile(r"^\s*(?:\d+[.)]|[A-Z][.)]|[-*])\s+")


def _prose_lines(text: str):
    """(line_no, line) for prose: code fences, table rows, headings and blank lines are skipped;
    inline code spans count as one word."""
    fenced = set()
    for m in _CODE.finditer(text):
        a = text.count("\n", 0, m.start())
        fenced.update(range(a, a + m.group(0).count("\n") + 1))
    for i, line in enumerate(text.split("\n")):
        t = line.strip()
        if i in fenced or not t or t.startswith(("#", "|", ">", "<!--")):
            continue
        yield i + 1, _SPAN.sub("X", line)


def check(text: str, kind: str = "auto") -> list:
    """Mechanical STE findings. kind: 'procedure' (20 words), 'description' (25), or 'auto' (a numbered or
    bulleted line is a procedure step, other prose is description)."""
    r = rules()
    lim = r["limits"]
    allowed = set(r["ing_allowed"])
    aux = "|".join(r["passive_aux"])
    parts = "|".join(map(re.escape, r["participles"]))
    passive = re.compile(rf"\b({aux})\s+(?:not\s+)?(\w+ed|{parts})\b", re.I)
    phrases = sorted(r["replace"], key=len, reverse=True)
    out = []
    for n, line in _prose_lines(text):
        is_step = bool(_STEP.match(line)) if kind == "auto" else kind == "procedure"
        limit = lim["procedure_words"] if is_step else lim["description_words"]
        body = _STEP.sub("", line).strip()
        for sent in re.split(r"(?<=[.!?:;])\s+", body):
            w = len(sent.split())
            if w > limit:
                out.append(Finding(n, "length", sent[:80], f"{w} words > {limit}: split into shorter sentences"))
        low = f" {body.lower()} "
        for ph in phrases:
            if re.search(rf"(?<![\w-]){re.escape(ph)}(?![\w-])", low):
                rep = r["replace"][ph]
                out.append(Finding(n, "word", ph, f"use '{rep}'" if rep else "delete it"))
        for m in re.finditer(r"\b([A-Za-z]{3,}ing)\b", body):
            if m.group(1).lower() not in allowed:
                out.append(Finding(n, "ing", m.group(1), "use a verb in a simple tense, or a noun"))
        for m in passive.finditer(body):
            out.append(Finding(n, "passive", m.group(0), "use the active voice: name who does it"))
    return out


def install_claude(path: Path) -> str:
    """Put the rules block into a CLAUDE.md between apex-router markers. Idempotent; text outside the
    markers is never changed. Returns 'added' | 'updated' | 'unchanged'."""
    block = f"{CLAUDE_BEGIN}\n{rules_text()}\n\n{brief_text()}\n{CLAUDE_END}"
    old = path.read_text() if path.exists() else ""
    if CLAUDE_BEGIN in old and CLAUDE_END in old:
        a, b = old.index(CLAUDE_BEGIN), old.index(CLAUDE_END) + len(CLAUDE_END)
        new = old[:a] + block + old[b:]
        state = "unchanged" if new == old else "updated"
    else:
        new = (old.rstrip("\n") + "\n\n" if old else "") + block + "\n"
        state = "added"
    if new != old:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)
    return state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router ste", description="ASD-STE100 format: rules, brief, checker.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rules", help="print the STE block apex-router injects into agent prompts")
    sub.add_parser("brief", help="print the sub-agent brief template")
    c = sub.add_parser("check", help="mechanical STE findings for a file or stdin (exit 1 when any)")
    c.add_argument("path", help="file, or - for stdin")
    c.add_argument("--kind", choices=["auto", "procedure", "description"], default="auto")
    c.add_argument("--json", action="store_true")
    i = sub.add_parser("install-claude", help="add/refresh the STE block in a CLAUDE.md (between markers)")
    i.add_argument("--path", type=Path, default=Path.home() / ".claude" / "CLAUDE.md")
    a = ap.parse_args(argv)
    if a.cmd == "rules":
        print(rules_text())
        return 0
    if a.cmd == "brief":
        print(brief_text())
        return 0
    if a.cmd == "install-claude":
        print(f"{install_claude(a.path)}: {a.path}")
        return 0
    text = sys.stdin.read() if a.path == "-" else Path(a.path).read_text()
    found = check(text, a.kind)
    if a.json:
        print(json.dumps([asdict(f) for f in found], indent=1))
    else:
        for f in found:
            print(f"{a.path}:{f.line}: [{f.rule}] {f.text} -> {f.hint}")
        print(f"{len(found)} finding(s)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
