"""Proxy/anthropic setup — merge env keys into ~/.claude/settings.json safely.

The load-bearing property: merging the proxy keys must PRESERVE every other setting
(permissions, hooks, enabledPlugins, unrelated env keys). A blind overwrite would wipe the
user's config. Values come from env/config only — nothing hardcoded, no secrets.
"""
import json
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router import proxy_setup  # noqa: E402


class TestResolveConfig(unittest.TestCase):
    def test_reads_from_env(self):
        env = {
            "ANTHROPIC_FOUNDRY_BASE_URL": "http://localhost:8788",
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "IRRELEVANT": "x",
        }
        cfg = proxy_setup.resolve_config(env=env, config_file=None)
        self.assertEqual(cfg["ANTHROPIC_FOUNDRY_BASE_URL"], "http://localhost:8788")
        self.assertEqual(cfg["CLAUDE_CODE_USE_FOUNDRY"], "1")
        self.assertNotIn("IRRELEVANT", cfg)  # only known proxy keys are pulled

    def test_reads_from_config_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / "proxy.env"
            cf.write_text(
                "# comment\nANTHROPIC_FOUNDRY_BASE_URL=http://localhost:9999\n"
                "CLAUDE_CODE_USE_FOUNDRY=1\n\n")
            cfg = proxy_setup.resolve_config(env={}, config_file=cf)
            self.assertEqual(cfg["ANTHROPIC_FOUNDRY_BASE_URL"], "http://localhost:9999")

    def test_env_overrides_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / "proxy.env"
            cf.write_text("ANTHROPIC_FOUNDRY_BASE_URL=http://from-file:1\n")
            cfg = proxy_setup.resolve_config(
                env={"ANTHROPIC_FOUNDRY_BASE_URL": "http://from-env:2"}, config_file=cf)
            self.assertEqual(cfg["ANTHROPIC_FOUNDRY_BASE_URL"], "http://from-env:2")

    def test_empty_when_nothing_provided(self):
        self.assertEqual(proxy_setup.resolve_config(env={}, config_file=None), {})


class TestMergeSettings(unittest.TestCase):
    def _settings(self):
        return {
            "model": "opus",
            "permissions": {"allow": ["Bash(ls)"]},
            "hooks": {"Stop": []},
            "enabledPlugins": {"superpowers": True},
            "env": {"ENABLE_PROMPT_CACHING_1H": "1", "BASH_MAX_OUTPUT_LENGTH": "50000"},
        }

    def test_merge_preserves_all_other_keys(self):
        s = self._settings()
        proxy = {"ANTHROPIC_FOUNDRY_BASE_URL": "http://localhost:8788",
                 "CLAUDE_CODE_USE_FOUNDRY": "1"}
        merged = proxy_setup.merge_settings(s, proxy)
        # every original top-level key survives
        for k in ("model", "permissions", "hooks", "enabledPlugins"):
            self.assertEqual(merged[k], s[k])
        # unrelated env keys survive
        self.assertEqual(merged["env"]["ENABLE_PROMPT_CACHING_1H"], "1")
        self.assertEqual(merged["env"]["BASH_MAX_OUTPUT_LENGTH"], "50000")
        # proxy keys are added
        self.assertEqual(merged["env"]["ANTHROPIC_FOUNDRY_BASE_URL"], "http://localhost:8788")
        self.assertEqual(merged["env"]["CLAUDE_CODE_USE_FOUNDRY"], "1")

    def test_merge_is_idempotent(self):
        s = self._settings()
        proxy = {"CLAUDE_CODE_USE_FOUNDRY": "1"}
        once = proxy_setup.merge_settings(s, proxy)
        twice = proxy_setup.merge_settings(once, proxy)
        self.assertEqual(once, twice)

    def test_merge_does_not_mutate_input(self):
        s = self._settings()
        proxy_setup.merge_settings(s, {"CLAUDE_CODE_USE_FOUNDRY": "1"})
        self.assertNotIn("CLAUDE_CODE_USE_FOUNDRY", s["env"])  # original untouched

    def test_merge_creates_env_block_if_absent(self):
        merged = proxy_setup.merge_settings({"model": "opus"}, {"CLAUDE_CODE_USE_FOUNDRY": "1"})
        self.assertEqual(merged["env"]["CLAUDE_CODE_USE_FOUNDRY"], "1")
        self.assertEqual(merged["model"], "opus")


class TestApplyWritesBackupAndParses(unittest.TestCase):
    def test_apply_backs_up_and_writes_valid_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            sf = Path(d) / "settings.json"
            sf.write_text(json.dumps({"model": "opus", "env": {"KEEP": "1"}}))
            proxy = {"CLAUDE_CODE_USE_FOUNDRY": "1"}
            backup = proxy_setup.apply(sf, proxy)
            # backup exists and holds the original
            self.assertTrue(backup.exists())
            self.assertEqual(json.loads(backup.read_text())["env"], {"KEEP": "1"})
            # settings.json now valid + merged + preserved
            after = json.loads(sf.read_text())
            self.assertEqual(after["model"], "opus")
            self.assertEqual(after["env"]["KEEP"], "1")
            self.assertEqual(after["env"]["CLAUDE_CODE_USE_FOUNDRY"], "1")

    def test_apply_noop_when_nothing_to_set(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            sf = Path(d) / "settings.json"
            sf.write_text(json.dumps({"model": "opus"}))
            before = sf.read_text()
            backup = proxy_setup.apply(sf, {})   # empty proxy config
            self.assertIsNone(backup)             # no change, no backup
            self.assertEqual(sf.read_text(), before)


if __name__ == "__main__":
    unittest.main()
