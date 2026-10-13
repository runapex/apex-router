"""Experiment E-D readout (worldmodel.ed): ITT per task type, A_e at matched tokens, verdict card."""
from __future__ import annotations

import json
import random

from apex_router.worldmodel import cli as C
from apex_router.worldmodel import ed as E


def _tasks(n_per_arm=40, p_w0=0.5, p_w2=0.8, tok_w0=1000.0, tok_w2=1500.0, seed=3, ttype="fix"):
    rng = random.Random(seed)
    out = []
    for arm, p, tok in (("W0", p_w0, tok_w0), ("W2", p_w2, tok_w2)):
        for i in range(n_per_arm):
            out.append({"task": f"s:{arm}{i}", "sid": f"sess-{i // 5}", "task_type": ttype, "ed_arm": arm,
                        "ed_tok": tok * (0.5 + rng.random()),
                        "outcome": "success" if rng.random() < p else "fail",
                        "workflow": arm if rng.random() < 0.9 else ("W0" if arm == "W2" else "W2"),
                        "steps": 5, "sub_steps": 3 if arm == "W2" else 0})
    return out


def test_units_filter_unassigned_and_unknown():
    tasks = _tasks(5) + [{"ed_arm": None, "outcome": "success"}, {"ed_arm": "W2", "outcome": "unknown"},
                         {"ed_arm": "W1", "outcome": "success"}]
    assert len(E._units(tasks)) == 10
    assert E._units([{"ed_arm": "W0", "outcome": "fail", "ed_tok": 0}])[0]["tok"] is None


def test_readout_card_and_rule():
    card = E.readout(_tasks(60), min_n=30, n_boot=300)
    assert card["schema"] == 1 and card["meta"]["experiment"] == "E-D"
    assert card["arms"]["W0"]["n"] == 60 and card["arms"]["W2"]["n"] == 60
    assert card["diagnostics"]["sessions"] == 12 and card["diagnostics"]["judged"] == ["fix"]
    fix = card["diagnostics"]["types"]["fix"]
    assert fix["W2"]["rate"] > fix["W0"]["rate"]
    assert card["contrasts"][0]["status"] in ("better", "no detectable difference")
    assert fix["a_e"] is not None and fix["a_e_ci"][0] is not None and fix["a_e_ci"][0] <= fix["a_e"] <= fix["a_e_ci"][1]
    assert fix["rule"] in ("keep W2", "keep W0", "no detectable difference")
    assert 0.8 <= fix["W0"]["compliance"] <= 1.0
    text = E.render(card)
    assert "E-D (W0 vs W2" in text and "fix" in text and "compliance" in text
    json.loads(E.to_json(card))


def test_small_n_is_inconclusive_and_no_tokens_is_inconclusive():
    card = E.readout(_tasks(10), min_n=30, n_boot=100)
    assert card["contrasts"][0]["status"] == "INCONCLUSIVE"
    assert card["diagnostics"]["types"]["fix"]["rule"] == "INCONCLUSIVE"
    assert card["decision"] == "INCONCLUSIVE"
    notok = [{**t, "ed_tok": None} for t in _tasks(40)]
    c2 = E.readout(notok, min_n=30, n_boot=100)
    assert c2["diagnostics"]["types"]["fix"]["a_e"] is None
    assert c2["diagnostics"]["types"]["fix"]["rule"] == "INCONCLUSIVE"
    assert c2["arms"]["W0"]["mean_tok"] is None
    assert c2["decision"] == "INCONCLUSIVE" and c2["kills"]["no_type_with_A_e_above_1"] is False
    assert E.readout([], n_boot=10)["decision"] == "INCONCLUSIVE"


