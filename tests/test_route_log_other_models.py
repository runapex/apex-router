"""The `other` tier names its models locally: by_tier["other"]["models"] counts the raw model
names that map to no known tier, so a high `other` bounce rate can be traced. The mesh dataset
never carries those names: it gets only `unmapped_ids` (the number of distinct names).

Hermetic: temp logs (tests/conftest.py points HOME at a temp dir).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from apex_router import route_join, route_log  # noqa: E402
from apex_router.mesh import datasets  # noqa: E402


def _w(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _row(tt, model, escalated):
    return {"ts": 100.0, "task_type": tt, "model": model, "passed": not escalated,
            "escalated": escalated, "note": "turn"}


class TestOtherModels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.log, self.labeled = d / "route_log.jsonl", d / "labeled.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_rates_counts_unmapped_names_in_other(self):
        _w(self.log, [_row("debug", "kimi-k2.6", True), _row("debug", "kimi-k2.6", False),
                      _row("debug", "auto", True), _row("debug", "claude-sonnet-5-5", False)])
        cell = route_log.read_rates(log_path=self.log, labeled_path=self.labeled)["debug"]
        other = cell["by_tier"]["other"]
        self.assertEqual((other["n"], other["escalated"]), (3, 2))
        self.assertEqual(other["models"], {"kimi-k2.6": 2, "auto": 1})
        self.assertNotIn("models", cell["by_tier"]["sonnet"])
        self.assertEqual((cell["n"], cell["escalated"]), (4, 2))  # headline unchanged

    def test_missing_and_long_names_are_bounded(self):
        _w(self.log, [{"ts": 1.0, "task_type": "debug", "escalated": False},
                      _row("debug", "x" * 500, False)])
        models = route_log.read_rates(log_path=self.log, labeled_path=self.labeled)[
            "debug"]["by_tier"]["other"]["models"]
        self.assertEqual(models["(none)"], 1)
        self.assertTrue(all(len(k) <= route_log.MODEL_NAME_MAX for k in models))

    def test_distinct_names_are_capped(self):
        rows = [_row("debug", f"local-{i}", False) for i in range(route_log.MODEL_NAMES_MAX + 5)]
        _w(self.log, rows)
        models = route_log.read_rates(log_path=self.log, labeled_path=self.labeled)[
            "debug"]["by_tier"]["other"]["models"]
        self.assertEqual(len(models), route_log.MODEL_NAMES_MAX + 1)
        self.assertEqual(models["(more)"], 5)

    def test_cell_rates_counts_unmapped_names_in_other(self):
        table = [{"task_type": "review", "escalated": True, "model": "kimi-k2.6"},
                 {"task_type": "review", "escalated": False, "model": "claude-opus-5-5"}]
        by_tier = route_join.cell_rates(table)["review"]["by_tier"]
        self.assertEqual(by_tier["other"]["models"], {"kimi-k2.6": 1})
        self.assertNotIn("models", by_tier["opus"])

    def test_redact_models_keeps_only_the_count(self):
        rates = {"debug": {"n": 3, "escalated": 2, "rate": 2 / 3, "null_ts": 0, "by_tier": {
            "other": {"n": 3, "escalated": 2, "rate": 2 / 3, "models": {"kimi-k2.6": 2, "auto": 1}},
            "sonnet": {"n": 1, "escalated": 0, "rate": 0.0}}}}
        out = route_log.redact_models(rates)
        self.assertEqual(out["debug"]["by_tier"]["other"],
                         {"n": 3, "escalated": 2, "rate": 2 / 3, "unmapped_ids": 2})
        self.assertIn("models", rates["debug"]["by_tier"]["other"])  # input not mutated
        self.assertEqual(route_log.redact_models({"bad": 1}), {"bad": 1})

    def test_mesh_dataset_never_carries_model_names(self):
        _w(self.log, [_row("debug", "kimi-k2.6", True)])
        from unittest import mock
        with mock.patch.object(route_log, "default_log_path", return_value=self.log):
            data = datasets._route_rates()
        self.assertNotIn("kimi", json.dumps(data))
        self.assertEqual(data["debug"]["by_tier"]["other"]["unmapped_ids"], 1)

    def test_route_readout_prints_other_model_names(self):
        import contextlib
        import io
        import os
        from unittest import mock
        from apex_router import cli as amr_cli
        _w(self.log, [_row("debug", "kimi-k2.6", True), _row("debug", "kimi-k2.6", False),
                      _row("debug", "auto", True)])
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"APEX_ROUTER_LOG": str(self.log),
                                          "APEX_LABELED_TABLE": str(self.labeled)}), \
                contextlib.redirect_stdout(buf):
            amr_cli.main(["route-readout"])
        self.assertIn("    models: kimi-k2.6 x2, auto x1", buf.getvalue())

    def test_format_models_limits_the_list(self):
        models = {f"m{i}": i + 1 for i in range(10)}
        self.assertEqual(route_log.format_models(models, limit=2), "m9 x10, m8 x9, +8 more")
        self.assertEqual(route_log.format_models({}), "")


if __name__ == "__main__":
    unittest.main()
