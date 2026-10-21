"""Stratified reservoir: exact counters, uniform per-stratum samples, weights, merge, design tools.
Statistical tests use fixed seeds, so they are deterministic."""
from __future__ import annotations

import json
import math
import random

import pytest

from apex_router.core import reservoir as R
from apex_router.core.stats import wilson_ci

# ---- validation --------------------------------------------------------------------------------

def test_key_and_row_validation():
    assert R.key("explore", "haiku", "ok") == "explore|haiku|ok"
    for bad in [("/etc", "x"), ("~", "y"), ("a\nb",), (R.OTHER,)]:
        with pytest.raises(ValueError):
            R.key(*bad)
    r = R.StratifiedReservoir()
    for row in [{"p": "/Users/x"}, {"p": "~/x"}, {"p": "a" * 65}, {"p": "a\nb"},
                {"p": "etc/passwd"}, {"p": "../secret"}, {"p": "user/file"}, {"p": "https://x/y"},
                {"p": "a b"}, {"p": "C:\\x"},
                {"p": float("nan")}, {"p": {"nested": 1}}, {"p": [1]}, {"bad key/": 1}]:
        with pytest.raises((ValueError, TypeError)):
            r.add("s", row)
    assert r.population() == {}                       # a rejected row is not counted


# ---- exact counters ----------------------------------------------------------------------------

def test_counters_are_exact_and_not_sampled():
    rng = random.Random(1)
    r = R.StratifiedReservoir(cap=5)
    data = {"a": [rng.random() for _ in range(300)], "b": [10 + rng.random() for _ in range(7)]}
    for h, xs in data.items():
        for i, x in enumerate(xs):
            r.add(h, {"cost": x, "ok": i % 3 == 0, "tier": "haiku" if i % 2 else "opus"})
    assert r.population() == {"a": 300, "b": 7}
    for h, xs in data.items():
        c = r.counters(h)
        m = sum(xs) / len(xs)
        assert abs(c["num"]["cost"]["mean"] - m) < 1e-12
        assert abs(c["num"]["cost"]["m2"] - sum((x - m) ** 2 for x in xs)) < 1e-9
        assert c["enum"]["ok"]["true"] == sum(1 for i in range(len(xs)) if i % 3 == 0)
        assert sum(c["enum"]["tier"].values()) == len(xs)
    allx = data["a"] + data["b"]
    assert abs(r.total_mean("cost") - sum(allx) / len(allx)) < 1e-12
    assert all(len(st["rows"]) <= 5 for st in r.strata.values())


def test_max_strata_folds_new_keys_into_other_with_exact_counts():
    r = R.StratifiedReservoir(cap=3, max_strata=2)
    for i in range(10):
        r.add(f"s{i % 4}", {"x": i})
    pop = r.population()
    assert set(pop) == {"s0", "s1", R.OTHER}
    assert sum(pop.values()) == 10 and pop[R.OTHER] == 4      # s2: i=2,6; s3: i=3,7


# ---- uniform sample (Algorithm R) --------------------------------------------------------------

def test_reservoir_inclusion_is_uniform():
    n_items, cap, reps = 200, 10, 3000
    hits = [0] * n_items
    for s in range(reps):
        r = R.StratifiedReservoir(cap=cap, seed=s)
        for i in range(n_items):
            r.add("s", {"i": i})
        for row in r.strata["s"]["rows"]:
            hits[row["i"]] += 1
    expect = reps * cap / n_items                     # 150 per item
    deciles = [sum(hits[d * 20:(d + 1) * 20]) for d in range(10)]
    chi2 = sum((o - expect * 20) ** 2 / (expect * 20) for o in deciles)
    assert chi2 < 21.67                               # chi2(9) at p = 0.01
    assert abs(hits[0] - expect) < 5 * math.sqrt(expect) and abs(hits[-1] - expect) < 5 * math.sqrt(expect)


# ---- snapshot, weights, estimates -------------------------------------------------------------

def _two_strata(seed=0, n_a=9000, n_b=100):
    """A: large, value ~ 1.0; B: rare, value ~ 10.0. True mean = (n_a*1 + n_b*10) / N."""
    rng = random.Random(seed)
    r = R.StratifiedReservoir(cap=200, seed=seed)
    for i in range(n_a + n_b):
        h = "b" if rng.random() < n_b / (n_a + n_b) else "a"
        r.add(h, {"v": (10.0 if h == "b" else 1.0) + rng.gauss(0, 0.5), "rare": h == "b"})
    return r


def test_snapshot_weights_sum_to_population_and_keep_the_rare_stratum():
    r = _two_strata()
    pop = r.population()
    snap = r.snapshot(budget=60, floor=30)
    assert snap["n"]["b"] == 30 and snap["n"]["a"] == 30            # floor keeps the rare case
    for h in pop:
        assert abs(sum(w for g, w, _ in snap["rows"] if g == h) - pop[h]) < 1e-9
    est = R.estimate(snap, lambda row: row["rare"])
    assert abs(est["mean"] - pop["b"] / sum(pop.values())) < 1e-12  # constant per stratum: exact
    unweighted = sum(row["rare"] for _, _, row in snap["rows"]) / len(snap["rows"])
    assert abs(unweighted - 0.5) < 1e-12                             # the distortion we avoid
    before = json.dumps(r.to_dict(), sort_keys=True)
    r.snapshot(budget=10, floor=5)
    assert json.dumps(r.to_dict(), sort_keys=True) == before         # snapshot does not mutate


