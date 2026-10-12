"""Item 4 (2026-10-12): pricing honesty.
  a) GPT tier SKUs (`gpt-5.6-luna/terra/sol`, `gpt-6-*`) must not carry an audited-looking `list:` label: the
     table has one gpt-5 rate, and the self-hosting doc lists Luna at $0.10/$0.50 — ~150x below it. They are
     priced at the gpt-5 placeholder but labelled `provisional:` (the F-i doctrine), same as gpt-6 already was.
  b) A negotiated-rate override file (APEX_PRICING_OVERRIDES or $APEX_HOME/pricing_overrides.json) replaces the
     table for listed models and labels every dollar `negotiated:<source>`. Bad files fail open to the table."""
from __future__ import annotations

import json

import pytest

from apex_router.proxy_engine.readout import pricing
from apex_router.proxy_engine.readout.pricing import rates_for


@pytest.fixture(autouse=True)
def _no_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("APEX_HOME", str(tmp_path))
    monkeypatch.delenv("APEX_PRICING_OVERRIDES", raising=False)
    pricing._overrides_cache.clear()
    yield
    pricing._overrides_cache.clear()


@pytest.mark.parametrize("m", ["it-entra-gpt-5.6-luna", "it-entra-gpt-5.6-terra", "it-entra-gpt-5.6-sol",
                               "it-entra-gpt-6-astra", "it-entra-gpt-6.1-sol", "it-entra-gpt-6-nova"])
def test_gpt_tier_skus_are_provisional_not_list(m):
    r = rates_for(m, "openai")
    assert r.pricing_regime.startswith("provisional:") and m in r.pricing_regime
    assert (r.input, r.output) == (15.0, 60.0)          # numbers unchanged: only the label stops overclaiming


def test_plain_gpt5_flagship_keeps_list_label():
    assert rates_for("<gateway>-gpt-5.x", "openai").pricing_regime.startswith("list:gpt-5/openai")


def _write(tmp_path, obj):
    p = tmp_path / "pricing_overrides.json"; p.write_text(json.dumps(obj)); return p


def test_override_file_wins_and_is_labelled(tmp_path):
    _write(tmp_path, {"source": "azure-ea-2026-10", "rates": {
        "it-entra-gpt-5.6-luna": {"endpoint": "openai", "input": 0.10, "cache_read": 0.01, "cache_write": 0.0, "output": 0.50},
        "it-entra-claude-opus-5-5": {"endpoint": "anthropic", "input": 5, "cache_read": 0.5, "cache_write": 6.25, "output": 25}}})
    r = rates_for("it-entra-gpt-5.6-luna", "openai")
    assert (r.input, r.cache_read, r.output) == (0.10, 0.01, 0.50)
    assert r.pricing_regime == "negotiated:azure-ea-2026-10(it-entra-gpt-5.6-luna)"
    assert rates_for("it-entra-claude-opus-5-5", "foundry").output == 25          # endpoint alias still applies
    assert rates_for("it-entra-gpt-5.6-terra", "openai").pricing_regime.startswith("provisional:")   # unlisted -> table


def test_override_wrong_endpoint_does_not_apply(tmp_path):
    _write(tmp_path, {"source": "s", "rates": {"it-entra-gpt-5.6-luna": {"endpoint": "anthropic", "input": 1, "cache_read": 0, "cache_write": 0, "output": 1}}})
    assert rates_for("it-entra-gpt-5.6-luna", "openai").pricing_regime.startswith("provisional:")


@pytest.mark.parametrize("bad", ["not json", json.dumps({"rates": {"m": {"endpoint": "openai", "input": -1, "cache_read": 0, "cache_write": 0, "output": 1}}}),
                                 json.dumps({"rates": {"m": {"endpoint": "openai", "input": "x"}}}), json.dumps([1, 2])])
def test_bad_override_file_fails_open_to_table(tmp_path, bad):
    (tmp_path / "pricing_overrides.json").write_text(bad)
    assert rates_for("it-entra-gpt-6-astra", "openai").pricing_regime.startswith("provisional:")


def test_override_path_env_beats_apex_home(tmp_path, monkeypatch):
    other = tmp_path / "x.json"; other.write_text(json.dumps({"source": "env", "rates": {
        "it-entra-gpt-6-astra": {"endpoint": "openai", "input": 1, "cache_read": 0.1, "cache_write": 0, "output": 4}}}))
    monkeypatch.setenv("APEX_PRICING_OVERRIDES", str(other))
    assert rates_for("it-entra-gpt-6-astra", "openai").pricing_regime.startswith("negotiated:env")


@pytest.mark.parametrize("val", ["Infinity", "-Infinity", "NaN", 1e400])
def test_override_rejects_non_finite_rates(tmp_path, val):
    p = tmp_path / "pricing_overrides.json"
    p.write_text('{"source":"s","rates":{"it-entra-gpt-6-astra":{"endpoint":"openai","input":%s,"cache_read":0,"cache_write":0,"output":1}}}'
                 % (json.dumps(val) if isinstance(val, str) else "1e400"))
    r = rates_for("it-entra-gpt-6-astra", "openai")
    assert r.pricing_regime.startswith("provisional:")     # whole entry rejected; falls back to the table
