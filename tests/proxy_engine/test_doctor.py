"""apex doctor — CONTROLS BEFORE TRUST (the instrument rule; doctor is an instrument).

Two controls, both required before any real output is shown to anyone (WP2):
  1. HAND-COMPUTED FIXTURE — a session where every metric is verified against manual arithmetic
     (the calculus-fixture pattern). If the code and the hand math disagree, the code is wrong.
  2. PERFECT-CACHE NEGATIVE CONTROL — a synthetic perfectly-cached session must produce ZERO alarms
     and $0 overpay. A doctor that alarms on healthy traffic is worse than no doctor.
"""
from __future__ import annotations

from apex_router.proxy_engine.readout.doctor import (
    is_auxiliary,
    is_generative,
    session_health,
    unsupported_schema_versions,
)
from apex_router.proxy_engine.readout.pricing import rates_for


def _gen(session, endpoint, cache_read, cache_write, input_uncached, output, model="opus",
         agent=None):
    """A generative telemetry row with the given token fields. `agent` = x-claude-code-agent-id
    (None = main thread; a value = a sub-agent, which carries its OWN prefix/cache — A2c)."""
    return {
        "schema_version": 3, "session_id": session, "endpoint_id": endpoint,
        "model_resolved": model, "is_error": False, "tokens_out": output,
        "tokens_in": input_uncached, "agent_id": agent,
        "cache_read_tokens": cache_read, "cache_write_tokens": cache_write,
        "usage": {"input_tokens": input_uncached, "output_tokens": output,
                  "cache_read_tokens": cache_read, "cache_creation_tokens": cache_write},
    }


def _aux(session):
    return {"schema_version": 3, "session_id": session, "endpoint_id": "anthropic",
            "is_error": False, "tokens_out": 0, "usage": None}


# ---------- CONTROL 1: hand-computed fixture ----------

def test_hand_computed_session_health():
    # A 3-turn session, one model. Hand arithmetic below each row.
    rows = [
        _gen("s1", "anthropic", cache_read=0,      cache_write=10_000, input_uncached=2_000, output=100),
        _gen("s1", "anthropic", cache_read=100_000, cache_write=500,    input_uncached=2_000, output=200),
        _gen("s1", "anthropic", cache_read=200_000, cache_write=500,    input_uncached=1_000, output=50),
    ]
    # keyed by (session_id, model); single-model session → exactly one bucket
    h = session_health(rows)[("s1", None, "anthropic", "opus")]
    # Σread = 0+100k+200k = 300_000 ; Σwrite = 10_000+500+500 = 11_000
    assert h.sum_cache_read == 300_000
    assert h.sum_cache_write == 11_000
    # r:w (cumulative) = 300_000 / 11_000
    assert h.read_write_ratio == 300_000 / 11_000
    # Σuncached-input = 2_000+2_000+1_000 = 5_000
    assert h.sum_input_uncached == 5_000
    # hit_rate = Σread / (Σread + Σuncached) = 300_000 / 305_000
    assert h.hit_rate == 300_000 / 305_000
    # per-request hits: turn1 = 0/(0+10000+2000)=0.0 (MISS) ; turn2 = 100000/(100000+500+2000)=0.975 ;
    #                   turn3 = 200000/(200000+500+1000)=0.9925 → 1 miss (<50%)
    assert h.miss_count == 1
    # dollars saved vs uncached at opus list ($15 in / $1.5 read / $18.75 write per M):
    #   save = 300_000·(15−1.5)/1e6 = 300_000·13.5/1e6 = 4.05
    #   cost = 11_000·(18.75−15)/1e6 = 11_000·3.75/1e6  = 0.04125
    #   saved = 4.05 − 0.04125 = 4.00875
    rates = rates_for("opus", "anthropic")
    assert abs(h.dollars_saved_vs_uncached(rates) - (4.05 - 0.04125)) < 1e-9


# ---------- CONTROL 2: perfect-cache negative control ----------

def test_perfect_cache_session_has_no_misses_and_positive_savings():
    # Every turn reads a big cached prefix, ~no writes, tiny uncached tail → healthy.
    rows = [_gen("s2", "anthropic", cache_read=500_000, cache_write=0, input_uncached=100, output=50)
            for _ in range(6)]
    h = session_health(rows)[("s2", None, "anthropic", "opus")]
    assert h.miss_count == 0, "a perfectly-cached session must raise ZERO misses"
    assert h.hit_rate is not None and h.hit_rate > 0.99
    rates = rates_for("opus", "anthropic")
    assert h.dollars_saved_vs_uncached(rates) > 0, "perfect cache must show POSITIVE savings, not overpay"


def test_zero_traffic_session_is_not_fabricated():
    # No generative rows → no sessions (a doctor must not invent a session from auxiliary-only traffic)
    assert session_health([_aux("s3"), _aux("s3")]) == {}


# ---------- partition + schema discipline ----------

