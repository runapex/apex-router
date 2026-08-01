"""§11 Tier-2 task-type classifier — the router input for adaptive model routing.

Task-types (the canonical five): debug · explore · review · refactor · generate.

Two fused, cheap signals (design §11):
  1. request-signal (free, ~0ms): the inbound tool-set + system-prompt markers. This
     is the PRIOR — confirmed present in captured traffic and never a network call.
  2. embedding-signal: nomic-embed-text vs a few labeled exemplar prompts per class.
     It only REFINES the prior, and only when it leads by more than `margin`.

Design rule, load-bearing: **conservative on ambiguity → the safe/heavy default**
(`debug`: minimal-lossy, keep traces/IDs verbatim, heavy tier). A misclassification
must never silently under-power a hard task, so when nothing discriminates we return
the heavy default at low confidence rather than guessing.
"""
from __future__ import annotations

from dataclasses import dataclass

# The canonical five task-types (§11). Order is the tie-break preference when
# signals are equal: the heavier/safer task wins ties.
TASK_TYPES = ("debug", "review", "refactor", "generate", "explore")

# The safe/heavy default when nothing discriminates (§11 conservative-on-ambiguity).
SAFE_DEFAULT = "debug"

# Tools that, when present, strongly indicate a task-type.
_REVIEW_TOOLS = {"reportfindings"}
_MUTATION_TOOLS = {"edit", "write", "notebookedit", "applypatch", "multiedit"}

# System-prompt markers → task-type (explicit intent beats tool inference).
_MARKER_TO_TYPE = {
    "debug": "debug",
    "review": "review",
    "refactor": "refactor",
    "generate": "generate",
    "explore": "explore",
    "plan": "explore",
}


@dataclass(frozen=True)
class Classification:
    task_type: str
    confidence: float
    source: str  # "request" | "embedding" | "fusion" | "default"


def _norm(names) -> set[str]:
    return {str(n).strip().lower() for n in (names or []) if str(n).strip()}


def classify_request(tools=None, sys_markers=None) -> Classification:
    """Classify from the free request-signal alone (tool-set + system markers).

    Precedence (most explicit first):
      1. an explicit task marker in `sys_markers`  → that type, high confidence
      2. ReportFindings tool present               → review
      3. a mutation tool (Edit/Write/…) present    → refactor (mutation, read+write)
         (refactor vs generate is not separable from tools alone; default to the
          heavier 'refactor' and let the embedding signal split them)
      4. read-only tool-set (no mutation, no findings) → explore
      5. nothing discriminating                    → SAFE_DEFAULT, low confidence
    """
    markers = _norm(sys_markers)
    tset = _norm(tools)

    for marker, ttype in _MARKER_TO_TYPE.items():
        if marker in markers:
            return Classification(ttype, 0.9, "request")

    if tset & _REVIEW_TOOLS:
        return Classification("review", 0.85, "request")

    if tset & _MUTATION_TOOLS:
        return Classification("refactor", 0.6, "request")

    if tset:  # read-only tool-set present, no mutation/findings
        return Classification("explore", 0.7, "request")

    return Classification(SAFE_DEFAULT, 0.4, "default")


def _embedding_scores(text, embed_fn, exemplars):
    """Return {task_type: best cosine over that class's exemplars}, or None if
    the embedding signal is unavailable/unusable."""
    if not text or embed_fn is None or not exemplars:
        return None
    from . import embed as _embed
    try:
        qv = embed_fn(text)
    except Exception:
        return None
    scores = {}
    for ttype, prompts in exemplars.items():
        best = None
        for p in prompts:
            try:
                sim = _embed.cosine(qv, embed_fn(p))
            except Exception:
                continue
            if best is None or sim > best:
                best = sim
        if best is not None:
            scores[ttype] = best
    return scores or None


# An embedding must match its top class at least this well (absolute cosine) before
# it is allowed to refine the prior. Below this floor the "match" is noise — a
# near-orthogonal exemplar must never override the request signal (Codex xval).
_ABS_COSINE_FLOOR = 0.30


def classify(text, tools=None, sys_markers=None, *, embed_fn=None,
             exemplars=None, margin: float = 0.05) -> Classification:
    """Fuse the request prior with an embedding refinement (§11).

    The request-signal is the prior. The embedding refines it ONLY when ALL hold:
      - its top class matches at an absolute cosine >= `_ABS_COSINE_FLOOR`
        (an embedding that matches nothing well is noise, not a signal), AND
      - it leads a genuine runner-up by more than `margin` (a lone surviving class
        is NOT a confident refinement — there is nothing it beat), AND
      - the request signal is not already a confident, discriminating classification.
    Conservative on ambiguity: otherwise the prior (or safe/heavy default) stands.
    """
    prior = classify_request(tools=tools, sys_markers=sys_markers)

    scores = _embedding_scores(text, embed_fn, exemplars)
    if not scores:
        return prior  # no usable embedding signal → prior stands

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_type, top_sim = ranked[0]

    # Absolute floor: the top class must genuinely resemble the query.
    if top_sim < _ABS_COSINE_FLOOR:
        return prior

    # Margin over a genuine competitor. If only one class scored, there is no
    # runner-up to beat: a lone survivor above the floor is a legitimate refinement,
    # but it never manufactures a huge synthetic lead (which would spuriously inflate
    # confidence). Use the floor itself as the reference for a lone survivor.
    if len(ranked) > 1:
        runner_sim = ranked[1][1]
        lead = top_sim - runner_sim
        if lead <= margin:
            return prior  # not enough separation from the competitor
    else:
        lead = top_sim - _ABS_COSINE_FLOOR  # bounded, non-negative reference

    prior_is_confident = prior.source == "request" and prior.confidence >= 0.8
    if prior_is_confident:
        return prior

    conf = min(0.95, 0.5 + lead)
    source = "fusion" if prior.source == "request" else "embedding"
    return Classification(top_type, conf, source)


def parent_cell(task_type: str) -> str:
    """The coarse parent cell id for a task-type — the fallback route a fine cluster
    falls back to before it is promoted (§6). Raises ValueError on an unknown type."""
    if task_type not in TASK_TYPES:
        raise ValueError(f"unknown task_type: {task_type!r}")
    return f"task:{task_type}"