def test_weighted_estimate_is_unbiased_and_ci_covers():
    est, cover = [], 0
    for s in range(200):
        r = _two_strata(seed=s, n_a=3000, n_b=60)
        truth = r.total_mean("v")                                     # exact, from counters
        e = R.estimate(r.snapshot(budget=40, floor=20, seed=s), lambda row: row["v"])
        est.append(e["mean"] - truth)
        cover += abs(e["mean"] - truth) <= 1.96 * e["se"]
        assert e["covered"] == 1.0 and e["n_eff"] < e["n"]
    bias = sum(est) / len(est)
    assert abs(bias) < 0.02                                           # naive mean would be ~+4.4
    assert 0.90 <= cover / 200 <= 0.99


def test_estimate_reports_partial_coverage():
    r = R.StratifiedReservoir()
    for i in range(10):
        r.add("a", {"x": i})
        r.add("b", {"y": i})
    e = R.estimate(r.snapshot(), lambda row: row.get("x"))
    assert e["covered"] == 0.5 and abs(e["mean"] - 4.5) < 1e-12


# ---- allocation and design tools --------------------------------------------------------------

def test_allocate_floor_proportional_neyman_and_bounds():
    pop = {"a": 900, "b": 90, "c": 10}
    t = R.allocate(pop, 100)
    assert t == {"a": 90, "b": 9, "c": 1} and sum(t.values()) == 100
    t = R.allocate(pop, 100, floor=10)
    assert t["c"] == 10 and t["b"] >= 10 and sum(t.values()) == 100
    t = R.allocate(pop, 100, method="neyman", sd={"a": 0.1, "b": 1.0, "c": 1.0})
    assert t["b"] > 9 and sum(t.values()) == 100                    # high-variance stratum gains
    assert R.allocate(pop, 5000) == pop                              # budget above N: take all
    assert R.allocate(pop, 100, upper={"a": 20})["a"] == 20         # reservoir size bounds n_h
    with pytest.raises(ValueError):
        R.allocate(pop, 20, floor=10)                                # floors need 30 > 20
    t = R.allocate(pop, 100, method="neyman", sd={"a": -1, "b": float("nan")})
    assert sum(t.values()) == 100


def test_design_effect_matches_kish_and_step1_numbers():
    pop, take = {"a": 900, "b": 100}, {"a": 40, "b": 40}
    deff = R.design_effect(pop, take)
    w = [900 / 40] * 40 + [100 / 40] * 40
    assert abs(deff - 80 / R.n_eff(w)) < 1e-12 and abs(deff - 1.64) < 1e-12
    assert abs(R.design_effect(pop, R.allocate(pop, 100)) - 1.0) < 1e-12


def test_rows_needed():
    assert R.rows_needed(0.85, 0.85) == math.inf
    n = R.rows_needed(0.93, 0.85)
    lo = wilson_ci(0.93 * n, n)[0]
    assert abs(lo - 0.85) < 1e-6 and 70 < n < 80
    assert abs(R.rows_needed(0.93, 0.85, deff=2.52) - 2.52 * n) < 1e-6
    with pytest.raises(ValueError):
        R.rows_needed(1.2, 0.85)


# ---- merge and persistence --------------------------------------------------------------------

def test_merge_counters_exact_and_sample_proportional_to_population():
    from_a = 0
    reps = 400
    for s in range(reps):
        a, b = R.StratifiedReservoir(cap=20, seed=s), R.StratifiedReservoir(cap=20, seed=s + 10_000)
        for i in range(900):
            a.add("x", {"node": "a", "v": i})
        for i in range(100):
            b.add("x", {"node": "b", "v": 1000 + i})
        m = a.merge(b)
        assert m.population() == {"x": 1000}
        assert len(m.strata["x"]["rows"]) == 20
        from_a += sum(row["node"] == "a" for row in m.strata["x"]["rows"])
    share = from_a / (20 * reps)
    assert abs(share - 0.9) < 0.01                                   # Hypergeometric(1000, 900, 20)
    single = R.StratifiedReservoir(cap=20)
    for i in range(900):
        single.add("x", {"v": i})
    for i in range(100):
        single.add("x", {"v": 1000 + i})
    mv, sv = m.counters("x")["num"]["v"], single.counters("x")["num"]["v"]
    assert mv["n"] == sv["n"] and abs(mv["mean"] - sv["mean"]) < 1e-9
    assert abs(mv["m2"] - sv["m2"]) / sv["m2"] < 1e-12