def test_partition_generative_vs_auxiliary_vs_error():
    gen = _gen("s", "anthropic", 100, 0, 10, 5)
    aux = _aux("s")
    err = {"schema_version": 3, "is_error": True, "usage": None, "tokens_out": 0}
    assert is_generative(gen) and not is_auxiliary(gen)
    assert is_auxiliary(aux) and not is_generative(aux)
    assert not is_generative(err) and not is_auxiliary(err)  # error is neither


def test_unknown_schema_version_is_flagged_not_guessed():
    rows = [{"schema_version": 99, "usage": None}, {"schema_version": 3, "usage": None}]
    assert unsupported_schema_versions(rows) == {99}


# ---------- apex_added_ms definition boundary (schema v4) ----------

def test_latency_safe_rows_excludes_pre_v4_error_rows():
    # apex_added_ms changed meaning at v4: pre-v4 ERROR rows billed the upstream stall to apex (a
    # 600s read-timeout reads as 600s of apex latency). A latency aggregation must not pool those
    # with clean post-v4 values. latency_safe_rows() drops pre-v4 error rows and keeps the rest, so
    # any apex_added_ms stat has ONE definition. (Estimand discipline applied to the schema migration.)
    from apex_router.proxy_engine.readout.doctor import latency_safe_rows
    rows = [
        {"schema_version": 3, "is_error": False, "apex_added_ms": 5.0},    # pre-v4 success: fine
        {"schema_version": 3, "is_error": True, "apex_added_ms": 600_000},  # pre-v4 error: OLD defn
        {"schema_version": 4, "is_error": True, "apex_added_ms": 6.0},      # post-v4 error: NEW defn
        {"schema_version": 4, "is_error": False, "apex_added_ms": 4.0},     # post-v4 success: fine
    ]
    safe = latency_safe_rows(rows)
    assert {r["apex_added_ms"] for r in safe} == {5.0, 6.0, 4.0}
    assert 600_000 not in {r["apex_added_ms"] for r in safe}, "the old-definition tail must be dropped"


def test_no_windowed_rw_variant_exists():
    # The windowed-250:1 trap is made unrepresentable: SessionHealth exposes ONLY cumulative r:w.
    from apex_router.proxy_engine.readout.doctor import SessionHealth
    assert not any("window" in a.lower() for a in dir(SessionHealth)), (
        "a windowed r:w variant exists — the windowed-250:1 artifact must be unrepresentable"
    )


# ---------- the two hit-rate definitions are distinct and both labeled ----------

def test_two_hit_rate_definitions_differ_by_the_write_term():
    # A session with real writes: the two definitions must diverge (spec read/(read+in) >
    # incl-write read/(read+write+in)), so the estimand fork the snapshot exposed is explicit.
    rows = [_gen("s", "anthropic", cache_read=100_000, cache_write=20_000, input_uncached=1_000, output=5)]
    h = session_health(rows)[("s", None, "anthropic", "opus")]
    assert h.hit_rate == 100_000 / (100_000 + 1_000)            # read/(read+in) = 0.990
    assert h.hit_rate_incl_write == 100_000 / (100_000 + 20_000 + 1_000)  # read/(read+write+in) = 0.826
    assert h.hit_rate > h.hit_rate_incl_write, "the two hit-rate definitions must be distinct"


# ---------- A2: per-(endpoint, model) PRICING stratification ----------
# The bucket key is (session_id, endpoint, model). This fixes PRICING (each turn priced at its own
# model rate, not the session's first-turn model) — NOT cache-health population validity: a
# (sid, model) bucket still pools main-thread + subagent turns (cross-validation), so hit_rate/alarm
# over it stay approximate pending an agent_id-stratified follow-up. These tests pin the PRICING fix
# and its token conservation; they do NOT claim the health metrics are population-correct.
_ANTHROPIC = "anthropic"


def test_model_switch_splits_into_per_model_buckets_hand_computed():
    # One session_id, two models. Each model's metrics must sum ONLY its own turns.
    rows = [
        _gen("sx", _ANTHROPIC, cache_read=200_000, cache_write=100, input_uncached=1_000, output=50, model="opus"),
        _gen("sx", _ANTHROPIC, cache_read=300_000, cache_write=100, input_uncached=1_000, output=50, model="opus"),
        _gen("sx", _ANTHROPIC, cache_read=0,       cache_write=5_000, input_uncached=2_000, output=50, model="haiku"),
    ]
    health = session_health(rows)
    # exactly two buckets, both tagged with the SAME session_id, DIFFERENT model
    assert set(health.keys()) == {("sx", None, _ANTHROPIC, "opus"), ("sx", None, _ANTHROPIC, "haiku")}
    opus = health[("sx", None, _ANTHROPIC, "opus")]
    haiku = health[("sx", None, _ANTHROPIC, "haiku")]
    # opus bucket sums ONLY the two opus turns; haiku's tokens do not leak in
    assert opus.sum_cache_read == 500_000          # 200k + 300k (NOT + 0 from haiku)
    assert opus.sum_input_uncached == 2_000        # 1k + 1k (NOT + 2k from haiku)
    assert opus.n_generative == 2
    # haiku bucket is its own cold-start population
    assert haiku.sum_cache_read == 0
    assert haiku.sum_input_uncached == 2_000
    assert haiku.n_generative == 1
    # shared session label, distinct model (the chosen presentation: split rows, shared session id)
    assert opus.session_id == haiku.session_id == "sx"
    assert opus.model == "opus" and haiku.model == "haiku"


