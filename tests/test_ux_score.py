"""scripts/ux_score.py v2: the fact matcher needs a whole labelled cell (reviewer counterexamples)."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("ux_score", Path(__file__).resolve().parents[1] / "scripts" / "ux_score.py")
ux = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ux)

LIM = {"pct": 61, "labels": ["5h", "limit"]}


@pytest.mark.parametrize("kind,val,text,ok", [
    ("limit", LIM, "apex AMBER · 5h 61% · context 40%", True),
    ("limit", LIM, "apex RED · 5h limit 61% · pause new subagents", True),
    ("limit", LIM, "not in the band (no room): 5h 61% · 2 subagents", True),
    ("limit", LIM, "apex AMBER · budget 61% · context 40%", False),
    ("limit", LIM, "apex AMBER · context 61%", False),
    ("ctx", 8, "apex GREEN · 5h 98% · $1.00", False),
    ("ctx", 8, "apex GREEN · context 8% · $1.00", True),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · $4.12M", False),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · $4.12/$10", True),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · $0.00/$4.12", False),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · budget $4.12", False),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · $0.00 · budget: $4.12", False),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · budget, $4.12", False),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · (budget) $4.12", False),
    ("cost", ["$9.10", "$9.10"], "apex AMBER · $9.10/$10, empty ≈18 min · pause new subagents", True),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · $4.12e3", False),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · $4.12,000", False),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · $4.12 per hour", False),
    ("cost", ["$1.2M", "$1.2M"], "apex GREEN · $1.2M/$1T, empty ≈18 min", True),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · $4.12/$10, budget is fake", False),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · $4.12/$1e3", False),
    ("cost", ["$4.12", "$4.12"], "apex GREEN · $4.12/$999T+", True),
    ("cost", ["$999T+", "$999T+"], "apex GREEN · $999T+/$999T+, empty ≈18 min", True),
    ("limit", {"pct": 61, "labels": ["my  kind", "limit"]}, "apex AMBER · my  kind 61%", True),
    ("subagents", 3, "apex GREEN · progress 1 of 3", False),
    ("subagents", 3, "apex GREEN · 3 subagents", True),
    ("subagents", 7, "apex AMBER · 2 of 7 differ from advice", True),
])
def test_fact_needs_its_label(kind, val, text, ok):
    assert ux.fact_shown(kind, val, [text]) is ok


def test_incomplete_fixture_is_not_scored():
    assert ux.inv_expected("noise\nUXINV_META {\"n\": 410}\n") == 410
    assert ux.inv_expected("no meta") is None


def test_a_wrapped_pane_row_is_joined_before_matching():
    rows = ["not in the band (no room): spend 37% · 1 of 3 differ from", "  advice"]
    assert ux.fact_shown("subagents", 3, rows) is True


def _case(band="apex GREEN · $1.00", desc_row="Review commit 1e3abc · explore", lim_row="spent $1.00 (no budget set)"):
    return {"id": "x", "level": "GREEN", "oracle": {"first": None, "actions": [], "firstKind": None, "quotaCause": False},
            "facts": {}, "band": band, "status": "apex GREEN · $1.00",
            "sections": [{"id": "now", "rows": ["GREEN · no action needed"]}, {"id": "signals", "rows": [lim_row]},
                         {"id": "dispatches", "rows": [desc_row]}]}


def test_exponent_check_reads_figures_not_user_descriptions():
    assert ux.check_case(_case())["no_junk"] is True
    assert ux.check_case(_case(band="apex GREEN · $1e+21"))["no_junk"] is False
    assert ux.check_case(_case(lim_row="spent $1e+21 (no budget set)"))["no_junk"] is False
