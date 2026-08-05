"""Tests for amr.gate — the §6 out-of-sample, FDR-corrected promotion gate.

The gate decides whether a candidate cell becomes an ACTIVE routing cell. It is the
single most load-bearing decision-metric in the system, so the discipline (from
measurement-over-attribution) is baked into the tests:

  1. sample floor        — < K paired outcomes per candidate -> NOT promoted
  2. out-of-sample flip  — winner chosen on the PROMOTION split must re-confirm on a
                           HELD-OUT confirmation split (kills winner's bias)
  3. FDR across cells    — "significant" = surviving Benjamini-Hochberg, not raw p
  4. replication         — the flip must appear across >= M distinct capture windows
  - healing              — a promoted cell that fails re-confirmation un-promotes
  - CANNOT-DECIDE        — anything short of all conditions -> parent's safe default

A CellEvidence bundles, per cell: the parent's incumbent model, and per candidate
model the paired per-step deltas (candidate - incumbent) on the promotion split and
on the confirmation split, plus the set of capture windows those confirmation deltas
came from.
"""
import pytest

from apex_router import gate, stats


def _cell(cell_id, parent_type, incumbent, promo, confirm, windows, provenance="objective"):
    """Build a CellEvidence. `promo`/`confirm` are {model: [deltas vs incumbent]}."""
    return gate.CellEvidence(
        cell_id=cell_id, parent_task_type=parent_type, incumbent_model=incumbent,
        promo_deltas=promo, confirm_deltas=confirm, confirm_windows=windows,
        provenance=provenance,
    )


def _strong(n, val=0.25):
    return [val] * n


# --------------------------------------------------------------------------- #
# evaluate_cell — per-cell verdict BEFORE cross-cell FDR
# --------------------------------------------------------------------------- #
def test_below_sample_floor_is_not_promotable():
    # Only 3 paired outcomes per candidate; floor K=8 -> data-starved -> parent default.
    c = _cell("c1", "generate", "opus",
              promo={"sonnet": _strong(3)}, confirm={"sonnet": _strong(3)},
              windows={"w1", "w2"})
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is False
    assert v.chosen_model == "opus"          # falls back to incumbent
    assert "sample floor" in v.reason.lower()


def test_insufficient_windows_is_not_promotable():
    # Enough samples and a real effect, but confirmation came from a SINGLE window
    # (M=2 required) -> replication not established -> not promotable.
    c = _cell("c2", "generate", "opus",
              promo={"sonnet": _strong(20)}, confirm={"sonnet": _strong(20)},
              windows={"w1"})
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is False
    assert "window" in v.reason.lower()


def test_winner_that_fails_confirmation_is_not_promotable():
    # 'sonnet' wins on the promotion split but its advantage EVAPORATES on the
    # held-out confirmation split (deltas ~ 0) -> out-of-sample flip fails.
    c = _cell("c3", "generate", "opus",
              promo={"sonnet": _strong(20, 0.3)},
              confirm={"sonnet": [0.0, 0.01, -0.01, 0.0] * 5},
              windows={"w1", "w2", "w3"})
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is False
    assert v.chosen_model == "opus"


def test_genuine_out_of_sample_winner_is_promotable():
    # 'sonnet' beats the incumbent on BOTH splits, enough samples, >=2 windows.
    c = _cell("c4", "generate", "opus",
              promo={"sonnet": _strong(20, 0.3)},
              confirm={"sonnet": _strong(20, 0.28)},
              windows={"w1", "w2", "w3"})
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is True
    assert v.chosen_model == "sonnet"
    assert 0.0 <= v.pvalue <= 1.0


def test_winner_selected_on_promotion_split_not_confirmation():
    # Two candidates: 'sonnet' wins the PROMOTION split; 'haiku' looks better only on
    # confirmation. The gate must pick the promotion-split winner (sonnet) and then
    # test THAT model out-of-sample — never peek at confirmation to choose the winner.
    c = _cell("c5", "explore", "opus",
              promo={"sonnet": _strong(20, 0.30), "haiku": _strong(20, 0.05)},
              confirm={"sonnet": _strong(20, 0.26), "haiku": _strong(20, 0.40)},
              windows={"w1", "w2"})
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.candidate_tested == "sonnet"


