"""true_token_count must never raise on corpus content (special-token strings).

Real transcripts contain the literal text `<|endoftext|>` (any session discussing tokenizers — like
the apex design sessions themselves). tiktoken's `encode` defaults to `disallowed_special="all"`,
which RAISES ValueError on such a string. That crashed the v2a compile mid-run (block_econs →
true_token_count → enc.encode). The tokenizer is a corpus-content oracle: a special-token string in
content is ORDINARY TEXT to be counted, never a control token — so it must be encoded as text, never
raise. A crash here takes down a signed-policy compile on adversarial-but-benign corpus content.
"""
from __future__ import annotations

import pytest

from apex_router.proxy_engine.tuner.tokens import has_true_tokenizer, true_token_count


@pytest.mark.skipif(not has_true_tokenizer(), reason="needs tiktoken for the exact path")
def test_true_token_count_survives_endoftext_literal():
    """The literal special-token string is counted as text, not raised on."""
    text = "here is the marker <|endoftext|> inside prose"
    n = true_token_count(text)
    assert n > 0


@pytest.mark.skipif(not has_true_tokenizer(), reason="needs tiktoken for the exact path")
def test_true_token_count_survives_multiple_special_tokens():
    """Several special-token strings in one blob (a tokenizer discussion) still count as text."""
    text = "<|endoftext|> and <|fim_prefix|> and <|im_start|> all appear in this transcript"
    n = true_token_count(text)
    assert n > 0
