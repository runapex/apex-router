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
# Defaults come from the ACTIVE LOCAL TIER rather than being spelled out here — otherwise this
# module (which runs before ornith_client binds its config) would pin the retired MLX server and
# silently defeat every tier switch.
from ..ornith import local_tier as _local_tier  # noqa: E402

_os.environ.update({k: v for k, v in _local_tier.client_env().items()
                    if k not in _os.environ})
