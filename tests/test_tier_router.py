"""codeqa tier router — the SECOND routing axis (which frontier Claude tier + reasoning effort).

Pure, offline: resolve() is deterministic and takes an injected env, so no network and no dependence
on the ambient CODEQA_* vars. Also asserts the model-picker split is tallied through validate_memory
so the telemetry/metrics readout has real numbers.
"""
from __future__ import annotations

import unittest

from apex_router.codeqa import tier_router


class TestResolve(unittest.TestCase):
    def test_task_kind_defaults(self):
        # judge/conclude → opus/high ; inference → sonnet/medium ; value/extract → haiku/(none)
        cases = {
            "judge": ("opus", "claude-opus-4-8", "high"),
            "conclude": ("opus", "claude-opus-4-8", "high"),
            "verify": ("opus", "claude-opus-4-8", "xhigh"),
            "runtime": ("opus", "claude-opus-4-8", "xhigh"),
            "synthesis": ("sonnet", "claude-sonnet-5", "medium"),
            "inference": ("sonnet", "claude-sonnet-5", "medium"),
            "extract": ("haiku", "claude-haiku-4-5", None),
            "value": ("haiku", "claude-haiku-4-5", None),
        }
        for task, (tier, model, effort) in cases.items():
            r = tier_router.resolve(task, env={})
            self.assertEqual((r.tier, r.model, r.effort), (tier, model, effort), task)
            self.assertFalse(r.fixed)

    def test_case_insensitive_and_unknown_fallback(self):
        self.assertEqual(tier_router.resolve("JUDGE", env={}).tier, "opus")
        r = tier_router.resolve("something-new", env={})
        self.assertEqual((r.tier, r.effort), ("opus", "high"))   # safe capable fallback
        self.assertIn("fallback", r.reason)

    def test_haiku_never_carries_effort_even_if_configured(self):
        # HARD API constraint: haiku rejects output_config.effort (400). A bad override is corrected.
        r = tier_router.resolve("extract", env={"CODEQA_TIER_ROUTES": "extract=haiku/high"})
        self.assertEqual(r.tier, "haiku")
        self.assertIsNone(r.effort)
        self.assertIn("dropped", r.reason)

    def test_invalid_effort_dropped(self):
        r = tier_router.resolve("synthesis", env={"CODEQA_TIER_ROUTES": "synthesis=sonnet/turbo"})
        self.assertEqual(r.tier, "sonnet")
        self.assertIsNone(r.effort)

    def test_env_model_override_per_tier(self):
        env = {"CODEQA_TIER_MODELS": "opus=my-opus,haiku=my-haiku"}
        self.assertEqual(tier_router.resolve("judge", env=env).model, "my-opus")
        self.assertEqual(tier_router.resolve("value", env=env).model, "my-haiku")
        # sonnet untouched → default id
        self.assertEqual(tier_router.resolve("inference", env=env).model, "claude-sonnet-5")

    def test_env_route_override(self):
        env = {"CODEQA_TIER_ROUTES": "judge=sonnet/low"}
        r = tier_router.resolve("judge", env=env)
        self.assertEqual((r.tier, r.effort), ("sonnet", "low"))

    def test_bad_tier_name_falls_back(self):
        r = tier_router.resolve("judge", env={"CODEQA_TIER_ROUTES": "judge=titan/high"})
        self.assertEqual(r.tier, "opus")           # unknown tier name → safe fallback, still runs
        self.assertIn("fell back", r.reason)

    def test_explicit_model_override_bypasses_routing(self):
        # CODEQA_JUDGE_MODEL is the back-compat single-model override — wins over the whole router.
        env = {"CODEQA_JUDGE_MODEL": "claude-opus-4-8[shadow]"}
        r = tier_router.resolve("value", env=env)   # would normally be haiku
        self.assertTrue(r.fixed)
        self.assertEqual(r.model, "claude-opus-4-8")   # trailing [..] marker stripped
        self.assertIsNone(r.effort)                    # override sends no effort
        self.assertEqual(tier_router.request_extras(r), {})


class TestRequestShaping(unittest.TestCase):
    def test_extras_effort_capable(self):
        r = tier_router.resolve("judge", env={})       # opus/high
        extras = tier_router.request_extras(r)
        self.assertEqual(extras["output_config"], {"effort": "high"})
        self.assertEqual(extras["thinking"], {"type": "adaptive"})

    def test_extras_empty_for_haiku(self):
        r = tier_router.resolve("value", env={})        # haiku, no effort
        self.assertEqual(tier_router.request_extras(r), {})

    def test_max_tokens_floor_scales_with_effort(self):
        haiku = tier_router.resolve("value", env={})
        opus_x = tier_router.resolve("verify", env={})  # xhigh
        self.assertEqual(tier_router.min_max_tokens(haiku), 0)     # no floor → caller's budget stands
        self.assertGreaterEqual(tier_router.min_max_tokens(opus_x), 4096)
        self.assertGreater(tier_router.timeout_for(opus_x), tier_router.timeout_for(haiku))


class TestTelemetryTally(unittest.TestCase):
    def test_validate_memory_tallies_tier_calls(self):
        # A memory with three claim kinds, checked against a tiny live repo. All-frontier (no local
        # verifier) → VALUE→haiku, INFERENCE→sonnet, RUNTIME→opus, and validate_memory records the split.
        import tempfile
        from pathlib import Path
        from apex_router.codeqa import freshness

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "conf.py").write_text("MAX_ITEMS = 30\nTIMEOUT = 5\n")
            memory = (
                "# notes\n"
                "- The MAX_ITEMS constant is set to 30 in conf.py for the batch envelope.\n"
                "- When MAX_ITEMS is exceeded the run is split, so large batches never overflow.\n"
                "- The worker daemon is currently loaded and draining the inbox right now.\n"
            )
            calls = {"n": 0}

            def fake_frontier(claim, code):
                calls["n"] += 1
                return "SUPPORTED"

            res = freshness.validate_memory(
                memory, root, verify_fn=fake_frontier, runtime_facts="worker: loaded",
                min_len=20, max_workers=1)

        # Something went to the frontier, and the tier split sums to n_frontier (telemetry is honest).
        self.assertGreater(res.n_frontier, 0)
        self.assertEqual(sum(res.tier_calls.values()), res.n_frontier)
        # Only real tiers appear.
        self.assertTrue(set(res.tier_calls).issubset({"haiku", "sonnet", "opus"}))


if __name__ == "__main__":
    unittest.main()
