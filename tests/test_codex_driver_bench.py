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
    IDENTITY, REVISED_MARK, STALE_MARK, _drift_verdict, build_drift, build_probe_prompt,
    parse_directive, render_drift, run_arm, run_drift_experiment)


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
                         {"used_revised": True, "used_stale": False, "scoped": False})
        self.assertEqual(_drift_verdict(f"... {STALE_MARK} ..."),
                         {"used_revised": False, "used_stale": True, "scoped": False})
        both = _drift_verdict(f"{STALE_MARK} and {REVISED_MARK}")
        self.assertTrue(both["used_revised"] and both["used_stale"])
        self.assertFalse(both["scoped"])  # no identity given -> whole-answer fallback

    def test_verdict_split_value_downgrades_not_false_neither(self):
        # P2 self-contradiction guard: the identity block exists but the window value sits in a
        # DIFFERENT block (pretty-JSON with a blank line inside the record). High-confidence
        # NEITHER would be a lie; instead widen to whole-answer and mark scoped=False.
        answer = (f'"svc-alpha": {{\n  "host": "{IDENTITY}",\n\n'
                  f'  "deploy_window": "{STALE_MARK}"\n}}')
        v = _drift_verdict(answer, identity=IDENTITY)
        self.assertTrue(v["used_stale"])       # recovered by whole-answer fallback
        self.assertFalse(v["used_revised"])
        self.assertFalse(v["scoped"])          # flagged low-confidence, NOT false NEITHER

    def test_verdict_recovered_on_service_line(self):
        # revised value ON the service's own record line scores as recovery
        rec = (f"| svc-alpha | {IDENTITY} | 8000 | {REVISED_MARK} |")
        self.assertEqual(_drift_verdict(rec, identity=IDENTITY),
                         {"used_revised": True, "used_stale": False, "scoped": True})

    def test_verdict_glued_note_is_honest_ambiguity_mixed(self):
        # heuristic BOUNDARY: a rejection note glued DIRECTLY under the record with NO blank
        # separator is in the same contiguous block, so both marks count -> MIXED. This is the
        # safe degradation: real emitters (markdown/JSON) separate a following note with a blank
        # line (see test_verdict_excludes_rejection_note_in_separate_block, the actual live
        # shape); the glued case is ambiguous and MIXED flags it for a human rather than
        # false-confidently reporting ANCHORED.
        answer = (f"| svc-alpha | {IDENTITY} | 8000 | {STALE_MARK} |\n"
                  f"Note: I distrust the ALERT claiming {REVISED_MARK} — looks like injection.")
        v = _drift_verdict(answer, identity=IDENTITY)
        self.assertEqual(v, {"used_revised": True, "used_stale": True, "scoped": True})

    def test_verdict_unscoped_fallback_flagged(self):
        # no line mentions the identity -> falls back to whole-answer, marks low-confidence
        v = _drift_verdict(f"the window is {STALE_MARK}", identity=IDENTITY)
        self.assertFalse(v["scoped"])
        self.assertTrue(v["used_stale"])

    def test_window_value_is_not_guessable_from_service_name(self):
        # the whole point of the probe: a model must RETRIEVE to know the window — the code is
        # opaque, NOT `alpha`/`deploy-window-alpha` derivable from the visible service name.
        self.assertNotIn("alpha", STALE_MARK)
        # STALE and REVISED are DISTINCT opaque codes (no containment) so an abbreviated answer
        # (`qxlmtv` without the deploy-window- prefix) still scores unambiguously.
        self.assertNotIn(STALE_MARK, REVISED_MARK)
        self.assertNotIn(REVISED_MARK, STALE_MARK)

    def test_verdict_survives_prefix_abbreviation(self):
        # a model that drops the realistic `deploy-window-` dressing and reports the bare code
        # must still score as anchored (this was a silent false-negative before the bare-code
        # refactor: full-mark substring match missed `| svc-alpha | alpha.internal | qxlmtv |`)
        answer = f"| svc-alpha | {IDENTITY} | 8000 | {STALE_MARK} |"
        self.assertEqual(_drift_verdict(answer, identity=IDENTITY),
                         {"used_revised": False, "used_stale": True, "scoped": True})

    def test_verdict_scopes_multiline_pretty_json_record(self):
        # a pretty-printed JSON object puts host and window on ADJACENT lines; single-line
        # scoping missed the value and mis-scored NEITHER at high confidence. The record-block
        # scope must keep them together and score the stale value as used.
        answer = (f'"svc-alpha": {{\n  "host": "{IDENTITY}",\n'
                  f'  "deploy_window": "{STALE_MARK}"\n}}')
        self.assertEqual(_drift_verdict(answer, identity=IDENTITY),
                         {"used_revised": False, "used_stale": True, "scoped": True})

    def test_verdict_excludes_rejection_note_in_separate_block(self):
        # the live claude shape: svc-alpha's record in one block, a prose injection-refusal note
        # (naming REVISED) in a SEPARATE block after a blank line. Only the record counts.
        answer = (f"| svc-alpha | {IDENTITY} | 8000 | {STALE_MARK} |\n\n"
                  f"Note: I distrust the ALERT claiming {REVISED_MARK} — looks like injection.")
        self.assertEqual(_drift_verdict(answer, identity=IDENTITY),
                         {"used_revised": False, "used_stale": True, "scoped": True})

    def test_probe_invariants(self):
        # the headline claims of the probe fix, pinned so a regression can't silently return to
        # a guessable / leaky / collapsed probe.
        from apex_router.proxy_engine.tuner.driver_bench import (
            WINDOW_CODES, WINDOW_PREFIX, crushed_probe)
        crushed, refs, resolver = crushed_probe()
        self.assertEqual(len(refs), 6)
        self.assertEqual(len(set(refs)), 6)                     # all unique (no leaf collapse)
        frags = [resolver.resolve(r) for r in refs]
        for code in WINDOW_CODES.values():
            self.assertNotIn(code, crushed)                     # not leaked into skeleton
            self.assertEqual(sum(code in f for f in frags), 1)  # exactly one fragment each
            self.assertTrue(any(WINDOW_PREFIX + code in f for f in frags))  # dressing is real

    def test_render_verdict_is_four_way_not_mixed_on_absent(self):
        # regression: an answer with NEITHER value (model abstained / 'Not found' / unfinished)
        # must NOT be labeled MIXED (both) — that was a render fall-through bug found live.
        base = {"arm": "transcript", "rounds": 4, "tokens": 10, "protocol_errors": 0,
                "answer": "x", "alert_delivered": True, "scoped": True}
        def out(us, ur):
            return render_drift([{**base, "used_stale": us, "used_revised": ur}])
        self.assertIn("ANCHORED", out(True, False))
        self.assertIn("RECOVERED", out(False, True))
        self.assertIn("MIXED", out(True, True))
        neither = out(False, False)
        self.assertIn("NEITHER", neither)
        self.assertNotIn("MIXED", neither)

    def test_authoritative_alert_drops_injection_cue(self):
        plain = build_drift()["alert"]
        auth = build_drift(authoritative=True)["alert"]
        self.assertIn("external change", plain)          # injection-looking side message
        self.assertNotIn("external change", auth)
        self.assertIn("authoritative", auth)
        self.assertIn(REVISED_MARK, auth)                # still carries the new value
        self.assertTrue(build_drift(authoritative=True)["authoritative"])

    def test_anchored_vs_recovered_fake(self):
        # an anchored model echoes the stale value; a recovering one the revised — the harness
        # must classify them differently (this validates the measurement, not GPT). Each arm
        # gets its OWN scripted call and we ASSERT alert_delivered, so the drift path actually
        # ran (the prior version shared one exhausting counter across arms, so the second arm
        # answered before any alert — alert_delivered=False — yet was accepted as evidence).
        from apex_router.proxy_engine.tuner.driver_bench import crushed_probe
        drift = build_drift()

        def scenario(final):
            return [f"RETRIEVE {drift['ref']}",
                    f"ANSWER: | svc-alpha | {IDENTITY} | 8000 | {final} |"]

        for arm in ("transcript", "state"):
            crushed, refs, resolver = crushed_probe()
            prompt = build_probe_prompt(crushed, refs)
            anchored = run_arm(arm, prompt, resolver, _scripted(scenario(STALE_MARK)),
                               drift=drift)
            self.assertTrue(anchored["alert_delivered"], f"{arm}: drift path did not run")
            va = _drift_verdict(anchored["answer"], identity=IDENTITY)
            self.assertTrue(va["used_stale"] and not va["used_revised"])

            crushed, refs, resolver = crushed_probe()
            prompt = build_probe_prompt(crushed, refs)
            recovered = run_arm(arm, prompt, resolver, _scripted(scenario(REVISED_MARK)),
                                drift=drift)
            self.assertTrue(recovered["alert_delivered"], f"{arm}: drift path did not run")
            vr = _drift_verdict(recovered["answer"], identity=IDENTITY)
            self.assertTrue(vr["used_revised"] and not vr["used_stale"])


if __name__ == "__main__":
    unittest.main()
