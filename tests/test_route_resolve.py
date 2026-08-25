"""Tests for amr.route_resolve — the live consumer binding of the routing core.

Hermetic: embedding is disabled (embed_fn=None) or stubbed; the route table is a tmp file;
the registry is injected. No ollama, no network, no home-dir state.
"""
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from apex_router import route_resolve


REG = {
    "tiers": {"haiku": "H", "sonnet": "S", "opus": "O"},
    "pi_families": {},
    "learn": {},
}


class _EnvIsolatedTestCase(unittest.TestCase):
    """Every test in this file can reach a default-path conformance emit via resolve_text().
    Isolate both log env vars to per-test temp files and restore the previous values after."""

    def setUp(self):
        self._prev_conformance_log = os.environ.get("APEX_CONFORMANCE_LOG")
        self._prev_router_log = os.environ.get("APEX_ROUTER_LOG")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["APEX_CONFORMANCE_LOG"] = str(Path(self._tmp.name) / "conformance.jsonl")
        os.environ["APEX_ROUTER_LOG"] = str(Path(self._tmp.name) / "route_log.jsonl")

    def tearDown(self):
        self._tmp.cleanup()
        if self._prev_conformance_log is None:
            os.environ.pop("APEX_CONFORMANCE_LOG", None)
        else:
            os.environ["APEX_CONFORMANCE_LOG"] = self._prev_conformance_log
        if self._prev_router_log is None:
            os.environ.pop("APEX_ROUTER_LOG", None)
        else:
            os.environ["APEX_ROUTER_LOG"] = self._prev_router_log


def _table(tmp: Path, cells) -> Path:
    p = tmp / "route_table.skill.json"
    p.write_text(json.dumps({"schema_version": 1, "venue": "skill",
                             "generated_from": {}, "cells": cells, "dropped_routes": []}))
    return p


class TestResolveText(_EnvIsolatedTestCase):
    def test_empty_table_falls_back_to_static_map(self):
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), [])
            out = route_resolve.resolve_text(
                "fix the crash in the parser", tools=["edit", "write"],
                table_path=tp, registry=REG, embed_fn=None)
            # mutation tools -> refactor prior (0.6) < min_confidence -> static for refactor
            self.assertEqual(out["model"], "S")
            self.assertEqual(out["task_type"], "refactor")
            self.assertEqual(out["source"], "static_default_low_confidence")
            self.assertEqual(out["cell"], "task:refactor")

    def test_nothing_discriminating_resolves_to_safe_default(self):
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), [])
            out = route_resolve.resolve_text("hello", table_path=tp, registry=REG,
                                             embed_fn=None)
            self.assertEqual(out["model"], "O")  # safe default = opus tier
            self.assertEqual(out["source"], "static_default_low_confidence")

    def test_promoted_table_cell_wins_over_static(self):
        cells = [{"cell_id": "task:refactor", "parent_task_type": "S",
                  "promoted": True, "chosen_model": "H",
                  "ranking": [{"model": "H", "quality": 0.9, "quality_ci": [0.8, 0.99],
                               "cost_usd": 0.01, "latency": 1.0, "provenance": "objective",
                               "n": 50}]}]
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), cells)
            # sys marker gives a confident classification (0.9) so the table is consulted.
            out = route_resolve.resolve_text("x", sys_markers=["refactor"],
                                             table_path=tp, registry=REG, embed_fn=None)
            self.assertEqual(out["model"], "H")
            self.assertEqual(out["source"], "route_table")
            self.assertEqual(out["table_cell"], {"promoted": True, "chosen": "H"})

    def test_unpromoted_cell_falls_back(self):
        cells = [{"cell_id": "task:refactor", "parent_task_type": "S",
                  "promoted": False, "chosen_model": "S", "ranking": []}]
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), cells)
            out = route_resolve.resolve_text("x", sys_markers=["refactor"],
                                             table_path=tp, registry=REG, embed_fn=None)
            self.assertEqual(out["model"], "S")
            self.assertEqual(out["source"], "static_default")

    def test_missing_table_file_is_safe(self):
        with tempfile.TemporaryDirectory() as d:
            tp = Path(d) / "nope.json"
            out = route_resolve.resolve_text("x", sys_markers=["debug"],
                                             table_path=tp, registry=REG, embed_fn=None)
            self.assertEqual(out["model"], "O")  # debug static = opus tier
            self.assertIsNone(out["table_cell"])

    def test_embedding_refinement_can_flip_class(self):
        # Stub embed_fn: query matches the "explore" exemplars strongly, others weakly.
        def fake_embed(text):
            t = text.lower()
            if "where is" in t or "callers" in t or "auth" in t:
                return [1.0, 0.0]
            return [0.0, 1.0]
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), [])
            out = route_resolve.resolve_text(
                "where is the auth middleware wired", tools=["read"],
                table_path=tp, registry=REG, embed_fn=fake_embed)
            self.assertEqual(out["task_type"], "explore")
            self.assertEqual(out["embedding"], "on")


if __name__ == "__main__":
    unittest.main()


