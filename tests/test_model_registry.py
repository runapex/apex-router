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
        self.assertEqual(model_registry.tier_model("sonnet"), "claude-sonnet-5-5")
        self.assertEqual(model_registry.tier_model("opus"), "claude-opus-5-5")
        self.assertEqual(model_registry.tier_model("haiku"), "claude-haiku-4-5")
        self.assertEqual(model_registry.tier_model("fable"), "claude-fable-5-1")

    def test_tier_model_returns_none_for_unknown(self):
        self.assertIsNone(model_registry.tier_model("nonexistent"))

    def test_gpt_families_use_the_codex_provider_and_matching_effort(self):
        fams = model_registry.families()
        self.assertEqual(fams["gpt-luna"],
                         {"provider": "openai-codex", "id": "gpt-5.6-luna", "effort": "low"})
        self.assertEqual(fams["gpt-terra"],
                         {"provider": "openai-codex", "id": "gpt-5.6-terra", "effort": "medium"})
        self.assertEqual(fams["gpt-sol"],
                         {"provider": "openai-codex", "id": "gpt-5.6-sol", "effort": "high"})

    def test_anthropic_families_match_routing_and_cross_validation_policy(self):
        fams = model_registry.families()
        self.assertEqual(fams["haiku"],
                         {"provider": "foundry", "id": "it-entra-claude-haiku-4-5"})
        self.assertEqual(fams["sonnet"],
                         {"provider": "foundry", "id": "it-entra-claude-sonnet-5-5", "effort": "medium"})
        self.assertEqual(fams["opus"],
                         {"provider": "foundry", "id": "it-entra-claude-opus-5-5", "effort": "high"})
        self.assertEqual(fams["fable"],
                         {"provider": "anthropic", "id": "claude-fable-5-1", "effort": "max"})
        # Routine independent review remains Opus; Fable is an explicit reasoning ceiling.
        self.assertEqual(fams["deep"], fams["opus"])
        self.assertNotEqual(fams["deep"]["id"], fams["fable"]["id"])

    def test_families_resolves_tier_family_and_omits_unresolvable(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = {"pi_families": {"frontier": {"provider": "anthropic", "tier": "sonnet", "effort": "medium"}}}
            p = self._write_overlay(Path(tmp), overlay)
            registry = model_registry.load(p)

            model_registry._local_model = lambda: "ollama-local"
            fams = model_registry.families(registry=registry)
            self.assertIn("frontier", fams)
            self.assertEqual(fams["frontier"], {"provider": "anthropic", "id": "claude-sonnet-5-5", "effort": "medium"})
            self.assertIn("local", fams)
            self.assertEqual(fams["local"], {"provider": "ollama", "id": "ollama-local"})

            broken = {"pi_families": {"frontier": {"provider": "anthropic", "tier": "nonexistent"}}}
            broken_p = self._write_overlay(Path(tmp), broken)
            broken_registry = model_registry.load(broken_p)
            model_registry._local_model = lambda: "ollama-local"
            broken_fams = model_registry.families(registry=broken_registry)
            self.assertNotIn("frontier", broken_fams)

    def test_review_family_is_an_independent_gpt_reviewer(self):
        fams = model_registry.families()
        self.assertEqual(fams["review"],
                         {"provider": "foundry-gpt", "id": "it-entra-gpt-6.1-sol", "effort": "high"})
        # Cross-vendor: the reviewer must not be a Claude family.
        self.assertNotIn("claude", fams["review"]["id"])

    def test_provider_id_prefix_is_keyed_by_provider_not_family(self):
        # The foundry prefix must not leak onto a family an overlay moves back to anthropic,
        # and the shared `tiers` (proxy/codeqa) stay plain Claude ids.
        self.assertEqual(model_registry.tier_model("sonnet"), "claude-sonnet-5-5")
        with tempfile.TemporaryDirectory() as tmp:
            overlay = {
                "pi_families": {"sonnet": {"provider": "anthropic"}},
                "provider_id_prefix": {"moonshotai": "x-"},
            }
            registry = model_registry.load(self._write_overlay(Path(tmp), overlay))
            model_registry._local_model = lambda: "ollama-local"
            fams = model_registry.families(registry=registry)
            self.assertEqual(fams["sonnet"]["id"], "claude-sonnet-5-5")
            self.assertEqual(fams["opus"]["id"], "it-entra-claude-opus-5-5")  # default prefix kept
            self.assertEqual(fams["kimi"]["id"], "kimi-k2.6")  # explicit ids never prefixed

    def test_subscription_overlay_keeps_pi_off_claude(self):
        # The shipped subscription overlay: Anthropic bills pi's subscription OAuth to extra
        # usage, so no pi family (bar ollama/kimi) may resolve to a Claude id, `review` lands on
        # the Codex subscription, and the shared tiers (Claude Code / codeqa) stay Claude.
        src = Path(__file__).resolve().parents[1] / "integrations/pi/registry-overlay.subscription.json"
        registry = model_registry.load(src)
        model_registry._local_model = lambda: "ollama-local"
        fams = model_registry.families(registry=registry)
        for name, fam in fams.items():
            self.assertNotIn(fam["provider"], ("anthropic", "foundry", "foundry-gpt"), name)
            self.assertNotIn("claude", fam["id"], name)
            self.assertFalse(fam["id"].startswith("it-entra-"), name)
        self.assertEqual(fams["review"], {"provider": "openai-codex", "id": "gpt-6.1-sol", "effort": "high"})
        self.assertEqual(model_registry.tier_model("sonnet", registry=registry), "claude-sonnet-5-5")
        self.assertEqual(model_registry.learn(registry=registry),
                         {"provider": "openai-codex", "validate": "gpt-5.6-terra", "explain": "gpt-5.6-sol"})

    def test_overlay_family_pinning_an_id_drops_the_default_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = {"pi_families": {"sonnet": {"provider": "openai-codex", "id": "gpt-x"},
                                       "opus": {"provider": "anthropic"}}}
            registry = model_registry.load(self._write_overlay(Path(tmp), overlay))
        model_registry._local_model = lambda: "ollama-local"
        fams = model_registry.families(registry=registry)
        # effort still merges from the default; the stale tier does not win over the pinned id.
        self.assertEqual(fams["sonnet"], {"provider": "openai-codex", "id": "gpt-x", "effort": "medium"})
        self.assertEqual(registry["pi_families"]["opus"]["tier"], "opus")  # no source key named → merge

    def test_family_with_both_id_and_tier_resolves_the_id(self):
        registry = {"pi_families": {"sonnet": {"provider": "openai-codex", "tier": "sonnet", "id": "gpt-x"}}}
        fams = model_registry.families(registry=registry)
        self.assertEqual(fams["sonnet"], {"provider": "openai-codex", "id": "gpt-x"})

    def test_learn_explicit_ids_override_tiers(self):
        registry = {"learn": {"provider": "foundry", "validate": "v-id", "explain_tier": "opus"}}
        self.assertEqual(model_registry.learn(registry=registry),
                         {"provider": "foundry", "validate": "v-id", "explain": "it-entra-claude-opus-5-5"})

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
        self.assertEqual(result["provider"], "foundry")
        self.assertEqual(result["validate"], "it-entra-claude-sonnet-5-5")
        self.assertEqual(result["explain"], "it-entra-claude-opus-5-5")

    def test_learn_uses_custom_registry(self):
        registry = {"learn": {"provider": "custom", "validate_tier": "sonnet", "explain_tier": "opus"}}
        result = model_registry.learn(registry=registry)
        self.assertEqual(result["provider"], "custom")
        self.assertEqual(result["validate"], "claude-sonnet-5-5")
        self.assertEqual(result["explain"], "claude-opus-5-5")


if __name__ == "__main__":
    unittest.main()
