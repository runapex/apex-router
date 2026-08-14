"""Shadow mode (M6b Stage A) — the compute-and-log pipeline, usage capture, and plane cleanliness.

The properties shadow mode must hold before it goes on the wire:
  - passthrough emission: the forwarded body/response bytes are byte-identical to the input
    (asserted end-to-end in the /tmp shadow_e2e drive; units here cover the compute + capture);
  - the (X, y) pair for R1 is present from request one: `bytes_by_class` (whole-request) + captured
    `usage.input_tokens`;
  - byte-only on the hot path: no tuner/tokenizer import reachable from apex_router.proxy_engine.pipeline.shadow /
    apex_router.proxy_engine.proxy.usage (plane separation);
  - fail-open for block-side doubt (unparseable body → empty report, never an exception);
  - fail-closed for authority-side doubt (an invalid policy bundle refuses to load).
"""
from __future__ import annotations

import json

import pytest

from apex_router.proxy_engine.pipeline.shadow import decompose, run_shadow
from apex_router.proxy_engine.proxy.usage import UsageScanner


def _req_body(messages: list[dict], model: str = "claude-opus-4-8") -> bytes:
    return json.dumps({"model": model, "messages": messages}).encode("utf-8")


# --- decompose: block extraction + frontier -------------------------------------------------------


def test_decompose_splits_all_and_frontier():
    body = _req_body([
        {"role": "user", "content": "first turn"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": [
            {"type": "text", "text": "newest turn"},
            {"type": "tool_result", "content": '[{"a":1},{"b":2}]'},
        ]},
    ])
    all_blocks, frontier = decompose(body)
    assert len(all_blocks) == 4  # 2 scalar + 2 in last message
    assert len(frontier) == 2  # only the last message's blocks
    assert frontier[-1].content_class == "json"  # the tool_result parses as JSON


def test_decompose_fail_open_on_garbage():
    assert decompose(b"not json at all") == ([], [])
    assert decompose(b'{"no":"messages"}') == ([], [])
    assert decompose(b"") == ([], [])


def test_decompose_includes_anthropic_system_in_X_not_frontier():
    # cross-validation: the top-level `system` field is billed input (R1's X) but is NOT the addressable
    # frontier (it's cached prefix). It must appear in all_blocks, never in frontier.
    body = json.dumps({
        "model": "claude-opus-4-8",
        "system": "You are a careful assistant. " * 20,
        "messages": [{"role": "user", "content": "newest turn text"}],
    }).encode()
    all_blocks, frontier = decompose(body)
    assert any(b.tool_name == "system" for b in all_blocks)  # system billed → in X
    assert all(b.tool_name != "system" for b in frontier)  # but never in the frontier
    assert len(frontier) == 1 and frontier[0].text == "newest turn text"


def test_decompose_system_as_block_list():
    body = json.dumps({
        "system": [{"type": "text", "text": "stable core prompt " * 10}],
        "messages": [{"role": "user", "content": "q"}],
    }).encode()
    all_blocks, _ = decompose(body)
    assert sum(1 for b in all_blocks if "stable core" in b.text) == 1


def test_decompose_openai_responses_input():
    # cross-validation: the OpenAI Responses API puts the turn under `input`, not `messages`.
    body = json.dumps({"model": "gpt-x", "input": "summarize the following data set"}).encode()
    all_blocks, frontier = decompose(body)
    assert len(frontier) == 1 and "summarize" in frontier[0].text
    assert frontier == all_blocks  # input is the whole request here


def test_decompose_includes_tool_schemas_in_X_not_frontier():
    # cross-validation (v2): top-level `tools` are billed input (render order tools→system→messages) — they
    # belong in X (bytes_by_class) but never the frontier (stable prefix, not the addressable turn).
    body = json.dumps({
        "model": "claude-opus-4-8",
        "tools": [{"name": "get_weather", "description": "…", "input_schema": {"type": "object"}},
                  {"name": "search", "description": "…" * 50, "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    all_blocks, frontier = decompose(body)
    tool_blocks = [b for b in all_blocks if b.tool_name == "tools"]
    assert len(tool_blocks) == 2  # both schemas counted as billed input
    assert all(b.tool_name != "tools" for b in frontier)  # never the frontier
    assert frontier[0].text == "hi"


def test_decompose_responses_structured_input_blocks():
    # cross-validation: Responses `input` can be structured items with input_text content — must flatten to
    # the text, not serialize the wrapper (which would misclassify prose as json).
    body = json.dumps({
        "model": "gpt-x",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "please analyze this prose paragraph"}]},
        ],
    }).encode()
    _all, frontier = decompose(body)
    assert len(frontier) == 1
    assert frontier[0].text == "please analyze this prose paragraph"
    assert frontier[0].content_class == "prose"  # NOT json (wrapper would have classified json)


