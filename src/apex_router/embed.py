"""Thin nomic-embed-text (ollama) client plus cosine similarity.

Standard library only: json, math, urllib.request.
"""

import json
import math
import urllib.request
from typing import Optional, Callable

OLLAMA_URL = "http://127.0.0.1:11434"


class EmbedError(Exception):
    """Raised on transport or JSON parsing failures with Ollama."""
    pass


def _http_post(url: str, payload: dict) -> dict:
    """POST json `payload` to `url`, return parsed json dict.

    Uses urllib.request with a 30s timeout and Content-Type application/json.
    Raises EmbedError on transport/JSON failure.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except (OSError, ValueError, IOError) as exc:
        raise EmbedError(f"HTTP request failed: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise EmbedError(f"Invalid JSON response: {exc}") from exc


def embed(
    text: str,
    model: str = "nomic-embed-text",
    post_fn: Optional[Callable[[str, dict], dict]] = None,
) -> list[float]:
    """Embed `text` via ollama /api/embeddings.

    Raises ValueError if text.strip() is empty.
    Builds url = OLLAMA_URL + "/api/embeddings" and payload
    {"model": model, "prompt": text}.
    Calls post_fn(url, payload) if given (dependency-injection seam for tests),
    else _http_post.
    The response must contain key "embedding" (a list of floats); returns it.
    Raises EmbedError if "embedding" is missing or not a non-empty list.
    """
    if not text.strip():
        raise ValueError("text must not be empty after stripping whitespace")

    url = f"{OLLAMA_URL}/api/embeddings"
    payload = {"model": model, "prompt": text}

    if post_fn is None:
        post_fn = _http_post

    try:
        response = post_fn(url, payload)
    except EmbedError:
        raise
    except Exception as exc:
        raise EmbedError(f"Embed call failed: {exc}") from exc

    if "embedding" not in response:
        raise EmbedError("Response missing 'embedding' key")

    embedding = response["embedding"]
    if not isinstance(embedding, list) or len(embedding) == 0:
        raise EmbedError("'embedding' must be a non-empty list")

    if not all(isinstance(v, (int, float)) for v in embedding):
        raise EmbedError("'embedding' must contain only numeric values")

    return [float(v) for v in embedding]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors.

    Raises ValueError if lengths differ or if either vector has zero norm.
    Returns dot(a,b)/(||a||*||b||) as a float.
    """
    if len(a) != len(b):
        raise ValueError(f"Vectors must have equal length, got {len(a)} and {len(b)}")

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y

    norm_a = math.sqrt(norm_a)
    norm_b = math.sqrt(norm_b)

    if norm_a == 0.0 or norm_b == 0.0:
        raise ValueError("Cannot compute cosine similarity: zero norm vector")

    return dot / (norm_a * norm_b)
