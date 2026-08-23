"""WP4 acceptance: ON included/SKIP dropped; ε re-includes; retrieve always on."""
import random
from apex_router.chain_planner import plan, proposed_slots, render_rationale, SlotDecision


def _analyze(opus="ON", kimi="SKIP", cls="algo"):
    return {
        f"validate:{cls}": {"verdict": "ON", "mean_delta": 0.2, "ci": [0.1, 0.3], "n_chains": 20},
        f"deepen:{cls}": {"verdict": opus, "mean_delta": 0.18, "ci": [0.09, 0.27], "n_chains": 23},
        f"synthesize:{cls}": {"verdict": kimi, "mean_delta": 0.03, "ci": [-0.01, 0.02], "n_chains": 23},
    }


def test_on_included_skip_dropped():
    decisions = plan("algo", _analyze(opus="ON", kimi="SKIP"), eps=0.0, rng=random.Random(0))
    slots = proposed_slots(decisions)
    assert "deepen" in slots and "synthesize" not in slots   # kimi (SKIP) dropped
    assert "retrieve" in slots                                # always on


def test_eps_reincludes_a_dropped_slot_as_exploration():
    decisions = plan("algo", _analyze(kimi="SKIP"), eps=1.0, rng=random.Random(0))
    synth = [d for d in decisions if d.slot == "synthesize"][0]
    assert synth.action == "include" and synth.exploration is True
    assert "synthesize" in proposed_slots(decisions)


def test_eps_never_removes_an_ON_slot():
    decisions = plan("algo", _analyze(opus="ON", kimi="SKIP"), eps=1.0, rng=random.Random(0))
    deepen = [d for d in decisions if d.slot == "deepen"][0]
    assert deepen.action == "include" and deepen.exploration is False  # ON untouched


def test_cold_start_is_labeled_prior():
    decisions = plan("newclass", {}, eps=0.0)
    assert all(d.action == "include" for d in decisions)
    assert any("prior" in d.note for d in decisions)
    r = render_rationale("newclass", decisions)
    assert "prior" in r


def test_rationale_mentions_offered():
    decisions = plan("algo", _analyze(opus="ON", kimi="OFFERED"), eps=0.0)
    r = render_rationale("algo", decisions, est_cost=0.07, est_latency_s=22)
    assert "available if wanted" in r and "$0.07" in r
