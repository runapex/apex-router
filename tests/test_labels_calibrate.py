"""C3: labels calibrate (held-out gates) and the relabel guard. Synthetic gold, no judge."""
from __future__ import annotations

import json

from apex_router import labels as L


def _rows(n_ok=80, n_fail=20, noisy=False):
    """Two rule voters. 'tests' is right on every row; 'commit' is right on every row too
    (or random-ish when noisy). Gold = the true outcome."""
    rows, gold = [], {}
    for i in range(n_ok + n_fail):
        out = "success" if i < n_ok else "fail"
        v = {"tests": out, "commit": out}
        if noisy:
            v = {"tests": "success", "commit": "fail"}
        rows.append({"id": f"t{i}", "source": "pi", "session": "s", "index": i, "n_calls": 1,
                     "n_errors": 0, "has_next": True, "votes": v})
        gold[f"t{i}"] = out
    return rows, gold


def test_calibrate_pass_reports_all_numbers():
    rows, gold = _rows()
    r = L.calibrate(rows, gold, 0.5, 7)
    assert r["status"] == "pass"
    assert r["gold"] == 100 and r["fit"] + r["heldout"] == 100
    assert r["wilson_low"] >= 0.85 and r["wilson_low"] <= r["accuracy"]
    assert r["emitted_share"] > 0 and r["fails_heldout"] >= 2 and r["fail_recall"] >= 0.5
    txt = L.calibrate_text(r)
    for w in ("emitted share", "accuracy on emitted", "Wilson low", "fail-class recall", "GATE: PASS"):
        assert w in txt


def test_calibrate_is_deterministic_and_seeded():
    rows, gold = _rows()
    a = L.calibrate(rows, gold, 0.5, 7)
    assert a == L.calibrate(rows, gold, 0.5, 7)
    assert L.calibrate(rows, gold, 0.5, 8)["fit"] == a["fit"]


def test_calibrate_fails_on_wrong_voters():
    rows, gold = _rows(noisy=True)       # voters disagree: nothing clears EMIT_MIN or it is wrong
    assert L.calibrate(rows, gold)["status"] == "fail"


def test_calibrate_fail_recall_gate_and_na():
    # voters that never say fail: accuracy on emitted is high, but held-out fails are missed
    rows, gold = _rows()
    for r in rows:
        r["votes"] = {"tests": "success", "commit": "success"}
    r = L.calibrate(rows, gold, 0.5, 7)
    assert r["fails_heldout"] >= 2 and r["gate"]["fail_recall"] is False
    assert r["status"] == "fail"
    # fewer than 2 fails held out -> recall cannot be judged: NOT a pass (reviewer finding)
    rows, gold = _rows(n_ok=80, n_fail=1)
    r = L.calibrate(rows, gold, 0.5, 7)
    assert r["fails_heldout"] < 2 and r["gate"]["fail_recall"] is None
    assert r["status"] == "insufficient" and "NOT ENOUGH DATA" in L.calibrate_text(r)


def test_tiny_emitted_set_cannot_pass_on_point_accuracy():
    # reviewer repro: 1 correct emission + abstentions gave PASS on point accuracy 100 %
    rows, gold = _rows(n_ok=16, n_fail=4)
    for r in rows:
        r["votes"] = {"tests": gold[r["id"]]} if r["index"] in (0, 16, 17, 18, 19) else {}
    r = L.calibrate(rows, gold, 0.5, 7)
    assert r["status"] != "pass"


def test_missed_single_fail_is_not_hidden_by_na():
    # reviewer repro: predict success throughout; one held-out fail missed -> must not PASS
    rows, gold = _rows(n_ok=80, n_fail=1)
    for r in rows:
        r["votes"] = {"tests": "success", "commit": "success"}
    assert L.calibrate(rows, gold, 0.5, 7)["status"] != "pass"


def test_calibrate_not_enough_data():
    rows, gold = _rows(n_ok=5, n_fail=2)
    r = L.calibrate(rows, gold)
    assert r["status"] == "insufficient" and "NOT ENOUGH DATA" in L.calibrate_text(r)


