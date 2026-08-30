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
    REVISED_MARK, STALE_MARK, _drift_verdict, build_drift, build_probe_prompt,
    parse_directive, run_arm, run_drift_experiment)


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


class TestDrift(unittest.TestCase):
    """The experiment-3 plumbing: alert delivery, resolver swap, and the anchor contest —
    transcript keeps v1 in history, state has v1 REPLACED in Σ. Scripted fakes simulate an
    anchored and a recovering model; the LIVE anchoring signal comes from --drift."""

    def _run(self, arm, replies):
        from apex_router.proxy_engine.tuner.driver_bench import crushed_probe
        crushed, refs, resolver = crushed_probe()
        prompt = build_probe_prompt(crushed, refs)
        drift = build_drift()
        prompts = []
        row = run_arm(arm, prompt, resolver,
                      _scripted(replies, record=prompts), drift=drift)
        return row, prompts, drift, resolver

    def test_alert_delivered_and_resolver_swapped(self):
        drift = build_drift()
        replies = [f"RETRIEVE {drift['ref']}", "RETRIEVE " + "x",  # second retrieve: unknown
                   f"RETRIEVE {drift['ref']}",  # re-retrieve AFTER alert -> serves v2
                   "ANSWER: done"]
        row, prompts, drift, resolver = self._run("state", replies)
        self.assertTrue(row["alert_delivered"])
        self.assertEqual(resolver.resolve(drift["ref"]), drift["new"])
        # the alert appears in the round after the first drift-ref retrieval
        self.assertIn("ALERT: external change", prompts[1])
        self.assertIn(REVISED_MARK, prompts[1])

    def test_state_replaces_stale_transcript_keeps_it(self):
        drift = build_drift()
        replies = [f"RETRIEVE {drift['ref']}", "ANSWER: done", "ANSWER: done"]
        # state: after the alert, v1 is GONE from the operative context
        _, sp, _, _ = self._run("state", replies)
        post_alert = sp[1].replace(REVISED_MARK, "")
        self.assertNotIn(STALE_MARK, post_alert)
        # transcript: v1 survives in history alongside the alert (the anchor contest)
        _, tp, _, _ = self._run("transcript", replies)
        post_alert_t = tp[1].replace(REVISED_MARK, "")
        self.assertIn(STALE_MARK, post_alert_t)
        self.assertIn("ALERT: external change", tp[1])

    def test_answer_before_alert_is_inconclusive(self):
        drift = build_drift()
        row, _, _, _ = self._run("state", [f"RETRIEVE {drift['ref']}"])  # never answers
        # drift ref retrieved on the final allowed round -> no alert round exists
        # (here max_rounds defaults high, so the loop ends on repetition; alert WAS delivered)
        # direct check of the flag on the immediate-answer path:
        prompts = []
        calls = {"i": 0}

        def fake(p):
            prompts.append(p)
            calls["i"] += 1
            return (f"RETRIEVE {drift['ref']}" if calls["i"] == 1 else "ANSWER: x", 100)

        from apex_router.proxy_engine.tuner.driver_bench import crushed_probe
        crushed, refs, resolver = crushed_probe()
        row2 = run_arm("state", build_probe_prompt(crushed, refs), resolver, fake,
                       max_rounds=1, drift=drift)
        self.assertFalse(row2["alert_delivered"])

    def test_verdict_detection(self):
        self.assertEqual(_drift_verdict(f"... {REVISED_MARK} ..."),
                         {"used_revised": True, "used_stale": False})
        self.assertEqual(_drift_verdict(f"... {STALE_MARK} ..."),
                         {"used_revised": False, "used_stale": True})
        both = _drift_verdict(f"{STALE_MARK} and {REVISED_MARK}")
        self.assertTrue(both["used_revised"] and both["used_stale"])

    def test_anchored_vs_recovered_fake(self):
        # an anchored model echoes the stale value; a recovering one the revised — the harness
        # must classify them differently (this validates the measurement, not GPT)
        anchored = run_drift_experiment(model_call=_scripted(
            [f"RETRIEVE {build_drift()['ref']}", f"ANSWER: window is {STALE_MARK}"]))
        self.assertTrue(anchored[0]["used_stale"] or anchored[1]["used_stale"])
        recovered = run_drift_experiment(model_call=_scripted(
            [f"RETRIEVE {build_drift()['ref']}", f"ANSWER: window is {REVISED_MARK}"]))
        for row in recovered:
            self.assertTrue(row["used_revised"])
            self.assertFalse(row["used_stale"])


if __name__ == "__main__":
    unittest.main()
