"""detect_client routing — the OpenAI/Codex wire must route by PATH, not by User-Agent.

Wiring Codex through apex (2026-07-17): Codex's real deployment is Azure the gateway with base_url
`.../gpt5/openai` and wire_api=responses, so with a host-only client base_url it POSTs to
`/responses` (or `/gpt5/openai/responses` if the prefix stays on the client) — NOT `/v1/responses`.
The old detect_client only matched `/v1/{chat/completions,completions,responses}` by path, so a real
Codex path fell through to the UA tiebreak — fragile: a UA without "codex"/"openai" misroutes Codex
to the Anthropic upstream (auth failure, broken CLI). xval #10's own rule is "path is the
deterministic signal"; this pins it for the un-versioned + prefixed Codex paths too.
"""
from __future__ import annotations

from apex_router.proxy_engine.proxy.handlers.passthrough import detect_client


class _Req:
    def __init__(self, path: str, headers: dict | None = None):
        self.url = type("U", (), {"path": path})()
        self.headers = headers or {}


# ── the OpenAI/Codex wire, routed by PATH with NO user-agent (path must be authoritative) ──

def test_bare_responses_path_routes_codex_without_ua():
    # host-only client base_url → Codex POSTs /responses
    assert detect_client(_Req("/responses")) == "codex"


def test_prefixed_responses_path_routes_codex_without_ua():
    # client base_url keeps the the gateway /gpt5/openai prefix → path is /gpt5/openai/responses
    assert detect_client(_Req("/gpt5/openai/responses")) == "codex"


def test_bare_chat_completions_routes_codex_without_ua():
    assert detect_client(_Req("/chat/completions")) == "codex"


def test_prefixed_chat_completions_routes_codex_without_ua():
    assert detect_client(_Req("/gpt5/openai/chat/completions")) == "codex"


# ── must NOT regress: existing versioned paths + the Anthropic wire ──

def test_v1_responses_still_codex():
    assert detect_client(_Req("/v1/responses")) == "codex"


def test_v1_chat_completions_still_codex():
    assert detect_client(_Req("/v1/chat/completions")) == "codex"


def test_anthropic_messages_still_claude_code():
    assert detect_client(_Req("/v1/messages")) == "claude-code"


def test_prefixed_anthropic_messages_still_claude_code():
    # symmetry: an the gateway-prefixed Anthropic path must stay claude-code, not fall to codex
    assert detect_client(_Req("/claude/v1/messages")) == "claude-code"


def test_unknown_path_with_anthropic_header_is_claude_code():
    assert detect_client(_Req("/", {"anthropic-version": "2023-06-01"})) == "claude-code"


def test_unknown_path_defaults_claude_code():
    # dominant traffic default preserved for a truly ambiguous request
    assert detect_client(_Req("/")) == "claude-code"


def test_embeddings_is_a_known_latent_gap():
    # DOCUMENTED LATENT GAP (cross-val 2026-07-17): /v1/embeddings is an OpenAI endpoint but does
    # not end in a matched suffix, so it currently routes claude-code. The Codex CODING agent never
    # calls embeddings (wire_api=responses → only /responses), so this does not fire on our traffic.
    # If an embeddings client is ever added, THIS test flips to `== "codex"` and the suffix list
    # gains "/embeddings". Pinned so the gap is visible, not silent.
    assert detect_client(_Req("/v1/embeddings")) == "claude-code"  # latent: not yet codex
