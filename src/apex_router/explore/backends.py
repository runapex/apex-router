"""Model backends for `apex-router explore`. A backend is a callable `(prompt: str, seed: int) -> str`.

- `ollama`: local ollama /api/chat (free). Default model qwen3.8:27b-mlx, thinking off.
- `cmd`: any shell command. Prompt on stdin, reply on stdout. $EXPLORE_SEED holds the seed.
Neither backend logs the prompt, the reply, stderr or the environment.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
DEFAULT_OLLAMA_MODEL = "qwen3.8:27b-mlx"


class OllamaBackend:
    name = "ollama"

    def __init__(self, model: str = DEFAULT_OLLAMA_MODEL, url: str = DEFAULT_OLLAMA_URL,
                 temperature: float = 0.8, num_predict: int = 2048, timeout: float = 600.0):
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
        return (data.get("message") or {}).get("content", "")


class CmdBackend:
    name = "cmd"

    def __init__(self, cmd: str, cwd: str | None = None, timeout: float = 600.0):
        self.cmd, self.cwd, self.timeout = cmd, cwd, timeout
        self.model = cmd.split()[0] if cmd.split() else "cmd"

    def __call__(self, prompt: str, seed: int = 0) -> str:
        p = subprocess.run(self.cmd, shell=True, input=prompt, capture_output=True, text=True,
                           timeout=self.timeout, cwd=self.cwd,
                           env={**os.environ, "EXPLORE_SEED": str(int(seed))})
        if p.returncode != 0:
            raise RuntimeError(f"cmd backend exit {p.returncode}")  # stderr not logged: may hold secrets
        return p.stdout


def make_backend(kind: str, model: str | None = None, cmd: str | None = None, cwd: str | None = None,
                 temperature: float = 0.8, num_predict: int = 2048, timeout: float = 600.0):
    if kind == "ollama":
        return OllamaBackend(model=model or DEFAULT_OLLAMA_MODEL, temperature=temperature,
                             num_predict=num_predict, timeout=timeout)
    if kind == "cmd":
        if not cmd:
            raise ValueError("--cmd is required for the cmd backend")
        return CmdBackend(cmd, cwd=cwd, timeout=timeout)
    raise ValueError(f"unknown backend {kind!r}")
