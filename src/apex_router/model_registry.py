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
        "sonnet": "claude-sonnet-5-5",
        "opus": "claude-opus-5-5",
        # Fable is deliberately separate from the standard heavy/Opus tier: it is the
        # expensive ceiling for the hardest pure-reasoning checks, not the routine
        # coding or cross-validation default.
        "fable": "claude-fable-5-1",
    },
    # Per-provider prefix for TIER-resolved pi family ids. pi's `foundry` provider names Azure
    # deployments `it-entra-<claude id>`, while `tiers` stays the plain Claude id the
    # proxy/codeqa consumers use. Keyed by provider (not per family) so an overlay that moves a
    # family back to `anthropic` gets the plain id. pi's built-in `anthropic` catalog lags the
    # live ids (pi 0.85.1 has no claude-sonnet-5-5 / claude-opus-5-5), hence foundry.
    "provider_id_prefix": {"foundry": "it-entra-"},
    # pi per-task families. A family either pins an explicit {"provider","id"} or names a
    # frontier {"provider","tier"} (resolved via `tiers`, so a tier bump moves every family).
    # "effort" is the optional reasoning-effort knob the pi extension applies per request.
    # A tier family's id gets `provider_id_prefix[provider]` prepended (see below).
    # "local" is special: source=ornith.env — resolved from the ACTIVE tier at read time.
    "pi_families": {
        "kimi": {"provider": "moonshotai", "id": "kimi-k2.6"},
        "kimi-code": {"provider": "moonshotai", "id": "kimi-k2.7-code"},
        "frontier": {"provider": "foundry", "tier": "sonnet", "effort": "medium"},
        "deep": {"provider": "foundry", "tier": "opus", "effort": "high"},
        # Named Anthropic families mirror model-routing's tiers. `opus` is the routine
        # heavy/coding + independent-review route; `fable` is opt-in max-effort pure
        # reasoning. Haiku has no effort knob and rejects output_config.effort.
        # Fable stays on `anthropic`: there is no foundry deployment for it.
        "haiku": {"provider": "foundry", "tier": "haiku"},
        "sonnet": {"provider": "foundry", "tier": "sonnet", "effort": "medium"},
        "opus": {"provider": "foundry", "tier": "opus", "effort": "high"},
        "fable": {"provider": "anthropic", "tier": "fable", "effort": "max"},
        # GPT tiers are explicit pi families. They use the Codex provider so they
        # work with the ChatGPT/Codex subscription without an OpenAI API key.
        # Ids must exist in pi's openai-codex catalog: gpt-6.1-sol is live on the Azure
        # gateway but absent from pi 0.85.1's catalog (modelRegistry.find -> undefined),
        # so `gpt-sol` stays on 5.6 until pi ships it.
        "gpt-luna": {"provider": "openai-codex", "id": "gpt-5.6-luna", "effort": "low"},
        "gpt-terra": {"provider": "openai-codex", "id": "gpt-5.6-terra", "effort": "medium"},
        "gpt-sol": {"provider": "openai-codex", "id": "gpt-5.6-sol", "effort": "high"},
        # Independent cross-validation reviewer: a DIFFERENT vendor than the Claude author, so the
        # two can disagree. GPT-6.1 Sol via the proxy's Azure GPT path (pi provider `foundry-gpt`,
        # api azure-openai-responses; see RUNBOOK-pi-integration.md for the provider entry).
        "review": {"provider": "foundry-gpt", "id": "it-entra-gpt-6.1-sol", "effort": "high"},
        "local": {"provider": "ollama", "source": "ornith.env"},
    },
    # /learn pipeline stages resolve through tiers too (+ provider_id_prefix, like pi families);
    # an explicit "validate"/"explain" id overrides its tier and is used verbatim.
    "learn": {"provider": "foundry", "validate_tier": "sonnet", "explain_tier": "opus"},
    # Venue routing policies (DECISION-kimi-codex-routing, measured 2026-08-24):
    # the codex venue's workload runs at p50 346k context (73.8% of requests >250k),
    # so the 1M-window kimi-k3 is the only Kimi model that fits it AS USED — k3's window
    # is load-bearing, and at list price k3 == sonnet-5 ($3/$0.3-read/$15-out) with FREE
    # cache writes. The cost lever is CONTEXT REDUCTION, not model choice: under
    # `downshift_ctx_ceiling`, `downshift_model` (k2.7-code, 262k window) is cheaper
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


