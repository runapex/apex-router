"""CLI model adapter — the Foundry replacement for the runapex toolkit.

The toolkit's tools (codeqa judge/freshness, etc.) used to POST to a Foundry proxy with
an internal model id. On a machine that has only the Claude and Codex subscriptions and
no Foundry endpoint, that path is dead. This adapter routes a model call through the
installed `claude` or `codex` CLI instead — the subscriptions the target actually has —
and parses the CLI output back into a small {content, usage} result the callers expect.

Design:
  - `model_call(prompt, backend=, model=, runner=)` is the single entry point.
  - `runner` is an injected seam (a function that runs argv + stdin and returns a
    RunResult). Tests pass a fake; production uses `_subprocess_runner`. This keeps the
    adapter hermetically testable and means NOTHING shells out during tests.
  - Only `claude` and `codex` are valid backends. There is NO Foundry/HTTP default and
    no internal model id — an unknown backend raises, so the no-Foundry guarantee holds
    by construction.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field


class AdapterError(RuntimeError):
    """A CLI model call failed (non-zero exit, error envelope, or unparseable output)."""


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass(frozen=True)
class ModelResult:
    content: str
    usage: dict = field(default_factory=dict)
    raw: str = ""

    # codeqa callers historically read `.answer` off the ornith client result; expose it
    # as an alias so those call sites work unchanged when pointed at the adapter.
    @property
    def answer(self) -> str:
        return self.content


def _subprocess_runner(cmd, input=None, timeout=None) -> RunResult:
    """Default runner: run argv with `input` on stdin, capture stdout/stderr."""
    p = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
    return RunResult(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)


def _call_claude(prompt, model, runner, timeout) -> ModelResult:
    # Non-interactive print mode with a JSON envelope. Prompt goes on STDIN (never argv)
    # so large / shell-metacharacter prompts are safe.
    cmd = ["claude", "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    res = runner(cmd, input=prompt, timeout=timeout)
    if res.returncode != 0:
        raise AdapterError(f"claude exited {res.returncode}: {res.stderr.strip()[:200]}")
    try:
        env = json.loads(res.stdout)
    except (ValueError, TypeError) as e:
        raise AdapterError(f"claude output was not JSON: {e}")
    if env.get("is_error"):
        raise AdapterError(f"claude returned an error envelope: {str(env.get('result'))[:200]}")
    content = env.get("result")
    if not isinstance(content, str):
        raise AdapterError("claude envelope missing a string 'result'")
    return ModelResult(content=content, usage=env.get("usage", {}) or {}, raw=res.stdout)


def _call_codex(prompt, model, runner, timeout) -> ModelResult:
    # `codex exec` with the prompt on stdin and the model via -c model=<id>. Codex prints
    # plain text (not a JSON envelope), so content is the trimmed stdout.
    cmd = ["codex", "exec"]
    if model:
        cmd += ["-c", f"model={model}"]
    res = runner(cmd, input=prompt, timeout=timeout)
    if res.returncode != 0:
        raise AdapterError(f"codex exited {res.returncode}: {res.stderr.strip()[:200]}")
    content = (res.stdout or "").strip()
    if not content:
        raise AdapterError("codex produced no output")
    return ModelResult(content=content, usage={}, raw=res.stdout)


_BACKENDS = {"claude": _call_claude, "codex": _call_codex}


def model_call(prompt, *, backend: str = "claude", model=None, runner=None,
               timeout: float = 120.0) -> ModelResult:
    """Call a model through the target's own CLI subscription.

    backend: 'claude' | 'codex' (no Foundry/HTTP option by design).
    model:   the model id to pass to that CLI (None -> the CLI's default).
    runner:  injected subprocess runner (defaults to the real one).
    Raises ValueError on an unknown backend, AdapterError on any call failure.
    """
    fn = _BACKENDS.get(backend)
    if fn is None:
        raise ValueError(f"unknown backend {backend!r}; expected one of {sorted(_BACKENDS)}")
    return fn(prompt, model, runner or _subprocess_runner, timeout)
