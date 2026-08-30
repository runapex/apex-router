"""SKILL.state driver + driver A/B bench — offline tests, no server.

Load-bearing claims under test:
  - run_state_loop sends exactly ONE user message per round composed as (P, Σ, O): the
    original prompt verbatim, the runtime-maintained state JSON (refs accumulate server-side),
    and ONLY the latest tool results — prior rounds' assistant text and results never reappear.
  - the return contract matches behavioral_driver exactly ({answer, retrieved_refs} + tally),
    so the gate/bench can swap arms by name;
  - behavioral_driver's new call_api seam leaves its loop semantics untouched;
  - the bench measures input tokens from actual request bytes (the transcript arm's growth is
    real, not scripted) and checks behavior parity between arms.
"""
import json
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from apex_router.proxy_engine.pipeline.resolver import StubResolver  # noqa: E402
from apex_router.proxy_engine.tuner.behavioral_driver import build_driver  # noqa: E402
from apex_router.proxy_engine.tuner.state_driver import (  # noqa: E402
    BudgetExceeded, build_state_driver, compose_prompt, run_state_loop)
from apex_router.proxy_engine.tuner.driver_bench import (  # noqa: E402
    OFFLINE_TASKS, TOOL, make_fake_api, run_offline)


def _resolver(*refs):
    r = StubResolver()
    for ref in refs:
        r._map[ref] = f"FRAGMENT-OF-{ref}"
    return r


def _scripted(responses, record=None):
    """Fake API replaying `responses` (content-block lists), recording bodies."""
    calls = {"i": 0}

    def call_api(body):
        if record is not None:
            record.append(body)
        r = responses[min(calls["i"], len(responses) - 1)]
        calls["i"] += 1
        return {"content": r, "usage": {"input_tokens": 100, "output_tokens": 10}}
    return call_api


def _tu(ref, i=1):
    return {"type": "tool_use", "id": f"tu_{i}", "name": "retrieve_elided", "input": {"ref": ref}}


class TestComposePrompt(unittest.TestCase):
    def test_order_and_state(self):
        p = compose_prompt("PROBE", {"retrieved_refs": ["r1"], "rounds_used": 1}, "RESULT")
        self.assertLess(p.index("PROBE"), p.index("RETRIEVAL STATE"))
        self.assertLess(p.index("RETRIEVAL STATE"), p.index("LATEST TOOL RESULTS"))
        self.assertIn('"r1"', p)
        self.assertIn("RESULT", p)

    def test_no_observation_first_round(self):
        p = compose_prompt("PROBE", {}, None)
        self.assertNotIn("LATEST TOOL RESULTS", p)


class TestStateLoop(unittest.TestCase):
    def test_two_rounds_then_answer(self):
        bodies = []
        api = _scripted([
            [{"type": "text", "text": "fetching one"}, _tu("r1", 1)],
            [{"type": "text", "text": "fetching two"}, _tu("r2", 2)],
            [{"type": "text", "text": "FINAL ANSWER"}],
        ], record=bodies)
        tally = {"in": 0, "out": 0}
        res = run_state_loop("PROBE-PROMPT", [TOOL], _resolver("r1", "r2"), api,
                             model="m", max_tokens=64, tally=tally)
        self.assertEqual(res["answer"], "FINAL ANSWER")
        self.assertEqual(res["retrieved_refs"], ["r1", "r2"])
        self.assertEqual(tally, {"in": 300, "out": 30})
        # every round is a SINGLE user message — no transcript
        for b in bodies:
            self.assertEqual(len(b["messages"]), 1)
            self.assertEqual(b["messages"][0]["role"], "user")
        # round 2 carries state + latest results; round 1's assistant text is GONE
        p2 = bodies[1]["messages"][0]["content"]
        self.assertIn("PROBE-PROMPT", p2)
        self.assertIn('"r1"', p2)
        self.assertIn("FRAGMENT-OF-r1", p2)
        self.assertNotIn("fetching one", p2)
        # round 3: r1's FRAGMENT persists in Σ (operationally required state — a multi-ref
        # synthesis is impossible without it); only the model's own text is discarded
        p3 = bodies[2]["messages"][0]["content"]
        self.assertIn("FRAGMENT-OF-r2", p3)
        self.assertIn("FRAGMENT-OF-r1", p3)  # in the state's retrieved map
        self.assertNotIn("fetching one", p3)
        self.assertNotIn("fetching two", p3)
        self.assertIn('"retrieved"', p3)

    def test_unknown_ref_surfaces_as_error_observation(self):
        bodies = []
        api = _scripted([
            [_tu("nope")],
            [{"type": "text", "text": "gave up"}],
        ], record=bodies)
        res = run_state_loop("P", [TOOL], _resolver("r1"), api,
                             model="m", max_tokens=64, tally={"in": 0, "out": 0})
        self.assertIn("ERROR: unknown ref nope",
                      bodies[1]["messages"][0]["content"])
        self.assertEqual(res["retrieved_refs"], ["nope"])  # attempted refs are recorded

    def test_round_cap(self):
        # never stops retrieving: 4 retrieval rounds + 1 forced answer-only round
        api = _scripted([[_tu("r1")]])
        tally = {"in": 0, "out": 0}
        res = run_state_loop("P", [TOOL], _resolver("r1"), api,
                             model="m", max_tokens=64, tally=tally)
        self.assertEqual(tally["in"], 500)  # MAX_TOOL_ROUNDS = 4, +1 pending-answer round

    def test_cap_exhausted_forces_answer_round_without_tools(self):
        # 4 refs one-per-round: without the forced round the "answer" would be the 4th
        # retrieval's incidental narration and its result would never be seen.
        refs = ["r1", "r2", "r3", "r4"]
        bodies = []
        responses = [[_tu(r)] for r in refs] + [[{"type": "text", "text": "REAL ANSWER"}]]
        api = _scripted(responses, record=bodies)
        res = run_state_loop("P", [TOOL], _resolver(*refs), api,
                             model="m", max_tokens=64, tally={"in": 0, "out": 0})
        self.assertEqual(res["answer"], "REAL ANSWER")
        self.assertEqual(len(bodies), 5)
        self.assertEqual(bodies[-1]["tools"], [])  # no tools offered on the forced round
        self.assertIn("Answer NOW", bodies[-1]["messages"][0]["content"])
        self.assertIn("FRAGMENT-OF-r4", bodies[-1]["messages"][0]["content"])

    def test_budget_enforced_between_rounds(self):
        api = _scripted([[_tu("r1")]])  # would retrieve forever
        ask = build_state_driver(token="t", call_api=api, budget_tokens=150)
        # round 1 spends 110 (within budget), round 2's pre-round check trips
        with self.assertRaises(BudgetExceeded):
            ask("P", [TOOL], resolver=_resolver("r1"))


