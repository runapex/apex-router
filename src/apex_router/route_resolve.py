"""Live resolve path — the FIRST wired consumer of the adaptive routing core.

`consumer.resolve` existed as a hermetic library with zero live call sites; this module
binds it to the machine's real inputs so `apex-router resolve` / `route-explain` (and the
pi `>>auto` family) can use it:

  text (+ optional tools/markers) → §11 classifier → cell → route table → Decision

Static defaults come from the shared model registry (model_registry) — one file edits
every consumer. The route table is the `skill` venue table; any miss/ambiguity falls back
to the static map, exactly the strict-superset guarantee (resolve NEVER names a model
worse than the static default).

Embedding refinement is fail-open: if ollama/nomic-embed is unreachable the request
prior stands. Exemplars are a small built-in set (below) — the classifier only lets an
embedding refine the prior past an absolute floor + margin, so a weak match changes
nothing.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import classify as _classify
from . import consumer, model_registry, route_table

# One-line exemplars per task-type for the embedding refinement signal (§11). Deliberately
# plain task-shaped utterances; the absolute-cosine floor keeps a weak match from refining.
EXEMPLARS: dict[str, list[str]] = {
    "debug": [
        "why is this test failing with a TypeError",
        "this script crashes on startup, here is the stack trace",
        "the cron job silently stopped working last week",
    ],
    "review": [
        "review this diff for bugs before I merge it",
        "audit this change for security issues",
        "find problems in this pull request",
    ],
    "refactor": [
        "rename this function across the whole codebase",
        "restructure this module into smaller pieces",
        "split this god object into separate classes",
    ],
    "generate": [
        "write a function that clamps a value between lo and hi",
        "implement a retry wrapper with exponential backoff",
        "create a script that converts csv to json",
    ],
    "explore": [
        "where is the config file loaded",
        "how does the authentication flow work in this repo",
        "find all callers of this function",
    ],
}

# Static per-task-type defaults, tier-resolved from the shared registry. The safe default
# (CANNOT-DECIDE floor) is the opus tier — the heavy, minimal-regret choice (§11).
_STATIC_TIER_MAP = {
    "debug": "opus",
    "review": "opus",
    "refactor": "sonnet",
    "generate": "sonnet",
    "explore": "sonnet",
}


def default_table_path(venue: str = "skill") -> Path:
    env = os.environ.get("APEX_ROUTE_TABLE")
    if env:
        return Path(env)
    return Path.home() / ".apex-router" / "tables" / f"route_table.{venue}.json"


def static_default_map(registry: dict | None = None) -> dict[str, str]:
    reg = model_registry.load() if registry is None else registry
    out = {}
    for tt, tier in _STATIC_TIER_MAP.items():
        m = model_registry.tier_model(tier, registry=reg)
        if m:
            out[tt] = m
    return out


def safe_default(registry: dict | None = None) -> str:
    reg = model_registry.load() if registry is None else registry
    return model_registry.tier_model("opus", registry=reg) or "claude-opus-4-8"


def _embed_fn():
    """ollama nomic-embed, or None when unreachable (fail-open: prior stands)."""
    from . import embed
    try:
        embed.embed("warmup")
    except Exception:
        return None
    return embed.embed


def _reader(table_path: Path):
    sentinel = "\x00cannot-decide\x00"  # a "model" that can never be a real id

    def read(cell_id: str):
        chosen = route_table.read_route(table_path, cell_id=cell_id, parent_task_type=sentinel)
        return None if chosen == sentinel else chosen

    return read


def resolve_text(text: str, *, tools=None, sys_markers=None, table_path: Path | None = None,
                 registry: dict | None = None, embed_fn="auto", venue: str = "skill") -> dict:
    """Resolve a model for free-text `text`. Returns a JSON-able dict with the Decision
    plus the explain payload (cell id, classification, why the table did/didn't win).

    `venue` selects the static-default map + route table: 'skill' (Claude tiers, the
    default) or a registry venue policy like 'codex' (Kimi venue: default k3 — its 1M
    window is load-bearing at the venue's p50 346k context — with a documented downshift
    to k2.7-code under the venue's ctx ceiling, ~2.9x cheaper)."""
    reg = model_registry.load() if registry is None else registry
    if embed_fn == "auto":
        embed_fn = _embed_fn()
    path = table_path if table_path is not None else default_table_path(venue)
    vpol = model_registry.venue(venue, registry=reg) if venue != "skill" else None

    def classifier(t, *, tools=None, sys_markers=None):
        return _classify.classify(t, tools=tools, sys_markers=sys_markers,
                                  embed_fn=embed_fn, exemplars=EXEMPLARS)

    if vpol:
        # Venue venue policy: one static default for every task class (the venue's model),
        # with the downshift alternative carried in the explain payload.
        vmodel = vpol.get("default_model") or safe_default(reg)
        static_map = {tt: vmodel for tt in _STATIC_TIER_MAP}
        vsafe = vmodel
    else:
        static_map = static_default_map(reg)
        vsafe = safe_default(reg)
    decision = consumer.resolve(
        text, tools=tools, sys_markers=sys_markers,
        classifier=classifier,
        static_default_map=static_map,
        route_reader=_reader(path),
        safe_default=vsafe,
    )
    cell = f"task:{decision.task_type}" if decision.task_type else None
    promoted = None
    if cell and decision.task_type in _classify.TASK_TYPES:
        try:
            import json
            table = json.loads(Path(path).read_text())
            for c in table.get("cells", []):
                if c.get("cell_id") == cell:
                    promoted = {"promoted": c.get("promoted"), "chosen": c.get("chosen_model")}
                    break
        except (OSError, ValueError):
            promoted = None
    out = {
        "model": decision.model,
        "task_type": decision.task_type,
        "confidence": decision.confidence,
        "source": decision.source,
        "cell": cell,
        "table": str(path),
        "table_cell": promoted,
        "embedding": "on" if embed_fn else "off",
        "venue": venue,
    }
    if vpol:
        out["venue_policy"] = {
            "default_model": vpol.get("default_model"),
            "downshift_model": vpol.get("downshift_model"),
            "downshift_ctx_ceiling": vpol.get("downshift_ctx_ceiling"),
            "note": "context below the ceiling -> downshift_model is ~2.9x cheaper "
                    "(measured); above it, default_model's 1M window is load-bearing",
        }
    return out
