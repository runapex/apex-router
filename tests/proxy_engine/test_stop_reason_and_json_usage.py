"""Items 2 + 3 (2026-10-12):
  2. record the provider's stop reason and a refusal flag (telemetry v12). Before: no stop field at all, so 9
     cyber refusals ($20.88) were invisible to the proxy and counted as ordinary successful requests.
  3. capture usage from a NON-SSE JSON body (non-streamed chat completions, e.g. ollama). Before: the scanner
     only read `data:` lines, so the local proxy captured usage on 393 of 4,011 qwen rows.
Fixtures use the documented wire shapes; the ollama ones are copied from a live ollama 0.x response."""
from __future__ import annotations

import json

from apex_router.proxy_engine.proxy.usage import UsageScanner


def _sse(*objs, events=None):
    out = []
    for i, o in enumerate(objs):
        if events:
            out.append(f"event: {events[i]}\n")
        out.append("data: " + json.dumps(o) + "\n\n")
    return "".join(out).encode()


def _scan(body: bytes, chunks=3):
    s = UsageScanner("")
    step = max(1, len(body) // chunks)
    for i in range(0, len(body), step):
        s.feed(body[i:i + step])            # split mid-line on purpose
    s.finish()
    return s


# ---- 3. usage from a plain JSON body ----
OLLAMA_JSON = {"id": "chatcmpl-851", "object": "chat.completion", "model": "qwen2.5:3b",
               "choices": [{"index": 0, "message": {"role": "assistant", "content": "Ok"}, "finish_reason": "length"}],
               "usage": {"prompt_tokens": 31, "completion_tokens": 5, "total_tokens": 36}}


def test_non_stream_json_usage_is_captured():
    s = _scan(json.dumps(OLLAMA_JSON).encode())
    assert s.usage.captured and (s.usage.input_tokens, s.usage.output_tokens) == (31, 5)
    assert s.stop_reason == "length" and s.refusal is False


def test_pretty_printed_json_body_is_captured():
    s = _scan(json.dumps(OLLAMA_JSON, indent=2).encode(), chunks=7)
    assert s.usage.captured and s.usage.input_tokens == 31


def test_json_body_over_cap_is_skipped_not_buffered():
    big = dict(OLLAMA_JSON, pad="x" * (UsageScanner.JSON_BODY_CAP + 10))
    s = _scan(json.dumps(big).encode(), chunks=50)
    assert not s.usage.captured                      # bounded memory beats capture on a pathological body


def test_finish_is_idempotent_and_safe_on_garbage():
    s = UsageScanner(""); s.feed(b"not json at all"); s.finish(); s.finish()
    assert not s.usage.captured and s.stop_reason is None


def test_sse_stream_still_works_and_finish_does_not_double_count():
    body = _sse({"choices": [{"delta": {"content": "Ok"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 31, "completion_tokens": 10}}) + b"data: [DONE]\n\n"
    s = _scan(body)
    assert (s.usage.input_tokens, s.usage.output_tokens) == (31, 10) and s.stop_reason == "stop"


# ---- 2. stop reason + refusal on each wire ----
def test_anthropic_stream_refusal():
    body = _sse({"type": "message_start", "message": {"usage": {"input_tokens": 2, "cache_read_input_tokens": 9, "output_tokens": 1}}},
                {"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_sequence": None}, "usage": {"output_tokens": 7}},
                {"type": "message_stop"}, events=["message_start", "message_delta", "message_stop"])
    s = _scan(body)
    assert s.stop_reason == "refusal" and s.refusal is True and s.usage.output_tokens == 7


def test_anthropic_stream_normal_end_turn():
    body = _sse({"type": "message_start", "message": {"usage": {"input_tokens": 2}}},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}})
    s = _scan(body)
    assert s.stop_reason == "tool_use" and s.refusal is False


