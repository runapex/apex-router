"""retry-ab readout: per-arm P(retry recovers), Wilson CIs, Newcombe difference, honest verdicts."""
from __future__ import annotations

import json

import pytest

from apex_router import retry_ab
from apex_router.cli import main as cli_main


def _row(ts, arm=None, policy="ab", cause=None, retries=1, prop=0.5, backoff=0.0):
    r = {"ts": ts, "is_error": bool(cause and not cause.startswith("http_4")),
         "error_cause": cause, "connect_retries": retries, "connect_backoff_ms": backoff}
    if arm:
        r.update(retry_policy=policy, retry_arm=arm, retry_propensity=prop)
    return r


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n"
                    + json.dumps({"ev": "hb", "ts": 1}) + "\nnot json\n")
    return path


def test_newcombe_matches_reference():
    # Newcombe (1998) example 1: 56/70 vs 48/80 → diff 0.20, CI [0.0524, 0.3339]
    d, lo, hi = retry_ab.newcombe_diff(56, 70, 48, 80)
    assert d == pytest.approx(0.2) and lo == pytest.approx(0.0524, abs=1e-3)
    assert hi == pytest.approx(0.3339, abs=1e-3)
    assert retry_ab.newcombe_diff(1, 0, 1, 1) == (None, None, None)


def test_inconclusive_below_min_n(tmp_path):
    rows = [_row(i, "immediate", cause="SSLError" if i % 2 else None) for i in range(29)]
    rows += [_row(100 + i, "wait") for i in range(40)]
    rep = retry_ab.report(_write(tmp_path / "t.jsonl", rows))
    assert rep["arms"]["immediate"]["n"] == 29
    assert rep["verdict"] == "INCONCLUSIVE"
    assert rep["comparisons"][0]["recovered"]["diff"] > 0


def test_decides_when_both_arms_have_enough(tmp_path):
    rows = [_row(i, "immediate", cause="ReadError" if i % 2 else None) for i in range(60)]
    rows += [_row(100 + i, "wait", backoff=1000.0) for i in range(60)]
    rep = retry_ab.report(_write(tmp_path / "t.jsonl", rows))
    imm, wait = rep["arms"]["immediate"], rep["arms"]["wait"]
    assert imm["p_recovered"] == pytest.approx(0.5) and wait["p_recovered"] == 1.0
    assert wait["mean_backoff_ms"] == 1000.0 and wait["propensities"] == [0.5]
    assert rep["verdict"] == "better"
    rows2 = [_row(i, a) for i in range(40) for a in ("immediate", "wait")]
    assert retry_ab.report(_write(tmp_path / "u.jsonl", rows2))["verdict"] == "no detectable difference"


def test_recovered_vs_clean_and_context_rows(tmp_path):
    rows = [_row(1, "immediate", cause="http_429"),        # a response came back: recovered, not clean
            _row(2, "immediate", cause="midstream_ReadError"),
            _row(3, "immediate", cause="ConnectError"),     # raise path: not recovered
            _row(4, "wait", policy="wait", prop=1.0),       # fixed policy: context only
            _row(5, retries=2, cause="SSLError"),           # pre-v11 retried rows
            _row(6, retries=1),
            _row(7, retries=0)]
    rep = retry_ab.report(_write(tmp_path / "t.jsonl", rows))
    imm = rep["arms"]["immediate"]
    assert (imm["n"], imm["recovered"], imm["clean"]) == (3, 2, 0)
    assert set(rep["arms"]) == {"immediate"} and rep["comparisons"] == []
    assert rep["fixed_policy"]["wait"]["n"] == 1
    assert rep["pre_v11_retried"]["n"] == 2 and rep["pre_v11_retried"]["recovered"] == 1
    assert rep["verdict"] == "INCONCLUSIVE"
    text = retry_ab.render(rep)
    assert "context only" in text and "pre-v11" in text and "INCONCLUSIVE" in text


def test_planning_numbers(tmp_path):
    day = 86400.0
    rows = [_row(i * day / 10, retries=1, cause=None if i % 4 else "SSLError") for i in range(70)]
    rep = retry_ab.report(_write(tmp_path / "t.jsonl", rows))
    assert rep["retried_per_day"] == pytest.approx(10.0, rel=0.05)    # 70 rows over ~7 days
    assert rep["days_to_min_n"] == pytest.approx(6.0, rel=0.05)       # 60 rows needed at 10/day
    assert rep["mde_at_min_n"] > 0.2 and rep["n_per_arm_for_10pts"] > 100
    assert retry_ab.n_for_diff(0.5, 0.0) is None


def test_missing_file_and_cli(tmp_path, capsys):
    rep = retry_ab.report(tmp_path / "none.jsonl")
    assert rep["arms"] == {} and rep["verdict"] == "INCONCLUSIVE" and rep["pre_v11_retried"] is None
    assert "no randomised retry rows yet" in retry_ab.render(rep)
    p = _write(tmp_path / "t.jsonl", [_row(1, "immediate")])
    assert cli_main(["retry-ab", "report", "--telemetry", str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["arms"]["immediate"]["n"] == 1
    assert retry_ab.main(["report", "--telemetry", str(p), "--min-n", "0"]) == 2
