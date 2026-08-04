"""Per-lane offload telemetry — the measure-first substrate for local-model offload.

Generalizes codeqa/impact.py's fail-open JSONL writer to ANY local-model lane
(review-prefilter, gated-codegen, fidelity, codeqa). The one field codeqa's impact
log never captured is the 5x-billed slice this whole effort targets: completion_tokens.
So `usage_tokens()` extracting completion_tokens correctly is the load-bearing test.

Pure functions — no live server; the suite runs offline.
"""
import json
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.ornith.offload_telemetry import (  # noqa: E402
    OffloadRecord,
    aggregate_offload,
    usage_tokens,
    write_offload,
)


class TestUsageTokens(unittest.TestCase):
    def test_extracts_all_three_counts_including_completion(self):
        # The exact usage shape the live MLX server returns (verified 2026-08-03).
        usage = {
            "prompt_tokens": 98,
            "completion_tokens": 40,
            "total_tokens": 138,
            "prompt_tokens_details": {"cached_tokens": 76},
        }
        p, c, cached = usage_tokens(usage)
        self.assertEqual(p, 98)
        self.assertEqual(c, 40)   # the slice impact.py dropped
        self.assertEqual(cached, 76)

    def test_missing_details_gives_zero_cached_not_crash(self):
        p, c, cached = usage_tokens({"prompt_tokens": 15, "completion_tokens": 8})
        self.assertEqual((p, c, cached), (15, 8, 0))

    def test_none_usage_is_all_zero(self):
        self.assertEqual(usage_tokens(None), (0, 0, 0))

    def test_malformed_details_type_does_not_crash(self):
        # server could return a non-dict under prompt_tokens_details; must degrade, not raise
        p, c, cached = usage_tokens(
            {"prompt_tokens": 5, "completion_tokens": 3, "prompt_tokens_details": None})
        self.assertEqual((p, c, cached), (5, 3, 0))