def test_no_candidate_beats_incumbent_is_not_promotable():
    # Every candidate is worse than the incumbent on the promotion split.
    c = _cell("c6", "review", "opus",
              promo={"sonnet": _strong(20, -0.2)},
              confirm={"sonnet": _strong(20, -0.2)},
              windows={"w1", "w2"})
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is False
    assert v.chosen_model == "opus"


# --------------------------------------------------------------------------- #
# run_gate — cross-cell FDR + final promotion decisions
# --------------------------------------------------------------------------- #
# A confirmation-split delta list whose one-sided bootstrap p-value is MARGINAL
# (~0.021 at seed 12345, n=10): clears raw alpha=0.05 but not a BH threshold once
# the tested family is large. Verified empirically.
_MARGINAL_CONFIRM = [0.124, 0.605, -0.085, -0.384, 0.235, 0.35, 0.279, 0.321, 0.134, -0.008]


# A near-null confirmation delta list: mean slightly positive (so the cell ENTERS the
# tested family) but its one-sided bootstrap p is high (~0.38 at seed 12345) — a
# "diluter" that BH never rejects yet still inflates the multiple-comparison count.
_DILUTER_CONFIRM = [0.3, -0.28, 0.3, -0.28, 0.3, -0.28, 0.3, -0.28, 0.3, -0.26]


def test_fdr_blocks_a_marginal_winner_diluted_by_family():
    # The marginal cell (raw p~0.021 < 0.05) plus 3 diluter cells that enter the
    # family with high p (~0.38). Family size m=4 -> BH rank-1 threshold 0.05/4 =
    # 0.0125 < 0.021, so the marginal cell is NOT promoted under FDR, even though a
    # raw per-cell test would promote it.
    cells = [_cell("real", "generate", "opus",
                   promo={"sonnet": _strong(10, 0.2)},
                   confirm={"sonnet": list(_MARGINAL_CONFIRM)},
                   windows={"w1", "w2", "w3"})]
    for i in range(3):
        cells.append(_cell(f"dil{i}", "generate", "opus",
                           promo={"sonnet": _strong(10, 0.2)},
                           confirm={"sonnet": list(_DILUTER_CONFIRM)},
                           windows={"w1", "w2", "w3"}))
    results = gate.run_gate(cells, k=8, m_windows=2, alpha=0.05)
    promoted = {r.cell_id for r in results if r.promoted}
    assert "real" not in promoted            # multiplicity correction suppressed it
    assert promoted == set()


def test_raw_marginal_would_pass_without_fdr_family():
    # Control: the SAME marginal cell, tested alone, DOES promote — proving it's the
    # multiplicity correction (not the effect size) that suppresses it above.
    solo = _cell("real", "generate", "opus",
                 promo={"sonnet": _strong(10, 0.2)},
                 confirm={"sonnet": list(_MARGINAL_CONFIRM)},
                 windows={"w1", "w2", "w3"})
    results = gate.run_gate([solo], k=8, m_windows=2, alpha=0.05)
    assert results[0].promoted is True


def test_strong_effect_survives_fdr_and_promotes():
    cells = [_cell("strong", "generate", "opus",
                   promo={"sonnet": _strong(30, 0.35)},
                   confirm={"sonnet": _strong(30, 0.33)},
                   windows={"w1", "w2", "w3"})]
    for i in range(10):
        cells.append(_cell(f"null{i}", "generate", "opus",
                           promo={"sonnet": [0.0, 0.01, -0.01, 0.0] * 5},
                           confirm={"sonnet": [0.0, -0.01, 0.01, 0.0] * 5},
                           windows={"w1", "w2"}))
    results = gate.run_gate(cells, k=8, m_windows=2, alpha=0.05)
    strong = next(r for r in results if r.cell_id == "strong")
    assert strong.promoted is True
    assert strong.chosen_model == "sonnet"


