#!/usr/bin/env python3
"""apex-router prefix-budget check — measure the re-read-every-turn prefix.

Cache-read cost is dominated by the *fixed* prefix that a Claude Code session
re-reads on every turn: global + project CLAUDE.md and the tool-schema JSON.
This tool measures that prefix, ranks its contributors, and compares the total
to a budget. Advisory only — it edits nothing; trimming (prompt-audit, deferring
MCP tool schemas) stays a human decision.

Token counting is pluggable: it uses the Anthropic SDK's count_tokens when the
SDK + credentials are present (exact), and otherwise falls back to a clearly
labeled character-based ESTIMATE so the tool still works offline. It never passes
an estimate off as exact.

Run:
    python3 scripts/prefix_budget.py --claude-md ~/.claude/CLAUDE.md \\
        --project-md ./CLAUDE.md --tools tools.json --budget 8000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MODEL = "claude-opus-5"
# Chars-per-token for the offline fallback. ~3.5 is a conservative (over-)estimate
# for English+code with the Opus tokenizer; deliberately not tuned to precision —
# the fallback is for relative ranking, not billing.
FALLBACK_CHARS_PER_TOKEN = 3.5


def count_tokens_sdk(text: str, model: str = MODEL):
    """Exact count via the Anthropic API, or None if unavailable (no SDK, no
    creds, or network error). Never raises — the caller falls back."""
    try:
        import anthropic  # noqa: PLC0415
    except Exception:
        return None
    try:
        client = anthropic.Anthropic()
        resp = client.messages.count_tokens(
            model=model, messages=[{"role": "user", "content": text}]
        )
        return int(resp.input_tokens)
    except Exception:
        return None


def estimate_tokens(text: str, chars_per_token: float = FALLBACK_CHARS_PER_TOKEN) -> int:
    """Offline character-based estimate. Labeled as an estimate everywhere it's used."""
    return int(len(text) / chars_per_token) if text else 0


def count_text(text: str, *, counter=None) -> tuple[int, bool]:
    """Return (tokens, exact). Prefer the exact counter; fall back to estimate.

    `counter` defaults to the module-level count_tokens_sdk, resolved at call time
    (not def time) so tests can monkeypatch it and callers can inject a fake."""
    if counter is None:
        counter = count_tokens_sdk
    exact = counter(text)
    if exact is not None:
        return exact, True
    return estimate_tokens(text), False


def measure_components(components: list[tuple[str, str]], *, counter=None) -> dict:
    """`components` is [(label, text), ...]. Returns per-component token counts,
    the total, whether any count is an estimate, and a size-ranked list."""
    rows = []
    any_estimate = False
    for label, text in components:
        toks, exact = count_text(text, counter=counter)
        any_estimate = any_estimate or not exact
        rows.append({"component": label, "tokens": toks, "exact": exact,
                     "chars": len(text or "")})
    total = sum(r["tokens"] for r in rows)
    rows.sort(key=lambda r: r["tokens"], reverse=True)
    return {"total_tokens": total, "any_estimate": any_estimate, "components": rows}


def _read(path) -> str:
    if not path:
        return ""
    p = Path(path).expanduser()
    try:
        return p.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return ""


def _tools_text(tools_path) -> str:
    """Serialize a tools JSON file deterministically (sorted keys) — that's the
    byte shape that sits at prefix position 0 and gets re-read every turn."""
    raw = _read(tools_path)
    if not raw.strip():
        return ""
    try:
        obj = json.loads(raw)
        return json.dumps(obj, sort_keys=True)
    except json.JSONDecodeError:
        return raw  # measure it as-is rather than fail


def build_report(*, claude_md=None, project_md=None, tools=None, budget=None,
                 counter=None) -> dict:
    comps = [
        ("global CLAUDE.md", _read(claude_md)),
        ("project CLAUDE.md", _read(project_md)),
        ("tool schemas", _tools_text(tools)),
    ]
    comps = [(lbl, txt) for lbl, txt in comps if txt]  # drop absent inputs
    m = measure_components(comps, counter=counter)
    over = (budget is not None and m["total_tokens"] > budget)
    return {
        "schema": "prefix-budget/1",
        "model": MODEL,
        "budget": budget,
        "over_budget": over,
        "overage_tokens": (m["total_tokens"] - budget) if (over and budget is not None) else 0,
        **m,
    }


def _fmt_text(rep: dict) -> str:
    lines = []
    est = "  (⚠ ESTIMATE — Anthropic SDK/creds unavailable, char-based)" if rep["any_estimate"] else ""
    lines.append(f"=== PREFIX BUDGET ({rep['model']}){est} ===")
    lines.append(f"  total prefix = {rep['total_tokens']:,} tokens"
                 + (f"  / budget {rep['budget']:,}" if rep['budget'] is not None else ""))
    if rep["over_budget"]:
        lines.append(f"  ⚠ OVER BUDGET by {rep['overage_tokens']:,} tokens — "
                     f"this is re-read every turn (cache-read). Trim the biggest contributor.")
    lines.append("  contributors (largest first):")
    for r in rep["components"]:
        tag = "" if r["exact"] else " (est)"
        lines.append(f"    {r['component']:20} {r['tokens']:>8,} tok{tag}  ({r['chars']:,} chars)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="measure the re-read-every-turn Claude Code prefix")
    ap.add_argument("--claude-md", default=str(Path.home() / ".claude" / "CLAUDE.md"),
                    help="global CLAUDE.md (default ~/.claude/CLAUDE.md)")
    ap.add_argument("--project-md", default=None, help="project-level CLAUDE.md")
    ap.add_argument("--tools", default=None, help="tools JSON (the tool-schema block)")
    ap.add_argument("--budget", type=int, default=None,
                    help="token budget; --check exits 2 if exceeded")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true", help="exit 2 if over budget")
    args = ap.parse_args(argv)

    rep = build_report(claude_md=args.claude_md, project_md=args.project_md,
                       tools=args.tools, budget=args.budget)
    print(json.dumps(rep, indent=2) if args.json else _fmt_text(rep))

    if args.check and rep["over_budget"]:
        print(f"\nprefix_budget: FAIL — {rep['overage_tokens']:,} tokens over budget",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
