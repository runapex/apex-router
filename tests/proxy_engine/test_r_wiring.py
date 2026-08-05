"""Wiring per-block remaining_requests into the compiler pricing sites (stock-vs-flow, Fable).

`Frontier` already carries `remaining_requests` (test_r_stratification). This pins the NEXT step: the
economics layer must PRICE each block at its own measured R instead of the band-aggregate
`_requests_for_regime(band)`. A block entering late in a session (small real R) must not be credited the
aggregate horizon it never occupies — that is the terminal/xl over-amortization the arc set out to fix.

Step 1 (this file, first test): `block_econs` must carry each frontier block's `remaining_requests` onto
its `BlockEcon`, so the pricing sites CAN see per-block R. Today `BlockEcon` has no such field.
"""
from __future__ import annotations

import json
from dataclasses import replace

from apex_router.proxy_engine.tuner.cachesim import Pricing
from apex_router.proxy_engine.tuner.compiler import (
    SAFETY_MARGIN,
    BlockEcon,
    band_sign_stability,
    block_econs,
    cell_break_even,
    cell_expected_saving,
    cell_retrieval_exposure,
    cell_sign_stable_across_projects,
    compile_policy,
    retrieval_ceiling,
)
from apex_router.proxy_engine.tuner.replay import Request
from apex_router.proxy_engine.tuner.sensitivity import DEFAULT_BAND


