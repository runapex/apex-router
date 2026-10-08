"""GPT tiers in the route log: pi on the subscription overlay runs its Claude-named families on
openai-codex GPT models. Those outcomes get GPT tiers (gpt-luna < gpt-terra < gpt-sol <
gpt-sol-6.1), never a Claude tier; Claude <-> GPT moves are cross_family, excluded from rates.

Hermetic: temp logs; the registry is the repo's subscription overlay written into the sandboxed
home (tests/conftest.py points HOME at a temp dir).
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from apex_router import cli as amr_cli  # noqa: E402
from apex_router import model_registry, route_conformance, route_join, route_log  # noqa: E402

OVERLAY = ROOT / "integrations" / "pi" / "registry-overlay.subscription.json"
S1 = "03aa612d-7c16-4bd5-8db8-2cc6481e81e0"


def _w(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _classic(tt, model, escalated, *, ts=100.0, family=None):
    row = {"ts": ts, "task_type": tt, "model": model, "passed": not escalated,
           "escalated": escalated, "note": "turn"}
    if family:
        row["family"] = family
    return row


def _dispatch(ts, tier, desc, agent_id, tool_use_id, *, tt="explore", resolved=None):
    return {"ts": ts, "task_type": tt, "model": tier, "passed": None, "escalated": False,
            "note": "agent:general-purpose", "session_id": S1, "label_pending": True,
            "outcome": "ok", "surface": "claude-code", "agent_id": agent_id,
            "tool_use_id": tool_use_id, "start_tier": tier, "description": desc,
            **({"resolved_model": resolved} if resolved else {})}


class TestTierOf(unittest.TestCase):
    def test_gpt_ids_map_to_gpt_tiers(self):
        cases = {
            "gpt-5.6-luna": "gpt-luna", "gpt-5.6-terra": "gpt-terra", "gpt-5.6-sol": "gpt-sol",
            "gpt-6.1-sol": "gpt-sol-6.1", "it-entra-gpt-6.1-sol": "gpt-sol-6.1",
            "openai-codex/gpt-5.6-terra": "gpt-terra", "GPT-5.6-Luna": "gpt-luna",
            "gpt-luna": "gpt-luna", "gpt-terra": "gpt-terra", "gpt-sol": "gpt-sol",
            "gpt-sol-6.1": "gpt-sol-6.1",
        }
        for name, want in cases.items():
            with self.subTest(name=name):
                self.assertEqual(route_log.tier_of(name), want)

    def test_every_pi_family_model_in_the_overlay_maps_to_a_gpt_tier(self):
        fams = json.loads(OVERLAY.read_text())["pi_families"]
        for name, spec in fams.items():
            with self.subTest(family=name):
                t = route_log.tier_of(spec["id"])
                self.assertEqual(route_log.tier_family(t), route_log.GPT_FAMILY)

    def test_gpt_names_never_resolve_to_a_claude_tier(self):
        # Unknown gpt ids are None — never retried against the Claude substrings.
        for name in ("gpt-4o", "gpt-5.6-opus-ish", "gpt-sonnet", "gpt-haiku-x", "gpt"):
            with self.subTest(name=name):
                self.assertNotIn(route_log.tier_of(name), route_log.TIER_RANK)

    def test_claude_mapping_unchanged(self):
        for name, want in {"claude-sonnet-5-5": "sonnet", "it-entra-claude-opus-5-5": "opus",
                           "haiku": "haiku", "claude-fable-5-1": "fable",
                           "kimi-k2.6": None, "inherit": None}.items():
            with self.subTest(name=name):
                self.assertEqual(route_log.tier_of(name), want)

    def test_rank_order_and_cross_family(self):
        r = route_log.ALL_TIER_RANK
        self.assertLess(r["gpt-luna"], r["gpt-terra"])
        self.assertLess(r["gpt-terra"], r["gpt-sol"])
        self.assertLess(r["gpt-sol"], r["gpt-sol-6.1"])
        self.assertTrue(route_log.is_cross_family("haiku", "gpt-sol"))
        self.assertTrue(route_log.is_cross_family("gpt-luna", "opus"))
        self.assertFalse(route_log.is_cross_family("gpt-luna", "gpt-sol"))
        self.assertFalse(route_log.is_cross_family("haiku", None))
        self.assertIn("gpt-luna", route_log.CHEAP_START_TIERS)
        self.assertIn("gpt-terra", route_log.CHEAP_START_TIERS)


class TestReadRates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.log = d / "route_log.jsonl"
        self.labeled = d / "labeled_table.jsonl"
        _w(self.labeled, [])

    def tearDown(self):
        self.tmp.cleanup()

    def test_gpt_rows_get_gpt_tier_rows_and_never_a_claude_rate(self):
        _w(self.log, [
            _classic("explore", "claude-haiku-4-5", False),
            _classic("explore", "claude-haiku-4-5", True),
            _classic("explore", "gpt-5.6-terra", True, family="sonnet"),
            _classic("explore", "gpt-5.6-terra", True, family="sonnet"),
            _classic("explore", "gpt-5.6-luna", False, family="haiku"),
        ])
        rates = route_log.read_rates(log_path=self.log, labeled_path=self.labeled)
        cell = rates["explore"]
        # Headline (Claude-priced) rate: the two Claude rows only.
        self.assertEqual((cell["n"], cell["escalated"]), (2, 1))
        self.assertEqual(cell["by_tier"]["haiku"], {"n": 2, "escalated": 1, "rate": 0.5})
        self.assertEqual(cell["by_tier"]["gpt-terra"], {"n": 2, "escalated": 2, "rate": 1.0})
        self.assertEqual(cell["by_tier"]["gpt-luna"], {"n": 1, "escalated": 0, "rate": 0.0})
        # The family label (sonnet/haiku) never pulls a GPT row into a Claude tier.
        self.assertNotIn("sonnet", cell["by_tier"])
        for t in route_log.TIER_RANK:
            self.assertEqual(cell["by_tier"].get(t, {"n": 0})["n"],
                             2 if t == "haiku" else 0)

    def test_gpt_only_task_type_has_empty_headline(self):
        _w(self.log, [_classic("review", "gpt-6.1-sol", False, family="review")])
        cell = route_log.read_rates(log_path=self.log, labeled_path=self.labeled)["review"]
        self.assertEqual(cell["n"], 0)
        self.assertEqual(cell["by_tier"], {"gpt-sol-6.1": {"n": 1, "escalated": 0, "rate": 0.0}})

    def test_unknown_models_land_in_other_and_stay_in_headline(self):
        _w(self.log, [_classic("explore", "kimi-k2.6", False), _classic("explore", "auto", True)])
        cell = route_log.read_rates(log_path=self.log, labeled_path=self.labeled)["explore"]
        self.assertEqual((cell["n"], cell["escalated"]), (2, 1))
        self.assertEqual(cell["by_tier"]["other"]["n"], 2)

    def test_cross_family_rows_are_excluded_from_every_rate(self):
        _w(self.log, [_classic("explore", "claude-haiku-4-5", False)])
        _w(self.labeled, [
            {"ts": 1.0, "task_type": "explore", "escalated": True, "surface": "claude-code",
             "requested_tier": "haiku", "effective_tier": "haiku", "tier": "haiku",
             "resolved_model": "gpt-5.6-sol", "cross_family": True, "label_status": "labeled"},
            # Not flagged, but requested/resolved families differ: still excluded.
            {"ts": 2.0, "task_type": "explore", "escalated": True, "surface": "claude-code",
             "requested_tier": "sonnet", "effective_tier": "sonnet",
             "resolved_model": "gpt-5.6-terra", "label_status": "labeled"},
        ])
        cell = route_log.read_rates(log_path=self.log, labeled_path=self.labeled)["explore"]
        self.assertEqual((cell["n"], cell["escalated"]), (1, 0))
        self.assertEqual(set(cell["by_tier"]), {"haiku"})

    def test_route_advise_never_sees_gpt_outcomes(self):
        from apex_router import route_advise
        _w(self.log, [_classic("explore", "gpt-5.6-luna", True, ts=float(i)) for i in range(40)])
        rates = route_log.read_rates(log_path=self.log, labeled_path=self.labeled)
        rec = route_advise.advise(rates=rates)["explore"]
        self.assertEqual(rec["n"], 0)


class TestFamilyField(unittest.TestCase):
    def test_log_outcome_writes_family(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.jsonl"
            self.assertTrue(route_log.log_outcome("explore", "gpt-5.6-terra", "ok",
                                                  log_path=p, family="sonnet"))
            row = json.loads(p.read_text())
            self.assertEqual((row["model"], row["family"]), ("gpt-5.6-terra", "sonnet"))
            self.assertFalse(route_log.log_outcome("explore", "gpt-5.6-terra", "ok",
                                                   log_path=p, family=3))

    def test_family_absent_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.jsonl"
            route_log.log_outcome("explore", "claude-haiku-4-5", "ok", log_path=p)
            self.assertNotIn("family", json.loads(p.read_text()))

    def test_cli_family_flag(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.jsonl"
            with mock.patch.dict(os.environ, {"APEX_ROUTER_LOG": str(p)}):
                rc = amr_cli.main(["route-log", "--task-type", "explore", "--start-tier",
                                   "gpt-5.6-sol", "--outcome", "escalated", "--family", "opus"])
            self.assertEqual(rc, 0)
            row = json.loads(p.read_text())
            self.assertEqual((row["model"], row["family"], row["escalated"]),
                             ("gpt-5.6-sol", "opus", True))

    def test_pi_extension_passes_family(self):
        ts = (ROOT / "integrations" / "pi" / "apex-route.ts").read_text()
        self.assertIn('args.push("--family", family)', ts)
        self.assertEqual(ts.count("contextSize, family)") + ts.count("contextSize,\n\t\t\t\tcue.family)")
                         + ts.count("contextSize, cue.family)"), 3)


class TestRouteJoin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.log, self.conf, self.tel = d / "route_log.jsonl", d / "conf.jsonl", d / "tel.jsonl"
        _w(self.tel, [])

    def tearDown(self):
        self.tmp.cleanup()

    def _join(self):
        return route_join.join_labels(self.log, self.conf, self.tel)

    def test_classic_pi_rows_get_gpt_tier_and_family(self):
        _w(self.log, [_classic("explore", "gpt-5.6-terra", True, family="sonnet"),
                      _classic("explore", "claude-haiku-4-5", False, ts=110.0)])
        _w(self.conf, [
            {"ts": 100.0, "surface": "pi", "task_type": "explore", "requested_tier": "sonnet",
             "resolved_model": "gpt-5.6-terra", "matched": True},
            {"ts": 110.0, "surface": "pi", "task_type": "explore", "requested_tier": "haiku",
             "resolved_model": "claude-haiku-4-5", "matched": True},
        ])
        res = self._join()
        rows = {r["model"]: r for r in res["table"]}
        gpt = rows["gpt-5.6-terra"]
        self.assertEqual((gpt["tier"], gpt["tier_family"], gpt["family"], gpt["cross_family"]),
                         ("gpt-terra", "gpt", "sonnet", False))
        rates = route_join.cell_rates(res["table"])["explore"]
        self.assertEqual((rates["n"], rates["escalated"]), (1, 0))
        self.assertEqual(rates["by_tier"]["gpt-terra"]["escalated"], 1)
        self.assertEqual(rates["by_tier"]["haiku"]["n"], 1)

    def test_cheap_claude_then_gpt_redo_is_cross_family_not_escalation(self):
        _w(self.conf, [])
        _w(self.log, [
            _dispatch(100.0, "haiku", "Map the code", "a1", "t1"),
            _dispatch(200.0, "inherit", "map the code", "a2", "t2", resolved="gpt-5.6-sol"),
        ])
        res = self._join()
        a = next(r for r in res["table"] if r["tool_use_id"] == "t1")
        self.assertFalse(a["escalated"])
        self.assertTrue(a["cross_family"])
        self.assertEqual(res["stats"]["escalated_inferred"], 0)
        self.assertEqual(res["stats"]["cross_family"], 1)
        self.assertEqual(route_join.cell_rates(res["table"]).get("explore", {}).get("by_tier", {})
                         .get("haiku"), None)

    def test_escalation_within_gpt_family(self):
        _w(self.conf, [])
        _w(self.log, [
            _dispatch(100.0, "gpt-luna", "Map the code", "a1", "t1"),
            _dispatch(200.0, "gpt-sol", "map the code", "a2", "t2"),
        ])
        res = self._join()
        a = next(r for r in res["table"] if r["tool_use_id"] == "t1")
        self.assertTrue(a["escalated"])
        self.assertFalse(a["cross_family"])
        self.assertEqual(a["tier"], "gpt-luna")

    def test_requested_claude_resolved_gpt_is_cross_family(self):
        _w(self.conf, [])
        _w(self.log, [_dispatch(100.0, "sonnet", "x", "a1", "t1", resolved="gpt-5.6-terra")])
        row = self._join()["table"][0]
        self.assertTrue(row["cross_family"])
        self.assertFalse(row["matched"])

    def test_labeled_gpt_rows_never_reach_a_claude_rate_via_read_rates(self):
        _w(self.conf, [])
        _w(self.log, [
            _dispatch(100.0, "gpt-luna", "Map the code", "a1", "t1"),
            _dispatch(200.0, "gpt-sol", "map the code", "a2", "t2"),
        ])
        labeled = Path(self.tmp.name) / "labeled.jsonl"
        route_join.write_table(self._join()["table"], labeled)
        rates = route_log.read_rates(log_path=self.log, labeled_path=labeled)
        cell = rates["explore"]
        self.assertEqual(cell["n"], 0)
        self.assertEqual(cell["by_tier"], {"gpt-luna": {"n": 1, "escalated": 1, "rate": 1.0}})


class TestConformanceSubscription(unittest.TestCase):
    """route-check on the subscription overlay: a pi family resolved to its GPT model is not
    drift from the Claude tier."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.models = d / "models.json"
        self.models.write_text(OVERLAY.read_text())
        self.conf = d / "conformance.jsonl"
        self.env = mock.patch.dict(os.environ, {"APEX_MODEL_REGISTRY": str(self.models),
                                                "APEX_CONFORMANCE_LOG": str(self.conf)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_expected_models_pi_surface_uses_the_active_family(self):
        reg = model_registry.load()
        self.assertEqual(route_conformance.expected_models("sonnet", surface="pi"), {"gpt-5.6-terra"})
        self.assertEqual(route_conformance.expected_models("fable", surface="pi"), {"gpt-6.1-sol"})
        self.assertEqual(route_conformance.expected_models("haiku", surface="pi", registry=reg),
                         {"gpt-5.6-luna"})
        # The resolve surface still answers in Claude tier ids.
        self.assertEqual(route_conformance.expected_models("sonnet"), {"claude-sonnet-5-5"})

    def test_expected_models_pi_surface_on_defaults_is_the_foundry_id(self):
        self.assertEqual(route_conformance.expected_models("sonnet", surface="pi",
                                                           registry=model_registry.DEFAULTS),
                         {"it-entra-claude-sonnet-5-5"})

    def test_pi_rows_are_not_drift_in_route_check(self):
        for fam, mid in (("sonnet", "gpt-5.6-terra"), ("opus", "gpt-5.6-sol")):
            # As the extension sends it (matched computed TS-side) ...
            route_conformance.main(["--record", json.dumps(
                {"surface": "pi", "task_type": "cue", "requested_tier": fam,
                 "resolved_model": mid, "matched": True})])
            # ... and with the verdict left to route-check.
            route_conformance.main(["--record", json.dumps(
                {"surface": "pi", "task_type": "cue", "requested_tier": fam,
                 "resolved_model": mid})])
        agg = route_conformance.read_conformance()
        cell = agg["pi\tcue"]
        self.assertEqual((cell["n"], cell["observed"], cell["mismatches"]), (4, 4, 0))

    def test_pi_row_resolved_to_a_claude_model_on_the_overlay_is_drift(self):
        route_conformance.main(["--record", json.dumps(
            {"surface": "pi", "task_type": "cue", "requested_tier": "sonnet",
             "resolved_model": "claude-sonnet-5-5"})])
        cell = route_conformance.read_conformance()["pi\tcue"]
        self.assertEqual(cell["mismatches"], 1)


class TestReadoutCli(unittest.TestCase):
    def test_route_readout_prints_per_tier_rows(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "route_log.jsonl"
            _w(log, [_classic("explore", "claude-haiku-4-5", False),
                     _classic("explore", "gpt-5.6-terra", True, family="sonnet")])
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"APEX_ROUTER_LOG": str(log)}), \
                    contextlib.redirect_stdout(buf):
                amr_cli.main(["route-readout"])
            out = buf.getvalue()
            self.assertIn("explore", out)
            self.assertRegex(out, r"\n  haiku\s+1\s+0\s+0\.00")
            self.assertRegex(out, r"\n  gpt-terra\s+1\s+1\s+1\.00")


if __name__ == "__main__":
    unittest.main()
