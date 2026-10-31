"""Grok model ids on the OpenAI wire go to xAI. Codex stays on OpenAI.

Rewritten from the pre-pull stash: studio traffic helpers were removed in D25, so this
file covers routing + token math + reservoir enums only.
"""
from __future__ import annotations

import asyncio

from apex_router.agent_resources.telemetry import fresh_input
from apex_router.proxy_engine.config import Config
from apex_router.proxy_engine.proxy.upstream import Upstream, is_grok_model, route_call
from apex_router.telemetry_reservoir import _enum


def test_is_grok_model_matches_id_not_substring():
    assert is_grok_model("grok-4")
    assert is_grok_model("Grok-4-fast")
    assert is_grok_model("xai/grok-3")
    assert is_grok_model("grok")
    assert is_grok_model("grok-4.7")
    assert not is_grok_model("gpt-4o")
    assert not is_grok_model("notgrok")
    assert not is_grok_model("grok_4")
    assert not is_grok_model(None)
    assert not is_grok_model("")


def test_grok_selects_xai_and_codex_stays_on_openai():
    up = Upstream(Config(xai_upstream="https://api.x.ai", openai_upstream="https://api.openai.com"))
    try:
        _assert_grok_routes(up)
    finally:
        asyncio.run(up.aclose())


def _assert_grok_routes(up):
    assert up.base_for("codex", "gpt-4o") == "https://api.openai.com"
    assert up.base_for("cursor", "gpt-4o") == "https://api.openai.com"
    assert up.base_for("cursor", "grok-4") == "https://api.x.ai"
    assert up.base_for("codex", "grok-4-fast") == "https://api.x.ai"
    assert up.base_for("codex", "grok-4.7") == "https://api.x.ai"
    assert up.base_for("claude-code", "grok-4") == up._cfg.anthropic_upstream
    assert up.endpoint_id("cursor", "grok-4") == "xai"
    assert up.endpoint_id("codex") == "openai"
    assert up.endpoint_id("claude-code", "grok-4") == "anthropic"
    assert up.build_url("cursor", "/v1/chat/completions", b"", model="grok-4") == (
        "https://api.x.ai/v1/chat/completions"
    )
    assert "api.openai.com" in up.build_url("codex", "/v1/responses", b"beta=1", model="gpt-5")


def test_route_call_skips_model_on_a_fixed_double():
    def build_url(k, p, q):
        return p

    assert route_call(build_url, "codex", "/v1/responses", b"", model="grok-4") == "/v1/responses"


def test_xai_wire_uses_openai_token_math():
    row = {"endpoint_id": "xai", "tokens_in": 1000, "cache_read_tokens": 400}
    assert fresh_input(row) == 600


def test_reservoir_keeps_cursor_xai_and_grok_ids():
    assert _enum("client", "cursor") == "cursor"
    assert _enum("endpoint_id", "xai") == "xai"
    assert _enum("model_requested", "grok-4") == "grok-4"
    assert _enum("model_requested", "grok-4.7") == "grok-4.7"
