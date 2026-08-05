"""§6 promotion gate — decide whether a candidate cell becomes an ACTIVE routing cell.

This is the single most load-bearing decision-metric in the adaptive-router: it is
what stops learned clusters from "routing on noise". The discipline (from
measurement-over-attribution) is built into the mechanism, not asserted after it:

  1. Sample floor      — a cell with < K paired outcomes per candidate is data-starved
                         and stays on the parent's safe default (2*K for judge-only).
  2. Out-of-sample     — the winner is chosen on the PROMOTION split ONLY, then must
                         RE-CONFIRM its advantage over the incumbent on a disjoint
                         HELD-OUT confirmation split (kills winner's/selection bias).
  3. FDR across cells  — significance is Benjamini-Hochberg across the whole family of
                         STRUCTURALLY-TESTED cells (every cell that reached the
                         confirmation test, incl. null ones), never a raw p-value.
  4. Replication       — the winner's confirmation evidence must span >= M distinct
                         capture windows (fresh data over time, not re-runs).
  - Healing            — a previously-promoted cell that no longer confirms un-promotes,
                         including one absent from the new input entirely.
  - Default            — anything short of ALL conditions routes to the parent incumbent.

KNOWN LIMIT (cross-validation#5): the single confirmation-split bootstrap p-value is not
perfectly calibrated under a heavily skewed null at small n (e.g. a zero-mean mixture
that lands all-positive with non-trivial probability). This is an inherent small-sample
bootstrap limitation, not fixable in the estimator. The gate does NOT rely on that one
p-value alone: FDR across the whole cell family AND replication across >= M distinct
capture windows are the layers that contain a single anti-conservative p. Cells with
small confirmation n should carry a larger K (raise the sample floor) where skew is a
concern.

Hardened after Codex adversarial cross-validation (the reference window): family membership no
longer depends on the confirmation outcome (F1); NaN/inf evidence is rejected (F2);
the winner is chosen on the promotion split without peeking at confirmation
availability (F3); the confirmation p-value imposes the null (F4, in stats.py); BH
decisions are mapped positionally so duplicate ids don't corrupt them (F5); absent
promoted cells heal (F6); per-candidate windows + case-insensitive provenance + k/m
validation (F7).

Two stages, independently testable:
  evaluate_cell(cell, k, m_windows) -> CellVerdict   (per-cell, pre-FDR)
  run_gate(cells, k, m_windows, alpha, previously_promoted) -> list[GateResult]
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import stats

# Bootstrap resamples for the confirmation p-value. Fixed seed for reproducibility.
_N_BOOT = 2000
_SEED = 12345


@dataclass(frozen=True)
class CellEvidence:
    """Per-cell evidence fed to the gate.

    promo_deltas / confirm_deltas map each candidate model -> the list of paired
    per-step deltas (candidate_score - incumbent_score) on the promotion split and
    the held-out confirmation split respectively.

    confirm_windows maps each candidate model -> the set of distinct capture-window
    ids ITS confirmation deltas were drawn from (per-candidate, so one candidate's
    windows can't satisfy another's replication requirement — cross-validation). For backward
    convenience a single set may be passed and is treated as shared across candidates.
    """
    cell_id: str
    parent_task_type: str
    incumbent_model: str
    promo_deltas: dict          # {model: [float, ...]}
    confirm_deltas: dict        # {model: [float, ...]}
    confirm_windows: object = field(default_factory=dict)   # {model: set} or a bare set
    provenance: str = "objective"   # "objective" | "judge" (judge => 2x floor)
    cost: dict = field(default_factory=dict)      # {model: real $ per step} (§5.4 tiebreak)
    latency: dict = field(default_factory=dict)   # {model: seconds} (§5.4 final tiebreak)

    def _candidate_models(self) -> set:
        return {m for m in self.confirm_deltas if m != self.incumbent_model}

    def windows_for(self, model: str) -> set:
        """Per-candidate confirmation windows for `model`, EXCLUDING blank/empty ids.

        An explicit {model: windows} dict is the safe form and is always honored. A
        bare collection is AMBIGUOUS about which candidate each window belongs to;
        sharing it across candidates would let a winner borrow another candidate's
        windows to fake replication (cross-validation#2). So a bare set is honored ONLY
        when the cell has a single candidate (nothing to borrow from); with multiple
        candidates it grants NO windows, forcing per-candidate provenance.

        A blank/empty window id ("" or whitespace) is NOT a real capture window and is
        dropped, so it can never count toward the replication requirement (cross-validation
        #3)."""
        cw = self.confirm_windows
        if isinstance(cw, dict):
            raw = cw.get(model, set())
        elif isinstance(cw, (set, frozenset, list, tuple)):
            raw = cw if len(self._candidate_models()) <= 1 else set()
        else:
            raw = set()
        return {w for w in raw if isinstance(w, str) and w.strip()}


@dataclass(frozen=True)
class CellVerdict:
    cell_id: str
    tested: bool                # reached the confirmation test (=> in the BH family)
    promotable: bool            # positive out-of-sample effect, pending FDR
    chosen_model: str           # candidate if promotable else incumbent
    candidate_tested: str | None
    pvalue: float               # confirmation-split one-sided p (1.0 if not tested)
    reason: str


@dataclass(frozen=True)
class GateResult:
    cell_id: str
    promoted: bool
    chosen_model: str
    pvalue: float
    parent_task_type: str
    healed: bool = False
    reason: str = ""


def _effective_floor(k: int, provenance) -> int:
    """Judge-only cells need a larger sample than objective cells (design §5.2).

    Fails CLOSED: match is case-insensitive, and any UNKNOWN provenance (typo, None,
    bytes, unexpected value) gets the stricter judge floor (2*k) — an invalid label
    must never silently weaken the gate (cross-validation#3). Only an explicit, recognized
    'objective' earns the lighter floor.
    """
    try:
        norm = str(provenance).strip().lower()
    except Exception:
        norm = ""
    if norm == "objective":
        return k
    # 'judge' AND anything unrecognized -> stricter floor (fail closed).
    return k * 2


def _mean(xs) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _all_finite(xs) -> bool:
    return all(isinstance(x, (int, float)) and math.isfinite(x) for x in xs)


def _mean_is_finite(xs) -> bool:
    """True iff the aggregate mean is finite — elementwise-finite values can still sum
    to +/-inf (e.g. 1e308 * many), which must not be treated as valid evidence."""
    if not xs:
        return False
    return math.isfinite(sum(xs) / len(xs))


def _independently_promotable(cell: CellEvidence, model: str, floor: int, m_windows: int):
    """Would `model` pass the FULL gate on its OWN evidence? Returns (ok, pvalue).

    This is the sound basis for the §5.4 cost tiebreak (cross-validation#1/#2/#3): a candidate
    is eligible to win on cost ONLY if it independently clears every promotion criterion —
    positive promotion mean, finite confirmation data at floor, positive confirmation
    mean, replication across >= m_windows — and we use ITS OWN confirmation p-value (never
    the quality winner's). We do NOT infer equivalence from a failure-to-reject-difference
    test (that fallacy, plus the absence of step-ids to verify pairing, is exactly what
    made the previous approach unsound), so a genuinely-tied cheaper model that can't
    independently pass is deferred, not fabricated.
    """
    pd = cell.promo_deltas.get(model)
    if pd is None or not _all_finite(pd) or len(pd) < floor or not _mean_is_finite(pd):
        return (False, 1.0)
    if _mean(pd) <= 0.0:
        return (False, 1.0)
    cd = cell.confirm_deltas.get(model)
    if cd is None or not _all_finite(cd) or len(cd) < floor or not _mean_is_finite(cd):
        return (False, 1.0)
    if _mean(cd) <= 0.0:
        return (False, 1.0)
    if len(cell.windows_for(model)) < m_windows:
        return (False, 1.0)
    pval = stats.paired_bootstrap_pvalue(cd, n_boot=_N_BOOT, seed=_SEED,
                                         alternative="greater")
    return (True, pval)


def _cost_key(cell: CellEvidence, model: str):
    """Sort key for the cost tiebreak: (cost, latency, name). A non-numeric/non-finite
    cost or latency sinks to the most-expensive end (treated as unknown) rather than
    raising (cross-validation#6)."""
    def num(d, m):
        v = d.get(m)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) \
            and math.isfinite(v) else math.inf
    return (num(cell.cost, model), num(cell.latency, model), str(model))


def evaluate_cell(cell: CellEvidence, *, k: int, m_windows: int) -> CellVerdict:
    """Per-cell verdict BEFORE cross-cell FDR.

    `tested` is True iff the cell reached the confirmation test (and therefore belongs
    in the BH family, whatever its outcome). `pvalue` is the confirmation-split
    one-sided bootstrap p-value for the promotion-split winner (1.0 when not tested).
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    if m_windows < 1:
        raise ValueError("m_windows must be >= 1")

    incumbent = cell.incumbent_model
    floor = _effective_floor(k, cell.provenance)

    def not_tested(reason, cand=None):
        return CellVerdict(cell.cell_id, False, False, incumbent, cand, 1.0, reason)

    # (2a) Choose the winner on the PROMOTION split ONLY. The candidate pool is those
    # whose PROMOTION data clears the floor and is finite — confirmation length/values
    # are NEVER consulted to pick the winner (cross-validation). Finiteness is required up
    # front so NaN/inf can't leak into any comparison (cross-validation).
    promo_eligible = [
        m for m in cell.promo_deltas
        if m != incumbent
        and _all_finite(cell.promo_deltas[m])
        and len(cell.promo_deltas[m]) >= floor
    ]
    if not promo_eligible:
        return not_tested(f"no candidate clears the promotion sample floor (K={floor})")

    winner = max(promo_eligible, key=lambda m: _mean(cell.promo_deltas[m]))
    if _mean(cell.promo_deltas[winner]) <= 0.0:
        return not_tested("no candidate beats incumbent on promotion split", winner)

    # From here the WINNER is fixed. If the winner lacks adequate confirmation data the
    # cell is simply not promotable — we do NOT fall back to a different candidate.
    # A non-finite AGGREGATE (finite elements whose sum/mean overflows, e.g. 1e308)
    # is also disqualifying (cross-validation#4): elementwise-finite is necessary but not
    # sufficient.
    cd = cell.confirm_deltas.get(winner)
    if cd is None or not _all_finite(cd) or len(cd) < floor:
        return not_tested(f"winner lacks finite confirmation data at floor (K={floor})", winner)
    if not _mean_is_finite(cd) or not _mean_is_finite(cell.promo_deltas[winner]):
        return not_tested("winner confirmation/promotion mean is non-finite (overflow)", winner)

    # (4) Replication — the WINNER's confirmation evidence must span >= M windows.
    if len(cell.windows_for(winner)) < m_windows:
        return not_tested(f"winner confirmation spans < {m_windows} capture windows", winner)

    # (2b) Out-of-sample test: the null-imposed one-sided bootstrap p-value that the
    # winner still beats the incumbent on the held-out confirmation split. Reaching
    # this point means the cell is TESTED and joins the BH family regardless of the
    # p-value or the sign of the confirmation mean (cross-validation — family membership must
    # not depend on the confirmation outcome).
    winner_pval = stats.paired_bootstrap_pvalue(cd, n_boot=_N_BOOT, seed=_SEED,
                                                 alternative="greater")
    if _mean(cd) <= 0.0:
        # Nonpositive out-of-sample mean cannot be a real flip; stay in the family but
        # force p to the null so BH can never reject it.
        return CellVerdict(cell.cell_id, True, False, incumbent, winner, 1.0,
                           "winner's advantage evaporates out-of-sample")

    # §5.4 cost tiebreak — SOUND version (cross-validation#1/#2/#3). With no cost data, route
    # the pure paired-quality winner. With cost data, choose the CHEAPEST candidate that
    # INDEPENDENTLY passes the whole gate on its own evidence, and report ITS OWN p-value
    # for FDR — so the promotion decision and the routed model are always the same model.
    # A candidate that couldn't be promoted alone (fails its own significance) is never
    # eligible; the harder "statistically-tied cheaper model" win is deferred, not faked.
    if not cell.cost:
        return CellVerdict(cell.cell_id, True, True, winner, winner, winner_pval,
                           "tested out-of-sample (pending FDR)")

    eligible = {winner: winner_pval}
    for m in promo_eligible:
        if m == winner:
            continue
        ok, mp = _independently_promotable(cell, m, floor, m_windows)
        if ok:
            eligible[m] = mp
    chosen = min(eligible, key=lambda m: _cost_key(cell, m))
    chosen_pval = eligible[chosen]
    reason = ("tested out-of-sample (pending FDR)" if chosen == winner
              else f"cost tiebreak: {chosen} independently passes and is cheaper than {winner}")
    return CellVerdict(cell.cell_id, True, True, chosen, chosen, chosen_pval, reason)


def run_gate(cells, *, k: int, m_windows: int, alpha: float = 0.05,
             previously_promoted: dict | None = None) -> list:
    """Evaluate every cell, apply Benjamini-Hochberg FDR across the family of cells
    that reached the confirmation test, and emit final promotion decisions.

    `previously_promoted` maps cell_id -> the model it was promoted to before. A cell
    in that map that does not re-promote (including one absent from `cells`) is
    reported with healed=True so an incremental consumer tears down the stale route.
    """
    previously_promoted = dict(previously_promoted or {})
    verdicts = [evaluate_cell(c, k=k, m_windows=m_windows) for c in cells]

    # The BH family is every cell that reached the confirmation test — including
    # confirmation-null cells (p forced to 1.0) — so membership is independent of the
    # confirmation outcome (cross-validation). Decisions are aligned POSITIONALLY, not by
    # cell_id, so duplicate ids can't overwrite each other (cross-validation).
    family_idx = [i for i, v in enumerate(verdicts) if v.tested]
    fam_pvals = [verdicts[i].pvalue for i in family_idx]
    fam_reject = stats.benjamini_hochberg(fam_pvals, alpha=alpha) if fam_pvals else []
    promoted_by_pos = {family_idx[j]: fam_reject[j] for j in range(len(family_idx))}

    incumbent_by_pos = [c.incumbent_model for c in cells]
    parent_by_pos = [c.parent_task_type for c in cells]

    # First pass: decide promotion per row. A cell is promoted only if it is genuinely
    # promotable AND survives FDR (the promotable guard stops a confirmation-null family
    # member from ever promoting even under a degenerate BH pass).
    promoted_flags = [bool(promoted_by_pos.get(i, False)) and verdicts[i].promotable
                      for i in range(len(verdicts))]
    # An id counts as promoted this run if ANY of its (possibly duplicate) rows promoted;
    # such an id must NOT also heal — that would tear down its own re-promotion
    # (cross-validation#1).
    promoted_ids = {verdicts[i].cell_id for i in range(len(verdicts)) if promoted_flags[i]}

    results = []
    seen_ids = set()
    for i, v in enumerate(verdicts):
        promoted = promoted_flags[i]
        was_promoted = v.cell_id in previously_promoted
        seen_ids.add(v.cell_id)
        chosen = v.chosen_model if promoted else incumbent_by_pos[i]
        healed = was_promoted and not promoted and v.cell_id not in promoted_ids
        if promoted:
            reason = "promoted: out-of-sample winner surviving FDR"
        elif healed:
            reason = "un-promoted: no longer confirms (healed to parent default)"
        else:
            reason = v.reason
        results.append(GateResult(
            cell_id=v.cell_id, promoted=promoted, chosen_model=chosen,
            pvalue=v.pvalue, parent_task_type=parent_by_pos[i],
            healed=healed, reason=reason,
        ))

    # (F6) A previously-promoted cell ABSENT from the new input must still heal, so the
    # consumer removes its now-unsupported route. We don't know its parent/incumbent
    # here, so report the safe intent: not promoted, healed, route dropped.
    for cid in previously_promoted:
        if cid not in seen_ids:
            results.append(GateResult(
                cell_id=cid, promoted=False, chosen_model="", pvalue=1.0,
                parent_task_type="", healed=True,
                reason="un-promoted: cell absent from new evidence (healed to parent default)",
            ))

    return results
