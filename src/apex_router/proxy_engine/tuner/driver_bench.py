"""A/B bench: transcript driver (behavioral_driver) vs SKILL.state driver (state_driver).

The transferability test for arXiv:2608.26263 on a FRONTIER model: the paper's evidence is
Gemini-3-Flash + open-weights; whether replacing history with explicit state preserves behavior
on Claude-class models is untested. This bench runs the same probe tasks through both arms and
measures the two things that matter:

  TOKENS    — cumulative input across the tool rounds (transcript grows O(rounds²); state
              grows only with the retrieved fragments it must keep). Offline mode reports
              APPROX tokens (actual request-body bytes // 4): a size proxy, not tokenizer
              output — but the structural delta (re-sent history vs compact state) is
              tokenizer-independent, so the RATIO between arms is meaningful.
  BEHAVIOR  — same answer, same retrieved refs. Offline: exact-match on both (scripted model).
              Live: refs compared exactly (identity + order); answers are printed side by side
              for the human to judge — free-text answers legitimately differ in wording, so the
              bench reports and the human decides (apex doctrine).

Offline mode (default) uses a scripted fake API: retrieves the task's refs one per round, then
answers. It proves the mechanism and the token math, NOT model transferability — the live mode
(`--live`, needs APEX_BEARER_TOKEN + the apex upstream) is the transferability evidence.

Live mode synthesizes a real probe: content is crushed by the actual json_crush transform so
the markers/refs/fragments are genuine (StubResolver.register), and the prompt asks a question
whose answer sits behind a ccr:// marker — exactly the behavioral gate's probe shape.

CLI:
  python -m apex_router.proxy_engine.tuner.driver_bench            # offline mechanism check
  python -m apex_router.proxy_engine.tuner.driver_bench --live     # real opus run (spends $)
"""
from __future__ import annotations

import json
import sys

from apex_router.proxy_engine.pipeline.resolver import StubResolver
from apex_router.proxy_engine.tuner.behavioral_driver import build_driver
from apex_router.proxy_engine.tuner.state_driver import build_state_driver

TOOL = {
    "name": "retrieve_elided",
    "description": "Retrieve the original bytes elided behind a ccr:// ref.",
    "input_schema": {"type": "object",
                     "properties": {"ref": {"type": "string"}},
                     "required": ["ref"]},
}

# Offline probe tasks: the answer is behind N refs; the scripted model fetches one per round.
OFFLINE_TASKS = [
    {"id": "probe-1ref", "refs": ["ccr://aa#0-10"]},
    {"id": "probe-2ref", "refs": ["ccr://aa#0-10", "ccr://bb#0-10"]},
    {"id": "probe-3ref", "refs": ["ccr://aa#0-10", "ccr://bb#0-10", "ccr://cc#0-10"]},
]
FRAGMENT = "0123456789" * 40  # 400 bytes per served ref — big enough for the growth to show


def _prompt_for(task: dict) -> str:
    markers = ", ".join(task["refs"])
    return (f"Summarize the elided configuration. The relevant values sit behind these "
            f"markers: {markers}. Use the retrieve_elided tool to fetch each one, then answer "
            f"with the concatenated content. " + ("pad " * 200))  # realistic probe-prompt size


