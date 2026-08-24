"""Tests for amr.route_resolve — the live consumer binding of the routing core.

Hermetic: embedding is disabled (embed_fn=None) or stubbed; the route table is a tmp file;
the registry is injected. No ollama, no network, no home-dir state.
"""
import json
import tempfile
import unittest
from pathlib import Path

from apex_router import route_resolve


REG = {
    "tiers": {"haiku": "H", "sonnet": "S", "opus": "O"},
    "pi_families": {},
    "learn": {},
}


def _table(tmp: Path, cells) -> Path:
    p = tmp / "route_table.skill.json"
    p.write_text(json.dumps({"schema_version": 1, "venue": "skill",
                             "generated_from": {}, "cells": cells, "dropped_routes": []}))
    return p


class TestResolveText(unittest.TestCase):
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


class TestVenueResolution(unittest.TestCase):
    VENUE_REG = {
        "tiers": {"haiku": "H", "sonnet": "S", "opus": "O"},
        "pi_families": {},
        "learn": {},
        "venues": {"codex": {"provider": "moonshotai", "default_model": "kimi-k3",
                             "downshift_model": "kimi-k2.7-code",
                             "downshift_ctx_ceiling": 250_000}},
    }

    def test_codex_venue_defaults_to_venue_model(self):
        with tempfile.TemporaryDirectory() as d:
            tp = _table(Path(d), [])
            out = route_resolve.resolve_text("port the config", venue="codex",
                                             table_path=tp, registry=self.VENUE_REG,
                                             embed_fn=None)
            self.assertEqual(out["model"], "kimi-k3")          # venue default, not a Claude tier
            self.assertEqual(out["venue"], "codex")
            self.assertEqual(out["venue_policy"]["downshift_model"], "kimi-k2.7-code")

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
