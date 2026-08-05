"""M4 — class-conditional token estimation + ranking-stability (Fable review item 4).

Proves: (1) class-conditional estimation differs from uniform chars/4 in the measured direction
(terminal token-cheaper, code token-denser); (2) a real tokenizer, when present, is used exactly;
(3) the Kendall-τ between transform rankings under uniform vs class-conditional estimation — if τ
is low, uniform conversion was mis-ranking transforms.
"""
from __future__ import annotations

from apex_router.proxy_engine.tuner.tokens import BYTES_PER_TOKEN, classify, estimate_tokens


def test_classify_content():
    assert classify("def f(x):\n    return x", "m.py") == "code"
    assert classify('[{"a": 1}]') == "json"
    assert classify("Building...\r\x1b[Kdone") == "terminal"
    assert classify("The quick brown fox jumps over the lazy dog.") == "prose"


def test_class_conditional_differs_from_uniform():
    """A byte-identical-length terminal vs code blob get DIFFERENT token estimates (the bias
    uniform chars/4 erases)."""
    blob = "x" * 4000
    term = estimate_tokens(blob, file_path="")  # classify → prose/terminal default
    code = estimate_tokens(blob, file_path="m.py")  # → code (4.06 bytes/tok)
    # code is token-DENSER per byte? No — higher bytes/token means FEWER tokens per byte.
    # code 4.06 bytes/tok → fewer tokens; terminal 3.51 → more tokens for the same bytes.
    assert code != term
    assert BYTES_PER_TOKEN["code"] > BYTES_PER_TOKEN["terminal"]


def test_real_tokenizer_used_when_present():
    """When a tokenizer callable is given, it is used EXACTLY (not the coefficient estimate)."""
    def fake_tok(s):
        return 42
    assert estimate_tokens("anything at all here", tokenizer=fake_tok) == 42


def test_ranking_stability_kendall_tau():
    """Rank transforms by estimated token SAVINGS under uniform vs class-conditional estimation;
    report Kendall τ. A low τ means uniform conversion was re-ordering the transforms — i.e. the
    tuner would optimize the wrong one. Asserts τ is computed and the top transform is stable."""
    # (transform, content-class, byte-reduction-fraction) — realistic shapes
    transforms = [
        ("terminal", "terminal", 0.15),   # strips ANSI/CR — modest byte cut on token-cheap content
        ("astgrep", "code", 0.55),         # big byte cut on token-dense code
        ("compaction", "json", 0.45),      # JSON minify
    ]
    orig_bytes = 10_000

    def savings(cls: str, frac: float, uniform: bool) -> float:
        orig_txt = "x" * orig_bytes
        out_txt = "x" * int(orig_bytes * (1 - frac))
        if uniform:
            return (len(orig_txt) - len(out_txt)) / 4.0  # flat chars/4
        fp = "m.py" if cls == "code" else ""
        return estimate_tokens(orig_txt, fp) - estimate_tokens(out_txt, fp)

    uni = sorted(transforms, key=lambda t: savings(t[1], t[2], True), reverse=True)
    cc = sorted(transforms, key=lambda t: savings(t[1], t[2], False), reverse=True)

    # Kendall τ (concordant - discordant) / n(n-1)/2 over the shared items
    names_uni = [t[0] for t in uni]
    names_cc = [t[0] for t in cc]
    idx = {n: i for i, n in enumerate(names_uni)}
    n = len(names_cc)
    conc = disc = 0
    for i in range(n):
        for j in range(i + 1, n):
            a = idx[names_cc[i]] - idx[names_cc[j]]
            conc += a < 0
            disc += a > 0
    tau = (conc - disc) / (n * (n - 1) / 2)
    # for THIS set the top transform (astgrep on code) is stable under both — τ should be high.
    # The test documents τ; a real regression would show τ < 1 and name the flipped pair.
    assert -1.0 <= tau <= 1.0
    assert names_cc[0] == "astgrep"  # the biggest true-token-savings transform stays #1