def test_unpromoted_cells_route_to_parent_default():
    cells = [_cell("weak", "review", "opus",
                   promo={"sonnet": [0.0, 0.01] * 10},
                   confirm={"sonnet": [0.0, -0.01] * 10},
                   windows={"w1", "w2"})]
    results = gate.run_gate(cells, k=8, m_windows=2, alpha=0.05)
    r = results[0]
    assert r.promoted is False
    assert r.chosen_model == "opus"          # parent incumbent / safe default


# --------------------------------------------------------------------------- #
# healing — a previously promoted cell that no longer confirms reverts
# --------------------------------------------------------------------------- #
def test_previously_promoted_cell_heals_when_effect_gone():
    # Was promoted before; new evidence shows no out-of-sample advantage -> un-promote.
    c = _cell("healme", "generate", "opus",
              promo={"sonnet": _strong(20, 0.3)},
              confirm={"sonnet": [0.0, 0.01, -0.02, 0.0] * 5},
              windows={"w1", "w2", "w3"})
    results = gate.run_gate([c], k=8, m_windows=2, alpha=0.05,
                            previously_promoted={"healme": "sonnet"})
    r = results[0]
    assert r.promoted is False
    assert r.chosen_model == "opus"
    assert r.healed is True


# --------------------------------------------------------------------------- #
# Regression — confirmed by Codex adversarial cross-validation (the reference window)
# --------------------------------------------------------------------------- #
def test_f1_confirmation_null_cells_stay_in_the_bh_family():
    # BUG (cross-validation): cells with a nonpositive confirmation mean were excluded from
    # the BH family, so a lone marginal winner became a "family of one" and promoted
    # with no multiplicity penalty. Null cells that STRUCTURALLY reached the
    # confirmation test must remain in the family (as p~1.0), diluting it.
    cells = [_cell("real", "generate", "opus",
                   promo={"sonnet": _strong(10, 0.2)},
                   confirm={"sonnet": list(_MARGINAL_CONFIRM)},
                   windows={"w1", "w2", "w3"})]
    for i in range(3):
        cells.append(_cell(f"cnull{i}", "generate", "opus",
                           promo={"sonnet": _strong(10, 0.2)},      # passes promo>0
                           confirm={"sonnet": [-0.01] * 10},         # null on confirmation
                           windows={"w1", "w2", "w3"}))
    results = gate.run_gate(cells, k=8, m_windows=2, alpha=0.05)
    promoted = {r.cell_id for r in results if r.promoted}
    assert "real" not in promoted        # family size 4 -> BH suppresses the marginal cell


def test_f2_nan_evidence_never_promotes():
    # BUG (cross-validation): NaN comparisons are all-false, so a NaN cell returned p=0.0005
    # and promoted. NaN/inf evidence must be treated as no-signal, never promoted.
    c = gate.CellEvidence("nan", "generate", "opus",
                          {"sonnet": [float("nan")] * 10},
                          {"sonnet": [float("nan")] * 10},
                          {"w1", "w2", "w3"})
    results = gate.run_gate([c], k=8, m_windows=2, alpha=0.05)
    assert results[0].promoted is False
    assert results[0].chosen_model == "opus"


def test_f3_winner_is_promotion_split_only_ignoring_confirmation_length():
    # BUG (cross-validation): the candidate filter required BOTH splits to clear the floor
    # BEFORE choosing the winner, so a promotion-split winner (A) missing one
    # confirmation sample was discarded and the runner-up (B) promoted. The winner
    # must be chosen on the promotion split alone.
    c = gate.CellEvidence("c", "explore", "opus",
                          {"A": _strong(8, 0.5), "B": _strong(8, 0.1)},
                          {"A": _strong(7, 0.5), "B": _strong(8, 0.1)},   # A short by 1 on confirm
                          {"w1", "w2", "w3"})
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    # A is the true promotion winner but is confirmation-under-floor -> the cell is
    # NOT promotable, and it must NOT silently promote B instead.
    assert v.promotable is False              # (cross-validation: assert non-promotable explicitly)
    assert v.candidate_tested in ("A", None)
    assert v.chosen_model != "B"


