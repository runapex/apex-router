"""Provider-usage capture — parse `usage` out of a streamed response WITHOUT touching the bytes
forwarded to the client. R1's regressand **y**.

Shadow mode forwards the upstream response verbatim (`aiter_raw`, possibly gzip/brotli-encoded, to
preserve byte + cache identity). To calibrate the wire-usage regression (R1) from the first shadow
request, telemetry needs the provider's own token accounting — `usage.input_tokens` and the cache
split — which arrives INSIDE that streamed body. This scanner tees a COPY of each chunk into an
incremental decoder + SSE line parser and extracts the usage fields; the original chunk is yielded
onward unchanged. It never buffers the whole body, never re-encodes, and fails open (any decode or
parse error → no usage captured, bytes still flow).

Two wires, both SSE:
  - Anthropic (`/v1/messages`): `event: message_start` carries `message.usage.{input_tokens,
    cache_read_input_tokens,cache_creation_input_tokens,output_tokens}`; `event: message_delta`
    carries the final `usage.output_tokens`. Input/cache tokens are known at message_start (the
    first usable event — exactly what "from response 1" needs).
  - OpenAI/Codex (`/v1/chat/completions`, `/v1/responses`): a terminal chunk carries
    `usage.{prompt_tokens,completion_tokens,...}` (only when `stream_options.include_usage`); the
    scanner reads it if present and leaves the fields zero otherwise (honest absence, not a guess).
  - A NON-SSE JSON body (non-streamed chat completion, e.g. ollama): no `data:` lines at all. Up to
    JSON_BODY_CAP decoded bytes are kept and parsed once by `finish()` (v12; before, usage was lost).

Stop reason (v12): the provider's own terminal reason, stored as received (bounded string) — Anthropic
`stop_reason` (message_delta.delta / non-stream top level), chat `choices[].finish_reason`, Responses
`response.status` (+ `:incomplete_details.reason`). `refusal` is True on Anthropic `refusal`, chat
`content_filter` or a non-empty `refusal` field, a Responses `refusal` content part, or an incomplete
reason of `content_filter`. A refusal is an HTTP 200, so without this the proxy saw it as a success.

PLANE-CLEAN: stdlib + `apex_router.proxy_engine.telemetry` types only; no tuner, no tokenizer. Bytes are counted, never
tokenized — the usage NUMBERS come from the provider, which is the whole point (the wire is the
Claude-side token oracle the offline tiktoken proxy can't be).
"""
from __future__ import annotations

import json
import zlib
from dataclasses import dataclass


@dataclass
class ProviderUsage:
    """The provider's own token accounting for one response. Zeros mean 'not reported' (fail-open /
    usage not included), never 'measured zero' — a captured flag disambiguates."""

    captured: bool = False
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "captured": self.captured,
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "output_tokens": self.output_tokens,
        }


def _wire_of(u: dict) -> str:
    """Classify a usage dict's vocabulary by its field NAMES — the one routing contract (three-way,
    not a bool). OpenAI chat-completions names prompt/completion tokens; OpenAI Responses names
    input/output tokens BUT carry an `input_tokens_details` (Anthropic never does); everything else
    with input_tokens is Anthropic. Share the observable (the field vocabulary), don't bridge two
    wires that overlap on `input_tokens` with a fragile boolean (the Codex-F3 failure)."""
    if "prompt_tokens" in u or "completion_tokens" in u:
        return "chat"
    if "input_tokens_details" in u:
        return "responses"
    # Anthropic message_delta carries output_tokens alone; message_start carries input_tokens — both
    # are Anthropic. A Responses usage WITHOUT a details block also lands here and is read the same
    # way for input/output (only its cache field, which it then lacks, would differ) — safe.
    return "anthropic"


