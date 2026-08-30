"""Nightly adaptivity metrics — provider-wire context semantics."""
from __future__ import annotations

import json

from apex_router import nightly


def test_openai_context_does_not_double_count_cached_subset():
    row = {
        "endpoint_id": "openai",
        "tokens_in": 1_000,
        "cache_read_tokens": 700,
        "cache_write_tokens": 0,
    }
    assert nightly._request_context_tokens(row) == 1_000


def test_anthropic_context_adds_disjoint_token_pools():
    row = {
        "endpoint_id": "anthropic",
        "tokens_in": 100,
        "cache_read_tokens": 700,
        "cache_write_tokens": 200,
    }
    assert nightly._request_context_tokens(row) == 1_000


def test_context_tokens_fall_back_to_normalized_usage():
    for primary in ("malformed", 0):
        row = {
            "endpoint_id": "openai",
            "tokens_in": primary,
            "usage": {"input_tokens": 1_234, "cache_read_tokens": 1_000},
        }
        assert nightly._request_context_tokens(row) == 1_234


def test_legacy_codex_row_without_endpoint_uses_openai_semantics():
    row = {
        "client": "codex",
        "tokens_in": 1_000,
        "cache_read_tokens": 700,
    }
    assert nightly._request_context_tokens(row) == 1_000


def test_malformed_usage_is_ignored_instead_of_crashing():
    row = {"endpoint_id": "openai", "tokens_in": 321, "usage": ["bad-shape"]}
    assert nightly._request_context_tokens(row) == 321


def test_codex_watch_reports_wire_correct_downshift_share(tmp_path, monkeypatch):
    now = 1_000_000.0
    rows = [
        {
            "schema_version": 4,
            "ts": now - 10,
            "client": "codex",
            "endpoint_id": "openai",
            "tokens_in": 200_000,
            "cache_read_tokens": 180_000,
        },
        {
            "schema_version": 4,
            "ts": now - 5,
            "client": "codex",
            "endpoint_id": "openai",
            "tokens_in": 300_000,
            "cache_read_tokens": 290_000,
        },
    ]
    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text("[]\n" + "\n".join(json.dumps(row) for row in rows) + "\n")
    monkeypatch.setattr(
        "apex_router.model_registry.venue",
        lambda _name: {
            "downshift_ctx_ceiling": 250_000,
            "downshift_model": "kimi-k2.7-code",
            "ceiling_ctx": 1_000_000,
        },
    )

    report = nightly._codex_context_watch(now, telemetry=telemetry)

    assert "requests=2" in report
    assert "ctx p50=300,000" in report
    assert "1/2 (50%)" in report
    assert "max=300,000" in report
