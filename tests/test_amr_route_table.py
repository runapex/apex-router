"""Tests for amr.route_table — the §7 per-venue route-table emitter + reader.

The table is the ONLY artifact the consumers read. It is regenerated from gate results
+ per-cell rankings. The chosen_model per cell follows the §5.4 LEXICOGRAPHIC objective
(not a ratio): quality first (candidates whose quality CI is credibly >= incumbent),
then real $ cost, then latency. Unpromoted / uncertain cells route to the parent's
safe/heavy default (CANNOT-DECIDE).
"""
import json

import pytest

from apex_router import route_table as rt


# --------------------------------------------------------------------------- #
# build_ranking — per-cell ranking rows (quality + Wilson CI, cost, n)
# --------------------------------------------------------------------------- #
def test_build_ranking_computes_pass_rate_and_ci():
    # model stats: {model: (passes, n, cost_usd, latency)}
    stats = {"opus": (9, 10, 0.05, 2.0), "sonnet": (6, 10, 0.01, 1.0)}
    ranking = rt.build_ranking(stats)
    by_model = {r["model"]: r for r in ranking}
    assert by_model["opus"]["quality"] == pytest.approx(0.9)
    assert by_model["opus"]["n"] == 10
    lo, hi = by_model["opus"]["quality_ci"]
    assert 0.0 <= lo <= 0.9 <= hi <= 1.0
    assert by_model["opus"]["cost_usd"] == 0.05


def test_build_ranking_sorted_by_quality_desc():
    stats = {"a": (5, 10, 0.01, 1.0), "b": (9, 10, 0.01, 1.0), "c": (7, 10, 0.01, 1.0)}
    ranking = rt.build_ranking(stats)
    assert [r["model"] for r in ranking] == ["b", "c", "a"]


# The §5.4 cost tiebreak now lives in the GATE (it owns quality+cost, using paired data).
# The route-table just EMITS the gate's decision — no re-derivation from marginal CIs.
def _rank(model, q, lo, hi, cost, lat, n=30):
    return {"model": model, "quality": q, "quality_ci": (lo, hi),
            "cost_usd": cost, "latency": lat, "provenance": "objective", "n": n}


# --------------------------------------------------------------------------- #
# emit_route_table + read_route — assembly and consumer read
# --------------------------------------------------------------------------- #
def _gate_result(cell_id, promoted, chosen, parent="generate", healed=False):
    from apex_router import gate
    return gate.GateResult(cell_id=cell_id, promoted=promoted, chosen_model=chosen,
                           pvalue=0.001 if promoted else 1.0, parent_task_type=parent,
                           healed=healed, reason="")


def test_emit_route_table_shape(tmp_path):
    results = [_gate_result("c1", True, "sonnet")]
    rankings = {"c1": [_rank("sonnet", 0.9, 0.8, 0.95, 0.01, 1.0),
                       _rank("opus", 0.6, 0.5, 0.7, 0.05, 2.0)]}
    p = tmp_path / "route_table.proxy.json"
    table = rt.emit_route_table(results, rankings, venue="proxy",
                                generated_from={"bench_run_id": "r1", "corpus_snapshot": "snap1"},
                                path=p)
    assert table["venue"] == "proxy"
    assert "schema_version" in table
    cell = table["cells"][0]
    assert cell["cell_id"] == "c1"
    assert cell["promoted"] is True
    assert cell["chosen_model"] == "sonnet"
    assert cell["fallback_model"] == "generate"      # parent task-type default
    # persisted and reloadable
    on_disk = json.loads(p.read_text())
    assert on_disk["cells"][0]["chosen_model"] == "sonnet"


def test_emit_unpromoted_cell_routes_to_parent_default():
    results = [_gate_result("c2", False, "opus", parent="debug")]
    rankings = {"c2": [_rank("opus", 0.5, 0.4, 0.6, 0.05, 2.0)]}
    table = rt.emit_route_table(results, rankings, venue="skill",
                                generated_from={"bench_run_id": "r1"}, path=None)
    cell = table["cells"][0]
    assert cell["promoted"] is False
    assert cell["chosen_model"] == "debug"           # CANNOT-DECIDE -> parent safe default


