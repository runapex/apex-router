"""WP1 acceptance: both record kinds; edited/proposed!=executed; fail-open mid-run."""
import json

from apex_router.learn_chain import ChainLogger, Slot, run_chain


def _read(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_records_both_kinds_and_edit_flags(tmp_path):
    log = tmp_path / "learn_chain.jsonl"
    logger = ChainLogger(log)
    proposed = [Slot("retrieve", "local/x"), Slot("deepen", "anthropic/opus"), Slot("synthesize", "moonshotai/kimi")]
    executed = [Slot("retrieve", "local/x"), Slot("deepen", "anthropic/opus")]  # user dropped kimi

    def call_fn(model, prompt):
        return {"output": f"out::{model}", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 2}, "cost_usd": 0.01}

    cid = run_chain("explain X", "algorithms/learn", executed, call_fn,
                    proposed=proposed, edited=True, shown_rationale=True, logger=logger)

    rows = _read(log)
    stages = [r for r in rows if r["kind"] == "stage"]
    chains = [r for r in rows if r["kind"] == "chain"]
    assert len(stages) == 2 and len(chains) == 1
    assert all(r["chain_id"] == cid for r in rows)
    ch = chains[0]
    assert ch["edited"] is True and ch["shown_rationale"] is True
    assert ch["proposed"] == ["retrieve", "deepen", "synthesize"]
    assert ch["executed"] == ["retrieve", "deepen"]
    assert ch["proposed"] != ch["executed"]
    # stage usage captured
    assert stages[0]["prompt_tokens"] == 10 and stages[0]["cost_usd"] == 0.01
    assert stages[0]["input_hash"] and stages[0]["output_hash"]


def test_fail_open_on_midrun_error(tmp_path):
    log = tmp_path / "learn_chain.jsonl"
    logger = ChainLogger(log)
    slots = [Slot("retrieve", "local/x"), Slot("deepen", "anthropic/opus")]
    calls = {"n": 0}

    def call_fn(model, prompt):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("network died")
        return {"output": "ok", "usage": {}, "cost_usd": 0.0}

    try:
        run_chain("t", "cls", slots, call_fn, logger=logger)
    except ConnectionError:
        pass  # the chain surfaces the error, but partial stages must be logged

    rows = _read(log)
    stages = [r for r in rows if r["kind"] == "stage"]
    chains = [r for r in rows if r["kind"] == "chain"]
    assert len(stages) == 1                      # the completed first stage persisted
    assert len(chains) == 1                      # chain record still written (finally)
    assert chains[0]["executed"] == ["retrieve"]  # only the completed slot


def test_logger_never_raises_on_bad_path():
    # unwritable path -> returns False, no exception (fail-open like route_log)
    logger = ChainLogger("/proc/nonexistent/cannot/write.jsonl")
    assert logger.log_chain("c", "cls", [], []) is False
