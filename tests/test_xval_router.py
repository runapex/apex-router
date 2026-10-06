"""xval_router: classify, P2C selection, discounted learning, command building, rollout metrics."""
from __future__ import annotations

import json
import random

import pytest

from apex_router import xval_router as xr


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_ROUTER_HOME", str(tmp_path))
    return tmp_path


def test_classify():
    assert xr.classify(["review", "--uncommitted"]) == "diff"
    assert xr.classify(["-m", "x", "review", "--base", "main"]) == "diff"
    assert xr.classify(["-s", "read-only", "refute the reasoning and every number in report.md"]) == "report"
    assert xr.classify(["-s", "read-only", "review src/app/models/foo.rb for race hazards"]) == "files"
    assert xr.classify(["-s", "read-only", "how does the scheduler decide retries?"]) == "investigate"
    assert xr.classify(["review m.py and big.txt for problems; one-line verdict"]) == "files"
    assert xr.classify(["refute the disposition in EYE-1 analysis"]) == "report"
    assert xr.classify(["-s", "read-only", "-"], stdin_text="check this diff:\n+ a") == "diff"


def test_prompt_extraction_skips_option_values():
    assert xr._prompt_of(["-m", "gpt", "-s", "read-only", "-o", "/tmp/o.md", "do X"]) == "do X"
    assert xr._prompt_of(["-c", "a=b", "--skip-git-repo-check", "do Y"]) == "do Y"
    assert xr._prompt_index(["src", "-C", "src"]) == 0  # an option VALUE equal to the prompt
    assert xr._prompt_of(["--add-dir", "/x", "--output-schema", "s.json", "p"]) == "p"
    assert xr._prompt_of(["-i", "a.png", "--", "look"]) == "look"
    assert xr._prompt_index(["-i", "a.png", "b.png"]) is None   # multi-value -i: never a prompt
    cmd, stdin = xr.build_command(["-i", "a.png", "b.png", "-"], "4000/focused", "piped prompt")
    assert cmd[-3:] == ["a.png", "b.png", "-"] and stdin.endswith(xr.FOCUS_HINT)


def test_focused_hint_never_lands_on_an_option_value():
    cmd, _ = xr.build_command(["src", "-C", "src"], "4000/focused")
    assert cmd[4].startswith("src") and xr.FOCUS_HINT in cmd[4]  # the prompt (position 0 of argv)
    assert cmd[-1] == "src"                                        # -C value untouched


def test_build_command_caps_output_and_scopes_only_prompt_mode():
    cmd, stdin = xr.build_command(["-m", "g", "-s", "read-only", "do X"], "4000/focused")
    assert cmd[:4] == ["codex", "exec", "-c", "tool_output_token_limit=4000"]
    assert cmd[-1].startswith("do X") and xr.FOCUS_HINT in cmd[-1]
    cmd, _ = xr.build_command(["review", "--uncommitted"], "2000/focused")
    assert cmd[-2:] == ["review", "--uncommitted"]  # review mode: cap only, codex owns the prompt
    cmd, stdin = xr.build_command(["-s", "read-only", "-"], "6000/focused", "the report text")
    assert cmd[-1] == "-" and stdin.endswith(xr.FOCUS_HINT)
    cmd, _ = xr.build_command(["do X"], "10000/open")
    assert cmd[-1] == "do X"


def test_cold_start_prefers_the_baseline_but_explores():
    cat = xr._fresh_state()["categories"]["report"]
    pi = xr.probabilities(cat)
    assert max(pi, key=pi.get) == xr.BASELINE
    picks = [xr.select(cat, random.Random(i))[0] for i in range(1000)]
    share = picks.count(xr.BASELINE) / len(picks)
    assert 0.35 < share < 0.75 and len(set(picks)) >= 5  # baseline-heavy, but P2C still explores


def test_learning_moves_probability_to_a_cheaper_equally_reliable_arm():
    cat = xr._fresh_state()["categories"]["report"]
    for _ in range(30):
        xr.update(cat, xr.BASELINE, ok=True, cost=400_000)
        xr.update(cat, "4000/focused", ok=True, cost=180_000)
    pi = xr.probabilities(cat)
    assert max(pi, key=pi.get) == "4000/focused"
    wins = sum(xr.select(cat, random.Random(i))[0] == "4000/focused" for i in range(400))
    assert wins > 200


def test_cheap_but_failing_arm_loses():
    cat = xr._fresh_state()["categories"]["report"]
    for _ in range(30):
        xr.update(cat, xr.BASELINE, ok=True, cost=400_000)
        xr.update(cat, "2000/focused", ok=False, cost=60_000)
    pi = xr.probabilities(cat)
    assert pi["2000/focused"] < pi[xr.BASELINE]