class TestWriteAndAggregate(unittest.TestCase):
    def _rec(self, lane, ok, pt, ct, cached, escalated=False, gated=True):
        return OffloadRecord(
            ts=0.0, lane=lane, model="ornith-35b", ok=ok,
            prompt_tokens=pt, completion_tokens=ct, cached_tokens=cached,
            latency_ms=1500, escalated=escalated, gated=gated,
        )

    def test_write_is_fail_open_on_bad_path(self):
        # a directory that cannot be created must NOT raise (instrument never breaks the tool)
        bad = Path("/dev/null/cannot/exist/log.jsonl")
        try:
            write_offload(bad, self._rec("fidelity", True, 10, 5, 0))
        except Exception as e:  # noqa: BLE001
            self.fail(f"write_offload should be fail-open, raised {e!r}")

    def test_roundtrip_and_completion_tokens_persisted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "offload.jsonl"
            write_offload(log, self._rec("codegen", True, 31, 21, 0))
            obj = json.loads(log.read_text().strip())
            self.assertEqual(obj["completion_tokens"], 21)
            self.assertEqual(obj["lane"], "codegen")
            self.assertTrue(obj["ok"])

    def test_aggregate_per_lane_rates_and_savings(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "offload.jsonl"
            # codegen: 2 ok(gated), 1 failed+escalated. The failed call carries completion=99 so a
            # naive sum-all impl would be CAUGHT (Codex xval #5: the old test gave it 0 and proved
            # nothing).
            write_offload(log, self._rec("codegen", True, 30, 20, 0))
            write_offload(log, self._rec("codegen", True, 30, 20, 10))
            write_offload(log, self._rec("codegen", False, 30, 99, 0, escalated=True))
            # review: ok BUT escalated (pre-filter always escalates) -> saves NOTHING.
            write_offload(log, self._rec("review", True, 100, 50, 80, escalated=True, gated=False))
            agg = aggregate_offload(log)

            self.assertEqual(agg["overall"]["n"], 4)
            cg = agg["by_lane"]["codegen"]
            self.assertEqual(cg["n"], 3)
            self.assertEqual(cg["gated"], 3)
            self.assertAlmostEqual(cg["ok_rate"], 2 / 3)   # ok/gated
            self.assertEqual(cg["escalated"], 1)
            # saved = gated AND ok AND not escalated -> 20+20 only; the failed call's 99 excluded.
            self.assertEqual(cg["frontier_completion_tokens_saved"], 40)
            self.assertEqual(cg["cached_tokens"], 10)
            # review saved nothing despite ok=True, because it escalated (Codex xval #4).
            rv = agg["by_lane"]["review"]
            self.assertEqual(rv["frontier_completion_tokens_saved"], 0)

    def test_ungated_completion_never_counts_as_saved(self):
        # a raw worker completion (gated=False, ok=False) must save nothing even with big completion
        # tokens — the worker runs no correctness gate (Codex xval #6).
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "offload.jsonl"
            write_offload(log, self._rec("adhoc", False, 500, 500, 0, gated=False))
            agg = aggregate_offload(log)
            self.assertEqual(agg["by_lane"]["adhoc"]["frontier_completion_tokens_saved"], 0)
            self.assertEqual(agg["overall"]["gated"], 0)

    def test_malformed_records_are_skipped_not_fatal(self):
        # null token counts, a bare JSON scalar, and a good record on the same log — aggregation must
        # survive and still count the good one (Codex xval #11).
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "offload.jsonl"
            log.write_text(
                '{"lane":"codegen","ok":true,"gated":true,"completion_tokens":null}\n'
                '42\n'
                '{"not":"a record"}\n'
                'not json at all\n'
            )
            write_offload(log, self._rec("codegen", True, 10, 7, 0))  # the one good, saving row
            agg = aggregate_offload(log)  # must not raise
            self.assertEqual(agg["by_lane"]["codegen"]["frontier_completion_tokens_saved"], 7)

    def test_string_boolean_does_not_inflate_saved_tokens(self):
        # Codex xval-2 #3: {"gated":"false","ok":"false"} are truthy under bool() but must NOT count.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "offload.jsonl"
            log.write_text(
                '{"lane":"codegen","gated":"false","ok":"false","escalated":"false",'
                '"completion_tokens":99}\n'
            )
            write_offload(log, self._rec("codegen", True, 10, 7, 0))  # one genuine saving row
            agg = aggregate_offload(log)
            # only the literal-true row's 7 tokens; the string-"false" row's 99 excluded.
            self.assertEqual(agg["by_lane"]["codegen"]["frontier_completion_tokens_saved"], 7)
            self.assertEqual(agg["by_lane"]["codegen"]["gated"], 1)

    def test_invalid_utf8_log_does_not_crash_aggregation(self):
        # Codex xval-2 #5: a stray non-UTF-8 byte in the log must not abort the report.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "offload.jsonl"
            good = '{"lane":"codegen","gated":true,"ok":true,"completion_tokens":5}\n'
            log.write_bytes(good.encode() + b"\xff\xfe not utf8\n")
            agg = aggregate_offload(log)  # must not raise
            self.assertEqual(agg["by_lane"]["codegen"]["frontier_completion_tokens_saved"], 5)

    def test_non_numeric_token_value_does_not_raise(self):
        # Codex xval-2 #5: usage_tokens must coerce "bad" -> 0, never raise ValueError.
        p, c, cached = usage_tokens({"prompt_tokens": "bad", "completion_tokens": 3})
        self.assertEqual((p, c, cached), (0, 3, 0))

    def test_two_arg_form_with_none_record_is_fail_open(self):
        # write_offload(path, None) must NOT be mistaken for the single-arg form and must not raise
        # (Codex xval #9).
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "offload.jsonl"
            try:
                write_offload(log, None)
            except Exception as e:  # noqa: BLE001
                self.fail(f"write_offload(path, None) should be fail-open, raised {e!r}")
            self.assertFalse(log.exists() and log.read_text().strip())

    def test_aggregate_missing_file_is_empty_not_crash(self):
        agg = aggregate_offload(Path("/tmp/does-not-exist-offload-xyz.jsonl"))
        self.assertEqual(agg["overall"]["n"], 0)
        self.assertEqual(agg["by_lane"], {})

    def test_single_arg_form_uses_default_sink(self):
        # write_offload(rec) must target DEFAULT_OFFLOAD_LOG without a path arg (worker/lane form).
        import apex_router.ornith.offload_telemetry as ot
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            sink = Path(d) / "default_offload.jsonl"
            orig = ot.DEFAULT_OFFLOAD_LOG
            ot.DEFAULT_OFFLOAD_LOG = sink
            try:
                ot.write_offload(self._rec("fidelity", True, 12, 6, 3))
            finally:
                ot.DEFAULT_OFFLOAD_LOG = orig
            obj = json.loads(sink.read_text().strip())
            self.assertEqual(obj["lane"], "fidelity")
            self.assertEqual(obj["completion_tokens"], 6)


if __name__ == "__main__":
    unittest.main()
