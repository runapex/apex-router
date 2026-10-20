"""C2 (label half): label rows carry model and tier when the task has them."""
from __future__ import annotations

import json

from apex_router import labels as L
from test_labels import _home, _pi_session  # noqa: F401  (autouse fixtures live in conftest)


def _with_model(p, model):
    out = []
    for ln in p.read_text().splitlines():
        d = json.loads(ln)
        m = d.get("message") or {}
        if m.get("role") == "assistant":
            m["model"] = model
        out.append(json.dumps(d))
    p.write_text("\n".join(out) + "\n")


def test_label_rows_carry_model_and_tier():
    h = _home()
    p = _pi_session(h, "2026-10-07_cccccccc-0000-0000-0000-000000000003", [
        ("fix it", [("bash", "pytest", False, "3 passed")], "ok")])
    _with_model(p, "claude-sonnet-5")
    res = L.build(log=None)
    row = json.loads((L.home() / "tasks.jsonl").read_text().splitlines()[0])
    lab = json.loads((L.home() / "labels.jsonl").read_text().splitlines()[0])
    assert row["model"] == "claude-sonnet-5" and lab["model"] == "claude-sonnet-5"
    from apex_router.route_log import tier_of
    assert tier_of("claude-sonnet-5") and lab["tier"] == tier_of("claude-sonnet-5")
    assert res["labels"][0]["model"] == "claude-sonnet-5"


def test_unknown_tier_keeps_model_only_and_old_rows_stay_valid():
    h = _home()
    p = _pi_session(h, "2026-10-07_dddddddd-0000-0000-0000-000000000004", [
        ("fix it", [("bash", "pytest", False, "3 passed")], "ok")])
    _with_model(p, "qwen3.8:27b-mlx")
    lab = L.build(log=None)["labels"][0]
    assert lab["model"] == "qwen3.8:27b-mlx" and "tier" not in lab
    # an old stored row (no model, no tier) relabels without the new keys
    old = {"id": "x1", "source": "pi", "session": "s", "index": 0, "n_calls": 1, "n_errors": 0,
           "has_next": False, "votes": {"tests": "success"}}
    out = L.relabel([old], write=False)["labels"][0]
    assert "model" not in out and "tier" not in out and out["id"] == "x1"
