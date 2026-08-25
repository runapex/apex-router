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


_CODE_CLASSES = frozenset({"debug", "refactor", "generate", "review"})


def _kimi_route(task_type: str, ctx_tokens: int | None, vpol: dict) -> tuple[str, str]:
    """Within-family Kimi routing (DECISION-kimi-codex-routing K1/K2/K3). Returns
    (model, reason). The decision that actually kicks:
      - ctx at/past the deep floor (or unknown-but-venue-codex long session) -> k3
        (its 1M window is the only fit; load-bearing, measured 73.8% of codex reqs >250k)
      - code-shaped task, ctx under the floor -> k2.7-code (code-specialized, ~3x cheaper)
      - anything else, ctx under the floor -> k2.6 (cheapest general)
    """
    deep_floor = vpol.get("deep_ctx_floor") or vpol.get("downshift_ctx_ceiling") or 250_000
    k3 = vpol.get("deep_ctx_model") or vpol.get("default_model") or "kimi-k3"
    code = vpol.get("code_model") or vpol.get("downshift_model") or "kimi-k2.7-code"
    general = vpol.get("default_model") if "default_model" in vpol and "code_model" in vpol \
        else (vpol.get("downshift_model") or "kimi-k2.6")
    if ctx_tokens is not None and ctx_tokens >= deep_floor:
        return k3, f"ctx {ctx_tokens:,} >= deep floor {deep_floor:,} → {k3} (1M window load-bearing)"
    if task_type in _CODE_CLASSES:
        return code, (f"code-shaped task ({task_type})"
                      + (f", ctx {ctx_tokens:,}" if ctx_tokens is not None else "")
                      + f" under floor → {code} (code-specialized, ~3x cheaper)")
    return general, (f"general task ({task_type or 'unclassified'}) under floor → "
                     f"{general} (cheapest general)")


def resolve_text(text: str, *, tools=None, sys_markers=None, table_path: Path | None = None,
                 registry: dict | None = None, embed_fn="auto", venue: str = "skill",
                 ctx_tokens: int | None = None) -> dict:
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
        # Within-family routing kicks FIRST (task shape + ctx decide among the family's
        # models); the route table can still override a cell once bench evidence promotes.
        vmodel, vreason = _kimi_route(
            # preliminary class from the free request prior; the full classification
            # below may refine it — re-route after classify if the class changes.
            _classify.classify_request(tools=tools, sys_markers=sys_markers).task_type,
            ctx_tokens, vpol)
        static_map = {tt: vmodel for tt in _STATIC_TIER_MAP}
        vsafe = vmodel
    else:
        vmodel, vreason = None, None
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
        # Re-route within the family on the FINAL classification (the embedding refine-
        # ment may have moved the class, e.g. explore -> generate, which flips k2.6 ->
        # k2.7-code). The route table / static fallback already used the prelim class;
        # the final route is the honest one. A LOW-CONFIDENCE class is not evidence for
        # the code tier: treat it as unclassified (general), same conservative rule the
        # consumer applies before consulting the table.
        confident_tt = decision.task_type if decision.confidence >= 0.7 else None
        final_model, final_reason = _kimi_route(confident_tt, ctx_tokens, vpol)
        if decision.source != "route_table":
            decision = type(decision)(final_model, decision.task_type,
                                      decision.confidence, decision.source)
        out["model"] = decision.model
        out["venue_policy"] = {
            "routed_model": final_model,
            "route_reason": final_reason,
            "ctx_tokens": ctx_tokens,
            "deep_ctx_floor": vpol.get("deep_ctx_floor") or vpol.get("downshift_ctx_ceiling"),
        }
    try:
        from .route_conformance import log_resolve_conformance
        _tt = out.get("task_type")
        _tier = _STATIC_TIER_MAP.get(_tt)
        # Only STATIC skill resolutions are tier-conformance-checkable: a venue (kimi/codex) or a
        # promoted route-table cell INTENTIONALLY returns a model off the static tier map, so
        # comparing it to _STATIC_TIER_MAP would be false drift. `venue_policy` is present in `out`
        # iff a venue policy applied (out["venue"] itself is always set — it defaults to "skill");
        # source == "route_table" marks a promoted cell.
        _is_static = "venue_policy" not in out and out.get("source") != "route_table"
        if _tier and _is_static:
            log_resolve_conformance(_tt, _tier, out.get("model"),
                                    context_size=ctx_tokens)
    except Exception:
        pass
    return out
