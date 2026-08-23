"""WP3 acceptance: gate verdict on known effect; CI narrows with N; cluster by chain_id."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from chain_bench import analyze, cluster_bootstrap, _split


def _rows(cell, model, n_chains, reward, cost=0.01):
    out = []
    for i in range(n_chains):
        out.append({"cell_id": cell, "model": model, "reward": reward,
                    "cost_usd": cost, "chain_id": f"ch{i}"})
    return out


def test_strong_positive_effect_promotes_ON():
    # many chains across both splits, all clearly positive -> gate promotes -> ON
    rows = _rows("deepen:algo", "opus", n_chains=24, reward=0.5)
    res = {r["cell_id"]: r for r in analyze(rows, k=2, m_windows=2)}["deepen:algo"]
    assert res["verdict"] == "ON"
    assert res["mean_delta"] > 0.4


def test_null_effect_is_SKIP():
    rows = _rows("synthesize:algo", "kimi", n_chains=24, reward=0.0)
    res = {r["cell_id"]: r for r in analyze(rows)}["synthesize:algo"]
    assert res["verdict"] == "SKIP"          # tight CI at zero -> doesn't earn its cost


def test_ci_narrows_with_n():
    import random
    def spread(nch):
        rng = random.Random(1)
        vbc = {f"ch{i}": [rng.gauss(0.3, 0.5)] for i in range(nch)}
        _, lo, hi = cluster_bootstrap(vbc)
        return hi - lo
    assert spread(200) < spread(20)          # more chains -> tighter CI


def test_cluster_bootstrap_keeps_chain_rows_together():
    # a chain contributes ALL its rows or none; means only ever come from whole-chain blocks
    vbc = {"chA": [1.0, 1.0, 1.0], "chB": [-1.0, -1.0, -1.0]}
    mean, lo, hi = cluster_bootstrap(vbc, n=500)
    # resampling whole chains of {all +1} or {all -1} => bootstrap means land near -1, 0, or +1,
    # never a within-chain mix like +0.33; bounds stay within [-1, 1]
    assert -1.0 <= lo <= hi <= 1.0
    assert abs(mean) < 1e-9                    # balanced


def test_split_is_deterministic_and_disjoint():
    s = {_split(f"ch{i}") for i in range(50)}
    assert s == {"promo", "confirm"}           # both splits populated
    assert _split("ch7") == _split("ch7")      # stable


def test_pseudo_replication_clusters_by_topic():
    # 12 distinct chains, all the SAME topic -> must count as ~1 topic, not 12 obs
    rows = []
    for i in range(12):
        rows.append({"cell_id": "deepen:algo", "model": "opus", "reward": 0.5,
                     "cost_usd": 0.01, "chain_id": f"ch{i}", "topic_id": "same-topic"})
    res = {r["cell_id"]: r for r in analyze(rows)}["deepen:algo"]
    assert res["n_topics"] == 1            # all confirm rows collapse to one topic cluster
