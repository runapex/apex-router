"""M5a — policy compiler exit gates. §2 / §8.

The compiler is the single component all correctness now concentrates in (§10.1), so these are
the gates, priced in dollars only (token-% is banned from gates — it caused round 2):

  - composition + admission gate compression on THIS deployment's traffic (efficacy, not
    applicability);
  - sign-stability: retrieval-priced Δ$ > 0 at EVERY point of [6:1, 30:1];
  - retrieval ceilings land in the dollar break-even band (~5-15%);
  - stratum leverage is nonzero where the corpus carries volume;
  - the freeze-aware replay is honest (no forbidden T3 re-compression penalty);
  - expected-Δ$ decomposition reconciles exactly (no xl regression can hide in a mean);
  - provenance: the policy is signed, immutable, and hand-edits are rejected;
  - plane separation holds — the neutral policy artifact never drags the tuner onto the hot path.

Several gates double as regressions for the Ornith economics review (findings 4/5/6).
"""
from __future__ import annotations

import json

from apex_router.proxy_engine.policy import CONTENT_CLASSES, PolicyVersion
from apex_router.proxy_engine.tuner.cachesim import CacheSimulator, Pricing
from apex_router.proxy_engine.tuner.compiler import (
    build_freeze_pipeline,
    compile_policy,
    measure_efficacy,
)
from apex_router.proxy_engine.tuner.replay import Request, score

# ---- corpora ---------------------------------------------------------------------------------

def _json_block(n: int, seed: int = 0) -> str:
    """A pretty-printed (indent=2) JSON tool-result — compaction's home turf (whitespace to
    minify without changing the parsed value)."""
    return json.dumps([{"id": i + seed, "name": f"item{i}", "vals": [1, 2, 3, 4, 5]}
                       for i in range(n)], indent=2)


def _growing_json_corpus(sessions=("A", "B"), turns: int = 5) -> list[Request]:
    """Realistic growing sessions: each turn appends a fresh JSON block to the prefix. The frozen
    prefix is byte-stable; only the newest block is addressable."""
    corpus: list[Request] = []
    for sess in sessions:
        content = ""
        for turn in range(turns):
            content += _json_block(60, turn * 60)
            corpus.append(Request(sess, content.encode(), 1500 * (turn + 1),
                                  ts=1000.0 + turn, model="opus-4-8"))
    return corpus


# ---- nested (class × stratum) rule helpers ---------------------------------------------------
# rules[class][stratum] → ClassRule (M5a.1 F1: rules are conditioned on context-size stratum).

def _enabled_anywhere(p, content_class: str) -> bool:
    """True if the class is admitted in at least one stratum cell."""
    return any(r.enabled for r in p.rules[content_class].values())


def _max_ceiling(p, content_class: str) -> float:
    """The largest retrieval ceiling across the class's enabled cells (0 if none enabled)."""
    return max((r.retrieval_ceiling for r in p.rules[content_class].values() if r.enabled),
               default=0.0)


# ---- composition + admission (efficacy, not applicability) -----------------------------------

def test_admission_enables_json_on_compressible_traffic():
    """Gate: a class whose transform actually shrinks this deployment's frontier bytes is
    admitted `enabled` in some stratum; a class with no addressable bytes is not."""
    res = compile_policy(_growing_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    p = res.policy
    assert _enabled_anywhere(p, "json") is True            # compaction shrinks the JSON blocks
    assert all(r.transform == "compaction" for r in p.rules["json"].values())
    # terminal/code have zero addressable bytes in this corpus → not admitted (honest, not a bug)
    assert _enabled_anywhere(p, "terminal") is False
    assert _enabled_anywhere(p, "code") is False
    # prose/opaque have no registered transform yet → never admitted
    assert _enabled_anywhere(p, "prose") is False
    assert _enabled_anywhere(p, "opaque") is False


def test_admission_measures_efficacy_not_applicability():
    """Ornith finding echo: a transform that APPLIES but does not SHRINK must not be admitted.
    Minified JSON (already compact) has ~0 efficacy → not enabled even though compaction 'applies'
    conceptually."""
    # already-minified JSON: compaction's applies() may match structurally, but there's nothing to
    # squeeze → byte_reduction ≈ 0.
    minified = json.dumps([{"id": i} for i in range(100)], separators=(",", ":"))
    corpus = [Request(f"s{i}", minified.encode(), 1200, ts=1000.0, model="opus")
              for i in range(4)]
    eff = measure_efficacy(corpus, "json", min_bytes=1, ratio_floor=0.0)
    res = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0)
    assert eff.byte_reduction < 0.05                       # essentially nothing to gain
    assert _enabled_anywhere(res.policy, "json") is False  # so admission refuses it in every cell


