"""Capability routing for local inference: the active Ornith tier behind one HTTP endpoint.

This module (1) hands out the Route for whichever tier is active and (2) keeps the proven
workload-envelope guardrails. Local serving is ollama via local_tier; older model/server
identities (Qwen chat, the MLX server and its launchd unit, the SSM co-residency guard)
were retired with the ollama migration — see git history, not live code.

Backward compatibility: `ornith_clone_projection.py` still imports ORNITH,
warn_if_unbounded, and rationale — those are preserved.
"""
from __future__ import annotations
from dataclasses import dataclass
import os, sys

from . import local_tier

# ── Model identity (kept for the un-repointed clone-projection driver) ───────
ORNITH = local_tier.resolve().api_model

# ── Endpoint ─────────────────────────────────────────────────────────────────
BASE_URL = os.environ.get("ORNITH_URL", local_tier.DEFAULT_URL)

# ── The proven envelope (still true under single-flight + concurrency 1) ─────
# ⚠ ORNITH_SECS_PER_ITEM was measured against the RETIRED MLX server running Ornith 1.0-35B. The
# large tier is now a 35B-A3B MoE (3B active/token) on ollama, so this is very likely pessimistic —
# it is left unchanged deliberately, because the honest fix is a re-measurement, not a guess. It
# only drives a warning and a split-the-run estimate, so an over-estimate is the safe direction.
MAX_ITEM_BYTES = 100_000
ORNITH_MAX_ITEMS = 30
ORNITH_SECS_PER_ITEM = 150

# ── Capability model ──────────────────────────────────────────────────────────
# The LARGE tier (35B-A3B) is a FIDELITY specialist: verbatim extraction + cross-log synthesis
# (see the local-model-verdict memory). Task-kinds that MATCH its strengths:
_ORNITH_TASK_KINDS = frozenset({
    "synthesis", "synthesize", "extract", "extraction", "narrate", "review",
    "summarize", "summary", "authoritative", "cross_log", "cross-log",
    # code GENERATION for WELL-SPECIFIED, self-contained functions only (measured 4/4 on such
    # tasks, thinking-OFF). NOT refactors/architecture/cross-file — those need a reasoner (Opus).
    # The caller MUST verify the output; offload saves tokens only when the code is correct.
    "code", "codegen", "code_gen",
})
# Bulk/interactive reasoning. Under the old single-model setup this was a hard DECLINE, because the
# only endpoint up was the big one and the bulk tier had been retired. With tiers this is no
# longer a miss — it is the SMALL tier's lane. The verdict changes from "decline" to "route small".
_BULK_TASK_KINDS = frozenset({"bulk_triage", "bulk", "triage", "chat", "interactive"})


@dataclass(frozen=True)
class Route:
    backend: str = "ornith-http"
    base_url: str = BASE_URL
    # ollama REQUIRES an explicit model id. The retired MLX server did not (it had one start-time
    # model), which is why this used to be None — omitting it now yields a 400 from the backend.
    model: str = ORNITH
    tier: str = local_tier.DEFAULT_TIER
    # Capability verdict. `fits` defaults True so legacy unconditional callers
    # (they pass no envelope) keep working unchanged; a scored call sets it honestly.
    fits: bool = True
    reason: str = "unscored"
    reuse_cache: bool = False  # advertise prompt-cache reuse for multi-item shared-prefix runs
    # True when the verdict wants a DIFFERENT tier than the one currently resident. The caller must
    # switch (see `apex-router ornith-tier`) before this Route will actually serve that model.
    needs_switch: bool = False


