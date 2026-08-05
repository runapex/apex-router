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


class UsageScanner:
    """Tee an SSE response through here: `feed(chunk)` per forwarded chunk, read `usage` at the end.

    Line-buffered: SSE frames are `\\n`-delimited `field: value` lines. We accumulate decoded text,
    split on newlines, and parse any `data:` payload as JSON, pulling usage from whichever wire
    shape matches. Partial trailing lines are held until the next chunk completes them. Once input
    are captured we keep scanning only for the output-token final delta (cheap). Bounded memory: the
    text buffer holds at most one incomplete line."""

    def __init__(self, content_encoding: str = "") -> None:
        self._decoder = _Decoder(content_encoding)
        self._buf = ""
        self.usage = ProviderUsage()

    def feed(self, chunk: bytes) -> None:
        """Consume a copy of one forwarded chunk. Never raises (fail-open)."""
        try:
            text = self._decoder.feed(chunk)
            if not text:
                return
            self._buf += text.decode("utf-8", "ignore")
            # process complete lines; keep the last partial fragment buffered
            *lines, self._buf = self._buf.split("\n")
            for line in lines:
                self._scan_line(line)
        except Exception:  # noqa: BLE001 — capture must never break the data plane
            return

    def _scan_line(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("data:"):
            return
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
