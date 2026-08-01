"""Prompt-cache-reusing batch runner for Ornith.

A multi-item summarizer/review run re-prefills its shared preamble on every item when the
items are sent as independent, unrelated requests — ~3.8s of wasted prefill per item on a
~2,500-token preamble (measured 2026-07-21 on the live :8080 server).

WHY A FROZEN PREFIX (not a growing thread): mlx_lm's server keeps a PromptTrie that can
reuse a prior request's KV in two ways (models/cache.py:fetch_nearest_cache):
  - `shorter` path: a stored entry is a byte-PREFIX of the new prompt → reused directly,
    NOT gated on cache trimmability;
  - `longer` path: a stored entry EXTENDS past the new prompt → needs `trim_prompt_cache`,
    which is GATED on `can_trim_prompt_cache`.
Ornith's architecture mixes non-trimmable `ArraysCache` (SSM/linear) layers with `KVCache`,
so `can_trim_prompt_cache` is False and the `longer`/trim path never fires (root-caused
2026-07-21). Only the un-gated `shorter` (prefix-extension) path yields reuse on this model.

So each item is sent as a fresh `[preamble, item_k]` request. The frozen preamble is a
byte-prefix of every request, so the trie serves it via the `shorter` path every time
(measured: `cached_tokens≈2,322` of ~2,340 prompt tokens, flat across items; 6 items in
1.85s vs 3.64s naive — 2× on tiny items, ~6× when the preamble dominates a slow item).

An EARLIER growing-thread design (append each item + its answer to one conversation) was
measured and REJECTED: `cached` stayed flat at the preamble while the accumulating tail
re-prefilled every turn (cross-item reuse is impossible here — it needs the trim-gated
`longer` path). The stateless per-item form reuses exactly as much, carries no dead tail,
and cannot blow the context window as item count grows.

This is the local analog of the proxy thesis: don't recompute the reused prefix.

Envelope: this reuses the SAME preamble across items — for runs where the instruction +
context is stable and only the per-item question varies. For genuinely independent prompts
with no shared prefix there is nothing to reuse — call `chat_messages` directly.
"""
from __future__ import annotations

from typing import Sequence

from . import ornith_client as oc


def batch_over_preamble(
    preamble: str,
    items: Sequence[str],
    *,
    max_tokens: int = 4096,
    enable_thinking: bool = False,
    temperature: float = 0.3,
    top_p: float = 0.95,
) -> list[oc.ChatResult]:
    """Run each item against a frozen `preamble`, reusing the prompt cache across items.

    The preamble is pinned as the system turn of every request; each item is the single user
    turn. Because the preamble is a byte-prefix of each request, mlx_lm's PromptTrie serves it
    from cache (the un-gated prefix-extension path) — item 1 pays the cold prefill, items 2+
    read the cached preamble.

    `enable_thinking` defaults to **False**: this runner is for fidelity extraction/synthesis
    where thinking is the runaway-budget vector (measured: a trivial extraction answers in ~2
    tokens with thinking off vs ~37 with it on, and an under-budgeted thinking turn emits NO
    final answer and raises OrnithProtocolError). Pass `enable_thinking=True` only for tasks
    that genuinely need reasoning, with a generous `max_tokens`.

    Returns one ChatResult per item, in order. `result.usage.prompt_tokens_details.
    cached_tokens` reports how much of each item's prompt was served from cache (~the preamble
    length once the preamble is warm).

    Raises whatever `chat_messages` raises (OrnithProtocolError on empty/truncated answers,
    OrnithUnavailable on transport failures) — a batch does not swallow a bad item, so the
    caller sees exactly which item failed rather than a silently-short result set.
    """
    if not items:
        return []

    results: list[oc.ChatResult] = []
    for item in items:
        messages = [
            {"role": "system", "content": preamble},
            {"role": "user", "content": item},
        ]
        results.append(
            oc.chat_messages(
                messages,
                max_tokens=max_tokens,
                enable_thinking=enable_thinking,
                temperature=temperature,
                top_p=top_p,
            )
        )
    return results
