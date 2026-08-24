"""codeqa model-tier router — pick the frontier Claude model + reasoning effort per task-kind.

This is the SECOND routing axis. The first (model_router.py / freshness.classify_claim) decides
LOCAL-vs-FRONTIER: the local 35B Ornith handles verbatim VALUE lookups, and INFERENCE/RUNTIME/judge
work escalates to a frontier model. This module decides, on the frontier side, WHICH tier
(haiku < sonnet < opus) and HOW MUCH reasoning effort — the cheapest tier that can decide the task
wins. A constant/name lookup doesn't need Opus; a runtime-inference judgement does.

The default map is grounded in the same measurements as classify_claim:

    value / extract / lookup   → haiku   (no reasoning knob — see the constraint below)
    synthesis / inference      → sonnet  + medium effort
    judge / conclude           → opus    + high  effort
    verify / runtime           → opus    + xhigh effort
    (unknown task-kind)        → opus    + high  effort   (safe, capable fallback)

HARD API CONSTRAINT (Claude API): `output_config.effort` and adaptive thinking exist ONLY on
Sonnet 5 and Opus 4.8. Haiku 4.5 REJECTS `effort` with a 400 and has no adaptive-thinking knob, so
haiku is the no-reasoning FLOOR tier: request_extras() emits neither field for it, and a route that
somehow assigns effort to haiku (a bad env override) is corrected here rather than sent to 400.

Nothing is snapshotted at import. Model ids and the task→tier/effort table have shipped defaults but
are overridable from the environment (CODEQA_TIER_MODELS / CODEQA_TIER_ROUTES), resolved fresh per
call — the same config-driven ethos as proxy_setup, and the same "hardcode a sensible default,
allow override" pattern as model_router.ORNITH. An explicit CODEQA_JUDGE_MODEL still wins over the
whole router (back-compat single-model override): see explicit_model_override().

Pure stdlib — offline-testable, no network, no anthropic dependency.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# tier -> default Claude model id. Hardcoded defaults (like model_router.ORNITH), env-overridable.
_DEFAULT_TIER_MODELS = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
}
# Only these tiers accept output_config.effort + adaptive thinking on the Claude API. haiku does NOT.
_EFFORT_CAPABLE = frozenset({"sonnet", "opus"})
_VALID_EFFORT = ("low", "medium", "high", "xhigh", "max")

# task-kind -> (tier, effort). effort is dropped for non-effort-capable tiers (see resolve()).
_DEFAULT_ROUTES = {
    "judge": ("opus", "high"), "conclude": ("opus", "high"),
    "verify": ("opus", "xhigh"), "runtime": ("opus", "xhigh"),
    "synthesis": ("sonnet", "medium"), "synthesize": ("sonnet", "medium"),
    "inference": ("sonnet", "medium"),
    "extract": ("haiku", None), "extraction": ("haiku", None),
    "lookup": ("haiku", None), "value": ("haiku", None),
}
_FALLBACK = ("opus", "high")   # unknown task-kind -> safe, capable default

# max_tokens FLOOR by effort — adaptive thinking spends tokens against max_tokens, so a 256-token
# judge budget would be STARVED by high-effort thinking (all budget spent reasoning, empty answer).
# Give headroom scaled to effort. None (haiku) imposes no floor — the caller's small budget stands.
_MAX_TOKENS_FLOOR = {None: 0, "low": 512, "medium": 1024, "high": 2048, "xhigh": 4096, "max": 8192}
# HTTP timeout floor by effort — higher effort takes longer to generate.
_TIMEOUT_FLOOR = {None: 60.0, "low": 60.0, "medium": 90.0, "high": 120.0, "xhigh": 180.0, "max": 300.0}


@dataclass(frozen=True)
class Route:
    tier: str            # "haiku" | "sonnet" | "opus" (or a fixed-override model id — see resolve)
    model: str           # the concrete model id to send to /v1/messages
    effort: str | None   # None for haiku / when no reasoning applies
    reason: str          # human-readable trace of why this route was chosen
    fixed: bool = False  # True when an explicit CODEQA_JUDGE_MODEL override bypassed routing


def _env(env):
    return os.environ if env is None else env


def _tier_models(env):
    """The tier→model map: the shared model registry (`apex_router.model_registry`, itself
    DEFAULTS + the user's ~/.apex-router/models.json overlay), then CODEQA_TIER_MODELS
    ('haiku=<id>,opus=<id>') on top — env still wins, so existing overrides are unaffected."""
    from .. import model_registry
    reg = model_registry.load()
    models = {t: (model_registry.tier_model(t, registry=reg) or d)
              for t, d in _DEFAULT_TIER_MODELS.items()}
    for pair in (env.get("CODEQA_TIER_MODELS") or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in models and v:
                models[k] = v
    return models


def _routes(env):
    """The task→(tier, effort) map, defaults overlaid with CODEQA_TIER_ROUTES
    ('judge=opus/high,extract=haiku' — effort optional after a slash)."""
    routes = dict(_DEFAULT_ROUTES)
    for item in (env.get("CODEQA_TIER_ROUTES") or "").split(","):
        item = item.strip()
        if "=" not in item:
            continue
        task, spec = item.split("=", 1)
        tier, _, eff = spec.strip().partition("/")
        routes[task.strip().lower()] = (tier.strip(), (eff.strip() or None))
    return routes


def explicit_model_override(env=None):
    """CODEQA_JUDGE_MODEL, stripped of a trailing '[...]' marker; None if unset/empty. When present it
    is a HARD single-model override that bypasses tier routing entirely — back-compat with the
    pre-router judge, which sent one fixed model for every call."""
    env = _env(env)
    m = env.get("CODEQA_JUDGE_MODEL")
    if not m:
        return None
    return re.sub(r"\[.*?\]$", "", m).strip() or None


def resolve(task=None, *, env=None) -> Route:
    """Route a task-kind to a (tier, model, effort) Route. Deterministic and pure.

    An explicit CODEQA_JUDGE_MODEL override wins (fixed=True, no effort). Unknown task-kinds fall back
    to opus/high. An effort assigned to a non-effort-capable tier (haiku) is dropped rather than sent
    to a 400, and an unrecognized effort value is dropped too.
    """
    env = _env(env)
    override = explicit_model_override(env)
    if override:
        return Route(tier=override, model=override, effort=None,
                     reason="CODEQA_JUDGE_MODEL override (routing bypassed)", fixed=True)

    models = _tier_models(env)
    routes = _routes(env)
    t = (task or "").strip().lower()
    known = t in routes
    tier, effort = routes.get(t, _FALLBACK)

    if tier not in models:                       # bad override tier name -> keep the run working
        reason = f"unknown tier {tier!r} for task {task!r} — fell back to {_FALLBACK[0]}"
        tier, effort = _FALLBACK
    elif known:
        reason = f"task {task!r} → {tier}"
    else:
        reason = f"unknown task {task!r} → fallback {tier}"

    # Constraint: only sonnet/opus accept effort; haiku 400s on it.
    if effort is not None and tier not in _EFFORT_CAPABLE:
        reason += f" (effort {effort!r} dropped — {tier} has no reasoning knob)"
        effort = None
    if effort is not None and effort not in _VALID_EFFORT:
        reason += f" (invalid effort {effort!r} dropped)"
        effort = None

    return Route(tier=tier, model=models[tier], effort=effort, reason=reason)


def request_extras(route: Route) -> dict:
    """The /v1/messages body fragment that applies the reasoning level: `output_config.effort` +
    adaptive thinking for sonnet/opus; EMPTY for haiku (which rejects both). Merge into the body."""
    if route.effort is None:
        return {}
    return {"output_config": {"effort": route.effort}, "thinking": {"type": "adaptive"}}


def min_max_tokens(route: Route) -> int:
    """Floor for `max_tokens` given the route — adaptive thinking draws from max_tokens, so a
    high-effort call needs headroom or the answer is starved (all budget spent thinking)."""
    return _MAX_TOKENS_FLOOR.get(route.effort, 0)


def timeout_for(route: Route) -> float:
    """Floor for the HTTP timeout given the route — higher effort takes longer to generate."""
    return _TIMEOUT_FLOOR.get(route.effort, 60.0)