def test_split_conserves_tokens_no_leak_or_double_count():
    # cross-validation: the split must PARTITION tokens, not lose or duplicate them. Sum every bucket's
    # read/write/fresh and assert it equals the raw row totals — catches a leak (tokens counted in
    # the wrong bucket) OR a double-add (a bucket summing a row twice). Uses DISTINCT read values so
    # a leak would change the totals (the hand-computed test's haiku read=0 could not).
    from apex_router.proxy_engine.readout.doctor import _fresh_input
    rows = [
        _gen("sx", _ANTHROPIC, cache_read=111, cache_write=11, input_uncached=1, output=5, model="opus"),
        _gen("sx", _ANTHROPIC, cache_read=222, cache_write=22, input_uncached=2, output=5, model="sonnet"),
        _gen("sx", _ANTHROPIC, cache_read=444, cache_write=44, input_uncached=4, output=5, model="haiku"),
    ]
    raw_read = sum(r["cache_read_tokens"] for r in rows)
    raw_write = sum(r["cache_write_tokens"] for r in rows)
    raw_fresh = sum(_fresh_input(r) for r in rows)
    health = session_health(rows)
    assert len(health) == 3, "three distinct models → three buckets"
    assert sum(h.sum_cache_read for h in health.values()) == raw_read == 111 + 222 + 444
    assert sum(h.sum_cache_write for h in health.values()) == raw_write == 11 + 22 + 44
    assert sum(h.sum_input_uncached for h in health.values()) == raw_fresh == 1 + 2 + 4


def test_headline_dollars_are_the_sum_of_per_bucket_savings():
    # cross-validation: pin the PRICING fix at the report level, not just the sums. The headline total
    # must equal the sum of each bucket priced at its OWN model — and pricing a mixed session by the
    # first turn's (cheap) model must UNDER-count. Two models with very different input rates.
    from apex_router.proxy_engine.readout.doctor import build_report, session_health
    rows = [
        # opus turns (input $15/M): big reads → big savings when priced as opus
        _gen("sx", _ANTHROPIC, cache_read=1_000_000, cache_write=0, input_uncached=10, output=5, model="opus"),
        _gen("sx", _ANTHROPIC, cache_read=1_000_000, cache_write=0, input_uncached=10, output=5, model="opus"),
        # a single cheap haiku turn FIRST-in-file would, pre-fix, price the whole session as haiku
        _gen("sx", _ANTHROPIC, cache_read=1_000, cache_write=0, input_uncached=10, output=5, model="haiku"),
    ]
    rep = build_report(rows)
    health = session_health(rows)
    # headline == Σ per-bucket dollars, each at its own regime
    per_bucket = sum(
        h.dollars_saved_vs_uncached(rates_for(h.model, h.endpoint_id)) for h in health.values()
    )
    assert abs(rep["totals"]["dollars_saved_by_cache"] - per_bucket) < 1e-9
    # and the opus reads are priced as OPUS ($13.5/M net), not haiku ($0.72/M net): the two opus
    # turns alone save 2_000_000·(15−1.5)/1e6 = 27.0, which a haiku-priced pool could never reach.
    assert rep["totals"]["dollars_saved_by_cache"] > 26.0, "opus reads must be priced as opus, not haiku"


def test_single_model_session_is_unchanged_by_the_split():
    # A session that never switches yields exactly ONE bucket — the fix is a no-op when no switch.
    rows = [
        _gen("s1", _ANTHROPIC, cache_read=100_000, cache_write=0, input_uncached=500, output=50, model="opus"),
        _gen("s1", _ANTHROPIC, cache_read=100_000, cache_write=0, input_uncached=500, output=50, model="opus"),
    ]
    health = session_health(rows)
    assert set(health.keys()) == {("s1", None, _ANTHROPIC, "opus")}
    assert health[("s1", None, _ANTHROPIC, "opus")].n_generative == 2


def test_no_session_id_branch_still_keys_by_endpoint_and_model():
    # Codex (no session id) was ALREADY per-model via the <no-session:endpoint:model> key. Guard it:
    # two models' unrelated no-session traffic must not merge.
    rows = [
        _gen(None, "openai", cache_read=0, cache_write=0, input_uncached=1_000, output=50, model="gpt-5"),
        _gen(None, "openai", cache_read=0, cache_write=0, input_uncached=1_000, output=50, model="o3"),
    ]
    health = session_health(rows)
    assert len(health) == 2, "two models' no-session traffic must stay in separate buckets"
    assert all(k.startswith("<no-session:openai:") for k in health)


