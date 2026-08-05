"""Pricing precision + schema exclusion — two Codex-xval findings.

F8: substring matching (`"gpt-5" in model`) silently priced EVERY variant at the flagship rate —
`gpt-5-mini`/`gpt-5-nano` (much cheaper SKUs) would be billed as full gpt-5 (~60× overprice), and
the match was invisible in the output. Fix: (a) refuse the known cheaper variants so they fall to a
labeled `unknown` rate instead of a wrong flagship one; (b) carry the ACTUAL model in the regime
label so every substring match is auditable (the "make the substitution honest" principle).

F10: `load_rows` claimed to keep only supported schema versions but appended every non-heartbeat row,
so unknown-schema rows silently flowed into the dollar totals while the report said "not analyzed".
Fix: is_generative/is_auxiliary now gate on the schema, so an unknown-schema row contributes to NO
metric.
"""
from __future__ import annotations

from apex_router.proxy_engine.readout.doctor import build_report, is_generative, session_health
from apex_router.proxy_engine.readout.pricing import rates_for


# ---------- F8: variant SKUs must not be silently flagship-priced ----------

def test_flagship_match_carries_the_actual_model_for_audit():
    # The real wire model is vendor-prefixed (`<gateway>-gpt-5.x`); substring is needed to strip
    # the prefix. But the regime label must name the ACTUAL model so the match is auditable.
    r = rates_for("<gateway>-gpt-5.x", "openai")
    assert r.input == 15.0, "the flagship gpt-5 family is priced at list"
    assert "<gateway>-gpt-5.x" in r.pricing_regime, "regime must name the actual matched model"


def test_cheaper_variants_are_not_priced_as_the_flagship():
    # gpt-5-mini / gpt-5-nano are materially cheaper SKUs — pricing them as full gpt-5 is a large
    # overprice. They must fall through to a labeled `unknown` rate, never the flagship number.
    for variant in ("gpt-5-mini", "gpt-5-nano", "<gateway>-gpt-5-mini"):
        r = rates_for(variant, "openai")
        assert r.pricing_regime.startswith("unknown:"), f"{variant} must not be flagship-priced"
        assert r.input == 0.0, f"{variant} unknown rate is zero (visibly un-priced)"


def test_opus_variant_still_resolves_and_names_the_model():
    r = rates_for("<gateway>-claude-opus-x", "anthropic")
    assert r.input == 15.0
    assert "<gateway>-claude-opus-x" in r.pricing_regime


def test_variant_marker_only_refuses_a_delimited_token_not_a_substring():
    # The denylist must match `mini`/`nano`/… as a DELIMITED TOKEN, not an arbitrary substring, or a
    # legitimate flagship routed through a proxy whose name merely CONTAINS the letters is wrongly
    # unpriced. `litellm-gpt-5` (contains "lite") and `satellite-gpt-5` (contains "lite") are real
    # flagship deployments and MUST price at gpt-5, not fall to unknown. (Codex pass-2 P2.)
    for alias in ("litellm-gpt-5", "satellite-gpt-5", "gpt-5-nanotech-eval"):
        r = rates_for(alias, "openai")
        assert not r.pricing_regime.startswith("unknown:"), f"{alias} is a flagship, must be priced"
        assert r.input == 15.0
    # ...but a true delimited variant token is still refused:
    assert rates_for("gpt-5-mini", "openai").pricing_regime.startswith("unknown:")
    assert rates_for("gpt-5-nano", "openai").pricing_regime.startswith("unknown:")


# ---------- F10: unknown-schema rows must not reach the dollar totals ----------

def _gen_v(schema_version):
    return {
        "schema_version": schema_version, "session_id": "s", "endpoint_id": "anthropic",
        "model_resolved": "opus", "is_error": False, "tokens_out": 50, "tokens_in": 1_000,
        "cache_read_tokens": 100_000, "cache_write_tokens": 0,
        "usage": {"input_tokens": 1_000, "output_tokens": 50},
    }


def test_unknown_schema_row_is_not_generative():
    assert is_generative(_gen_v(3)) is True
    assert is_generative(_gen_v(99)) is False, "an unknown-schema row must not count as generative"


def test_unknown_schema_row_does_not_reach_the_dollar_total():
    # One supported + one unknown-schema row (same big cache read). The total must reflect ONLY the
    # supported row — the report says unknown schemas are "not analyzed", so they must not add $.
    rep = build_report([_gen_v(3), _gen_v(99)])
    assert rep["denominators"]["generative_turns"] == 1, "only the supported row is generative"
    assert 99 in rep["unsupported_schema_versions"]
    one_row = build_report([_gen_v(3)])["totals"]["dollars_saved_by_cache"]
    assert rep["totals"]["dollars_saved_by_cache"] == one_row, "unknown-schema $ must not be added"


def test_session_health_ignores_unknown_schema_rows():
    h = session_health([_gen_v(3), _gen_v(99)])
    assert list(h.values())[0].n_generative == 1


def test_unknown_schema_error_is_not_counted_in_the_errors_denominator():
    # An error row on an UNKNOWN schema must not increment the `errors` denominator either — the
    # report says unknown schemas are "not analyzed", so NO metric (gen/aux/err) may include them.
    # (Codex pass-2 P1.)
    supported_err = {"schema_version": 3, "is_error": True, "usage": None, "tokens_out": 0}
    unknown_err = {"schema_version": 99, "is_error": True, "usage": None, "tokens_out": 0}
    rep = build_report([supported_err, unknown_err])
    assert rep["denominators"]["errors"] == 1, "only the supported-schema error is counted"
    assert 99 in rep["unsupported_schema_versions"]