# --- _requested_model: total fail-open, byte-safe model extraction --------------------------


def test_requested_model_extracts_and_is_totally_fail_open():
    from apex_router.proxy_engine.proxy.handlers.shadow import _requested_model

    # the happy path — same `model` key on both wires
    assert _requested_model(b'{"model":"claude-opus-4-8","messages":[]}') == "claude-opus-4-8"
    assert _requested_model(b'{"model":"gpt-5.6","input":"hi"}') == "gpt-5.6"
    # every malformed / adversarial input yields None (absent), never raises, never mutates:
    for bad in (
        b"not json at all",
        b"",
        b'{"no_model":1}',
        b'{"model":123}',        # non-string model
        b'{"model":""}',         # blank
        b'{"model":null}',
        b"[1,2,3]",              # non-dict top level
        b"\xff\xfe garbage",     # invalid utf-8
    ):
        before = bytes(bad)
        assert _requested_model(bad) is None
        assert bad == before  # byte-identity: the forwarded body is never touched


def test_requested_model_fails_open_on_recursion_bomb():
    # Codex cross-validation lead (the reference window): a deeply-nested body makes json.loads raise
    # RecursionError — a RuntimeError, NOT a ValueError/TypeError. The helper runs on the hot path
    # BEFORE the shadow-compute try/except, so a raise here would break the live request. It must
    # catch broadly and return None (the handler's "a parse failure drops the prediction, never the
    # request" contract). Pin it: a 50k-deep array/object → None, not a crash.
    from apex_router.proxy_engine.proxy.handlers.shadow import _requested_model

    for bomb in (("[" * 50000 + "0" + "]" * 50000).encode(),
                 ('{"a":' * 50000 + "0" + "}" * 50000).encode()):
        before = bytes(bomb)
        assert _requested_model(bomb) is None  # fails open, does not raise
        assert bomb == before


# --- run_shadow: byte-only report, R1's X present even with no policy ------------------------


def test_shadow_report_has_bytes_by_class_without_policy():
    body = _req_body([{"role": "user", "content": [
        {"type": "text", "text": "hello world prose that is reasonably long"},
        {"type": "tool_result", "content": json.dumps([{"id": i} for i in range(50)])},
    ]}])
    rep = run_shadow(body, None)  # no signed bundle yet
    assert rep.has_policy is False
    assert rep.n_blocks == 2
    assert sum(rep.bytes_by_class.values()) == rep.context_bytes > 0  # R1's X, from request one
    assert "json" in rep.bytes_by_class
    # every frontier block decided raw (no policy) but is still logged
    assert all(b.reason == "no_policy" and b.bytes_saved == 0 for b in rep.blocks)


