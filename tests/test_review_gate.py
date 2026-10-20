"""review_gate: B3 records (no text, idempotent, APEX_HOME) and B1 gate (ACCEPT/HOLD) incl. UX-loop replay."""
from __future__ import annotations

import importlib
import json

import pytest

OPUS, SONNET, GPT = "it-entra-claude-opus-5-5", "it-entra-claude-sonnet-5-5", "it-entra-gpt-6-astra (xval)".split()[0]
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
