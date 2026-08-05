"""`apex doctor` — a telemetry cost report. Pure consumer of `~/.apex/telemetry.jsonl` (offline).

Reads the telemetry jsonl (no hot-path changes), partitions rows into GENERATIVE turns (usage
present), AUXILIARY requests (the count_tokens-shape null class — no usage event by design), and
ERRORS, then computes cache-health per session + rolled up. EVERY rate states its denominator (the
estimand discipline made product), and every dollar carries its `pricing_regime` (F-i doctrine).

Guards baked into the shapes, not left to the caller:
  - r:w is CUMULATIVE-per-session only (there is no windowed variant to misuse — the register's
    windowed-250:1 lesson made structural).
  - a dollar on unpriced traffic reads as un-priced (labeled `unknown` regime), never a fake number.
  - controls before trust: `tests/test_doctor.py` verifies every metric against hand arithmetic and
    a perfect-cache negative control BEFORE any real output is shown.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from apex_router.proxy_engine.readout.pricing import Rates

SUPPORTED_SCHEMA = {2, 3, 4}  # v4 added upstream_error_wait_ms; doctor's fields are v2-compatible

# Cache-served floor below which a large session is flagged for prefix instability. A BOUND, not a
# policy: derived from CORRECTED two-wire data (the reference window, served = read/(read+FRESH), wire-aware —
# re-confirmed after the witness-9 fix). Anthropic per-session cache-served was min 0.939 / p10 0.96
# (a sample, unchanged by the fix: the Anthropic branch was never mis-read), so the healthy floor is
# ~0.94. The one flagged OpenAI session sits at 0.445 (was mis-reported 0.308 pre-fix). The cutoff
# 0.700 sits cleanly BETWEEN them (0.445 < 0.700 < 0.939): a flag means "materially worse than any
# healthy Anthropic session AND clearly separated from the observed low-cache case", not "low end of
# normal". = healthy-min − 0.239; nudged off the round 0.70 to mark it derived, not chosen. Below
# this AND a large multi-turn session → alarm.
_HEALTHY_CACHE_SERVED_MIN = 0.939  # measured Anthropic per-session min (the healthy floor)
CACHE_SERVED_ALARM_FLOOR = round(_HEALTHY_CACHE_SERVED_MIN - 0.239, 3)  # 0.700 (derived not chosen)
# Below this input volume a session is too small to cache (sub-min-prefix) — never alarm (no overpay
# is recoverable). Bound: OpenAI/Anthropic min cacheable prefix is ~1024 tokens; require materially
# more so a genuinely-large session is what triggers the alarm.
_MIN_INPUT_FOR_ALARM = 50_000

# Cache idle-eviction TTL (seconds). MUST equal the authoritative cachesim value
# (apex_router.proxy_engine.tuner.cachesim.Pricing.ttl_s) — the two are asserted equal by a test so attribution and
# pricing never disagree on when a cache expires. Not imported, to avoid a readout→tuner dependency.
_TTL_S = 300.0

# ---------- error / timeout panel (A2b) ----------
# Telemetry emits NO HTTP status on error rows — only is_error, upstream_error_wait_ms (66/73
# populated on the live window), and sparsely ttft_ms. So failures are classified by BEHAVIORAL
# SIGNATURE, not status. Thresholds are named constants with derivations (mirroring the alarm floor):
_TIMEOUT_WAIT_MS = 300_000  # half the ~600s read-timeout ceiling; the live data has ZERO errors
#   between 106s and 600s, so this cut cleanly isolates the 600s-timeout cluster from all fast-fails.
_SLOW_TTFB_MS = 60_000      # first byte took > 60s and then failed (a stalling, not-yet-timed-out turn)

# Burst alarm: a timeout on a long agentic turn is COMPULSORY (long-horizon turns hit the ceiling), so
# rate/count floors would false-alarm on normal traffic (measured: timeout rate is a stable ~1.5–6%).
# What is anomalous is CLUSTERING — many timeouts near-together = upstream degradation / routing stall.
_BURST_WINDOW_S = 600  # 10-minute window
# K DERIVED, not chosen: on the live window the max timeouts in any sliding 10-min window was 4
# (scattered long-turn timeouts, not a burst). K=5 sits just above that observed non-bursty ceiling —
# "between observed-normal and a real anomaly", the same discipline as the 0.939 cache floor. Records
# the baseline so the choice is auditable. Re-derive if the traffic's normal scatter changes.
_BURST_OBSERVED_MAX_NONBURSTY = 4  # measured on the reference window); K must exceed this
_BURST_K = 5
# K must sit strictly above the observed non-bursty ceiling (else it fires on normal scatter). This
# assertion documents the derivation IN CODE and trips loudly if someone lowers K into the noise.
assert _BURST_K > _BURST_OBSERVED_MAX_NONBURSTY, "burst K must exceed the observed non-bursty max"


def classify_error(d: dict) -> str:
    """OBSERVED-signature class for one error row — NO causal inference (cross-validation).
    Classifies ONLY by what the telemetry directly records; it does NOT claim WHY (whether the
    request reached upstream, whether the client vs the backend gave up — the fields can't tell us).

    The two fields that ARE trustworthy: `upstream_error_wait_ms` (time blocked on upstream before it
    raised — populated when the upstream call itself failed) and `ttft_ms` (set only once a first byte
    reached the client). `tokens_in` is NOT a signal here: it defaults to 0 pre-dispatch and is
    overwritten only when provider usage is captured, so `tokens_in==0` on an error row means
    'no usage captured', not 'no dispatch' (passthrough.py:87/167).

      - upstream_timeout : blocked on upstream > _TIMEOUT_WAIT_MS then raised (the ~600s ceiling).
      - stream_failed    : a first byte arrived (ttft>0) then the stream errored — the request DID
                           reach upstream and started responding (covers mid-stream read timeouts,
                           which carry wait=0 by contract).
      - slow_first_byte  : blocked past _SLOW_TTFB_MS waiting for the first byte, then failed.
      - no_usage_captured: none of the above — the error left no usage and no first byte. This is a
                           DESCRIPTION (what we saw), not an attribution (fast 5xx, connection
                           failure, and a genuine client abort are indistinguishable here)."""
    wait = d.get("upstream_error_wait_ms") or 0
    ttft = d.get("ttft_ms") or 0
    t_upstream_ttfb = d.get("t_upstream_ttfb_ms") or 0
    if wait > _TIMEOUT_WAIT_MS:
        return "upstream_timeout"
    if ttft > 0 or t_upstream_ttfb > 0:
        return "stream_failed"          # a first byte arrived, then it errored (incl. mid-stream)
    if wait > _SLOW_TTFB_MS:
        return "slow_first_byte"        # long wait for a first byte that never came
    return "no_usage_captured"          # observed-only: no wait signal, no first byte, no usage


def error_panel(rows: list[dict], total_requests: int) -> dict:
    """Structured error/timeout panel. `rows` are the ERROR rows; `total_requests` is the panel's
    denominator (all non-heartbeat request rows in the window). Every number states its denominator;
    the timeout wait is reported as LATENCY hours, never dollars (error rows have usage=null)."""
    from collections import Counter
    by_class = Counter(classify_error(d) for d in rows)
    timeouts = [d for d in rows if classify_error(d) == "upstream_timeout"]
    wait_ms = sum(d.get("upstream_error_wait_ms") or 0 for d in timeouts)
    by_endpoint = Counter((d.get("endpoint_id") or "?") for d in timeouts)
    by_model = Counter(
        (d.get("model_resolved") or d.get("model_requested") or "?") for d in timeouts
    )
    return {
        "total": len(rows),
        "denominator": total_requests,
        "rate": (len(rows) / total_requests) if total_requests else None,
        "by_class": dict(by_class),
        "timeout": {
            "count": len(timeouts),
            "cumulative_wait_hours": round(wait_ms / 1000 / 3600, 1),  # LATENCY, not $
            "by_endpoint": dict(by_endpoint),
            "by_model": dict(by_model),
        },
        # Burst is detected PER ENDPOINT (cross-validation): 5 independent timeouts split across
        # anthropic+openai are not one backend degrading. Returns the first endpoint that bursts, or None.
        "burst_alarm": timeout_burst_alarm(timeouts),
    }


def timeout_burst_alarm(timeout_rows: list[dict]) -> dict | None:
    """Fire iff >= _BURST_K timeouts START within any _BURST_WINDOW_S window ON A SINGLE ENDPOINT — a
    per-backend cluster signalling that endpoint's degradation, NOT the scattered long-turn timeouts
    that are normal, and NOT a coincidental mix across endpoints (cross-validation). Returns None on
    scattered/mixed timeouts. The cause names the ACTUAL bursting endpoint, not a hardcoded one."""
    from collections import Counter, defaultdict
    by_ep = defaultdict(list)
    for d in timeout_rows:
        if isinstance(d.get("ts"), (int, float)):
            by_ep[d.get("endpoint_id") or "?"].append(d)
    for endpoint, rows_ep in by_ep.items():
        ts = sorted(d["ts"] for d in rows_ep)
        worst, worst_start = 0, None
        for t in ts:
            c = sum(1 for u in ts if t <= u < t + _BURST_WINDOW_S)
            if c > worst:
                worst, worst_start = c, t
        if worst >= _BURST_K:
            window_rows = [d for d in rows_ep
                           if worst_start <= d["ts"] < worst_start + _BURST_WINDOW_S]
            models = Counter(d.get("model_resolved") or d.get("model_requested") or "?"
                             for d in window_rows)
            return {
                "endpoint": endpoint,
                "timeouts_in_window": worst,
                "window_start_ts": worst_start,
                "window_seconds": _BURST_WINDOW_S,
                "by_model": dict(models),
                "cause": f"{worst} upstream timeouts clustered on endpoint '{endpoint}' — likely "
                         f"that backend degrading or a routing stall; check '{endpoint}' health for "
                         "this window (a per-endpoint cluster, not a client prefix issue)",
            }
    return None


# ---------- ingestion + partition ----------

def load_rows(path: str) -> list[dict]:
    """Telemetry jsonl → request-event dicts (heartbeats + malformed lines dropped, fail-open per
    line). Keeps ONLY supported schema versions; an unknown version is banner-able by the caller
    via
    `unsupported_schema_versions` (the TUI rule: don't guess field meanings on an unknown
    version)."""
    out = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(d, dict) and d.get("ev") != "hb":
                out.append(d)
    return out


def unsupported_schema_versions(rows: list[dict]) -> set:
    return {d.get("schema_version") for d in rows} - SUPPORTED_SCHEMA - {None}


# The schema version at which apex_added_ms stopped billing the upstream wait to apex on the error
# path (upstream_error_wait_ms was split out). BEFORE this, an ERROR row's apex_added_ms includes
# the full upstream stall (a 600s read-timeout reads as 600s of apex latency); a latency aggregation
# must not pool that OLD definition with clean post-v4 values.
APEX_ADDED_MS_SPLIT_SCHEMA = 4


def latency_safe_rows(rows: list[dict]) -> list[dict]:
    """Rows whose `apex_added_ms` carries ONE consistent definition — for any latency aggregation.
    Drops PRE-v4 ERROR rows (there apex_added_ms wrongly includes the upstream stall — the
    the reference window finding), keeps everything else (pre-v4 SUCCESS rows are unaffected; post-v4 rows are
    already clean). Without this, a p99 over error rows spanning the v3→v4 boundary mixes a
    ~600,000ms old-definition tail with milliseconds — the estimand trap applied to the migration.
    Use this before pooling apex_added_ms; a future latency panel must not skip it."""
    out = []
    for d in rows:
        sv = d.get("schema_version")
        pre_split = sv is None or (isinstance(sv, int) and sv < APEX_ADDED_MS_SPLIT_SCHEMA)
        if pre_split and d.get("is_error"):
            continue  # OLD apex_added_ms definition (upstream stall included) — not poolable
        out.append(d)
    return out


def _supported(d: dict) -> bool:
    """Row's schema version is one we know how to read. Unknown → neither generative nor auxiliary,
    so it contributes to NO metric (the docstring's "not analyzed" made true — was a lie: unknown
    rows used to flow into the dollar totals). Still surfaced by `unsupported_schema_versions`."""
    return d.get("schema_version") in SUPPORTED_SCHEMA


def is_generative(d: dict) -> bool:
    """A real model turn: provider usage present, on a supported schema."""
    return _supported(d) and d.get("usage") is not None and not d.get("is_error")


def is_auxiliary(d: dict) -> bool:
    """The count_tokens-shape null class: no usage event, no output, not an error (by design)."""
    return (
        _supported(d)
        and d.get("usage") is None
        and not d.get("is_error")
        and (d.get("tokens_out") or 0) == 0
    )


def _fresh_input(d: dict) -> int:
    """FRESH (uncached) input tokens for a row — WIRE-AWARE, because the two wires disagree on what
    the provider's `input_tokens` counts (verified against apex/proxy/usage.py + real telemetry):

      - OpenAI/Responses (`endpoint_id == "openai"`): `input_tokens` is the TOTAL prompt and
        `cache_read` (input_tokens_details.cached_tokens) is a SUBSET of it → fresh = total − read.
        Real row: tokens_in=14986, cache_read=14208 → fresh=778 (NOT 14986).
      - Anthropic (anthropic wire): `input_tokens` is ALREADY the fresh remainder; `cache_read`/`cache_write`
        are DISJOINT sibling pools → fresh = input_tokens unchanged. Real row: tokens_in=2,
        cache_read=17490.

    Treating `input_tokens` as fresh on both wires (the pre-fix bug) double-counted the OpenAI
    cached prefix: it reported a 44.5%-served Codex session as 30.8% and would alarm when fully
    cached."""
    u = d.get("usage") or {}
    ti = d.get("tokens_in") or u.get("input_tokens") or 0
    if (d.get("endpoint_id") or "").lower() == "openai":
        cr = d.get("cache_read_tokens") or 0
        return max(0, ti - cr)  # total prompt minus the cached subset
    return ti  # Anthropic: already the fresh remainder


# ---------- cache-health (all denominators explicit) ----------

@dataclass
class SessionHealth:
    session_id: str | None
    endpoint_id: str | None
    model: str | None
    n_generative: int = 0
    sum_cache_read: int = 0
    sum_cache_write: int = 0
    sum_input_uncached: int = 0
    sum_output: int = 0
    per_request_hits: list[float] = field(default_factory=list)  # for the miss taxonomy
    # A2c: sub-agents carry SEPARATE prefixes/caches → agent_id is part of the health key. APPENDED
    # (not inserted mid-dataclass) so the existing positional construction order is unchanged — a
    # positional SessionHealth(sid, ep, model, n) still assigns n to n_generative (Codex A2c-F6).
    agent_id: str | None = None

    @property
    def read_write_ratio(self) -> float | None:
        """CUMULATIVE per-session r:w = Σread/Σwrite. None if no writes. There is deliberately NO
        windowed variant — the windowed-250:1 artifact is made unrepresentable (register)."""
        return (self.sum_cache_read / self.sum_cache_write) if self.sum_cache_write else None

    @property
    def hit_rate(self) -> float | None:
        """`read/(read + uncached-input)` — the fraction of READ-ELIGIBLE prefix input served from
        cache (the WP2b definition). Excludes cache_write (freshly-written tokens aren't "eligible
        to
        have been a read"). None if the denominator is empty. LABEL it `hit_rate(read/(read+in))`
        wherever printed — it differs from `hit_rate_incl_write` by the write term."""
        den = self.sum_cache_read + self.sum_input_uncached
        return (self.sum_cache_read / den) if den else None

    @property
    def hit_rate_incl_write(self) -> float | None:
        """`read/(read + write + uncached-input)` — the fraction of ALL prefix input that hit
        cache,
        counting cache-creation writes in the denominator (the historical 97.9% baseline
        definition).
        More conservative than `hit_rate` (writes are input the user paid for). Both are reported,
        labeled — they answer different questions (read-eligible vs all-input). None if empty."""
        den = self.sum_cache_read + self.sum_cache_write + self.sum_input_uncached
        return (self.sum_cache_read / den) if den else None

    @property
    def miss_count(self) -> int:
        """Generative requests whose per-request hit-rate was < 50%."""
        return sum(1 for h in self.per_request_hits if h < 0.5)

    def dollars_saved_vs_uncached(self, rates: Rates) -> float:
        """$ the cache saved vs resending everything uncached, at `rates` ($/M):
          saved = Σread·(P_in − P_read)      # reads billed at the discount instead of full input
                − Σwrite·(P_write − P_in)     # minus the write premium paid to create the cache
        A positive number = the cache is net-saving; negative = writes cost more than reads saved
        (the cache-churn case). OpenAI: P_write=0 → the write term is 0 (no premium)."""
        save = self.sum_cache_read * (rates.input - rates.cache_read) / 1e6
        cost = self.sum_cache_write * (rates.cache_write - rates.input) / 1e6
        return save - cost


def _per_request_hit(d: dict) -> float | None:
    """Per-request served fraction read/(read + write + FRESH). Uses wire-aware fresh input so the
    OpenAI cached subset isn't counted twice (else a fully-cached OpenAI turn reads as ~50%)."""
    cr = d.get("cache_read_tokens") or 0
    cw = d.get("cache_write_tokens") or 0
    den = cr + cw + _fresh_input(d)
    return (cr / den) if den else None


def session_health(rows: list[dict]) -> dict:
    """Per-session SessionHealth over the GENERATIVE turns. Keyed by session_id (None → one bucket
    per (endpoint) since Codex sends no session id — labeled, not merged)."""
    out: dict = {}
    for d in rows:
        if not is_generative(d):
            continue
        sid = d.get("session_id")
        model = d.get("model_resolved") or d.get("model_requested")
        # Bucket key = (session_id, endpoint, model). This is a PRICING-CORRECTNESS key, NOT yet a
        # cache-population key (see caveat). Pricing is per-(endpoint, model): the pre-fix code keyed
        # a whole session by session_id alone and priced every turn at the FIRST turn's model rate,
        # so a session whose turn-0 was routed to a cheap model (Claude Code routes turn-0 to Haiku)
        # had its expensive Opus/Sonnet turns priced at Haiku's low rate — measured ~2× under-count
        # of cache savings ($1.8k→$3.7k on the live window). Keying by (sid, endpoint, model) prices
        # each turn at its own model. Endpoint is included for consistency with the no-session branch
        # and to prevent a cross-endpoint bucket from being priced by one endpoint's first row (latent
        # today: 0 live cross-endpoint buckets).
        #
        # A2c (a measurement window): the key includes agent_id, because sub-agents carry SEPARATE prefixes and
        # SEPARATE provider caches (events.py: the freeze/CCR key is (session_id, agent_id)). Pooling
        # main-thread + sub-agent turns into one bucket mixes distinct cache populations, so hit_rate /
        # r:w / the alarm would be computed over the wrong population (Codex A2-F2). This is the
        # cache-HEALTH population; PRICING is key-invariant (Σ read·Δ − Σ write·Δ is a linear sum), so
        # the dollar total is unchanged by the split — only the per-(sid,agent,ep,model) ROWS are finer.
        # (The 0.939 healthy-floor recalibration is DEFERRED: only 1 of 62 healthy buckets falls below
        # it — n=1 is not a basis to move a floor; see the A2c spec.)
        agent = d.get("agent_id")
        key = (sid, agent, d.get("endpoint_id"), model) if sid is not None \
            else f"<no-session:{d.get('endpoint_id')}:{model}>"
        h = out.get(key)
        if h is None:
            h = SessionHealth(sid, d.get("endpoint_id"), model, agent_id=agent)
            out[key] = h
        u = d.get("usage") or {}
        h.n_generative += 1
        h.sum_cache_read += d.get("cache_read_tokens") or 0
        h.sum_cache_write += d.get("cache_write_tokens") or 0
        h.sum_input_uncached += _fresh_input(d)  # wire-aware: OpenAI total − cached subset
        h.sum_output += d.get("tokens_out") or u.get("output_tokens") or 0
        hr = _per_request_hit(d)
        if hr is not None:
            h.per_request_hits.append(hr)
    return out


# ---------- cold-turn attribution (A1', measure-only) ----------

def cold_turn_attribution(rows: list[dict]) -> dict:
    """OBSERVATIONAL split of cold generative turns — NO cause is attributed (cross-validation).
    A cold turn's cause is NOT identifiable from measure-only telemetry: distinguishing an idle TTL
    eviction from a client edit / rerender / transform needs the previously-cached prefix BYTES, which
    apex does not store. So this reports only what IS observable — was a cold turn preceded by a
    ≥ _TTL_S idle gap — and names neither a cause nor a verdict ('benign' / 'anomaly').

    Grouped by the FULL cache identity `(session_id, agent_id, endpoint, model)` — agent_id is
    included because sub-agents carry SEPARATE prefixes/caches (events.py); omitting it lets one
    agent's timestamp corrupt another's inter-turn gap (cross-validation). Turns sorted by ts per bucket:

      - warm                : cache_read > 0 (the prefix was served).
      - first_observed_cold : the bucket's first OBSERVED turn is cold. NOT necessarily the first
                              request ever — the window may start/rotate mid-session — so it is
                              'first-observed', not 'compulsory cold start'.
      - cold_after_ttl_gap  : a later cold turn whose gap to the previous turn >= _TTL_S. TTL eviction
                              was POSSIBLE in that gap — but this is NOT a claim it caused the miss (an
                              edit in the same gap is identical on the wire). Observational only.
      - cold_no_ttl_gap     : a later cold turn with gap < _TTL_S. The cache was likely still live, so
                              a below-min-cacheable turn or a real prefix change are both candidates —
                              cause not attributable measure-only. This is the triage-worthy bucket,
                              stated WITHOUT a cause claim.

    Rows with no session_id are skipped (no orderable per-session cache); their count is returned as
    `skipped_no_session` so they are named, not silently dropped."""
    from collections import defaultdict
    buckets: dict = defaultdict(list)
    skipped_no_session = 0
    for d in rows:
        if not is_generative(d):
            continue
        sid = d.get("session_id")
        if sid is None:
            skipped_no_session += 1  # no session id → no orderable per-session cache
            continue
        model = d.get("model_resolved") or d.get("model_requested")
        buckets[(sid, d.get("agent_id"), d.get("endpoint_id"), model)].append(d)

    first_observed_cold = cold_after_ttl_gap = cold_no_ttl_gap = warm = 0
    ttl_gaps: list[float] = []
    for turns in buckets.values():
        turns.sort(key=lambda d: d.get("ts") or 0.0)
        prev_ts = None
        for d in turns:
            read = d.get("cache_read_tokens") or 0
            ts = d.get("ts") or 0.0
            if read > 0:
                warm += 1
            elif prev_ts is None:
                first_observed_cold += 1   # first turn we SAW in this bucket (may predate the window)
            else:
                gap = ts - prev_ts
                if gap >= _TTL_S:
                    cold_after_ttl_gap += 1
                    ttl_gaps.append(gap)
                else:
                    cold_no_ttl_gap += 1
            prev_ts = ts

    gaps_sorted = sorted(ttl_gaps)
    gap_stats = {
        "min": gaps_sorted[0], "median": gaps_sorted[len(gaps_sorted) // 2], "max": gaps_sorted[-1],
    } if gaps_sorted else None
    return {
        "first_observed_cold": first_observed_cold,
        "cold_after_ttl_gap": cold_after_ttl_gap,
        "cold_no_ttl_gap": cold_no_ttl_gap,
        "warm": warm,
        "skipped_no_session": skipped_no_session,
        "ttl_gap_seconds": gap_stats,  # evidence the ≥TTL gaps are genuinely long idle periods
    }


# ---------- the Codex-wire prefix-instability alarm ----------

# Static cause list — honest, actionable, NO inference dressed as diagnosis (WP2c/WP3).
_INSTABILITY_CAUSES = (
    "a system prompt containing a timestamp/UUID (changes every turn)",
    "tool definitions re-serialized in unstable order",
    "conversation history re-rendered with whitespace/escape drift",
)


def prefix_instability_alarm(h: SessionHealth, rates: Rates) -> dict | None:
    """Flag a LARGE, MULTI-TURN session whose cache-served fraction is materially below the healthy
    floor — prefix instability, where the client re-sends a prefix the cache could have served,
    paying full input rate. None (no alarm) when the session is:
      - a single turn (n_generative < 2): a cold start has NO prior prefix to have read from cache,
        so 0% served is compulsory, not instability (cross-validation);
      - too small to cache (fresh+read below the min cacheable prefix);
      - healthy (served >= floor).

    `served` = read/(read + FRESH) — deliberately EXCLUDING cache_write, because the 0.939 healthy
    floor was measured on exactly this denominator (real healthy Anthropic sessions run as low as
    0.33 once creation writes are included, so folding writes in would false-alarm on normal cache
    CREATION — verified the reference window). `served` uses wire-aware fresh input, so a fully-cached OpenAI
    session reads as ~100%, not ~50% (cross-validation).

    `recoverable_ceiling_dollars` is an UPPER BOUND, not a realized overpay: it prices the fresh
    input as if ALL of it could have been a cache read (fresh × (input − cache_read)). Real fresh
    input always contains compulsory cold-start + newly-appended-suffix tokens that no stable prefix
    could serve, so the true recoverable figure is strictly less (cross-validation). Labeled by
    `pricing_regime` (F-i); carries a STATIC cause list; points at the divergence report (WP3)."""
    if h.n_generative < 2:
        return None  # a lone cold-start turn has no prior prefix — 0% served is compulsory
    total_input = h.sum_cache_read + h.sum_input_uncached  # read + fresh (excl-write, per floor)
    if total_input < _MIN_INPUT_FOR_ALARM:
        return None  # too small to cache — no recoverable prefix
    served = h.sum_cache_read / total_input  # total_input >= _MIN_INPUT_FOR_ALARM > 0 here
    if served >= CACHE_SERVED_ALARM_FLOOR:
        return None  # above the 0.700 ALARM floor — not flagged (NOT necessarily "healthy": a bucket
        # in the 0.700–0.939 band is below the historical healthy reference but not alarm-worthy;
        # A2c-F4). The alarm only fires materially-below-any-healthy-session, by design.
    ceiling = h.sum_input_uncached * (rates.input - rates.cache_read) / 1e6
    return {
        "session_id": h.session_id,
        "agent_id": h.agent_id,  # A2c: which sub-agent's prefix (None = main thread)
        "endpoint_id": h.endpoint_id,
        "cache_served": served,
        "uncached_input_tokens": h.sum_input_uncached,
        "recoverable_ceiling_dollars": ceiling,  # UPPER BOUND (all fresh priced as cacheable)
        "pricing_regime": rates.pricing_regime,
        "causes": list(_INSTABILITY_CAUSES),
        "next": "See the divergence report (apex doctor --divergence) for per-event detail.",
    }


# ---------- report assembly (WP2d): structured dict → text or JSON ----------

# Gateway/vendor prefixes to strip for a readable model name. A gateway that prefixes model ids
# (e.g. "<gateway>-claude-opus-4-8") adds its prefixes via APEX_MODEL_PREFIXES (comma-separated).
_MODEL_PREFIXES = tuple(
    p for p in (os.environ.get("APEX_MODEL_PREFIXES", "") + ",claude-").split(",") if p
)


def _strip_model(model: str | None) -> str:
    """Readable model name: strip a known vendor/gateway prefix but KEEP the family+version
    ("opus-4-8", "sonnet-4-6") — never over-truncate. Longest prefix wins."""
    m = model or "?"
    for pfx in sorted(_MODEL_PREFIXES, key=len, reverse=True):
        if m.startswith(pfx):
            return m[len(pfx):]
    return m


def _window(rows: list[dict]) -> dict:
    """The POPULATION LABEL that must travel with every number: which rows this report describes.
    A rate is only comparable across two runs if both name their window (the estimand discipline
    generalized from 'every rate states its denominator' to 'every report states its window' — so a
    hackathon team comparing runs can't rediscover estimand-switching the hard way). Reflects any
    `--since`/`--session` filtering because it's computed on the already-filtered rows."""
    ts = sorted(d["ts"] for d in rows if isinstance(d.get("ts"), (int, float)))
    span_h = (ts[-1] - ts[0]) / 3600 if len(ts) >= 2 else 0.0
    return {
        "rows": len(rows),
        "first_ts": ts[0] if ts else None,
        "last_ts": ts[-1] if ts else None,
        "span_hours": round(span_h, 1),
    }


def build_report(rows: list[dict]) -> dict:
    """Assemble the full doctor report as a structured dict (the `--json` payload). Pure function of
    the telemetry rows; the text renderer consumes this same dict, so text/JSON can't diverge."""
    from apex_router.proxy_engine.readout.pricing import rates_for

    gen = [d for d in rows if is_generative(d)]
    aux = [d for d in rows if is_auxiliary(d)]
    err = [d for d in rows if _supported(d) and d.get("is_error")]  # unknown-schema errors excluded
    health = session_health(rows)

    sessions = []
    total_saved = 0.0
    total_ceiling = 0.0
    unpriced_sessions = 0
    alarms = []
    for h in health.values():
        rates = rates_for(h.model, h.endpoint_id)
        priced = not rates.pricing_regime.startswith("unknown:")
        # A session at an UNKNOWN rate contributes NO dollars to the headline — summing its zeros in
        # would print a fake "$0 saved" as if the cache did nothing (cross-validation). Count it separately
        # so the report can say "N sessions unpriced" instead of silently under-counting the total.
        if priced:
            saved = h.dollars_saved_vs_uncached(rates)
            total_saved += saved
        else:
            saved = None
            unpriced_sessions += 1
        alarm = prefix_instability_alarm(h, rates) if priced else None
        if alarm is not None:
            alarms.append(alarm)
            total_ceiling += alarm["recoverable_ceiling_dollars"]
        sessions.append({
            "session_id": h.session_id, "agent_id": h.agent_id,  # A2c: distinguish sub-agent buckets
            "endpoint_id": h.endpoint_id, "model": h.model,
            "n_generative": h.n_generative,
            "read_write_ratio": h.read_write_ratio,
            "hit_rate": h.hit_rate, "hit_rate_incl_write": h.hit_rate_incl_write,
            "miss_count": h.miss_count,
            "dollars_saved": saved, "pricing_regime": rates.pricing_regime,
        })
    return {
        "window": _window(rows),  # population label — which rows every number below describes
        "denominators": {
            "generative_turns": len(gen), "auxiliary_requests": len(aux), "errors": len(err),
            "note": "all rates on the GENERATIVE denominator; auxiliary/errors counted separately",
        },
        "unsupported_schema_versions": sorted(unsupported_schema_versions(rows)),
        "unpriced_sessions": unpriced_sessions,  # sessions at an unknown rate (NOT in the $ total)
        "totals": {
            "dollars_saved_by_cache": total_saved,
            # UPPER BOUND on recoverable spend if every flagged session's fresh input were cacheable
            # — not a realized overpay (cross-validation). Named to match the alarm field.
            "recoverable_ceiling_instability": total_ceiling,
        },
        "sessions": sorted(sessions, key=lambda s: -(s["dollars_saved"] or 0.0)),
        "alarms": alarms,
        # Cold-turn attribution (A1', measure-only): split cold prefixes into ttl (benign idle
        # eviction) vs unknown (cold prefix the TTL can't explain — worth a look) vs first_cold.
        "cold_turns": cold_turn_attribution(rows),
        # Error/timeout panel (A2b). Denominator = all analyzed request rows (generative + auxiliary
        # + errors) — the request population the error rate is a fraction OF. Errors have usage=null,
        # so they never touch the dollar total; the timeout wait is reported as latency hours.
        "errors": error_panel(err, len(gen) + len(aux) + len(err)),
    }


def format_report(report: dict) -> str:
    """Render the report dict as terminal text. Structure: headline dollars → per-session table →
    alarms → denominators/regime footnotes (WP2d). No advice engine — numbers, causes, one fix."""
    L = []
    t = report["totals"]
    L.append("apex doctor — cache-cost report")
    L.append("=" * 60)
    w = report.get("window") or {}
    if w.get("rows"):
        # Population label first — every number below describes THIS window (state it so two runs
        # are comparable without guessing; reflects --since/--session, it's the filtered rows).
        span = f"{w['span_hours']:.1f}h" if w.get("span_hours") else "n/a"
        L.append(f"window: {w['rows']:,} rows over {span}  (all figures below are for this window)")
    L.append(f"cache SAVED you: ${t['dollars_saved_by_cache']:,.2f}")
    if t["recoverable_ceiling_instability"] > 0:
        # UPPER BOUND, not a realized loss — the wording must not overclaim (cross-validation).
        L.append("prefix-instability recoverable ceiling (upper bound): "
                 f"${t['recoverable_ceiling_instability']:,.2f}  ⚠")
    d = report["denominators"]
    L.append(f"({d['generative_turns']} generative turns · {d['auxiliary_requests']} auxiliary · "
             f"{d['errors']} errors — rates on the generative denominator)")
    if report.get("unpriced_sessions"):
        L.append(f"⚠ {report['unpriced_sessions']} (session×agent×model) bucket(s) at an UNKNOWN rate "
                 "— excluded from the dollar total (not priced as $0)")
    if report["unsupported_schema_versions"]:
        L.append("⚠ unknown schema versions present (not analyzed): "
                 f"{report['unsupported_schema_versions']}")
    L.append("")
    # A ROW is a (session, agent, endpoint, model) cache-health bucket (A2c) — a session that used >1
    # model, or a main thread + sub-agents, appears as >1 row (same session id; the `agent` column
    # distinguishes sub-agent buckets from the main thread). PRICING is still per-(session,endpoint,
    # model) and key-invariant, so the dollar TOTAL is unchanged; only the per-bucket rows are finer.
    L.append("per (session × agent × model) cache bucket — top by savings (a session may span rows):")
    # `hit` = read/(read+fresh); `hit+w` = read/(read+write+fresh) — label both denominators (F11).
    L.append(f"  {'session':14} {'agent':8} {'wire':8} {'model':10} {'r:w':>7} {'hit':>6} {'hit+w':>6} "
             f"{'miss':>5} {'saved$':>9}  regime")
    for s in report["sessions"][:20]:
        sid = (s["session_id"] or "<none>")[:12]
        agent = (s.get("agent_id") or "main")[:7]  # 'main' = main thread (agent_id None); else sub-id
        model_short = _strip_model(s.get("model"))
        rw = f"{s['read_write_ratio']:.1f}" if s["read_write_ratio"] is not None else "—"
        hit = f"{100*s['hit_rate']:.0f}%" if s["hit_rate"] is not None else "—"
        hw = s["hit_rate_incl_write"]
        hitw = f"{100*hw:.0f}%" if hw is not None else "—"
        saved = "—" if s["dollars_saved"] is None else f"{s['dollars_saved']:>9.2f}"
        L.append(f"  {sid:14} {agent:8} {(s['endpoint_id'] or '?'):8} {model_short[:10]:10} {rw:>7} {hit:>6} {hitw:>6} "
                 f"{s['miss_count']:>5} {saved:>9}  {s['pricing_regime']}")
    L.append("  (hit = read/(read+fresh); hit+w includes cache-writes in the denominator)")
    # cold-turn OBSERVATION (A1'): purely observational — cold turns split by whether a >=TTL idle
    # gap preceded them. NO cause is named and NONE is called benign: a >=TTL gap only proves
    # eviction was POSSIBLE, not that it happened (a client edit in the same gap is identical on the
    # wire); the split is a triage hint, not a diagnosis (cross-validation).
    ct = report.get("cold_turns")
    if ct and (ct["cold_after_ttl_gap"] or ct["cold_no_ttl_gap"] or ct["first_observed_cold"]):
        parts = []
        if ct["cold_after_ttl_gap"]:
            parts.append(f"{ct['cold_after_ttl_gap']} after a ≥{int(_TTL_S)}s idle gap (TTL-compatible)")
        if ct["cold_no_ttl_gap"]:
            parts.append(f"{ct['cold_no_ttl_gap']} with no idle gap (cause not attributable measure-only)")
        if ct["first_observed_cold"]:
            parts.append(f"{ct['first_observed_cold']} first-observed (may predate the window)")
        L.append("cold prefixes (cause NOT attributable — observational): " + " · ".join(parts))
    # ALARMS render UNCONDITIONALLY on their own presence — NOT gated on cold turns (the cold-prefix
    # block above must never swallow the alarm block; regression caught by cross-validation-07-25).
    if report["alarms"]:
        L.append("")
        L.append("ALARMS — prefix instability (cacheable prefix re-sent uncached):")
        for a in report["alarms"]:
            L.append(f"  ⚠ session {(a['session_id'] or '<none>')[:12]} [{a['endpoint_id']}]: "
                     f"only {100*a['cache_served']:.0f}% cache-served, up to "
                     f"${a['recoverable_ceiling_dollars']:,.2f} recov. [{a['pricing_regime']}]")
            L.append(f"    likely: {a['causes'][0]}; {a['next']}")
    # ---- error / timeout panel (A2b) ----
    e = report.get("errors")
    if e and e["total"]:
        L.append("")
        rate = f"{100*e['rate']:.1f}%" if e["rate"] is not None else "n/a"
        L.append(f"ERRORS (this window): {e['total']} / {e['denominator']:,} requests = {rate}")
        # OBSERVED classes only — no causal labels (cross-validation).
        _notes = {
            "upstream_timeout": "(blocked on upstream > 300s, then raised — the ~600s ceiling)",
            "stream_failed": "(first byte arrived, then the stream errored — incl. mid-stream)",
            "slow_first_byte": "(long wait for a first byte that never came)",
            "no_usage_captured": "(errored with no first byte and no usage — cause not attributable)",
        }
        for cls in ("upstream_timeout", "stream_failed", "slow_first_byte", "no_usage_captured"):
            n = e["by_class"].get(cls, 0)
            if n:
                L.append(f"  {n:>3} {cls:18} {_notes[cls]}")
        to = e["timeout"]
        if to["count"]:
            # LATENCY hours, explicitly NOT dollars (error rows have usage=null → no billable tokens)
            L.append(f"      timeouts: {to['cumulative_wait_hours']}h cumulative upstream wait "
                     f"(latency, not $)")
            bye = " · ".join(f"{ep} {c}"
                             for ep, c in sorted(to.get("by_endpoint", {}).items(), key=lambda kv: -kv[1]))
            if bye:
                L.append(f"      by endpoint: {bye}")
            bym = " · ".join(f"{_strip_model(m)} {c}"
                             for m, c in sorted(to["by_model"].items(), key=lambda kv: -kv[1]))
            if bym:
                L.append(f"      by model: {bym}")
        ba = e.get("burst_alarm")
        if ba:
            L.append(f"  ⚠ TIMEOUT BURST on '{ba['endpoint']}': {ba['timeouts_in_window']} within "
                     f"{ba['window_seconds']//60} min — {ba['cause']}")
        else:
            L.append("  (timeouts scattered / not clustered on any single endpoint — no burst alarm)")
    L.append("")
    L.append("— dollars are LIST price at each session's pricing_regime (unknown rates excluded); "
             "'recoverable' is an upper bound, not a realized loss —")
    return "\n".join(L)
