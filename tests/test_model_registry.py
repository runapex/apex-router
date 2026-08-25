import json
import os
import tempfile
import unittest
from pathlib import Path

import apex_router.model_registry as model_registry


class TestModelRegistry(unittest.TestCase):
    def setUp(self):
        self._orig_local = model_registry._local_model
        self.addCleanup(setattr, model_registry, "_local_model", self._orig_local)

    def _write_overlay(self, tmp: Path, obj) -> Path:
        p = tmp / "models.json"
        p.write_text(json.dumps(obj))
        return p

    def test_load_missing_overlay_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = model_registry.load(Path(tmp) / "models.json")
        self.assertEqual(result, model_registry.DEFAULTS)

    def test_load_deep_merges_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = {
                "tiers": {"sonnet": "custom-sonnet"},
                "pi_families": {"kimi": {"provider": "moonshotai", "id": "kimi-k2.9"}},
            }
            p = self._write_overlay(Path(tmp), overlay)
            result = model_registry.load(p)
        self.assertEqual(result["tiers"]["sonnet"], "custom-sonnet")
        self.assertEqual(result["tiers"]["opus"], model_registry.DEFAULTS["tiers"]["opus"])
        self.assertEqual(result["pi_families"]["kimi"], {"provider": "moonshotai", "id": "kimi-k2.9"})
        self.assertEqual(result["pi_families"]["frontier"], model_registry.DEFAULTS["pi_families"]["frontier"])

    def test_load_malformed_overlay_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "models.json"
            p.write_text("{not valid json")
            result = model_registry.load(p)
        self.assertEqual(result, model_registry.DEFAULTS)

    def test_tier_model_resolves_tiers(self):
        self.assertEqual(model_registry.tier_model("sonnet"), "claude-sonnet-5")
        self.assertEqual(model_registry.tier_model("opus"), "claude-opus-4-8")
        self.assertEqual(model_registry.tier_model("haiku"), "claude-haiku-4-5")

    def test_tier_model_returns_none_for_unknown(self):
        self.assertIsNone(model_registry.tier_model("nonexistent"))

    def test_gpt_5_6_families_use_the_codex_provider_and_matching_effort(self):
        fams = model_registry.families()
        self.assertEqual(fams["gpt-luna"],
                         {"provider": "openai-codex", "id": "gpt-5.6-luna", "effort": "low"})
        self.assertEqual(fams["gpt-terra"],
                         {"provider": "openai-codex", "id": "gpt-5.6-terra", "effort": "medium"})
        self.assertEqual(fams["gpt-sol"],
                         {"provider": "openai-codex", "id": "gpt-5.6-sol", "effort": "high"})

    def test_families_resolves_tier_family_and_omits_unresolvable(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = {"pi_families": {"frontier": {"provider": "anthropic", "tier": "sonnet", "effort": "medium"}}}
            p = self._write_overlay(Path(tmp), overlay)
            registry = model_registry.load(p)

            model_registry._local_model = lambda: "ollama-local"
            fams = model_registry.families(registry=registry)
            self.assertIn("frontier", fams)
            self.assertEqual(fams["frontier"], {"provider": "anthropic", "id": "claude-sonnet-5", "effort": "medium"})
            self.assertIn("local", fams)
            self.assertEqual(fams["local"], {"provider": "ollama", "id": "ollama-local"})

            broken = {"pi_families": {"frontier": {"provider": "anthropic", "tier": "nonexistent"}}}
            broken_p = self._write_overlay(Path(tmp), broken)
            broken_registry = model_registry.load(broken_p)
            model_registry._local_model = lambda: "ollama-local"
            broken_fams = model_registry.families(registry=broken_registry)
            self.assertNotIn("frontier", broken_fams)

    def test_families_local_raises_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = {"pi_families": {"local": {"provider": "ollama", "source": "ornith.env"}}}
            p = self._write_overlay(Path(tmp), overlay)
            registry = model_registry.load(p)
            model_registry._local_model = lambda: (_ for _ in ()).throw(RuntimeError("no ollama"))
            fams = model_registry.families(registry=registry)
            self.assertNotIn("local", fams)

    def test_local_family_follows_resolved_pin(self):
        import apex_router.model_registry as mr
        from apex_router.ornith import local_tier
        from unittest import mock
        with mock.patch.object(local_tier, "resolve",
                               return_value=local_tier.Tier(
                                   name="pinned", api_model="some/backend:tag",
                                   weights_gb=0, active_b=0, total_b=0, note="")):
            fams = mr.families()
        self.assertEqual(fams["local"]["id"], "some/backend:tag")
        self.assertEqual(fams["local"]["provider"], "ollama")

    def test_learn_resolves_through_tiers(self):
        result = model_registry.learn()
        self.assertEqual(result["provider"], "anthropic")
        self.assertEqual(result["validate"], "claude-sonnet-5")
        self.assertEqual(result["explain"], "claude-opus-4-8")

    def test_learn_uses_custom_registry(self):
        registry = {"learn": {"provider": "custom", "validate_tier": "sonnet", "explain_tier": "opus"}}
        result = model_registry.learn(registry=registry)
        self.assertEqual(result["provider"], "custom")
        self.assertEqual(result["validate"], "claude-sonnet-5")
        self.assertEqual(result["explain"], "claude-opus-4-8")


if __name__ == "__main__":
    unittest.main()
