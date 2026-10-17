"""DeepSeek remote backend: registry entries, prices, peak rule, hygiene guard, key lookup, client."""
import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from apex_router import model_registry as mr
from apex_router.remote import deepseek_client as ds


def _reg(**remote_over):
    reg = json.loads(json.dumps(mr.DEFAULTS))
    reg["remote"]["deepseek"].update(remote_over)
    return reg


def test_registry_has_deepseek_remote_and_prices():
    r = mr.remote("deepseek")
    assert r["base_url"] == "https://api.deepseek.com"
    assert r["default_model"] == "deepseek-v4-pro"
    assert mr.price("claude-sonnet-5-5") == {"in": 2.0, "cached": 0.10, "out": 10.0}
    assert mr.price("claude-opus-5-5") == {"in": 4.0, "cached": 0.20, "out": 20.0}
    assert mr.price("deepseek-v4-pro") == {"in": 1.32, "cached": 0.044, "out": 3.96}
    assert mr.price("no-such-model") is None
    assert mr.remote("no-such-backend") is None


def test_price_resolves_a_tier_name():
    assert mr.price("sonnet") == mr.price("claude-sonnet-5-5")
    assert mr.price("haiku") == {"in": 1.0, "cached": 0.10, "out": 5.0}


@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc), True),    # Monday 07:00
    (datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc), True),    # Monday 01:00
    (datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc), False),   # Monday 04:00 (end is exclusive)
    (datetime(2026, 10, 19, 5, 30, tzinfo=timezone.utc), False),  # Monday 05:30
    (datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc), False),   # Saturday
])
def test_peak_rule(when, expected):
    assert ds.is_peak(when, registry=mr.DEFAULTS) is expected


def test_call_cost_peak_and_offpeak():
    usage = {"prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000,
             "completion_tokens": 1_000_000}
    peak = ds.call_cost("deepseek-v4-pro", usage, peak=True, registry=mr.DEFAULTS)
    off = ds.call_cost("deepseek-v4-pro", usage, peak=False, registry=mr.DEFAULTS)
    assert peak == pytest.approx(0.044 + 1.32 + 3.96)
    assert off == pytest.approx(peak / 2)


def test_call_cost_without_cache_split_counts_prompt_as_miss():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    assert ds.call_cost("deepseek-flash", usage, peak=True, registry=mr.DEFAULTS) == pytest.approx(0.30)


def test_baseline_cost_prices_completion_at_the_claude_tier():
    assert ds.baseline_cost("sonnet", 1_000_000, registry=mr.DEFAULTS) == pytest.approx(10.0)
    assert ds.baseline_cost("unknown-tier", 1_000_000, registry=mr.DEFAULTS) is None


@pytest.mark.parametrize("text,reason", [
    ("key sk-abcdefghijklmnopqrstuvwxyz0123", "secret"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "secret"),
    ("password: hunter22", "secret"),
    ("mail a.b@example.com today", "e-mail address"),
])
def test_hygiene_refuses(text, reason):
    with pytest.raises(ds.RemoteRefused) as e:
        ds.check_text(text, registry=mr.DEFAULTS)
    assert e.value.reason == reason


def test_hygiene_overlay_deny_regex():
    reg = _reg(deny_regex=["(?i)secretproject"])
    with pytest.raises(ds.RemoteRefused) as e:
        ds.check_text("notes on SecretProject", registry=reg)
    assert e.value.reason == "denied pattern"
    ds.check_text("a plain post draft about a parcel depot game", registry=reg)


def test_api_key_env_wins_then_keychain():
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return "kc-key\n"

    assert ds.api_key(mr.DEFAULTS, env={"DEEPSEEK_API_KEY": "env-key"}, runner=runner) == "env-key"
    assert calls == []
    assert ds.api_key(mr.DEFAULTS, env={}, runner=runner) == "kc-key"
    assert "deepseek-api" in calls[0]


def test_api_key_missing_raises_unavailable():
    def runner(cmd):
        raise OSError("no keychain")

    with pytest.raises(ds.RemoteUnavailable):
        ds.api_key(mr.DEFAULTS, env={}, runner=runner)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_chat_success_returns_answer_usage_and_cost():
    seen = {}

    def transport(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data)
        return _Resp(json.dumps({"choices": [{"message": {"content": " ok "}, "finish_reason": "stop"}],
                                 "usage": {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100,
                                           "completion_tokens": 10}}).encode())

    now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)  # Saturday: off-peak
    r = ds.chat("say ok", registry=mr.DEFAULTS, transport=transport, key="k", now=now)
    assert r.answer == "ok"
    assert r.model == "deepseek-v4-pro"
    assert r.peak is False
    assert r.cost_usd == pytest.approx((100 * 1.32 + 10 * 3.96) / 1e6 / 2)
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "deepseek-v4-pro"


def test_chat_refuses_before_any_request():
    def transport(req, timeout):
        raise AssertionError("must not send")

    with pytest.raises(ds.RemoteRefused):
        ds.chat("token = abcdefgh123", registry=mr.DEFAULTS, transport=transport, key="k")


def test_chat_http_402_is_unavailable():
    def transport(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 402, "Payment Required", {}, None)

    with pytest.raises(ds.RemoteUnavailable) as e:
        ds.chat("hello", registry=mr.DEFAULTS, transport=transport, key="k")
    assert "402" in str(e.value)


def test_chat_empty_answer_is_unavailable():
    def transport(req, timeout):
        return _Resp(json.dumps({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                                 "usage": {}}).encode())

    with pytest.raises(ds.RemoteUnavailable):
        ds.chat("hello", registry=mr.DEFAULTS, transport=transport, key="k")