def _json_session(sid: str, n_turns: int, project: str | None = None) -> list[Request]:
    """A session whose frontier each turn is a fresh, standalone JSON array (classifies as json, the
    one class with a registered lossless transform, so blocks are real and priceable). Set
    `frontier_block` explicitly so each turn's priced block is exactly that array — the growing
    cached prefix is modeled by `context_bytes`, not by re-diffing concatenated content."""
    reqs = []
    ctx = 0
    for t in range(n_turns):
        arr = json.dumps([{"id": i, "name": f"item_{i}_{t}"} for i in range(40)]).encode("utf-8")
        reqs.append(
            Request(
                sid,
                content=b"",
                tokens=max(1, ctx // 4),
                ts=float(t),
                model="opus",
                frontier_block=arr,
                context_bytes=ctx,
                project=project,
            )
        )
        ctx += len(arr)
    return reqs


def test_block_econ_carries_per_block_remaining_requests():
    """Each BlockEcon must expose the remaining_requests of the frontier block it prices. For a
    5-turn session the per-position values are [4,3,2,1,0] (same as the Frontier), so pricing can
    amortize each block over its OWN horizon rather than a session-aggregate."""
    econs = block_econs(_json_session("s0", 5), "json", min_bytes=1, ratio_floor=0.0)
    # one econ per frontier block, in session order
    assert [e.remaining_requests for e in econs] == [4, 3, 2, 1, 0]


def test_block_econ_carries_project_for_jackknife():
    """Each BlockEcon must carry its source project, so admission can check Δ$-sign across per-project
    subpopulations (the non-tautological successor to band sign-stability — heterogeneity robustness
    against a cell whose value concentrates in one deployment)."""
    corpus = _json_session("a", 3, project="the reference proxy") + _json_session("b", 3, project="ml")
    econs = block_econs(corpus, "json", min_bytes=1, ratio_floor=0.0)
    assert {e.project for e in econs} == {"the reference proxy", "ml"}


def _econ(block_tokens, prefix_tokens, retain, remaining_requests, compresses=True):
    """A hand-built compressing BlockEcon for exact hindsight-pricing arithmetic (bytes irrelevant
    to the R-pricing under test)."""
    return BlockEcon(
        block_tokens=block_tokens,
        prefix_tokens=prefix_tokens,
        retain=retain,
        orig_bytes=block_tokens,
        out_bytes=int(block_tokens * retain),
        compresses=compresses,
        stratum="l",
        remaining_requests=remaining_requests,
    )


def test_cell_expected_saving_sums_per_block_real_R():
    """Cell expected saving = Σ over compressing blocks of saving at each block's OWN R (hindsight
    accounting over the real population), NOT one aggregate R applied to all."""
    p = Pricing()
    # saving(R) = (1-retain)*block_tokens*p_read*R ; retain 0.5, p_read 0.10
    a = _econ(1000, 10_000, 0.5, 10)  # 0.5*1000*0.1*10 = 500
    b = _econ(2000, 20_000, 0.5, 5)   # 0.5*2000*0.1*5  = 500
    c = _econ(1000, 30_000, 0.5, 0)   # R=0 → 0
    assert cell_expected_saving([a, b, c], p) == 500.0 + 500.0 + 0.0


def test_cell_retrieval_exposure_excludes_R0_terminal_blocks():
    """Σ retrieval cost over blocks WITH retrieval opportunity (R>0). A terminal R=0 block has no
    future request in which a retrieval could occur → economically inert → excluded from the
    denominator (it must NOT drag the ceiling, the min→0 censoring bug)."""
    p = Pricing()
    a = _econ(1000, 10_000, 0.5, 10)
    b = _econ(2000, 20_000, 0.5, 5)
    c = _econ(1000, 30_000, 0.5, 0)   # inert
    # retrieval_cost(R) = (prefix+block)*p_read + block*p_write + block*p_read*R + 500*p_output
    exp_a = 11_000 * 0.10 + 1000 * 1.25 + 1000 * 0.10 * 10 + 500 * 5.0   # 1100+1250+1000+2500=5850
    exp_b = 22_000 * 0.10 + 2000 * 1.25 + 2000 * 0.10 * 5 + 500 * 5.0    # 2200+2500+1000+2500=8200
    assert cell_retrieval_exposure([a, b, c], p) == exp_a + exp_b


def test_cell_break_even_is_ratio_of_sums():
    """p*_cell = Σ(R_i·s_i) / Σ(c_i over R>0) — the portfolio break-even (ratio of SUMS), the single
    probability at which the cell's total expected Δ$ crosses zero."""
    p = Pricing()
    a = _econ(1000, 10_000, 0.5, 10)
    b = _econ(2000, 20_000, 0.5, 5)
    c = _econ(1000, 30_000, 0.5, 0)
    num = 500.0 + 500.0
    den = (11_000 * 0.10 + 1000 * 1.25 + 1000 * 0.10 * 10 + 500 * 5.0) + (
        22_000 * 0.10 + 2000 * 1.25 + 2000 * 0.10 * 5 + 500 * 5.0
    )
    assert cell_break_even([a, b, c], p) == num / den


def test_ceiling_is_the_zero_of_the_admission_function():
    """SELF-CONSISTENCY (the property the old min never had): the ceiling is exactly SAFETY times the
    p where admission Δ(p) = Σ(R_i·s_i) − p·Σ(c_i) = 0. So Δ(ceiling/SAFETY) ≈ 0 per cell — guarding
    the saving site and the ceiling site from drifting apart."""
    p = Pricing()
    econs = [_econ(1000, 10_000, 0.5, 10), _econ(2000, 20_000, 0.5, 5), _econ(1000, 30_000, 0.5, 0)]
    p_star = cell_break_even(econs, p)
    ceiling = SAFETY_MARGIN * p_star
    delta_at_break_even = cell_expected_saving(econs, p) - (ceiling / SAFETY_MARGIN) * (
        cell_retrieval_exposure(econs, p)
    )
    assert abs(delta_at_break_even) < 1e-9


def test_terminal_block_does_not_censor_the_ceiling():
    """THE censoring-bug fix, behaviorally. A cell of two healthy blocks + one inevitable terminal
    (R=0) block. Under the old `min break_even`, the worst block bound the whole cell; the terminal
    block (if priced at its real R=0) would drag the ceiling toward zero — refusing the bet because
    one member resolves after the session ends. Under ratio-of-sums with R=0 inert, the ceiling is
    the portfolio break-even of the blocks that actually carry economics — strictly HIGHER, and the
    terminal block neither helps nor harms it."""
    p = Pricing()
    healthy = [_econ(2000, 50_000, 0.5, 20), _econ(1500, 60_000, 0.5, 12)]
    terminal = _econ(1000, 80_000, 0.5, 0)

    ceiling_with_terminal = retrieval_ceiling(healthy + [terminal], DEFAULT_BAND, p)
    ceiling_without = retrieval_ceiling(healthy, DEFAULT_BAND, p)

    # the terminal block is economically INERT — it washes out of both numerator and denominator,
    # so it does not move the ceiling at all (the min would have let it dominate).
    assert ceiling_with_terminal == ceiling_without
    # and the portfolio ceiling clears the terminal block's own (real-R=0) break-even by a wide
    # margin — the cell is NOT priced on its inevitable last member.
    assert ceiling_with_terminal > terminal.break_even(0, p)


def test_jackknife_rejects_cell_underwater_in_one_project():
    """The non-tautological successor to band sign-stability: a cell may be Δ$-POSITIVE pooled while
    its value concentrates in ONE project and another is underwater at the pooled ceiling — exactly
    the terminal/xl failure shape (value in a subpopulation that may not exist next deployment). The
    per-project jackknife, priced at the POOLED cell's ceiling, must REJECT such a cell."""
    p = Pricing()
    # project A carries the cell (high-R, cheap retrieval); project B is thin + expensive to retrieve
    a = [_econ(3000, 20_000, 0.4, 25), _econ(3000, 20_000, 0.4, 25)]
    b = [_econ(800, 150_000, 0.7, 2), _econ(800, 150_000, 0.7, 2)]
    for e in a:
        e.project = "A"
    for e in b:
        e.project = "B"
    econs = a + b
    ceiling = retrieval_ceiling(econs, DEFAULT_BAND, p)

    # pooled is positive at its own ceiling (the tautology) — a pooled gate would ADMIT
    assert cell_expected_saving(econs, p) - ceiling * cell_retrieval_exposure(econs, p) > 0
    # but project B is underwater at the pooled ceiling → jackknife REJECTS
    assert cell_sign_stable_across_projects(econs, ceiling, p) is False


def test_jackknife_admits_cell_positive_in_every_project():
    """A cell whose Δ$ is positive within EVERY project at the pooled ceiling is heterogeneity-robust
    — the jackknife admits it."""
    p = Pricing()
    a = [_econ(3000, 20_000, 0.4, 25), _econ(3000, 20_000, 0.4, 25)]
    b = [_econ(3000, 22_000, 0.4, 20), _econ(3000, 22_000, 0.4, 20)]
    for e in a:
        e.project = "A"
    for e in b:
        e.project = "B"
    econs = a + b
    ceiling = retrieval_ceiling(econs, DEFAULT_BAND, p)
    assert cell_sign_stable_across_projects(econs, ceiling, p) is True


def test_jackknife_single_project_reduces_to_pooled_positive():
    """With one project (or an unlabeled corpus), the jackknife has no heterogeneity to check and
    reduces to the pooled positivity — it must not spuriously reject a healthy single-project cell."""
    p = Pricing()
    econs = [_econ(3000, 20_000, 0.4, 25), _econ(3000, 20_000, 0.4, 25)]
    for e in econs:
        e.project = "solo"
    ceiling = retrieval_ceiling(econs, DEFAULT_BAND, p)
    assert cell_sign_stable_across_projects(econs, ceiling, p) is True


def _growing_json_corpus(sessions, turns, project):
    """A realistic growing-prefix json corpus (compaction's home turf: indent=2 whitespace to
    minify), labeled with a project so the jackknife sees per-project subpopulations."""
    corpus = []
    for sess in sessions:
        content = ""
        for turn in range(turns):
            content += json.dumps(
                [{"id": i + turn * 60, "name": f"item{i}", "vals": [1, 2, 3, 4, 5]} for i in range(60)],
                indent=2,
            )
            corpus.append(
                Request(sess, content.encode(), 1500 * (turn + 1), ts=1000.0 + turn, model="opus",
                        project=project)
            )
    return corpus


def test_compile_admits_healthy_multiproject_json_through_jackknife():
    """Integration: a healthy corpus that is Δ$-positive in EVERY project still admits json after the
    admission gate switches from the (now-tautological) pooled band-sign to the per-project jackknife.
    Guards the mainline — the rework must not spuriously reject heterogeneity-robust cells."""
    corpus = (
        _growing_json_corpus(("A", "B"), turns=5, project="the reference proxy")
        + _growing_json_corpus(("C", "D"), turns=5, project="ml")
    )
    res = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0)
    assert res.policy.verify()
    enabled = [r for r in res.policy.rules["json"].values() if r.enabled]
    assert enabled, "healthy multi-project json must still admit under the jackknife gate"
    assert all(r.retrieval_ceiling > 0.0 for r in enabled)