def test_model_switch_cold_start_does_not_false_alarm_on_realistic_shape():
    # Ground-truth-derived (2026-07-25): real sessions are Opus-dominant + a TINY Haiku bucket. The
    # split-off Haiku bucket has served=0 but must NOT alarm — the existing guards absorb it
    # (n<2 cold-start exemption, or below _MIN_INPUT_FOR_ALARM). Splitting introduces ZERO new alarms.
    from apex_router.proxy_engine.readout.doctor import prefix_instability_alarm
    rates = rates_for("haiku", "anthropic")
    # Haiku bucket: 1 turn, 388 fresh tokens, 0 read → n<2 exemption
    haiku_1turn = session_health([
        _gen("s", _ANTHROPIC, cache_read=0, cache_write=0, input_uncached=388, output=10, model="haiku"),
    ])[("s", None, _ANTHROPIC, "haiku")]
    assert prefix_instability_alarm(haiku_1turn, rates) is None, "a lone cold-start turn must not alarm"
    # Haiku bucket: 2 turns but only 9_600 fresh — below the 50k min-cacheable floor
    haiku_2turn = session_health([
        _gen("s", _ANTHROPIC, cache_read=0, cache_write=0, input_uncached=4_800, output=10, model="haiku"),
        _gen("s", _ANTHROPIC, cache_read=0, cache_write=0, input_uncached=4_800, output=10, model="haiku"),
    ])[("s", None, _ANTHROPIC, "haiku")]
    assert prefix_instability_alarm(haiku_2turn, rates) is None, "a sub-min-prefix bucket must not alarm"


def test_large_second_model_bucket_alarms_the_documented_boundary():
    # cross-validation: the prior "boundary" test had NO first model (not a switch at all) — fixed here.
    # A real switch: opus turns FIRST (healthy, fully cached), THEN sonnet turns that are large and
    # cold (>=2 turns, >=50k fresh, <70% served). The sonnet bucket IS a real cold prefix and SHOULD
    # alarm; the opus bucket must NOT. This pins that splitting surfaces a genuine post-switch cold
    # prefix rather than masking it — and that a future cold-start exemption is a deliberate choice.
    from apex_router.proxy_engine.readout.doctor import prefix_instability_alarm
    rows = [
        # first model: opus, healthy (big reads, tiny fresh)
        _gen("s", _ANTHROPIC, cache_read=200_000, cache_write=0, input_uncached=100, output=50, model="opus"),
        _gen("s", _ANTHROPIC, cache_read=200_000, cache_write=0, input_uncached=100, output=50, model="opus"),
        # switched-to model: sonnet, large + cold (2 turns, 120k fresh, 0 read → served 0%)
        _gen("s", _ANTHROPIC, cache_read=0, cache_write=0, input_uncached=60_000, output=50, model="sonnet"),
        _gen("s", _ANTHROPIC, cache_read=0, cache_write=0, input_uncached=60_000, output=50, model="sonnet"),
    ]
    health = session_health(rows)
    opus = health[("s", None, _ANTHROPIC, "opus")]
    sonnet = health[("s", None, _ANTHROPIC, "sonnet")]
    assert prefix_instability_alarm(opus, rates_for("opus", "anthropic")) is None, \
        "the healthy first-model (opus) bucket must NOT alarm"
    alarm = prefix_instability_alarm(sonnet, rates_for("sonnet", "anthropic"))
    assert alarm is not None, "the large cold switched-to (sonnet) bucket SHOULD alarm (documented boundary)"
    assert alarm["cache_served"] == 0.0


# ---------- A2c: cache-HEALTH population keyed by agent_id ----------
# A (session, model) bucket pools the main thread + its sub-agents, which carry SEPARATE prefixes /
# caches. Cache-HEALTH (hit_rate, r:w, the alarm) must key by the full identity incl. agent_id so a
# metric names its exact prefix population. PRICING stays key-invariant (a linear sum → same total).

def test_health_splits_main_thread_from_subagent(tmp_path=None):
    # Same session + model, but one main turn (agent None) and one sub-agent turn (agent 'x') →
    # TWO health buckets, each summing only its own turns (separate caches).
    rows = [
        _gen("s", "anthropic", cache_read=100_000, cache_write=0, input_uncached=500, output=50, agent=None),
        _gen("s", "anthropic", cache_read=0,       cache_write=0, input_uncached=2_000, output=50, agent="sub1"),
    ]
    health = session_health(rows)
    assert len(health) == 2, "main thread and sub-agent must be separate health buckets"
    main = next(h for h in health.values() if h.agent_id is None)
    sub = next(h for h in health.values() if h.agent_id == "sub1")
    assert main.sum_cache_read == 100_000 and main.sum_input_uncached == 500
    assert sub.sum_cache_read == 0 and sub.sum_input_uncached == 2_000  # sub's cold turn not masked


def test_pricing_total_is_unchanged_by_agent_id_split():
    # A2c keys health by agent_id, which fragments the per-session ROWS — but the dollar TOTAL is
    # key-invariant (Σ read·Δ − Σ write·Δ is a linear sum). This pins that the headline holds.
    from apex_router.proxy_engine.readout.doctor import build_report
    rows = [
        _gen("s", "anthropic", cache_read=200_000, cache_write=0, input_uncached=100, output=50, agent=None),
        _gen("s", "anthropic", cache_read=200_000, cache_write=0, input_uncached=100, output=50, agent="sub1"),
        _gen("s", "anthropic", cache_read=200_000, cache_write=0, input_uncached=100, output=50, agent="sub2"),
    ]
    rep = build_report(rows)
    # three agent buckets, but the dollar total is the sum of all reads priced as opus — the same as
    # if they were one bucket. 600_000 read · (15−1.5)/1e6 = 8.10
    assert abs(rep["totals"]["dollars_saved_by_cache"] - 600_000 * (15 - 1.5) / 1e6) < 1e-9


