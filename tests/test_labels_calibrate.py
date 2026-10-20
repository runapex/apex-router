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