def test_reported_sign_stability_is_real_R_consistent():
    """`band_sign_stability` is now a REPORTING function (not the gate). Its numbers must be priced on
    the SAME real-per-block-R expected value as the ceiling — never a mixed unit (real-R ceiling ×
    aggregate-R retrieval). So the reported net_delta at the cell's own ceiling is the correctly-
    labeled tautology Σ(R_i·s_i)·(1−SAFETY), identical at every reported point (real R doesn't vary by
    band regime)."""
    p = Pricing()
    econs = [_econ(3000, 20_000, 0.4, 25), _econ(1500, 30_000, 0.5, 8), _econ(1000, 40_000, 0.5, 0)]
    ceiling = retrieval_ceiling(econs, DEFAULT_BAND, p)
    pts = band_sign_stability(econs, ceiling, DEFAULT_BAND, p)
    expected_net = cell_expected_saving(econs, p) * (1.0 - SAFETY_MARGIN)
    assert pts  # one point per reported regime
    for pt in pts:
        assert abs(pt.net_delta - expected_net) < 1e-6


def test_cold_start_floor_counts_only_priced_blocks_not_terminals():
    """The MIN_CELL_BLOCKS cold-start floor must count only blocks that carry economics (R>0). A cell
    priced from ONE real block plus filler terminal (R=0) blocks is still a one-block bet — the
    terminals contribute nothing to saving or exposure, so they must not satisfy the evidence floor.
    Compile a corpus whose only non-terminal file_read-ish compressing block is a single early block;
    the cell must NOT enable purely because terminals pad the count."""
    # 1 priced block (R=3) + 5 terminal blocks (R=0) in the same cell → priced count is 1, not 6
    econs = [_econ(3000, 20_000, 0.4, 3)] + [_econ(3000, 20_000, 0.4, 0) for _ in range(5)]
    from apex_router.proxy_engine.tuner.compiler import MIN_CELL_BLOCKS, priced_block_count

    assert priced_block_count(econs) == 1
    assert priced_block_count(econs) < MIN_CELL_BLOCKS  # the floor must see 1, not 6