def test_read_route_returns_chosen_for_promoted_cell(tmp_path):
    results = [_gate_result("c1", True, "sonnet")]
    rankings = {"c1": [_rank("sonnet", 0.9, 0.8, 0.95, 0.01, 1.0)]}
    p = tmp_path / "route_table.proxy.json"
    rt.emit_route_table(results, rankings, venue="proxy",
                        generated_from={"bench_run_id": "r1"}, path=p)
    assert rt.read_route(p, cell_id="c1", parent_task_type="generate") == "sonnet"


def test_read_route_unknown_cell_falls_back_to_parent_default(tmp_path):
    results = [_gate_result("c1", True, "sonnet")]
    rankings = {"c1": [_rank("sonnet", 0.9, 0.8, 0.95, 0.01, 1.0)]}
    p = tmp_path / "route_table.proxy.json"
    rt.emit_route_table(results, rankings, venue="proxy",
                        generated_from={"bench_run_id": "r1"}, path=p)
    # a cell not in the table -> the caller's parent task-type default (CANNOT-DECIDE)
    assert rt.read_route(p, cell_id="unknown", parent_task_type="debug") == "debug"


def test_read_route_missing_table_falls_back(tmp_path):
    # No table on disk yet -> read returns the parent default (superset-of-defaults).
    assert rt.read_route(tmp_path / "nope.json", cell_id="c1",
                         parent_task_type="review") == "review"


def test_healed_cell_routes_to_parent_default():
    results = [_gate_result("c3", False, "opus", parent="refactor", healed=True)]
    rankings = {"c3": [_rank("opus", 0.5, 0.4, 0.6, 0.05, 2.0)]}
    table = rt.emit_route_table(results, rankings, venue="proxy",
                                generated_from={"bench_run_id": "r1"}, path=None)
    assert table["cells"][0]["chosen_model"] == "refactor"
    assert table["cells"][0]["healed"] is True


# --------------------------------------------------------------------------- #
# Regression — confirmed by Codex cross-validation (2026-07-31)
# --------------------------------------------------------------------------- #
def test_rt5_promoted_chosen_must_be_in_ranking():
    # BUG (Codex #5): a promoted cell whose gate chosen_model is NOT in the ranking
    # emitted an unsupported route. The emitter must fall back to the parent default
    # rather than emit a model with no supporting evidence.
    results = [_gate_result("c1", True, "not-ranked", parent="generate")]
    rankings = {"c1": [_rank("sonnet", 0.9, 0.8, 0.95, 0.01, 1.0)]}
    table = rt.emit_route_table(results, rankings, venue="proxy",
                                generated_from={"bench_run_id": "r1"}, path=None)
    cell = table["cells"][0]
    assert cell["chosen_model"] == "generate"      # unsupported -> parent default
    assert cell["promoted"] is False               # demoted to safe route


def test_rt5_empty_ranking_promoted_falls_back():
    results = [_gate_result("c1", True, "sonnet", parent="generate")]
    table = rt.emit_route_table(results, {"c1": []}, venue="proxy",
                                generated_from={"bench_run_id": "r1"}, path=None)
    assert table["cells"][0]["chosen_model"] == "generate"


def test_rt5_empty_parent_on_non_healed_row_is_rejected():
    # BUG (Codex #5): an empty parent yields an unroutable fallback. A NON-healed row with
    # an empty parent is a genuine error and must be rejected at emit time. (An absent-cell
    # HEAL row legitimately has an empty parent and is tolerated — see rt_p2f4.)
    results = [_gate_result("c1", False, "", parent="", healed=False)]
    with pytest.raises(ValueError):
        rt.emit_route_table(results, {}, venue="proxy",
                            generated_from={"bench_run_id": "r1"}, path=None)