def _score_fit(task: str | None, items: int | None, item_bytes: int | None
               ) -> tuple[bool, str, str | None]:
    """Decide whether this task+envelope FITS a local tier. Returns (fits, reason, wanted_tier).

    `wanted_tier` is None when the task-kind does not imply one, so the caller keeps whatever is
    resident rather than churning the switch for an unscored call.

    Envelope guards win first (they are hard capacity bounds), then task-kind. When
    nothing is specified, default to fits=True — a bare call is the legacy contract.
    """
    # Hard envelope bounds — a capacity miss is a decline regardless of task-kind.
    if items is not None and items > ORNITH_MAX_ITEMS:
        est_min = items * ORNITH_SECS_PER_ITEM / 60
        return False, (f"{items} items > {ORNITH_MAX_ITEMS}-item bound "
                       f"(~{est_min:.0f} min) — split the run"), None
    if item_bytes is not None and item_bytes > MAX_ITEM_BYTES:
        return False, (f"item {item_bytes/1000:.0f} KB > {MAX_ITEM_BYTES//1000} KB "
                       f"slice bound — split the item first"), None
    # Task-kind match.
    if task is not None:
        t = task.lower()
        if t in _BULK_TASK_KINDS:
            return True, f"bulk/interactive task {task!r} → small tier", "small"
        if t in _ORNITH_TASK_KINDS:
            return True, f"capability match: {task} → large tier", "large"
    # Unspecified task-kind within envelope: legacy default — usable, unscored.
    return True, "within envelope (task-kind unspecified)", None


def select(task: str | None = None, items: int | None = None,
           override: str | None = None, item_bytes: int | None = None,
           tier: str | None = None) -> Route:
    """Route a task to the local Ornith endpoint, WITH a capability verdict and a tier.

    Backward compatible: a bare `select()` or `select(task=...)` still returns a usable
    ornith-http Route (`fits=True`), so existing unconditional callers are unchanged.
    A scored call (passing `items`/`item_bytes`/a known task-kind) additionally reports
    `route.fits` + `route.reason` so the caller can decline a mis-route, and
    `route.reuse_cache` when a multi-item shared-prefix run should reuse the prompt cache.

    Tier precedence: explicit `tier=` argument > task-kind implication > whatever is resident.
    The route NEVER switches tiers by itself — it reports `needs_switch` and names the model it
    wants. Switching is a ~21 GB load that must not happen as a side effect of asking for a route.

    `override` must be None, 'ornith-http', or the name of a configured local family (see
    `local_tier.load_families()`); any other value is a hard error. An explicit override FORCES
    the route even out of envelope, but the verdict is still reported honestly (fits may be False).
    """
    _known = set(local_tier.load_families()) | {"ornith-http"}
    if override is not None and override not in _known:
        raise ValueError(
            f"Unsupported model override {override!r}; known local families: "
            f"{', '.join(sorted(_known))}")
    warn_if_unbounded(items=items, item_bytes=item_bytes)
    fits, reason, wanted = _score_fit(task, items, item_bytes)
    resident = local_tier.resolve()
    chosen_name = (tier or wanted or resident.name).strip().lower()
    # Resolve the chosen tier through the ACTIVE family (the one `resident` belongs to), not the
    # committed ornith alias — otherwise a request against an overlay family silently serves ornith.
    _families = local_tier.load_families()
    _active_tiers = _families.get(local_tier.family_of(resident), local_tier.TIERS)
    chosen = _active_tiers.get(chosen_name, resident)
    reuse = items is not None and items > 1  # >1 item ⇒ shared preamble worth reusing
    return Route(model=chosen.api_model, tier=chosen.name, fits=fits, reason=reason,
                 reuse_cache=reuse, needs_switch=chosen.name != resident.name)


def warn_if_unbounded(model_id=None, items=None, item_bytes=None) -> None:
    """Non-fatal stderr warning when a run leaves the proven envelope.
    Signature is backward-compatible with the old positional (model_id, ...)."""
    if items is not None and items > ORNITH_MAX_ITEMS:
        est_min = items * ORNITH_SECS_PER_ITEM / 60
        print(f"[router] WARNING: {items} items exceeds the {ORNITH_MAX_ITEMS}-item "
              f"bound (~{est_min:.0f} min, dense 35B). Split the run.", file=sys.stderr)
    if item_bytes is not None and item_bytes > MAX_ITEM_BYTES:
        print(f"[router] WARNING: item is {item_bytes/1000:.0f} KB > the "
              f"{MAX_ITEM_BYTES//1000} KB slice bound — slice it first.", file=sys.stderr)


def rationale(model_id=None, task=None, items=None) -> str:
    """One-line stderr explanation. Kept for clone-projection compatibility."""
    n = f", ~{items} item(s)" if items is not None else ""
    return f"[router] {task} → Ornith (single always-on HTTP endpoint{n})"
