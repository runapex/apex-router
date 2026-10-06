"""Telemetry event — §3.3, one jsonl line per proxied request.

The `bust_cause` taxonomy (ttl / transform / client_edit / frontier_rerender) is what keeps
the M4 tripwire from auto-reverting an innocent knob after a cache miss the knob didn't cause
(round 1 TTL confound; round 2 frontier re-render). M0 emits the envelope with transforms=[]
and bust=False; later milestones fill the transform/guard/ccr fields.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

BustCause = Literal["none", "ttl", "transform", "client_edit", "frontier_rerender", "unknown"]
GuardAction = Literal["none", "fallback", "invalidate"]
# Matcher outcome per request. "unwired" = the matcher was not consulted for this row (no store
# on the handler path, or the fail-open guard dropped identification); any other value is a real
# §4 match event from the wired request path (session/wire.py). It documents the pre-wiring era.
# runs in the M0-derived identity layer, not decide()); the field exists so the TUI schema is total
# and shows "not identified" rather than a missing key. Real events (extend/new/client_edit/
# compaction) have landed since session/wire.py joined both handlers.
MatcherEvent = Literal["unwired", "extend", "new", "client_edit", "compaction", "error"]

# TELEMETRY_SCHEMA_VERSION — every emitted line carries this so a consumer can detect drift and, on
# a version it doesn't know, say so rather than guess field meanings. Bump on ANY change to the
# emitted shape (added/renamed/removed field, changed semantics), the same discipline as
# CLASSIFIER_VERSION / compiler_hash. (The Ink TUI that was the original strict consumer was removed
# from the product in c1c3835; the readout harness reads lines without a version gate, so a bump no
# longer rejects any live consumer — but the drift contract stands for whatever re-consumes these.)
# v2: added `endpoint_id` (where a wire was reached). The pre-shadow attribution field that lets
# shadow-era data be endpoint-attributed at birth (adding it later leaves the shadow week
# unattributable). NOT a constant — DERIVED from the resolved upstream by `Upstream.endpoint_id`
# (Codex cross-validation: codex traffic routes to openai, not anthropic, so a static "anthropic"
# mislabels it).
# v3: added `content_encoding` (the response's Content-Encoding, as seen by the usage scanner). The
# a measurement window) root-caused ~16.7% `usage=null` to the scanner self-disabling on
# undecodable encodings (brotli, uninstalled at the time); logging the encoding converts that from a
# silent, retro-unprovable gap into a monitored field — the discriminating instrument for the
# pre-registered brotli acceptance test (join content_encoding × usage-present on the next window).
# v4: added `upstream_error_wait_ms` AND changed `apex_added_ms` semantics on the upstream-raise
# error path. Pre-v4, a send_stream failure billed the whole elapsed wait (t0→raise) to
# apex_added_ms, so a 600s read-timeout read as 600s of apex latency (live finding the reference window:
# 42/127 errors mis-billed ~600_000ms). v4 bills apex only its own pre_forward cost and records the
# upstream wait-until-failure separately. Consumers pooling pre-v4 apex_added_ms across error rows
# will see an inflated tail; filter is_error or split on schema_version >= 4.
# v5: added `error_cause` — the DISCRIMINATING label for an errored row. Pre-v5, `is_error` was one
# undifferentiated bool: a read-timeout, a connection reset/pool-exhaustion, and an HTTP 5xx were
# indistinguishable, so a burst of failures couldn't be root-caused (live finding: a Sep-16/17 codex
# error cluster was mislabeled a "429 rate-limit storm" — the data literally could not confirm it was
# a 429 vs a connection-level raise). v5 records the exception class on the upstream-raise path
# (`ReadTimeout`/`ConnectError`/`PoolTimeout`/...) and `http_<status>` on a >=400 response, so an
# error's MECHANISM is provable from telemetry, not inferred. None on success rows.
# v6: added `matcher_error`, `connect_retries`, `connect_backoff_ms` + `matcher_event="error"` (xval
# improvement #3). Pre-v6, a matcher EXCEPTION (e.g. the state.db schema-drift outage: `no such
# column: sys_prompt_hash` on every request) was swallowed by fail-open and booked as the ambiguous
# `matcher_event="unwired"` — indistinguishable from 'matcher legitimately not consulted', so a total
# session-identity outage was INVISIBLE in telemetry. And connect-retry recoveries (v-nothing) left no
# trace: a request that succeeded only after N backoff sleeps looked identical to a clean one. v6
# records the matcher failure MECHANISM (`matcher_error` = exception class; `matcher_event="error"`)
# and the retry cost (`connect_retries` count + `connect_backoff_ms` apex-slept), so both a matcher
# outage and a flaky-upstream recovery are provable from telemetry, not silent.
# v7: added `error_detail` — the provider's OWN explanation of a rejection, the missing half of v5's
# `error_cause`. v5 made an error's mechanism provable (`http_429` vs `PoolTimeout` vs `ReadTimeout`);
# it did not make it ACTIONABLE. Live finding (2026-09-30): 247 rows logged `http_429` in one day, all
# of them the Claude Code auto-mode Bash-safety classifier, and telemetry could not say which ceiling
# was hit — an input-token-per-minute limit, a request-per-minute limit, and a subscription session
# cap are one indistinguishable label, and they have different fixes. The provider names it exactly,
# in the response body (`error.type`/`error.message`) and in `anthropic-ratelimit-*`/`retry-after` —
# both of which apex discarded, because `UsageScanner` tees the body only for `usage` and a 4xx/5xx
# carries none, so `captured` stayed False and every byte was dropped. v7 captures a BOUNDED prefix of
# that body plus the rate-limit/retry headers on any status >= 400 (None on clean rows), so a
# rate-limit cluster is diagnosable from the log instead of by reproducing it with curl. Same lesson
# as v3's `content_encoding`: an error you can see but not attribute is a measurement gap.
# v8: added `upstream_rejected` — True for ANY upstream response status >= 400 (4xx AND 5xx). It does
# NOT change `is_error`, which stays >=500-only BY DESIGN (a client-retried 429/4xx is not a proxy
# failure; the doctor error panel and the heartbeat `errors` counter are built on that meaning). Live
# finding (2026-10-02): 478 `http_429` rows in 7 days, every one `is_error=false`, countable only by
# string-matching `error_cause`. `upstream_rejected` makes "the provider said no" a first-class bool.
# False on success and on the upstream-RAISE path (no response ever existed there).
# Also v8 (semantics, no new field): `connect_retries` / `connect_backoff_ms` now also count read/TLS
# retries (SSLError/ReadError/WriteError/RemoteProtocolError before response headers) — fast-fail only
# (< APEX_RETRY_FAST_FAIL_MS, default 3000 ms) and on POST/GET completion endpoints only. A declined
# body-sent retry (slow failure or non-completion path) records `error_cause` as before.
# v9: added `cache_key_rewrite` — a row records when the proxy edited the forwarded body (the opt-in
# APEX_CODEX_CACHE_KEY stable prompt_cache_key; proxy.cache_key). None = forwarded byte-identical, so
# a with/without-rewrite cache-hit comparison is a filter, not an inference.
TELEMETRY_SCHEMA_VERSION = 9

# Default endpoint label. The handlers OVERRIDE this per request from `Upstream.endpoint_id(client)`
# (anthropic for the Anthropic wire, openai for codex) — this default is only the fallback for an
# event constructed without a handler (e.g. `start()` before routing). A real endpoints table
# (A-Proto) replaces the derivation with a lookup on the resolved endpoint profile.
ENDPOINT_ID = "anthropic"


@dataclass
class TransformRecord:
    name: str
    block_hash: str
    orig_tokens: int
    out_tokens: int
    fidelity_class: str
    offloaded_ms: float


@dataclass
class TelemetryEvent:
    # identity
    ts: float
    apex_version: str
    request_id: str | None
    session_id: str | None
    turn: int
    epoch_id: str | None
    client: str
    model_requested: str | None
    model_resolved: str | None
    stratum: str
    # endpoint the wire was reached through — set per request by the handler from the resolved
    # upstream (anthropic | openai). Attribution substrate: shadow-era data is endpoint-labeled from
    # line one, so a later multi-endpoint world can bucket it without re-collecting. See ENDPOINT_ID
    # for the derivation.
    endpoint_id: str = ENDPOINT_ID
    # schema version — first field a consumer checks (TUI refuses on an unknown version)
    schema_version: int = TELEMETRY_SCHEMA_VERSION
    # agent_id — sub-agent (Task) requests carry `x-claude-code-agent-id` (P0.2, 20/20 captures);
    # the freeze/CCR key is (session_id, agent_id), so sub-agent traffic must be attributable apart
    # from the main stream. None for main-session requests.
    agent_id: str | None = None
    # matcher outcome ("unwired" until the matcher joins the request path — see MatcherEvent).
    # "error" (v6) means the matcher was consulted but THREW (fail-open returned None); the
    # exception class is in matcher_error. This is distinct from "unwired" (not consulted).
    matcher_event: MatcherEvent = "unwired"
    # matcher_error (v6) — the exception class name when the matcher threw (e.g. "OperationalError"
    # from a schema-drift `no such column`), else None. Makes a session-identity OUTAGE visible in
    # telemetry instead of hidden behind a "unwired" that also means "not consulted".
    matcher_error: str | None = None
    # tokens
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # cache-safety
    bust: bool = False
    bust_cause: BustCause = "none"
    # transforms / guard / ccr
    transforms: list[TransformRecord] = field(default_factory=list)
    frontier_rerenders: int = 0
    guard_fired: bool = False
    guard_action: GuardAction = "none"
    ccr_retrievals: int = 0
    # timing / health
    ttft_ms: float = 0.0  # time from request arrival to FIRST byte streamed back to the client
    # t_upstream_ttfb_ms — time from issuing the upstream send to the upstream's first byte, taken
    # AROUND the upstream call (not inside it). Distinct from ttft_ms (which includes apex's own
    # pre-forward work + the upstream wait): the perf panel needs upstream latency separated from
    # apex-added latency to attribute a slow request. 0 until the first upstream byte arrives.
    t_upstream_ttfb_ms: float = 0.0
    apex_added_ms: float = 0.0
    # upstream_error_wait_ms — on the upstream-raise error path (send_stream throws, e.g. a read
    # timeout), how long apex waited on the upstream BEFORE it failed. Kept SEPARATE from
    # apex_added_ms so a 600s read-timeout is attributed to the upstream, not billed to apex (whose
    # own cost stays pre_forward_ms). 0 on success and on the mid-stream error path (there ttfb_ms
    # already captured the wait). A latency panel reads apex_added_ms for apex cost, this for the
    # upstream-failure tail. See the the reference window finding: 42/127 errors showed ~600_000ms mis-billed.
    upstream_error_wait_ms: float = 0.0
    # connect-retry accounting (v6): how many times send_stream retried a ConnectError (v8: or a fast
    # read/TLS error on a completion endpoint) before this
    # row resolved, and the total backoff apex SLEPT (already billed into apex_added_ms, not
    # upstream latency — see F4). 0/0.0 on the common no-retry path. A recovered-after-retry request
    # is otherwise indistinguishable from a clean one; these make flaky-upstream recovery provable.
    connect_retries: int = 0
    connect_backoff_ms: float = 0.0
    is_error: bool = False
    # error_cause — WHY a row errored, provable not inferred (v5). Upstream-raise path: the exception
    # class name (`ReadTimeout`, `ConnectError`, `PoolTimeout`, ...). Response path: `http_<status>`
    # for any status >= 400 (so a 429 rate-limit is captured even though it does NOT set is_error,
    # which triggers only on >= 500). None on clean success. This is the field that distinguishes a
    # concurrency/pool problem from an upstream rate-limit from a timeout — the prerequisite for any
    # targeted guard.
    error_cause: str | None = None
    # upstream_rejected (v8) — the upstream RESPONDED with a status >= 400 (4xx or 5xx). Orthogonal to
    # is_error (>=500 / raise / mid-stream only): a 429 is upstream_rejected=True, is_error=False.
    # False on success AND on the upstream-raise path (no response existed — see error_cause there).
    upstream_rejected: bool = False
    # error_detail (v7) — WHAT the provider said when it rejected the request, for any status >= 400:
    # {"body": <bounded decoded error envelope>, "headers": {<rate-limit/retry headers>}}. None on
    # clean rows AND on the upstream-RAISE path (no response ever existed there — `error_cause` alone
    # carries that case). This is what turns an `http_429` cluster from "rate-limited, somehow" into a
    # named ceiling with a `retry-after`. Body capture is capped (usage.ERROR_BODY_CAP_BYTES) and
    # marked when truncated, so a telemetry row can't balloon and a consumer can't mistake a clipped
    # envelope for a whole one.
    error_detail: dict | None = None
    # cache_key_rewrite (v9) — set when the opt-in stable Codex prompt_cache_key replaced the
    # client's key on the forwarded body ({"from": "client", "key": "apex:<head>:<shard>"}); None when
    # the request was forwarded byte-identical (the default, and every non-Responses request).
    cache_key_rewrite: dict | None = None
    # content_encoding — the response Content-Encoding the usage scanner saw (gzip/br/identity/...).
    # None when unset (non-shadow line, or no header). Logged so a `usage=null` row is attributable
    # to its encoding: the pre-registered acceptance test joins this against usage-present to
    # prove/refute that the ~16.7% censoring was brotli (vs zstd or a non-SSE shape). See v3 note.
    content_encoding: str | None = None
    # shadow mode (M6b Stage A): the per-request decide() diff + provider-usage capture. `shadow`
    # is the byte-only prediction (ShadowReport.to_dict — cell keys, chosen transforms, predicted
    # BYTES saved, floor outcomes, bytes_by_class = R1's regressor X); `usage` is the provider's own
    # token accounting (ProviderUsage.to_dict — input/cache/output tokens = R1's regressand y). Both
    # None outside shadow mode, so a non-shadow line is byte-identical to before (asdict drops
    # nothing but stays null). Together they are the (X, y) pair R1 consumes from request one.
    shadow: dict | None = None
    usage: dict | None = None

    @classmethod
    def start(cls, *, apex_version: str, client: str) -> TelemetryEvent:
        """Open an event at request arrival with sane defaults; fill the rest before emit."""
        return cls(
            ts=time.time(), apex_version=apex_version, request_id=None, session_id=None,
            turn=0, epoch_id=None, client=client, model_requested=None,
            model_resolved=None, stratum="unknown",
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)


class TelemetryWriter:
    """Append-only jsonl sink. One line per request; fail-open (never break the proxy).

    Two line shapes, both carrying `schema_version` so a consumer (the TUI) can tell them apart and
    detect drift:
      - a request event (`TelemetryEvent.to_json`), the common case;
      - a HEARTBEAT (`{"ev":"hb", ...}`), emitted on a timer by the app lifespan so an IDLE proxy is
        visibly alive rather than indistinguishable from a dead one. Carries running counters so a
        consumer that started mid-stream can still show totals.

    ROTATION (documented so the TUI survives it — Step 3 hint 4): this writer appends to a single
    file; size-based rotation (rename `telemetry.jsonl` → `telemetry.jsonl.1` at ~64 MB, reopen) is
    an operator/deploy concern, NOT done on the hot path here. A consumer MUST handle reopen on an
    inode change; `rotate_if_large()` is provided for a caller (the lifespan tick) to invoke off hot
    path. The proxy never blocks a request on rotation.
    """

    ROTATE_BYTES = 64 * 1024 * 1024  # 64 MB — rename + reopen threshold (operator-invoked)

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # lightweight running counters for the heartbeat (a dead proxy emits nothing; a live-idle
        # one emits heartbeats with unchanging counters — the TUI shows "alive, 0 rpm").
        self.requests = 0
        self.errors = 0

    def emit(self, event: TelemetryEvent) -> None:
        self.requests += 1
        if event.is_error:
            self.errors += 1
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(event.to_json() + "\n")
        except OSError:
            pass  # telemetry must never take down the data plane

    def heartbeat(self) -> None:
        """Emit one heartbeat line (called on a ~15 s timer by the app lifespan). Fail-open."""
        line = {
            "ev": "hb",
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "ts": time.time(),
            "requests": self.requests,
            "errors": self.errors,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def rotate_if_large(self) -> None:
        """If the file exceeds ROTATE_BYTES, rename it aside (`.1`) so a fresh file starts. Invoked
        off the hot path (lifespan tick), never from `emit`. Fail-open — a rotation failure leaves
        the current file in place and appends continue."""
        try:
            if self.path.exists() and self.path.stat().st_size >= self.ROTATE_BYTES:
                self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
        except OSError:
            pass
