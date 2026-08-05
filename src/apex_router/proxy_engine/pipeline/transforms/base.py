"""Transform protocol + shared types — §7.1.

A Transform is a PURE function of (block, knobs) → rendering (§5.3): deterministic and
registry-blind. It NEVER reads the store; it just computes. The pipeline decides whether to
ship the rendering, freeze it, or fall back — the transform only renders. `run` raising is the
fail-open contract: the pipeline ships the original bytes, 200 (§6 step 7).

Fidelity classes — taxonomy v2 (Δ6), honest about what recovery each class actually needs:
  - "wire_canonicalization": bytes change under a DEFINED semantic equivalence; no inverse is
                    claimed (compaction minifies JSON, terminal normalizes ANSI/CR/MOTD — v1 called
                    these "lossless", but minification cannot reconstruct the original whitespace).
  - "self_contained":  the omitted material is reconstructable from the RENDERING ALONE — reserved;
                    no transform is truly self-contained today (astgrep is not, see below).
  - "external_retrieval": recovery needs a VERSIONED EXTERNAL object (astgrep's outline points at
                    line spans in the repo file — recoverable only while that file stays at the
                    referenced state; v1's "recoverable" overclaimed self-containment).
  - "ccr_retrieval":   information dropped from the wire; the ORIGINAL must be persisted in CCR so
                    the agent can retrieve it (json_crush — v1's "lossy_ccr").

Dedup directionality rule (recorded here for the v4 return): a newer occurrence points to the
older one; the EARLIER occurrence is never rewritten. This keeps dedup freeze-compatible — a
frozen (already-shipped) block is behind the frontier and must never change, so only the new
copy may be replaced by a reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Fidelity = Literal["wire_canonicalization", "self_contained", "external_retrieval", "ccr_retrieval"]


@dataclass(frozen=True)
class Block:
    """One compressible unit — a tool-output block within a message. `content` is the exact
    original bytes/text the client sent; `tool_name` and `meta` let a transform decide if it
    applies (e.g. terminal only fires on Bash output; astgrep skips ranged Reads)."""

    content: str
    tool_name: str | None = None
    block_hash: str | None = None  # sha256 of ORIGINAL content (set by the pipeline)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rendering:
    """A transform's output. `text` is what ships. `original` is carried for ccr_retrieval so the
    pipeline can store it in CCR. `recover` is an optional inverse token for wire_canonicalization
    transforms that need reconstruction context (compaction stores nothing extra — its inverse is
    structural). `fidelity` echoes the transform's class for telemetry + the fidelity floor."""

    text: str
    fidelity: Fidelity
    original: str | None = None  # the pre-transform bytes, for CCR (ccr_retrieval)
    recover: dict[str, Any] = field(default_factory=dict)  # inverse metadata if any
    meta: dict[str, Any] = field(default_factory=dict)  # per-run stats (elided spans, etc.)


# A knob snapshot is an epoch-frozen name→value mapping (§3.4 registry.session_snapshot()).
Snapshot = dict[str, Any]


@runtime_checkable
class Transform(Protocol):
    name: str
    fidelity: Fidelity
    knobs: list[str]  # knob names it reads from the snapshot

    def applies(self, block: Block) -> bool:
        """Cheap predicate: should this transform touch this block? Must be side-effect free."""
        ...

    def run(self, block: Block, knobs: Snapshot) -> Rendering:
        """Pure (block, knobs) → rendering. Deterministic; registry-blind; no store access.
        Raising is the fail-open signal — the pipeline ships the original, 200."""
        ...
