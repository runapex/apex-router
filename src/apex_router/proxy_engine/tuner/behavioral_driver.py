"""Real-model driver — the `ask_model` that spends real dollars (offline evidence collection).

`behavioral_gate.run_gate` is model-injected and fully tested with a fake model; this module is the
thin adapter that plugs a LIVE model into it. It runs a real tool-use loop against the production
model (the same tier live traffic uses, so the gate measures what the real agent would do) through
apex's own upstream, serving the `retrieve_elided` tool from the stub resolver.

Auth is INJECTABLE (public-safe): pass `token=` a Bearer directly, or `token_provider=` a callable
that mints one. The default provider reads APEX_BEARER_TOKEN from the environment. A gateway that
uses `az`/OAuth can supply a provider that mints via its own flow — apex does not hardcode any
identity provider. NOTHING here runs on the hot path — it is an offline tool that spends the model
budget deliberately.

BUDGET DISCIPLINE: every call is bounded max_tokens; the caller sizes the task batch. `build_driver`
returns an `ask_model` plus a running token tally so a loop can stop at a budget. A token/model
failure fails the RUN loudly (a silent zero would poison the evidence), never fails open.
"""
from __future__ import annotations

import json
import os
import urllib.request

from apex_router.proxy_engine.pipeline.resolver import StubResolver

# The production model — the gate must probe the model the real agent uses (tier must match). Override
# via APEX_DRIVER_MODEL to match your gateway's model-id naming.
DRIVER_MODEL = os.environ.get("APEX_DRIVER_MODEL", "claude-opus-4-1")
MAX_TOOL_ROUNDS = 4  # a probe needs at most: answer, or retrieve→answer; cap the loop hard


def default_token_provider() -> str:
    """Default Bearer source: APEX_BEARER_TOKEN from the environment. A gateway with its own auth
    (OAuth/CLI) supplies a different provider to build_driver(). Raises if unset — the driver must
    not silently proceed without auth (a 401 would poison the evidence by scoring every task wrong)."""
    tok = os.environ.get("APEX_BEARER_TOKEN", "").strip()
    if not tok:
        raise RuntimeError(
            "no bearer token: set APEX_BEARER_TOKEN or pass token=/token_provider= to build_driver()")
    return tok


class BudgetExceeded(RuntimeError):
    pass


def build_driver(
    *,
    base_url: str = "http://127.0.0.1:8788",
    token: str | None = None,
    token_provider=default_token_provider,
    max_tokens: int = 512,
    budget_tokens: int | None = None,
    call_api=None,
):
    """Build an `ask_model(prompt, tools)` bound to a live model, plus a `.spent` token tally.

    Auth: pass `token=` a Bearer directly, or leave it None and `token_provider()` mints one (default
    reads APEX_BEARER_TOKEN). `budget_tokens` (output+input across all calls) caps spend — the next
    call past it raises BudgetExceeded rather than quietly overrunning. The retrieval tool is served
    from a per-call StubResolver the gate registered.

    call_api: injectable (body -> response dict) for offline A/B tests against state_driver;
    None = POST to the apex upstream (default, production behavior unchanged).
    """
    tally = {"in": 0, "out": 0}

    def _spent() -> int:
        return tally["in"] + tally["out"]

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

    def ask_model(prompt: str, tools: list, *, resolver: StubResolver | None = None) -> dict:
        if budget_tokens is not None and _spent() >= budget_tokens:
            raise BudgetExceeded(f"budget {budget_tokens} tok exhausted (spent {_spent()})")
        # Anthropic tool schema: map the gate's tool dict to a Messages API tool.
        api_tools = [{"name": t["name"], "description": t["description"],
                      "input_schema": t["input_schema"]} for t in tools]
        messages = [{"role": "user", "content": prompt}]
        retrieved_refs: list[str] = []
        answer_text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            body = {"model": DRIVER_MODEL, "max_tokens": max_tokens,
                    "messages": messages, "tools": api_tools}
            resp = call_api(body)
            usage = resp.get("usage") or {}
            tally["in"] += (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
            tally["out"] += usage.get("output_tokens") or 0
            content = resp.get("content") or []
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            # accumulate any text the model emitted this round as the running answer
            answer_text = " ".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            ) or answer_text
            if not tool_uses:
                break
            messages.append({"role": "assistant", "content": content})
            results = []
            for tu in tool_uses:
                ref = (tu.get("input") or {}).get("ref", "")
                retrieved_refs.append(ref)
                served = resolver.resolve(ref) if resolver else None
                results.append({
                    "type": "tool_result", "tool_use_id": tu.get("id"),
                    "content": served if served is not None else "ERROR: unknown ref",
                    "is_error": served is None,
                })
            messages.append({"role": "user", "content": results})
        return {"answer": answer_text, "retrieved_refs": retrieved_refs}

    ask_model.spent = _spent  # type: ignore[attr-defined]
    ask_model.tally = tally  # type: ignore[attr-defined]
    return ask_model
