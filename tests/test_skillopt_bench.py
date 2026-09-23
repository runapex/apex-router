"""Skill-quality benchmark harness — the SkillOpt borrow behind apex's gate.

Pins the load-bearing behaviors: a real quality lift PROMOTES only after replicating across
>= m_windows; a no-effect or harmful skill NEVER promotes; grading is objective (executable
tests); the over-time ledger accumulates windows.
"""
from __future__ import annotations

import json

from apex_router import skillopt_bench as sb

_TESTS = "def test_add():\n    assert add(2,3)==5\n    assert add(-1,1)==0"


def _tasks(n=8):
    return [sb.SkillTask(id=f"t{i}", spec="Write add(a,b) returning a+b", tests=_TESTS)
            for i in range(n)]


def _lift_model(msgs, arm):
    # baseline wrong, skill right → clean +1.0 separation
    return "def add(a,b):\n    return a+b" if arm != sb.BASELINE_ID else "def add(a,b):\n    return a-b"


def _run_windows(skill_id, model_call, windows, tasks_per_window=None, tasks=None,
                 skill_text="guidance", model_id="frozen-m"):
    """Run a campaign. By default each window gets DISTINCT tasks (real replication). Pass
    `tasks` to force the SAME task set every window (to exercise the pseudoreplication guard)."""
    rows = []
    for i, w in enumerate(windows):
        if tasks is not None:
            wt = tasks
        elif tasks_per_window is not None:
            wt = tasks_per_window[i]
        else:
            # distinct task ids per window → genuine fresh evidence
            wt = [sb.SkillTask(id=f"{w}_t{j}", spec="Write add(a,b) returning a+b", tests=_TESTS)
                  for j in range(8)]
        rows += sb.run_skill_bench(wt, skill_id=skill_id, skill_text=skill_text,
                                   model_id=model_id, model_call=model_call, window_id=w)
    return rows


def test_real_lift_promotes_after_replication():
    # DISTINCT tasks per window = genuine fresh evidence across 2 windows.
    rows = _run_windows("skill:v1", _lift_model, ["w1", "w2"])
    v = sb.grade_skill(rows, skill_id="skill:v1", k=2, m_windows=2)
    assert v.promoted is True, v.reason
    assert v.mean_delta == 1.0
    assert v.ci_low > 0.0
    assert v.windows_seen == 2


def test_single_window_lift_is_held_back_by_replication():
    # A genuine lift in ONE window must NOT promote — the over-time requirement (>=2 windows).
    rows = _run_windows("skill:v1", _lift_model, ["only"])
    v = sb.grade_skill(rows, skill_id="skill:v1", k=2, m_windows=2)
    assert v.promoted is False
    assert "window" in v.reason.lower()


def test_pseudoreplication_same_tasks_across_windows_is_rejected():
    # astra F4: re-running the SAME tasks in multiple windows is NOT fresh evidence. The base
    # bench's duplicate-(step_id, model) guard must reject it, not promote.
    rows = _run_windows("skill:v1", _lift_model, ["w1", "w2"], tasks=_tasks())
    v = sb.grade_skill(rows, skill_id="skill:v1", k=2, m_windows=2)
    assert v.promoted is False
    assert "pseudoreplication" in v.reason.lower()


def test_candidate_only_window_does_not_satisfy_replication():
    # astra F3: a window with only the skill arm (baseline row dropped) must NOT count toward
    # replication. One real paired window + one candidate-only window => not 2 windows.
    good = _run_windows("skill:v1", _lift_model, ["w1"])  # real paired window
    # forge a candidate-only second window: skill rows only, no baseline
    extra = [r for r in _run_windows("skill:v1", _lift_model, ["w2"]) if r["model"] != sb.BASELINE_ID]
    v = sb.grade_skill(good + extra, skill_id="skill:v1", k=2, m_windows=2)
    assert v.promoted is False, "a candidate-only window must not satisfy 2-window replication"


def test_changed_skill_bytes_do_not_merge_into_old_campaign():
    # astra F5: evidence is bound to skill BYTES. A harmful new revision must not promote on an
    # old winner's windows just because the skill_id name matches.
    win_rows = _run_windows("skill:v1", _lift_model, ["w1", "w2"], skill_text="GOOD-REV")

    def harmful(msgs, arm):
        return "def add(a,b):\n    return a-b" if arm != sb.BASELINE_ID else "def add(a,b):\n    return a+b"
    bad_rows = _run_windows("skill:v1", harmful, ["w3", "w4"], skill_text="HARMFUL-REV")
    # different skill_text => different corpus_snapshot => the rows carry different snapshots;
    # grading the combined set cannot promote the harmful revision on the good one's windows.
    v = sb.grade_skill(win_rows + bad_rows, skill_id="skill:v1", k=2, m_windows=2)
    assert v.promoted is False


def test_complementary_cross_window_failure_does_not_promote():
    # pass2 F1: a task whose baseline lands in one window and skill in another must NOT pair
    # across windows to fake replication. One real paired window + split complementary rows
    # for shared tasks across two other windows must be rejected by the window-isolation guard.
    w1 = _run_windows("skill:v1", _lift_model, ["w1"])  # one real paired window
    shared = [sb.SkillTask(id=f"sh_t{j}", spec="Write add(a,b) returning a+b", tests=_TESTS)
              for j in range(4)]
    r2 = sb.run_skill_bench(shared, skill_id="skill:v1", skill_text="guidance", model_id="frozen-m",
                            model_call=_lift_model, window_id="w2")
    base_only = [r for r in r2 if r["model"] == sb.BASELINE_ID]
    r3 = sb.run_skill_bench(shared, skill_id="skill:v1", skill_text="guidance", model_id="frozen-m",
                            model_call=_lift_model, window_id="w3")
    cand_only = [dict(r, window_id="w3") for r in r3 if r["model"] != sb.BASELINE_ID]
    v = sb.grade_skill(w1 + base_only + cand_only, skill_id="skill:v1", k=2, m_windows=2)
    assert v.promoted is False
    assert "window" in v.reason.lower()


