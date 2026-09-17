"""Pricing table — every dollar figure is labeled by regime (F-i doctrine)."""
from __future__ import annotations

from apex_router.proxy_engine.readout.pricing import rates_for


def test_anthropic_opus_rates_labeled():
    r = rates_for("<gateway>-claude-opus-x", "anthropic")
    assert r.input == 15.0 and r.cache_read == 1.5 and r.cache_write == 18.75
    assert "list:opus/anthropic" in r.pricing_regime


def test_openai_gpt5_has_no_cache_write_and_is_labeled():
    r = rates_for("<gateway>-gpt-5.x", "openai")
    assert r.input == 15.0 and r.cache_read == 1.5
    assert r.cache_write == 0.0, "OpenAI caching is automatic — NO write premium (structural, not unknown)"
    assert "list:gpt-5/openai" in r.pricing_regime


def test_kimi_openai_wire_rates_are_pinned_and_labeled():
    expected = {
        "kimi-k3": (3.0, 0.3, 15.0),
        "kimi-k2.7-code": (0.95, 0.19, 4.0),
        "kimi-k2.6": (0.95, 0.16, 4.0),
    }
    for model, (input_rate, read_rate, output_rate) in expected.items():
        r = rates_for(model, "openai")
        assert (r.input, r.cache_read, r.cache_write, r.output) == (
            input_rate, read_rate, 0.0, output_rate,
        )
        assert f"list:{model}/openai:2026-08-pi-catalog" in r.pricing_regime


def test_kimi_premium_variants_stay_unpriced_until_their_rate_is_pinned():
    for model in ("kimi-k3-turbo", "kimi-k2.7-code-highspeed"):
        assert rates_for(model, "openai").pricing_regime.startswith("unknown:")


def test_unknown_pair_is_labeled_unknown_with_zero_rates():
    # a dollar figure on unpriced traffic must read as un-priced, never faked
    r = rates_for("some-future-model", "some-endpoint")
    assert r.input == 0.0 and r.cache_read == 0.0
    assert r.pricing_regime.startswith("unknown:"), "unpriced traffic must be labeled unknown"


def test_foundry_endpoint_aliases_to_anthropic_rates():
    # the wire tags the Azure-Foundry Anthropic deployment endpoint_id='foundry'; it must price at
    # anthropic rates, not miss the table and read as $0-unknown (regression: 10,763 Claude reqs).
    r = rates_for("it-entra-claude-opus-4-8", "foundry")
    assert r.input == 15.0 and r.cache_read == 1.5 and r.cache_write == 18.75 and r.output == 75.0
    assert "list:opus/anthropic" in r.pricing_regime
    # sonnet + haiku over foundry too
    assert rates_for("it-entra-claude-sonnet-4-6", "foundry").cache_write == 3.75
    assert rates_for("it-entra-claude-haiku-4-5", "foundry").input == 0.8


def test_gpt6_is_priced_provisionally_and_labeled_as_such():
    # gpt-6 (e.g. it-entra-gpt-6-astra) has no audited list price yet: priced at gpt-5 rates but the
    # regime must say `provisional`, never `list`, so the assumption is visible (F-i doctrine).
    r = rates_for("it-entra-gpt-6-astra", "openai")
    assert (r.input, r.cache_read, r.cache_write, r.output) == (15.0, 1.5, 0.0, 60.0)
    assert r.pricing_regime.startswith("provisional:"), "gpt-6 rate is assumed, must not read as audited list"
    # gpt-5.x must still bind to the audited gpt-5 row, not the provisional gpt-6 one
    assert "list:gpt-5/openai" in rates_for("it-entra-gpt-5.6-sol", "openai").pricing_regime


def test_endpoint_disambiguates_same_model_shape():
    # anthropic vs openai are distinct even though both could look 'gpt/claude'-ish; endpoint is exact
    assert rates_for("opus", "anthropic").pricing_regime != rates_for("gpt-5", "openai").pricing_regime
    # opus on the wrong endpoint is unknown, not silently mispriced
    assert rates_for("opus", "openai").pricing_regime.startswith("unknown:")