class TestDrivers(unittest.TestCase):
    def test_state_driver_contract_and_budget(self):
        task = {"refs": ["r1"]}
        ask = build_state_driver(token="t", call_api=make_fake_api(task),
                                 budget_tokens=10_000)
        res = ask("prompt", [TOOL], resolver=_resolver("r1"))
        self.assertIn("ANSWER", res["answer"])
        self.assertEqual(res["retrieved_refs"], ["r1"])
        self.assertGreater(ask.spent(), 0)

    def test_budget_fails_loudly(self):
        ask = build_state_driver(token="t", call_api=make_fake_api({"refs": []}),
                                 budget_tokens=1)
        ask("prompt", [TOOL], resolver=None)  # spends past the micro-budget
        with self.assertRaises(BudgetExceeded):
            ask("prompt", [TOOL], resolver=None)

    def test_transcript_driver_call_api_seam(self):
        # the refactored build_driver runs its append loop through the injected seam
        bodies = []
        task = {"refs": ["r1", "r2"]}
        ask = build_driver(token="t", call_api=make_fake_api(task, requests=bodies))
        res = ask("prompt", [TOOL], resolver=_resolver("r1", "r2"))
        self.assertEqual(res["retrieved_refs"], ["r1", "r2"])
        # transcript arm: message list GROWS across rounds (the contrast under test)
        self.assertEqual(len(bodies[0]["messages"]), 1)
        self.assertGreater(len(bodies[1]["messages"]), 1)


class TestBench(unittest.TestCase):
    def test_offline_rows_parity_and_token_shape(self):
        rows = run_offline()
        self.assertEqual(len(rows), len(OFFLINE_TASKS))
        for r in rows:
            self.assertTrue(r["answers_match"], r)
            self.assertTrue(r["refs_match"], r)
        # at a 4-round horizon the arms are within token PARITY, not savings: the state arm
        # must persist retrieved fragments in Σ (correctness — a multi-ref synthesis needs
        # them), so its advantage only appears at horizons far beyond MAX_TOOL_ROUNDS=4.
        # Pin the honest claim: same order of magnitude, behavior parity.
        biggest = rows[-1]  # 3 refs / 4 rounds
        ratio = biggest["state_in"] / biggest["transcript_in"]
        self.assertGreater(ratio, 0.5)
        self.assertLess(ratio, 1.5)
        # tokens come from actual body bytes, so they are positive and nonzero per arm
        for r in rows:
            self.assertGreater(r["state_in"], 0)
            self.assertGreater(r["transcript_in"], 0)

    def test_fake_api_usage_reflects_body_size(self):
        task = {"refs": ["r1"]}
        api = make_fake_api(task)
        small = api({"messages": [{"role": "user", "content": "x"}], "tools": []})
        big = api({"messages": [{"role": "user", "content": "x" * 4000}], "tools": []})
        self.assertGreater(big["usage"]["input_tokens"], small["usage"]["input_tokens"])


if __name__ == "__main__":
    unittest.main()
