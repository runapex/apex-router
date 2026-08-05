"""Entity-floor validators — Δ13 pure-function fidelity checks for lossy transforms.

A validator is `(original: str, rendering_text: str) -> (ok: bool, reason: str)`: it structurally
verifies a lossy transform preserved the entity floor, WITHOUT re-running the transform. The compiler
seals a `validator_id` into a lossy rule and the runtime runs it as decide()'s floor step — a
rendering that fails the floor ships RAW (fail-open), never the lossy bytes. Building the validator
INDEPENDENTLY of the transform is the point (roadmap §Δ13): it must catch a broken transform variant,
so it re-derives the properties from (original, rendering), not from the transform's own bookkeeping.

Registry: `VALIDATORS[id] -> fn`. `_LOSSY_CAPABILITIES` in the compiler names the id a cell requires.
"""
from __future__ import annotations

import re
from collections.abc import Callable

_GUTTER = re.compile(r"^(\s*)(\d+)([\t:|])(.*)$")
_STRIP_MARKER = re.compile(r"\[… line-number gutter stripped: (\d+) lines · (ccr://[0-9a-f]+)\]")


def gutter_floor_v1(original: str, rendering_text: str) -> tuple[bool, str]:
    """Verify a file_read gutter-strip rendering against its original. Three properties:
      1. LOCATOR present — a `ccr://` ref is on the wire (the original is retrievable);
      2. COUNT survives — the marker's announced stripped-line count equals the gutters actually
         removed (recomputed from the original, not trusted from the transform);
      3. CONTENT verbatim — every guttered line's content (gutter removed) appears in the rendering,
         and no non-guttered line was dropped.
    Returns (ok, reason). Never raises — a malformed rendering is a floor failure, not a crash."""
    try:
        m = _STRIP_MARKER.search(rendering_text)
        if m is None:
            return False, "no strip marker (locator + count) on the wire"
        announced = int(m.group(1))
        ref = m.group(2)
        if not ref.startswith("ccr://"):
            return False, "locator missing: no ccr:// ref in the marker"

        # recompute the true gutter count + expected content from the ORIGINAL (independent check)
        true_count = 0
        expected_contents: list[str] = []
        passthrough: list[str] = []
        for ln in original.split("\n"):
            g = _GUTTER.match(ln)
            if g:
                true_count += 1
                expected_contents.append(g.group(4))
            else:
                passthrough.append(ln)

        if announced != true_count:
            return False, f"count mismatch: marker says {announced}, original has {true_count} gutters"

        body = rendering_text[m.end():] if m.end() <= len(rendering_text) else rendering_text
        # 3a. every stripped line's content survives verbatim (as a line in the body)
        body_lines = set(body.split("\n"))
        for c in expected_contents:
            if c not in body_lines:
                return False, f"content not retained verbatim: missing stripped line {c!r}"
        # 3b. no non-guttered line was dropped
        for p in passthrough:
            if p and p not in body_lines:
                return False, f"content dropped: non-guttered line {p!r} absent"
        return True, "ok"
    except Exception as e:  # noqa: BLE001 - a validator must never crash the caller; floor-fail instead
        return False, f"validator error: {e}"


# id → validator fn. `_LOSSY_CAPABILITIES[transform]['validator_id']` must be a key here.
VALIDATORS: dict[str, Callable[[str, str], tuple[bool, str]]] = {
    "gutter_floor_v1": gutter_floor_v1,
}