def test_merge_disjoint_strata_and_other_fold():
    a, b = R.StratifiedReservoir(cap=5, max_strata=2), R.StratifiedReservoir(cap=5, max_strata=2)
    for i in range(10):
        a.add("p", {"i": i})
        b.add("q", {"i": i})
        b.add("r", {"i": i})
    m = a.merge(b)
    assert sum(m.population().values()) == 30 and len([h for h in m.strata if h != R.OTHER]) <= 2


def test_round_trip_resumes_the_same_stream():
    a = R.StratifiedReservoir(cap=4, seed=3)
    b = R.StratifiedReservoir(cap=4, seed=3)
    for i in range(50):
        a.add("s", {"i": i})
        b.add("s", {"i": i})
    b = R.StratifiedReservoir.from_dict(json.loads(json.dumps(b.to_dict())))
    for i in range(50, 120):
        a.add("s", {"i": i})
        b.add("s", {"i": i})
    assert a.to_dict() == b.to_dict()
    with pytest.raises(ValueError):
        R.StratifiedReservoir.from_dict({"v": 2})


# ---- regressions from xval pass 1 ---------------------------------------------------------------

def test_snapshot_subsample_is_independent_of_the_reservoir_stream():
    # xval repro: cap 2, 3 items, budget 1 picked item 2 in 66 % of seeds (same seed reused)
    from collections import Counter
    for cap, budget in ((2, 1), (5, 2)):
        hits, reps, items = Counter(), 6000, cap + 1
        for s in range(reps):
            r = R.StratifiedReservoir(cap=cap, seed=s)
            for i in range(items):
                r.add("s", {"i": i})
            hits.update(row["i"] for _, _, row in r.snapshot(budget=budget)["rows"])
        expect = reps * budget / items
        for i in range(items):
            assert abs(hits[i] - expect) < 5 * math.sqrt(expect), (cap, budget, dict(hits))


def test_merge_inclusion_is_uniform_when_caps_differ():
    from collections import Counter
    hits, reps = Counter(), 6000
    for s in range(reps):
        a, b = R.StratifiedReservoir(cap=2, seed=s), R.StratifiedReservoir(cap=1, seed=s + 50_000)
        a.add("x", {"i": 0})
        a.add("x", {"i": 1})
        b.add("x", {"i": 2})
        hits.update(row["i"] for row in a.merge(b).strata["x"]["rows"])
    for i in range(3):
        assert abs(hits[i] - reps / 3) < 5 * math.sqrt(reps / 3), dict(hits)


def test_singleton_stratum_has_no_standard_error():
    r = R.StratifiedReservoir(cap=1)
    for x in (0, 10):
        r.add("s", {"x": x})
    assert R.estimate(r.snapshot(), lambda row: row["x"])["se"] is None
    r = R.StratifiedReservoir(cap=1)
    r.add("s", {"x": 3})                                             # N_h = 1: census, se = 0
    assert R.estimate(r.snapshot(), lambda row: row["x"])["se"] == 0.0


def test_allocate_rejects_bad_numbers_and_survives_huge_sd():
    for kw in ({"budget": -1}, {"budget": 2.5}, {"budget": float("inf")}, {"budget": True},
               {"budget": 5, "floor": -1}, {"budget": 5, "upper": {"a": -1}}):
        with pytest.raises(ValueError):
            R.allocate({"a": 10}, **kw)
    t = R.allocate({"a": 10, "b": 10}, 5, method="neyman", sd={"a": 1e308, "b": 1e300})
    assert sum(t.values()) == 5 and t["a"] == 5
    assert R.allocate({}, 3) == {}
    big = 2 ** 53 + 3                                                # xval pass 3: float precision
    with pytest.raises(ValueError):
        R.allocate({"a": big}, big, method="neyman", sd={"a": 1.0})


# ---- regressions from xval pass 2 ---------------------------------------------------------------

def test_neyman_fallback_sd_uses_the_callers_units():
    # a missing S_h is 0.5 in the caller's units: same result as passing 0.5 explicitly
    for sd_a in (10.0, 0.1):
        pop = {"a": 100, "b": 100}
        assert (R.allocate(pop, 100, method="neyman", sd={"a": sd_a})
                == R.allocate(pop, 100, method="neyman", sd={"a": sd_a, "b": 0.5}))
    # Neyman shares do not change under a common scale of S_h
    pop, sd = {"a": 300, "b": 100, "c": 50}, {"a": 1.0, "b": 3.0, "c": 2.0}
    assert (R.allocate(pop, 90, method="neyman", sd=sd)
            == R.allocate(pop, 90, method="neyman", sd={h: v * 1e-200 for h, v in sd.items()}))


def test_neyman_spends_the_whole_budget_when_scores_underflow():
    pop = {"a": 100, "b": 100}
    t = R.allocate(pop, 100, method="neyman", sd={"a": 10, "unused": 1e308})
    assert sum(t.values()) == 100 and t["a"] > t["b"] > 0          # unused key sets no scale
    t = R.allocate({"a": 10, "b": 10}, 10, method="neyman", sd={"a": 1e308, "b": 1e-308},
                   upper={"a": 1})
    assert t == {"a": 1, "b": 9}
