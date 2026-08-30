"""GPT transferability A/B: transcript vs SKILL.state prompting, driven by `codex exec`.

The Anthropic-wire drivers (behavioral_driver / state_driver) need an apex-upstream bearer,
which this machine doesn't have in-env. This bench answers the SAME transferability question
with GPT instead: does the model behave identically when its history is replaced by explicit
structured state — and what do the two context profiles cost?

Model-agnostic protocol (works over any CLI/API, no native tool-use needed): the model replies
to each round with EXACTLY one directive — `RETRIEVE <ref>` to fetch the bytes behind one
ccr:// marker, or `ANSWER: <final answer>`. The runtime serves fragments from the real
json_crush/StubResolver probe (driver_bench.crushed_probe). Both arms see the same probe, same
rounds, same fragments; they differ ONLY in what context is re-sent:

  transcript — the full growing conversation (every prior prompt, reply, and result)
  state      — (P, Σ, O): the probe prompt, runtime-maintained JSON state (retrieved fragments
               persist; model narration is discarded), the latest result only

COST HONESTY: each round is a FRESH `codex exec` (no prefix caching between rounds), so the
transcript arm pays its full history every round — an API loop with cache reads would be
cheaper than shown. Absolute totals therefore overstate the transcript arm's API cost; the
signals that survive this caveat are the GROWTH PROFILE and BEHAVIOR PARITY, not the totals.

model_call is injectable for offline tests: (prompt) -> (reply_text, tokens_used).
Live default: `codex exec --sandbox read-only --ephemeral` (tokens parsed from its own report).

CLI: python -m apex_router.proxy_engine.tuner.codex_driver_bench [--rounds 6] [--model ID]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

from apex_router.proxy_engine.tuner.driver_bench import crushed_probe

MAX_ROUNDS = 9  # probe has 6 unique refs; 6 retrieves + answer + slack for 1 protocol error
RETRIEVE_RE = re.compile(r"^\s*RETRIEVE\s+(\S+)", re.M)
ANSWER_RE = re.compile(r"^\s*ANSWER:\s*(.*)", re.S)

_PROTOCOL = """\
PROTOCOL — reply with EXACTLY ONE directive per message, no other text:
  RETRIEVE <ref>   fetch the original bytes elided behind one ccr:// ref (one per reply)
  ANSWER: <text>   your final answer, once you have everything you need"""


def build_probe_prompt(crushed: str, refs: list[str]) -> str:
    return (f"Here is a crushed configuration document:\n\n{crushed}\n\n"
            f"List every service's host, port, and deploy window. Some values are elided "
            f"behind ccr:// markers ({len(refs)} elisions) — fetch each one you need."
            f"\n\n{_PROTOCOL}")


def parse_directive(reply: str) -> tuple[str, str]:
    """(kind, payload): kind in {"retrieve", "answer", "bad"}. ANSWER wins if both present
    (a model that answers while mentioning a ref has finished); first RETRIEVE line else."""
    m = ANSWER_RE.search(reply)
    if m:
        return ("answer", m.group(1).strip())
    m = RETRIEVE_RE.search(reply)
    if m:
        return ("retrieve", m.group(1).strip())
    return ("bad", reply.strip()[:200])


def render_transcript(turns: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"[{role}]\n{text}" for role, text in turns)


def run_arm(arm: str, prompt: str, resolver, model_call, *,
            max_rounds: int = MAX_ROUNDS, drift: dict | None = None) -> dict:
    """One arm of the A/B. Returns the row; never raises on model misbehavior (protocol
    errors are counted, and two consecutive ones end the arm — the GPT-structured-output
    signal, same role as the codegen lane's taxonomy).

    drift (optional, the paper's experiment-3 state-recovery probe): {"ref", "new", "alert"}.
    After the model retrieves drift["ref"] the FIRST time, the environment silently changes:
    the resolver is swapped to drift["new"] content and the NEXT round carries the corrective
    alert. The arms differ exactly as the paper predicts matters — the transcript keeps the
    STALE v1 fragment in its history alongside the alert; the state arm's Σ is REPLACED with
    v2, so only the alert text and current state survive. `used_revised` in the row records
    whether the final answer reflects the post-drift value; `alert_delivered` guards the
    inconclusive case (model answered before the alert round).
    """
    turns: list[tuple[str, str]] = [("USER", prompt)]
    state: dict = {"retrieved": {}, "rounds_used": 0}
    observation: str | None = None
    refs: list[str] = []
    tokens = 0
    protocol_errors = 0
    consecutive_bad = 0
    drift_pending = False
    alert_delivered = False

    for _ in range(max_rounds):
        if drift_pending:
            # the corrective alert arrives between rounds; the environment has already moved
            drift_pending = False
            alert_delivered = True
            resolver._map[drift["ref"]] = drift["new"]
            if arm == "transcript":
                # v1 stays in history; the alert (carrying v2) is appended — the anchor contest
                turns.append(("USER", drift["alert"]))
            else:
                # Σ is REPLACED: v1 is gone from the model's operative context entirely
                state["retrieved"][drift["ref"]] = drift["new"]
                observation = drift["alert"]
        if arm == "transcript":
            rendered = render_transcript(turns)
        else:  # state
            from apex_router.proxy_engine.tuner.state_driver import compose_prompt
            rendered = compose_prompt(prompt, state, observation)
        reply, used = model_call(rendered)
        tokens += used
        state["rounds_used"] += 1
        kind, payload = parse_directive(reply)

        if kind == "answer":
            return {"arm": arm, "answer": payload, "refs": refs, "tokens": tokens,
                    "rounds": state["rounds_used"], "protocol_errors": protocol_errors,
                    "finished": True, "alert_delivered": alert_delivered}
        if kind == "retrieve":
            consecutive_bad = 0
            refs.append(payload)
            served = resolver.resolve(payload)
            if (drift is not None and payload == drift["ref"]
                    and not alert_delivered and not drift_pending):
                drift_pending = True  # first retrieval of the drifting value -> alert next round
            if served is not None:
                state["retrieved"][payload] = served
                # confirmation only — the content lives in Σ; re-sending it as O double-sends
                # every fragment (measured: state arm > transcript arm before this fix)
                result = f"retrieved {payload} — content is now in state"
            else:
                protocol_errors += 1
                result = f"--- ERROR: unknown ref {payload} ---"
            observation = result
            # the transcript arm needs the content inline (it has no state map)
            turns += [("ASSISTANT", reply),
                      ("USER", result if served is None else
                            f"--- result for {payload} ---\n{served}")]
            continue
        # unparseable reply: count it, correct it once as the next observation
        protocol_errors += 1
        consecutive_bad += 1
        correction = (f"PROTOCOL ERROR: your reply matched neither directive. "
                      f"Reply with EXACTLY one line: RETRIEVE <ref> or ANSWER: <text>.")
        observation = correction
        turns += [("ASSISTANT", reply), ("USER", correction)]
        if consecutive_bad >= 2:
            break
    return {"arm": arm, "answer": "", "refs": refs, "tokens": tokens,
            "rounds": state["rounds_used"], "protocol_errors": protocol_errors,
            "finished": False, "alert_delivered": alert_delivered}


# --------------------------------------------------------------------------------------------------
# Live model call: codex exec, tokens from its own report.
# --------------------------------------------------------------------------------------------------

_TOKENS_RE = re.compile(r"tokens used\s*\n\s*([\d,]+)")


def codex_call_factory(model: str | None = None, *, timeout_s: int = 300):
    """(prompt) -> (reply_text, tokens_used) via `codex exec`. Read-only + ephemeral: the probe
    needs no tools and leaves no session files. Raises RuntimeError on non-zero exit — a failed
    call must not be scored as a model failure."""
    def call(prompt: str) -> tuple[str, int]:
        cmd = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
               "--ephemeral"]
        if model:
            cmd += ["-m", model]
        cmd.append("-")
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=timeout_s)
        if proc.returncode != 0:
            raise RuntimeError(f"codex exec exit {proc.returncode}: {proc.stderr[-400:]}")
        # verified against codex-cli 0.151: stdout carries ONLY the final message; the run
        # metadata (incl. "tokens used") goes to stderr.
        m = _TOKENS_RE.search(proc.stderr)
        tokens = int(m.group(1).replace(",", "")) if m else 0
        return proc.stdout.strip(), tokens
    return call


STALE_MARK = "deploy-window-alpha"
REVISED_MARK = "deploy-window-alpha-REVISED"


def build_drift() -> dict:
    """The experiment-3 drift spec over the shared probe: svc-alpha's window silently changes
    to REVISED after the model first retrieves it. The alert carries the new value factually
    (no 'use only this' instruction — a directive alert would flatten the anchoring signal)."""
    _, refs, resolver = crushed_probe()
    ref = next(r for r in refs if STALE_MARK in resolver.resolve(r))
    old = resolver.resolve(ref)
    new = old.replace(STALE_MARK, REVISED_MARK)
    assert new != old, "probe fragment does not contain the drift marker"
    alert = (f"ALERT: external change — the fragment behind {ref} was REVISED outside this "
             f"session. New content:\n{new}")
    return {"ref": ref, "new": new, "alert": alert}


def _drift_verdict(answer: str) -> dict:
    return {"used_revised": REVISED_MARK in answer,
            "used_stale": STALE_MARK in answer.replace(REVISED_MARK, "")}


def run_drift_experiment(*, model: str | None = None, model_call=None,
                         max_rounds: int = 12) -> list[dict]:
    """Paper experiment 3 on GPT: silent mid-run drift + corrective alert. Per arm, did the
    final answer track the revised value (recovery) or the stale one (anchoring)?"""
    crushed, refs, resolver = crushed_probe()
    prompt = build_probe_prompt(crushed, refs)
    drift = build_drift()
    call = model_call or codex_call_factory(model)
    rows = []
    for arm in ("transcript", "state"):
        row = run_arm(arm, prompt, resolver, call, max_rounds=max_rounds, drift=drift)
        row.update(_drift_verdict(row["answer"]))
        rows.append(row)
    return rows


def render_drift(rows: list[dict]) -> str:
    lines = ["GPT DRIFT experiment (silent mid-run change + corrective alert — paper exp. 3)",
             "=" * 76]
    for r in rows:
        if not r["alert_delivered"]:
            lines.append(f"[{r['arm']}] INCONCLUSIVE — model answered before the alert round")
            continue
        verdict = ("RECOVERED (revised value)" if r["used_revised"] and not r["used_stale"]
                   else "ANCHORED (stale value)" if r["used_stale"] and not r["used_revised"]
                   else "MIXED (both values in answer)")
        lines.append(f"[{r['arm']}] {verdict} rounds={r['rounds']} tokens={r['tokens']} "
                     f"protocol_errors={r['protocol_errors']}")
        lines.append(f"  answer: {r['answer'][:300]}")
    return "\n".join(lines)


def run_gpt_bench(*, model: str | None = None, model_call=None,
                  max_rounds: int = MAX_ROUNDS) -> list[dict]:
    crushed, refs, resolver = crushed_probe()
    prompt = build_probe_prompt(crushed, refs)
    call = model_call or codex_call_factory(model)
    return [run_arm(arm, prompt, resolver, call, max_rounds=max_rounds)
            for arm in ("transcript", "state")]


def render(rows: list[dict]) -> str:
    lines = ["GPT driver A/B (codex exec — fresh call per round, NO prefix caching)",
             "=" * 72]
    for r in rows:
        lines.append(f"[{r['arm']}] tokens={r['tokens']} rounds={r['rounds']} "
                     f"refs={len(r['refs'])} protocol_errors={r['protocol_errors']} "
                     f"finished={r['finished']}")
    if len(rows) == 2 and rows[0]["tokens"]:
        t, s = rows
        lines.append(f"state tokens = {s['tokens'] / t['tokens']:.0%} of transcript "
                     "(uncached CLI calls — overstates transcript API cost; growth profile "
                     "and parity are the signal, not totals)")
        lines.append(f"retrieval parity (exact refs): "
                     f"{'MATCH' if t['refs'] == s['refs'] else 'DIVERGED: ' + str((t['refs'], s['refs']))}")
        lines.append("--- answers (judge equivalence yourself) ---")
        for r in rows:
            lines.append(f"[{r['arm']}] {r['answer'][:600] or '(no answer — arm did not finish)'}")
    return "\n".join(lines)


def _cli(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="codex-driver-bench")
    p.add_argument("--model", help="codex model id (default: user's codex config)")
    p.add_argument("--rounds", type=int, default=MAX_ROUNDS)
    p.add_argument("--drift", action="store_true",
                   help="run the drift/anchoring experiment (paper exp. 3) instead of the A/B")
    p.add_argument("--report", help="write rows JSON here")
    args = p.parse_args(argv)
    if args.drift:
        rows = run_drift_experiment(model=args.model, max_rounds=args.rounds + 3)
        print(render_drift(rows))
    else:
        rows = run_gpt_bench(model=args.model, max_rounds=args.rounds)
        print(render(rows))
    if args.report:
        from pathlib import Path
        Path(args.report).write_text(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