class TestVenueResolution(_EnvIsolatedTestCase):
    VENUE_REG = {
        "tiers": {"haiku": "H", "sonnet": "S", "opus": "O"},
        "pi_families": {},
        "learn": {},
        "venues": {"codex": {"provider": "moonshotai", "default_model": "kimi-k3",
                             "downshift_model": "kimi-k2.7-code",
                             "downshift_ctx_ceiling": 250_000}},
    }

    def test_codex_venue_routes_within_family(self):
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), [])
            out = route_resolve.resolve_text("port the config", venue="codex",
                                             table_path=tp, registry=self.VENUE_REG,
                                             embed_fn=None)
            # code-shaped task under the ctx floor -> downshift code model (K2), not a Claude tier
            self.assertEqual(out["model"], "kimi-k2.7-code")
            self.assertEqual(out["venue"], "codex")
            self.assertIn("under floor", out["venue_policy"]["route_reason"])

    def test_deep_ctx_forces_k3(self):
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), [])
            out = route_resolve.resolve_text("continue the migration", venue="codex",
                                             ctx_tokens=400_000, table_path=tp,
                                             registry=self.VENUE_REG, embed_fn=None)
            self.assertEqual(out["model"], "kimi-k3")   # 1M window load-bearing
            self.assertIn("deep floor", out["venue_policy"]["route_reason"])

    def test_kimi_venue_general_task_routes_cheapest(self):
        reg = dict(self.VENUE_REG)
        reg["venues"] = {"kimi": {"provider": "moonshotai", "default_model": "kimi-k2.6",
                                  "code_model": "kimi-k2.7-code", "deep_ctx_model": "kimi-k3",
                                  "deep_ctx_floor": 250_000}}
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), [])
            out = route_resolve.resolve_text("x", sys_markers=["explore"], venue="kimi",
                                             table_path=tp, registry=reg, embed_fn=None)
            self.assertEqual(out["model"], "kimi-k2.6")

    def test_skill_venue_unaffected_by_venue_policy(self):
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), [])
            out = route_resolve.resolve_text("x", venue="skill", table_path=tp,
                                             registry=self.VENUE_REG, embed_fn=None)
            self.assertEqual(out["model"], "O")                # opus tier safe default
            self.assertNotIn("venue_policy", out)

    def test_unknown_venue_falls_back_to_skill_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), [])
            out = route_resolve.resolve_text("x", venue="atlantis", table_path=tp,
                                             registry=self.VENUE_REG, embed_fn=None)
            self.assertEqual(out["model"], "O")


class TestConformanceEmitGate(_EnvIsolatedTestCase):
    """P1-b: the tier-conformance emitter fires ONLY for static skill resolutions. A venue
    route (kimi/codex) or a promoted route-table cell INTENTIONALLY returns a model off the
    static tier map, so emitting a conformance row for it would be false drift."""

    VENUE_REG = {
        "tiers": {"haiku": "H", "sonnet": "S", "opus": "O"},
        "pi_families": {},
        "learn": {},
        "venues": {"codex": {"provider": "moonshotai", "default_model": "kimi-k3",
                             "downshift_model": "kimi-k2.7-code",
                             "downshift_ctx_ceiling": 250_000}},
    }

    def _rows(self, log_path):
        if not log_path.exists():
            return []
        return [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]

    def test_venue_route_emits_no_conformance_row(self):
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), [])
            log = Path(d) / "c.jsonl"
            with mock.patch.dict(os.environ, {"APEX_CONFORMANCE_LOG": str(log)}):
                out = route_resolve.resolve_text("port the config", venue="codex",
                                                 table_path=tp, registry=self.VENUE_REG,
                                                 embed_fn=None)
            self.assertEqual(out["model"], "kimi-k2.7-code")  # venue route (off static map)
            self.assertEqual(self._rows(log), [])             # NO conformance row written

    def test_promoted_route_table_cell_emits_no_conformance_row(self):
        cells = [{"cell_id": "task:refactor", "parent_task_type": "S",
                  "promoted": True, "chosen_model": "H",
                  "ranking": [{"model": "H", "quality": 0.9, "quality_ci": [0.8, 0.99],
                               "cost_usd": 0.01, "latency": 1.0, "provenance": "objective",
                               "n": 50}]}]
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), cells)
            log = Path(d) / "c.jsonl"
            with mock.patch.dict(os.environ, {"APEX_CONFORMANCE_LOG": str(log)}):
                out = route_resolve.resolve_text("x", sys_markers=["refactor"],
                                                 table_path=tp, registry=REG, embed_fn=None)
            self.assertEqual(out["source"], "route_table")   # promoted cell (off static map)
            self.assertEqual(self._rows(log), [])            # NO conformance row written

    def test_static_skill_resolution_still_emits_a_row(self):
        # the legitimate case must still be observed: a static skill resolve logs one row.
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), [])
            log = Path(d) / "c.jsonl"
            with mock.patch.dict(os.environ, {"APEX_CONFORMANCE_LOG": str(log)}):
                out = route_resolve.resolve_text("hello", table_path=tp, registry=REG,
                                                 embed_fn=None)
            rows = self._rows(log)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["surface"], "resolve")
            self.assertEqual(rows[0]["task_type"], out["task_type"])
