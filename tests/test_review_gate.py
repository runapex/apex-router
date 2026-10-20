"""review_gate: B3 records (no text, idempotent, APEX_HOME) and B1 gate (ACCEPT/HOLD) incl. UX-loop replay."""
from __future__ import annotations

import importlib
import json

import pytest

OPUS, SONNET, GPT = "it-entra-claude-opus-5-5", "it-entra-claude-sonnet-5-5", "it-entra-gpt-6-astra"
QWEN = "qwen3-coder:30b"


@pytest.fixture
def rg(tmp_path, monkeypatch):
    monkeypatch.setenv("APEX_HOME", str(tmp_path))
    import apex_router.review_gate as mod
    return importlib.reload(mod)


def rows(rg):
    return [json.loads(l) for l in rg.out_path().read_text().splitlines()]


# ---------- B3 ----------

def test_vendor_from_model_name(rg):
    assert rg.vendor_of("claude-opus-4") == "anthropic"
    assert rg.vendor_of(OPUS) == "anthropic"
    assert rg.vendor_of("gpt-5") == "openai" and rg.vendor_of("codex") == "openai"
    assert rg.vendor_of(GPT) == "openai"
    assert rg.vendor_of("qwen3:8b") == "local" and rg.vendor_of("ornith-1") == "local"
    assert rg.vendor_of("mystery") == "unknown" and rg.vendor_of(None) == "unknown"


def test_path_respects_apex_home(rg, tmp_path):
    assert rg.out_path() == tmp_path / "labels" / "review_loops.jsonl"


def test_record_writes_enums_and_counts_only(rg):
    assert rg.record_review("L1", 1, OPUS, "NO-GO", author=GPT, code_access=True, confirmed="F1,F2", rejected="3")
    (r,) = rows(rg)
    assert r["kind"] == "review" and r["verdict"] == "NO-GO" and r["vendor"] == "anthropic" and r["author_vendor"] == "openai"
    assert r["confirmed"] == 2 and r["confirmed_ids"] == ["F1", "F2"] and r["rejected"] == 3 and "rejected_ids" not in r
    assert r["code_access"] is True


def test_record_is_idempotent_and_latest_wins(rg):
    assert rg.record_review("L1", 1, OPUS, "NO-GO") is True
    assert rg.record_review("L1", 1, OPUS, "NO-GO") is False
    assert rg.record_review("L1", 1, OPUS, "GO") is True      # reviewer changed the verdict: new row
    assert rg.record_review("L1", 1, OPUS, "NO-GO") is True   # and back: not skipped as "seen"
    assert len(rows(rg)) == 3
    assert rg.record_tests("L1", 1, True) and not rg.record_tests("L1", 1, True)
    assert rg.record_regress("L1", "F1", "test_a") and not rg.record_regress("L1", "F1", "test_a")
    assert rg.record_post_go("L1", 2) and not rg.record_post_go("L1", 2) and rg.record_post_go("L1", 3)


def test_rejects_free_text_and_bad_input(rg):
    for bad in ("two words", "SECRET text /srv/private/x", "a;b", ""):
        with pytest.raises(ValueError):
            rg.record_review("L1", 1, OPUS, "GO", confirmed=bad)
        with pytest.raises(ValueError):
            rg.record_regress("L1", "F1", bad)
    with pytest.raises(ValueError):
        rg.record_review("L1", 1, OPUS, "MAYBE")
    with pytest.raises(ValueError):
        rg.record_review("L1", 0, OPUS, "GO")
    with pytest.raises(ValueError):
        rg.record_post_go("L1", -1)
    assert not rg.out_path().exists()