def test_signed_by_stratum_equals_admission_per_block_net():
    """SINGLE PRICING SOURCE (bug #2 cure): the signed `expected_by_stratum` must be the SAME per-block
    net admission prices — Σ over each enabled cell's admitted (survivor) blocks of
    `saving(R) − ceiling·retrieval_cost(R)` — re-aggregated by byte stratum. NOT a freeze-replay gross
    saving minus a separately-computed expected spend (three incommensurable 'saving' definitions).
    So a cell admitted as Δ$-positive can never report a negative signed expected on its own stratum."""
    from apex_router.proxy_engine.tuner.compiler import (
        _min_bytes_for,
        block_econs,
        cell_net_delta,
        compile_policy,
    )

    corpus = (
        _growing_json_corpus(("A", "B"), turns=6, project="the reference proxy")
        + _growing_json_corpus(("C", "D"), turns=6, project="ml")
    )
    p = Pricing()
    n_sessions = len({r.session_id for r in corpus})
    res = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0)

    # independently recompute Σ(admission per-block net) per stratum for the enabled json cells. The
    # signed by_stratum is stored PER SESSION (matching delta_dollars_per_session), so divide.
    expected_by_st: dict[str, float] = {}
    all_econs = block_econs(corpus, "json", min_bytes=_min_bytes_for("json"), ratio_floor=0.10)
    by_st: dict[str, list] = {}
    for e in all_econs:
        by_st.setdefault(e.stratum, []).append(e)
    for st, cell_econs in by_st.items():
        rule = res.policy.rules["json"].get(st)
        if not rule or not rule.enabled:
            continue
        survivors = [e for e in cell_econs if e.compresses and e.orig_bytes >= rule.min_bytes]
        expected_by_st[st] = cell_net_delta(survivors, rule.retrieval_ceiling, p) / n_sessions

    signed = res.policy.expected.by_stratum
    for st, net in expected_by_st.items():
        assert abs(signed.get(st, 0.0) - net) < 1.0, (
            f"stratum {st}: signed {signed.get(st)} != admission per-block net {net}"
        )
        # an admitted (positive-net) cell must not report a negative signed expected
        assert signed.get(st, 0.0) >= 0.0


