"""Local-model tier selection + switching — pure/offline.

Everything here injects env and paths; nothing touches ollama, launchd or the network. The parts
that DO need a live backend (warm/unload) are exercised only for their failure handling.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apex_router.ornith import local_tier, model_router, tier_switch


class TestResolve(unittest.TestCase):
    def test_default_when_nothing_set(self):
        t = local_tier.resolve(env={}, state_file=Path("/nonexistent/ornith.env"))
        self.assertEqual(t.name, local_tier.DEFAULT_TIER)

    def test_env_wins_over_state_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ornith.env"
            p.write_text("ORNITH_TIER=small\n")
            t = local_tier.resolve(env={"ORNITH_TIER": "large"}, state_file=p)
            self.assertEqual(t.name, "large")

    def test_state_file_used_when_env_absent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ornith.env"
            p.write_text("# comment\n\nORNITH_TIER = large \nORNITH_URL=http://x\n")
            self.assertEqual(local_tier.resolve(env={}, state_file=p).name, "large")

    def test_unknown_tier_falls_back_not_raises(self):
        # A bad tier name must never be why a batch job dies.
        t = local_tier.resolve(env={"ORNITH_TIER": "27b"}, state_file=Path("/nonexistent"))
        self.assertEqual(t.name, local_tier.DEFAULT_TIER)

    def test_case_and_whitespace_insensitive(self):
        t = local_tier.resolve(env={"ORNITH_TIER": "  LARGE "}, state_file=Path("/nonexistent"))
        self.assertEqual(t.name, "large")

    def test_pinned_api_model_is_honored_verbatim(self):
        t = local_tier.resolve(env={"ORNITH_API_MODEL": "some/backend:tag"},
                               state_file=Path("/nonexistent"))
        self.assertEqual(t.api_model, "some/backend:tag")
        self.assertEqual(t.name, "pinned")

    def test_pinned_unknown_id_has_zero_size_so_fits_does_not_gate(self):
        t = local_tier.resolve(env={"ORNITH_API_MODEL": "some/backend:tag"},
                               state_file=Path("/nonexistent"))
        ok, why = local_tier.fits(t, total_gb=1.0)  # absurdly small RAM
        self.assertTrue(ok)
        self.assertIn("not gating", why)

    def test_family_plus_tier_resolves_through_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "models.json"
            p.write_text(json.dumps({"local_families": {"acme": {"tiers": {
                "big": {"api_model": "acme/model:tag", "weights_gb": 18}}}}}))
            t = local_tier.resolve(env={"LOCAL_FAMILY": "acme", "ORNITH_TIER": "big"},
                                   state_file=Path("/nonexistent"), overlay_path=p)
            self.assertEqual(t.api_model, "acme/model:tag")

    def test_unknown_family_falls_back_to_default_family(self):
        t = local_tier.resolve(env={"LOCAL_FAMILY": "nope", "ORNITH_TIER": "small"},
                               state_file=Path("/nonexistent"))
        self.assertEqual(t.api_model, local_tier.FAMILIES["ornith"]["small"].api_model)


class TestTierTable(unittest.TestCase):
    def test_no_27b_exists(self):
        # Ornith 1.5 ships 9B / 35B-A3B / 397B. This asserts the documented reality so a future
        # edit cannot quietly reintroduce a size that has no weights behind it.
        self.assertEqual(set(local_tier.TIERS), {"small", "large"})

    def test_large_is_moe_small_is_dense(self):
        self.assertTrue(local_tier.TIERS["large"].is_moe)
        self.assertFalse(local_tier.TIERS["small"].is_moe)

    def test_models_are_ollama_ids_not_mlx_paths(self):
        for t in local_tier.TIERS.values():
            self.assertNotIn("mlx-community", t.api_model)

    def test_ornith_is_the_default_committed_family(self):
        self.assertEqual(local_tier.DEFAULT_FAMILY, "ornith")
        self.assertEqual(set(local_tier.FAMILIES["ornith"]), {"small", "large"})

    def test_TIERS_is_backcompat_alias_of_default_family(self):
        self.assertIs(local_tier.TIERS, local_tier.FAMILIES[local_tier.DEFAULT_FAMILY])


class TestLoadFamilies(unittest.TestCase):
    def test_default_only_when_no_overlay(self):
        fams = local_tier.load_families(overlay_path=Path("/nonexistent/models.json"))
        self.assertEqual(set(fams), {"ornith"})

    def test_overlay_adds_a_family(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "models.json"
            p.write_text(json.dumps({"local_families": {"acme": {"tiers": {
                "big": {"api_model": "acme/model:tag", "weights_gb": 18,
                        "active_b": 27, "total_b": 27, "note": "test"}}}}}))
            fams = local_tier.load_families(overlay_path=p)
            self.assertIn("acme", fams)
            self.assertEqual(fams["acme"]["big"].api_model, "acme/model:tag")
            self.assertIn("ornith", fams)   # base family survives the merge

    def test_malformed_overlay_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "models.json"
            p.write_text("{ not json")
            self.assertEqual(set(local_tier.load_families(overlay_path=p)), {"ornith"})


class TestFits(unittest.TestCase):
    def test_large_fits_36gb(self):
        ok, why = local_tier.fits(local_tier.TIERS["large"], total_gb=36.0)
        self.assertTrue(ok, why)

    def test_large_does_not_fit_16gb(self):
        ok, _ = local_tier.fits(local_tier.TIERS["large"], total_gb=16.0)
        self.assertFalse(ok)

    def test_small_fits_16gb(self):
        ok, _ = local_tier.fits(local_tier.TIERS["small"], total_gb=16.0)
        self.assertTrue(ok)

    def test_unknown_ram_does_not_gate(self):
        # Refuse to block work on a number we could not measure.
        ok, why = local_tier.fits(local_tier.TIERS["large"], total_gb=0.0)
        self.assertTrue(ok)
        self.assertIn("not gating", why)


class TestClientEnv(unittest.TestCase):
    def test_carries_model_and_reasoning_effort_style(self):
        env = local_tier.client_env(local_tier.TIERS["large"])
        self.assertEqual(env["ORNITH_API_MODEL"], local_tier.TIERS["large"].api_model)
        # ollama gates thinking with reasoning_effort, not chat_template_kwargs.
        self.assertEqual(env["ORNITH_THINKING_STYLE"], "reasoning_effort")

    def test_render_state_roundtrips_through_resolve(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ornith.env"
            p.write_text(local_tier.render_state(local_tier.TIERS["large"]))
            self.assertEqual(local_tier.resolve(env={}, state_file=p).name, "large")

    def test_client_env_includes_family(self):
        env = local_tier.client_env(local_tier.FAMILIES["ornith"]["large"])
        self.assertEqual(env["LOCAL_FAMILY"], "ornith")

    def test_render_state_roundtrips_family_and_tier(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ornith.env"
            p.write_text(local_tier.render_state(local_tier.FAMILIES["ornith"]["large"]))
            t = local_tier.resolve(env={}, state_file=p, overlay_path=Path("/nonexistent"))
            self.assertEqual(t.name, "large")


class TestRouteSelection(unittest.TestCase):
    def test_route_carries_explicit_model(self):
        # The retired MLX server accepted a missing model; ollama 400s on it.
        r = model_router.select(task="synthesis")
        self.assertTrue(r.model)
        self.assertIsInstance(r.model, str)

    def test_bulk_routes_to_small_instead_of_declining(self):
        # Under the single-model setup this was a hard decline. With tiers it is the small lane.
        r = model_router.select(task="triage")
        self.assertTrue(r.fits)
        self.assertEqual(r.tier, "small")

    def test_fidelity_task_routes_large(self):
        self.assertEqual(model_router.select(task="synthesis").tier, "large")

    def test_explicit_tier_beats_task_implication(self):
        r = model_router.select(task="synthesis", tier="small")
        self.assertEqual(r.tier, "small")

    def test_envelope_miss_still_declines(self):
        r = model_router.select(task="synthesis", items=999)
        self.assertFalse(r.fits)

    def test_oversized_item_declines(self):
        r = model_router.select(task="extract", item_bytes=200_000)
        self.assertFalse(r.fits)

    def test_qwen_override_still_rejected(self):
        # Isolate from the machine-local families overlay so the assertion holds regardless of
        # what the developer's ~/.apex-router/models.json declares: patch load_families to the
        # committed-only set, so the override is genuinely not a known family.
        with mock.patch.object(local_tier, "load_families",
                               return_value={"ornith": dict(local_tier.FAMILIES["ornith"])}):
            with self.assertRaises(ValueError):
                model_router.select(override="qwen")

    def test_override_accepts_a_configured_family(self):
        # "ornith" is a committed family; overriding to it must not raise.
        r = model_router.select(task="synthesis", override="ornith")
        self.assertTrue(r.model)

    def test_unknown_override_message_is_generic(self):
        with self.assertRaises(ValueError) as cm:
            model_router.select(override="definitely-not-a-family")
        # Positive lock on the GENERIC message — fails against the old Ornith-only string.
        self.assertIn("known local families:", str(cm.exception))

    def test_override_accepts_overlay_family(self):
        # A family present only via a (mocked) loaded overlay is accepted — this exercises the
        # DYNAMIC lookup, which the hardcoded-tuple code could not satisfy. Keep the REAL ornith
        # tiers in the mocked dict so resolve() (which reads families[DEFAULT_FAMILY][DEFAULT_TIER])
        # still works; add the overlay-only family as a new KEY.
        fams = dict(local_tier.load_families())
        fams["synthetic-local"] = {}
        with mock.patch.object(local_tier, "load_families", return_value=fams):
            r = model_router.select(task="synthesis", override="synthetic-local")
            self.assertTrue(r.model)

    def test_needs_switch_flags_a_non_resident_tier(self):
        with mock.patch.object(local_tier, "resolve",
                               return_value=local_tier.TIERS["small"]):
            r = model_router.select(task="synthesis")
            self.assertEqual(r.tier, "large")
            self.assertTrue(r.needs_switch)


class TestUnloadMatching(unittest.TestCase):
    def test_latest_suffix_matches_without_rstrip_bug(self):
        # str.rstrip(":latest") strips a CHARACTER SET and would mangle ids ending in t/s/e/a/l.
        model = local_tier.TIERS["small"].api_model
        with mock.patch.object(tier_switch, "resident_models",
                               side_effect=[[f"{model}:latest"], []]), \
             mock.patch.object(tier_switch, "unload", return_value=True) as un:
            freed = tier_switch.unload_all_tiers()
        self.assertEqual(freed, [f"{model}:latest"])
        un.assert_called_once()

    def test_unrelated_models_are_not_evicted(self):
        # nomic-embed-text is not ours to unload.
        with mock.patch.object(tier_switch, "resident_models",
                               return_value=["nomic-embed-text:latest"]), \
             mock.patch.object(tier_switch, "unload", return_value=True) as un:
            self.assertEqual(tier_switch.unload_all_tiers(), [])
        un.assert_not_called()

    def test_unload_scope_covers_all_family_tiers(self):
        ids = [t.api_model for tiers in local_tier.load_families().values() for t in tiers.values()]
        with mock.patch.object(tier_switch, "resident_models", side_effect=[[ids[0]], []]), \
             mock.patch.object(tier_switch, "unload", return_value=True) as un:
            tier_switch.unload_all_tiers()
        un.assert_called_once()


class TestSwitchGuards(unittest.TestCase):
    def test_unknown_tier_exits_2(self):
        self.assertEqual(tier_switch.switch("27b"), 2)

    def test_refuses_when_not_pulled(self):
        with mock.patch.object(tier_switch, "pulled_models", return_value=[]), \
             mock.patch.object(local_tier, "total_ram_gb", return_value=36.0):
            self.assertEqual(tier_switch.switch("large"), 1)

    def test_refuses_when_ram_too_small(self):
        with mock.patch.object(local_tier, "total_ram_gb", return_value=16.0):
            self.assertEqual(tier_switch.switch("large"), 1)

    def test_unload_runs_before_state_write(self):
        """Capacity invariant: the outgoing tier must be evicted BEFORE the new one is warmed,
        or both sets of weights are briefly resident (~27 GB on a 36 GB box)."""
        order = []
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(local_tier, "total_ram_gb", return_value=36.0), \
             mock.patch.object(tier_switch, "pulled_models",
                               return_value=[local_tier.TIERS["large"].api_model]), \
             mock.patch.object(tier_switch, "unload_all_tiers",
                               side_effect=lambda: order.append("unload") or []), \
             mock.patch.object(tier_switch, "warm",
                               side_effect=lambda t, **k: (order.append("warm"), (True, "ok"))[1]), \
             mock.patch.object(tier_switch, "reload_consumers", return_value={}):
            rc = tier_switch.switch("large", state_path=Path(d) / "ornith.env")
        self.assertEqual(rc, 0)
        self.assertEqual(order, ["unload", "warm"])

    def test_failed_warm_is_reported_as_failure(self):
        # A tier that is written but not answering must not report success.
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(local_tier, "total_ram_gb", return_value=36.0), \
             mock.patch.object(tier_switch, "pulled_models",
                               return_value=[local_tier.TIERS["small"].api_model]), \
             mock.patch.object(tier_switch, "unload_all_tiers", return_value=[]), \
             mock.patch.object(tier_switch, "warm", return_value=(False, "boom")), \
             mock.patch.object(tier_switch, "reload_consumers", return_value={}):
            rc = tier_switch.switch("small", state_path=Path(d) / "ornith.env")
        self.assertEqual(rc, 1)

    def test_state_write_is_atomic_and_parses(self):
        with tempfile.TemporaryDirectory() as d:
            p = tier_switch.write_state(local_tier.TIERS["large"], Path(d) / "sub" / "ornith.env")
            self.assertTrue(p.exists())
            self.assertFalse(list(p.parent.glob("*.tmp")))
            self.assertEqual(local_tier.resolve(env={}, state_file=p).name, "large")

    def test_switch_accepts_family_and_tier(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(local_tier, "total_ram_gb", return_value=36.0), \
             mock.patch.object(tier_switch, "pulled_models",
                               return_value=[local_tier.FAMILIES["ornith"]["large"].api_model]), \
             mock.patch.object(tier_switch, "unload_all_tiers", return_value=[]), \
             mock.patch.object(tier_switch, "warm", return_value=(True, "ok")), \
             mock.patch.object(tier_switch, "reload_consumers", return_value={}):
            rc = tier_switch.switch("large", family="ornith",
                                    state_path=Path(d) / "ornith.env")
            # resolve INSIDE the with — the state file lives in the TemporaryDirectory, which is
            # removed on exit (matching the sibling test_state_write_is_atomic_and_parses).
            self.assertEqual(rc, 0)
            self.assertEqual(local_tier.resolve(env={}, state_file=Path(d) / "ornith.env",
                                                overlay_path=Path("/nonexistent")).name, "large")


class TestHealthProbe(unittest.TestCase):
    def test_accepts_both_backend_shapes(self):
        from apex_router.ornith import ornith_client as oc
        self.assertTrue(oc._is_healthy({"status": "ok"}))              # MLX / vLLM
        self.assertTrue(oc._is_healthy({"object": "list", "data": []}))  # ollama, no models
        self.assertTrue(oc._is_healthy({"data": [{"id": "x"}]}))
        self.assertFalse(oc._is_healthy({"error": "nope"}))
        self.assertFalse(oc._is_healthy(None))
        self.assertFalse(oc._is_healthy("ok"))

    def test_default_health_path_is_not_slash_health(self):
        # ollama 404s /health; probing it reported a healthy server as dead.
        from apex_router.ornith import ornith_client as oc
        self.assertEqual(oc.HEALTH_PATH, "/v1/models")


class TestUnloadFailureHandling(unittest.TestCase):
    def test_unload_reports_success_when_model_already_gone(self):
        # An error talking to an already-unloaded model is success, not failure.
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")), \
             mock.patch.object(tier_switch, "resident_models", return_value=[]):
            self.assertTrue(tier_switch.unload("anything"))

    def test_unload_reports_failure_when_still_resident(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")), \
             mock.patch.object(tier_switch, "resident_models", return_value=["anything"]):
            self.assertFalse(tier_switch.unload("anything"))


class TestStatusShape(unittest.TestCase):
    def test_status_is_json_serialisable_and_separates_pulled_from_resident(self):
        with mock.patch.object(tier_switch, "resident_models", return_value=[]), \
             mock.patch.object(tier_switch, "pulled_models",
                               return_value=[local_tier.TIERS["small"].api_model]):
            st = tier_switch.status()
        json.dumps(st)  # must not raise
        # "configured" never implies "serving" — ollama loads lazily.
        self.assertIn("configured_is_pulled", st)
        self.assertIn("configured_is_resident", st)
        self.assertFalse(st["configured_is_resident"])


if __name__ == "__main__":
    unittest.main()


class TestPerCallModelOverride(unittest.TestCase):
    """MODEL binds at import, so without a per-call override a caller holding a Route for the
    OTHER tier could not act on it without restarting the process."""

    def test_override_replaces_module_model(self):
        from apex_router.ornith import ornith_client as oc
        body = oc._apply_backend({}, enable_thinking=False, model="other-model")
        self.assertEqual(body["model"], "other-model")

    def test_no_override_uses_module_model(self):
        from apex_router.ornith import ornith_client as oc
        body = oc._apply_backend({}, enable_thinking=False)
        self.assertEqual(body["model"], oc.MODEL)

    def test_thinking_off_uses_reasoning_effort_for_ollama(self):
        from apex_router.ornith import ornith_client as oc
        with mock.patch.object(oc, "THINKING_STYLE", "reasoning_effort"):
            self.assertEqual(
                oc._apply_backend({}, enable_thinking=False)["reasoning_effort"], "none")
            # thinking ON must NOT set the key at all — "none" is the only value that gates.
            self.assertNotIn("reasoning_effort", oc._apply_backend({}, enable_thinking=True))
