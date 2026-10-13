"""core.verdict — the one decision schema (retry A/B, panel studies)."""
import json
import random

import pytest

from apex_router import gate, retry_ab
from apex_router.core import stats, verdict as V


def test_classify_boundaries():
    assert V.classify(0.01, 0.2) == V.BETTER
    assert V.classify(-0.2, -0.01) == V.WORSE
    assert V.classify(-0.2, 0.2) == V.NO_DIFF
    assert V.classify(-0.005, 0.008, margin=0.01) == V.EQUIVALENT
    assert V.classify(-0.02, 0.008, margin=0.01) == V.NO_DIFF       # wider than the margin
    assert V.classify(0.0, 0.2) == V.NO_DIFF                          # touching 0 is not exclusion
    # equivalence is checked before direction when a margin is declared (K3's pre-declared order)
    assert V.classify(0.0099, 0.0099, margin=0.01) == V.EQUIVALENT
    assert V.classify(-0.008, -0.002, margin=0.01) == V.EQUIVALENT
    assert V.classify(0.002, 0.011, margin=0.01) == V.BETTER
    assert V.classify(0.1, 0.2, enough=False) == V.INCONCLUSIVE
    assert V.classify(None, None) == V.INCONCLUSIVE
    assert V.classify(float("nan"), 0.1) == V.INCONCLUSIVE


def test_paired_contrast_orientation_and_floor():
    rng = random.Random(3)
    better = [0.05 + rng.gauss(0, 0.02) for _ in range(60)]
    c = V.paired_contrast("x", "a", "b", better, metric="brier_gain")
    assert c.status == V.BETTER and c.estimate > 0 and c.p < 0.01 and c.n == 60
    w = V.paired_contrast("x", "a", "b", [-x for x in better], metric="brier_gain")
    assert w.status == V.WORSE and w.estimate < 0
    small = V.paired_contrast("x", "a", "b", better[:10], metric="brier_gain", min_n=30)
    assert small.status == V.INCONCLUSIVE and small.ci[0] is not None   # interval kept, not interpreted
    empty = V.paired_contrast("x", "a", "b", [], metric="brier_gain")
    assert empty.status == V.INCONCLUSIVE and empty.estimate is None


def test_apply_fdr_aligned_and_skips_interval_only():
    rng = random.Random(5)
    strong = V.paired_contrast("s", "a", "b", [0.05 + rng.gauss(0, 0.01) for _ in range(50)], metric="m")
    null = V.paired_contrast("n", "a", "b", [rng.gauss(0, 0.05) for _ in range(50)], metric="m")
    prop = V.proportion_contrast("p", "a", "b", 40, 50, 20, 50, metric="m")
    V.apply_fdr([null, prop, strong])
    assert strong.fdr_reject is True and null.fdr_reject is False and prop.fdr_reject is None
    expect = stats.benjamini_hochberg([null.p, strong.p])
    assert [null.fdr_reject, strong.fdr_reject] == expect


def _old_retry_verdict(n1, n0, lo, hi, min_n):
    """The pre-refactor retry_ab._verdict, verbatim, as the oracle for the before/after matrix."""
    if n1 < min_n or n0 < min_n:
        return "INCONCLUSIVE"
    if lo is not None and lo > 0:
        return "better"
    if hi is not None and hi < 0:
        return "worse"
    return "no detectable difference"


def test_proportion_contrast_matches_pre_refactor_retry_verdict():
    rng = random.Random(11)
    for _ in range(3000):
        n1, n0 = rng.randint(0, 120), rng.randint(0, 120)
        k1, k0 = rng.randint(0, n1), rng.randint(0, n0)
        min_n = rng.choice([1, 10, 30])
        c = V.proportion_contrast("r", "wait", "immediate", k1, n1, k0, n0, metric="p", min_n=min_n)
        d, lo, hi = stats.newcombe_diff(k1, n1, k0, n0)
        assert (c.estimate, c.ci) == (d, (lo, hi))
        assert c.status == _old_retry_verdict(n1, n0, lo, hi, min_n), (k1, n1, k0, n0, min_n)


def test_newcombe_reexported_from_retry_ab():
    assert retry_ab.newcombe_diff is stats.newcombe_diff


def test_decide_rules():
    assert V.decide("inc", kills={"K2": True, "K3": "undecided"}).startswith("stay on inc (kill: K2")
    g = [{"cell_id": "c", "promoted": True, "chosen_model": "x"}]
    assert V.decide("inc", gate=g) == "promote x (c)"
    assert V.decide("inc", gate=[{"cell_id": "c", "promoted": False}]) == "stay on inc (gate: no promotion)"
    b = V.Contrast("n", "a", "inc", "m", 0.1, (0.05, 0.15), 40, V.BETTER)
    assert V.decide("inc", primary=b) == "adopt a over inc"
    assert V.decide("inc", primary=None) == V.INCONCLUSIVE


def test_gate_rows_and_json_roundtrip():
    rng = random.Random(2)
    cell = gate.CellEvidence(
        cell_id="t", parent_task_type="p", incumbent_model="inc",
        promo_deltas={"cand": [0.05 + rng.gauss(0, 0.01) for _ in range(40)]},
        confirm_deltas={"cand": [0.05 + rng.gauss(0, 0.01) for _ in range(40)]},
        confirm_windows={"cand": {"w0", "w1"}}, provenance="objective")
    rows = V.gate_rows([cell], k=30, m_windows=2)
    assert rows[0]["promoted"] and rows[0]["chosen_model"] == "cand"
    v = V.Verdict(question="q", incumbent="inc", decision=V.decide("inc", gate=rows), arms={"inc": {}},
                  contrasts=[V.paired_contrast("c", "cand", "inc", cell.confirm_deltas["cand"], metric="m")],
                  gate=rows)
    d = json.loads(v.to_json())
    assert d["schema"] == V.SCHEMA and d["decision"] == "promote cand (t)"
    assert d["contrasts"][0]["status"] == V.BETTER and v.contrast("c") is v.contrasts[0]


@pytest.mark.parametrize("arms_n", [(40, 40), (5, 40)])
def test_retry_report_carries_card_consistent_with_verdict(tmp_path, arms_n):
    rows = []
    for arm, n, rec in (("immediate", arms_n[0], 0.5), ("wait", arms_n[1], 0.9)):
        for i in range(n):
            r = {"ts": 1.79e9 + i, "retry_arm": arm, "retry_policy": "ab", "retry_propensity": 0.5,
                 "connect_retries": 1, "is_error": False}
            if i >= rec * n:
                r["error_cause"] = "ConnectError"
            rows.append(r)
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    rep = retry_ab.report(p)
    card = rep["card"]
    assert card["schema"] == V.SCHEMA and card["incumbent"] == "immediate"
    assert card["contrasts"][0]["status"] == rep["verdict"] == rep["comparisons"][0]["recovered"]["verdict"]
    if arms_n == (5, 40):
        assert rep["verdict"] == V.INCONCLUSIVE and card["decision"] == V.INCONCLUSIVE