def make_fake_api(task: dict, *, requests: list | None = None):
    """Scripted Anthropic-API stand-in for ONE task: retrieves the task's refs one per round,
    then answers. input_tokens is computed from the ACTUAL body bytes (//4) — the token delta
    between arms is measured, not scripted. `requests` (optional) records every body it saw.
    """
    calls = {"n": 0}

    def call_api(body: dict) -> dict:
        calls["n"] += 1
        if requests is not None:
            # snapshot NOW: transcript drivers mutate and reuse their messages list across
            # rounds, so recording the reference would alias every snapshot to the final state
            requests.append(json.loads(json.dumps(body)))
        usage = {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": 40}
        if calls["n"] <= len(task["refs"]):
            ref = task["refs"][calls["n"] - 1]
            return {"content": [{"type": "text", "text": f"fetching {ref}"},
                                {"type": "tool_use", "id": f"tu_{calls['n']}",
                                 "name": "retrieve_elided", "input": {"ref": ref}}],
                    "usage": usage}
        return {"content": [{"type": "text", "text": "ANSWER: concatenated content"}],
                "usage": usage}
    return call_api


def _resolver_for(task: dict) -> StubResolver:
    r = StubResolver()
    for ref in task["refs"]:
        r._map[ref] = FRAGMENT  # bench fixture: direct stub population, no transform needed
    return r


def run_offline(tasks: list[dict] = OFFLINE_TASKS) -> list[dict]:
    """Both arms over the scripted tasks. Returns per-task rows with tokens + behavior parity."""
    rows = []
    for task in tasks:
        resolver = _resolver_for(task)
        out = {}
        for arm, builder in (("transcript", build_driver), ("state", build_state_driver)):
            ask = builder(token="offline-bench", call_api=make_fake_api(task))
            res = ask(_prompt_for(task), [TOOL], resolver=resolver)
            out[arm] = {"answer": res["answer"], "refs": list(res["retrieved_refs"]),
                        "in": ask.tally["in"], "out": ask.tally["out"]}
        rows.append({
            "task_id": task["id"], "n_refs": len(task["refs"]),
            "transcript_in": out["transcript"]["in"], "state_in": out["state"]["in"],
            "transcript_out": out["transcript"]["out"], "state_out": out["state"]["out"],
            "answers_match": out["transcript"]["answer"] == out["state"]["answer"],
            "refs_match": out["transcript"]["refs"] == out["state"]["refs"] == task["refs"],
        })
    return rows


# Service names are VISIBLE (they survive crushing as JSON keys/hosts); the deploy-window
# value must NOT be derivable from them, or a frontier model shortcuts retrieval by pattern-
# guessing `deploy-window-<name>` from the visible service name and answers correctly without
# fetching (measured on live claude sonnet/haiku: transcript arm retrieved 2/6 refs, guessed
# the rest — and once the code is opaque, HALLUCINATED plausible wrong codes for the 4 it
# never fetched). Each window carries an OPAQUE 6-letter CODE with no relation to its service
# name; the elided text is `deploy-window-<code>` (the prefix is realistic dressing, the code
# is the secret). Codes are word-shaped (no numeric tokens) to stay clear of json_crush's Δ7
# lexeme-stability guard, and unique per service so identical leaves don't collapse to one
# content-addressed ref. Verdict/parity logic keys on the BARE code (WINDOW_CODES) so it
# survives a model abbreviating `deploy-window-qxlmtv` down to `qxlmtv` in its answer.
SERVICE_NAMES = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
WINDOW_CODES = {
    "alpha": "qxlmtv", "beta": "zrpkwd", "gamma": "hjnbfc",
    "delta": "wgtspl", "epsilon": "mkvxrn", "zeta": "cbqhdz",
}
WINDOW_PREFIX = "deploy-window-"  # realistic field dressing; NOT the discriminator (the code is)


def crushed_probe() -> tuple[str, list[str], StubResolver]:
    """A genuine crushed-content probe: real transform, real refs, real fragments.

    Returns (crushed_text, refs, resolver) so both the Anthropic-wire drivers and the
    codex/CLI bench can compose their own protocol prompt over the SAME probe.
    """
    # NOTE: avoid numeric-looking substrings anywhere in the content (svc-00, 10.0.x.x,
    # web-0.internal) — json_crush's lexeme-stability guard (Δ7) refuses to crush content
    # whose numeric tokens wouldn't re-serialize byte-identically, and the probe ends up with
    # zero elisions. Hosts use word names; ports are the only numbers (clean integers).
    names = SERVICE_NAMES
    # notes: the deploy window sits AFTER the 200-char kept prefix, inside the elided span, and
    # is UNIQUE per service (identical leaves share one content-addressed ref — the first
    # version of this probe had 1 ref answering nothing). The window code is OPAQUE (not the
    # service name), so the question genuinely REQUIRES retrieving every ref — a model cannot
    # guess `deploy-window-<name>` from the visible service name.
    content = json.dumps({"services": {f"svc-{nm}": {
        "host": f"{nm}.internal", "port": 8000 + i,
        "notes": "x" * 220 + WINDOW_PREFIX + WINDOW_CODES[nm] + "x" * 220}
        for i, nm in enumerate(names)}})
    resolver = StubResolver()
    n = resolver.register(content)
    if n == 0:
        raise RuntimeError("transform produced no elisions — cannot build the live probe")
    from apex_router.proxy_engine.pipeline.transforms import json_crush
    from apex_router.proxy_engine.pipeline.transforms.base import Block
    crushed = json_crush.run(Block(content=content, tool_name="tool_result"), {}).text
    return crushed, list(resolver._map.keys()), resolver


def _live_task() -> tuple[str, StubResolver]:
    crushed, refs, resolver = crushed_probe()
    prompt = (f"Here is a crushed configuration document:\n\n{crushed}\n\n"
              f"List every service's host, port, and deploy window. Values behind ccr:// "
              f"markers must be fetched with the retrieve_elided tool ({len(refs)} elisions).")
    return prompt, resolver


def run_live() -> list[dict]:
    prompt, resolver = _live_task()
    rows = []
    for arm, builder in (("transcript", build_driver), ("state", build_state_driver)):
        ask = builder()  # live auth: APEX_BEARER_TOKEN
        res = ask(prompt, [TOOL], resolver=resolver)
        rows.append({"task_id": "live-crushed-config", "arm": arm,
                     "in": ask.tally["in"], "out": ask.tally["out"],
                     "refs": list(res["retrieved_refs"]), "answer": res["answer"]})
    return rows


def render_offline(rows: list[dict]) -> str:
    lines = ["driver A/B (OFFLINE mechanism check — scripted model, tokens measured from "
             "actual request bytes)", "=" * 78]
    tot_t = tot_s = 0
    for r in rows:
        tot_t += r["transcript_in"]
        tot_s += r["state_in"]
        lines.append(
            f"[{r['task_id']}] refs={r['n_refs']} approx input tok (bytes//4): "
            f"transcript={r['transcript_in']} "
            f"state={r['state_in']} ({r['state_in'] / r['transcript_in']:.0%}) "
            f"answers_match={r['answers_match']} refs_match={r['refs_match']}")
    if tot_t:
        lines.append(f"TOTAL input: transcript={tot_t} state={tot_s} "
                     f"({tot_s / tot_t:.0%} of transcript)")
    bad = [r["task_id"] for r in rows if not (r["answers_match"] and r["refs_match"])]
    lines.append("behavior parity: " + ("ALL MATCH" if not bad else f"DIVERGED: {bad}"))
    lines.append("NOTE: scripted model — this proves mechanism + token math, NOT frontier "
                 "transferability. --live is the transferability run.")
    lines.append("NOTE: at the gate's 4-round horizon expect token PARITY between arms (the "
                 "state arm must keep retrieved fragments in state); the paper's token savings "
                 "appear at horizons far beyond MAX_TOOL_ROUNDS. The state arm's value at this "
                 "horizon is history-anchoring robustness, not cost.")
    return "\n".join(lines)


def render_live(rows: list[dict]) -> str:
    lines = ["driver A/B (LIVE — real model, real dollars)", "=" * 50]
    for r in rows:
        lines.append(f"[{r['arm']}] input={r['in']} output={r['out']} refs={r['refs']}")
    if len(rows) == 2 and rows[0]["in"]:
        lines.append(f"state input = {rows[1]['in'] / rows[0]['in']:.0%} of transcript")
        same = rows[0]["refs"] == rows[1]["refs"]
        lines.append(f"retrieval parity (exact refs): {'MATCH' if same else 'DIVERGED'}")
        lines.append("--- answers (side by side — judge equivalence yourself; wording differs "
                     "legitimately) ---")
        for r in rows:
            lines.append(f"[{r['arm']}] {r['answer'][:600]}")
    return "\n".join(lines)


def _cli(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="driver-bench")
    p.add_argument("--live", action="store_true",
                   help="run both drivers against the live upstream (APEX_BEARER_TOKEN; spends $)")
    p.add_argument("--report", help="write rows JSON here")
    args = p.parse_args(argv)
    if args.live:
        rows = run_live()
        print(render_live(rows))
    else:
        rows = run_offline()
        print(render_offline(rows))
    if args.report:
        from pathlib import Path
        Path(args.report).write_text(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