def test_shadow_predicts_bytes_saved_under_enabled_policy(tmp_path):
    # a policy where json/xl compaction is admitted — build it from a corpus of big minifiable json
    from apex_router.proxy_engine.tuner.compiler import compile_policy
    from apex_router.proxy_engine.tuner.replay import Request

    # indented → compaction shrinks it; large → xl stratum (>128 KB). Item count kept so the request
    # BODY stays UNDER OVERSIZE_FRONTIER_BYTES (544 KB) — above it run_shadow skips decompose
    # (the observation budget), which is a separate path from this prediction test.
    big = json.dumps([{"id": i, "name": f"item{i}", "vals": list(range(10))} for i in range(2200)],
                     indent=2)
    corpus, prev = [], ""
    for t in range(6):
        content = (prev + big).encode("utf-8")
        corpus.append(Request(session_id="s1", content=content, tokens=max(1, len(content) // 4),
                              ts=float(t), model="claude-opus-4-8"))
        prev = content.decode() + "\n"
    policy = compile_policy(corpus, version=1, compiled_at=1e9).policy
    if not policy.has_active_policy():
        pytest.skip("synthetic corpus admitted no cell on this build — covered by real-corpus e2e")
    body = _req_body([{"role": "user", "content": [{"type": "tool_result", "content": big}]}])
    rep = run_shadow(body, policy)
    assert rep.has_policy
    # the frontier json block should route to an enabled cell and predict a positive byte saving
    jb = [b for b in rep.blocks if b.content_class == "json"]
    assert jb and jb[0].transform == "compaction"


# --- UsageScanner: Anthropic + OpenAI wire shapes, tee-safety --------------------------------


def test_usage_scanner_anthropic_message_start():
    scanner = UsageScanner("")  # identity encoding
    sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":12345,'
        b'"cache_read_input_tokens":10000,"cache_creation_input_tokens":200,"output_tokens":1}}}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":678}}\n\n'
    )
    # feed in two arbitrary splits to exercise line-buffering across chunk boundaries
    scanner.feed(sse[:120])
    scanner.feed(sse[120:])
    assert scanner.usage.captured
    assert scanner.usage.input_tokens == 12345
    assert scanner.usage.cache_read_tokens == 10000
    assert scanner.usage.cache_creation_tokens == 200
    assert scanner.usage.output_tokens == 678  # final delta wins


def test_usage_scanner_openai_terminal_usage():
    scanner = UsageScanner("")
    sse = (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b'data: {"usage":{"prompt_tokens":999,"completion_tokens":50,'
        b'"prompt_tokens_details":{"cached_tokens":800}}}\n\n'
        b'data: [DONE]\n\n'
    )
    scanner.feed(sse)
    assert scanner.usage.captured
    assert scanner.usage.input_tokens == 999
    assert scanner.usage.cache_read_tokens == 800
    assert scanner.usage.output_tokens == 50


def test_usage_scanner_cache_fields_distinct_for_read_write_ratio():
    # The read:write ratio the [6:1,30:1] calibration band brackets is the FIRST live measurement
    # shadow yields. It requires cache_read and cache_creation captured as DISTINCT fields, never
    # folded into a total. Pin that: read=48000, creation=2400 → 20:1, inside the band.
    scanner = UsageScanner("")
    scanner.feed(
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":1200,'
        b'"cache_read_input_tokens":48000,"cache_creation_input_tokens":2400,"output_tokens":5}}}\n\n'
    )
    u = scanner.usage
    assert u.cache_read_tokens == 48000  # reads — distinct field
    assert u.cache_creation_tokens == 2400  # writes — distinct field
    assert u.input_tokens == 1200  # fresh (uncached) input — a third distinct field
    # the ratio is derivable per-request; a week of these collapses the band to a measured point
    assert u.cache_read_tokens / u.cache_creation_tokens == 20.0


def test_usage_scanner_openai_responses_nested():
    # cross-validation: the Responses API nests usage under `response` with input/output_tokens.
    scanner = UsageScanner("")
    sse = (
        b'data: {"type":"response.completed","response":{"usage":'
        b'{"input_tokens":777,"output_tokens":88}}}\n\n'
    )
    scanner.feed(sse)
    assert scanner.usage.captured
    assert scanner.usage.input_tokens == 777
    assert scanner.usage.output_tokens == 88


def test_usage_scanner_responses_cached_tokens():
    # cross-validation: Responses usage carries cache reads under input_tokens_details.cached_tokens (NOT
    # cache_read_input_tokens). Routing it through the Anthropic branch dropped it → read:write
    # silently 0 for Codex Responses. The three-way _wire_of routing must capture it.
    scanner = UsageScanner("")
    scanner.feed(
        b'data: {"type":"response.completed","response":{"usage":{"input_tokens":900,'
        b'"input_tokens_details":{"cached_tokens":700},"output_tokens":40}}}\n\n'
    )
    u = scanner.usage
    assert u.captured and u.input_tokens == 900
    assert u.cache_read_tokens == 700  # F3: was 0 before the fix
    assert u.output_tokens == 40


def test_usage_scanner_fail_open_on_undecodable_encoding():
    # zstd is the documented RESIDUAL — no decoder, so capture stays disabled (honest absence). Now
    # the negative-encoding control; brotli moved to a positive control below (it IS decoded).
    scanner = UsageScanner("zstd")
    scanner.feed(b"\x28\xb5\x2f\xfd some zstd-looking bytes")
    assert scanner.usage.captured is False  # honest absence, not a guess


