"""SKILL.state variant of the behavioral driver — (P, Σ, O) prompts instead of a transcript.

arXiv:2608.26263 applied to the offline evidence driver. `behavioral_driver.build_driver` runs
the gate's probe as an append-only Anthropic tool loop: every round re-sends the original prompt
PLUS every prior assistant turn and tool result (O(rounds²) cumulative input). This driver runs
the SAME probe contract (`ask_model(prompt, tools, resolver) -> {answer, retrieved_refs}`) but
each round sends exactly ONE user message composed of:

    P  — the original probe prompt (immutable, verbatim)
    Σ  — runtime-maintained JSON state: the ref→fragment map retrieved so far + rounds used.
         The fragments MUST persist in Σ: they are exactly the paper's "information required
         for future execution" (a multi-ref synthesis is impossible if the final round can
         only see the last fragment). What is discarded each round is the model's own text —
         reasoning, narration, prior assistant turns — never retrieved content.
    O  — the latest tool results, rendered as plain text (prior observations are dropped;
         their content survives in Σ, their scaffolding does not)

The state is maintained by the RUNTIME, deterministically (refs accumulate server-side) — the
frontier model is never asked to emit structured patches, so the paper's open-weight
structured-output failure modes (§5.7) don't apply here. What transfers and is under test:
whether a frontier model sustains probe accuracy when history is replaced by explicit state,
and the flat-vs-growing prompt profile. That is what driver_bench.py measures.

Same discipline as behavioral_driver: bounded max_tokens per call, budget fails LOUDLY, auth
injectable, never on the hot path. `call_api` is the seam for tests/bench (a callable taking
the request body, returning the API response dict); default posts to the apex upstream.
"""
from __future__ import annotations

import json
import os
import urllib.request

from apex_router.proxy_engine.pipeline.resolver import StubResolver

DRIVER_MODEL = os.environ.get("APEX_DRIVER_MODEL", "claude-opus-4-1")
MAX_TOOL_ROUNDS = 4  # same hard cap as the transcript driver — arms must differ ONLY in history


def default_token_provider() -> str:
    tok = os.environ.get("APEX_BEARER_TOKEN", "").strip()
    if not tok:
        raise RuntimeError(
            "no bearer token: set APEX_BEARER_TOKEN or pass token=/token_provider= "
            "to build_state_driver()")
    return tok


class BudgetExceeded(RuntimeError):
    pass


def compose_prompt(prompt: str, state: dict, observation: str | None) -> str:
    """The paper's A_t = (P, Σ_t, O_t) as a single user message. Byte order keeps the immutable
    probe prompt as the stable prefix; state and observation ride after it."""
    parts = [prompt, "", "=== RETRIEVAL STATE (JSON, runtime-maintained) ===",
             json.dumps(state, indent=2)]
    if observation is not None:
        parts += ["", "=== LATEST TOOL RESULTS ===", observation]
    return "\n".join(parts)