def test_discounting_tracks_drift():
    cat = xr._fresh_state()["categories"]["diff"]
    for _ in range(40):
        xr.update(cat, "6000/open", ok=True, cost=100_000)
    for _ in range(60):  # the arm starts failing
        xr.update(cat, "6000/open", ok=False, cost=100_000)
    x = cat["arms"]["6000/open"]
    assert x["alpha"] / (x["alpha"] + x["beta"]) < 0.3  # old successes decayed away


def test_rollout_metrics(tmp_path):
    p = tmp_path / "rollout-x-abc.jsonl"
    rows = [
        {"type": "turn_context", "payload": {"model": "it-entra-gpt-6-astra"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 10}}}},
        {"type": "response_item", "payload": {"type": "function_call_output",
                                              "output": "Chunk ID: 1\nWarning: truncated output (original token count: 9)"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 3000, "cached_input_tokens": 1000, "output_tokens": 50}}}},
        {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "verdict"}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    m = xr.rollout_metrics(p)
    assert m["ok"] and m["calls"] == 2 and m["truncated_outputs"] == 1
    assert m["cost"] == pytest.approx(2000 + 100 + 200)


def _arm(home, cat="files", arm="4000/open"):
    return json.loads((home / "xval_bandit.json").read_text())["categories"][cat]["arms"][arm]


def test_feedback_moves_the_decayed_weight_and_can_be_revised(_home):
    with xr._locked_state(_home / "xval_bandit.json") as st:
        xr.update(st["categories"]["files"], "4000/open", ok=True, cost=50_000)
        obs = st["categories"]["files"]["obs"]
        for _ in range(10):  # later observations decay run r1's weight
            xr.update(st["categories"]["files"], xr.BASELINE, ok=True, cost=90_000)
    (_home / "xval_runs.jsonl").write_text(json.dumps(
        {"run_id": "r1", "category": "files", "arm": "4000/open", "ok": True, "cost": 50_000, "obs": obs}) + "\n")
    w = xr.GAMMA ** 10
    b0 = _arm(_home)
    assert xr.feedback("r1", "bad") == 0
    b1 = _arm(_home)
    assert b1["beta"] == pytest.approx(b0["beta"] + w) and b1["alpha"] == pytest.approx(b0["alpha"] - w)
    assert xr.feedback("r1", "bad") == 0 and _arm(_home) == b1          # idempotent
    assert xr.feedback("r1", "ok") == 0                                   # revisable
    b2 = _arm(_home)
    assert b2["alpha"] == pytest.approx(b0["alpha"]) and b2["beta"] == pytest.approx(b0["beta"])
    assert xr.feedback("nope", "ok") == 2


def test_concurrent_feedback_applies_once(_home):
    import threading
    with xr._locked_state(_home / "xval_bandit.json") as st:
        xr.update(st["categories"]["files"], "4000/open", ok=True, cost=50_000)
        obs = st["categories"]["files"]["obs"]
    (_home / "xval_runs.jsonl").write_text(json.dumps(
        {"run_id": "r2", "category": "files", "arm": "4000/open", "ok": True, "cost": 50_000, "obs": obs}) + "\n")
    b0 = _arm(_home)
    ts = [threading.Thread(target=xr.feedback, args=("r2", "bad")) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    b1 = _arm(_home)
    assert b1["beta"] == pytest.approx(b0["beta"] + 1) and b1["alpha"] == pytest.approx(b0["alpha"] - 1)


def test_run_log_ok_matches_what_was_learned(_home, monkeypatch, tmp_path):
    roll = tmp_path / "rollout-x-11111111-2222-3333-4444-555555555555.jsonl"
    roll.write_text("\n".join(json.dumps(r) for r in [
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 1}}}},
        {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "v"}}]))
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\necho 'session id: 11111111-2222-3333-4444-555555555555' >&2\ncat >/dev/null\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(fake))
    monkeypatch.setattr(xr, "find_rollout", lambda sid: roll)
    monkeypatch.setattr(xr.sys, "stdin", open("/dev/null"))
    assert xr.run(["-s", "read-only", "do X"]) == 3                       # exit code passes through
    row = json.loads((_home / "xval_runs.jsonl").read_text().splitlines()[-1])
    assert row["rollout_ok"] is True and row["ok"] is False and "obs" in row


def test_dry_run_and_cli_dispatch(capsys):
    from apex_router.cli import main
    assert main(["xval", "--dry-run", "-m", "g", "-s", "read-only", "review src/a.py"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["category"] == "files" and out["arm"] in xr.ARMS
    assert out["cmd"][:3] == ["codex", "exec", "-c"]
