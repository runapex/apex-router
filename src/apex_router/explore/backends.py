"""Model backends for `apex-router explore`. A backend is a callable `(prompt: str, seed: int) -> str`.

- `ollama`: local ollama /api/chat (free). Default model qwen3.8:27b-mlx, thinking off.
- `cmd`: any shell command. Prompt on stdin, reply on stdout. $EXPLORE_SEED holds the seed. The command runs
  under sandbox-exec: no reads of the hidden dirs (`deny_read`, e.g. the oracle dir), writes only in its
  workdir. Each call gets a fresh workdir with a copy of the files in `cwd` (goal.md only); the harness
  deletes it after the call. Without sandbox-exec the backend fails closed (EXPLORE_ALLOW_UNSANDBOXED=1 opts out).
Neither backend logs the prompt, the reply, stderr or the environment.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
DEFAULT_NUM_PREDICT = 6144   # 2048 cut a 27b model's code replies (smoke pilot, 2026-10-27)
DEFAULT_OLLAMA_MODEL = "qwen3.8:27b-mlx"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"


class ReplyTruncated(RuntimeError):
    """The model stopped at the token limit. The call counts and gives no candidate."""


class OllamaBackend:
    name = "ollama"

    def __init__(self, model: str = DEFAULT_OLLAMA_MODEL, url: str = DEFAULT_OLLAMA_URL,
                 temperature: float = 0.8, num_predict: int = DEFAULT_NUM_PREDICT, timeout: float = 600.0):
        self.model, self.url = model, url
        self.temperature, self.num_predict, self.timeout = temperature, num_predict, timeout
        # local server: never route through an HTTP proxy from the environment
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __call__(self, prompt: str, seed: int = 0) -> str:
        body = {"model": self.model, "stream": False, "think": False,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"temperature": self.temperature, "seed": int(seed),
                            "num_predict": self.num_predict}}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with self._opener.open(req, timeout=self.timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if data.get("done_reason") == "length":   # cut at num_predict: a half candidate is a failed call
            raise ReplyTruncated(f"reply cut at num_predict={self.num_predict}")
        return (data.get("message") or {}).get("content", "")


class CmdBackend:
    name = "cmd"

    def __init__(self, cmd: str, cwd: str | None = None, timeout: float = 600.0,
                 deny_read: list | None = None):
        self.cmd, self.cwd, self.timeout = cmd, cwd, timeout
        self.deny_read = list(deny_read or [])  # dirs the command must not read (run_task adds the oracle dir)
        self.model = cmd.split()[0] if cmd.split() else "cmd"

    def _sandbox(self, workdir: str) -> list[str]:
        if not os.path.exists(SANDBOX_EXEC):
            if os.environ.get("EXPLORE_ALLOW_UNSANDBOXED") == "1":
                return []
            raise RuntimeError("cmd backend: no sandbox-exec; set EXPLORE_ALLOW_UNSANDBOXED=1 to run unsandboxed")
        args = [SANDBOX_EXEC, "-D", "W=" + os.path.realpath(workdir)]
        prof = ('(version 1)(allow default)(deny file-write*)'
                '(allow file-write* (subpath (param "W")) (literal "/dev/null") (literal "/dev/tty"))')
        for i, d in enumerate(self.deny_read):
            args += ["-D", f"O{i}=" + os.path.realpath(d)]
            prof += f'(deny file-read* (subpath (param "O{i}")))'
        return args + ["-p", prof]

    def __call__(self, prompt: str, seed: int = 0) -> str:
        wd = tempfile.mkdtemp(prefix="explore-call-")
        try:
            if self.cwd:  # a fresh copy of the run's generator files (goal.md only) for each call
                for f in os.listdir(self.cwd):
                    if os.path.isfile(os.path.join(self.cwd, f)):
                        shutil.copy(os.path.join(self.cwd, f), os.path.join(wd, f))
            argv = self._sandbox(wd) + ["/bin/sh", "-c", self.cmd]
            p = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=self.timeout, cwd=wd,
                               env={**os.environ, "EXPLORE_SEED": str(int(seed)), "TMPDIR": wd})
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        if p.returncode != 0:
            raise RuntimeError(f"cmd backend exit {p.returncode}")  # stderr not logged: may hold secrets
        return p.stdout


def make_backend(kind: str, model: str | None = None, cmd: str | None = None, cwd: str | None = None,
                 temperature: float = 0.8, num_predict: int = DEFAULT_NUM_PREDICT, timeout: float = 600.0):
    if kind == "ollama":
        return OllamaBackend(model=model or DEFAULT_OLLAMA_MODEL, temperature=temperature,
                             num_predict=num_predict, timeout=timeout)
    if kind == "cmd":
        if not cmd:
            raise ValueError("--cmd is required for the cmd backend")
        return CmdBackend(cmd, cwd=cwd, timeout=timeout)
    raise ValueError(f"unknown backend {kind!r}")