def test_subagent_in_gray_band_does_not_alarm_but_is_not_masked():
    # Ground-truth-derived (2026-07-26): the ONE live sub-0.939 bucket is a sub-agent at served
    # 0.9332 (2 turns here reproducing the same served ratio). It sits in the 0.700–0.939 GRAY BAND:
    # below the historical healthy reference (0.939) but above the 0.700 ALARM floor → it must NOT
    # alarm, AND (post-A2c) it is no longer masked inside a warm main-thread bucket — it's its own row.
    from apex_router.proxy_engine.readout.doctor import prefix_instability_alarm
    rows = [
        _gen("s", "anthropic", cache_read=112_834, cache_write=0, input_uncached=8_076, output=50,
             agent="sub1"),
        _gen("s", "anthropic", cache_read=1, cache_write=0, input_uncached=1, output=1, agent="sub1"),
        # a warm main thread that WOULD have masked the sub-agent under the old (session,model) key
        _gen("s", "anthropic", cache_read=5_000_000, cache_write=0, input_uncached=100, output=50, agent=None),
    ]
    health = session_health(rows)
    sub = next(x for x in health.values() if x.agent_id == "sub1")
    served = sub.sum_cache_read / (sub.sum_cache_read + sub.sum_input_uncached)
    assert 0.70 < served < 0.939  # the gray band
    assert prefix_instability_alarm(sub, rates_for("sonnet", "anthropic")) is None, \
        "a gray-band sub-agent must NOT alarm (it's above the 0.700 floor)"
    # and it is a SEPARATE bucket from the warm main thread (no longer masked)
    assert sub is not next(x for x in health.values() if x.agent_id is None)


def test_alarm_floor_and_reference_are_decoupled_and_unchanged_by_a2c():
    # Codex A2c-F3/F4: the recalibration is DEFERRED. Pin BOTH constants unchanged AND that 0.939 is
    # only a HISTORICAL POOLED reference — the alarm fires on 0.700, not 0.939, so the two are
    # deliberately decoupled (a bucket in [0.700, 0.939) is neither alarmed nor asserted "healthy").
    from apex_router.proxy_engine.readout.doctor import CACHE_SERVED_ALARM_FLOOR, _HEALTHY_CACHE_SERVED_MIN
    assert CACHE_SERVED_ALARM_FLOOR == 0.700, "alarm floor is the trigger — must not move on n=1"
    assert _HEALTHY_CACHE_SERVED_MIN == 0.939, "0.939 stays a historical pooled reference, unrecalibrated"
    assert CACHE_SERVED_ALARM_FLOOR < _HEALTHY_CACHE_SERVED_MIN  # they are distinct by design


def test_no_session_branch_with_an_agent_id_does_not_mismerge():
    # Codex A2c-F5: the no-session key ignores agent_id (Codex traffic has none). Guard the assumption:
    # if a no-session row DID carry an agent, it must not silently merge two agents' unrelated traffic
    # under one endpoint:model bucket. (Documents/enforces the branch's contract.)
    rows = [
        _gen(None, "openai", cache_read=0, cache_write=0, input_uncached=1_000, output=50,
             model="gpt-5", agent=None),
        _gen(None, "openai", cache_read=0, cache_write=0, input_uncached=1_000, output=50,
             model="gpt-5", agent="ghost"),
    ]
    health = session_health(rows)
    # Both are no-session/openai/gpt-5. Today they share ONE bucket (agent_id not in the no-session
    # key). This test PINS that current behavior so a future agent-carrying no-session wire is a
    # deliberate re-key, not a silent merge discovered in production.
    assert len(health) == 1, "documented: no-session key is (endpoint, model) — agent not yet keyed here"
    assert all(k.startswith("<no-session:openai:") for k in health)


def test_agent_id_emitted_in_report_rows_and_alarm(tmp_path=None):
    # Codex A2c-F1: agent_id must appear in the report row (else two sub-agents render identically).
    from apex_router.proxy_engine.readout.doctor import build_report
    rows = [
        _gen("s", "anthropic", cache_read=100_000, cache_write=0, input_uncached=500, output=50, agent=None),
        _gen("s", "anthropic", cache_read=0, cache_write=0, input_uncached=500, output=50, agent="sub1"),
    ]
    rep = build_report(rows)
    agents = {r["agent_id"] for r in rep["sessions"]}
    assert agents == {None, "sub1"}, "each report row must carry its agent_id (main vs sub-agent)"