def test_a_e_direction():
    # W2 twice the success at the same tokens: A_e ≈ 2, interval above 1 → keep W2
    tasks = _tasks(80, p_w0=0.4, p_w2=0.8, tok_w0=1000.0, tok_w2=1000.0, seed=5)
    card = E.readout(tasks, min_n=30, n_boot=400)
    fix = card["diagnostics"]["types"]["fix"]
    assert 1.4 < fix["a_e"] < 2.8 and fix["rule"] == "keep W2"
    assert card["diagnostics"]["keep_w2_on"] == ["fix"]
    assert card["kills"]["no_type_with_A_e_above_1"] is False
    assert card["decision"] == "adopt W2 on fix; W0 elsewhere"
    # W2 same success at twice the tokens: A_e ≈ 0.5 → keep W0
    tasks = _tasks(80, p_w0=0.6, p_w2=0.6, tok_w0=1000.0, tok_w2=2000.0, seed=7)
    card = E.readout(tasks, min_n=30, n_boot=400)
    fix = card["diagnostics"]["types"]["fix"]
    assert fix["a_e"] < 0.8 and fix["rule"] == "keep W0"
    assert card["decision"] == "stay on W0 (kill: no_type_with_A_e_above_1)"
    assert card["kills"]["no_type_with_A_e_above_1"] is True


def test_decision_follows_the_a_e_rule_not_the_success_contrast():
    # W2 wins on success but costs far more tokens: success contrast "better", rule not "keep W2"
    tasks = _tasks(80, p_w0=0.4, p_w2=0.8, tok_w0=1000.0, tok_w2=4000.0, seed=11)
    card = E.readout(tasks, min_n=30, n_boot=400)
    assert card["contrasts"][1]["status"] == "better"
    assert card["diagnostics"]["types"]["fix"]["rule"] in ("keep W0", "no detectable difference")
    assert not card["decision"].startswith("adopt")


def test_w0_without_successes_is_unbounded_not_inconclusive():
    tasks = _tasks(50, p_w0=0.0, p_w2=0.9, seed=13)
    card = E.readout(tasks, min_n=30, n_boot=200)
    fix = card["diagnostics"]["types"]["fix"]
    assert fix["a_e"] is None and fix["a_e_unbounded"] is True and fix["rule"] == "keep W2"
    assert "∞" in E.render(card)
    json.loads(E.to_json(card))
    # one W0 success in 40: the infinite draws stay in the upper tail (hi unbounded), lo finite
    tasks = _tasks(50, p_w0=0.0, p_w2=0.9, seed=13)
    tasks[0]["outcome"] = "success"
    fix = E.readout(tasks, min_n=30, n_boot=300)["diagnostics"]["types"]["fix"]
    assert fix["a_e"] is not None and fix["a_e_unbounded"] is True and fix["a_e_ci"][1] is None


def test_min_sessions_gates_the_verdict():
    tasks = _tasks(80, p_w0=0.4, p_w2=0.8, tok_w0=1000.0, tok_w2=1000.0, seed=5)
    for t in tasks:
        t["sid"] = "one" if t["ed_arm"] == "W0" else "two"            # 160 tasks, 2 sessions
    card = E.readout(tasks, min_n=30, n_boot=100)
    assert card["diagnostics"]["types"]["fix"]["rule"] == "INCONCLUSIVE" and card["decision"] == "INCONCLUSIVE"
    assert E.readout(tasks, min_n=30, min_sessions=2, n_boot=100)["diagnostics"]["types"]["fix"]["rule"] == "keep W2"


def test_cli_ed(tmp_path, capsys, monkeypatch):
    home = tmp_path / "ar"
    (home / "worldmodel").mkdir(parents=True)
    (home / "worldmodel" / "tasks.jsonl").write_text("\n".join(json.dumps(t) for t in _tasks(35)) + "\n")
    assert C.main(["ed", "--home", str(home), "--min-n", "30"]) == 0
    out = capsys.readouterr().out
    assert "E-D (W0 vs W2" in out and "units 70" in out
    assert C.main(["ed", "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["meta"]["experiment"] == "E-D"
    assert C.main([]) == 0
    assert ",ed" in capsys.readouterr().out