def test_mixed_campaign_identity_is_rejected():
    # pass2 F5: promotion evidence from one revision + confirmation from another (different
    # corpus_snapshot) must not merge into one campaign.
    good = _run_windows("skill:v1", _lift_model, ["w1", "w2"], skill_text="REV-A")
    other = _run_windows("skill:v1", _lift_model, ["w3", "w4"], skill_text="REV-B")
    v = sb.grade_skill(good + other, skill_id="skill:v1", k=2, m_windows=2)
    assert v.promoted is False
    assert "campaign" in v.reason.lower() or "window" in v.reason.lower()


def test_family_dedups_duplicate_skill_ids():
    # pass2 F6: listing a skill N times must not give it N BH slots.
    rows = _run_windows("skill:real", _lift_model, ["w1", "w2"])
    verdicts = sb.grade_skills_family(rows, ["skill:real", "skill:real", "skill:real"],
                                      k=2, m_windows=2)
    assert len(verdicts) == 1


def test_no_effect_skill_never_promotes():
    def noop(msgs, arm):
        return "def add(a,b):\n    return a+b"  # both arms pass → Δ=0
    rows = _run_windows("skill:noop", noop, ["w1", "w2"])
    v = sb.grade_skill(rows, skill_id="skill:noop", k=2, m_windows=2)
    assert v.promoted is False
    assert v.mean_delta == 0.0


def test_harmful_skill_never_promotes():
    def hurt(msgs, arm):
        # skill arm WRONG, baseline right → Δ=-1.0
        return "def add(a,b):\n    return a-b" if arm != sb.BASELINE_ID else "def add(a,b):\n    return a+b"
    rows = _run_windows("skill:bad", hurt, ["w1", "w2"])
    v = sb.grade_skill(rows, skill_id="skill:bad", k=2, m_windows=2)
    assert v.promoted is False
    assert v.mean_delta == -1.0


def test_family_fdr_corrects_across_many_skills():
    # astra F6: many skills tried together = a multiple-comparison campaign. grade_skills_family
    # runs the gate over ALL cells so BH-FDR is real. A real winner among null skills still holds;
    # the nulls do not promote.
    rows = []
    rows += _run_windows("skill:real", _lift_model, ["w1", "w2"])
    for nid in ["skill:n1", "skill:n2", "skill:n3"]:
        rows += _run_windows(nid, lambda m, a: "def add(a,b):\n    return a+b", ["w1", "w2"])
    verdicts = {v.skill_id: v for v in sb.grade_skills_family(
        rows, ["skill:real", "skill:n1", "skill:n2", "skill:n3"], k=2, m_windows=2)}
    assert verdicts["skill:real"].promoted is True
    assert all(not verdicts[n].promoted for n in ["skill:n1", "skill:n2", "skill:n3"])


def test_grading_is_objective_executable():
    # The score must come from RUNNING the tests, not from the answer's plausibility.
    # A confidently-worded but WRONG answer scores 0.
    def confident_wrong(msgs, arm):
        return "# This is definitely correct.\ndef add(a,b):\n    return a*b"
    rows = sb.run_skill_bench(_tasks(4), skill_id="skill:x", skill_text="s", model_id="m",
                              model_call=confident_wrong, window_id="w1")
    skill_rows = [r for r in rows if r["model"] == "skill:x"]
    assert all(r["outcome"]["score"] == 0.0 for r in skill_rows)


def test_skill_injected_only_on_candidate_arm():
    seen = {"baseline": [], "skill": []}

    def spy(msgs, arm):
        key = "skill" if arm != sb.BASELINE_ID else "baseline"
        seen[key].append(msgs)
        return "def add(a,b):\n    return a+b"

    sb.run_skill_bench(_tasks(2), skill_id="skill:v1", skill_text="MAGIC_SKILL_TEXT", model_id="m",
                       model_call=spy, window_id="w1")
    # baseline arm: no system message with the skill; candidate arm: skill present
    assert all(not any("MAGIC_SKILL_TEXT" in m.get("content", "") for m in msgs)
               for msgs in seen["baseline"])
    assert all(any("MAGIC_SKILL_TEXT" in m.get("content", "") for m in msgs)
               for msgs in seen["skill"])


def test_ledger_accumulates_windows(tmp_path):
    ledger = tmp_path / "skill_ledger.jsonl"
    rows = _run_windows("skill:v1", _lift_model, ["w1", "w2"])
    v = sb.grade_skill(rows, skill_id="skill:v1", k=2, m_windows=2)
    sb.append_ledger(ledger, v, window_id="w2")
    sb.append_ledger(ledger, v, window_id="w3")
    lines = [json.loads(x) for x in ledger.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["skill_id"] == "skill:v1"
    assert lines[0]["promoted"] is True
    assert lines[1]["window_id"] == "w3"


def test_load_tasks_reuses_codegen_format(tmp_path):
    p = tmp_path / "b.jsonl"
    p.write_text(json.dumps({"id": "x", "spec": "spec", "tests": _TESTS}) + "\n")
    tasks = sb.load_tasks(p)
    assert len(tasks) == 1 and tasks[0].id == "x"
