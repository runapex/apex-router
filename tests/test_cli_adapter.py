"""Tests for apex_router.backend.cli_adapter — the Foundry replacement.

The moved tools (codeqa judge/freshness) used to POST to a Foundry proxy (localhost:8788)
with an internal model id. On a Claude+Codex-only machine that endpoint doesn't exist.
This adapter routes their model calls through the installed `claude` / `codex` CLIs —
the subscriptions the target actually has — and parses the CLI output back into the
{content, usage} envelope the callers expect.

The subprocess runner is an injected seam so tests never shell out for real.
"""
import json

import pytest

from apex_router.backend import cli_adapter as A


# A realistic `claude -p --output-format json` envelope (trimmed to the fields we read).
def _claude_json(text="ADAPTER_OK", in_tok=2, out_tok=11):
    return json.dumps({
        "is_error": False,
        "result": text,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 100},
        "total_cost_usd": 0.01,
    })


def _fake_runner(stdout="", returncode=0, stderr=""):
    """Return a runner(cmd, input=...) stub that records the argv and yields a result."""
    seen = {}
    def run(cmd, input=None, timeout=None):
        seen["cmd"] = cmd
        seen["input"] = input
        return A.RunResult(returncode=returncode, stdout=stdout, stderr=stderr)
    return run, seen


# --------------------------------------------------------------------------- #
# claude backend
# --------------------------------------------------------------------------- #
def test_claude_call_returns_content_and_usage():
    run, seen = _fake_runner(stdout=_claude_json("hello world", in_tok=5, out_tok=7))
    r = A.model_call("say hi", backend="claude", model="claude-opus", runner=run)
    assert r.content == "hello world"
    assert r.usage["input_tokens"] == 5
    assert r.usage["output_tokens"] == 7


def test_claude_call_uses_print_and_model_flags():
    run, seen = _fake_runner(stdout=_claude_json())
    A.model_call("prompt text", backend="claude", model="claude-sonnet", runner=run)
    cmd = seen["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd or "--print" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert "--model" in cmd and "claude-sonnet" in cmd


def test_claude_prompt_passed_via_stdin_not_argv():
    # The prompt can be large / contain shell metacharacters -> pass on stdin, not argv.
    run, seen = _fake_runner(stdout=_claude_json())
    A.model_call("weird `$(rm -rf)` prompt", backend="claude", model="m", runner=run)
    assert seen["input"] is not None and "weird" in seen["input"]


def test_claude_error_envelope_raises():
    run, _ = _fake_runner(stdout=json.dumps({"is_error": True, "result": "boom"}))
    with pytest.raises(A.AdapterError):
        A.model_call("x", backend="claude", model="m", runner=run)


def test_claude_nonzero_exit_raises():
    run, _ = _fake_runner(stdout="", returncode=1, stderr="cli failed")
    with pytest.raises(A.AdapterError):
        A.model_call("x", backend="claude", model="m", runner=run)


def test_claude_unparseable_output_raises():
    run, _ = _fake_runner(stdout="not json at all")
    with pytest.raises(A.AdapterError):
        A.model_call("x", backend="claude", model="m", runner=run)


# --------------------------------------------------------------------------- #
# codex backend
# --------------------------------------------------------------------------- #
def test_codex_call_returns_content():
    run, seen = _fake_runner(stdout="ADAPTER_OK\n")
    r = A.model_call("say ok", backend="codex", model="gpt-5", runner=run)
    assert "ADAPTER_OK" in r.content
    assert seen["cmd"][0] == "codex" and seen["cmd"][1] == "exec"


def test_codex_passes_model_via_config_flag():
    run, seen = _fake_runner(stdout="ok")
    A.model_call("x", backend="codex", model="gpt-5", runner=run)
    joined = " ".join(seen["cmd"])
    assert "-c" in seen["cmd"] and "model=gpt-5" in joined


def test_codex_nonzero_exit_raises():
    run, _ = _fake_runner(stdout="", returncode=2, stderr="err")
    with pytest.raises(A.AdapterError):
        A.model_call("x", backend="codex", model="m", runner=run)


# --------------------------------------------------------------------------- #
# backend selection / safety
# --------------------------------------------------------------------------- #
def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        A.model_call("x", backend="foundry", model="m", runner=lambda *a, **k: None)


def test_default_backend_is_claude():
    run, seen = _fake_runner(stdout=_claude_json())
    A.model_call("x", model="m", runner=run)     # no backend -> claude
    assert seen["cmd"][0] == "claude"


def test_as_chat_messages_shape_matches_ornith_client():
    # codeqa callers expect an object with `.answer`/`.content`; expose a compatible
    # result so ab.py/driver.py work unchanged when pointed at the adapter.
    run, _ = _fake_runner(stdout=_claude_json("the answer"))
    r = A.model_call("q", backend="claude", model="m", runner=run)
    assert r.content == "the answer"
    assert hasattr(r, "usage")