def test_usage_scanner_gzip_roundtrip():
    # NEGATIVE control for the brotli change: gzip decoding must be UNCHANGED (no regression).
    import zlib
    payload = (b'data: {"message":{"usage":{"input_tokens":42}}}\n\n')
    co = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)  # gzip
    gz = co.compress(payload) + co.flush()
    scanner = UsageScanner("gzip")
    scanner.feed(gz)
    assert scanner.usage.captured and scanner.usage.input_tokens == 42


def test_usage_scanner_brotli_roundtrip():
    # POSITIVE control for the brotli fix (the reference window 3-day mine root-cause): a br-encoded Anthropic
    # message_start, fed in two splits, must now DECODE and capture — the previously-censored path.
    brotli = pytest.importorskip("brotli")
    payload = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":31337,'
        b'"cache_read_input_tokens":30000,"cache_creation_input_tokens":300,"output_tokens":2}}}\n\n'
    )
    br = brotli.compress(payload)
    scanner = UsageScanner("br")
    half = len(br) // 2
    scanner.feed(br[:half])  # split mid-stream to exercise incremental brotli + line-buffering
    scanner.feed(br[half:])
    assert scanner.usage.captured  # was False before the fix (silent censoring)
    assert scanner.usage.input_tokens == 31337
    assert scanner.usage.cache_read_tokens == 30000
    assert scanner.usage.cache_creation_tokens == 300


