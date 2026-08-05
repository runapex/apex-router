"""Codex-wire prefix-instability alarm — the section that saves teams real money.

Fires when a session's cache-served fraction is MATERIALLY below what stable prefixes achieve, on a
LARGE-input session (small sessions can't cache — sub-min-prefix — and mustn't false-alarm). The
threshold is a BOUND derived from the two-wire data, not a round number: Anthropic per-session
cache-served is min 0.939 / p10 0.963 (2026-07-19 snapshot), so the healthy floor is ~0.94; the alarm
fires below 0.70 — comfortably below the worst healthy Anthropic session, so a flagged session is
materially worse than healthy, not merely at the low end of normal. The alarm names the overpay $ and
points at the divergence report (WP3); it never dresses inference as diagnosis (static cause list).
"""
from __future__ import annotations

from apex_router.proxy_engine.readout.doctor import CACHE_SERVED_ALARM_FLOOR, SessionHealth, prefix_instability_alarm
from apex_router.proxy_engine.readout.pricing import rates_for


def _health(cache_read, input_uncached, n=6, endpoint="openai", model="gpt-5"):
    h = SessionHealth("s", endpoint, model, n_generative=n)
    h.sum_cache_read = cache_read
    h.sum_input_uncached = input_uncached
    return h


def test_threshold_is_a_derived_bound_below_the_healthy_floor():
    # healthy Anthropic min cache-served was 0.939; the alarm floor must sit clearly below it, so a
    # flagged session is materially worse than healthy — and NOT a round number chosen for tidiness.
    assert CACHE_SERVED_ALARM_FLOOR < 0.939, "alarm floor must be below the healthy Anthropic min"
    assert CACHE_SERVED_ALARM_FLOOR not in {0.5, 0.6, 0.75, 0.8, 0.9}, "must be a derived bound"


def test_alarm_fires_on_large_low_cache_session():
    # 500k input, only 30% cache-served, multi-turn → prefix instability, recoverable ceiling > 0
    h = _health(cache_read=150_000, input_uncached=350_000)
    a = prefix_instability_alarm(h, rates_for("gpt-5", "openai"))
    assert a is not None
    assert a["cache_served"] < CACHE_SERVED_ALARM_FLOOR
    assert a["recoverable_ceiling_dollars"] > 0  # an UPPER BOUND, not a realized overpay
    assert "pricing_regime" in a  # every dollar labeled
    assert "divergence" in a["next"].lower()  # points at WP3


def test_no_alarm_on_healthy_session():
    # 500k input, 97% cache-served (Anthropic-healthy) → NO alarm
    h = _health(cache_read=485_000, input_uncached=15_000)
    assert prefix_instability_alarm(h, rates_for("gpt-5", "openai")) is None


def test_no_alarm_on_small_session_even_if_uncached():
    # a tiny session (below min cacheable prefix) can't cache — must NOT false-alarm on low served
    h = _health(cache_read=0, input_uncached=800, n=2)  # 800 tok total, sub-1024 min prefix
    assert prefix_instability_alarm(h, rates_for("gpt-5", "openai")) is None


def test_recoverable_ceiling_is_the_fresh_mass_at_full_minus_read_rate():
    # ceiling = ALL fresh input × (input − cache_read) rate — the UPPER BOUND if every fresh token
    # could have been a cache read. At gpt-5 ($15 in / $1.5 read): 350_000·(15−1.5)/1e6 = 4.725.
    # It is an upper bound, not a realized loss: real fresh input has compulsory cold-start tokens.
    h = _health(cache_read=150_000, input_uncached=350_000)
    a = prefix_instability_alarm(h, rates_for("gpt-5", "openai"))
    assert abs(a["recoverable_ceiling_dollars"] - 350_000 * (15.0 - 1.5) / 1e6) < 1e-9


def test_single_turn_cold_start_does_not_alarm_even_if_large():
    # A lone first request (n_generative=1) has no prior prefix — 0% served is compulsory, not
    # instability. The alarm must require >= 2 generative turns (Codex F3).
    h = _health(cache_read=0, input_uncached=200_000, n=1)
    assert prefix_instability_alarm(h, rates_for("gpt-5", "openai")) is None
