"""Secrets canary — a standing guard that credentials never reach a sink the operator reads.

The threat this closes the CLASS of: apex forwards client auth headers verbatim to the upstream
(the passthrough auth model — core never stores or mints creds). That is safe today only because
NOTHING on the request path serializes a header value into a durable sink. The dangerous drift is
one line away: a future `logger.exception(exc)` / `logger.error(f"...{exc.request.headers}")` on the
upstream-failure branch would embed the httpx request — and `dict(exc.request.headers)` DOES expose
the real Bearer value (verified: str/repr/traceback of an httpx error redact headers, but the
request object on the exception does not). The telemetry jsonl is the specific sink that matters:
the TUI tail-reads it, so a secret written there is a secret shown on screen and persisted to disk.

So this test is a tripwire, not a today-bug reproduction. It drives BOTH handlers with a sentinel
secret in the auth header, through the success path AND a forced upstream error (the realistic leak
trigger), and asserts the sentinel appears in NONE of the operator-visible sinks: the emitted
telemetry line, the 502 body, captured `logging` records, or stderr. It goes red the day someone
pipes a header value into any of them. Same discipline as test_authority_defaults: close the class,
not the instance.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import httpx

from apex_router.proxy_engine.proxy.handlers import passthrough
from apex_router.proxy_engine.proxy.handlers import shadow as shadow_h
from apex_router.proxy_engine.telemetry.events import TelemetryWriter

# A value that would never occur legitimately in telemetry — if it shows up anywhere the operator
# reads, a credential leaked. Planted in EVERY credential header apex forwards (passthrough model):
# `authorization` (Bearer, the gateway/OpenAI) AND `x-api-key` (Anthropic direct) — a tripwire that only
# watches one header misses a leak of the other (Codex cross-validation, Finding 4).
SENTINEL = "SENTINEL-SECRET-b3ad1decafc0ffee-do-not-log"
AUTH_HEADER = ("authorization", f"Bearer {SENTINEL}")
APIKEY_HEADER = ("x-api-key", f"{SENTINEL}-apikey")
CRED_HEADERS = (AUTH_HEADER, APIKEY_HEADER)

SSE = b'data: {"message":{"usage":{"input_tokens":5}}}\n\n'


class _RespOK:
    status_code = 200

    def __init__(self):
        self.headers = httpx.Headers({"content-type": "text/event-stream", "x-model": "m"})

    async def aiter_raw(self):
        yield SSE

    async def aclose(self):
        pass


class _UpOK:
    def build_url(self, k, p, q):
        return "http://up" + p

    def endpoint_id(self, client_kind):
        return "anthropic"

    async def inject_auth(self, headers, client_kind, *, raw_headers=None):
        return headers  # injection disabled by default → passthrough no-op

    async def send_stream(self, m, u, *, headers, content):
        return _RespOK()


class _UpBoom:
    """Upstream that fails the forward with an httpx error CARRYING the request+auth header — the
    exact object a careless error-logger would serialize. The handler's except branch must not."""

    def build_url(self, k, p, q):
        return "http://up" + p

    def endpoint_id(self, client_kind):
        return "anthropic"

    async def inject_auth(self, headers, client_kind, *, raw_headers=None):
        return headers  # injection disabled by default → passthrough no-op

    async def send_stream(self, m, u, *, headers, content):
        # `headers` is the handler's fwd_headers — already a list[(bytes, bytes)] from
        # filter_request_headers (NOT str pairs). httpx.Request accepts bytes header pairs
        # directly; the constructed request then CARRIES the sentinel auth value, so
        # `dict(exc.request.headers)` would expose it — exactly the surface the canary guards.
        req = httpx.Request(m, u, headers=list(headers))
        raise httpx.ConnectError("upstream unreachable", request=req)


class _CapturingWriter(TelemetryWriter):
    """A real TelemetryWriter (writes the jsonl the TUI tail-reads) that also retains the emitted
    events so the test can re-serialize each and assert cleanliness at the object layer too."""

    def __init__(self, path):
        super().__init__(path)
        self.events = []

    def emit(self, event):
        self.events.append(event)
        super().emit(event)


class _URL:
    path = "/v1/messages"


class _Req:
    method = "POST"
    url = _URL()
    # The sentinel rides in BOTH the header map and the forwarded scope header list, across EVERY
    # credential header, so a sink dumping request.headers OR the filtered forward list is caught.
    headers = {
        "x-request-id": "r",
        "x-claude-code-session-id": "s",
        "x-claude-code-agent-id": "agent-9",
        **{k: v for k, v in CRED_HEADERS},
    }
    scope = {
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            *[(k.encode(), v.encode()) for k, v in CRED_HEADERS],
        ],
    }

    async def body(self):
        return b'{"model":"m","messages":[{"role":"user","content":"hi"}]}'