# ---------- A2b: error / timeout panel ----------
# No HTTP status is emitted, so failures classify by OBSERVED signature — NOT a cause (Codex xval
# 2026-07-25: `tokens_in==0` is the pre-dispatch DEFAULT, not evidence of "no dispatch"; the fields
# cannot attribute WHY). Classes are observed-only: upstream_timeout / stream_failed / slow_first_byte
# / no_usage_captured. The only alarm is a PER-ENDPOINT burst detector (>= K timeouts in W min on one
# endpoint), not a rate floor — a lone ~600s timeout on a long agentic turn is compulsory, not a fault.

def _err(ts=1000.0, wait=0.0, ttft=0.0, model="<gateway>-claude-opus-x", endpoint="anthropic"):
    return {"schema_version": 4, "is_error": True, "usage": None, "tokens_out": 0,
            "ts": ts, "upstream_error_wait_ms": wait, "ttft_ms": ttft,
            "model_resolved": model, "endpoint_id": endpoint}


def test_error_taxonomy_is_observed_only_no_causal_inference():
    from apex_router.proxy_engine.readout.doctor import classify_error
    assert classify_error(_err(wait=600_000)) == "upstream_timeout"       # blocked >300s then raised
    # a first byte arrived, THEN it errored → stream_failed (covers mid-stream timeouts, wait=0)
    assert classify_error(_err(wait=0, ttft=1_200)) == "stream_failed"
    assert classify_error(_err(wait=0, ttft=0, model="x") | {"t_upstream_ttfb_ms": 800}) == "stream_failed"
    # long wait for a first byte that never came → slow_first_byte
    assert classify_error(_err(wait=90_000, ttft=0)) == "slow_first_byte"
    # no wait signal, no first byte → no_usage_captured (a DESCRIPTION, not "client abort")
    assert classify_error(_err(wait=0, ttft=0)) == "no_usage_captured"


def test_tokens_in_zero_is_not_treated_as_a_signal():
    # cross-validation: tokens_in defaults to 0 pre-dispatch, so it must NOT drive classification. Two
    # rows identical except tokens_in must classify the same.
    from apex_router.proxy_engine.readout.doctor import classify_error
    a = _err(wait=600_000); a["tokens_in"] = 0
    b = _err(wait=600_000); b["tokens_in"] = 5000
    assert classify_error(a) == classify_error(b) == "upstream_timeout"


def test_error_panel_counts_rate_and_latency_hours_not_dollars():
    from apex_router.proxy_engine.readout.doctor import error_panel
    errs = [_err(wait=600_000), _err(wait=600_000), _err(wait=0, ttft=0)]  # 2 timeout, 1 no_usage
    panel = error_panel(errs, total_requests=100)
    assert panel["total"] == 3
    assert panel["denominator"] == 100
    assert panel["rate"] == 3 / 100
    assert panel["by_class"] == {"upstream_timeout": 2, "no_usage_captured": 1}
    # 2 × 600s = 1200s = 0.333h — reported as LATENCY hours, never a dollar figure
    assert panel["timeout"]["count"] == 2
    assert panel["timeout"]["cumulative_wait_hours"] == round(1_200_000 / 1000 / 3600, 1)


def test_error_panel_survives_all_optional_fields_null():
    from apex_router.proxy_engine.readout.doctor import classify_error, error_panel
    bare = {"schema_version": 4, "is_error": True, "usage": None}
    assert classify_error(bare) == "no_usage_captured"  # observed-only; no crash, no cause claim
    panel = error_panel([bare], total_requests=1)
    assert panel["total"] == 1 and panel["timeout"]["count"] == 0


def test_burst_alarm_is_per_endpoint_not_cross_endpoint():
    # cross-validation: 5 timeouts SPLIT across anthropic+openai are NOT one backend degrading → no alarm.
    from apex_router.proxy_engine.readout.doctor import timeout_burst_alarm
    mixed = ([_err(ts=1000.0 + i * 30, wait=600_000, endpoint="anthropic") for i in range(3)]
             + [_err(ts=1000.0 + i * 30, wait=600_000, endpoint="openai") for i in range(3)])
    assert timeout_burst_alarm(mixed) is None, "a cross-endpoint mix must not trip a per-endpoint burst"
    # but 5 on ONE endpoint within the window DOES burst, and names that endpoint
    same = [_err(ts=1000.0 + i * 30, wait=600_000, endpoint="anthropic") for i in range(5)]
    alarm = timeout_burst_alarm(same)
    assert alarm is not None and alarm["endpoint"] == "anthropic"
    assert "anthropic" in alarm["cause"] and alarm["timeouts_in_window"] >= 5


def test_burst_boundary_k_minus_one_does_not_fire():
    # Threshold boundary (Codex: tests must exercise the edge). K=5, so exactly 4 on one endpoint in
    # the window must NOT fire (the observed non-bursty ceiling was 4); the 5th fires it.
    from apex_router.proxy_engine.readout.doctor import timeout_burst_alarm
    four = [_err(ts=1000.0 + i * 30, wait=600_000, endpoint="anthropic") for i in range(4)]
    assert timeout_burst_alarm(four) is None, "K-1=4 in a window must not alarm (the observed ceiling)"
    five = four + [_err(ts=1000.0 + 4 * 30, wait=600_000, endpoint="anthropic")]
    assert timeout_burst_alarm(five) is not None, "the K-th timeout in the window must fire"


