"""ast-grep outline — replace long code with signatures + elision markers. §7 (M3, offload).

A large source file in a tool result is mostly function bodies the model rarely needs verbatim.
astgrep extracts the structural OUTLINE — imports, class/def signatures — and replaces each body
with a marker recording the exact line span it elided. Fidelity is "external_retrieval": rendering
itself carries the line ranges, so an agent (or the pipeline) can re-Read the elided span without
a separate CCR store.

Rules (§7 table):
  - min_chars=500, min_defs=3: only fire on files big enough and with enough structure to gain.
  - SKIPS RANGED READS: a Read tool call with an offset/limit already returned a slice the user
    asked for — outlining it would fight the user's intent. Detected via block.meta.

Offload transform: shelling out to `ast-grep` is not sub-25ms, so the pipeline runs this in the
offload pool and monetizes it at the frontier (§5.1), never on the request's critical path.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from apex_router.proxy_engine.pipeline.transforms.base import Block, Rendering, Snapshot

name = "astgrep"
fidelity = "external_retrieval"
# Only min_defs is read from the knob snapshot (in run()). min_chars gates in applies(), which
# the Transform protocol calls WITHOUT a snapshot, so it can't be a tunable knob until the
# registry lands and applies() takes the snapshot — declaring it here would mislead the tuner
# into optimizing a dead knob (review finding). Left as a module constant for now.
knobs = ["min_defs"]

DEFAULT_MIN_CHARS = 500
DEFAULT_MIN_DEFS = 3

# suffix → (ast-grep lang id, file suffix for the temp file, def pattern). ast-grep infers the
# grammar from the file EXTENSION, so the temp file must carry the real suffix (a `.python`
# suffix silently returns zero matches). Kept small for v1; extend as fixtures demand.
_LANGS = {
    ".py": ("python", ".py", "def $N($$$A): $$$B"),
    ".js": ("javascript", ".js", "function $N($$$A) { $$$B }"),
    ".ts": ("typescript", ".ts", "function $N($$$A) { $$$B }"),
    ".go": ("go", ".go", "func $N($$$A) { $$$B }"),
    ".rs": ("rust", ".rs", "fn $N($$$A) { $$$B }"),
}


def _lang_for(block: Block) -> tuple[str, str, str] | None:
    path = block.meta.get("file_path") or block.meta.get("path") or ""
    suffix = Path(str(path)).suffix.lower()
    return _LANGS.get(suffix)


def _is_ranged_read(block: Block) -> bool:
    """A Read with offset/limit already returned the slice the user wanted — don't outline it."""
    if (block.tool_name or "").lower() != "read":
        return False
    m = block.meta
    return any(k in m and m[k] is not None for k in ("offset", "limit", "range", "start_line"))


def applies(block: Block) -> bool:
    if len(block.content) < DEFAULT_MIN_CHARS:
        return False
    if _is_ranged_read(block):
        return False
    return _lang_for(block) is not None


def _find_defs(content: str, lang: str, suffix: str, pattern: str) -> list[dict]:
    """Run ast-grep, return [{name, start_line, end_line}] (0-based lines). The temp file MUST
    carry the language's real suffix (e.g. .py) — ast-grep infers the grammar from it."""
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=True) as f:
        f.write(content)
        f.flush()
        try:
            proc = subprocess.run(
                ["ast-grep", "--pattern", pattern, "--lang", lang, "--json=compact", f.name],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"ast-grep unavailable: {e}") from e  # fail-open (§6)
    if proc.returncode not in (0, 1):  # 1 = no matches, still valid
        raise RuntimeError(f"ast-grep error: {proc.stderr[:200]}")
    if not proc.stdout.strip():
        return []
    matches = json.loads(proc.stdout)
    defs = []
    for m in matches:
        nm = m.get("metaVariables", {}).get("single", {}).get("N", {}).get("text", "?")
        rng = m["range"]
        defs.append({"name": nm, "start": rng["start"]["line"], "end": rng["end"]["line"]})
    return defs


def run(block: Block, knobs: Snapshot) -> Rendering:
    lang_pat = _lang_for(block)
    if lang_pat is None:
        raise ValueError("astgrep: unknown language")  # fail-open
    lang, suffix, pattern = lang_pat
    min_defs = int(knobs.get("min_defs", DEFAULT_MIN_DEFS))
    lines = block.content.split("\n")
    defs = _find_defs(block.content, lang, suffix, pattern)
    if len(defs) < min_defs:
        # not enough structure to gain — return the original unchanged (a no-op rendering)
        return Rendering(
            text=block.content,
            fidelity="external_retrieval",
            meta={"reason": "below min_defs", "defs": len(defs)},
        )

    # Keep only OUTERMOST defs: a def whose range is contained within another def's range is a
    # nested def (a method inside a class, a closure inside a function). Eliding the outer def
    # already elides the nested one — and its recover marker records the FULL span, so re-Reading
    # recovers everything including the nested def. Outlining a nested def separately would
    # double-elide or corrupt (cross-validation: `outer` [0-3] swallows `inner`'s signature). We sort by
    # (start asc, end desc) and drop any def whose [start,end] is inside the previous kept def.
    defs.sort(key=lambda d: (d["start"], -d["end"]))
    outer: list[dict] = []
    for d in defs:
        if outer and d["start"] >= outer[-1]["start"] and d["end"] <= outer[-1]["end"]:
            continue  # nested inside the last kept def → skip
        outer.append(d)

    # Build the outline: keep the signature line, replace the body with a marker recording the
    # elided line span so it's recoverable. Only elide bodies of >= MIN_ELIDE_LINES — replacing a
    # 1-line body with a longer marker would GROW the output (net loss).
    MIN_ELIDE_LINES = 2
    elide: dict[int, dict] = {}  # start_line → {name, start, end}
    for d in outer:
        if d["end"] - d["start"] >= MIN_ELIDE_LINES:  # body spans >= 2 lines past the signature
            elide[d["start"] + 1] = d
    out_lines: list[str] = []
    elided_spans: list[dict] = []
    i = 0
    while i < len(lines):
        if i in elide:
            d = elide[i]
            span_lines = d["end"] - d["start"]
            indent = " " * (len(lines[d["start"]]) - len(lines[d["start"]].lstrip()))
            # ast-grep is 0-BASED; the agent's Read tool is 1-BASED. Emit 1-based line numbers
            # in BOTH the marker text and the recover span so a re-Read of the marked range
            # lands on the right lines (review finding: off-by-one broke recoverability).
            s1, e1 = d["start"] + 1, d["end"] + 1
            out_lines.append(
                f"{indent}    # … {span_lines} lines elided [astgrep:{d['name']}:{s1}-{e1}]"
            )
            elided_spans.append({"name": d["name"], "start": s1, "end": e1})
            i = d["end"] + 1  # skip the elided body (0-based index into lines[])
        else:
            out_lines.append(lines[i])
            i += 1
    text = "\n".join(out_lines)
    # only worth it if we actually shrank it
    if len(text) >= len(block.content):
        return Rendering(
            text=block.content, fidelity="external_retrieval", meta={"reason": "no gain"}
        )
    return Rendering(
        text=text,
        fidelity="external_retrieval",
        recover={"elided": elided_spans, "orig_lines": len(lines)},
        meta={
            "orig_chars": len(block.content),
            "out_chars": len(text),
            "defs": len(defs),
            "elided": len(elided_spans),
        },
    )