def test_cli_commands_and_exit_codes(rg, capsys):
    base = ["record", "--loop", "L1", "--rev", "1", "--reviewer", OPUS, "--verdict", "GO", "--code-access", "--confirmed", "2"]
    assert rg.main(base) == 0 and capsys.readouterr().out.strip() == "recorded"
    assert rg.main(base) == 0 and capsys.readouterr().out.strip() == "unchanged"
    assert rg.main(["tests", "--loop", "L1", "--rev", "1", "--failed"]) == 0
    assert rg.main(["regress", "--loop", "L1", "--finding", "F1", "--test", "tests/t.py::test_x"]) == 0
    assert rg.main(["post-go", "--loop", "L1", "--defects", "4"]) == 0
    assert rg.main(["record", "--loop", "bad id", "--rev", "1", "--reviewer", OPUS, "--verdict", "GO"]) == 2
    assert {r["kind"] for r in rows(rg)} == {"review", "tests", "regress", "post_go"}


def test_old_or_broken_rows_are_ignored(rg):
    p = rg.out_path()
    p.parent.mkdir(parents=True)
    p.write_text('not json\n{"kind": "other"}\n[1]\n')
    assert rg.check("L1")["reasons"] == ["no_records"]
    assert rg.report()["loops"] == []


# ---------- B1 ----------

def _seed(rg, loop, author, reviews, passed=True, rev=1):
    for reviewer, verdict, code in reviews:
        rg.record_review(loop, rev, reviewer, verdict, author=author, code_access=code)
    rg.record_tests(loop, rev, passed)


def test_accept_needs_cross_vendor_go_with_code_access_and_tests(rg):
    _seed(rg, "L", OPUS, [(GPT, "GO", True)])
    d = rg.check("L")
    assert d["decision"] == "ACCEPT" and d["reasons"] == [] and d["rev"] == 1


@pytest.mark.parametrize("reviewer,code,why", [
    (SONNET, True, "same_vendor_go_only"),     # same vendor as the author
    ("claude-haiku-4", True, "same_vendor_go_only"),
    ("gpt-5-mini", True, "cheap_go_only"),     # cheap cross-vendor
    (QWEN, True, "cheap_go_only"),             # local model is cheap
    (GPT, False, "no_code_access"),
    ("mystery-model", True, "reviewer_vendor_unknown"),
])
def test_single_weak_go_holds_with_reason(rg, reviewer, code, why):
    _seed(rg, "L", OPUS, [(reviewer, "GO", code)])
    d = rg.check("L")
    assert d["decision"] == "HOLD" and "no_qualified_go" in d["reasons"] and why in d["reasons"]


def test_go_with_changes_is_not_a_go(rg):
    _seed(rg, "L", OPUS, [(GPT, "GO-WITH-CHANGES", True)])
    d = rg.check("L")
    assert d["decision"] == "HOLD" and "no_go" in d["reasons"]


def test_unknown_author_vendor_fails_closed(rg):
    rg.record_review("L", 1, GPT, "GO", code_access=True)   # no --author
    rg.record_tests("L", 1, True)
    d = rg.check("L")
    assert d["decision"] == "HOLD" and "author_unknown" in d["reasons"]


def test_tests_missing_or_failed_hold(rg):
    rg.record_review("L", 1, GPT, "GO", author=OPUS, code_access=True)
    assert "tests_missing" in rg.check("L")["reasons"]
    rg.record_tests("L", 1, False)
    assert rg.check("L")["reasons"] == ["tests_failed"]
    rg.record_tests("L", 1, True)
    assert rg.check("L")["decision"] == "ACCEPT"


def test_open_no_go_blocks_a_qualified_go(rg):
    _seed(rg, "L", OPUS, [(GPT, "GO", True), ("gpt-5", "NO-GO", True)])
    d = rg.check("L")
    assert d["decision"] == "HOLD" and d["reasons"] == ["open_no_go"]


def test_regression_test_needed_for_confirmed_earlier_findings(rg):
    rg.record_review("L", 1, GPT, "NO-GO", author=OPUS, code_access=True, confirmed="F1,F2")
    rg.record_tests("L", 1, True)
    _seed(rg, "L", OPUS, [(GPT, "GO", True)], rev=2)
    d = rg.check("L")
    assert d["decision"] == "HOLD" and d["regression_missing_ids"] == ["F1", "F2"]
    rg.record_regress("L", "F1", "test_f1")
    assert rg.check("L")["regression_missing_ids"] == ["F2"]
    rg.record_regress("L", "F2", "test_f2")
    assert rg.check("L")["decision"] == "ACCEPT"


