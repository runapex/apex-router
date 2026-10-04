"""Spec §8 as amended by the pivot: one condensed skill, ≤ 350 lines, Route/Verify/Review/Ship,
backend commands once (in Route), ledger numbers fed to model selection, advise-only."""
import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "datapce" / "SKILL.md"
RETIRED = ["model-routing", "disciplined-execution", "verify-claims", "evidence-labels", "cross-validate",
           "change-classification", "local-references", "public-repo-hygiene", "dependency-vetting", "unattended-loop"]
COMMANDS = ["apex-router pressure", "apex-router route-advise", "apex-router review-preread"]
LABELS = ["PASS", "FAIL", "PARTIAL", "BLOCKED", "INCONCLUSIVE", "STALE", "ASSUMED", "N/A", "WAIVED"]


def _text() -> str:
    return SKILL.read_text()


def _sections(text: str) -> dict:
    parts = re.split(r"^## ", text, flags=re.M)
    return {p.split("\n", 1)[0].strip(): p for p in parts[1:]}


def test_frontmatter_and_length():
    text = _text()
    assert text.startswith("---\nname: datapce\n")
    assert len(text.splitlines()) <= 350
    description = text.split("\n---\n", 1)[0]
    for name in RETIRED:
        assert name in description, name


def test_sections_in_order():
    assert list(_sections(_text()))[:4] == ["Route", "Verify", "Review", "Ship"]


def test_backend_commands_once_and_only_in_route():
    text = _text()
    route = _sections(text)["Route"]
    for cmd in COMMANDS:
        assert text.count(cmd) == 1, cmd
        assert cmd in route, cmd
    assert "injection-ab" not in text


def test_verify_carries_the_five_gates_and_nine_labels():
    verify = _sections(_text())["Verify"]
    for gate in ["Scope before work", "Evidence before reasoning", "Reason adversarially",
                 "Verify before declaring done", "Report calibrated"]:
        assert gate in verify, gate
    for label in LABELS:
        assert f"**{label}**" in verify, label


def test_route_feeds_ledger_numbers_and_is_advise_only():
    route = _sections(_text())["Route"]
    assert "explicit `model:`" in route
    assert "/apex" in route
    for field in ["`n`", "`ok%`", "`tok μ`", "`dur μ`", "unavailable"]:
        assert field in route, field
    assert "no quality label yet" in route.lower()
    assert "35 of 35" in route


def test_no_enforce_planner_or_anomaly_wording():
    low = _text().lower()
    for word in ["enforce", "anomaly", "n/30", "30+ labels"]:
        assert word not in low, word
    assert "does not plan" in low


def test_no_personal_paths_or_private_names():
    text = _text()
    assert "/Users/" not in text and "~/src" not in text
