"""CLI model adapter — routes toolkit model calls through the installed `claude`/`codex` CLI.

The toolkit's tools (codeqa judge/freshness) grade text with a frontier model. On a
machine that has only the Claude/Codex subscriptions this adapter shells to those CLIs.

SECURITY POSTURE (hardened over two Codex adversarial reviews; flags VERIFIED against the
live CLIs):
  - **No tool execution.** These are GRADING calls, not agents. `claude` runs in
    `--permission-mode plan` (a read-only mode that cannot execute) with an explicit
    `--disallowedTools` deny-list; `codex` runs in a read-only sandbox (`-s read-only`).
    So even if the graded text (e.g. scanned source) says "run this command", the CLI
    cannot act. ('deny'/'bypassPermissions' modes are NOT used — 'deny' is invalid and
    'bypass' would be the opposite of safe.)
  - **Config resolved by the CALLER at call time** (never snapshotted at import).
  - **Model id off argv where the CLI allows it.** `claude` reads ANTHROPIC_MODEL from the
    child env (kept out of argv). `codex` IGNORES a model env var, so its model must go via
    `-c model=` (unavoidably in argv — the CLI's contract).
  - **Bounded, validated output.** stdout is capped by BYTE length; a non-object / partial
    / oversize response raises AdapterError.
  - **Every failure is an AdapterError** — timeout, a misbehaving runner, a malformed
    result — nothing else escapes.

HONEST LIMITATION (documented, not a code guarantee): this adapter hardcodes no endpoint,
but it cannot control which provider the user's own `claude`/`codex` CLI is configured to
reach. "No Foundry" means we ship no Foundry default; it does not override a user CLI
configured to point at an internal provider.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field

# Cap captured stdout by BYTES (not characters — multibyte content must not slip past).
MAX_OUTPUT_BYTES = 4 * 1024 * 1024   # 4 MiB is ample for a grading reply

# Tools a grading call must never be able to use. `plan` mode already blocks execution;
# this deny-list is defense-in-depth against any tool that could act or exfiltrate.
_CLAUDE_DISALLOWED = "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task"


class AdapterError(RuntimeError):
    """A CLI model call failed (non-zero exit, timeout, misbehaving runner, or bad output)."""


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
    p = subprocess.run(cmd, input=input, capture_output=True, text=True,
                       timeout=timeout, env=env)
    return RunResult(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)


def _runner_accepts_env(runner) -> bool:
    """Detect whether the runner accepts an env= kwarg WITHOUT calling it — so we never
    invoke a runner twice (a TypeError raised *after* launch must not trigger a retry,
    Codex pass2 #5)."""
    try:
        import inspect
        sig = inspect.signature(runner)
        params = sig.parameters
        return "env" in params or any(
            p.kind == p.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        return True   # builtins / C funcs: assume kwargs ok; the real runner accepts env


def _run(cmd, prompt, timeout, runner, extra_env=None) -> RunResult:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        if _runner_accepts_env(runner):
            res = runner(cmd, input=prompt, timeout=timeout, env=env)
        else:
            res = runner(cmd, input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AdapterError(f"{cmd[0]} timed out after {timeout}s")
    except AdapterError:
        raise
    except Exception as e:
        raise AdapterError(f"{cmd[0]} failed to run: {type(e).__name__}")
    # Validate the runner's return shape — a misbehaving runner must become AdapterError,
    # not an uncaught AttributeError downstream (Codex pass2 #6).
    if res is None or not hasattr(res, "returncode") or not hasattr(res, "stdout"):
        raise AdapterError(f"{cmd[0]} runner returned a malformed result")
    stdout = res.stdout if isinstance(res.stdout, str) else ""
    if len(stdout.encode("utf-8", "ignore")) > MAX_OUTPUT_BYTES:
        raise AdapterError(f"{cmd[0]} output exceeded {MAX_OUTPUT_BYTES} bytes")
    return res


def _call_claude(prompt, model, runner, timeout) -> ModelResult:
    # Non-interactive print + JSON envelope, prompt on stdin, and NO execution: 'plan'
    # mode (read-only) + a disallowed-tools deny-list. Model via env, not argv.
    cmd = ["claude", "-p", "--output-format", "json",
           "--permission-mode", "plan", "--disallowedTools", _CLAUDE_DISALLOWED]
    extra_env = {"ANTHROPIC_MODEL": model} if model else None
    res = _run(cmd, prompt, timeout, runner, extra_env)
    if res.returncode != 0:
        raise AdapterError(f"claude exited {res.returncode}")
    try:
        env = json.loads(res.stdout)
    except (ValueError, TypeError):
        raise AdapterError("claude output was not JSON")
    if not isinstance(env, dict):
        raise AdapterError("claude output JSON was not an object")
    # A complete, successful envelope carries an explicit is_error flag AND a string
    # result. A partial envelope (missing is_error) is not trusted (Codex pass2 #3).
    if "is_error" not in env:
        raise AdapterError("claude envelope missing 'is_error' (partial response)")
    if env.get("is_error"):
        raise AdapterError("claude returned an error envelope")
    content = env.get("result")
    if not isinstance(content, str) or not content.strip():
        raise AdapterError("claude envelope missing a non-empty string 'result'")
    usage = env.get("usage")
    return ModelResult(content=content, usage=usage if isinstance(usage, dict) else {})


def _call_codex(prompt, model, runner, timeout) -> ModelResult:
    # `codex exec` in a READ-ONLY sandbox. The codex CLI ignores a model env var, so the
    # model goes via `-c model=` (the CLI's contract) — unavoidably in argv.
    cmd = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check"]
    if model:
        cmd += ["-c", f"model={model}"]
    res = _run(cmd, prompt, timeout, runner, None)
    if res.returncode != 0:
        raise AdapterError(f"codex exited {res.returncode}")
    content = res.stdout if isinstance(res.stdout, str) else ""
    content = content.strip()
    if not content:
        raise AdapterError("codex produced no output")
    return ModelResult(content=content, usage={})


_BACKENDS = {"claude": _call_claude, "codex": _call_codex}


def model_call(prompt, *, backend: str = "claude", model=None, runner=None,
               timeout: float = 120.0) -> ModelResult:
    """Grade `prompt` with a frontier model via the target's own CLI (no tool execution).

    backend: 'claude' | 'codex' (no Foundry/HTTP option). model: id or None (CLI default).
    Raises ValueError on an unknown backend, AdapterError on ANY call failure.
    """
    fn = _BACKENDS.get(backend)
    if fn is None:
        raise ValueError(f"unknown backend {backend!r}; expected one of {sorted(_BACKENDS)}")
    return fn(prompt, model, runner or _subprocess_runner, timeout)
