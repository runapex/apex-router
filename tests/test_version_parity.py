"""pyproject.toml is the version source; the plugin manifest and the marketplace must match it (spec §3)."""
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_plugin_json_matches_pyproject():
    manifest = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "datapce"
    assert manifest["version"] == _pyproject_version()


def test_marketplace_lists_the_one_plugin():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "datapce"
    [entry] = market["plugins"]
    assert entry["name"] == "datapce"
    assert entry["source"] == "./plugin"
    assert entry["category"] == "productivity"