def test_expected_report_carries_r_denomination_positional_and_is_sealed():
    """STRUCTURAL denomination (pre-empting the arc's own bug class): every Δ$ number the compiler
    signs is priced on POSITIONAL R (an upper bound) until the R_eff survival scan lands. The sealed
    ExpectedReport must carry `r_denomination` so a v2 positional-R policy is MECHANICALLY
    distinguishable from a future R_eff one — nobody (incl. future-you) can compare them as
    commensurable. It must survive round-trip AND be covered by the seal (tampering breaks verify)."""
    from apex_router.proxy_engine.policy import PolicyVersion

    corpus = _growing_json_corpus(("A", "B"), turns=5, project="the reference proxy")
    res = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0)

    assert res.policy.expected.r_denomination == "positional"
    # round-trips through serialization
    d = res.policy.to_dict()
    assert d["expected"]["r_denomination"] == "positional"
    assert PolicyVersion.from_dict(d).expected.r_denomination == "positional"
    # it is SEALED: flipping the denomination on the wire must fail verify()
    assert PolicyVersion.load_verified(d).verify() is True
    tampered = {**d, "expected": {**d["expected"], "r_denomination": "effective"}}
    resealed_body_only = PolicyVersion.from_dict(tampered)  # same seal, changed body
    assert resealed_body_only.verify() is False  # seal no longer matches the tampered denomination


def test_r_denomination_is_seal_backward_compatible_for_positional():
    """BACK-COMPAT: `positional` is the default, so a policy SEALED BY OLD CODE (whose `expected` had
    no `r_denomination` at seal time) must STILL verify — the new field must not fold into the seal
    when it is the default (mirrors the `expires_at` inf→null seal convention). Reconstruct an old
    seal from first principles (seal computed over a body WITHOUT the field) and require it to verify.
    A future `effective` policy DOES seal the field (the tamper-distinguishable case)."""
    from apex_router.proxy_engine.policy import ExpectedReport

    corpus = _growing_json_corpus(("A", "B"), turns=5, project="the reference proxy")
    pos = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0).policy

    # An OLD signed policy: its seal was computed over a body whose "expected" had ONLY
    # delta_dollars_per_session + by_stratum (no r_denomination). Reproduce that exact seal.
    import hashlib
    import hmac

    from apex_router.proxy_engine.policy import resolve_seal_key

    # Reconstruct the old seal under the SAME per-install key compile_policy sealed `pos` with — the
    # shared `_SEAL_KEY` constant was removed (per-install key; see test_per_install_seal_key).
    seal_key = resolve_seal_key()
    body = pos._body()
    body["expected"] = {
        "delta_dollars_per_session": pos.expected.delta_dollars_per_session,
        "by_stratum": dict(pos.expected.by_stratum),
    }
    old_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    old_seal = hmac.new(seal_key, old_bytes, hashlib.sha256).hexdigest()
    old_policy = replace(pos, seal=old_seal)  # default r_denomination=positional, old-style seal
    assert old_policy.verify() is True, "a positional policy sealed by old code must still verify"

    # a policy explicitly priced on effective R seals the field → a different seal than positional
    eff = replace(pos, expected=ExpectedReport(
        pos.expected.delta_dollars_per_session, dict(pos.expected.by_stratum),
        r_denomination="effective",
    )).sealed()
    assert eff.expected.r_denomination == "effective"
    assert eff.verify() is True
    # positional and effective policies with identical numbers must NOT share a seal
    assert eff.seal != old_policy.seal


def test_load_verified_rejects_unknown_r_denomination():
    """FAIL-CLOSED (Codex): the schema defines exactly {positional, effective}. A policy signed with a
    typo'd denomination (`efffective`) has a self-consistent seal (verify() True) but is MALFORMED —
    `load_verified` must reject it at the gate rather than activate a policy whose denomination no
    consumer understands. Missing (legacy) still defaults to positional; a WRONG value fails closed."""
    from apex_router.proxy_engine.policy import ExpectedReport, InvalidPolicy, PolicyVersion

    corpus = _growing_json_corpus(("A", "B"), turns=5, project="the reference proxy")
    pos = compile_policy(corpus, version=1, compiled_at=1_720_600_000.0).policy

    bad = replace(pos, expected=ExpectedReport(
        pos.expected.delta_dollars_per_session, dict(pos.expected.by_stratum),
        r_denomination="efffective",
    )).sealed()
    assert bad.verify() is True  # the SEAL is self-consistent — tamper protection isn't the gap
    try:
        PolicyVersion.load_verified(bad.to_dict())
        raise AssertionError("load_verified must reject an unknown r_denomination")
    except InvalidPolicy:
        pass
    # the two VALID denominations still load
    assert PolicyVersion.load_verified(pos.to_dict()).verify() is True
    eff = replace(pos, expected=ExpectedReport(
        pos.expected.delta_dollars_per_session, dict(pos.expected.by_stratum),
        r_denomination="effective",
    )).sealed()
    assert PolicyVersion.load_verified(eff.to_dict()).verify() is True
