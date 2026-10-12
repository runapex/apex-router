"""Pricing table for `apex doctor` — absolute $/M-token rates keyed by (model, endpoint).

F-i unit doctrine (register): NEVER emit an unlabeled dollar. Every price carries a
`pricing_regime`
string that the report prints alongside every dollar figure, so a reader always knows what rate it
was
computed at (list-price default, overridable). These are LIST prices — a reader on a negotiated/
enterprise rate overrides via a config file; the regime label makes the substitution honest.

Rates are $ per 1,000,000 tokens. `input` = fresh (uncached) input; `cache_read` = cached-prefix
read
(the discounted tier); `cache_write` = cache creation (Anthropic's write premium; OpenAI has none —
its caching is automatic with no separate write line, verified on the wire); `output` =
generated tokens. Unknown (model, endpoint) → a labeled `unknown` regime with zeros, so a dollar
figure
on unpriced traffic reads as `$0 [pricing_regime=unknown:...]` — visibly un-priced, never a fake
number.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rates:
    """$/M-token rates for one (model, endpoint) + the regime label printed with every figure."""

    input: float
    cache_read: float
    cache_write: float
    output: float
    pricing_regime: str


# List-price table. Keys are (model_substring, endpoint_id). Model matched by SUBSTRING (the wire
# sends `<gateway>-claude-opus-x` etc.; we match `opus`/`haiku`/`gpt-5`). Endpoint is the
# telemetry
# `endpoint_id` (`anthropic` = Anthropic wire, `openai` = Codex/OpenAI wire). Update as list prices
# move;
# the regime string carries the date so a stale table is visible.
_LIST_PRICE_DATE = "2026-07-list"
_KIMI_PRICE_DATE = "2026-08-pi-catalog"

_TABLE: tuple[tuple[str, str, Rates], ...] = (
    # Anthropic — opus tier: input 15 / cache-read 1.5 (0.1×) / cache-write 18.75 (1.25×) / output 75
    ("opus", "anthropic", Rates(15.0, 1.5, 18.75, 75.0, f"list:opus/anthropic:{_LIST_PRICE_DATE}")),
    ("sonnet", "anthropic", Rates(3.0, 0.3, 3.75, 15.0, f"list:sonnet/anthropic:{_LIST_PRICE_DATE}")),
    ("haiku", "anthropic", Rates(0.8, 0.08, 1.0, 4.0, f"list:haiku/anthropic:{_LIST_PRICE_DATE}")),
    # Moonshot/Kimi over the OpenAI-compatible wire. Rates are pinned in
    # docs/DECISION-kimi-codex-routing.md from the 2026-08 pi catalog. Cache writes are free.
    ("kimi-k2.7-code", "openai", Rates(0.95, 0.19, 0.0, 4.0,
                                       f"list:kimi-k2.7-code/openai:{_KIMI_PRICE_DATE}")),
    ("kimi-k2.6", "openai", Rates(0.95, 0.16, 0.0, 4.0,
                                  f"list:kimi-k2.6/openai:{_KIMI_PRICE_DATE}")),
    ("kimi-k3", "openai", Rates(3.0, 0.3, 0.0, 15.0,
                                 f"list:kimi-k3/openai:{_KIMI_PRICE_DATE}")),
    # OpenAI (Codex) — gpt-5 tier: input 15 / cache-read 1.5 / NO cache-write (automatic caching) /
    # output 60.
    # cache_write=0.0 is STRUCTURAL, not unknown: the OpenAI Responses usage carries no write field
    # (verified from a raw-wire capture), so there is nothing to price and no write premium is paid.
    ("gpt-5", "openai", Rates(15.0, 1.5, 0.0, 60.0, f"list:gpt-5/openai:{_LIST_PRICE_DATE}")),
    # OpenAI (Codex) — gpt-6 tier (e.g. it-entra-gpt-6-astra). No AUDITED list price exists yet, so
    # these are the gpt-5 rates carried forward as a PLACEHOLDER. The `provisional:` regime (not
    # `list:`) makes the assumption visible per the F-i doctrine — a reader sees the dollar figure is
    # an assumed rate, not an audited one. Replace with the real gpt-6 list price (and flip the regime
    # to `list:`) the moment it is pinned. Placed AFTER gpt-5 so `gpt-5.x` still matches gpt-5 first.
    ("gpt-6", "openai", Rates(15.0, 1.5, 0.0, 60.0, f"provisional:gpt-6-as-gpt5/openai:{_LIST_PRICE_DATE}")),
)

# Endpoint-label aliases: the wire emits a deployment-specific `endpoint_id` that is NOT the pricing
# key. `foundry` is the Azure-AI-Foundry Anthropic wire (ANTHROPIC_FOUNDRY_BASE_URL) — the same rates
# as the `anthropic` upstream. Without this alias, ALL Claude traffic tagged `foundry` misses the
# table and reads as $0-unknown (verified: 10,763 Claude requests carried endpoint_id=foundry). The
# alias is safe because the family invariant holds in the data: foundry only ever carries Claude,
# openai only ever carries gpt/kimi. NORMALIZED before the table lookup, never stored.
_ENDPOINT_ALIASES: dict[str, str] = {"foundry": "anthropic"}


# Differently-priced SKUs that SHARE a base-model substring. Substring matching is needed to strip
# vendor prefixes (`<gateway>-gpt-5.x` → gpt-5 family), but it would also map cheap
# `gpt-5-mini`/`nano` or premium `kimi-k3-turbo`/`highspeed` variants onto the base rate. Markers
# force these models to labeled `unknown` until their own rate is pinned. Matched as DELIMITED TOKENS,
# not raw substrings, so `litellm-gpt-5` (contains the letters "lite") is not refused.
_UNPRICED_VARIANT_MARKERS = frozenset({"mini", "nano", "small", "lite", "turbo", "highspeed"})


def _has_variant_token(model: str) -> bool:
    """True iff a differently-priced variant marker is a whole DELIMITED model-name token."""
    tokens = set(re.split(r"[^a-z0-9]+", model))
    return bool(tokens & _UNPRICED_VARIANT_MARKERS)


# GPT tier SKUs (`gpt-5.6-luna`, `gpt-6-astra`, ...): the table has ONE gpt-5 rate, and tiers are known to differ
# by far more than rounding (the self-hosting doc lists Luna at $0.10 in / $0.50 out vs gpt-5's $15 / $60). They
# keep the placeholder numbers but are labelled `provisional:`, never `list:` (F-i doctrine; same as gpt-6).
_GPT_TIER_TOKENS = frozenset({"luna", "terra", "sol", "astra", "nova"})

# Negotiated-rate overrides: APEX_PRICING_OVERRIDES (file) > $APEX_HOME/pricing_overrides.json > ~/.apex/...
#   {"source": "azure-ea-2026-10", "rates": {"<exact model id>": {"endpoint": "openai"|"anthropic",
#     "input": n, "cache_read": n, "cache_write": n, "output": n}}}   ($ per 1M tokens, all >= 0)
# A listed (model, endpoint) wins over the table and every dollar on it reads `negotiated:<source>(<model>)`.
# Anything malformed fails open to the table (a pricing file must never break a readout). Cached per mtime.
_overrides_cache: dict = {}


def _overrides_path() -> Path:
    env = os.environ.get("APEX_PRICING_OVERRIDES")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("APEX_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".apex") / "pricing_overrides.json"


def _load_overrides() -> dict:
    p = _overrides_path()
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {}
    key = (str(p), mt)
    if key in _overrides_cache:
        return _overrides_cache[key]
    out: dict = {}
    try:
        d = json.loads(p.read_text())
        src = str(d.get("source") or "overrides")[:40] if isinstance(d, dict) else None
        for model, r in (d.get("rates") or {}).items() if isinstance(d, dict) else []:
            ep = _ENDPOINT_ALIASES.get(str(r.get("endpoint", "")).lower(), str(r.get("endpoint", "")).lower())
            vals = [float(r[k]) for k in ("input", "cache_read", "cache_write", "output")]
            if ep and all(math.isfinite(v) and v >= 0 for v in vals):
                out[(str(model).lower(), ep)] = Rates(*vals, f"negotiated:{src}({model})")
    except Exception:  # noqa: BLE001 — malformed overrides fail open to the list table
        out = {}
    _overrides_cache.clear()
    _overrides_cache[key] = out
    return out


def rates_for(model: str | None, endpoint_id: str | None) -> Rates:
    """Look up $/M rates for a (model, endpoint). Endpoint exact; model family matched by SUBSTRING
    (to strip vendor prefixes), EXCEPT a delimited differently-priced variant token
    (`mini`/`nano`/`turbo`/…) forces `unknown` so a variant is never billed at the base rate.
    The returned regime NAMES the actual model so every substring match is auditable. Unknown →
    a labeled zero-rate `unknown`
    regime (a dollar figure on it reads as un-priced, never faked)."""
    m = (model or "").lower()
    ep = (endpoint_id or "").lower()
    ep = _ENDPOINT_ALIASES.get(ep, ep)  # normalize deployment endpoint label → pricing endpoint key
    ov = _load_overrides().get((m, ep))
    if ov is not None:
        return ov
    if not _has_variant_token(m):
        for sub, endpoint, base in _TABLE:
            if sub in m and endpoint == ep:
                # Rebuild the regime to carry the ACTUAL model, not just the family substring.
                regime = f"{base.pricing_regime}({model})"
                if regime.startswith("list:") and set(re.split(r"[^a-z0-9]+", m)) & _GPT_TIER_TOKENS:
                    regime = "provisional:gpt-tier-as-" + regime[len("list:"):]
                return Rates(base.input, base.cache_read, base.cache_write, base.output, regime)
    return Rates(0.0, 0.0, 0.0, 0.0, f"unknown:{model!r}/{endpoint_id!r}")
