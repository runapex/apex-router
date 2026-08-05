"""M5a.1 — the enforcement plane `decide()`. §3.

The dumb runtime: table lookup + pure transform + structural floors + freeze, no economics and no
tokenizer on the hot path. These gates pin the §3 behavioural surface (which is fully enumerable
from the policy table) and the plane-neutrality of the classifier the runtime routes with.
"""
from __future__ import annotations

import json

from apex_router.proxy_engine.pipeline.decide import Emission, decide
from apex_router.proxy_engine.policy import classify
from apex_router.proxy_engine.tuner.compiler import compile_policy
from apex_router.proxy_engine.tuner.replay import Request


def _json_policy():
    """A compiled policy with json enabled on pretty-printed JSON (the s-stratum cell)."""
    def blk(n, s=0):
        return json.dumps([{"id": i + s, "name": f"item{i}", "vals": [1, 2, 3, 4, 5]}
                           for i in range(n)], indent=2)
    corpus = []
    for sess in ("A", "B"):
        content = ""
        for turn in range(5):
            content += blk(60, turn * 60)
            corpus.append(Request(sess, content.encode(), 1500 * (turn + 1),
                                  ts=1000.0 + turn, model="opus-4-8"))
    return compile_policy(corpus, version=1, compiled_at=1_720_600_000.0).policy


def _big_json(n=70):
    return json.dumps([{"id": i, "name": f"item{i}", "vals": [1, 2, 3, 4, 5]} for i in range(n)],
                      indent=2)


# ---- the §3 decision surface ------------------------------------------------------------------

def test_frozen_block_emits_stored_bytes_and_never_recomputes():
    """§3/§5.1: freeze wins — a frozen block ships its stored bytes verbatim, no transform runs."""
    p = _json_policy()
    e = decide(_big_json(), p, context_bytes=13000, tool_name="Read",
               frozen=True, frozen_text="STORED-BYTES")
    assert e == Emission("STORED-BYTES", False, "frozen")


def test_enabled_cell_transforms_a_block_over_the_floor():
    """An addressable block (enabled cell, clears min_bytes, clears the byte floor) is transformed
    and emitted — and compaction is lossless (same parsed value)."""
    p = _json_policy()
    block = _big_json(70)
    e = decide(block, p, context_bytes=13000, tool_name="Read")
    assert e.transformed is True and e.reason == "emit"
    assert len(e.text) < len(block)                        # actually smaller
    assert json.loads(e.text) == json.loads(block)         # lossless


def test_block_below_compiled_min_bytes_ships_raw():
    """The compiled min_bytes is a hard step function — a block under it ships raw, unchanged."""
    p = _json_policy()
    rule = p.rules["json"]["s"]
    # a block engineered just below the compiled min_bytes
    small = _big_json(20)
    assert len(small.encode()) < rule.min_bytes
    e = decide(small, p, context_bytes=13000, tool_name="Read")
    assert e.transformed is False and e.reason == "not_addressable"
    assert e.text == small


def test_prose_ships_raw_no_transform_registered():
    """prose has no transform yet → any prose block ships raw (the honest gap; T1-P lands later)."""
    p = _json_policy()
    e = decide("-- Docs: https://example.com/warnings.html — a plain log line", p,
               context_bytes=13000)
    assert e.transformed is False
    assert e.text.startswith("-- Docs:")


def test_stratum_routing_selects_the_context_sized_cell():
    """route() bins by CURRENT context size: the same block routes to different (class × stratum)
    cells at different context sizes — the runtime twin of the compiler's per-stratum pricing."""
    p = _json_policy()
    block = _big_json(70)
    at_s = decide(block, p, context_bytes=13000, tool_name="Read")      # s-stratum: enabled here
    at_xl = decide(block, p, context_bytes=600000, tool_name="Read")  # xl cell: not admitted
    assert at_s.reason == "emit"
    assert at_xl.reason == "not_addressable"               # different cell, different verdict


def test_unknown_transform_name_fails_open():
    """A rule naming a transform the runtime registry doesn't have ships raw (fail-open), never
    crashes the hot path."""
    from dataclasses import replace
    p = _json_policy()
    cell = p.rules["json"]["s"]
    bogus = {**p.rules, "json": {**p.rules["json"],
                                 "s": replace(cell, transform="does-not-exist")}}
    p2 = replace(p, rules=bogus)
    e = decide(_big_json(70), p2, context_bytes=13000, tool_name="Read")
    assert e.transformed is False and e.reason == "unknown_transform"


def test_decide_is_deterministic_and_pure():
    """Same (content, policy, context, tool) → identical Emission. The whole behavioural surface is
    a pure function of the block bytes and the table."""
    p = _json_policy()
    block = _big_json(70)
    a = decide(block, p, context_bytes=13000, tool_name="Read")
    b = decide(block, p, context_bytes=13000, tool_name="Read")
    assert a == b


# ---- routing agreement between the planes (a divergence would mis-route) ----------------------

def test_runtime_stratum_bins_match_the_compiler():
    """Δ2 revised: the runtime routes and the compiler prices with the SAME `size_stratum_bytes` on
    the SAME observable (context bytes) — literally one function, so they cannot diverge (the old
    token→byte ratio, which could misroute a block to a differently-priced cell, is gone)."""
    from apex_router.proxy_engine.pipeline.decide import size_stratum_bytes as runtime_bin
    from apex_router.proxy_engine.policy import size_stratum_bytes as policy_bin
    from apex_router.proxy_engine.tuner import compiler
    assert runtime_bin is policy_bin is compiler.size_stratum_bytes  # one object, no drift possible
    for b in (0, 7999, 8000, 31_999, 32_000, 127_999, 128_000, 511_999, 512_000, 2_000_000):
        assert runtime_bin(b) in ("xs", "s", "m", "l", "xl")


