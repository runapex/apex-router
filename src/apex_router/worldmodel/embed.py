"""Optional frozen request embedding for the P6 world model (off by default).

A task's request text can be embedded once with ollama ``nomic-embed-text`` and cached as a
float16 ``.npy`` (768-d) under ``<data home>/embed/`` keyed by a hash of the task id. The text is
never stored — only the vector. The ollama call is loopback-only (127.0.0.1), has a 2 s timeout
and fails open: on any error the caller gets zeros and ``ok=False``, which the feature builder
turns into a "missing embedding" flag column.

Who calls the network: E1 (dataset build, which has the request text) and E4 populate the cache
via ``task_embedding(task_id, text)``. E3 never calls the network during training or scoring:
``task_embeddings`` reads the cache only, and a task without a cached vector gets zeros + flag.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from pathlib import Path

import numpy as np

EMBED_DIM = 768
EMBED_URL = "http://127.0.0.1:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
TIMEOUT_S = 2.0


def data_home() -> Path:
    return Path(os.environ.get("APEX_ROUTER_HOME") or Path.home() / ".apex-router") / "worldmodel"


def cache_dir(home: Path | None = None) -> Path:
    return (home or data_home()) / "embed"


def _key(task_id: str) -> str:
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:32]


def _post(text: str) -> list[float]:
    req = urllib.request.Request(
        EMBED_URL, data=json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    if req.host not in ("127.0.0.1:11434", "127.0.0.1"):      # loopback only
        raise ValueError("embedding endpoint must be loopback")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))["embedding"]


def task_embedding(task_id: str, text: str | None = None, home: Path | None = None,
                   post_fn=None) -> tuple[np.ndarray, bool]:
    """Return ``(vector float32 (768,), ok)`` for ``task_id``.

    Cached vector -> it. Else, if ``text`` is given, embed it (``post_fn(text)`` overrides the
    HTTP call for tests), cache the float16 vector (file 0600, dir 0700) and return it. Any failure
    or no text -> zeros and ``ok=False``. ``text`` is used only for the call and never written.
    """
    d = cache_dir(home)
    path = d / f"{_key(task_id)}.npy"
    try:
        if path.exists():
            v = np.load(path, allow_pickle=False).astype(np.float32).reshape(-1)
            if v.shape == (EMBED_DIM,):
                return v, True
    except (OSError, ValueError):
        pass
    if not text or not text.strip():
        return np.zeros(EMBED_DIM, np.float32), False
    try:
        vec = np.asarray((post_fn or _post)(text), dtype=np.float32).reshape(-1)
        if vec.shape != (EMBED_DIM,) or not np.all(np.isfinite(vec)):
            return np.zeros(EMBED_DIM, np.float32), False
    except Exception:            # fail open: ollama down, timeout, bad JSON, wrong shape
        return np.zeros(EMBED_DIM, np.float32), False
    try:
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        tmp = path.with_suffix(".tmp.npy")
        np.save(tmp, vec.astype(np.float16), allow_pickle=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass
    return vec, True


def task_embeddings(task_ids, home: Path | None = None) -> dict[str, np.ndarray]:
    """Cached vectors only (no network) for ``task_ids``, each with a trailing missing-flag:
    ``(769,)`` = 768 embedding dims + 1.0 when the task had no cached embedding."""
    out = {}
    for tid in task_ids:
        v, ok = task_embedding(tid, None, home=home)
        out[tid] = np.concatenate([v, np.array([0.0 if ok else 1.0], np.float32)])
    return out