class _Decoder:
    """Incremental content-decoder for gzip/deflate/identity/brotli. `zstd` and any other unknown
    encoding still degrade to no-capture, honestly (the `content_encoding` telemetry field makes
    that visible rather than silent). Errors on a chunk stop decoding permanently (fail-open: the
    forwarded bytes are unaffected — this decoder only ever touches a COPY of the chunk).

    Brotli: the a measurement window) found ~16.7% of requests logged `usage=null`,
    root-caused to this class self-disabling on `br` because brotli wasn't installed — Claude Code
    negotiates `br` per pooled connection, so those responses were silently uncaptured.
    `brotli.Decompressor().process()` is the streaming analogue of zlib's
    `decompressobj().decompress()`. If the `brotli` package is absent at runtime, `br` degrades to
    no-capture exactly as before (import guarded, never raises)."""

    def __init__(self, content_encoding: str) -> None:
        enc = (content_encoding or "").lower().strip()
        self._broken = False
        self._br = None  # brotli.Decompressor when enc == br and the package is importable
        if enc in ("gzip", "x-gzip"):
            self._dec = zlib.decompressobj(zlib.MAX_WBITS | 16)
        elif enc == "deflate":
            self._dec = zlib.decompressobj()
        elif enc in ("", "identity"):
            self._dec = None  # passthrough
        elif enc == "br":
            self._dec = None
            try:
                import brotli  # optional dep; absent → degrade to no-capture (as before)

                self._br = brotli.Decompressor()
            except Exception:  # noqa: BLE001 — no brotli / init failure → honest no-capture
                self._broken = True
        else:  # zstd / unknown → cannot decode, disable capture (visible via content_encoding)
            self._dec = None
            self._broken = True

    def feed(self, chunk: bytes) -> bytes:
        if self._broken:
            return b""
        if self._br is not None:
            try:
                return self._br.process(chunk)
            except Exception:  # noqa: BLE001 — brotli error → stop decoding, forwarded bytes untouched
                self._broken = True
                return b""
        if self._dec is None:
            return chunk
        try:
            return self._dec.decompress(chunk)
        except (zlib.error, OSError):
            self._broken = True
            return b""


# Headers a provider uses to EXPLAIN a rejection — which limit, how long to wait, which request to
# quote in a support ticket. Captured verbatim on a >=400 so an operator can tell an input-token
# ceiling from a request-rate ceiling without reproducing the failure by hand. Exact names plus two
# families (Anthropic's `anthropic-ratelimit-*`, the OpenAI wire's `x-ratelimit-*`).
_ERROR_HEADER_NAMES = frozenset({"retry-after", "request-id", "x-request-id", "x-should-retry"})
_ERROR_HEADER_PREFIXES = ("anthropic-ratelimit-", "x-ratelimit-")

# Cap on the captured error body. A telemetry row must not balloon to the size of a hostile or merely
# verbose error page, and the data plane must not buffer one — the provider's `error.type` /
# `error.message` live in the first few hundred bytes of the envelope.
ERROR_BODY_CAP_BYTES = 512


def capture_error_headers(headers) -> dict[str, str]:
    """The rate-limit/retry headers from a >=400 response, lowercased. Fail-open: any iteration
    failure yields what was collected so far, never raises onto the data plane."""
    out: dict[str, str] = {}
    try:
        for k, v in headers.items():
            lk = k.lower()
            if lk in _ERROR_HEADER_NAMES or lk.startswith(_ERROR_HEADER_PREFIXES):
                out[lk] = v
    except Exception:  # noqa: BLE001 — capture must never break the data plane
        return out
    return out


class ErrorBodyScanner:
    """Tee a >=400 response body through here to capture the provider's own error envelope (v7).

    `UsageScanner` is deliberately blind to this: it looks only for `usage`, which a 4xx/5xx never
    carries, so every byte of the explanation was dropped. This scanner reuses the SAME `_Decoder`
    (the body is content-encoded like any other) and keeps a BOUNDED prefix — `ERROR_BODY_CAP_BYTES`
    decoded bytes, after which it stops accumulating and flags truncation.

    Identical fail-open contract to UsageScanner: it only ever sees a COPY of a forwarded chunk, and
    any decode failure yields an empty capture rather than disturbing the bytes going to the client.
    """

    def __init__(self, content_encoding: str = "", cap_bytes: int = ERROR_BODY_CAP_BYTES) -> None:
        self._decoder = _Decoder(content_encoding)
        self._cap = cap_bytes
        self._buf = bytearray()
        self._truncated = False

    def feed(self, chunk: bytes) -> None:
        """Consume a copy of one forwarded chunk. Never raises (fail-open)."""
        try:
            if not chunk:
                return
            if len(self._buf) >= self._cap:
                self._truncated = True
                return
            text = self._decoder.feed(chunk)
            if not text:
                return
            room = self._cap - len(self._buf)
            if len(text) > room:
                self._buf.extend(text[:room])
                self._truncated = True
            else:
                self._buf.extend(text)
        except Exception:  # noqa: BLE001 — capture must never break the data plane
            return

    def text(self) -> str:
        """The captured prefix as text, with an explicit truncation marker so a consumer never reads
        a clipped body as a complete one."""
        s = bytes(self._buf).decode("utf-8", "ignore")
        return s + "…[truncated]" if self._truncated else s


