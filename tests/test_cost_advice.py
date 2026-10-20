"""cost_advice: per-cell done-cost p75/p90, n gate, no pooling, advice only."""
from __future__ import annotations

import json

import pytest

from apex_router import cost_advice as ca


def _row(cost, *, agent="worker", model="opus", thinking="high", state="done", **kw):
    return {"agent": agent, "model": model, "thinking": thinking, "state": state, "cost_usd": cost, **kw}


def test_percentiles_per_cell():
    rows = [_row(c) for c in [1, 2, 3, 4, 5, 6, 7, 8, 9]]
    a = ca.advice("worker", "opus", "high", rows=rows)
    assert a["advice"] is True and a["n"] == 9
    assert a["p75"] == 7.0 and a["p90"] == pytest.approx(8.2)


def test_small_cell_gives_no_advice():
    a = ca.advice("worker", "opus", "high", rows=[_row(1.0)] * 7)
    assert a == {"agent": "worker", "model": "opus", "thinking": "high", "n": 7, "min_n": 8, "advice": False, "p75": None, "p90": None}
    assert "no advice" in ca.line(a)


def test_no_pooling_other_cells_and_states_ignored():
    rows = [_row(1.0)] * 8
    rows += [_row(100.0, thinking="medium")] * 50          # other thinking: never pooled in
    rows += [_row(100.0, model="sonnet")] * 50             # other model
    rows += [_row(100.0, agent="reviewer")] * 50           # other agent
    rows += [_row(100.0, state="killed(cost)")] * 50       # not done
    rows += [_row(100.0, capped=True)] * 50                # done after the soft cap: cut by the cap
    rows += [_row(None), _row(0), _row(True), "junk", {}]  # bad rows
    a = ca.advice("worker", "opus", "high", rows=rows)
    assert (a["n"], a["p75"], a["p90"]) == (8, 1.0, 1.0)


def test_below_p75_flag_and_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APEX_HOME", str(tmp_path))
    (tmp_path / "labels").mkdir()
    rows = [_row(float(c)) for c in range(1, 11)]
    (tmp_path / "labels" / "agent_runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    assert ca.main(["--agent", "worker", "--model", "opus", "--thinking", "high", "--json", "--max-cost", "5"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["n"] == 10 and out["p75"] == 7.75 and out["below_p75"] is True
    assert ca.main(["--agent", "worker", "--model", "opus", "--thinking", "high"]) == 0
    assert capsys.readouterr().out.strip() == "cost advice worker/opus/high: done-cost p75 $7.75, p90 $9.10 (n=10)"


def test_missing_file_is_no_advice(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_HOME", str(tmp_path))
    assert ca.advice("worker", "opus", "high")["advice"] is False


def test_module_entry_point(tmp_path):
    import subprocess, sys, os
    env = {**os.environ, "APEX_HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "apex_router.cost_advice", "--agent", "w", "--model", "m", "--thinking", "t", "--json"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads(r.stdout)["advice"] is False
