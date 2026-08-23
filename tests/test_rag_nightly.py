"""WP6/WP7 acceptance: promotion by L1 gate; book-derived RAG-only; 3-part trigger."""
import json

from apex_router.rag_nightly import (
    Candidate, harvest, stage, l1_gate, should_train, approved_count,
)


def _cand(ans="corrected", source="learn_chain", cls="algorithms"):
    return Candidate(messages=[{"role": "user", "content": "q"}], corrected_answer=ans,
                     task_class=cls, source=source)


def test_book_derived_is_rag_only():
    rec = _cand(source="book").to_record()
    assert rec["approved_for_training"] is False
    assert "book" in rec["tags"]
    # first-party stays trainable
    assert _cand(source="learn_chain").to_record()["approved_for_training"] is True


def test_harvest_dedupes(tmp_path):
    a, b = _cand("same"), _cand("same")   # identical content -> one survives
    c = _cand("different")
    out = harvest([lambda: [a, b, c]])
    assert len(out) == 2


def test_l1_gate_promotes_only_improvers(tmp_path):
    approved = tmp_path / "approved.jsonl"
    good = _cand("good-answer")
    bad = _cand("bad-answer")

    # mocked rag_eval: 'good' lowers held-out escalation (improvement>0), 'bad' doesn't
    def measure(rec):
        return 0.2 if "good" in rec["corrected_answer"] else -0.1

    promoted = l1_gate([good, bad], measure_fn=measure, approved_path=approved)
    assert len(promoted) == 1
    lines = [json.loads(l) for l in approved.read_text().splitlines()]
    assert len(lines) == 1 and "good" in lines[0]["corrected_answer"]
    assert approved_count(approved) == 1


def test_deidentification_applied():
    c = Candidate(messages=[], corrected_answer="mail me a@b.com with sk-ABCDEFGH12345678",
                  task_class="x", source="manual")
    rec = c.to_record()
    assert "<email>" in rec["corrected_answer"] and "<token>" in rec["corrected_answer"]
    assert rec["deidentified"] is True


def test_should_train_three_part_gate():
    def cyc(ci_upper, crowding):
        return {"kind": "cycle", "l1_ci": [0.0, ci_upper], "crowding": crowding}

    saturated3 = [cyc(0.01, 0.35), cyc(0.015, 0.35), cyc(0.02, 0.35)]
    # 1) too few approved
    assert should_train(saturated3, approved=499) is False
    # 2) only 2 saturated cycles
    assert should_train(saturated3[:2], approved=600) is False
    # 3) not crowded
    assert should_train([cyc(0.01, 0.25)] * 3, approved=600) is False
    # all three satisfied
    assert should_train(saturated3, approved=600) is True
    # never-fires path is fine (RAG-only forever = success)
    assert should_train([], approved=10) is False
