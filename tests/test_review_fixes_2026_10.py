"""Regression tests for the four findings of the 2026-10 post-pull cross-validated review:
unknown GPT ids leaking into the Claude-priced headline, an unguarded env float at import,
planning numbers at a degenerate base rate, and install-snapshot swallowing a launchctl failure."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from apex_router import retry_ab, route_join, route_log, watch

ROOT = Path(__file__).resolve().parents[1]


# 1. an unrecognised GPT id (e.g. astra) is still GPT family: never in a Claude-priced headline
def _w(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_unknown_gpt_id_is_excluded_from_the_headline(tmp_path):
    log = tmp_path / "route_log.jsonl"
    _w(log, [
        {"ts": 1.0, "task_type": "review", "model": "it-entra-gpt-6-astra", "passed": True,
         "escalated": False, "note": "turn"},
        {"ts": 2.0, "task_type": "review", "model": "claude-sonnet-4-6", "passed": True,
         "escalated": False, "note": "turn"},
    ])
    cell = route_log.read_rates(log_path=log, labeled_path=tmp_path / "none.jsonl")["review"]
    assert cell["n"] == 1                                    # only the Claude row is headline
    assert cell["by_tier"]["other"]["n"] == 1                # astra stays visible, unranked
    assert cell["by_tier"]["sonnet"]["n"] == 1


def test_non_gpt_unknowns_stay_in_the_headline(tmp_path):
    log = tmp_path / "route_log.jsonl"
    _w(log, [{"ts": 1.0, "task_type": "explore", "model": "kimi-k2.6", "passed": True,
              "escalated": False, "note": "turn"}])
    assert route_log.read_rates(log_path=log, labeled_path=tmp_path / "n.jsonl")["explore"]["n"] == 1


def test_is_gpt_family_helper():
    assert route_log.is_gpt_family("gpt-terra", None)
    assert route_log.is_gpt_family(None, "it-entra-gpt-6-astra")
    assert not route_log.is_gpt_family(None, "kimi-k2.6", "auto")
    assert not route_log.is_gpt_family("sonnet", "claude-sonnet-4-6")
    assert not route_log.is_gpt_family(None, None, 7)
    # token-bounded: quantisation suffixes / product names are not the GPT family
    assert not route_log.is_gpt_family("Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4", "chatgpt")
    assert route_log.is_gpt_family("openai/gpt-oss-120b")
    for name in ("gpt5", "gpt_5", "GPT-5", "gpt4o", "openai:gpt-4o", "it-entra-gpt-6-astra"):
        assert route_log.is_gpt_family(name), name


# 2. APEX_LABEL_JUDGE_MIN_CONF garbage must not make `import labels` raise
@pytest.mark.parametrize("val,want", [("banana", 0.8), ("nan", 0.8), ("", 0.8), ("0.9", 0.9)])
def test_judge_min_conf_env_is_guarded(val, want):
    out = subprocess.run(
        [sys.executable, "-c", "from apex_router import labels; print(labels.JUDGE_MIN_CONF)"],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"),
             "APEX_LABEL_JUDGE_MIN_CONF": val, "HOME": str(Path.home())},
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert float(out.stdout) == want


# 3. planning numbers are undefined at a 0% / 100% base rate
@pytest.mark.parametrize("p0", [0.0, 1.0])
def test_planning_numbers_degenerate_base_rate(p0):
    assert retry_ab.mde(p0, 30) is None
    assert retry_ab.n_for_diff(p0, 0.10) is None


def test_planning_numbers_unchanged_for_a_real_rate():
    assert retry_ab.mde(0.5, 30) == pytest.approx(2.8 * (2 * 0.25 / 30) ** 0.5)
    assert retry_ab.n_for_diff(0.5, 0.10) > 100


def test_render_at_zero_base_rate_says_so(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("".join(json.dumps({
        "ts": 1_000.0 + i, "is_error": True, "connect_retries": 1,
        "error_cause": "ConnectError"}) + "\n" for i in range(40)))
    rep = retry_ab.report(t)
    assert rep["pre_v11_retried"]["p_recovered"] == 0.0     # the fixture really is the 0% case
    text = retry_ab.render(rep)
    assert "≈ 0/arm" not in text and "≈ 0.0 pts" not in text
    assert "no planning numbers" in text


# 4. every launchd installer must not report success when launchctl bootstrap failed — but one
#    rc=5 right after bootout of a running daemon is the normal unload race, so it is retried
def _run_seq(bootstrap_rcs):
    """subprocess.run stand-in: bootout always ok; successive bootstraps return `bootstrap_rcs`."""
    rcs = list(bootstrap_rcs)
    calls = []

    def run(cmd, *a, **k):
        calls.append(cmd)
        if "bootstrap" in cmd:
            rc = rcs.pop(0) if len(rcs) > 1 else rcs[0]
            return subprocess.CompletedProcess(cmd, rc, "", "Bootstrap failed: 5: Input/output error")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    run.calls = calls
    return run


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    return tmp_path


INSTALLERS = [
    (lambda: watch._launchd_install(), [watch.LABEL_DRAIN, watch.LABEL_DAILY]),
    (lambda: watch._launchd_install_serve(), [watch.LABEL_SERVE]),
    (lambda: watch._launchd_install_snapshot(), [watch.LABEL_SNAPSHOT]),
]


@pytest.mark.parametrize("install,labels", INSTALLERS)
def test_installers_raise_when_bootstrap_keeps_failing(launchd, install, labels):
    run = _run_seq([5])
    with mock.patch.object(watch.subprocess, "run", side_effect=run):
        with pytest.raises(SystemExit) as e:
            install()
    msg = str(e.value)
    assert labels[0] in msg and "Input/output" in msg and f"{watch._BOOTSTRAP_TRIES} tries" in msg
    assert sum("bootstrap" in c for c in run.calls) == watch._BOOTSTRAP_TRIES


@pytest.mark.parametrize("install,labels", INSTALLERS)
def test_installers_retry_the_unload_race_then_succeed(launchd, install, labels):
    run = _run_seq([5, 5, 0])          # the first two bootstraps lose the race with the unload
    with mock.patch.object(watch.subprocess, "run", side_effect=run):
        assert install() == labels


@pytest.mark.parametrize("install,labels", INSTALLERS)
def test_installers_ok_first_try(launchd, install, labels):
    run = _run_seq([0])
    with mock.patch.object(watch.subprocess, "run", side_effect=run):
        assert install() == labels
    assert sum("bootstrap" in c for c in run.calls) == len(labels)   # no gratuitous retries


def test_route_join_headline_excludes_unknown_gpt_too():
    # the joined-table path has its own headline gate; it must agree with route_log's
    table = [
        {"task_type": "review", "escalated": False, "model": "it-entra-gpt-6-astra"},
        {"task_type": "review", "escalated": True, "model": "claude-sonnet-4-6"},
    ]
    cell = route_join.cell_rates(table)["review"]
    assert (cell["n"], cell["escalated"]) == (1, 1)         # astra not priced as Claude
    assert cell["by_tier"]["other"]["n"] == 1


def test_raw_and_joined_headlines_agree_on_mixed_metadata(tmp_path):
    # start_tier carries the unknown GPT id, model does not: the join discards start_tier, so it
    # must record the family fact itself for both aggregators to agree.
    route_row = {"ts": 1.0, "task_type": "review", "model": "kimi-k2.6",
                 "start_tier": "it-entra-gpt-6-astra", "escalated": False, "passed": True,
                 "note": "turn"}
    log = tmp_path / "route_log.jsonl"
    _w(log, [route_row])
    raw = route_log.read_rates(log_path=log, labeled_path=tmp_path / "none.jsonl")["review"]["n"]
    out = route_join._build_joined(route_row, {})
    assert out["tier_family"] == "gpt"
    joined = route_join.cell_rates([{**out, "task_type": "review", "escalated": False}])["review"]["n"]
    assert raw == joined == 0


def test_gptq_model_stays_in_the_headline(tmp_path):
    log = tmp_path / "route_log.jsonl"
    _w(log, [{"ts": 1.0, "task_type": "explore", "model": "Qwen/Qwen2.5-7B-Instruct-GPTQ-Int4",
              "passed": True, "escalated": False, "note": "turn"}])
    assert route_log.read_rates(log_path=log, labeled_path=tmp_path / "n.jsonl")["explore"]["n"] == 1
