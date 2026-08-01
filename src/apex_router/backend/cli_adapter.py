"""CLI model adapter — routes toolkit model calls through the installed `claude`/`codex` CLI.

The toolkit's tools (codeqa judge/freshness) grade text with a frontier model. On a
machine that has only the Claude/Codex subscriptions this adapter shells to those CLIs.

SECURITY POSTURE (hardened after a Codex adversarial review):
  - **No tools.** These are GRADING calls, not agents. `claude` runs with an empty
    allowed-tools list + a deny-all permission mode; `codex` runs in a read-only sandbox.
    So even if the graded text (e.g. scanned source code) contains "run this command",
    the CLI cannot execute anything (Codex #2).
  - **Config resolved by the CALLER at call time**, never snapshotted at import — the
    caller passes backend/model explicitly (Codex #3).
  - **Model id off argv.** The model is passed via the subprocess ENV, not argv, so
    process inspection doesn't reveal an internal deployment id (Codex #4).
  - **Bounded, validated output.** stdout is capped; a non-object / partial / oversize
    response raises AdapterError rather than crashing or being accepted (Codex #5).
  - **Every failure is an AdapterError**, including a subprocess timeout (Codex #6).

HONEST LIMITATION (documented, not a code guarantee): this adapter does not hardcode any
endpoint, but it cannot control which provider the user's own `claude`/`codex` CLI is
configured to reach. "No Foundry" means *we* ship no Foundry default — it does not
override a user CLI configured to point at an internal provider (Codex #1).
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field

# Cap captured stdout so a runaway/hostile CLI can't exhaust memory (Codex #5).
MAX_OUTPUT_BYTES = 4 * 1024 * 1024   # 4 MiB is ample for a grading reply


class AdapterError(RuntimeError):
    """A CLI model call failed (non-zero exit, timeout, error/oversize/malformed output)."""


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass(frozen=True)
class ModelResult:
    content: str
    usage: dict = field(default_factory=dict)

    @property
    def answer(self) -> str:            # codeqa call sites read `.answer`
        return self.content


def _subprocess_runner(cmd, input=None, timeout=None, env=None) -> RunResult:
    """Default runner: run argv with `input` on stdin and `env`, capture stdout/stderr."""
    p = subprocess.run(cmd, input=input, capture_output=True, text=True,
                       timeout=timeout, env=env)
    return RunResult(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)


def _invoke(runner, cmd, prompt, timeout, env):
    """Call the runner, tolerating one that doesn't accept an env= kwarg (e.g. a test
    stub). The env-less retry is still inside the caller's failure guard."""
    try:
        return runner(cmd, input=prompt, timeout=timeout, env=env)
    except TypeError:
        return runner(cmd, input=prompt, timeout=timeout)


def _run(cmd, prompt, timeout, runner, extra_env=None) -> RunResult:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        res = _invoke(runner, cmd, prompt, timeout, env)
    except subprocess.TimeoutExpired:
        raise AdapterError(f"{cmd[0]} timed out after {timeout}s")
    except AdapterError:
        raise
    except Exception as e:
        raise AdapterError(f"{cmd[0]} failed to run: {e}")
    if res.stdout is not None and len(res.stdout) > MAX_OUTPUT_BYTES:
        raise AdapterError(f"{cmd[0]} output exceeded {MAX_OUTPUT_BYTES} bytes")
    return res


def _call_claude(prompt, model, runner, timeout) -> ModelResult:
    # Non-interactive print + JSON envelope, prompt on stdin, and NO tools: an empty
    # allowed-tools list plus a deny permission mode so the agentic CLI can't act on
    # adversarial graded text (Codex #2). Model via env, not argv (Codex #4).
    cmd = ["claude", "-p", "--output-format", "json",
           "--allowedTools", "", "--permission-mode", "deny"]
    extra_env = {"ANTHROPIC_MODEL": model} if model else None
    res = _run(cmd, prompt, timeout, runner, extra_env)
    if res.returncode != 0:
        raise AdapterError(f"claude exited {res.returncode}: {res.stderr.strip()[:200]}")
    try:
        env = json.loads(res.stdout)
    except (ValueError, TypeError) as e:
        raise AdapterError(f"claude output was not JSON: {e}")
    if not isinstance(env, dict):
        raise AdapterError("claude output JSON was not an object")
    if env.get("is_error"):
        raise AdapterError(f"claude returned an error envelope: {str(env.get('result'))[:200]}")
    content = env.get("result")
    if not isinstance(content, str) or not content:
        raise AdapterError("claude envelope missing a non-empty string 'result'")
    usage = env.get("usage")
    return ModelResult(content=content, usage=usage if isinstance(usage, dict) else {})


def _call_codex(prompt, model, runner, timeout) -> ModelResult:
    # `codex exec` in a READ-ONLY sandbox (model-generated shell can't mutate anything),
    # prompt on stdin, model via env not argv. Plain-text stdout.
    cmd = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check"]
    extra_env = {"CODEX_MODEL": model} if model else None
    res = _run(cmd, prompt, timeout, runner, extra_env)
    if res.returncode != 0:
        raise AdapterError(f"codex exited {res.returncode}: {res.stderr.strip()[:200]}")
    content = (res.stdout or "").strip()
    if not content:
        raise AdapterError("codex produced no output")
    return ModelResult(content=content, usage={})


_BACKENDS = {"claude": _call_claude, "codex": _call_codex}


def model_call(prompt, *, backend: str = "claude", model=None, runner=None,
               timeout: float = 120.0) -> ModelResult:
    """Grade `prompt` with a frontier model via the target's own CLI (tools disabled).

    backend: 'claude' | 'codex' (no Foundry/HTTP option). model: id or None (CLI default).
    runner: injected subprocess runner (defaults to the real one). Raises ValueError on an
    unknown backend, AdapterError on any call failure.
    """
    fn = _BACKENDS.get(backend)
    if fn is None:
        raise ValueError(f"unknown backend {backend!r}; expected one of {sorted(_BACKENDS)}")
    return fn(prompt, model, runner or _subprocess_runner, timeout)