def test_f5_duplicate_cell_ids_do_not_corrupt_bh():
    # BUG (cross-validation): rejection decisions keyed by cell_id overwrote duplicates, so a
    # high-p duplicate could inherit a low-p sibling's promotion. Positional mapping
    # must keep each verdict's own decision.
    strong = _cell("dup", "generate", "opus",
                   promo={"sonnet": _strong(30, 0.35)}, confirm={"sonnet": _strong(30, 0.33)},
                   windows={"w1", "w2", "w3"})
    weak = _cell("dup", "generate", "opus",
                 promo={"sonnet": [-0.2] * 10}, confirm={"sonnet": [-0.2] * 10},
                 windows={"w1", "w2", "w3"})
    results = gate.run_gate([strong, weak], k=8, m_windows=2, alpha=0.05)
    # exactly one result per input row, and the structurally-failing one is not promoted
    assert len(results) == 2
    promoted_flags = [r.promoted for r in results]
    assert promoted_flags.count(True) <= 1
    # the weak duplicate (row 2) must never be promoted
    assert results[1].promoted is False


def test_f6_absent_previously_promoted_cell_is_healed():
    # BUG (cross-validation): a previously-promoted cell missing from the new input produced
    # NO result, so a consumer left the route active. It must emit a healed result.
    results = gate.run_gate([], k=8, m_windows=2, alpha=0.05,
                            previously_promoted={"gone": "sonnet"})
    gone = [r for r in results if r.cell_id == "gone"]
    assert len(gone) == 1
    assert gone[0].promoted is False
    assert gone[0].healed is True
    assert gone[0].chosen_model == "opus" or gone[0].chosen_model != "sonnet"


def test_f7_provenance_case_insensitive_uses_judge_floor():
    # BUG (cross-validation): only exact 'judge' got the 2x floor; 'Judge'/'JUDGE' fell through
    # to the objective floor. With K=8 judge-floor=16, a 10-sample judge cell must be
    # below floor regardless of case.
    c = gate.CellEvidence("j", "review", "opus",
                          {"sonnet": _strong(10, 0.3)}, {"sonnet": _strong(10, 0.3)},
                          {"w1", "w2", "w3"}, provenance="JUDGE")
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is False
    assert "floor" in v.reason.lower()


def test_f7_nonpositive_k_or_windows_rejected():
    # BUG (cross-validation): negative k / nonpositive m_windows allowed one-sample zero-window
    # promotion. They must be rejected.
    c = _cell("c", "generate", "opus",
              promo={"sonnet": _strong(10, 0.3)}, confirm={"sonnet": _strong(10, 0.3)},
              windows={"w1"})
    with pytest.raises(ValueError):
        gate.evaluate_cell(c, k=0, m_windows=2)
    with pytest.raises(ValueError):
        gate.evaluate_cell(c, k=8, m_windows=0)


# --------------------------------------------------------------------------- #
# Regression — Codex PASS 2 (validating the pass-1 fixes; found rework defects)
# --------------------------------------------------------------------------- #
def test_p2_duplicate_id_promoted_is_not_also_healed():
    # BUG (cross-validation#1): a dup id with one strong (promoted) and one weak row, where
    # the id was previously promoted, emitted BOTH a promoted row and a healed row —
    # the healed row could tear down the legitimate re-promotion. If ANY row for the id
    # promoted this run, NO row for that id may be marked healed.
    strong = _cell("dup", "generate", "opus",
                   promo={"sonnet": _strong(30, 0.35)}, confirm={"sonnet": _strong(30, 0.33)},
                   windows={"w1", "w2", "w3"})
    weak = _cell("dup", "generate", "opus",
                 promo={"sonnet": [-0.2] * 10}, confirm={"sonnet": [-0.2] * 10},
                 windows={"w1", "w2", "w3"})
    results = gate.run_gate([strong, weak], k=8, m_windows=2, alpha=0.05,
                            previously_promoted={"dup": "sonnet"})
    dup = [r for r in results if r.cell_id == "dup"]
    assert any(r.promoted for r in dup)              # the strong row promotes
    assert not any(r.healed for r in dup)            # ...so nothing for 'dup' heals