def test_regression_with_unnamed_findings_uses_counts(rg):
    rg.record_review("L", 1, GPT, "NO-GO", author=OPUS, code_access=True, confirmed=2)
    _seed(rg, "L", OPUS, [(GPT, "GO", True)], rev=2)
    assert rg.check("L")["regression_missing_anon"] == 2
    rg.record_regress("L", "A", "t1")
    assert rg.check("L")["regression_missing_anon"] == 1
    rg.record_regress("L", "B", "t2")
    assert rg.check("L")["decision"] == "ACCEPT"


def test_check_cli_json_and_advisory_exit_code(rg, capsys):
    assert rg.main(["check", "--loop", "none", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "HOLD"
    _seed(rg, "L", OPUS, [(SONNET, "GO", True)])
    assert rg.main(["check", "--loop", "L"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("HOLD") and "same_vendor_go_only" in out


def test_report_loops_and_reviewers(rg):
    rg.record_review("L", 1, GPT, "NO-GO", author=OPUS, code_access=True, confirmed="F1", rejected="F9")
    rg.record_review("L", 1, SONNET, "GO", author=OPUS)
    rg.record_tests("L", 1, True)
    _seed(rg, "L", OPUS, [(GPT, "GO", True)], rev=2)
    rg.record_regress("L", "F1", "t")
    rg.record_post_go("L", 3)
    rg.record_post_go("L", 5)
    rep = rg.report()
    (loop,) = rep["loops"]
    assert loop["revisions"] == 2 and loop["revisions_to_accept"] == 2 and loop["latest_decision"] == "ACCEPT"
    assert loop["confirmed"] == 1 and loop["rejected"] == 1 and loop["post_go_defects"] == 5
    by = {r["reviewer"]: r for r in rep["reviewers"]}
    assert by[GPT]["reviews"] == 2 and by[GPT]["go_rate"] == 0.5 and by[GPT]["precision"] == 0.5
    assert by[SONNET]["go_rate"] == 1.0 and by[SONNET]["precision"] is None


def test_report_cli_runs(rg, capsys):
    _seed(rg, "L", OPUS, [(GPT, "GO", True)])
    assert rg.main(["report"]) == 0 and "L" in capsys.readouterr().out
    assert rg.main(["report", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["loops"][0]["revisions_to_accept"] == 1


# ---------- replay: UX loop, docs/research/ux-rsi/metrics.jsonl ----------
# Revision map: 1 = E1 (round 1), 2 = round 2 (15ffeb9), 3 = E1c (round 3, gpt author),
# 4 = fix 3a (fcb7273), 5 = fix 3b (1a56fb1), 6 = fix 3c (ec781c4). Code access: J3 (xval/codex) and J1 read code;
# J2 round 3 "did not read code". Finding ids are invented labels (R<rev>-<n>); counts come from the metrics file.
# (reviewer, verdict, code_access, confirmed)
UX_AUTHOR = {1: OPUS, 2: OPUS, 3: GPT, 4: OPUS, 5: OPUS, 6: OPUS}
UX_REVIEWS = {
    1: [(OPUS, "GO-WITH-CHANGES", True, ""), (SONNET, "GO-WITH-CHANGES", True, ""), (GPT, "GO-WITH-CHANGES", True, "")],
    2: [(OPUS, "NO-GO", True, "R2-1,R2-2,R2-3,R2-4,R2-5"), (SONNET, "GO", False, ""), (GPT, "NO-GO", True, "R2-1,R2-2,R2-3,R2-4,R2-5")],
    3: [(OPUS, "GO", True, ""), (SONNET, "GO", False, ""), (GPT, "NO-GO", True, "R3-1")],
    4: [(GPT, "NO-GO", True, "R4-1")],
    5: [(GPT, "NO-GO", True, "R5-1,R5-2")],
    6: [(GPT, "GO", True, "")],
}
UX_REGRESS = ["R2-1", "R2-2", "R2-3", "R2-4", "R2-5", "R3-1", "R4-1", "R5-1", "R5-2"]


def _replay(rg, *, regress=True, upto=6):
    for rev in range(1, upto + 1):
        for reviewer, verdict, code, conf in UX_REVIEWS[rev]:
            rg.record_review("ux-rsi", rev, reviewer, verdict, author=UX_AUTHOR[rev], code_access=code, confirmed=conf or None)
        rg.record_tests("ux-rsi", rev, True)
    if regress:
        for f in UX_REGRESS:
            rg.record_regress("ux-rsi", f, f"test_{f.lower().replace('-', '_')}")


def test_replay_ux_loop_accepts_only_the_final_revision(rg):
    _replay(rg)
    st = rg._loop_state("ux-rsi", rg._read())
    got = {r: rg.evaluate(st, r) for r in rg.revisions(st)}
    assert [r for r, d in got.items() if d["decision"] == "ACCEPT"] == [6]
    for rev in (1, 2, 3, 4, 5):
        assert got[rev]["decision"] == "HOLD", rev
    assert "no_go" in got[1]["reasons"]                       # only GO-WITH-CHANGES
    assert "open_no_go" in got[2]["reasons"] and "same_vendor_go_only" in got[2]["reasons"]
    assert "open_no_go" in got[3]["reasons"] and "no_qualified_go" not in got[3]["reasons"]   # cross-vendor GO, but NO-GO open
    assert rg.check("ux-rsi")["decision"] == "ACCEPT"
    assert rg.report()["loops"][0]["revisions_to_accept"] == 6


def test_replay_round2_same_vendor_go_alone_holds(rg):
    _seed(rg, "ux-r2", OPUS, [(SONNET, "GO", False)], rev=2)
    d = rg.check("ux-r2")
    assert d["decision"] == "HOLD" and "same_vendor_go_only" in d["reasons"] and "open_no_go" not in d["reasons"]


def test_replay_final_revision_holds_without_regression_tests(rg):
    _replay(rg, regress=False)
    d = rg.check("ux-rsi")
    assert d["decision"] == "HOLD" and d["reasons"] == ["regression_missing"]
    assert d["regression_missing_ids"] == sorted(UX_REGRESS)


def test_replay_every_prefix_holds_until_rev6(rg):
    for upto in range(1, 6):
        import shutil
        shutil.rmtree(rg.out_path().parent, ignore_errors=True)
        _replay(rg, upto=upto)
        assert rg.check("ux-rsi")["decision"] == "HOLD", upto


def test_unknown_author_vendor_cannot_accept(tmp_path, monkeypatch):
    """xval repro: --author mystery + GPT GO + tests passed gave ACCEPT."""
    monkeypatch.setenv("APEX_HOME", str(tmp_path))
    from apex_router import review_gate as g
    g.record_review("L", 1, "it-entra-gpt-6-astra", "GO", author="mystery", code_access=True)
    g.record_tests("L", 1, True)
    r = g.check("L")
    assert r["decision"] == "HOLD" and "author_unknown" in r["reasons"]


def test_finding_confirmed_on_latest_revision_needs_regression(tmp_path, monkeypatch):
    """xval repro: confirmed F1 on the latest revision with no regression test gave ACCEPT."""
    monkeypatch.setenv("APEX_HOME", str(tmp_path))
    from apex_router import review_gate as g
    g.record_review("L", 1, "it-entra-gpt-6-astra", "GO", author="it-entra-claude-opus-5-5",
                    code_access=True, confirmed="F1")
    g.record_tests("L", 1, True)
    r = g.check("L")
    assert r["decision"] == "HOLD" and r["regression_missing_ids"] == ["F1"]
    g.record_regress("L", "F1", "test_f1")
    assert g.check("L")["decision"] == "ACCEPT"