def run_state_loop(prompt: str, tools: list, resolver: StubResolver | None,
                   call_api, *, model: str, max_tokens: int, tally: dict,
                   budget_check=None) -> dict:
    """The (P, Σ, O) tool loop, API-injectable. Returns the gate's expected dict.

    `tally` accumulates {"in", "out"} across rounds (mutated in place) so the caller's budget
    accounting is identical to the transcript driver's. `budget_check` (optional callable) is
    invoked at the top of EVERY round — the transcript driver checks spend only between probes;
    this loop enforces it between rounds too, so a runaway retrieval can't overshoot the budget
    by a whole loop.

    If the round cap is hit with a retrieval still pending (the model retrieved on its last
    allowed round), ONE final tool-less call is made so the answer reflects the last result
    instead of being the retrieval round's incidental narration. This is a deliberate
    documented asymmetry with the transcript driver (which returns the incidental text); it
    costs at most one extra call, only on the exhausted-cap path.
    """
    api_tools = [{"name": t["name"], "description": t["description"],
                  "input_schema": t["input_schema"]} for t in tools]
    state: dict = {"retrieved": {}, "rounds_used": 0}
    retrieved_refs: list[str] = []
    answer_text = ""
    observation: str | None = None
    pending = False  # last round retrieved -> its results have not been answered yet

    for _ in range(MAX_TOOL_ROUNDS):
        if budget_check is not None:
            budget_check()
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "user",
                              "content": compose_prompt(prompt, state, observation)}],
                "tools": api_tools}
        resp = call_api(body)
        usage = resp.get("usage") or {}
        tally["in"] += (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
        tally["out"] += usage.get("output_tokens") or 0
        state["rounds_used"] += 1

        content = resp.get("content") or []
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        answer_text = " ".join(
            b.get("text", "") for b in content if b.get("type") == "text"
        ) or answer_text
        if not tool_uses:
            pending = False
            break

        # Runtime applies the state transition: served fragments accumulate in Σ (they are the
        # operationally required state); the model's own prior text is discarded (the paper's
        # core move), its fetchings are not. A successful serve is observed as a one-line
        # confirmation — the content is in Σ, so re-sending it as O would double-send every
        # fragment (measured: state arm > transcript arm on the live probe before this).
        pending = True
        results_text = []
        for tu in tool_uses:
            ref = (tu.get("input") or {}).get("ref", "")
            retrieved_refs.append(ref)
            served = resolver.resolve(ref) if resolver else None
            if served is not None:
                state["retrieved"][ref] = served
                results_text.append(f"retrieved {ref} — content is now in state")
            else:
                results_text.append(f"--- ERROR: unknown ref {ref} ---")
        observation = "\n".join(results_text)

    if pending:
        # Cap exhausted mid-retrieval: force one answer-only round (no tools offered) so the
        # returned answer is about the RESULTS, not the retrieval narration.
        if budget_check is not None:
            budget_check()
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "user",
                              "content": compose_prompt(
                                  prompt + "\n\nAnswer NOW from the retrieved state above. "
                                           "Do not call any tool.", state, observation)}],
                "tools": []}
        resp = call_api(body)
        usage = resp.get("usage") or {}
        tally["in"] += (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
        tally["out" ] += usage.get("output_tokens") or 0
        content = resp.get("content") or []
        answer_text = " ".join(
            b.get("text", "") for b in content if b.get("type") == "text"
        ) or answer_text

    return {"answer": answer_text, "retrieved_refs": retrieved_refs}


def build_state_driver(
    *,
    base_url: str = "http://127.0.0.1:8788",
    token: str | None = None,
    token_provider=default_token_provider,
    max_tokens: int = 512,
    budget_tokens: int | None = None,
    call_api=None,
):
    """Build a SKILL.state `ask_model` with the exact contract of behavioral_driver.build_driver
    (plus `.spent` / `.tally`), so the behavioral gate and A/B bench can swap arms by name.

    call_api: injectable (body -> response dict) for offline tests/bench; None = POST to the
    apex upstream (requires the same bearer discipline as behavioral_driver).
    """
    if call_api is None:
        bearer = token or token_provider()

        def call_api(body: dict) -> dict:  # noqa: F811 — intentional rebind as the live POST
            req = urllib.request.Request(
                base_url.rstrip("/") + "/v1/messages",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "authorization": f"Bearer {bearer}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.loads(resp.read().decode("utf-8"))

    tally = {"in": 0, "out": 0}

    def _spent() -> int:
        return tally["in"] + tally["out"]

    def ask_model(prompt: str, tools: list, *, resolver: StubResolver | None = None) -> dict:
        def _check() -> None:
            if budget_tokens is not None and _spent() >= budget_tokens:
                raise BudgetExceeded(f"budget {budget_tokens} tok exhausted (spent {_spent()})")
        _check()
        return run_state_loop(prompt, tools, resolver, call_api,
                              model=DRIVER_MODEL, max_tokens=max_tokens, tally=tally,
                              budget_check=_check)

    ask_model.spent = _spent  # type: ignore[attr-defined]
    ask_model.tally = tally  # type: ignore[attr-defined]
    return ask_model
