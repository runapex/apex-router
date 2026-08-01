"""§7 consumer read-through shim — how both consumers resolve a model.

Composes the pieces built earlier: the §11 classifier (slice 2) -> a cell id -> the
route table (slice 5) -> a static fallback. Its load-bearing guarantee (finding #17):

    resolve() ALWAYS returns a valid, non-empty-string model, and NEVER routes to
    anything worse than the consumer's hand-authored static default. Every surprise —
    a raising/​malformed classifier, a raising/​malformed reader, a missing static
    mapping, an out-of-range confidence — resolves to a safe default, not a crash and
    not an unvalidated route.

To make that guarantee hold at the boundaries (Codex hardening pass), the shim:
  - validates `safe_default` and `min_confidence` at ENTRY (a misconfigured consumer
    fails loudly rather than silently routing everything);
  - wraps the classifier and the route_reader in try/except -> safe default;
  - validates every produced value is a non-empty string model, else falls back;
  - uses an UNAMBIGUOUS CANNOT-DECIDE signal: the reader returns None (a valid model is
    always a non-empty string), so a model literally named after a task-type is a real
    route, not a false decline (Codex #4);
  - snapshots `static_default_map` so a stateful reader can't mutate the fallback
    mid-resolve (Codex #7).

route_reader contract: `route_reader(cell_id) -> str | None` — the chosen model, or None
for CANNOT-DECIDE. (Bind amr.route_table.read_route by mapping its parent-sentinel to
None at the call site.)

Wiring into the live model-routing skill / apex proxy is a separate, explicitly-approved
step; this module is a pure, hermetic library.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import classify as _classify


@dataclass(frozen=True)
class Decision:
    model: str
    task_type: str
    confidence: float
    source: str  # "static_default_low_confidence" | "route_table" | "static_default"
                 # | "static_default_error" | "static_default_invalid_class"


def _clean_model(v):
    """Return a plain-`str` model if `v` is a non-empty/​non-whitespace string, else None.

    Coerces to a fresh `str(...)` so a stateful `str` subclass (whose `.strip()` returns
    different values on repeated calls) cannot slip an empty/​mutating value past
    validation and into a Decision (Codex pass2 #2/#5). Anything non-string -> None.
    """
    if not isinstance(v, str):
        return None
    s = str(v)                       # detach from any overridden __str__/strip behavior
    return s if s.strip() else None


def _known_ok(model, known_models):
    """A model passes the known-model gate iff known_models is unset (opt-in) OR the model
    is in it. This is the cross-machine safety: a route table naming a model the TARGET
    machine can't run (e.g. a Foundry-only id on a Claude+Codex-only box) is rejected here
    and the caller falls back to a model this machine actually has (Codex pass2 #1)."""
    return known_models is None or model in known_models


def resolve(text, *, tools=None, sys_markers=None, classifier, static_default_map,
            route_reader, min_confidence: float = 0.7, safe_default: str = "opus",
            known_models=None) -> Decision:
    """Resolve a model for `text`, returning a Decision (model + provenance).

    GUARANTEE: always returns a Decision whose `model` is a non-empty string. Entry
    misconfiguration (`safe_default`/`min_confidence`) raises; everything else — a
    raising/malformed classifier or reader, out-of-range confidence, an unknown routed
    model, or ANY unexpected internal error — resolves to a valid default, never a crash
    and never an unrunnable route. `known_models` (optional) is the set of models this
    machine can actually run; a routed/static model outside it is rejected.
    """
    # --- entry validation (these are the CALLER's contract; misconfig fails loudly) ---
    if not (isinstance(safe_default, str) and safe_default.strip()):
        raise ValueError(f"safe_default must be a non-empty string model, got {safe_default!r}")
    if known_models is not None and safe_default not in known_models:
        raise ValueError(f"safe_default {safe_default!r} is not in known_models")
    if not isinstance(min_confidence, (int, float)) or isinstance(min_confidence, bool) \
            or not math.isfinite(min_confidence) or not (0.0 <= min_confidence <= 1.0):
        raise ValueError(f"min_confidence must be a number in [0,1], got {min_confidence!r}")

    safe = str(safe_default)

    # --- everything below is wrapped: ANY unexpected escape -> safe_default (Codex #3) ---
    try:
        # Snapshot the static map so a stateful reader can't mutate the fallback (Codex #7).
        try:
            static_map = dict(static_default_map or {})
        except Exception:
            static_map = {}

        def static_for(tt) -> str:
            try:
                m = _clean_model(static_map.get(tt))
            except Exception:
                m = None
            if m is not None and _known_ok(m, known_models):
                return m
            return safe                  # static missing/​unknown -> safe_default (Codex pass2 #1/#5)

        # (0) classify, tolerating a raising/malformed classifier.
        try:
            c = classifier(text, tools=tools, sys_markers=sys_markers)
            task_type = c.task_type
            confidence = c.confidence
        except Exception:
            return Decision(safe, "", 0.0, "static_default_error")

        # A task-type must be a usable, hashable, non-empty string; else safe default.
        tt = _clean_model(task_type)
        if tt is None:
            return Decision(safe, "", 0.0, "static_default_invalid_class")

        # Confidence must be a finite number in [0,1] AND >= the floor; else ABSTAIN.
        try:
            conf_ok = (isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                       and math.isfinite(confidence) and 0.0 <= confidence <= 1.0)
        except (TypeError, ValueError, OverflowError):
            conf_ok = False
        if not conf_ok or confidence < min_confidence:
            conf_out = confidence if conf_ok else 0.0
            return Decision(static_for(tt), tt, conf_out, "static_default_low_confidence")

        # (2)/(3) consult the table, tolerating a raising/malformed reader.
        try:
            cell_id = _classify.parent_cell(tt) if tt in _classify.TASK_TYPES else f"task:{tt}"
        except Exception:
            cell_id = f"task:{tt}"
        try:
            routed = route_reader(cell_id)
        except Exception:
            return Decision(static_for(tt), tt, confidence, "static_default")

        # None == CANNOT-DECIDE. A valid route is a clean, KNOWN model; else fall back.
        model = _clean_model(routed)
        if model is None or not _known_ok(model, known_models):
            return Decision(static_for(tt), tt, confidence, "static_default")
        return Decision(model, tt, confidence, "route_table")
    except Exception:
        # Belt-and-suspenders: nothing above should escape, but if it does, stay safe.
        return Decision(safe, "", 0.0, "static_default_error")


def resolve_model(text, *, tools=None, sys_markers=None, classifier, static_default_map,
                  route_reader, min_confidence: float = 0.7, safe_default: str = "opus",
                  known_models=None) -> str:
    """Convenience wrapper returning just the model name (see `resolve`)."""
    return resolve(text, tools=tools, sys_markers=sys_markers, classifier=classifier,
                   static_default_map=static_default_map, route_reader=route_reader,
                   min_confidence=min_confidence, safe_default=safe_default,
                   known_models=known_models).model