def _store(tmp_path, monkeypatch, rows, gold):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    h = tmp_path / "labels"
    h.mkdir(exist_ok=True)
    (h / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (h / "gold").mkdir(exist_ok=True)
    (h / "gold" / "consensus.csv").write_text(
        "task_id,outcome,correct_without_rework\n" + "".join(f"{k},{v},yes\n" for k, v in gold.items()))
    return h


def test_cli_exit_codes_and_never_writes(tmp_path, monkeypatch, capsys):
    rows, gold = _rows()
    h = _store(tmp_path, monkeypatch, rows, gold)
    before = sorted(p.name for p in h.rglob("*"))
    assert L.main(["calibrate"]) == 0
    assert "GATE: PASS" in capsys.readouterr().out
    assert L.main(["calibrate", "--json", "--seed", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"
    assert sorted(p.name for p in h.rglob("*")) == before          # no labels.jsonl written
    assert not (h / "labels.jsonl").exists()
    rows, gold = _rows(noisy=True)
    _store(tmp_path, monkeypatch, rows, gold)
    assert L.main(["calibrate"]) == 1
    rows, gold = _rows(n_ok=3, n_fail=2)
    _store(tmp_path, monkeypatch, rows, gold)
    assert L.main(["calibrate"]) == 2


def test_relabel_guard_uses_fitted_accuracy_only_when_gate_passes(tmp_path, monkeypatch):
    # one voter each: a single vote with no calibration gives posterior < EMIT_MIN
    rows, gold = _rows()
    for r in rows:
        r["votes"] = {"tests": gold[r["id"]]}
    _store(tmp_path, monkeypatch, rows, gold)
    res = L.relabel(write=False, calibrated=True)
    assert res["gate"] == "pass" and res["acc"]
    assert sum(x["label"] != "unknown" for x in res["labels"]) == len(rows)
    # gate fails (fit voters wrong half the time): no fitted accuracy, nothing emitted
    for r in rows:
        r["votes"] = {"tests": "success"}
    rows2 = [{**r, "votes": {"tests": "success" if r["index"] % 2 else "fail"}} for r in rows]
    _store(tmp_path, monkeypatch, rows2, gold)
    res = L.relabel(write=False, calibrated=True)
    assert res["gate"] == "fail" and res["acc"] == {}
    assert all(x["label"] == "unknown" for x in res["labels"])
    # the default path (calibrated=False) is unchanged
    assert L.relabel(write=False)["gate"] is None


# ---- design weights for the stratified gold sample (report only) ----------------------------

def _design(h, strata: dict, sheet: dict):
    """strata: (project, task_type) -> population; sheet: task id -> (project, task_type)."""
    (h / "gold" / "sample_manifest.json").write_text(json.dumps({"strata": [
        {"project": p, "task_type": t, "population": n, "sampled": 0} for (p, t), n in strata.items()]}))
    (h / "gold" / "sheet.csv").write_text(
        "\ufefftask_id,project,task_type\n" + "".join(f"{i},{p},{t}\n" for i, (p, t) in sheet.items()))


def _two_strata():
    """Stratum A (population 900): voters always right. Stratum B (population 100, same sample
    size, so 9x over-represented): voters wrong on half. True accuracy = 0.9*1 + 0.1*0.5 = 0.95;
    the unweighted sample mean is about 0.75."""
    rows, gold, sheet = [], {}, {}
    for i in range(80):
        a = i % 2 == 0
        wrong = not a and (i // 2) % 2 == 0
        v = "fail" if wrong else "success"
        rows.append({"id": f"t{i}", "source": "pi", "session": "s", "index": i, "n_calls": 1,
                     "n_errors": 0, "has_next": True, "votes": {"tests": v, "commit": v}})
        gold[f"t{i}"] = "success"
        sheet[f"t{i}"] = ("p", "A" if a else "B")
    return rows, gold, sheet


def test_weighted_accuracy_recovers_population_and_unweighted_does_not(tmp_path, monkeypatch):
    rows, gold, sheet = _two_strata()
    h = _store(tmp_path, monkeypatch, rows, gold)
    _design(h, {("p", "A"): 900, ("p", "B"): 100}, sheet)
    w = L.gold_design(gold)
    assert w["t0"] == 900 / 40 and w["t1"] == 100 / 40
    r = L.calibrate(rows, gold, 0.5, 7, weights=w)
    assert abs(r["accuracy"] - 0.75) < 0.1                      # sample mix, not the population
    assert abs(r["weighted"]["accuracy"] - 0.95) < 0.03         # population truth 0.95
    assert r["weighted"]["n_eff"] < r["weighted"]["n"]          # unequal weights cost precision


def test_weights_do_not_change_the_gate_or_any_unweighted_number():
    rows, gold = _rows()
    base = L.calibrate(rows, gold, 0.5, 7)
    w = L.calibrate(rows, gold, 0.5, 7, weights={k: 1.0 for k in gold})
    assert "weighted" not in base
    assert {k: v for k, v in w.items() if k != "weighted"} == base
    # equal weights: weighted == unweighted and n_eff == n
    assert abs(w["weighted"]["accuracy"] - base["accuracy"]) < 1e-12
    assert abs(w["weighted"]["n_eff"] - base["emitted"]) < 1e-9
    assert abs(w["weighted"]["wilson_low"] - base["wilson_low"]) < 1e-9
    assert "weighted accuracy" in L.calibrate_text(w) and "weighted" not in L.calibrate_text(base)


def test_gold_design_is_none_without_a_valid_single_design(tmp_path, monkeypatch):
    rows, gold, sheet = _two_strata()
    h = _store(tmp_path, monkeypatch, rows, gold)
    assert L.gold_design(gold) is None                                    # no manifest, no sheet
    _design(h, {("p", "A"): 900, ("p", "B"): 100}, sheet)
    # milestone 5: an id outside the design gets no weight (it no longer voids the design), so
    # gold from a later draw cannot hide the weighted numbers of a complete design
    w = L.gold_design({**gold, "other": "fail"})
    assert w is not None and "other" not in w and set(w) == set(gold)
    assert L.gold_design({}) is None
    _design(h, {("p", "A"): 900}, sheet)
    assert L.gold_design(gold) is None                                    # stratum without N_h
    _design(h, {("p", "A"): 900, ("p", "B"): 0}, sheet)
    assert L.gold_design(gold) is None                                    # bad N_h
    _design(h, {("p", "A"): 900, ("p", "B"): 100, ("p", "C"): 50}, sheet)
    assert L.gold_design(gold) is None                                    # stratum C uncovered
    (h / "gold" / "sample_manifest.json").write_text("{not json")
    assert L.gold_design(gold) is None


def test_gold_design_divides_by_answered_rows_per_stratum(tmp_path, monkeypatch):
    # non-response: the sheet has 4 ids in A and 2 in B, but only 2 and 1 have an outcome
    rows, _, _ = _two_strata()
    h = _store(tmp_path, monkeypatch, rows, {})
    _design(h, {("p", "A"): 90, ("p", "B"): 10},
            {"a1": ("p", "A"), "a2": ("p", "A"), "a3": ("p", "A"), "a4": ("p", "A"),
             "b1": ("p", "B"), "b2": ("p", "B")})
    w = L.gold_design({"a1": "success", "a2": "fail", "b1": "success"})
    assert w == {"a1": 45.0, "a2": 45.0, "b1": 10.0}                    # N_h / answered r_h
    assert sum(w.values()) == 100                                         # weights sum to N


def test_weighted_block_matches_hand_computed_kish_and_fractional_wilson():
    # weights 3, 1, 1, 1; correct = T, F, T, T. Independent arithmetic, not the function under test.
    r = L._weighted([(3.0, True), (1.0, False), (1.0, True), (1.0, True)])
    assert abs(r["accuracy"] - 5 / 6) < 1e-12
    assert abs(r["n_eff"] - 36 / 12) < 1e-12                             # (sum w)^2 / sum w^2
    z, n, p = 1.96, 3.0, 5 / 6
    lo = ((p + z * z / (2 * n)) - z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    assert abs(r["wilson_low"] - lo) < 1e-12
    assert r["n"] == 4
    assert L._weighted([]) == {"accuracy": None, "n_eff": 0.0, "wilson_low": 0.0, "n": 0}


def test_cli_reports_weighted_block_and_keeps_exit_codes(tmp_path, monkeypatch, capsys):
    rows, gold = _rows()
    h = _store(tmp_path, monkeypatch, rows, gold)
    _design(h, {("p", "ok"): 800, ("p", "fail"): 20}, {k: ("p", v if v == "fail" else "ok")
                                                        for k, v in gold.items()})
    assert L.main(["calibrate"]) == 0
    out = capsys.readouterr().out
    assert "weighted accuracy" in out and "not gated" in out and "GATE: PASS" in out
    assert L.main(["calibrate", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["weighted"]["n_eff"] > 0


def test_report_counts_consensus_gold_like_calibrate(tmp_path, monkeypatch):
    # consensus.csv gold (no gold.jsonl) must show in `labels report`, as `calibrate` uses it
    rows, gold = _rows(n_ok=30, n_fail=10)
    h = _store(tmp_path, monkeypatch, rows, gold)
    assert not (h / "gold.jsonl").exists()
    txt = L.report()
    assert "gold 40 / target" in txt and "consensus 40" in txt
    assert "no gold yet" not in txt
    assert "emitted-label precision on gold" in txt
    tests_row = next(line for line in txt.splitlines() if line.strip().startswith("tests "))
    assert tests_row.split()[3] == "40"                 # voter accuracy measured on the 40 gold rows
    # the relabel/gate path is unchanged: without a passing gate no label comes from consensus gold
    assert L.relabel(write=False)["gold"] == {}
    assert not (h / "labels.jsonl").exists()