def test_scattered_timeouts_do_not_burst_alarm_ground_truth_shape():
    # Ground-truth-derived (2026-07-25): 41 live timeouts, max 4 in any 10-min window (scattered).
    # The same count spread far apart must NOT alarm — the burst catches the NEXT degradation, not
    # today's normal long-turn timeouts.
    from apex_router.proxy_engine.readout.doctor import timeout_burst_alarm
    scattered = [_err(ts=1000.0 + i * 3600, wait=600_000) for i in range(41)]  # one per hour
    assert timeout_burst_alarm(scattered) is None, "scattered timeouts must not trip the burst alarm"


def test_error_panel_wired_into_build_report_and_renders():
    from apex_router.proxy_engine.readout.doctor import build_report, format_report
    rows = [
        _gen("s1", "anthropic", 100_000, 0, 100, 50),               # a healthy generative turn
        _err(ts=1000.0, wait=600_000, model="<gateway>-claude-opus-x"),   # 1 timeout
        _err(ts=1001.0, wait=0, ttft=0),                          # 1 no_usage_captured
    ]
    rep = build_report(rows)
    assert rep["errors"]["total"] == 2
    assert rep["errors"]["by_class"]["upstream_timeout"] == 1
    text = format_report(rep)
    assert "ERRORS (this window)" in text
    assert "latency, not $" in text          # the wait is labeled latency, never dollars
    assert "no burst alarm" in text          # 2 timeouts → no burst
    # the panel must NOT contain a causal claim we can't support
    assert "never reached upstream" not in text


# ---------- A1': cold-turn OBSERVATION (measure-only, no cause attributed) ----------
# A cold turn's CAUSE is not identifiable measure-only (cross-validation-07-25): a >=TTL gap only
# proves eviction was POSSIBLE, not that it happened (an edit in the same gap is identical on the
# wire). So this is purely observational: cold_after_ttl_gap vs cold_no_ttl_gap vs first_observed_cold.
# Keyed by the FULL cache identity incl. agent_id (sub-agents carry separate prefixes).

def _cold_gen(session, ts, cache_read, model="opus", endpoint="anthropic", agent=None):
    d = _gen(session, endpoint, cache_read=cache_read, cache_write=0, input_uncached=1_000,
             output=50, model=model)
    d["ts"] = ts
    d["agent_id"] = agent
    return d


def test_cold_after_ttl_gap_is_observational_not_benign():
    from apex_router.proxy_engine.readout.doctor import _TTL_S, cold_turn_attribution
    rows = [
        _cold_gen("s", ts=0.0, cache_read=100_000),          # warm
        _cold_gen("s", ts=_TTL_S + 60.0, cache_read=0),      # cold, gap 360s >= 300 → cold_after_ttl_gap
    ]
    att = cold_turn_attribution(rows)
    assert att["cold_after_ttl_gap"] == 1
    assert att["cold_no_ttl_gap"] == 0
    assert att["warm"] == 1
    assert att["first_observed_cold"] == 0


def test_ttl_boundary_is_inclusive_at_exactly_300s():
    # Codex #6: exercise the exact boundary. gap == _TTL_S → cold_after_ttl_gap (>= is inclusive);
    # one microsecond less → cold_no_ttl_gap.
    from apex_router.proxy_engine.readout.doctor import _TTL_S, cold_turn_attribution
    at = cold_turn_attribution([_cold_gen("s", 0.0, 100_000), _cold_gen("s", _TTL_S, 0)])
    assert at["cold_after_ttl_gap"] == 1 and at["cold_no_ttl_gap"] == 0
    below = cold_turn_attribution([_cold_gen("s", 0.0, 100_000), _cold_gen("s", _TTL_S - 0.001, 0)])
    assert below["cold_no_ttl_gap"] == 1 and below["cold_after_ttl_gap"] == 0


def test_cold_no_ttl_gap_when_gap_below_ttl():
    from apex_router.proxy_engine.readout.doctor import cold_turn_attribution
    rows = [_cold_gen("s", 0.0, 100_000), _cold_gen("s", 30.0, 0)]  # gap 30s < 300
    att = cold_turn_attribution(rows)
    assert att["cold_no_ttl_gap"] == 1 and att["cold_after_ttl_gap"] == 0


def test_first_observed_cold_is_not_a_gap_class():
    from apex_router.proxy_engine.readout.doctor import cold_turn_attribution
    att = cold_turn_attribution([_cold_gen("s", 0.0, 0)])
    assert att["first_observed_cold"] == 1
    assert att["cold_after_ttl_gap"] == 0 and att["cold_no_ttl_gap"] == 0


