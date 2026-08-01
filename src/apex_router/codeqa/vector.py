"""Vector-similarity fallback for the code-Q&A retriever (PHASE 2 — seam only).

The hybrid design does symbol/keyword retrieval first (retriever.py) and only falls
back here when a question shares no keywords with the code. That layer needs an
embedding model + a vector store, neither of which is a dependency of the working
keyword path — so this module is intentionally a stub with a stable interface.

To activate later, implement `build_index(cfg)` (chunk the repo, embed, persist) and
`similar_chunks(cfg, question, k)` (embed query, cosine top-k → Chunk list). Keep the
Chunk shape identical so the caller is unchanged. Candidate local embedders: an MLX
sentence-embedding model on the same box, or a small CPU model; persist vectors under
codeqa/index/<repo>.faiss (or a flat npy) to avoid a heavy DB.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .retriever import Chunk, RepoConfig


class VectorIndexNotBuilt(RuntimeError):
    pass


def build_index(cfg: "RepoConfig") -> None:  # pragma: no cover - phase 2
    raise VectorIndexNotBuilt(
        "Vector fallback is a phase-2 seam and is not built yet. The keyword/symbol "
        "path (retriever.retrieve) is the supported default.")


def similar_chunks(cfg: "RepoConfig", question: str, k: int = 5) -> "list[Chunk]":  # pragma: no cover
    # Deliberately raise so retriever.retrieve's try/except cleanly ignores the
    # fallback until this is implemented. Returning [] would hide non-implementation.
    raise VectorIndexNotBuilt("vector fallback not implemented")
