"""WP5 acceptance: both orders produce distinguishable replay rows the gate can consume."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from apex_router.chain_replay import replay_both_orders, PayloadLogger
from chain_bench import analyze


def test_both_orders_distinguishable_and_flagged():
    def call_fn(model, prompt):
        return {"output": f"{model}-out-{len(prompt)}", "usage": {"prompt_tokens": 3}, "cost_usd": 0.01}
    def judge_fn(model, prompt):
        return 0.5
    def embed_fn(t):  # orthogonal by first char -> never skipped
        return [1.0, 0.0] if t[:1] == "a" else [0.0, 1.0]

    rows = replay_both_orders("solve X", ("validate", "sonnet"), ("deepen", "opus"),
                              "algo", call_fn=call_fn, judge_fn=judge_fn, embed_fn=embed_fn)
    orders = {r["order"] for r in rows}
    assert orders == {"AB", "BA"}                       # both orders present
    assert all(r["replay"] is True for r in rows)       # every replay row flagged
    # rows are valid SC2 rows the gate consumes like any cell
    res = analyze(rows, k=1, m_windows=1)
    assert res and all("verdict" in r for r in res)


def test_payload_logger_fail_open(tmp_path):
    pl = PayloadLogger(tmp_path / "payloads.jsonl")
    assert pl.log("c1", "deepen", "in", "out") is True
    assert (tmp_path / "payloads.jsonl").read_text().count("chain_id") == 1
    # unwritable path -> False, no raise
    assert PayloadLogger("/proc/nope/x.jsonl").log("c", "s", "i", "o") is False