def test_agent_id_interleaving_does_not_corrupt_the_gap():
    # cross-validation (verified counterexample): without agent_id in the key, agent B's OWN first cold
    # turn is mislabeled cold_after_ttl_gap using agent A's unrelated earlier timestamp. With agent_id
    # in the key, B's cold turn is its own first_observed_cold — no cross-agent gap.
    from apex_router.proxy_engine.readout.doctor import cold_turn_attribution
    rows = [
        _cold_gen("s", ts=0.0, cache_read=100_000, agent="A"),  # agent A warm
        _cold_gen("s", ts=360.0, cache_read=0, agent="B"),      # agent B's FIRST turn, cold
    ]
    att = cold_turn_attribution(rows)
    assert att["first_observed_cold"] == 1, "B's own first cold turn must not borrow A's timeline"
    assert att["cold_after_ttl_gap"] == 0, "must NOT be labeled ttl-gap using another agent's ts"
    # and a REAL within-agent >=TTL gap still classifies as cold_after_ttl_gap
    same_agent = cold_turn_attribution([
        _cold_gen("s", ts=0.0, cache_read=100_000, agent="A"),
        _cold_gen("s", ts=400.0, cache_read=0, agent="A"),
    ])
    assert same_agent["cold_after_ttl_gap"] == 1


def test_no_session_rows_are_counted_not_silently_dropped():
    # Codex #5: no-session traffic (Codex/openai) is skipped from gap analysis but COUNTED.
    from apex_router.proxy_engine.readout.doctor import cold_turn_attribution
    rows = [_cold_gen(None, ts=0.0, cache_read=0, endpoint="openai", model="gpt-5")]
    att = cold_turn_attribution(rows)
    assert att["skipped_no_session"] == 1
    assert att["first_observed_cold"] == 0  # not classified, only counted as skipped


def test_ttl_constant_matches_cachesim_authority():
    from apex_router.proxy_engine.readout.doctor import _TTL_S
    from apex_router.proxy_engine.tuner.cachesim import Pricing
    assert _TTL_S == Pricing().ttl_s


def test_cold_observation_renders_without_a_causal_or_benign_claim():
    from apex_router.proxy_engine.readout.doctor import _TTL_S, build_report, format_report
    rows = [
        _cold_gen("s", ts=0.0, cache_read=100_000),
        _cold_gen("s", ts=_TTL_S + 100.0, cache_read=0),     # cold_after_ttl_gap
        _cold_gen("s", ts=_TTL_S + 130.0, cache_read=0),     # cold_no_ttl_gap (30s after prev)
    ]
    rep = build_report(rows)
    assert rep["cold_turns"]["cold_after_ttl_gap"] == 1
    assert rep["cold_turns"]["cold_no_ttl_gap"] == 1
    text = format_report(rep)
    assert "cold prefixes" in text
    # the render must NOT launder a cause or a verdict (Codex xval)
    assert "benign" not in text
    assert "cause NOT attributable" in text


def test_alarms_still_render_when_there_are_no_cold_turns():
    # cross-validation (verified regression): the cold-prefix block must NOT gate the ALARMS block. A
    # session that alarms but has no cold turns must still print ALARMS.
    from apex_router.proxy_engine.readout.doctor import build_report, format_report
    # a large, low-served, multi-turn openai session → alarms; both turns warm (cache_read>0) so no
    # cold_turns fire the new block.
    rows = [
        _gen("s", "openai", cache_read=20_000, cache_write=0, input_uncached=200_000, output=50, model="gpt-5"),
        _gen("s", "openai", cache_read=20_000, cache_write=0, input_uncached=200_000, output=50, model="gpt-5"),
    ]
    rep = build_report(rows)
    assert rep["alarms"], "fixture must alarm"
    text = format_report(rep)
    assert "ALARMS" in text, "alarms must render even when there are no cold turns (regression guard)"


# ---------- report assembly: text renders, JSON round-trips, text and JSON agree ----------

def test_build_and_format_report_end_to_end():
    import json

    from apex_router.proxy_engine.readout.doctor import build_report, format_report
    # NOTE `_gen`'s input arg is FRESH for anthropic but TOTAL for openai (the wire asymmetry): on the
    # openai rows below tokens_in=200_000 is the total prompt and cache_read=20_000 the cached subset
    # → fresh=180_000, served=10% → alarm (needs >= 2 turns, so two rows).
    rows = [
        _gen("s1", "anthropic", 200_000, 500, 1_000, 50),
        _gen("s1", "anthropic", 300_000, 500, 1_000, 50),
        # a large, low-cache, MULTI-TURN openai session → should alarm (prefix instability)
        _gen("s2", "openai", 20_000, 0, 200_000, 400, model="gpt-5"),
        _gen("s2", "openai", 20_000, 0, 200_000, 400, model="gpt-5"),
    ]
    rep = build_report(rows)
    # JSON round-trips (the --json payload is serializable)
    assert json.loads(json.dumps(rep, default=str))
    # text renders without error and contains the headline + the alarm
    text = format_report(rep)
    assert "cache SAVED you" in text
    assert rep["alarms"], "the large low-cache multi-turn openai session must raise an alarm"
    assert "recoverable" in text.lower()  # upper-bound wording, not "OVERPAY" (F2)
    # denominators are stated (estimand discipline)
    assert rep["denominators"]["generative_turns"] == 4
    # the population label travels: window row-count present in dict and printed in text
    assert rep["window"]["rows"] == 4
    assert "window:" in text
