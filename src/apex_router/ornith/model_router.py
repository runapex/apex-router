"""Capability routing for local inference: one always-on Ornith HTTP endpoint.

Qwen is RETIRED for chat (see docs/superpowers/specs/2026-07-13-ornith-server-design.md).
This module now (1) hands out the single Ornith Route, (2) keeps the proven
workload-envelope guardrails, and (3) guards the 52 GB ceiling against a second
resident model (SSM pipeline) while Ornith is up.

Backward compatibility: `ornith_clone_projection.py` still imports ORNITH,
warn_if_unbounded, and rationale — those are preserved.
"""
from __future__ import annotations
from dataclasses import dataclass
import os, subprocess, sys

# ── Model identity (kept for the un-repointed clone-projection driver) ───────
ORNITH = "mlx-community/Ornith-1.0-35B-4bit"
RETIRED_QWEN = "mlx-community/Qwen3.6-35B-A3B-4bit"  # history only; not routed

# ── Endpoint ─────────────────────────────────────────────────────────────────
BASE_URL = os.environ.get("ORNITH_URL", "http://127.0.0.1:8080")

# ── The proven envelope (still true under single-flight + concurrency 1) ─────
MAX_ITEM_BYTES = 100_000
ORNITH_MAX_ITEMS = 30
ORNITH_SECS_PER_ITEM = 150

SERVICE = f"gui/{os.getuid()}/com.ornith.server"


# ── Capability model ──────────────────────────────────────────────────────────
# Ornith (dense 35B) is a FIDELITY specialist: verbatim extraction + cross-log
# synthesis (see ornith-vs-qwen verdict). It is NOT the bulk/triage tier — that was
# Qwen (MoE, retired). So a bulk-reasoning task does not "fit" dense Ornith even when
# it is the only endpoint up: routing it there is a mis-route we name, not silently
# accept. Task-kinds that MATCH Ornith's strengths:
_ORNITH_TASK_KINDS = frozenset({
    "synthesis", "synthesize", "extract", "extraction", "narrate", "review",
    "summarize", "summary", "authoritative", "cross_log", "cross-log",
    # code GENERATION for WELL-SPECIFIED, self-contained functions only (measured 4/4 on such
    # tasks, thinking-OFF). NOT refactors/architecture/cross-file — those need a reasoner (Opus).
    # The caller MUST verify the output; offload saves tokens only when the code is correct.
    "code", "codegen", "code_gen",
})
# Task-kinds that are an explicit MISS (bulk/interactive reasoning — Qwen's old lane):
_BULK_TASK_KINDS = frozenset({"bulk_triage", "bulk", "triage", "chat", "interactive"})


@dataclass(frozen=True)
class Route:
    backend: str = "ornith-http"
    base_url: str = BASE_URL
    model: None = None  # clients OMIT model; the server default wins
    # Capability verdict (new). `fits` defaults True so legacy unconditional callers
    # (they pass no envelope) keep working unchanged; a scored call sets it honestly.
    fits: bool = True
    reason: str = "unscored"
    reuse_cache: bool = False  # advertise prompt-cache reuse for multi-item shared-prefix runs


def _score_fit(task: str | None, items: int | None, item_bytes: int | None) -> tuple[bool, str]:
    """Decide whether this task+envelope FITS dense Ornith. Returns (fits, reason).

    Envelope guards win first (they are hard capacity bounds), then task-kind. When
    nothing is specified, default to fits=True — a bare call is the legacy contract.
    """
    # Hard envelope bounds — a capacity miss is a decline regardless of task-kind.
    if items is not None and items > ORNITH_MAX_ITEMS:
        est_min = items * ORNITH_SECS_PER_ITEM / 60
        return False, (f"{items} items > {ORNITH_MAX_ITEMS}-item bound "
                       f"(~{est_min:.0f} min dense 35B) — split the run")
    if item_bytes is not None and item_bytes > MAX_ITEM_BYTES:
        return False, (f"item {item_bytes/1000:.0f} KB > {MAX_ITEM_BYTES//1000} KB "
                       f"slice bound — split the item first")
    # Task-kind match. Bulk/interactive reasoning is an explicit miss (Qwen's retired lane).
    if task is not None:
        t = task.lower()
        if t in _BULK_TASK_KINDS:
            return False, (f"bulk/interactive task {task!r} is not a fit for dense Ornith "
                           "(Qwen's retired lane) — defer or run elsewhere")
        if t in _ORNITH_TASK_KINDS:
            return True, f"capability match: {task}"
    # Unspecified task-kind within envelope: legacy default — usable, unscored.
    return True, "within envelope (task-kind unspecified)"


def select(task: str | None = None, items: int | None = None,
           override: str | None = None, item_bytes: int | None = None) -> Route:
    """Route a task to the single Ornith HTTP endpoint, WITH a capability verdict.

    Backward compatible: a bare `select()` or `select(task=...)` still returns a usable
    ornith-http Route (`fits=True`), so existing unconditional callers are unchanged.
    A scored call (passing `items`/`item_bytes`/a known task-kind) additionally reports
    `route.fits` + `route.reason` so the caller can decline a mis-route, and
    `route.reuse_cache` when a multi-item shared-prefix run should reuse the prompt cache.

    `override` must be None/'ornith'/'ornith-http' — Qwen is retired, so any other value
    is a hard error. An explicit override FORCES the route even out of envelope, but the
    verdict is still reported honestly (fits may be False).
    """
    if override not in (None, "ornith", "ornith-http"):
        raise ValueError(
            f"Unsupported model override {override!r}; Qwen is retired for chat")
    warn_if_unbounded(items=items, item_bytes=item_bytes)
    fits, reason = _score_fit(task, items, item_bytes)
    reuse = items is not None and items > 1  # >1 item ⇒ shared preamble worth reusing
    return Route(fits=fits, reason=reason, reuse_cache=reuse)


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


def assert_ssm_can_start() -> None:
    """Refuse to load a second ~19 GB model while Ornith is resident.

    HARD invariant: BOTH must hold (closes the crash/restart race) —
      (1) the service is NOT bootstrapped (launchctl print exits non-zero;
          use EXIT STATUS only, do not parse output), AND
      (2) ornith_client.liveness() is False.
    """
    registered = subprocess.run(["launchctl", "print", SERVICE],
                                capture_output=True).returncode == 0
    live = False
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "ornith"))
        from . import ornith_client
        live = ornith_client.liveness()
    except Exception:
        live = False
    if registered or live:
        sys.exit(
            "ERROR: Ornith server is still registered/reachable — refusing to load "
            "a second model (52 GB ceiling). Stop it first:\n"
            f"  launchctl bootout {SERVICE}")