class UsageScanner:
    """Tee an SSE response through here: `feed(chunk)` per forwarded chunk, read `usage` at the end.

    Line-buffered: SSE frames are `\\n`-delimited `field: value` lines. We accumulate decoded text,
    split on newlines, and parse any `data:` payload as JSON, pulling usage from whichever wire
    shape matches. Partial trailing lines are held until the next chunk completes them. Once input
    are captured we keep scanning only for the output-token final delta (cheap). Bounded memory: the
    text buffer holds at most one incomplete line."""

    JSON_BODY_CAP = 1 << 20  # 1 MiB of decoded text kept for a non-SSE body; larger bodies are not parsed
    _STOP_MAX = 64

    def __init__(self, content_encoding: str = "") -> None:
        self._decoder = _Decoder(content_encoding)
        self._buf = ""
        self.usage = ProviderUsage()
        self.stop_reason: str | None = None
        self.refusal = False
        self._sse = False           # saw a `data:`/`event:` line -> SSE; stop keeping the raw body
        self._raw: list[str] = []   # decoded text of a non-SSE body, up to JSON_BODY_CAP
        self._raw_len = 0
        self._finished = False

    def feed(self, chunk: bytes) -> None:
        """Consume a copy of one forwarded chunk. Never raises (fail-open)."""
        try:
            text = self._decoder.feed(chunk)
            if not text:
                return
            decoded = text.decode("utf-8", "ignore")
            if not decoded:
                return
            if not self._sse:
                if self._raw_len + len(decoded) <= self.JSON_BODY_CAP:
                    self._raw.append(decoded)
                    self._raw_len += len(decoded)
                else:
                    self._raw, self._raw_len = [], self.JSON_BODY_CAP + 1  # over the cap: give up, free memory
            self._buf += decoded
            # process complete lines; keep the last partial fragment buffered
            *lines, self._buf = self._buf.split("\n")
            for line in lines:
                self._scan_line(line)
        except Exception:  # noqa: BLE001 — capture must never break the data plane
            return

    def finish(self) -> None:
        """End of body. For a non-SSE body, parse the kept text as ONE JSON document (usage + stop reason).
        Idempotent, never raises. Call before reading `usage` / `stop_reason`."""
        if self._finished:
            return
        self._finished = True
        try:
            if self._sse or self._raw_len > self.JSON_BODY_CAP or not self._raw:
                return
            text = "".join(self._raw).strip()
            self._raw = []
            if text.startswith("{"):
                obj = json.loads(text)
                if isinstance(obj, dict):
                    self._extract(obj)
        except Exception:  # noqa: BLE001 — capture must never break the data plane
            return

    def _set_stop(self, v) -> None:
        if isinstance(v, str) and v:
            self.stop_reason = v[: self._STOP_MAX]

    def _extract_stop(self, obj: dict) -> None:
        # Anthropic: non-stream top-level stop_reason; stream message_delta.delta.stop_reason.
        if obj.get("type") in ("message", "message_delta") or "stop_reason" in obj:
            self._set_stop(obj.get("stop_reason"))
        delta = obj.get("delta")
        if isinstance(delta, dict):
            self._set_stop(delta.get("stop_reason"))
        # OpenAI chat: choices[].finish_reason; refusal text in message/delta.refusal
        choices = obj.get("choices")
        if isinstance(choices, list):
            for ch in choices:
                if not isinstance(ch, dict):
                    continue
                self._set_stop(ch.get("finish_reason"))
                for k in ("delta", "message"):
                    part = ch.get(k)
                    if isinstance(part, dict) and part.get("refusal"):
                        self.refusal = True
        # OpenAI Responses: response.status (+ incomplete reason), refusal content parts. Streamed events nest
        # the response under `response`; a non-streamed body IS the response object (top level).
        resp = obj.get("response") if isinstance(obj.get("response"), dict) else (obj if obj.get("object") == "response" else None)
        if isinstance(resp, dict) and isinstance(resp.get("status"), str) and resp["status"] not in ("in_progress", "queued"):
            reason = (resp.get("incomplete_details") or {}).get("reason") if isinstance(resp.get("incomplete_details"), dict) else None
            self._set_stop(resp["status"] + (f":{reason}" if isinstance(reason, str) and reason else ""))
            if reason == "content_filter":
                self.refusal = True
            for item in resp.get("output") or []:
                for part in (item.get("content") or []) if isinstance(item, dict) else []:
                    if isinstance(part, dict) and part.get("type") == "refusal":
                        self.refusal = True
        if self.stop_reason in ("refusal", "content_filter"):
            self.refusal = True

    def _scan_line(self, line: str) -> None:
        line = line.strip()
        if line.startswith("event:"):
            self._sse = True
            self._raw, self._raw_len = [], 0
            return
        if not line.startswith("data:"):
            return
        if not self._sse:
            self._sse = True
            self._raw, self._raw_len = [], 0
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return
        if isinstance(obj, dict):
            self._extract(obj)

    def _extract(self, obj: dict) -> None:
        try:
            self._extract_stop(obj)
        except Exception:  # noqa: BLE001 — a stop-reason parse must never cost the usage capture
            pass
        # Anthropic: message_start → {message:{usage:{...}}}; message_delta → {usage:{...}} with
        # ONLY output_tokens. OpenAI: terminal {usage:{prompt_tokens,completion_tokens,...}}. Route
        # by the field VOCABULARY, not by presence of input_tokens — the Anthropic message_delta
        # carries output_tokens alone, so "no input_tokens ⇒ OpenAI" mis-routes it (the delta's
        # output_tokens would be read as an OpenAI field and dropped). A usage dict is OpenAI iff it
        # names prompt/completion tokens; otherwise it is Anthropic.
        msg = obj.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
            self._apply(msg["usage"], _wire_of(msg["usage"]))
        # OpenAI Responses API (`/v1/responses`): the terminal event nests usage under `response`.
        resp = obj.get("response")
        if isinstance(resp, dict) and isinstance(resp.get("usage"), dict):
            self._apply(resp["usage"], _wire_of(resp["usage"]))
        u = obj.get("usage")
        if isinstance(u, dict):
            self._apply(u, _wire_of(u))

    def _apply(self, u: dict, wire: str) -> None:
        # THREE usage vocabularies, routed by field names (never one bool — Responses shares
        # Anthropic's input/output_tokens but uses OpenAI's *_details.cached_tokens for cache,
        # so a two-way anthropic/openai flag drops Responses cache reads; cross-validation). See `_wire_of`.
        if wire == "anthropic":
            it = u.get("input_tokens")
            if it is not None:
                self.usage.input_tokens = int(it)
                self.usage.captured = True
            if u.get("cache_read_input_tokens") is not None:
                self.usage.cache_read_tokens = int(u["cache_read_input_tokens"])
            if u.get("cache_creation_input_tokens") is not None:
                self.usage.cache_creation_tokens = int(u["cache_creation_input_tokens"])
            if u.get("output_tokens") is not None:
                self.usage.output_tokens = int(u["output_tokens"])
        elif wire == "responses":  # OpenAI Responses: input/output_tokens + input_tokens_details
            it = u.get("input_tokens")
            if it is not None:
                self.usage.input_tokens = int(it)
                self.usage.captured = True
            details = u.get("input_tokens_details")
            if isinstance(details, dict) and details.get("cached_tokens") is not None:
                self.usage.cache_read_tokens = int(details["cached_tokens"])
            if u.get("output_tokens") is not None:
                self.usage.output_tokens = int(u["output_tokens"])
        else:  # "chat" — OpenAI chat-completions terminal usage
            pt = u.get("prompt_tokens")
            if pt is not None:
                self.usage.input_tokens = int(pt)
                self.usage.captured = True
            details = u.get("prompt_tokens_details")
            if isinstance(details, dict) and details.get("cached_tokens") is not None:
                self.usage.cache_read_tokens = int(details["cached_tokens"])
            if u.get("completion_tokens") is not None:
                self.usage.output_tokens = int(u["completion_tokens"])