def test_p2_windows_cannot_be_borrowed_across_candidates():
    # BUG (cross-validation#2): a legacy bare window set let winner A (confirmed only in wA)
    # borrow B's window wB to satisfy replication. Per-candidate windows must be used;
    # A confirmed in a single window fails m_windows=2.
    c = gate.CellEvidence(
        "c", "explore", "opus",
        {"A": _strong(10, 0.5), "B": _strong(10, 0.1)},
        {"A": _strong(10, 0.5), "B": _strong(10, 0.1)},
        {"A": {"wA"}, "B": {"wB"}},                  # per-candidate: A only in wA
    )
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is False
    assert "window" in v.reason.lower()


def test_p2_unknown_provenance_fails_closed_to_judge_floor():
    # BUG (cross-validation#3): provenance != exact 'judge' fell OPEN to the objective
    # floor, so 'judeg'/None/bytes weakened the gate. Unknown provenance must fail
    # CLOSED to the stricter judge floor (2*k). 10 samples < 16 -> not promotable.
    for bad in ("judeg", None, "Objectivee", "xyz"):
        c = gate.CellEvidence("c", "review", "opus",
                              {"sonnet": _strong(10, 0.3)}, {"sonnet": _strong(10, 0.3)},
                              {"w1", "w2", "w3"}, provenance=bad)
        v = gate.evaluate_cell(c, k=8, m_windows=2)
        assert v.promotable is False, f"provenance={bad!r} promoted below judge floor"


def test_p2_known_objective_provenance_still_uses_objective_floor():
    # Guard the fix doesn't over-correct: an explicitly 'objective' cell keeps floor k.
    c = gate.CellEvidence("c", "generate", "opus",
                          {"sonnet": _strong(10, 0.35)}, {"sonnet": _strong(10, 0.33)},
                          {"w1", "w2", "w3"}, provenance="objective")
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is True

def test_p2_overflow_magnitude_deltas_do_not_promote():
    # BUG (cross-validation#4): [1e308]*8 passes the elementwise finite check but its mean
    # overflows to inf; centered values become -inf and the p floored to 0.0005. A
    # non-finite mean must make the p-value raise / the cell not promote.
    c = gate.CellEvidence("c", "generate", "opus",
                          {"sonnet": [1e308] * 10}, {"sonnet": [1e308] * 10},
                          {"w1", "w2", "w3"})
    results = gate.run_gate([c], k=8, m_windows=2, alpha=0.05)
    assert results[0].promoted is False


# --------------------------------------------------------------------------- #
# §5.4 cost tiebreak — SOUND version (cross-validation): cost/latency break ties ONLY
# among candidates that EACH independently pass the full gate; promotion uses the
# CHOSEN model's OWN p-value. No "not-different => tied" fallacy, no wrong-p promotion.
# --------------------------------------------------------------------------- #
def test_cost_tiebreak_prefers_cheaper_independently_passing_candidate():
    # BOTH candidates independently pass the gate (own positive promo+confirm, own
    # significant p, floor, windows) -> the cheaper one is chosen.
    c = gate.CellEvidence(
        "c", "generate", "opus",
        promo_deltas={"sonnet": _strong(20, 0.3), "haiku": _strong(20, 0.3)},
        confirm_deltas={"sonnet": _strong(20, 0.28), "haiku": _strong(20, 0.28)},
        confirm_windows={"sonnet": {"w1", "w2"}, "haiku": {"w1", "w2"}},
        cost={"sonnet": 0.01, "haiku": 0.002}, latency={"sonnet": 1.0, "haiku": 0.5},
    )
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is True
    assert v.chosen_model == "haiku"       # cheaper of two independently-passing winners


