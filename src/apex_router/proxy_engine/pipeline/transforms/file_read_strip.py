"""Gutter-strip — lossy file_read compression (F2 split, roadmap T1-P).

A file_read block (cat -n / Read tool / grep -n) carries a leading line-number gutter `<ws><num><sep>`
on most lines. Measured on 3,014 real blocks: normalizing the gutter (canonicalize) saves ~0% — it's
already the minimal `N\t` — but DROPPING it (strip) saves a stable ~9-10% in tokens. Strip is LOSSY:
line numbers are FUNCTIONAL entities (view_range, grep -n, the model reasoning in "line N"), so this
transform is `ccr_retrieval` — it drops the gutter from the wire and carries the ORIGINAL so an agent
can retrieve the exact guttered text.

Fidelity contract (mirrors json_crush, the other ccr_retrieval tenant):
  - CONTENT is kept byte-verbatim — only the per-line gutter is removed;
  - ONE counted+located marker is emitted (`[… line-number gutter stripped: N lines · ccr://<hash>]`)
    so cardinality + a locator survive on the wire (the entity floor) and the model knows numbers were
    removed and can retrieve;
  - `original` carries the guttered bytes for the CCR store / stub resolver.

Because it's ccr_retrieval, decide()'s Δ1 capability gate keeps it INERT until a resolver registers,
and the compiler refuses to sign it without behavioral evidence (the Δ14 gate). Pure fn of block
bytes; `run` raising on a non-guttered block is the fail-open signal (§6).
"""
from __future__ import annotations

import hashlib
import re

from apex_router.proxy_engine.pipeline.transforms.base import Block, Rendering

name = "file_read_strip"
fidelity = "ccr_retrieval"
knobs: list[str] = []

# The line-number gutter: optional leading whitespace, digits, one separator (tab / colon / pipe),
# then the content. Same shape the classifier keys file_read on (`_GUTTER_RE` in apex_router.proxy_engine.policy).
_GUTTER = re.compile(r"^(\s*)(\d+)([\t:|])(.*)$")
MIN_GUTTER_FRACTION = 0.5  # a majority of non-empty lines must be guttered (matches classify())
MIN_LINES = 4  # below this, the marker's own tokens outweigh the strip saving


def _gutter_lines(content: str) -> tuple[list[str], int]:
    """Return (stripped_lines, n_stripped): each guttered line's content without its gutter, plus
    how many gutters were removed. Non-guttered lines pass through unchanged."""
    out: list[str] = []
    n = 0
    for ln in content.split("\n"):
        m = _GUTTER.match(ln)
        if m:
            out.append(m.group(4))  # the content after the gutter, verbatim
            n += 1
        else:
            out.append(ln)
    return out, n


def ccr_ref(original: str) -> str:
    """Stable CCR ref for the guttered original — hash of the exact bytes to retrieve. The resolver
    keys on this; the emitted marker carries it so a retrieval fetches precisely this block back."""
    h = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    return f"ccr://{h}"


def resolve_original(original: str) -> str:
    """The stub-resolver inverse: given the carried original, return it verbatim (the guttered text
    with line numbers intact). Trivial for the stub — the store (Δ12) will key by ref, but the
    behavioral gate serves straight from the carried `original`, same as json_crush."""
    return original


def _is_guttered(content: str) -> bool:
    lines = [ln for ln in content.split("\n") if ln.strip()]
    if len(lines) < MIN_LINES:
        return False
    guttered = sum(1 for ln in lines if _GUTTER.match(ln))
    return guttered >= MIN_GUTTER_FRACTION * len(lines)


def applies(block: Block) -> bool:
    """True iff the block is a line-number-guttered file read with enough lines to profit."""
    return _is_guttered(block.content)


def run(block: Block, knobs) -> Rendering:
    """Pure (block, knobs) → Rendering. Strips the gutter, keeps content verbatim, emits one counted
    marker + carries the original. Raises on a non-guttered block (fail-open: pipeline ships raw)."""
    content = block.content
    if not _is_guttered(content):
        raise ValueError("file_read_strip: block is not a line-number-guttered file read")
    stripped, n = _gutter_lines(content)
    ref = ccr_ref(content)
    marker = f"[… line-number gutter stripped: {n} lines · {ref}]"
    text = marker + "\n" + "\n".join(stripped)
    return Rendering(
        text=text,
        fidelity="ccr_retrieval",
        original=content,  # carried for the CCR store / stub resolver (retrieval path)
        meta={"stripped_lines": n, "orig_chars": len(content), "out_chars": len(text)},
    )