def test_rt6_read_route_rejects_non_bool_promoted(tmp_path):
    # BUG (Codex #6): "promoted": "false" is truthy and wrongly routed its chosen model.
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"schema_version": 1, "venue": "proxy", "cells": [
        {"cell_id": "c1", "promoted": "false", "chosen_model": "sonnet",
         "fallback_model": "generate"}]}))
    assert rt.read_route(p, cell_id="c1", parent_task_type="generate") == "generate"


def test_rt7_malformed_table_shapes_fall_back_to_default(tmp_path):
    # BUG (Codex #7): valid JSON with the wrong shape ([] / {"cells": null} / non-object
    # cells) raised instead of falling back. All must return the parent default.
    for payload in ("[]", '{"cells": null}', '{"cells": [42]}', '"a string"', "null"):
        p = tmp_path / "t.json"
        p.write_text(payload)
        assert rt.read_route(p, cell_id="c1", parent_task_type="debug") == "debug"


def test_rt6_duplicate_cell_id_prefers_safe(tmp_path):
    # Duplicate cell ids: don't silently return the first (possibly stale). An ambiguous
    # table must not route a promoted model on a duplicate id.
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"schema_version": 1, "venue": "proxy", "cells": [
        {"cell_id": "c1", "promoted": True, "chosen_model": "sonnet", "fallback_model": "generate",
         "ranking": [{"model": "sonnet"}]},
        {"cell_id": "c1", "promoted": True, "chosen_model": "haiku", "fallback_model": "generate",
         "ranking": [{"model": "haiku"}]}]}))
    # ambiguous -> safe parent default, not an arbitrary pick.
    assert rt.read_route(p, cell_id="c1", parent_task_type="generate") == "generate"


def test_rt8_confidence_rejects_non_finite_pvalue():
    from apex_router import gate
    for bad_p in (float("nan"), float("inf"), float("-inf"), -1.0, 2.0):
        gr = gate.GateResult("c", True, "sonnet", bad_p, "generate")
        conf = rt._confidence(gr)
        assert 0.0 <= conf <= 1.0, f"p={bad_p} -> conf={conf}"


def test_rt9_build_ranking_zero_n_does_not_crash():
    # BUG (Codex #9): n=0 raised from wilson_ci before the zero-sample guard. A model
    # with no samples must be handled (quality 0, no CI crash) or excluded, not crash.
    stats_in = {"opus": (0, 0, 0.05, 2.0), "sonnet": (6, 10, 0.01, 1.0)}
    ranking = rt.build_ranking(stats_in)
    models = {r["model"] for r in ranking}
    assert "sonnet" in models                      # the valid model survives


# --------------------------------------------------------------------------- #
# Regression — Codex PASS 2 (route-table + gate composition)
# --------------------------------------------------------------------------- #
def test_rt_p2f4_absent_heal_row_does_not_break_emit():
    # BUG (Codex pass2 #4): run_gate emits absent-heal cells with parent_task_type="",
    # which the emitter then REJECTED -> the whole table failed to regenerate and the
    # stale route stayed live. An absent-heal row (no real cell to route) must be
    # tolerated: skipped from the cells, surfaced as a dropped route, not a hard error.
    from apex_router import gate
    absent_heal = gate.GateResult(cell_id="gone", promoted=False, chosen_model="",
                                  pvalue=1.0, parent_task_type="", healed=True)
    real = _gate_result("c1", True, "sonnet", parent="generate")
    rankings = {"c1": [_rank("sonnet", 0.9, 0.8, 0.95, 0.01, 1.0)]}
    table = rt.emit_route_table([real, absent_heal], rankings, venue="proxy",
                                generated_from={"bench_run_id": "r1"}, path=None)
    ids = {c["cell_id"] for c in table["cells"]}
    assert "c1" in ids                             # the real cell is emitted
    # the absent-heal 'gone' is recorded as a dropped route, not a routable cell.
    assert "gone" in table.get("dropped_routes", [])


