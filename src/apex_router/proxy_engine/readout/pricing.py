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

import re
from dataclasses import dataclass


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
)


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


def rates_for(model: str | None, endpoint_id: str | None) -> Rates:
    """Look up $/M rates for a (model, endpoint). Endpoint exact; model family matched by SUBSTRING
    (to strip vendor prefixes), EXCEPT a delimited differently-priced variant token
    (`mini`/`nano`/`turbo`/…) forces `unknown` so a variant is never billed at the base rate.
    The returned regime NAMES the actual model so every substring match is auditable. Unknown →
    a labeled zero-rate `unknown`
    regime (a dollar figure on it reads as un-priced, never faked)."""
    m = (model or "").lower()
    ep = (endpoint_id or "").lower()
    if not _has_variant_token(m):
        for sub, endpoint, base in _TABLE:
            if sub in m and endpoint == ep:
                # Rebuild the regime to carry the ACTUAL model, not just the family substring.
                regime = f"{base.pricing_regime}({model})"
                return Rates(base.input, base.cache_read, base.cache_write, base.output, regime)
    return Rates(0.0, 0.0, 0.0, 0.0, f"unknown:{model!r}/{endpoint_id!r}")
