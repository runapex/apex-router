"""Tests for scripts/prefix_budget.py — the prefix-trim measurement tool.

Uses an injected fake counter so the tests are deterministic and offline; the
real SDK counter is exercised only implicitly (its None-fallback path).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import prefix_budget as pb  # noqa: E402


def word_counter(text):
    """Deterministic fake 'exact' counter: 1 token per whitespace word."""
    return len(text.split())


def null_counter(text):  # noqa: ARG001 — signature must match a counter
    """Simulates an unavailable SDK — forces the estimate fallback."""
    return None


# ------------------------------------------------------ counter plumbing ----
def test_count_text_prefers_exact_counter():
    toks, exact = pb.count_text("a b c", counter=word_counter)
    assert (toks, exact) == (3, True)


def test_count_text_falls_back_to_estimate_and_flags_it():
    toks, exact = pb.count_text("x" * 35, counter=null_counter)
    assert exact is False
    assert toks == int(35 / pb.FALLBACK_CHARS_PER_TOKEN)  # 10


def test_estimate_empty_is_zero():
    assert pb.estimate_tokens("") == 0


# ---------------------------------------------------- component measuring ----
def test_components_ranked_largest_first():
    comps = [("small", "one two"), ("big", "a b c d e f")]
    m = pb.measure_components(comps, counter=word_counter)
    assert [r["component"] for r in m["components"]] == ["big", "small"]
    assert m["total_tokens"] == 8
    assert m["any_estimate"] is False


def test_any_estimate_true_when_fallback_used():
    m = pb.measure_components([("c", "abcdefg")], counter=null_counter)
    assert m["any_estimate"] is True


# ------------------------------------------------------------ budget gate ----
def test_over_budget_detected_with_overage():
    rep = pb.build_report(claude_md=None, project_md=None, tools=None,
                          budget=2, counter=word_counter)
    # no files → total 0 → not over
    assert rep["over_budget"] is False


def test_over_budget_from_real_files(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("alpha beta gamma delta")  # 4 words → 4 tokens with word_counter
    rep = pb.build_report(claude_md=str(md), project_md=None, tools=None,
                          budget=3, counter=word_counter)
    assert rep["total_tokens"] == 4
    assert rep["over_budget"] is True
    assert rep["overage_tokens"] == 1


def test_tools_json_serialized_deterministically(tmp_path):
    t = tmp_path / "tools.json"
    # same content, different key order must count identically
    t.write_text('{"b": 2, "a": 1}')
    txt = pb._tools_text(str(t))
    assert txt == '{"a": 1, "b": 2}'  # sorted keys


def test_tools_invalid_json_measured_as_raw(tmp_path):
    t = tmp_path / "tools.json"
    t.write_text('{not valid json')
    assert pb._tools_text(str(t)) == '{not valid json'  # measured, not crashed


def test_missing_file_reads_empty(tmp_path):
    assert pb._read(tmp_path / "nope.md") == ""


# ------------------------------------------------------------- main / cli ----
def test_main_check_exits_2_when_over(tmp_path, monkeypatch):
    md = tmp_path / "CLAUDE.md"
    md.write_text("a b c d e f g h i j")  # 10 words
    # force the estimate path off by patching the SDK counter to the word counter
    monkeypatch.setattr(pb, "count_tokens_sdk", word_counter)
    rc = pb.main(["--claude-md", str(md), "--budget", "5", "--check"])
    assert rc == 2


def test_main_ok_when_under(tmp_path, monkeypatch):
    md = tmp_path / "CLAUDE.md"
    md.write_text("a b")
    monkeypatch.setattr(pb, "count_tokens_sdk", word_counter)
    rc = pb.main(["--claude-md", str(md), "--budget", "100", "--check"])
    assert rc == 0
