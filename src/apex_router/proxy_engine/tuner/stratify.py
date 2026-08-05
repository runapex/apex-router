"""Stratification — (model_family × size) cells with xl volume weights. §8.

The tuner scores per (model_family × size-stratum) cell and volume-weights the blend, because
real traffic is dominated by the xl stratum (P0.1: xl = 57.6% of volume and the hardest to
compress at 6.4%). A knob that helps small contexts but hurts xl is a net loss — the weighting
makes the objective reflect that.

Size strata match the P0.1 decomposition (the reference proxy baseline) so the A/B is apples-to-apples.
"""
from __future__ import annotations

from typing import Literal

Stratum = Literal["xs", "s", "m", "l", "xl"]


def size_stratum(before_tokens: int) -> Stratum:
    """P0.1 strata (by request input tokens)."""
    if before_tokens < 2_000:
        return "xs"
    if before_tokens < 8_000:
        return "s"
    if before_tokens < 32_000:
        return "m"
    if before_tokens < 128_000:
        return "l"
    return "xl"


def model_family(model: str) -> str:
    """Coarse family from a model id (<gateway>-claude-opus-x[1m] → opus). The tuner scores per
    family because compression behavior differs by model tier, but v1 traffic is ~opus-dominated."""
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku", "fable", "gpt", "gemini"):
        if fam in m:
            return fam
    return "unknown"


def cell(model: str, before_tokens: int) -> tuple[str, Stratum]:
    return (model_family(model), size_stratum(before_tokens))
