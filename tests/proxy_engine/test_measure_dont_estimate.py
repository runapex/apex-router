"""The measure-don't-estimate invariant — a structural guard, in the spirit of
`test_plane_separation` (M5a.1 review §4).

The compiler's Δ$ target (`expected.delta_dollars_per_session`) is the exact quantity the transfer
gap G grades every future policy version against (§7). It must be built from MEASURED token counts.
The subtle trap: the class-conditional byte→token coefficients (`tokens.BYTES_PER_TOKEN`) are for
ABSOLUTE conversion; in a RATIO of two same-class blobs (orig vs compressed) the coefficient
CANCELS, silently collapsing `retain` to a byte-ratio blind to token-density change. Measured bias:
−42.5% (string-heavy JSON) … +28.5% (deep-nested), and the SIGN FLIPS within a class, so no
constant offset corrects it — it breaks G convergence.

At compile time both byte strings are in hand, so estimating is a choice the compiler must never
make. These tests convert that from a discipline into a structural impossibility:

  1. STRUCTURAL: no economics path in `compiler.py` calls the coefficient estimator — it uses the
     exact tokenizer. (AST check, like the plane-separation import guard.)
  2. BEHAVIORAL: the compiler REFUSES to sign a policy when the exact tokenizer is absent, rather
     than falling back to the estimate.
  3. NUMERICAL: `retain` from the compiler path equals the exact tokenizer, not the byte-ratio.
"""
from __future__ import annotations

import ast
import inspect
import json

from apex_router.proxy_engine.tuner import compiler
from apex_router.proxy_engine.tuner.replay import Request


def test_compiler_never_calls_the_coefficient_estimator():
    """STRUCTURAL: `compiler.py` must not call `estimate_tokens` anywhere — every token quantity a
    signed policy depends on flows through the exact `true_token_count`. A future edit that reaches
    for the estimator (whose ratio cancels the coefficient) trips this immediately."""
    src = inspect.getsource(compiler)
    tree = ast.parse(src)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
    assert "estimate_tokens" not in called, (
        "compiler.py calls estimate_tokens — a coefficient-based token ratio cancels to a "
        "byte-ratio and biases the Δ$ target (§4). Use true_token_count.")
    # and it does not import it either (belt and suspenders)
    assert "estimate_tokens" not in {a.name for n in ast.walk(tree)
                                     if isinstance(n, ast.ImportFrom) for a in n.names}


def test_compression_decision_is_not_re_derived_as_a_byte_ratio():
    """STRUCTURAL: the recurring bug (Codex found it three times) is a sibling path re-deriving the
    "does this block compress" gate as a CHARACTER/BYTE ratio — `1 - len(cand)/len(text)` compared
    to a floor — which drifts from admission's token gate and once signed a dollar-NEGATIVE policy.
    Enforce that ONLY `emit_decision` may pair a `len()/len()` reduction with a ratio_floor
    comparison; every other path must call emit_decision, not re-derive it."""
    src = inspect.getsource(compiler)
    tree = ast.parse(src)

    def _has_len_over_len_ratio(fn: ast.FunctionDef) -> bool:
        for node in ast.walk(fn):
            # match `len(...) / len(...)` — the byte/char ratio shape
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                    and isinstance(node.left, ast.Call) and isinstance(node.left.func, ast.Name)
                    and node.left.func.id == "len"
                    and isinstance(node.right, ast.Call) and isinstance(node.right.func, ast.Name)
                    and node.right.func.id == "len"):
                return True
        return False

    def _mentions_ratio_floor(fn: ast.FunctionDef) -> bool:
        return any((isinstance(n, ast.Attribute) and n.attr == "ratio_floor")
                   or (isinstance(n, ast.Name) and n.id == "ratio_floor")
                   for n in ast.walk(fn))

    # emit_decision IS the gate; compile_byte_floor legitimately derives the runtime byte floor
    # from byte reductions (its whole job), comparing token_red to the token floor — not a drifting
    # emit decision. Every OTHER function must call emit_decision, never re-derive a ratio gate.
    allowed = {"emit_decision", "compile_byte_floor"}
    offenders = [fn.name for fn in ast.walk(tree)
                 if isinstance(fn, ast.FunctionDef) and fn.name not in allowed
                 and _has_len_over_len_ratio(fn) and _mentions_ratio_floor(fn)]
    assert not offenders, (
        f"a byte/char-ratio compression gate leaked into {sorted(set(offenders))} — only "
        "emit_decision may derive the gate; other paths must call it (cross-validation round 3).")


