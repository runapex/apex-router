"""Single source of truth for model identity across every apex-router component.

Before this module, each component hardcoded its own "current" model ids and they DRIFTED:
`apex-route.ts` defaulted sonnet-4-5/opus-4-5, `pi-routes.json` sonnet-4-6/opus-4-8,
`learn.ts` sonnet-4-6/opus-4-8, `codeqa.tier_router` sonnet-5/opus-4-8. An adaptive router
whose static floor disagrees with itself routes on vibes. Now:

  - DEFAULTS below are the canonical ids (kept in sync with `integrations/pi/models.json`
    and the user's `~/.apex-router/models.json` overlay).
  - A user overlay at `~/.apex-router/models.json` (env APEX_MODEL_REGISTRY to relocate)
    merges over DEFAULTS — one file edits every consumer at once.
  - The pi extensions (`apex-route.ts`, `learn.ts`) read the SAME file; `local` resolves
    through `local_tier.resolve()` (the active tier), never a hardcoded id — routing
    `>>local` at the non-resident tier triggers an unintended multi-GB ollama load.
    `local_tier.resolve()` now honors `ORNITH_API_MODEL` + `LOCAL_FAMILY` and the
    `local_families` overlay, so `families()["local"]["id"]` follows the resolved backend.
  - ~/.apex-router/models.json may also carry a "local_families" key read by local_tier
    (machine-local model families).

Consumers:
  - codeqa.tier_router  -> tier_model()  (CODEQA_TIER_MODELS env still wins)
  - pi apex-route.ts    -> families()    (same JSON, read in TS)
  - pi learn.ts         -> learn()       (same JSON, read in TS)

Pure stdlib, offline, never raises on a missing/malformed overlay (falls back to DEFAULTS).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SCHEMA_VERSION = 1

DEFAULTS: dict = {
    "schema_version": SCHEMA_VERSION,
    # Frontier Claude tiers (codeqa tier_router; pi frontier/deep families resolve through these).
    "tiers": {
        "haiku": "claude-haiku-4-5",
        "sonnet": "claude-sonnet-5",
        "opus": "claude-opus-4-8",
    },
    # pi per-task families. A family either pins an explicit {"provider","id"} or names a
    # frontier {"provider","tier"} (resolved via `tiers`, so a tier bump moves every family).
    # "effort" is the optional reasoning-effort knob the pi extension applies per request.
    # "local" is special: source=ornith.env — resolved from the ACTIVE tier at read time.
    "pi_families": {
        "kimi": {"provider": "moonshotai", "id": "kimi-k2.6"},
        "kimi-code": {"provider": "moonshotai", "id": "kimi-k2.7-code"},
        "frontier": {"provider": "anthropic", "tier": "sonnet", "effort": "medium"},
        "deep": {"provider": "anthropic", "tier": "opus", "effort": "high"},
        "local": {"provider": "ollama", "source": "ornith.env"},
    },
    # /learn pipeline stages resolve through tiers too.
    "learn": {"provider": "anthropic", "validate_tier": "sonnet", "explain_tier": "opus"},
    # Venue routing policies (DECISION-kimi-codex-routing, measured 2026-08-24):
    # the codex venue's workload runs at p50 346k context (73.8% of requests >250k),
    # so the 1M-window kimi-k3 is the only Kimi model that fits it AS USED — k3's window
    # is load-bearing, and at list price k3 == sonnet-5 ($3/$0.3-read/$15-out) with FREE
    # cache writes. The cost lever is CONTEXT REDUCTION, not model choice: under
    # `downshift_ctx_ceiling`, `downshift_model` (k2.7-code, 262k window) is ~2.9x cheaper
    # and code-specialized. Turbo/highspeed variants are never defaults (2x price,
    # latency-only benefit).
    "venues": {
        "codex": {
            "provider": "moonshotai",
            "default_model": "kimi-k3",
            "downshift_model": "kimi-k2.7-code",
            "downshift_ctx_ceiling": 250_000,
            "ceiling_ctx": 1_000_000,
        },
        "kimi": {
            "provider": "moonshotai",
            "default_model": "kimi-k2.6",
            "code_model": "kimi-k2.7-code",
            "deep_ctx_model": "kimi-k3",
            "deep_ctx_floor": 250_000,
        },
    },
}


def default_path() -> Path:
    env = os.environ.get("APEX_MODEL_REGISTRY")
    if env:
        return Path(env)
    return Path.home() / ".apex-router" / "models.json"


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None = None) -> dict:
    """DEFAULTS merged with the user overlay. Never raises: a missing/malformed overlay
    yields DEFAULTS (routing must not break because a config file is bad)."""
    p = path if path is not None else default_path()
    try:
        overlay = json.loads(p.read_text())
        if isinstance(overlay, dict):
            return _deep_merge(DEFAULTS, overlay)
    except (OSError, ValueError):
        pass
    return dict(DEFAULTS)


def tier_model(tier: str, *, registry: dict | None = None) -> str | None:
    """The model id for a frontier tier name ('haiku'|'sonnet'|'opus'), else None."""
    reg = DEFAULTS if registry is None else registry
    tiers = reg.get("tiers", {})
    m = tiers.get(tier)
    if not (isinstance(m, str) and m):
        # A partially-specified injected registry falls back to the DEFAULT tier id —
        # losing the whole tier map because one overlay omitted it must not break routing.
        m = DEFAULTS["tiers"].get(tier)
    return m if isinstance(m, str) and m else None


def _local_model() -> str:
    """The ACTIVE ornith tier's api model id (from ornith.env via local_tier.resolve())."""
    from .ornith import local_tier
    return local_tier.resolve().api_model


def families(*, registry: dict | None = None) -> dict[str, dict]:
    """pi families resolved to concrete {"provider","id",...} maps.

    A {"provider","tier"} family resolves through `tiers`; the "local" family resolves
    from ornith.env. A family that can't resolve is omitted (the extension warns).
    """
    reg = DEFAULTS if registry is None else registry
    out: dict[str, dict] = {}
    for name, spec in (reg.get("pi_families") or {}).items():
        if not isinstance(spec, dict):
            continue
        provider = spec.get("provider")
        if not isinstance(provider, str) or not provider:
            continue
        entry: dict = {"provider": provider}
        if spec.get("source") == "ornith.env":
            try:
                entry["id"] = _local_model()
            except Exception:
                continue
        elif isinstance(spec.get("tier"), str):
            mid = tier_model(spec["tier"], registry=reg)
            if not mid:
                continue
            entry["id"] = mid
        elif isinstance(spec.get("id"), str) and spec["id"]:
            entry["id"] = spec["id"]
        else:
            continue
        if isinstance(spec.get("effort"), str) and spec["effort"]:
            entry["effort"] = spec["effort"]
        out[name] = entry
    return out


def venue(name: str, *, registry: dict | None = None) -> dict | None:
    """A venue routing policy (e.g. 'codex', 'kimi'), or None if not configured."""
    reg = DEFAULTS if registry is None else registry
    v = (reg.get("venues") or {}).get(name)
    return dict(v) if isinstance(v, dict) else None


def learn(*, registry: dict | None = None) -> dict:
    """The /learn pipeline's (provider, validate_model, explain_model), tier-resolved."""
    reg = DEFAULTS if registry is None else registry
    spec = reg.get("learn") or {}
    provider = spec.get("provider") if isinstance(spec.get("provider"), str) else "anthropic"
    return {
        "provider": provider,
        "validate": tier_model(spec.get("validate_tier", "sonnet"), registry=reg),
        "explain": tier_model(spec.get("explain_tier", "opus"), registry=reg),
    }