def test_usage_scanner_brotli_absent_degrades_to_no_capture(monkeypatch):
    # Fail-safe: if the brotli package is unavailable at runtime, `br` must degrade to no-capture
    # (exactly the pre-fix behavior) and NEVER raise — the fix must not make brotli a hard
    # dependency of the data plane. Simulate absence by blocking the import.
    import builtins
    real_import = builtins.__import__

    def _no_brotli(name, *a, **k):
        if name == "brotli":
            raise ImportError("simulated: brotli not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_brotli)
    scanner = UsageScanner("br")  # __init__ tries `import brotli` → blocked → self-disables
    scanner.feed(b"\xce\xb2\x00 brotli bytes that will never be decoded")
    assert scanner.usage.captured is False  # honest absence, no exception


# --- Step 2: telemetry contract fields (TUI data source) -----------------------------------------


def test_telemetry_line_carries_schema_version_and_new_fields():
    from apex_router.proxy_engine.telemetry.events import TELEMETRY_SCHEMA_VERSION, TelemetryEvent

    e = TelemetryEvent.start(apex_version="0.0.1", client="claude-code")
    d = json.loads(e.to_json())
    # every line is schema-versioned (the TUI keys off it, refuses on unknown)
    assert d["schema_version"] == TELEMETRY_SCHEMA_VERSION
    # the fields the perf/optim panels need, all present with fail-safe defaults
    assert "agent_id" in d and d["agent_id"] is None
    assert d["matcher_event"] == "unwired"  # matcher not yet on the shadow path — placeholder
    assert d["t_upstream_ttfb_ms"] == 0.0  # distinct from ttft_ms


def test_heartbeat_line_shape():
    import tempfile
    from pathlib import Path

    from apex_router.proxy_engine.telemetry.events import TELEMETRY_SCHEMA_VERSION, TelemetryWriter

    with tempfile.TemporaryDirectory() as td:
        w = TelemetryWriter(Path(td) / "t.jsonl")
        w.requests = 42
        w.errors = 1
        w.heartbeat()
        line = json.loads((Path(td) / "t.jsonl").read_text().strip())
        assert line["ev"] == "hb"  # distinguishable from a request line
        assert line["schema_version"] == TELEMETRY_SCHEMA_VERSION
        assert line["requests"] == 42 and line["errors"] == 1


def test_both_handlers_populate_agent_id_and_upstream_ttfb():
    # cross-validation/F2 on Step 2: agent_id + t_upstream_ttfb_ms are handler-agnostic contract fields, so
    # BOTH passthrough (default mode) and shadow must capture them — a contract one handler silently
    # violates isn't hardened. Drive each handler against a mock upstream, assert the fields land.
    import asyncio

    import httpx

    from apex_router.proxy_engine.proxy.handlers import passthrough
    from apex_router.proxy_engine.proxy.handlers import shadow as shadow_h

    SSE = b'data: {"message":{"usage":{"input_tokens":5}}}\n\n'

    class _Resp:
        def __init__(self):
            self.status_code = 200
            self.headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_raw(self):
            yield SSE

        async def aclose(self):
            pass

    class _Up:
        def build_url(self, k, p, q):
            return "http://up" + p

        def endpoint_id(self, client_kind):
            return "anthropic"

        async def inject_auth(self, headers, client_kind, *, raw_headers=None):
            return headers  # injection disabled by default → passthrough no-op

        async def send_stream(self, m, u, *, headers, content):
            return _Resp()

    class _Tel:
        def __init__(self):
            self.ev = []

        def emit(self, e):
            self.ev.append(e)

    class _URL:
        path = "/v1/messages"

    class _Req:
        method = "POST"
        url = _URL()
        headers = {"x-request-id": "r", "x-claude-code-session-id": "s",
                   "x-claude-code-agent-id": "agent-9"}
        scope = {"raw_path": b"/v1/messages", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")]}

        async def body(self):
            return b'{"model":"m","messages":[{"role":"user","content":"hi"}]}'

    async def _drive(handler_call):
        tel = _Tel()
        resp = await handler_call(tel)
        async for _ in resp.body_iterator:
            pass
        return tel.ev[0]

    # passthrough (default mode)
    ev_pt = asyncio.run(_drive(lambda t: passthrough.handle(_Req(), _Up(), t)))
    assert ev_pt.agent_id == "agent-9"  # cross-validation: was null in passthrough
    assert ev_pt.t_upstream_ttfb_ms >= 0.0  # cross-validation: recorded, not left 0-by-omission
    assert ev_pt.endpoint_id == "anthropic"  # derived from upstream, handler-agnostic contract field
    # shadow mode
    ev_sh = asyncio.run(_drive(lambda t: shadow_h.handle(_Req(), _Up(), t, None)))
    assert ev_sh.agent_id == "agent-9"
    assert ev_sh.endpoint_id == "anthropic"  # both handlers set it (the agent_id lesson: no drift)


def test_endpoint_id_follows_the_resolved_upstream_not_a_constant():
    # Codex cross-validation (the reference window): a static endpoint_id="anthropic" mislabels codex rows,
    # which route to openai_upstream, not the gateway. The label must be DERIVED from the same
    # client→upstream routing as base_for. Pin both wires so a regression to a constant fails here.
    from apex_router.proxy_engine.config import CONFIG
    from apex_router.proxy_engine.proxy.upstream import Upstream

    up = Upstream(CONFIG)
    try:
        assert up.endpoint_id("claude-code") == "anthropic"  # Anthropic wire → the gateway/the gateway
        assert up.endpoint_id("codex") == "openai"  # OpenAI wire → api.openai.com, NOT the gateway
        assert up.endpoint_id("claude-code") != up.endpoint_id("codex")  # not a constant
    finally:
        import asyncio
        asyncio.run(up.aclose())


def test_rotation_renames_at_threshold():
    import tempfile
    from pathlib import Path

    from apex_router.proxy_engine.telemetry.events import TelemetryWriter

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.jsonl"
        w = TelemetryWriter(p)
        w.ROTATE_BYTES = 100  # tiny threshold for the test
        p.write_text("x" * 200)  # exceed it
        w.rotate_if_large()
        assert not p.exists() and (Path(td) / "t.jsonl.1").exists()  # renamed aside; fresh next


# --- plane cleanliness: shadow + usage never import the tuner -------------------------------------


def test_shadow_modules_are_plane_clean():
    import ast
    import pathlib

    # locate the package source relative to this test (tests/proxy_engine/ -> src/apex_router/proxy_engine)
    pkg = pathlib.Path(__file__).resolve().parents[2] / "src" / "apex_router" / "proxy_engine"
    for rel in ("pipeline/shadow.py", "proxy/usage.py", "proxy/handlers/shadow.py"):
        src = (pkg / rel).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # hot-path modules must not reach the offline tuner plane
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "proxy_engine.tuner" not in node.module, f"{rel} imports {node.module}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "proxy_engine.tuner" not in alias.name, f"{rel} imports {alias.name}"