def test_runtime_registry_covers_every_compiler_transform():
    """M5a.1 (Codex #3): the runtime transform registry (_BY_NAME) must resolve every transform the
    compiler can name (module.name for each _TRANSFORMS entry). A drift — e.g. renaming
    compaction.name — would have the compiler price+sign a transform the runtime returns
    `unknown_transform` for → silent under-compression fleet-wide. Pin the agreement."""
    from apex_router.proxy_engine.pipeline.decide import _BY_NAME
    from apex_router.proxy_engine.tuner.compiler import _TRANSFORMS
    compiler_names = {mod.name for mod, _tool in _TRANSFORMS.values() if mod is not None}
    assert compiler_names <= set(_BY_NAME), (
        f"runtime registry missing transforms the compiler emits: {compiler_names - set(_BY_NAME)}")
    # and every runtime entry resolves to a module whose .name matches its key (no typo drift)
    for name, mod in _BY_NAME.items():
        assert mod.name == name


def test_surrogate_content_fails_open_never_crashes():
    """M5a.1 (Codex #1b): a lone surrogate in a block must not crash the hot path — decide() ships
    raw (fail-open), never raises UnicodeEncodeError."""
    p = _json_policy()
    surrogate = json.dumps({"x": "\ud800", "pad": "a" * 8000}, ensure_ascii=True)
    e = decide(surrogate, p, context_bytes=13000, tool_name="Read")  # must not raise
    assert e.transformed is False
    assert e.text == surrogate                              # shipped raw, unchanged


def test_classify_is_the_shared_plane_neutral_contract():
    """The runtime routes with the SAME classify the compiler priced with — they must be the exact
    same function (imported from the plane-neutral apex_router.proxy_engine.policy), or routing and pricing disagree."""
    from apex_router.proxy_engine.tuner.tokens import classify as compiler_classify
    assert classify is compiler_classify                   # literally the same object
    for text, fp, expected in [
        ('[{"a": 1}]', "", "json"),
        ("def f(): pass", "m.py", "code"),
        ("Building...\r\x1b[Kdone", "", "terminal"),
        ("just some plain prose here", "", "prose"),
        # F3 taxonomy: file_read (line-number gutter) and diff are first-class now
        ("  648\tfoo\n  649\tbar\n  650\tbaz\n  651\tqux", "", "file_read"),
        ("1:heading\n2:body\n3:more\n4:tail", "", "file_read"),
        ("diff --git a/x b/x\n@@ -1,2 +1,3 @@\n line", "", "diff"),
    ]:
        assert classify(text, fp) == expected


def test_transfer_gap_guards_zero_expected():
    """M5a.1 review: G = |realized-expected|/expected divides by zero when expected~0 (a zero-value
    policy — today's real-corpus output). The guard falls back to an absolute-dollar band so M5b's
    gate stays defined."""
    from apex_router.proxy_engine.policy import transfer_gap
    # normal case: ratio
    assert transfer_gap(realized_delta=120.0, expected_delta=100.0) == 0.2
    # zero-expected: must NOT raise / must be finite
    g = transfer_gap(realized_delta=0.5, expected_delta=0.0, abs_band=1.0)
    assert g == 0.5 and g == g                             # finite, no div-by-zero
    # a policy that predicted 0 and realized ~0 has a small G, not undefined
    assert transfer_gap(realized_delta=0.0, expected_delta=0.0) == 0.0


def test_zero_value_policy_is_shadow_only():
    """M5a.1 review: a policy with no admitted cell must not run in the live emit path — it's pure
    risk for $0. has_active_policy() gates shadow-only vs live."""
    from apex_router.proxy_engine.tuner.compiler import compile_policy
    # the real-corpus-style outcome: nothing admits → shadow-only
    minified = json.dumps([{"id": i} for i in range(80)], separators=(",", ":"))
    thin = [Request(f"s{i}", minified.encode(), 900, ts=1000.0, model="opus") for i in range(4)]
    p_zero = compile_policy(thin, version=1, compiled_at=1e9).policy
    assert p_zero.has_active_policy() is False             # → run shadow-only
    # a policy that admits json is live-eligible
    assert _json_policy().has_active_policy() is True


def test_classifier_version_folds_into_compiler_hash():
    """M5a.1 review F3: the classifier keys the rule table, so a taxonomy change must make policies
    incomparable — CLASSIFIER_VERSION folds into compiler_hash. Pin that the version exists and
    participates, so a silent taxonomy change can't preserve a stale hash (G-comparability trap)."""
    from apex_router.proxy_engine.policy import CLASSIFIER_VERSION
    from apex_router.proxy_engine.tuner.cachesim import Pricing
    from apex_router.proxy_engine.tuner.compiler import _compiler_hash
    from apex_router.proxy_engine.tuner.sensitivity import DEFAULT_BAND
    assert isinstance(CLASSIFIER_VERSION, int)
    import apex_router.proxy_engine.policy as pol
    base = _compiler_hash(Pricing(), DEFAULT_BAND)
    saved = pol.CLASSIFIER_VERSION
    try:
        pol.CLASSIFIER_VERSION = saved + 1                 # simulate a taxonomy migration
        assert _compiler_hash(Pricing(), DEFAULT_BAND) != base   # → different hash, incomparable
    finally:
        pol.CLASSIFIER_VERSION = saved
