"""WP2 acceptance: cosine skip avoids judge; swapped (+0.4,-0.2) -> reward 0.1."""
from apex_router.chain_reward import judge_pair, cosine_pregate, compute_rewards


def test_swapped_judge_averages():
    calls = []
    def judge_fn(model, prompt):
        calls.append(model); return 0.4 if len(calls) == 1 else -0.2
    r = judge_pair("prev", "cur", judge_fn=judge_fn)
    assert abs(r - 0.1) < 1e-9
    assert calls == ["anthropic/claude-haiku-4-5", "anthropic/claude-haiku-4-5"]  # pinned, twice


def test_cosine_pregate_skips_judge_when_unchanged():
    # identical outputs -> embed vectors identical -> cosine 1.0 -> skip
    def embed_fn(t): return [1.0, 0.0, 0.0]
    assert cosine_pregate("same", "same", embed_fn=embed_fn) is True

    judged = {"n": 0}
    def judge_fn(model, prompt):
        judged["n"] += 1; return 0.5
    records = [
        {"kind": "chain", "chain_id": "c1", "task_class": "algo"},
        {"kind": "stage", "chain_id": "c1", "slot": "retrieve", "model": "local", "prompt_tokens": 1},
        {"kind": "stage", "chain_id": "c1", "slot": "deepen", "model": "opus", "prompt_tokens": 2},
    ]
    outputs = {("c1", "retrieve"): "identical", ("c1", "deepen"): "identical"}
    rows = compute_rewards(records, outputs, judge_fn=judge_fn, embed_fn=embed_fn)
    assert judged["n"] == 0                       # judge never invoked (skipped)
    assert len(rows) == 1 and rows[0]["reward"] == 0.0
    assert rows[0]["cell_id"] == "deepen:algo" and rows[0]["arm"] == "candidate"


def test_compute_rewards_judges_changed_output():
    def embed_fn(t): return [1.0, 0.0] if t.startswith("a") else [0.0, 1.0]  # orthogonal -> not skipped
    def judge_fn(model, prompt): return 0.6
    records = [
        {"kind": "chain", "chain_id": "c1", "task_class": "algo"},
        {"kind": "stage", "chain_id": "c1", "slot": "retrieve", "model": "local"},
        {"kind": "stage", "chain_id": "c1", "slot": "deepen", "model": "opus"},
    ]
    outputs = {("c1", "retrieve"): "aaa", ("c1", "deepen"): "bbb"}
    rows = compute_rewards(records, outputs, judge_fn=judge_fn, embed_fn=embed_fn)
    assert len(rows) == 1 and abs(rows[0]["reward"] - 0.6) < 1e-9
