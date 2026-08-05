"""The single source of truth for the local job-queue location.

The worker and every enqueuer resolve the queue dir through `queue_root()` so they can never
disagree. It is config-driven — `APEX_ORNITH_QUEUE` overrides, else a stable default under the
user's home that does NOT depend on where the code lives. That independence is what lets a machine
run the installed/packaged worker while keeping its queue in one fixed place (fixing the drift where
the queue moved into the package source tree).
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT = Path.home() / ".apex-router" / "queue"


def queue_root(env: dict | None = None) -> Path:
    """Resolve the queue root: APEX_ORNITH_QUEUE if set (non-blank), else the stable default.
    `~` is expanded so an env value like `~/q` works."""
    source = os.environ if env is None else env
    override = (source.get("APEX_ORNITH_QUEUE") or "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT
