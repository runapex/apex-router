"""Terminal normalizer — strip ANSI, collapse CR runs, strip MOTD. §7 (M3, inline).

Bash/terminal tool output is heavy with bytes the model doesn't need: ANSI color/cursor escape
sequences, carriage-return progress-bar runs (`\r`-overwritten frames), and login MOTD banners.
Stripping them is lossless-to-final-state: the visible terminal result is preserved exactly; only
the control bytes that a terminal itself would have consumed are removed.

Algorithm ported from the P0.3 reference normalizer (fixtures/extract_terminal.py), validated
against real a Ruby service captures. The ORIGINAL is carried in the rendering so the pipeline can put
it in CCR (an agent that truly needs the raw escape bytes can retrieve it) — hence fidelity is
lossless (the model-visible content is faithful) but CCR-backed for the raw bytes.
"""

from __future__ import annotations

import re

from apex_router.proxy_engine.pipeline.transforms.base import Block, Rendering, Snapshot

name = "terminal"
fidelity = "wire_canonicalization"
knobs = ["strip_ansi", "collapse_cr", "strip_motd"]

_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")  # CSI (colors, cursor)
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")  # OSC (window title)
_MOTD_PATTERNS = [
    r"Welcome to Ubuntu",
    r"System information as of",
    r"\* Documentation:\s+https://help\.ubuntu\.com",
    r"\* Management:\s+https://landscape\.canonical\.com",
    r"\* Support:\s+https://ubuntu\.com/(pro|advantage)",
    r"Last login:",
]
_MOTD_RE = re.compile("|".join(f"(?:{p})" for p in _MOTD_PATTERNS))

_ESC = "\x1b"


def _looks_terminal(content: str) -> bool:
    return (_ESC in content) or (content.count("\r") >= 2) or bool(_MOTD_RE.search(content))


def applies(block: Block) -> bool:
    # Bash output is always worth normalizing; other blocks only if they carry terminal
    # artifacts (ANSI escapes, CR runs, or an MOTD banner).
    if block.tool_name and block.tool_name.lower() in {"bash", "shell", "terminal"}:
        return True
    return _looks_terminal(block.content)


def strip_ansi(s: str) -> str:
    return _ANSI_OSC.sub("", _ANSI_CSI.sub("", s))


_ERASE_LINE = re.compile(r"\x1b\[([012]?)K")  # erase-line CSI at a segment start


def collapse_cr(s: str) -> str:
    """Collapse `\\r` progress runs to final visible state, per line, ERASE-AWARE.

    A terminal renders `a\\rbb` as `bb` (CR → col 0, later chars overwrite). But the common
    progress pattern is `text\\r\\x1b[Knewtext`: CR to col 0, then `\\x1b[K` ERASES the line, so
    the result is `newtext` — NOT `newtextt` (cross-validation: stripping ANSI first lost the erase and
    produced garbage like 'Doneloading 99%'). So collapse_cr MUST run BEFORE strip_ansi and
    honor erase sequences at each `\\r` segment boundary:
      \\x1b[2K → erase whole line;  \\x1b[K/\\x1b[0K → erase cursor→eol (cursor at col0 ⇒ all);
      \\x1b[1K → erase start→cursor (cursor at col0 ⇒ nothing).
    """
    out = []
    for line in s.split("\n"):
        if "\r" not in line:
            out.append(line)
            continue
        row = ""
        for seg in line.split("\r"):
            m = _ERASE_LINE.match(seg)
            if m:  # erase at col 0 (right after the \r)
                code = m.group(1)
                if code in ("", "0", "2"):  # erase-to-eol or whole-line from col 0 → clears row
                    row = ""
                seg = seg[m.end() :]
            row = seg + row[len(seg) :] if len(seg) < len(row) else seg
        out.append(row)
    return "\n".join(out)


def strip_motd(s: str) -> str:
    return "\n".join(ln for ln in s.split("\n") if not _MOTD_RE.search(ln))


def run(block: Block, knobs: Snapshot) -> Rendering:
    text = block.content
    # ORDER MATTERS (cross-validation): collapse_cr FIRST (erase-aware, needs the \x1b[K sequences),
    # then strip remaining ANSI, then MOTD.
    if knobs.get("collapse_cr", True):
        text = collapse_cr(text)
    if knobs.get("strip_ansi", True):
        text = strip_ansi(text)
    if knobs.get("strip_motd", True):
        text = strip_motd(text)
    return Rendering(
        text=text,
        fidelity="wire_canonicalization",
        original=block.content,  # raw bytes preserved for CCR retrieval
        meta={"orig_chars": len(block.content), "out_chars": len(text)},
    )
