"""0.4.1: privacy and test-isolation patch; hook retirement moves to 0.4.2."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _section():
    text = (ROOT / "CHANGELOG.md").read_text()
    m = re.search(r"^## 0\.4\.1 — \d{4}-\d{2}-\d{2}\n(.*?)(?=^## )", text, re.M | re.S)
    assert m, "dated 0.4.1 heading missing"
    return m.group(1)


def test_version_is_0_4_1_in_both_files():
    assert re.search(r'^version = "0\.4\.1"', (ROOT / "pyproject.toml").read_text(), re.M)
    plugin = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"] == "0.4.1"


def test_security_entry_mentions_salting_and_restart():
    section = _section()
    assert "### Security" in section
    low = section.lower()
    assert "salt" in low and "restart open sessions" in low


def test_isolation_fix_is_listed():
    assert "no longer writes to the live install" in _section()


def test_hook_retirement_is_0_4_2():
    text = (ROOT / "CHANGELOG.md").read_text()
    assert "Both are retired in 0.4.2" in text
    assert "retired in 0.4.1" not in (ROOT / "README.md").read_text()
    assert "retired in 0.4.1" not in (ROOT / "install.sh").read_text()
    assert "retired in 0.4.2" in (ROOT / "install.sh").read_text()