async def _drive_to_completion(handler_call, telemetry) -> bytes:
    """Run a handler, fully draining any streamed body. Returns the response body bytes (for the
    error path, that's the 502 payload; for the success path, the streamed SSE)."""
    resp = await handler_call(telemetry)
    body = b""
    if hasattr(resp, "body_iterator"):
        async for chunk in resp.body_iterator:
            body += chunk if isinstance(chunk, bytes) else chunk.encode()
    elif getattr(resp, "body", None):
        body = resp.body
    return body


def _sinks_for(tmp_path: Path):
    """A telemetry file + captured logging + captured stderr — every operator-visible sink."""
    tel = _CapturingWriter(tmp_path / "telemetry.jsonl")
    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    return tel, log_buf, handler, root, prev_level


def _assert_clean(*, telemetry_file: Path, resp_body: bytes, log_text: str,
                  stderr_text: str, stdout_text: str):
    haystacks = {
        "telemetry file": telemetry_file.read_text() if telemetry_file.exists() else "",
        "response body": resp_body.decode("utf-8", "replace"),
        "log output": log_text,
        "stderr": stderr_text,
        "stdout": stdout_text,
    }
    for where, text in haystacks.items():
        assert SENTINEL not in text, f"credential leaked into {where}: {text[:400]!r}"


def _run_case(tmp_path: Path, handler_call_factory) -> None:
    tel, log_buf, handler, root, prev_level = _sinks_for(tmp_path)
    err, out = io.StringIO(), io.StringIO()
    try:
        with redirect_stderr(err), redirect_stdout(out):
            asyncio.run(_run_both_paths(tel, handler_call_factory))
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)
    # every emitted event, serialized as it would be written, must also be clean
    for ev in tel.events:
        js = ev.to_json()
        assert SENTINEL not in js, f"credential leaked into a telemetry event: {js[:400]!r}"
    _assert_clean(
        telemetry_file=tel.path,
        resp_body=b"".join(getattr(tel, "_bodies", [])),
        log_text=log_buf.getvalue(),
        stderr_text=err.getvalue(),
        stdout_text=out.getvalue(),
    )


async def _run_both_paths(tel, handler_call_factory):
    bodies = []
    # success path
    bodies.append(await _drive_to_completion(handler_call_factory(_UpOK()), tel))
    # forced-upstream-error path (the realistic leak trigger)
    bodies.append(await _drive_to_completion(handler_call_factory(_UpBoom()), tel))
    tel._bodies = bodies


def test_passthrough_never_leaks_credential_to_any_sink(tmp_path):
    """Default-mode handler: sentinel auth header, success + forced upstream error, sinks clean."""
    _run_case(tmp_path, lambda up: lambda t: passthrough.handle(_Req(), up, t))


def test_shadow_never_leaks_credential_to_any_sink(tmp_path):
    """Shadow-mode handler (what runs during shadow week): same guard, incl. the body-parse path
    that now reads model_requested — a parse that stringifies the request must not surface auth."""
    _run_case(tmp_path, lambda up: lambda t: shadow_h.handle(_Req(), up, t, None))


def test_canary_would_fire_if_a_secret_were_written(tmp_path):
    """Meta-assertion: the guard is not vacuous. If ANY sink contained the sentinel, _assert_clean
    must raise — otherwise a real leak would pass silently. Checked for EACH sink independently, so
    a sink that silently stopped being inspected can't hide a leak."""
    clean = dict(telemetry_file=tmp_path / "absent.jsonl", resp_body=b"", log_text="",
                 stderr_text="", stdout_text="")
    leaky_file = tmp_path / "leaky.jsonl"
    leaky_file.write_text(json.dumps({"authorization": AUTH_HEADER[1]}) + "\n")
    per_sink_leaks = [
        {**clean, "telemetry_file": leaky_file},
        {**clean, "resp_body": f"boom {SENTINEL}".encode()},
        {**clean, "log_text": f"error: {SENTINEL}"},
        {**clean, "stderr_text": f"traceback {SENTINEL}"},
        {**clean, "stdout_text": f"print {SENTINEL}"},
    ]
    for leak in per_sink_leaks:
        raised = False
        try:
            _assert_clean(**leak)
        except AssertionError:
            raised = True
        assert raised, f"canary is vacuous for a sink — a leak into {leak} would pass silently"
