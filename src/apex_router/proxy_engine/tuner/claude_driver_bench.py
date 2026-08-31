"""Claude-family transferability A/B: transcript vs SKILL.state prompting, driven by `claude`.

The companion to codex_driver_bench: it answers the DESIGN-skill-state.md open item "Live opus
run not executed (no bearer in CI env)" without a raw Anthropic bearer, by driving the SAME
model-agnostic RETRIEVE/ANSWER protocol (build_probe_prompt / run_arm / run_drift_experiment)
through the `claude` CLI in print mode. The Anthropic-wire drivers (state_driver/driver_bench)
still need the apex upstream; this bench needs only a logged-in `claude`.

Invocation: `claude -p --bare --output-format json --model <m>`.
  --bare pins a fixed ~1.7k-token system prompt (no CLAUDE.md / hooks / plugins), so the
  MARGINAL growth between arms is the signal, not harness overhead. Same COST-HONESTY caveat as
  the codex bench: each round is a FRESH CLI call with NO prefix caching, so absolute transcript
  totals overstate a cached API loop — the GROWTH PROFILE and BEHAVIOR PARITY are what transfer.
  Auth under --bare is ANTHROPIC_API_KEY / apiKeyHelper, or a third-party provider
  (Bedrock/Vertex/Foundry) using its own credentials — NOT interactive OAuth/keychain.

PROBE INTEGRITY: the RETRIEVE/ANSWER protocol is pure text, so the model needs NO tools — and
must not have any, or it could read the opaque window codes straight from this repo's source
instead of retrieving them (the codes are static constants in driver_bench.py). We pass
`--disallowedTools` for the file/exec tools AND run from a neutral cwd, so retrieval is the only
path to the codes.

Tokens: a single logical total = input + cache-read + cache-creation + output, summed from the
CLI's usage report (the codex harness scores one int per call). NOTE this is NOT state_driver's
convention, which tracks `in` (input + cache-read, excluding cache-creation) and `out`
separately; the aggregate here is deliberate for the round-total growth profile. model_call is
injectable for offline tests.

CLI:
  python -m apex_router.proxy_engine.tuner.claude_driver_bench --model sonnet
  python -m apex_router.proxy_engine.tuner.claude_driver_bench --model sonnet --drift
  python -m apex_router.proxy_engine.tuner.claude_driver_bench --model sonnet --drift --authoritative
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile

from apex_router.proxy_engine.tuner.codex_driver_bench import (
    MAX_ROUNDS, render, render_drift, run_drift_experiment, run_gpt_bench)

def claude_call_factory(model: str = "sonnet", *, timeout_s: int = 300):
    """(prompt) -> (reply_text, tokens_used) via `claude -p`. Raises on non-zero exit AND on a
    zero-exit error envelope (`is_error: true`), so a transport/API failure is never scored as
    model behavior. Tools are disabled and cwd is neutral so the model cannot read the window
    codes from source (probe integrity). Auth under --bare: ANTHROPIC_API_KEY / apiKeyHelper /
    third-party provider creds."""
    def call(prompt: str) -> tuple[str, int]:
        # `--tools ""` is an EMPTY ALLOWLIST (fail-closed): it grants zero tools, so the model
        # cannot read the opaque window codes from this repo's source (probe integrity). This
        # beats a denylist — a denylist must enumerate every risky tool (Read/Bash/Task/Skill/
        # …) and silently fails open on any it misses; the allowlist blocks all by construction.
        cmd = ["claude", "-p", "--bare", "--output-format", "json", "--model", model,
               "--tools", ""]
        # neutral cwd: even with tools blocked, don't run the reviewer inside the repo whose
        # source is the answer key.
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  timeout=timeout_s, cwd=td)
        if proc.returncode != 0:
            raise RuntimeError(f"claude exit {proc.returncode}: {proc.stderr[-400:]}")
        data = json.loads(proc.stdout)
        if data.get("is_error"):
            raise RuntimeError(f"claude error envelope: {str(data)[:400]}")
        u = data.get("usage") or {}
        tokens = ((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                  + (u.get("cache_creation_input_tokens") or 0) + (u.get("output_tokens") or 0))
        return (data.get("result") or "").strip(), tokens
    return call


def _cli(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="claude-driver-bench")
    p.add_argument("--model", default="sonnet", help="claude model alias/id (default: sonnet)")
    p.add_argument("--rounds", type=int, default=MAX_ROUNDS)
    p.add_argument("--drift", action="store_true",
                   help="run the drift/anchoring experiment (paper exp. 3) instead of the A/B")
    p.add_argument("--authoritative", action="store_true",
                   help="drift alert on the trusted retrieval channel (isolates anchoring from "
                        "claude's prompt-injection refusal)")
    p.add_argument("--report", help="write rows JSON here")
    args = p.parse_args(argv)
    call = claude_call_factory(args.model)
    if args.drift:
        rows = run_drift_experiment(model_call=call, max_rounds=args.rounds + 3,
                                    authoritative=args.authoritative)
        print(f"### CLAUDE {args.model} — DRIFT"
              f"{' (authoritative channel)' if args.authoritative else ''}\n")
        print(render_drift(rows))
    else:
        rows = run_gpt_bench(model_call=call, max_rounds=args.rounds)
        print(f"### CLAUDE {args.model} — A/B transcript vs (P,\u03a3,O)\n")
        print(render(rows))
    if args.report:
        from pathlib import Path
        Path(args.report).write_text(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