def test_anthropic_non_stream_body():
    s = _scan(json.dumps({"type": "message", "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 4}}).encode())
    assert s.stop_reason == "end_turn" and s.usage.captured


def test_chat_content_filter_and_refusal_field_are_refusals():
    s = _scan(_sse({"choices": [{"delta": {}, "finish_reason": "content_filter"}]}))
    assert s.stop_reason == "content_filter" and s.refusal is True
    s = _scan(_sse({"choices": [{"delta": {"refusal": "I can't help with that."}, "finish_reason": None}]},
                   {"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    assert s.stop_reason == "stop" and s.refusal is True


def test_responses_completed_and_incomplete():
    done = {"type": "response.completed", "response": {"status": "completed", "incomplete_details": None,
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 31, "output_tokens": 14, "input_tokens_details": {"cached_tokens": 0}}}}
    s = _scan(_sse(done, events=["response.completed"]))
    assert s.stop_reason == "completed" and s.refusal is False and s.usage.input_tokens == 31
    inc = {"type": "response.incomplete", "response": {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}}
    s = _scan(_sse(inc))
    assert s.stop_reason == "incomplete:content_filter" and s.refusal is True


def test_responses_refusal_content_part():
    done = {"type": "response.completed", "response": {"status": "completed",
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}}
    s = _scan(_sse(done))
    assert s.refusal is True


def test_stop_reason_is_bounded_and_scalar():
    s = _scan(_sse({"choices": [{"delta": {}, "finish_reason": "x" * 500}]}))
    assert isinstance(s.stop_reason, str) and len(s.stop_reason) <= 64
    s = _scan(_sse({"choices": [{"delta": {}, "finish_reason": {"weird": 1}}]}))
    assert s.stop_reason is None                          # non-string values are ignored, never stored raw


# ---- telemetry contract ----
def test_event_carries_v12_fields():
    from apex_router.proxy_engine.readout.doctor import SUPPORTED_SCHEMA
    from apex_router.proxy_engine.telemetry.events import TELEMETRY_SCHEMA_VERSION, TelemetryEvent
    assert TELEMETRY_SCHEMA_VERSION == 12 and 12 in SUPPORTED_SCHEMA
    fields = TelemetryEvent.__dataclass_fields__
    assert "stop_reason" in fields and "refusal" in fields


# ---- doctor surfaces refusals (they are HTTP 200, so the error panel never shows them) ----
def _row(i, **kw):
    r = {"ts": 1000.0 + i, "schema_version": 12, "client": "claude-code", "endpoint_id": "anthropic",
         "model_resolved": "it-entra-claude-opus-5-5", "session_id": "s1", "agent_id": None, "is_error": False,
         "tokens_in": 2, "tokens_out": 10, "cache_read_tokens": 1000, "cache_write_tokens": 0,
         "usage": {"captured": True, "input_tokens": 2, "cache_read_tokens": 1000, "cache_creation_tokens": 0, "output_tokens": 10},
         "stop_reason": "end_turn", "refusal": False}
    r.update(kw); return r


def test_doctor_report_counts_and_prices_refusals():
    from apex_router.proxy_engine.readout.doctor import build_report, format_report
    rows = [_row(i) for i in range(8)] + [_row(10, stop_reason="refusal", refusal=True), _row(11, stop_reason="refusal", refusal=True)]
    rep = build_report(rows)
    rf = rep["refusals"]
    assert rf["total"] == 2 and rf["observed_rows"] == 10 and rf["by_model"] == {"it-entra-claude-opus-5-5": 2}
    assert rf["dollars"] > 0 and rf["stop_reasons"]["end_turn"] == 8
    assert "REFUSALS" in format_report(rep)


def test_doctor_refusal_panel_absent_on_pre_v12_rows():
    from apex_router.proxy_engine.readout.doctor import build_report, format_report
    rows = [dict(_row(i), schema_version=11) for i in range(5)]
    for r in rows:
        r.pop("stop_reason"); r.pop("refusal")
    rep = build_report(rows)
    assert rep["refusals"]["observed_rows"] == 0 and "REFUSALS" not in format_report(rep)


# ---- cross-validation pass 1 findings (2026-10-12) ----
def test_invalid_utf8_does_not_grow_the_raw_buffer():
    s = UsageScanner("")
    for _ in range(5000):
        s.feed(b"\xff")
    assert len(s._raw) <= 1


def test_non_stream_responses_body_stop_and_refusal():
    body = {"object": "response", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 0}}}
    s = _scan(json.dumps(body).encode())
    assert s.usage.captured and s.stop_reason == "completed" and s.refusal is True
    inc = {"object": "response", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}
    s = _scan(json.dumps(inc).encode())
    assert s.stop_reason == "incomplete:max_output_tokens" and s.refusal is False


def test_doctor_refusal_dollars_carry_their_regimes():
    from apex_router.proxy_engine.readout.doctor import build_report, format_report
    rows = [_row(i) for i in range(3)] + [_row(10, stop_reason="refusal", refusal=True)]
    rep = build_report(rows)
    assert rep["refusals"]["regimes"] and all(k.split(":")[0] in ("list", "provisional", "negotiated") for k in rep["refusals"]["regimes"])
    assert "(list)" not in format_report(rep)