def test_cost_tiebreak_pvalue_is_for_the_chosen_model():
    # BUG (cross-validation#1): promotion used the winner's p while routing 'chosen'. The
    # verdict's p-value MUST be the chosen model's own confirmation p-value.
    c = gate.CellEvidence(
        "c", "generate", "opus",
        promo_deltas={"sonnet": _strong(20, 0.3), "haiku": _strong(20, 0.3)},
        confirm_deltas={"sonnet": _strong(20, 0.28), "haiku": _strong(20, 0.28)},
        confirm_windows={"sonnet": {"w1", "w2"}, "haiku": {"w1", "w2"}},
        cost={"sonnet": 0.01, "haiku": 0.002},
    )
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    # haiku is chosen; its own confirmation deltas are strongly positive -> small p.
    haiku_p = stats.paired_bootstrap_pvalue(_strong(20, 0.28), n_boot=2000, seed=12345,
                                            alternative="greater")
    assert v.chosen_model == "haiku"
    assert v.pvalue == pytest.approx(haiku_p)


def test_cost_tiebreak_does_not_pick_candidate_that_fails_gate_alone():
    # BUG (cross-validation#2): a cheaper candidate with a ZERO promotion mean (fails the
    # gate on its own) must NOT win the tiebreak. Only independently-promotable candidates
    # are eligible.
    c = gate.CellEvidence(
        "c", "generate", "opus",
        promo_deltas={"sonnet": [0.8] * 4 + [-0.2] * 4, "haiku": [0.0] * 8},
        confirm_deltas={"sonnet": _strong(8, 0.3), "haiku": _strong(8, 0.2)},
        confirm_windows={"sonnet": {"w1", "w2"}, "haiku": {"w1", "w2"}},
        cost={"sonnet": 0.05, "haiku": 0.001},
    )
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.chosen_model != "haiku"       # haiku's promo mean is 0 -> not eligible


def test_cost_tiebreak_does_not_pick_cheaper_but_credibly_worse():
    # A cheaper candidate that is credibly WORSE (fails its own significance) is never
    # chosen just for being cheaper.
    c = gate.CellEvidence(
        "c", "generate", "opus",
        promo_deltas={"sonnet": _strong(20, 0.5), "haiku": _strong(20, 0.05)},
        confirm_deltas={"sonnet": _strong(20, 0.48), "haiku": [0.03, -0.05] * 10},
        confirm_windows={"sonnet": {"w1", "w2"}, "haiku": {"w1", "w2"}},
        cost={"sonnet": 0.05, "haiku": 0.001},
    )
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is True
    assert v.chosen_model == "sonnet"      # haiku fails its own confirm significance


def test_cost_tiebreak_absent_costs_keeps_quality_winner():
    # With no cost data, the gate falls back to the pure paired-quality winner.
    c = gate.CellEvidence(
        "c", "generate", "opus",
        promo_deltas={"sonnet": _strong(20, 0.3), "haiku": _strong(20, 0.3)},
        confirm_deltas={"sonnet": _strong(20, 0.28), "haiku": _strong(20, 0.28)},
        confirm_windows={"sonnet": {"w1", "w2"}, "haiku": {"w1", "w2"}},
    )
    v = gate.evaluate_cell(c, k=8, m_windows=2)
    assert v.promotable is True
    assert v.chosen_model in ("sonnet", "haiku")   # deterministic, quality-first


def test_cost_tiebreak_invalid_cost_value_ignored_not_crash():
    # BUG (cross-validation#6): a non-numeric cost (None) raised TypeError in the sort key.
    # An invalid cost must be treated as unknown (sinks to most-expensive), never crash.
    c = gate.CellEvidence(
        "c", "generate", "opus",
        promo_deltas={"sonnet": _strong(20, 0.3), "haiku": _strong(20, 0.3)},
        confirm_deltas={"sonnet": _strong(20, 0.28), "haiku": _strong(20, 0.28)},
        confirm_windows={"sonnet": {"w1", "w2"}, "haiku": {"w1", "w2"}},
        cost={"sonnet": 0.01, "haiku": None},      # haiku cost invalid
    )
    v = gate.evaluate_cell(c, k=8, m_windows=2)    # must not raise
    assert v.chosen_model == "sonnet"              # valid cheaper cost wins over unknown
