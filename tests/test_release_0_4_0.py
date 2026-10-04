"""0.4.0: plugin side by side with deprecated hooks; spec §10 privacy verbatim in listing and README;
README/listing lead with pressure, cost and dispatches and say the ledger is not answer quality."""
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVACY = ("never stores prompt text, file contents, or command text; descriptions are truncated "
           "dispatch labels. No network calls. No telemetry to anyone.")
FORMER_SKILLS = ("model-routing", "cross-validate", "verify-claims", "disciplined-execution",
                 "public-repo-hygiene", "local-references", "change-classification",
                 "evidence-labels", "unattended-loop", "dependency-vetting")


def _readme() -> str:
    return (ROOT / "README.md").read_text()


def _market() -> dict:
    return json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())


def _section_0_4_0() -> str:
    text = (ROOT / "CHANGELOG.md").read_text()
    start = text.index("## 0.4.0")
    return text[start:text.index("\n## ", start + 1)]


def test_hooks_are_deprecated_not_deleted():
    for name in ("agent-route-log.sh", "cache-handoff-nudge.sh"):
        head = "\n".join((ROOT / "hooks" / name).read_text().splitlines()[:4])
        assert "DEPRECATED in 0.4.0" in head and "0.4.1" in head, name


def test_version_is_0_4_0():
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"] == "0.4.0"


def test_privacy_is_stated_in_readme_and_listing():
    assert PRIVACY in _readme()
    assert PRIVACY in _market()["plugins"][0]["description"]


def test_readme_leads_with_the_plugin_install():
    head = "\n".join(_readme().splitlines()[:30])
    assert head.startswith("# datapce (apex-router)")
    assert "claude plugin marketplace add runapex/apex-router" in head
    assert "claude plugin install datapce@datapce" in head


def test_readme_head_leads_with_pressure_and_is_honest_about_quality():
    head = "\n".join(_readme().splitlines()[:40]).lower()
    assert "advise-only" in head and "enforc" not in head
    assert "not answer quality" in head and "v1.1" in head
    assert head.index("pressure") < head.index("ledger")


def test_listing_is_honest_about_quality():
    entry = _market()["plugins"][0]["description"].lower()
    assert "not answer quality" in entry and "pressure" in entry
    assert entry.index("pressure") < entry.index("ledger")


def test_readme_skill_claim_matches_the_skill():
    skill = (ROOT / "plugin" / "skills" / "datapce" / "SKILL.md").read_text()
    description = re.search(r"^description: (.*)$", skill, re.M).group(1)
    for name in FORMER_SKILLS:
        assert name in description, name
        assert f"`{name}`" in _readme(), name


def test_changelog_0_4_0_on_top_with_0_3_1_folded_in():
    text = (ROOT / "CHANGELOG.md").read_text()
    assert re.findall(r"^## (\d+\.\d+\.\d+)", text, re.M)[:2] == ["0.4.0", "0.3.0"]
    section = _section_0_4_0()
    heads = re.findall(r"^### (\w+)", section, re.M)
    assert heads == ["Added", "Changed", "Deprecated", "Fixed"], heads
    for marker in ("--probe-thinking", "ORNITH_LOCK_TIMEOUT_SECS", "clamped median"):
        assert marker in section, marker


def test_changelog_says_what_0_4_0_is_and_is_not():
    section = _section_0_4_0()
    low = section.lower()
    assert "injection-ab" not in section
    assert "enforc" not in low
    assert "quality_labels" in low and "0.4.1" in section and "writer_parity" in section
    assert "parity_span_days" in section and "parity_days" in section and "10" in section


def test_changelog_covers_branch_content_and_the_recency_gate():
    section = _section_0_4_0()
    assert "connect-retry backoff test no longer depends on wall-clock timing" in section
    assert "repository hygiene test" in section
    assert "parity_until" in section and "gate_open" in section