# ---- sign-stability across the calibration band (a DIRECT dollar check) -----------------------

def test_sign_stability_holds_across_full_band():
    """Gate (§2.3.1): for every admitted class, retrieval-priced Δ$ > 0 at EVERY band regime.
    This is checked directly in dollars — NOT via knob-robustness, which an inert knob fools
    (Ornith finding 4)."""
    res = compile_policy(_growing_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    pts = res.band_points["json"]
    assert len(pts) == 5                                   # the full DEFAULT_BAND
    assert all(bp.net_delta > 0 for bp in pts), [round(bp.net_delta, 1) for bp in pts]
    # monotone increasing in regime depth — the property that makes one ceiling cover the band
    deltas = [bp.net_delta for bp in pts]
    assert deltas == sorted(deltas)


def test_retrieval_ceiling_in_dollar_break_even_band():
    """Gate (§6): compiled retrieval ceilings land in the ~5-15% dollar break-even band —
    conservative (SAFETY_MARGIN halves the shallowest break-even)."""
    res = compile_policy(_growing_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    ceiling = _max_ceiling(res.policy, "json")
    assert 0.0 < ceiling <= 0.15                           # priced, positive, conservative
    # disabled classes carry a 0 ceiling (no compression → no retrieval risk to bound)
    assert _max_ceiling(res.policy, "opaque") == 0.0


# ---- stratum leverage ------------------------------------------------------------------------

def test_expected_delta_positive_and_has_stratum_leverage():
    """Gate: predicted Δ$ > 0 on the deployment's own composition, and the win shows up in a real
    stratum (nonzero leverage) rather than being an artifact of an empty blend."""
    res = compile_policy(_growing_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    exp = res.policy.expected
    assert exp.delta_dollars_per_session > 0
    assert any(v > 0 for v in exp.by_stratum.values())     # the saving lives in a populated stratum


def _xl_json_corpus(sessions=("A", "B"), turns: int = 4) -> list[Request]:
    """xl-stratum traffic: each turn's request tokens land in xl (>=128k). The gate that matters
    most — P0.1 has xl at 57.6% of volume and the M4-report error was citing tuner safety with NO
    xl leverage."""
    corpus: list[Request] = []
    for sess in sessions:
        content = ""
        toks = 0
        for turn in range(turns):
            content += json.dumps([{"id": i, "name": f"item{i}", "vals": list(range(10))}
                                   for i in range(3000)], indent=2)
            toks += 140_000
            corpus.append(Request(sess, content.encode(), toks, ts=1000.0 + turn, model="opus-4-8"))
    return corpus


def test_nonzero_xl_leverage_on_xl_traffic():
    """M5a gate: 'stratum-leverage shows nonzero xl leverage'. On xl-scale traffic the compiled
    policy must show a real xl dollar delta — not an inert stratum that a healthy blend hides
    (the exact M4-report error). xl is the stratum that dominates spend (P0.1: 57.6%)."""
    res = compile_policy(_xl_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    exp = res.policy.expected
    assert "xl" in exp.by_stratum
    assert exp.by_stratum["xl"] != 0.0                     # nonzero xl leverage
    assert res.policy.rules["json"]["xl"].enabled is True  # admitted in the xl cell specifically
    assert abs(sum(exp.by_stratum.values()) - exp.delta_dollars_per_session) < 1e-6


def test_expected_delta_decomposition_reconciles_exactly():
    """Ornith finding 5: the by-stratum decomposition must sum to the headline delta, so an xl
    regression cannot hide in a mismatch between the total and its parts. Retrieval spend is
    attributed per-stratum for exactly this reason."""
    res = compile_policy(_growing_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    exp = res.policy.expected
    assert abs(sum(exp.by_stratum.values()) - exp.delta_dollars_per_session) < 1e-6


# ---- freeze-aware replay honesty (the core correctness surface) ------------------------------

def _compiled_rules(corpus):
    """The emitted rule table for a corpus (what build_freeze_pipeline now consumes)."""
    return compile_policy(corpus, version=1, compiled_at=1_720_600_000.0).policy.rules


def _all_raw_rules(rules):
    """Same nested table with every cell disabled — the reference (all-raw) arm."""
    from apex_router.proxy_engine.policy import ClassRule
    return {c: {st: ClassRule(r.transform, False, r.min_bytes, r.ratio_floor, r.retrieval_ceiling)
                for st, r in strata.items()} for c, strata in rules.items()}


def test_freeze_pipeline_never_busts_the_cache():
    """The freeze-aware pipeline transforms only the newest block and keeps the frozen prefix
    byte-identical, so every emission EXTENDS the prior cached bytes — no transform bust ever on a
    monotonically-growing session. (The M4 _real_pipeline re-compressed the whole growing prefix,
    a forbidden T3 rewrite that manufactured a bust penalty — Ornith finding 5 / benchmark note.)"""
    corpus = _growing_json_corpus()
    pipe = build_freeze_pipeline(corpus, _compiled_rules(corpus))
    result = score(corpus, {}, pipe, Pricing())
    assert result.transform_busts == 0

    # and each turn's emission is a byte-prefix of the next (freeze invariant, per session)
    by_sess: dict[str, list[bytes]] = {}
    for req in corpus:
        emitted, _t, _d, _c = pipe(req, {})
        by_sess.setdefault(req.session_id, []).append(emitted)
    for emissions in by_sess.values():
        for earlier, later in zip(emissions, emissions[1:], strict=False):
            assert later.startswith(earlier)               # prefix-freeze holds


def test_freeze_pipeline_beats_raw_without_the_recompaction_penalty():
    """Under freeze, compressing the frontier is a strict dollar win on compressible traffic —
    no shallow-session loss, because behind-frontier blocks are never re-compressed."""
    corpus = _growing_json_corpus()
    rules = _compiled_rules(corpus)
    comp_cost = score(corpus, {}, build_freeze_pipeline(corpus, rules), Pricing()).total_cost
    raw_cost = score(corpus, {}, build_freeze_pipeline(corpus, _all_raw_rules(rules)),
                     Pricing()).total_cost
    assert comp_cost < raw_cost                            # a win, not the M4 shallow penalty
    assert (raw_cost - comp_cost) / raw_cost < 0.7         # sane fraction of raw, not absurd


# ---- per-block pricing regressions (Codex F1/F2/F5/F6) ---------------------------------------

def test_ceiling_derived_from_worst_block_not_aggregate():
    """Codex F1/F2: the retrieval ceiling must be safe for the WORST block — a small frontier
    block on a large cached context (expensive to retrieve) — not an aggregate 'median' block that
    averages a tiny block together with a huge one. Prove the small-on-huge block is priced against
    its REAL context and the ceiling reflects it."""
    from apex_router.proxy_engine.tuner.compiler import _min_bytes_for, block_econs, retrieval_ceiling
    from apex_router.proxy_engine.tuner.sensitivity import DEFAULT_BAND
    big = json.dumps([{"id": i, "v": list(range(20))} for i in range(3000)], indent=2)
    tiny = json.dumps([{"x": i} for i in range(12)], indent=2)
    corpus = [Request("s", big.encode(), 150_000, ts=1000.0, model="opus"),
              Request("s", (big + tiny).encode(), 150_100, ts=1001.0, model="opus")]
    econs = block_econs(corpus, "json", min_bytes=_min_bytes_for("json"), ratio_floor=0.10)
    # the second block is small but sits on the ~150k-token prefix — priced against real context
    small = [e for e in econs if e.block_tokens < 1000]
    assert small and small[0].prefix_tokens > 100_000     # real context, not synthetic block·R
    ceiling = retrieval_ceiling(econs, DEFAULT_BAND, Pricing())
    assert ceiling > 0.0                                   # a positive, worst-case-safe ceiling


def test_ratio_floor_gates_marginal_blocks():
    """Codex F5: a block that compresses BELOW the emitted ratio_floor must not be credited as a
    win — the runtime would ship it raw, so the compiler must model that. Already-minified JSON
    (≈0% further shrink) compresses zero blocks under a 10% floor."""
    from apex_router.proxy_engine.tuner.compiler import _min_bytes_for, block_econs
    minified = json.dumps([{"id": i, "name": f"n{i}"} for i in range(80)], separators=(",", ":"))
    corpus = [Request(f"s{i}", minified.encode(), 900, ts=1000.0, model="opus") for i in range(4)]
    econs = block_econs(corpus, "json", min_bytes=_min_bytes_for("json"), ratio_floor=0.10)
    assert econs and all(not e.compresses for e in econs)  # nothing clears the floor → no credit


def test_diverged_turn_resets_prefix_and_busts():
    """Codex F3: a turn whose content does NOT extend the previous (client edit / compaction) must
    reset the frozen prefix and report a bust — never append divergent bytes to a stale prefix."""
    c1 = json.dumps([{"id": i} for i in range(80)], indent=2)
    c2 = json.dumps([{"id": i} for i in range(40)], indent=2)   # shorter → diverges
    corpus = [Request("s", c1.encode(), 1500, ts=1000.0, model="opus"),
              Request("s", c2.encode(), 1200, ts=1001.0, model="opus")]
    rules = _compiled_rules(corpus)
    pipe = build_freeze_pipeline(corpus, rules)
    emissions = [pipe(r, {}) for r in corpus]
    assert emissions[0][2] is False                        # first turn: clean
    assert emissions[1][2] is True                         # diverged turn: bust reported
    assert emissions[1][3] == "client_edit"
    # the diverged emission is NOT prefix1 + block2 (no stale-prefix append)
    assert not emissions[1][0].startswith(emissions[0][0])


# ---- F1 structural containment + cold start (M5a.1) ------------------------------------------

def test_outlier_block_is_contained_to_its_stratum_cell():
    """M5a.1 F1: a pathological block (tiny frontier on a huge context, ruinous to retrieve) lands
    in the xl cell and must NOT poison the ceiling of the healthy s cell. Under the old per-class
    ceiling one such block collapsed the whole class ~69×; per-(class×stratum) rules contain it."""
    healthy = _growing_json_corpus(sessions=("A", "B", "C", "D"), turns=3)
    big = json.dumps([{"id": i, "v": list(range(30))} for i in range(4000)], indent=2)
    tiny = json.dumps([{"x": i, "y": f"n{i}"} for i in range(8)], indent=2)
    outlier = [Request("OUT", big.encode(), 200_000, ts=1000.0, model="opus"),
               Request("OUT", (big + tiny).encode(), 200_100, ts=1001.0, model="opus")]
    s_clean = compile_policy(healthy, version=1, compiled_at=1e9).policy.rules["json"]["s"]
    dirty = compile_policy(healthy + outlier, version=1, compiled_at=1e9).policy
    s_dirty = dirty.rules["json"]["s"]
    assert s_dirty.retrieval_ceiling == s_clean.retrieval_ceiling   # outlier didn't touch s cell
    assert s_dirty.enabled == s_clean.enabled


def test_min_bytes_is_compiled_not_defaulted():
    """M5a.1 F1: min_bytes is chosen from the per-block econ distribution (Δ$-positive threshold),
    not hard-defaulted to the transform's static gate. On uniform-size traffic it lands at the
    bottom of the distribution (all blocks admitted); it is a real, finite compiled value."""
    res = compile_policy(_growing_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    enabled_cells = [r for r in res.policy.rules["json"].values() if r.enabled]
    assert enabled_cells
    for r in enabled_cells:
        assert 0 < r.min_bytes < (1 << 30)                 # compiled finite threshold, not sentinel


def test_cold_start_empty_and_single_session_are_safe():
    """M5a.1: the every-deployment path. Empty and single-session corpora must emit a safe,
    conservative, signed v0 — all cells disabled, ceilings 0, Δ$ ≥ 0 — without crashing (no
    div-by-zero, no empty-band, no KeyError)."""
    for corpus in ([],
                   [Request("s", json.dumps([{"id": i} for i in range(80)]).encode(),
                            1500, ts=1000.0, model="opus")]):
        res = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0)
        p = res.policy
        assert p.verify() is True                          # signed and valid
        assert p.expected.delta_dollars_per_session >= 0.0  # never promises a loss
        for cls in CONTENT_CLASSES:
            for rule in p.rules[cls].values():
                assert rule.enabled is False               # nothing admitted on no/thin evidence
                assert rule.retrieval_ceiling == 0.0


# ---- token-unit consistency (Codex M5a.1 F1/F2/F3) -------------------------------------------

def test_compression_decision_is_token_based_not_byte_based():
    """M5a.1 F1: the emit test and economics must agree in units. A block credited as compressing
    must actually shed TOKENS (the economics unit), never merely bytes — else retain>1 yields a
    negative break-even on an enabled cell. No enabled cell may carry a non-positive ceiling."""
    res = compile_policy(_growing_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    enabled = [r for strata in res.policy.rules.values() for r in strata.values() if r.enabled]
    assert enabled
    assert all(r.retrieval_ceiling > 0.0 for r in enabled)     # token-real compression → +ceiling

    # a block that sheds bytes must also shed tokens to be credited (retain ≤ 1 for every block)
    from apex_router.proxy_engine.tuner.compiler import _min_bytes_for, block_econs
    econs = block_econs(_growing_json_corpus(), "json",
                        min_bytes=_min_bytes_for("json"), ratio_floor=0.10)
    assert all(e.retain <= 1.0 for e in econs)


def test_expected_delta_uses_measured_tokens_not_byte_ratio():
    """M5a.1 F2: the freeze pipeline's out_tokens (which drives expected.delta — the G target) must
    scale by MEASURED token compression, not a byte-ratio. On whitespace-heavy JSON (byte savings ≫
    token savings) a byte-ratio would overstate the dollar win; the measured path must not."""
    import tiktoken

    from apex_router.proxy_engine.tuner.compiler import build_freeze_pipeline
    enc = tiktoken.get_encoding("cl100k_base")
    # indent=8 JSON: bytes shrink far more than tokens under compaction. A growing multi-turn
    # session over ≥MIN_CELL_BLOCKS turns so the cell actually admits and the frontier compresses.
    corpus = []
    content = ""
    for turn in range(6):
        content += json.dumps([{"x": i, "y": i} for i in range(120)], indent=8)
        corpus.append(Request("s", content.encode(), 2000 * (turn + 1), ts=1000.0 + turn,
                              model="opus"))
    rules = _compiled_rules(corpus)
    pipe = build_freeze_pipeline(corpus, rules)
    # a NON-TERMINAL compressed turn: pick the first turn whose frontier actually compressed in an
    # enabled cell. (Not corpus[-1] — the terminal block has R=0, so its cell may not admit under the
    # priced-block evidence floor; this test is about token-vs-byte scaling, not admission.)
    req = emitted = out_tokens = None
    for candidate in corpus:
        e, ot, _d, _c = pipe(candidate, {})
        if e != candidate.content:
            req, emitted, out_tokens = candidate, e, ot
            break
    assert req is not None, "expected at least one turn whose frontier compressed"
    tok_ratio = len(enc.encode(emitted.decode())) / len(enc.encode(req.content.decode()))
    byte_ratio = len(emitted) / len(req.content)
    assert out_tokens == max(1, round(req.tokens * tok_ratio))  # measured-token scaling
    assert out_tokens != max(1, round(req.tokens * byte_ratio))  # demonstrably not the byte-ratio


def test_min_bytes_threshold_uses_utf8_bytes_consistently():
    """M5a.1 F3: admission's orig_bytes and the runtime's min_bytes gate must both be UTF-8 byte
    lengths (what the wire/cache sees), not Python char counts — else a multibyte block near the
    threshold is admitted in one unit and enforced in another."""
    from apex_router.proxy_engine.tuner.compiler import _min_bytes_for, block_econs
    # multibyte content: chars ≠ UTF-8 bytes
    text = "café résumé " * 400
    corpus = [Request(f"s{i}", text.encode("utf-8"), 2000, ts=1000.0, model="opus")
              for i in range(4)]
    econs = block_econs(corpus, "prose", min_bytes=_min_bytes_for("prose"), ratio_floor=0.0)
    # orig_bytes is the UTF-8 byte length, matching what freeze/enforcement measure
    assert econs and all(e.orig_bytes == len(text.encode("utf-8")) for e in econs)
    assert all(e.orig_bytes != len(text) for e in econs)       # and NOT the char count


def test_byte_floor_token_safety_is_a_measured_admission_gate():
    """M5a.1 review F1: byte-floor token-safety is NOT a theorem — BPE is non-monotone under
    deletion. It is a per-cell MEASURED property: admission runs `_byte_floor_is_token_safe`, and a
    cell where any floor-clearing corpus block is token-negative is REFUSED. Assert every enabled
    cell passes the check that gates its admission.
    """
    from apex_router.proxy_engine.tuner.compiler import _byte_floor_is_token_safe
    corpus = _growing_json_corpus()
    p = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0).policy
    from apex_router.proxy_engine.tuner.compiler import STRATA
    for cls, strata in p.rules.items():
        for st in STRATA:
            rule = strata[st]
            if rule.enabled:
                # every admitted cell provably passes the measured token-safety gate
                assert _byte_floor_is_token_safe(corpus, cls, st, rule.min_bytes, rule.ratio_floor)


def test_token_count_is_non_monotone_under_deletion():
    """M5a.1 review F1 — the FACT that forced the demotion: deleting bytes can INCREASE tokens
    (a merge breaks and the remainder fragments). Pins the reason the byte floor can't be a theorem,
    so no future edit re-asserts monotonicity."""
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    full = "        return    x  +  y"
    deleted = full[:8] + full[9:]                          # delete one space
    assert len(enc.encode(deleted)) > len(enc.encode(full))  # FEWER bytes, MORE tokens


def test_emitted_ratio_floor_admits_only_token_positive_blocks():
    """The runtime byte gate, applied to a cell's real blocks, admits only token-positive ones —
    the observable consequence of the F1 admission gate above."""
    import tiktoken

    from apex_router.proxy_engine.pipeline.transforms import compaction
    from apex_router.proxy_engine.pipeline.transforms.base import Block
    from apex_router.proxy_engine.policy import size_stratum_bytes
    from apex_router.proxy_engine.tuner.composition import session_frontiers
    from apex_router.proxy_engine.tuner.tokens import classify
    enc = tiktoken.get_encoding("cl100k_base")

    corpus = _growing_json_corpus()
    p = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0).policy
    for fr in session_frontiers(corpus):
        if not fr.block:
            continue
        text = fr.block.decode("utf-8", "replace")
        cls = classify(text)
        rule = p.rules.get(cls, {}).get(size_stratum_bytes(len(fr.req.content)))
        if not rule or not rule.enabled or len(fr.block) < rule.min_bytes:
            continue
        b = Block(content=text, tool_name="Read")
        if not compaction.applies(b):
            continue
        em = compaction.run(b, {}).text
        byte_red = 1.0 - len(em.encode("utf-8")) / len(fr.block)
        if byte_red >= rule.ratio_floor:                   # what the runtime byte gate would admit
            token_red = 1.0 - len(enc.encode(em)) / max(1, len(enc.encode(text)))
            assert token_red >= 0.0                        # never token-expanding (safety floor)


def test_no_enabled_cell_yields_dollar_negative_policy():
    """M5a.1 (Codex round 3): the compression decision is made ONCE (`emit_decision`) and shared by
    admission, the freeze replay, and retrieval-spend. A divergent gate previously enabled a cell
    (token-gated admission) that the replay then emitted raw (char-gated) → a signed dollar-NEGATIVE
    policy. The counterexample: JSON whose char reduction is <10% but token reduction is >10%."""
    # char_red ≈ 0.095 < 0.10 but token_red ≈ 0.24 > 0.10 (Codex's exact repro)
    text = json.dumps({f"k{i}": "a" * 10 for i in range(20)}, ensure_ascii=False)
    corpus = [Request(f"s{i}", text.encode(), 2000, ts=1000.0 + i, model="opus") for i in range(3)]
    p = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0).policy
    # whatever admission decided, an ENABLED cell must never produce a net loss
    if any(r.enabled for strata in p.rules.values() for r in strata.values()):
        assert p.expected.delta_dollars_per_session >= 0.0    # never sign a dollar-negative policy


def test_emit_decision_is_the_single_shared_gate():
    """The freeze pipeline's final emitted bytes for a session equal an independent emit_decision
    reconstruction — proof admission and replay use the SAME gate and can't diverge (Codex round 3
    root cause). Uses the char<10% / token>=10% counterexample so a char-gate would disagree."""
    from apex_router.proxy_engine.policy import size_stratum_bytes
    from apex_router.proxy_engine.tuner.compiler import build_freeze_pipeline, emit_decision
    from apex_router.proxy_engine.tuner.composition import session_frontiers
    from apex_router.proxy_engine.tuner.tokens import classify
    # a growing session of the token-dense/char-light JSON (where char and token gates disagree)
    corpus, content = [], ""
    for turn in range(6):
        content += json.dumps({f"k{turn}_{i}": "a" * 10 for i in range(20)}, ensure_ascii=False)
        corpus.append(Request("s", content.encode(), 1500 * (turn + 1), ts=1000.0 + turn,
                              model="opus"))
    rules = _compiled_rules(corpus)
    pipe = build_freeze_pipeline(corpus, rules)

    expected_prefix = b""
    expected_by_req: dict[float, bytes] = {}
    for fr in session_frontiers(corpus):
        if fr.block:
            text = fr.block.decode("utf-8", "replace")
            cls = classify(text)
            rule = rules.get(cls, {}).get(size_stratum_bytes(len(fr.req.content)))
            if rule and rule.enabled and rule.transform:
                # omit the floor arg — exactly like the production freeze pipeline, which uses the
                # TOKEN floor default, NOT rule.ratio_floor (now a byte floor) — Codex M5a.1 #4.
                _c, out = emit_decision(cls, text, len(fr.block), rule.min_bytes)
            else:
                out = text
            expected_prefix += out.encode("utf-8")
        expected_by_req[fr.req.ts] = expected_prefix

    for req in corpus:
        emitted, _t, _d, _c = pipe(req, {})
        assert emitted == expected_by_req[req.ts]   # byte-exact agreement, no char-gate drift


# ---- load totality (Codex M5a.1 F7) ----------------------------------------------------------
# NOTE: the per-block real-R spend + band-invariance properties are now pinned by the SINGLE pricing
# source (test_r_wiring.py::test_signed_by_stratum_equals_admission_per_block_net) — the separate
# `_expected_retrieval_spend` path was deleted (2026-07-15, witness-six chapter close).


def test_load_verified_rejects_non_total_table():
    """M5a.1 F7: load_verified refuses a signed-but-structurally-incomplete policy (a class missing
    from the table), so rule_for's opaque fallback can never KeyError on the hot path."""
    from apex_router.proxy_engine.policy import InvalidPolicy
    res = compile_policy(_growing_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    d = res.policy.to_dict()
    d["rules"].pop("opaque")                                    # drop the fallback class
    resealed = PolicyVersion.from_dict(d).sealed()             # re-sign the malformed table
    try:
        PolicyVersion.load_verified(resealed.to_dict())
        raise AssertionError("expected InvalidPolicy on a non-total table")
    except InvalidPolicy:
        pass


# ---- provenance load gate (Codex F9) ---------------------------------------------------------

def test_load_verified_rejects_tampered_policy():
    """Codex F9: the registry's only load path verifies the seal and refuses tampered policy, so a
    hand-edited rule can't reach rule_for(). load_verified raises InvalidPolicy."""
    from apex_router.proxy_engine.policy import InvalidPolicy
    res = compile_policy(_growing_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    good = res.policy.to_dict()
    # untampered → loads
    assert PolicyVersion.load_verified(good).verify() is True
    # tamper a real cell field in the serialized form → seal no longer matches → refused
    tampered_cell = {**good["rules"]["json"]["s"], "min_bytes": 1}
    tampered = {**good, "rules": {**good["rules"],
                                  "json": {**good["rules"]["json"], "s": tampered_cell}}}
    try:
        PolicyVersion.load_verified(tampered)
        raise AssertionError("expected InvalidPolicy on tampered policy")
    except InvalidPolicy:
        pass


# ---- break-even / retrieval consistency (Ornith finding 6) -----------------------------------

def test_break_even_prob_matches_retrieval_cost_model():
    """Ornith finding 6 (its headline was a FALSE POSITIVE, but the guard is cheap): the
    break-even formula uses the SAME cost model as retrieval(). Pin them together so they can't
    drift apart."""
    p = Pricing()
    sim = CacheSimulator(p)
    L, B, R, f = 20_000, 4_000, 30, 0.365
    sim.request("s", b"X" * (L * 4), L, ts=1000.0)
    cost_event = sim.retrieval("s", B, R, output_tokens=500)
    p_be = CacheSimulator.retrieval_break_even_prob(
        B, f, R, context_tokens=L, pricing=p, output_tokens=500)
    saving = (1 - f) * B * p.p_read * R
    # p_be is the probability where expected retrieval cost == compression saving
    assert abs(p_be * cost_event - saving) < 1e-6
    assert 0.0 < p_be < 1.0


# ---- provenance + immutability (§5 policy_provenance) ----------------------------------------

def test_policy_is_signed_and_verifies():
    res = compile_policy(_growing_json_corpus(), version=3, compiled_at=1_720_600_000.0)
    assert res.policy.seal                                  # a seal was written
    assert res.policy.verify() is True                     # and it matches the body


def test_hand_edit_breaks_the_seal():
    """policy_provenance: the registry loads only compiler-emitted policy. A hand-edit to any
    field changes the canonical bytes and fails verify() — the runtime has no write path."""
    from dataclasses import replace
    res = compile_policy(_growing_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    p = res.policy
    cell = p.rules["json"]["s"]
    flipped = replace(cell, enabled=not cell.enabled)
    tampered = replace(p, rules={**p.rules, "json": {**p.rules["json"], "s": flipped}})
    assert tampered.verify() is False                      # seal no longer matches the body


def test_compilation_is_deterministic():
    """Gate (§3): same corpus + version + compiled_at → byte-identical, identically-sealed policy.
    Reproducible across deployments; compiled_at is an input, never now()."""
    corpus = _growing_json_corpus()
    a = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0).policy
    b = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0).policy
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.seal == b.seal


def test_policy_roundtrips_through_dict():
    """The on-disk form reconstructs to the same sealed policy (registry load path)."""
    res = compile_policy(_growing_json_corpus(), version=2, compiled_at=1_720_600_000.0)
    restored = PolicyVersion.from_dict(res.policy.to_dict())
    assert restored.canonical_bytes() == res.policy.canonical_bytes()
    assert restored.verify() is True


def test_policy_table_is_total_over_content_classes_and_strata():
    """The runtime does pure lookup — the table must have a rule for every (class × stratum) so the
    hot path never KeyErrors. rule_for() falls unknown class/stratum back to opaque (ship raw)."""
    from apex_router.proxy_engine.tuner.compiler import STRATA
    res = compile_policy(_growing_json_corpus(), version=1, compiled_at=1_720_600_000.0)
    p = res.policy
    for cls in CONTENT_CLASSES:
        assert cls in p.rules
        for st in STRATA:
            assert st in p.rules[cls]                             # total over class × stratum
    assert p.rule_for("some-unknown-class", "xl").transform is None   # → opaque, ships raw
    assert p.rule_for("json", "some-unknown-stratum").transform is None  # opaque fallback


# ---- plane separation (the artifact must not drag the tuner onto the hot path) ---------------

def test_policy_module_is_plane_neutral():
    """apex_router.proxy_engine.policy crosses the offline/runtime boundary, so it must import stdlib only — no
    apex_router.proxy_engine.tuner, no pipeline. (test_plane_separation.py guards the hot-path modules; this pins the
    artifact itself.)"""
    import inspect

    import apex_router.proxy_engine.policy as policy_mod
    src = inspect.getsource(policy_mod)
    assert "import apex_router.proxy_engine.tuner" not in src
    assert "from apex_router.proxy_engine.tuner" not in src
    assert "from apex_router.proxy_engine.pipeline" not in src
    assert "import apex_router.proxy_engine.pipeline" not in src