def test_rt_p2f5_empty_model_name_in_ranking_not_routed():
    # BUG (Codex pass2 #5): a ranking containing model "" allowed a promoted chosen=""
    # to be 'supported'. An empty model name is not a real route -> demote to default.
    results = [_gate_result("c1", True, "", parent="generate")]
    rankings = {"c1": [{"model": "", "quality": 0.9, "quality_ci": (0.8, 0.95),
                        "cost_usd": 0.01, "latency": 1.0, "n": 30}]}
    table = rt.emit_route_table(results, rankings, venue="proxy",
                                generated_from={}, path=None)
    assert table["cells"][0]["chosen_model"] == "generate"
    assert table["cells"][0]["promoted"] is False


def test_rt_p2f5_read_route_requires_chosen_in_ranking(tmp_path):
    # BUG (Codex pass2 #5): read_route returned a promoted chosen_model without checking
    # it is in the persisted ranking. A promoted cell whose chosen isn't ranked -> default.
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"schema_version": 1, "venue": "proxy", "cells": [
        {"cell_id": "c1", "promoted": True, "chosen_model": "unranked",
         "fallback_model": "generate", "ranking": []}]}))
    assert rt.read_route(p, cell_id="c1", parent_task_type="generate") == "generate"


def test_rt_p2f5_read_route_none_path_falls_back():
    # BUG (Codex pass2 #5): Path(None) raised before the fallback. A None/invalid path
    # must return the parent default, not crash.
    assert rt.read_route(None, cell_id="c1", parent_task_type="debug") == "debug"


# --------------------------------------------------------------------------- #
# Integration — the WHOLE measured path: bench -> gate -> route table -> read
# --------------------------------------------------------------------------- #
def test_full_pipeline_bench_to_route_table(tmp_path):
    from apex_router import bench, gate

    def steps(split, n):
        return [bench.Step(f"{split}-s{i}", "proxy", "c1", split,
                           {"messages": [{"role": "user", "content": str(i)}]},
                           window_id=("wA" if i % 2 == 0 else "wB")) for i in range(n)]

    costs = {"opus": 0.05, "sonnet": 0.01}
    lats = {"opus": 2.0, "sonnet": 1.0}

    def replay(step, model):
        return bench.Replay(model, costs[model], 100, 20, lats[model])

    def score(step, model, rep):
        base = {"opus": 0.5, "sonnet": 0.8}[model]
        jitter = 0.1 * ((hash(step.step_id) % 5) - 2) / 2.0
        return bench.objective_score((base + jitter) >= 0.65)

    rows = []
    for split in ("promotion", "confirmation"):
        rows += bench.run_bench(steps(split, 12), candidate_set=["opus", "sonnet"],
                                replay_fn=replay, score_fn=score, bench_run_id="r1",
                                corpus_snapshot="snap1", store_path=tmp_path / "o.jsonl",
                                now_fn=lambda: "t")

    cell = bench.cell_evidence_from_rows(rows, cell_id="c1", parent_task_type="generate",
                                         incumbent="opus")
    results = gate.run_gate([cell], k=8, m_windows=2, alpha=0.05)
    assert results[0].promoted is True and results[0].chosen_model == "sonnet"

    mstats = {}
    for m in ("opus", "sonnet"):
        mrows = [r for r in rows if r["model"] == m]
        passes = sum(1 for r in mrows if r["outcome"]["score"] == 1.0)
        mstats[m] = (passes, len(mrows), costs[m], lats[m])
    ranking = rt.build_ranking(mstats)
    p = tmp_path / "route_table.proxy.json"
    rt.emit_route_table(results, {"c1": ranking}, venue="proxy",
                        generated_from={"bench_run_id": "r1", "corpus_snapshot": "snap1"}, path=p)

    assert rt.read_route(p, cell_id="c1", parent_task_type="generate") == "sonnet"
    assert rt.read_route(p, cell_id="absent", parent_task_type="debug") == "debug"
