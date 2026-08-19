"""Repo-agnostic code-Q&A harness over a local model behind an optional proxy.

Deterministic tools traverse (retriever.py: ripgrep + optional clangd index); the model
reads the retrieved, cited chunks and answers (driver.py), with the repo's architecture
digest pinned as a frozen preamble for prompt-cache reuse. Repos are registered as
codeqa/repos/<name>.json — a C++ repo and a Ruby repo are the reference configs.

Backend configuration is a SINGLE point, set HERE — before any `import ornith_client`, whose
module-level config binds from the environment at import — so EVERY seam (answerer in driver.py,
verifier in cli.py, A/B in ab.py) agrees without depending on a shell wrapper's exports. We use
`setdefault`, so an explicit environment value always wins; the defaults below reproduce the
stock local-server behavior. Point a deployment at a different endpoint/model by exporting
ORNITH_URL / ORNITH_API_MODEL / ORNITH_THINKING_STYLE (e.g. to front the model with a proxy and
serve an OpenAI-compatible backend that needs an explicit model id and `reasoning_effort` gating).
"""
import os as _os

# setdefault: only fill what the caller/shell hasn't already set, so an explicit override wins.
# Defaults reproduce the stock local server (single-model, chat-template thinking) — override via env.
_os.environ.setdefault("ORNITH_URL", "http://127.0.0.1:8080")
_os.environ.setdefault("ORNITH_API_MODEL", "")            # empty = single-model server; set for OpenAI-style
_os.environ.setdefault("ORNITH_THINKING_STYLE", "chat_template")