def test_compiler_refuses_to_sign_without_an_exact_tokenizer():
    """BEHAVIORAL: with no exact tokenizer, `compile_policy` raises rather than signing a policy
    whose Δ$ came from estimated ratios. Simulate absence by forcing the encoder cache to None."""
    import apex_router.proxy_engine.tuner.tokens as tokens
    saved_enc, saved_tried = tokens._ENCODER, tokens._ENCODER_TRIED
    tokens._ENCODER, tokens._ENCODER_TRIED = None, True  # pretend tiktoken is unavailable
    try:
        assert tokens.has_true_tokenizer() is False
        corpus = [Request("s", json.dumps([{"id": i} for i in range(80)]).encode(),
                          1500, ts=1000.0, model="opus")]
        raised = False
        try:
            compiler.compile_policy(corpus, version=1, compiled_at=1_720_600_000.0)
        except RuntimeError:
            raised = True
        assert raised, "compiler signed a policy without an exact tokenizer"
    finally:
        tokens._ENCODER, tokens._ENCODER_TRIED = saved_enc, saved_tried


def test_retain_equals_exact_tokenizer_not_byte_ratio():
    """NUMERICAL: the compiler's per-block `retain` equals the exact tokenizer ratio — NOT the
    coefficient estimate's byte-ratio — on a shape where the two disagree with opposite sign."""
    import tiktoken

    from apex_router.proxy_engine.tuner.compiler import _min_bytes_for, block_econs
    from apex_router.proxy_engine.tuner.tokens import estimate_tokens
    enc = tiktoken.get_encoding("cl100k_base")

    # string-heavy JSON: byte-ratio OVER-estimates the saving (whitespace-light, token-dense)
    text = json.dumps([{"msg": f"the quick brown fox {i} jumps"} for i in range(80)], indent=2)
    corpus = [Request(f"s{i}", text.encode(), 1500, ts=1000.0, model="opus") for i in range(3)]
    econs = block_econs(corpus, "json", min_bytes=_min_bytes_for("json"), ratio_floor=0.0)
    e = econs[0]
    assert e.compresses

    from apex_router.proxy_engine.pipeline.transforms import compaction
    from apex_router.proxy_engine.pipeline.transforms.base import Block
    emitted = compaction.run(Block(content=text, tool_name="Read"), {}).text
    exact_retain = len(enc.encode(emitted)) / len(enc.encode(text))
    byte_retain = estimate_tokens(emitted) / estimate_tokens(text)

    assert abs(e.retain - exact_retain) < 1e-9        # tracks the exact tokenizer
    assert abs(e.retain - byte_retain) > 0.05         # and is meaningfully NOT the byte-ratio


def test_true_token_count_is_exact_and_memoized():
    """`true_token_count` matches tiktoken exactly and caches by content (finite corpus →
    one-time cost)."""
    import tiktoken

    from apex_router.proxy_engine.tuner.tokens import _TOKENIZER_CACHE, true_token_count
    enc = tiktoken.get_encoding("cl100k_base")
    blob = json.dumps([{"k": f"value {i}"} for i in range(40)])
    assert true_token_count(blob) == len(enc.encode(blob))
    import hashlib
    key = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    assert key in _TOKENIZER_CACHE                    # memoized after first call