_FAMILY_SOURCES = ("source", "tier", "id")


def _merge_registry(base: dict, overlay: dict) -> dict:
    """`_deep_merge`, except a pi family is resolved by ONE of id/tier/source: an overlay family
    that names any of them replaces the base's (else a pinned id under a default `tier` family
    would keep resolving through the tier). Other keys (provider, effort) still merge."""
    out = _deep_merge(base, overlay)
    fams = overlay.get("pi_families")
    if isinstance(fams, dict) and isinstance(out.get("pi_families"), dict):
        for name, spec in fams.items():
            merged = out["pi_families"].get(name)
            if isinstance(spec, dict) and isinstance(merged, dict) and any(k in spec for k in _FAMILY_SOURCES):
                out["pi_families"][name] = {k: v for k, v in merged.items()
                                            if k not in _FAMILY_SOURCES or k in spec}
    return out


def load(path: Path | None = None) -> dict:
    """DEFAULTS merged with the user overlay. Never raises: a missing/malformed overlay
    yields DEFAULTS (routing must not break because a config file is bad)."""
    p = path if path is not None else default_path()
    try:
        overlay = json.loads(p.read_text())
        if isinstance(overlay, dict):
            return _merge_registry(DEFAULTS, overlay)
    except (OSError, ValueError):
        pass
    return dict(DEFAULTS)


def tier_model(tier: str, *, registry: dict | None = None) -> str | None:
    """The model id for a frontier tier name ('haiku'|'sonnet'|'opus'|'fable'), else None."""
    reg = DEFAULTS if registry is None else registry
    tiers = reg.get("tiers", {})
    m = tiers.get(tier)
    if not (isinstance(m, str) and m):
        # A partially-specified injected registry falls back to the DEFAULT tier id —
        # losing the whole tier map because one overlay omitted it must not break routing.
        m = DEFAULTS["tiers"].get(tier)
    return m if isinstance(m, str) and m else None


def _provider_prefix(provider: str, reg: dict) -> str:
    """`provider_id_prefix[provider]` (e.g. foundry -> "it-entra-"), else ""."""
    prefixes = reg.get("provider_id_prefix")
    if not isinstance(prefixes, dict):
        prefixes = DEFAULTS["provider_id_prefix"]
    prefix = prefixes.get(provider)
    return prefix if isinstance(prefix, str) else ""


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
        elif isinstance(spec.get("id"), str) and spec["id"]:
            # A pinned id beats a tier left beside it (e.g. by a deep-merged models.json).
            entry["id"] = spec["id"]
        elif isinstance(spec.get("tier"), str):
            mid = tier_model(spec["tier"], registry=reg)
            if not mid:
                continue
            entry["id"] = _provider_prefix(provider, reg) + mid
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
    provider = spec.get("provider") if isinstance(spec.get("provider"), str) else "foundry"
    prefix = _provider_prefix(provider, reg)

    def stage(name: str, default_tier: str) -> str | None:
        # An explicit id (`validate`/`explain`) is used as-is, like a pinned pi family — it lets a
        # deployment run /learn off Claude (e.g. on openai-codex). Else tier-resolved + prefix.
        explicit = spec.get(name)
        if isinstance(explicit, str) and explicit:
            return explicit
        mid = tier_model(spec.get(f"{name}_tier", default_tier), registry=reg)
        return prefix + mid if mid else mid

    return {"provider": provider, "validate": stage("validate", "sonnet"), "explain": stage("explain", "opus")}
