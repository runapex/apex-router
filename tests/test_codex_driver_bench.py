"""GPT (codex exec) transcript-vs-state A/B — offline tests, scripted model_call.

Pins: the RETRIEVE/ANSWER protocol parser; the transcript arm re-sends its full growing
history while the state arm sends (P, Σ, O) with fragments persisted and narration dropped;
token accounting sums per-call usage; unknown refs and protocol errors are counted and fed
back; two consecutive protocol errors end the arm unfinished.
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.proxy_engine.pipeline.resolver import StubResolver  # noqa: E402
from apex_router.proxy_engine.tuner.codex_driver_bench import (  # noqa: E402
    build_probe_prompt, parse_directive, run_arm)


def _resolver():
    r = StubResolver()
    r._map["ccr://a#0-4"] = "FRAG-A"
    r._map["ccr://b#0-4"] = "FRAG-B"
    return r


def _scripted(replies, record=None):
    calls = {"i": 0}

    def call(prompt):
        if record is not None:
            record.append(prompt)
        r = replies[min(calls["i"], len(replies) - 1)]
        calls["i"] += 1
        return (r, 100)
    return call


class TestProtocol(unittest.TestCase):
    def test_retrieve(self):
        self.assertEqual(parse_directive("RETRIEVE ccr://a#0-4"), ("retrieve", "ccr://a#0-4"))

    def test_answer_wins_over_mention(self):
        kind, payload = parse_directive("ANSWER: hosts are a and b")
        self.assertEqual(kind, "answer")
        self.assertIn("hosts", payload)

    def test_bad(self):
        self.assertEqual(parse_directive("let me think about this")[0], "bad")


class TestArms(unittest.TestCase):
    PROMPT = build_probe_prompt("CRUSHED-DOC", ["ccr://a#0-4", "ccr://b#0-4"])

    def test_transcript_grows_and_carries_history(self):
        prompts = []
        run_arm("transcript", self.PROMPT, _resolver(),
                _scripted(["RETRIEVE ccr://a#0-4", "RETRIEVE ccr://b#0-4",
                           "ANSWER: all hosts"], record=prompts))
        self.assertEqual(len(prompts), 3)
        # round 3 re-sends everything: original prompt, both replies, both results
        p3 = prompts[2]
        self.assertIn("CRUSHED-DOC", p3)
        self.assertIn("RETRIEVE ccr://a#0-4", p3)   # prior assistant turn
        self.assertIn("FRAG-A", p3)                  # prior result
        self.assertIn("FRAG-B", p3)
        self.assertGreater(len(p3), len(prompts[1]))  # growth

    def test_state_flat_and_discards_narration(self):
        prompts = []
        row = run_arm("state", self.PROMPT, _resolver(),
                      _scripted(["RETRIEVE ccr://a#0-4", "RETRIEVE ccr://b#0-4",
                                 "ANSWER: all hosts"], record=prompts))
        p3 = prompts[2]
        self.assertIn("CRUSHED-DOC", p3)       # P restated
        self.assertIn("FRAG-A", p3)            # fragments persist in Σ (required for synthesis)
        self.assertIn("FRAG-B", p3)
        self.assertNotIn("RETRIEVE ccr://a#0-4", p3)  # prior directive narration discarded
        self.assertEqual(row["answer"], "all hosts")
        self.assertEqual(row["refs"], ["ccr://a#0-4", "ccr://b#0-4"])
        self.assertEqual(row["tokens"], 300)
        self.assertTrue(row["finished"])

    def test_unknown_ref_counted_and_served_as_error(self):
        prompts = []
        row = run_arm("state", self.PROMPT, _resolver(),
                      _scripted(["RETRIEVE ccr://nope", "ANSWER: partial"], record=prompts))
        self.assertEqual(row["protocol_errors"], 1)
        self.assertIn("ERROR: unknown ref ccr://nope", prompts[1])

    def test_two_consecutive_bad_replies_end_unfinished(self):
        row = run_arm("transcript", self.PROMPT, _resolver(),
                      _scripted(["gibberish"]))
        self.assertFalse(row["finished"])
        self.assertEqual(row["protocol_errors"], 2)
        self.assertEqual(row["answer"], "")

    def test_round_cap(self):
        row = run_arm("state", self.PROMPT, _resolver(),
                      _scripted(["RETRIEVE ccr://a#0-4"]), max_rounds=3)
        self.assertEqual(row["rounds"], 3)
        self.assertFalse(row["finished"])


if __name__ == "__main__":
    unittest.main()
