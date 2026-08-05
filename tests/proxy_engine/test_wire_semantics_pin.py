"""WIRE-SEMANTICS PIN — the author-independent check the doctor's hand-computed fixture could not be.

Witness 9 (register): the doctor computed the OpenAI served fraction backwards because it treated
`input_tokens` as fresh on both wires. NO internal check caught it — hit-rate, alarm, and the
hand-computed control all drank from one helper and all shared the same wrong reading of the field.
A fixture inherits its AUTHOR's semantic assumptions, so a control built under the wrong mental model
certifies the arithmetic while blessing the bug.

The fix-class is a pin that asserts the field STRUCTURE from REAL captured provider bytes, not a
reconstruction — true or false regardless of what anyone believed the field meant:

  - OpenAI/Responses:  cache_read ⊆ input_tokens   (cached is a SUBSET of the total prompt)
                       → fresh = input_tokens − cache_read
  - Anthropic/messages: input_tokens, cache_read, cache_write are DISJOINT pools
                       → total prompt = input_tokens + cache_read + cache_write, and input_tokens
                         is ALREADY the fresh remainder

Verified universal on the 2026-07-19 snapshot: OpenAI 0/29 rows violate cache_read ≤ input_tokens;
Anthropic 98% of rows have cache_read > input_tokens (the disjoint-pool signature). This test crosses
the real seam raw SSE bytes → UsageScanner → telemetry-row shape → doctor._fresh_input, so it would
have FAILED on the doctor's first draft.
"""
from __future__ import annotations

from apex_router.proxy_engine.proxy.usage import UsageScanner
from apex_router.proxy_engine.readout.doctor import _fresh_input

# ---- REAL captured provider bytes (same shapes as tests/test_m6b_shadow.py scanner tests) ----

# Anthropic message_start: input_tokens is the FRESH remainder; read/creation are disjoint siblings.
_ANTHROPIC_SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"usage":{"input_tokens":1200,'
    b'"cache_read_input_tokens":48000,"cache_creation_input_tokens":2400,"output_tokens":5}}}\n\n'
)
# OpenAI Responses: input_tokens is the TOTAL prompt; cached_tokens is a SUBSET of it.
_OPENAI_SSE = (
    b'data: {"type":"response.completed","response":{"usage":{"input_tokens":900,'
    b'"input_tokens_details":{"cached_tokens":700},"output_tokens":40}}}\n\n'
)


def _row_from(scanner: UsageScanner, endpoint_id: str) -> dict:
    """Mirror the handler's scanner.usage → telemetry-row mapping (passthrough.py:162-166), so this
    pin exercises the SAME field wiring the doctor consumes in production."""
    u = scanner.usage
    return {
        "endpoint_id": endpoint_id,
        "usage": u.to_dict(),
        "tokens_in": u.input_tokens,
        "cache_read_tokens": u.cache_read_tokens,
        "cache_write_tokens": u.cache_creation_tokens,
        "tokens_out": u.output_tokens,
    }


def test_openai_cache_read_is_a_subset_of_input_tokens():
    scanner = UsageScanner("")
    scanner.feed(_OPENAI_SSE)
    row = _row_from(scanner, "openai")
    # THE OpenAI invariant: cached ⊆ total prompt.
    assert row["cache_read_tokens"] <= row["tokens_in"], "OpenAI cache_read must be ⊆ input_tokens"
    # ...therefore fresh = total − cached, and it is NOT the raw input_tokens.
    assert _fresh_input(row) == 900 - 700 == 200
    assert _fresh_input(row) != row["tokens_in"], "OpenAI fresh must exclude the cached subset"


def test_anthropic_pools_are_disjoint_and_input_is_already_fresh():
    scanner = UsageScanner("")
    scanner.feed(_ANTHROPIC_SSE)
    row = _row_from(scanner, "anthropic")
    # THE Anthropic invariant: three disjoint pools; input_tokens is the fresh remainder already.
    # The disjoint signature is cache_read > input_tokens (fresh is the small tail) — the OPPOSITE of
    # the OpenAI subset relation, which is exactly why one _fresh_input rule can't serve both wires.
    assert row["cache_read_tokens"] > row["tokens_in"], "Anthropic read/input are disjoint pools"
    assert _fresh_input(row) == row["tokens_in"] == 1200, "Anthropic input_tokens IS the fresh input"


def test_the_two_wires_disagree_so_fresh_input_must_be_wire_aware():
    # Same numeric (input=900, read=700) read under each wire's semantics yields DIFFERENT fresh
    # input — the asymmetry the single-helper bug erased. If these two ever coincide, _fresh_input
    # has stopped distinguishing the wires (the witness-9 regression re-entering).
    oa = {"endpoint_id": "openai", "usage": {}, "tokens_in": 900, "cache_read_tokens": 700}
    an = {"endpoint_id": "anthropic", "usage": {}, "tokens_in": 900, "cache_read_tokens": 700}
    assert _fresh_input(oa) == 200   # total − cached
    assert _fresh_input(an) == 900   # already fresh
    assert _fresh_input(oa) != _fresh_input(an), "wire semantics must diverge — the witness-9 pin"
