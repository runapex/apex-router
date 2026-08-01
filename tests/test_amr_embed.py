"""Tests for amr.embed — thin nomic-embed-text (ollama) client + cosine (§7/§11).

The real client hits ollama at /api/embeddings; tests inject a fake `post_fn` so
they stay hermetic. One opt-in live smoke test (skipped unless RUN_LIVE_EMBED=1)
exercises the real server.
"""
import math
import os

import pytest

from apex_router import embed


def _fake_post(vec):
    """Return a post_fn stub yielding ollama's {'embedding': vec} shape, and record calls."""
    seen = {}
    def post(url, payload):
        seen["url"] = url
        seen["payload"] = payload
        return {"embedding": list(vec)}
    return post, seen


def test_embed_returns_vector_from_response():
    post, seen = _fake_post([0.1, 0.2, 0.3])
    v = embed.embed("fix the failing auth test", post_fn=post)
    assert v == [0.1, 0.2, 0.3]


def test_embed_posts_model_and_prompt_to_embeddings_endpoint():
    post, seen = _fake_post([1.0, 0.0])
    embed.embed("hello", model="nomic-embed-text", post_fn=post)
    assert "api/embeddings" in seen["url"]
    assert seen["payload"]["model"] == "nomic-embed-text"
    assert seen["payload"]["prompt"] == "hello"


def test_embed_raises_on_empty_text():
    post, _ = _fake_post([1.0])
    with pytest.raises(ValueError):
        embed.embed("   ", post_fn=post)


def test_embed_raises_on_malformed_response():
    def bad_post(url, payload):
        return {"not_embedding": []}
    with pytest.raises(embed.EmbedError):
        embed.embed("hello", post_fn=bad_post)


def test_cosine_identical_vectors_is_one():
    assert embed.cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0, abs=1e-9)


def test_cosine_orthogonal_is_zero():
    assert embed.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0, abs=1e-9)


def test_cosine_opposite_is_negative_one():
    assert embed.cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0, abs=1e-9)


def test_cosine_scale_invariant():
    a, b = [1.0, 2.0, 3.0], [2.0, 4.0, 6.0]  # b = 2a
    assert embed.cosine(a, b) == pytest.approx(1.0, abs=1e-9)


def test_cosine_zero_vector_raises():
    with pytest.raises(ValueError):
        embed.cosine([0.0, 0.0], [1.0, 1.0])


def test_cosine_length_mismatch_raises():
    with pytest.raises(ValueError):
        embed.cosine([1.0, 2.0], [1.0, 2.0, 3.0])


@pytest.mark.skipif(os.environ.get("RUN_LIVE_EMBED") != "1",
                    reason="set RUN_LIVE_EMBED=1 to hit the live ollama server")
def test_embed_live_smoke():
    v = embed.embed("fix the failing test in the auth module")
    assert len(v) == 768
    assert all(math.isfinite(x) for x in v)
