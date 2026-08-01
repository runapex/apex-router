"""Tests for apex_router.backend.cli_adapter — the Foundry replacement.

The moved tools (codeqa judge/freshness) used to POST to a Foundry proxy (an internal proxy)
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
    """Return a runner(cmd, input=, env=) stub that records argv/input/env and yields a result."""
    seen = {}
    def run(cmd, input=None, timeout=None, env=None):
        seen["cmd"] = cmd
        seen["input"] = input
        seen["env"] = env or {}
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


def test_claude_call_uses_print_and_json_flags():
    run, seen = _fake_runner(stdout=_claude_json())
    A.model_call("prompt text", backend="claude", model="claude-sonnet", runner=run)
    cmd = seen["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd or "--print" in cmd
    assert "--output-format" in cmd and "json" in cmd
    # model is via env, not argv (Codex #4)
    assert "claude-sonnet" not in cmd
    assert seen["env"].get("ANTHROPIC_MODEL") == "claude-sonnet"


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
    # VERIFIED (Codex #4): the codex CLI IGNORES a CODEX_MODEL env var; only `-c model=` /
    # `-m` changes the model. So the model MUST go via `-c model=` (unavoidably in argv —
    # that is the CLI's contract), NOT a silently-ignored env var.
    run, seen = _fake_runner(stdout="ok")
    A.model_call("x", backend="codex", model="gpt-5", runner=run)
    assert "-c" in seen["cmd"] and "model=gpt-5" in " ".join(seen["cmd"])


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


# --------------------------------------------------------------------------- #
# Security hardening — confirmed by Codex migration review (2026-07-31)
# --------------------------------------------------------------------------- #
def test_sec2_claude_launched_with_tools_disabled():
    # BUG (Codex #2, CRITICAL): claude -p is AGENTIC. A grading/verifier call must run it
    # with NO tools, so adversarial text in scanned source ("run this command") cannot
    # trigger tool execution. Assert the lockdown flags are present.
    run, seen = _fake_runner(stdout=_claude_json())
    A.model_call("grade this", backend="claude", model="m", runner=run)
    cmd = seen["cmd"]
    joined = " ".join(cmd)
    # VERIFIED against the live CLI: 'deny' is NOT a valid --permission-mode. The real
    # lockdown is 'plan' mode (read-only, cannot execute) + an explicit --disallowedTools.
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"
    # closed-by-default allow-list: only Read is available (safer than a deny-list).
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == "Read"
    assert "bypassPermissions" not in joined
    assert "--dangerously-skip-permissions" not in joined


def test_sec2_codex_launched_read_only_sandbox():
    # BUG (Codex #2): codex exec must run read-only so model-generated shell commands
    # can't mutate anything.
    run, seen = _fake_runner(stdout="ok")
    A.model_call("grade this", backend="codex", model="m", runner=run)
    assert "-s" in seen["cmd"] and "read-only" in seen["cmd"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in " ".join(seen["cmd"])


def test_sec4_model_id_not_in_argv():
    # BUG (Codex #4): an internal model id in argv is process-visible. It must not appear
    # as a plain argv token; passed via config/stdin instead (here we assert it's not a
    # bare argv element that process inspection would reveal in the clear).
    run, seen = _fake_runner(stdout=_claude_json())
    A.model_call("x", backend="claude", model="internal-deployment-id", runner=run)
    # the model id must not be a standalone argv token (process-visible); it goes via env.
    assert "internal-deployment-id" not in seen["cmd"]
    assert seen["env"].get("ANTHROPIC_MODEL") == "internal-deployment-id"


def test_sec5_bounded_output_rejects_oversize():
    # BUG (Codex #5): unbounded capture. An enormous stdout must be rejected, not held.
    huge = "x" * (A.MAX_OUTPUT_BYTES + 1)
    run, _ = _fake_runner(stdout=huge)
    with pytest.raises(A.AdapterError):
        A.model_call("x", backend="codex", model="m", runner=run)


def test_sec5_non_object_json_raises_adaptererror():
    # BUG (Codex #5): valid JSON null/42/[]/"text" hit AttributeError instead of a clean
    # AdapterError. All must raise AdapterError.
    for payload in ("null", "42", "[]", '"just a string"'):
        run, _ = _fake_runner(stdout=payload)
        with pytest.raises(A.AdapterError):
            A.model_call("x", backend="claude", model="m", runner=run)


def test_sec6_timeout_becomes_adaptererror():
    # BUG (Codex #6): subprocess.TimeoutExpired escaped, contradicting "AdapterError on
    # any call failure". The runner raising TimeoutExpired must surface as AdapterError.
    import subprocess
    def timing_out(cmd, input=None, timeout=None, env=None):
        raise subprocess.TimeoutExpired(cmd, timeout or 1)
    with pytest.raises(A.AdapterError):
        A.model_call("x", backend="claude", model="m", runner=timing_out)


# --- pass-2 residual security findings ---
def test_p2_byte_limit_counts_bytes_not_chars():
    # BUG (Codex pass2 #2): the cap counted characters; multibyte content slipped past.
    # A string whose UTF-8 encoding exceeds the byte cap must be rejected.
    payload = "é" * A.MAX_OUTPUT_BYTES        # 2 bytes each -> ~2x the byte cap
    run, _ = _fake_runner(stdout=payload)
    with pytest.raises(A.AdapterError):
        A.model_call("x", backend="codex", model="m", runner=run)


def test_p2_claude_partial_envelope_without_is_error_still_validated():
    # BUG (Codex pass2 #3): {"result":"ok"} (no is_error/usage) was accepted as complete.
    # A valid grade needs the envelope's success signal; a partial one must raise.
    run, _ = _fake_runner(stdout='{"result":"ok"}')   # missing is_error / usage
    with pytest.raises(A.AdapterError):
        A.model_call("x", backend="claude", model="m", runner=run)


def test_p2_codex_non_error_shape_is_still_text():
    # codex has no envelope; nonempty text is its answer. But whitespace-only must raise.
    run, _ = _fake_runner(stdout="   \n  ")
    with pytest.raises(A.AdapterError):
        A.model_call("x", backend="codex", model="m", runner=run)


def test_p2_runner_returning_none_is_adaptererror():
    # BUG (Codex pass2 #6): a runner returning None caused an uncaught AttributeError.
    with pytest.raises(A.AdapterError):
        A.model_call("x", backend="claude", model="m",
                     runner=lambda cmd, input=None, timeout=None, env=None: None)


def test_p2_no_double_invoke_on_internal_typeerror():
    # BUG (Codex pass2 #5): a TypeError raised AFTER the runner launched was mistaken for
    # an unsupported env= kwarg and the runner ran a SECOND time. The runner must be called
    # exactly once when it accepts env=.
    calls = []
    def run(cmd, input=None, timeout=None, env=None):
        calls.append(1)
        raise TypeError("internal failure after launch")
    with pytest.raises(A.AdapterError):
        A.model_call("x", backend="claude", model="m", runner=run)
    assert len(calls) == 1                     # NOT retried
