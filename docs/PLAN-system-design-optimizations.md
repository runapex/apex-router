# Plan: system-design principles → apex-router optimizations

**Status:** plan only, nothing implemented. **Date:** 2026-10-06.
**Source:** a local 105-page system-design study guide (observability, SLOs, queueing, caching,
resilience; its running example is an Apex-style proxy). **Evidence:** proxy telemetry 2026-09-27 → 2026-10-06, ~14.8k calls.
Numbers in the baseline table come from `python3 scripts/sd_baseline.py` (lines B1–B6, S); re-run it
before and after each item. Numbers marked *(one-off)* came from ad-hoc runs not in that script;
*(estimate)* marks a counterfactual.

Scope: apex-router is a **single-process proxy on one machine, one user, one upstream per wire**.
The PDF's fleet-scale material (quorum, fencing, DRR across tenants, fleet histograms) is mostly a
design target for a multi-tenant Apex, not for this box. Items that don't fit are listed under
*Not adopting*, with the reason, instead of being forced in.

## The PDF's rules that bite here

| PDF rule (section) | What it says about apex-router |
|---|---|
| Tails hide in averages; p99/mean > 10 = suspect two populations (§1, telemetry map) | claude-code cost p99/mean 12.8×; removing the 3% of calls that write > 20k cache tokens drops Cs² from 3.88 to 0.40 — a separate population, consistent with cold-cache rewrites |
| Validate the signal, locate the population, follow the path (§Observability 1) | `apex_added_ms` is one number; the slow path can't be located from it |
| Timeouts the backend never hears about; deadlines; breakers (resilience) | outage calls wait up to 30 s to fail; stalled streams up to 10 min |
| Retry at one layer; attempts per request > 1.1 = warning (§3, tier-0) | proxy retries connect + fast-fail body-sent errors *and* both clients retry; attempts aren't counted |
| Burn rate over two windows; failures are not Poisson (§4, thinning trap) | pressure uses one 15-min window; failures cluster (dispersion 2.8 after adjusting for traffic per minute) |
| Cold cache = 50× the steady state (§6) | 33.4% of claude-code cost is cache writes; 83% of tokens in > 20k-token writes follow a 5–60 min idle gap |
| Little's law needs W (§Queueing) | rows carry ttft but no total duration: in-flight and occupancy can't be computed |
| Keep telemetry off the critical path (§Observability walkthroughs) | `emit()` appends synchronously on the event loop; rows average 8.2 KB |

## Baseline (from `scripts/sd_baseline.py`)

| Line | Measure | Value |
|---|---|---|
| B1 | `apex_added_ms` p50 / p95 / p99; share over the 20 ms budget | 20.1 / 100.5 / 286.5 ms; **50.2%** |
| B1 | by path: claude-code `client_edit` · `new` · codex | p50 33.9 · 3.5 · 16.0 ms; p95 216.3 · 61.5 · 44.1 ms |
| B1 | `client_edit` p50, isolated (no call within ±60 s) vs busy (20+) | 60.8 ms (n=48) vs 28.4 ms (n=1,298) |
| B2 | time to fail: ConnectError p95 · ReadError p50/p95 · ReadTimeout p50 | 12.6 s · 36.7/363.8 s · 603 s |
| B2 | calls rescued by a proxy transport retry (connect or fast-fail body-sent; the row doesn't say which) | **2** of ~14.8k |
| B2 | connect failures that exhausted both retries | 68 of 68 |
| B2 | longest run of consecutive failed codex calls (10-03/04 outage) | 65 |
| B3 | dispersion of errors per active minute: raw / adjusted for calls per minute (independent = 1) | 2.6 / 2.8 |
| B4 | claude-code cost units in cache writes | **33.4%** |
| B4 | tokens in > 20k-token cache writes that followed a 5–60 min idle gap (successful calls) | 83% |
| B4 | claude-code cost Cs²: all calls / without the 3.0% > 20k-token writers | 3.88 / 0.40 |
| B5 | telemetry row size mean / p99 | 8,211 / 32,226 bytes |
| B6 | fields not recorded | stage timings, total duration, max chunk gap, retry linkage |
| S | sessions with an id: count · calls mean/median · completion · failed calls carrying an id | 88 · 51/1 · 99.75% · 11 |
| S | `http_429` rows · rows per day | 0 · ~1,560 |

Two checks on B1 *(one-off)*. A synthetic microbench — 400 messages, up to 822 KB, both `extend`
and an edited last turn — took matcher 2–5 ms and shadow ≤ 2 ms. The store calls replayed on a
copy of the live `state.db` took < 0.5 ms. And isolated `client_edit` requests are **slower** than
busy ones (B1 row 3). The synthetic bodies are not the live ones, and the neighbour count is a
rough proxy for concurrency, so neither check excludes the benchmarked code or contention. Both
point away from those and toward something like a cold path after idle, and that is unproven.
That is why P0-1 measures before anything is changed.

## Plan, in priority order

### P0-1 Per-stage timings for the pre-forward path (measure before fixing)
- **Rule:** validate the signal, then follow the path stage by stage.
- **Change:** add `stage_ms` to the telemetry event: `{read_body, parse, matcher, shadow, prepare,
  auth}` (perf_counter deltas, already taken around each call), plus `loop_lag_ms` from a 100 ms
  lifespan ticker (scheduled vs actual wake). Schema v10; `doctor.SUPPORTED_SCHEMA` and the
  telemetry-contract test move with it (memory: the v9 bump needed exactly these two).
- **Cost/risk:** ~60 bytes/row; no behaviour change.
- **Validate:** after 2–3 days, B1 decomposes: the stages sum to `apex_added_ms` within 1 ms on
  ≥ 99% of rows, and one stage (or loop lag) explains the `client_edit` p95.
- **Then decide:** fix that stage, or move the 20 ms budget to a measured p99 + margin (the option
  `config.py` already names). Don't touch the matcher or shadow before this data exists.

### P0-2 Fail fast during an upstream outage (circuit breaker on the connect path)
- **Rule:** the open state removes load from a drowning dependency; ratio over a window with a
  minimum count; probe a few with jitter; expose state as a metric.
- **Evidence:** every connect failure exhausted both retries (B2) and cost up to 30 s; at most 2
  calls in the whole window were rescued by any proxy transport retry. The outage produced a
  65-call failure run.
- **Change:** a per-endpoint breaker in `Upstream.send_stream`. It opens when the last 30 s hold
  ≥ 5 connect-phase failures and ≥ 50% of attempts failed. While open it returns 503 at once with
  `retry-after`. After 10 s ± jitter it goes half-open and lets 2 probes through. It counts
  `ConnectError`/`ConnectTimeout` only — those never reached the upstream. ReadError and 4xx don't
  count. Telemetry: `breaker_state`, new cause `breaker_open`. In `pressure._classify`, map
  `breaker_open` → `transport`.
- **Retries:** `APEX_CONNECT_RETRIES` budgets *both* retry classes in `send_stream`: pre-write
  connect errors and fast-fail body-sent errors. The second class exists for a measured
  dead-keep-alive failure (`upstream.py:20-48`), so don't cut the shared knob. First split the
  counter (`connect_retries` vs `body_sent_retries` in telemetry) so B2 can attribute rescues to a
  class. Then lower only the connect budget, if connect rescues stay ~0. One layer should retry,
  and both clients already do.
- **Cost/risk:** a false open fails calls that would have succeeded. Bound it with the min-count
  floor and the short cooldown. All traffic to that wire shares the proxy, so a false open hits
  every caller.
- **Validate:** replay the 10-03/04 rows through the breaker state machine as a unit-test fixture.
  Time-to-fail for ConnectError should drop from p95 12.6 s to < 0.1 s once open, with no open
  state on the 09-27 → 10-02 baseline (zero false opens).

### P1-3 The cold-cache tail: price a 1-hour cache TTL before touching bytes
- **Rule:** cold cache = 50× steady state; measure the miss cost, then choose TTL.
- **Evidence:** B4. *(estimate)* Rough counterfactual: re-read the > 20k-token writes that followed
  a 5–60 min gap, and charge every other write at the 1-hour rate (2.0× vs 1.25×). That gives
  **−15% of claude-code cost units**. It's crude: it assumes the whole written span would still be
  a reusable prefix, which B4 doesn't establish. cachesim (step 1) is the real test.
- **Change, in order:**
  1. Run `proxy_engine/tuner/cachesim.py` (it already models TTL, `ttl_s=300`) at 300 s vs 3600 s
     with per-TTL write pricing. That replaces the −15% estimate.
  2. Check that the Azure gateway accepts `cache_control.ttl="1h"`, and whether Claude Code can
     set it itself. A client setting beats a proxy edit.
  3. Only if (1) holds and (2) needs the proxy: add an opt-in `APEX_CLAUDE_CACHE_TTL=1h`. It would
     set the TTL on the **last stable breakpoint only**, following the opt-in, measured-separately
     precedent of `APEX_CODEX_CACHE_KEY`.
- **Cost/risk:** this mutates request bytes, which is an exception to the no-mutation decision
  (record it there, as the cache-key change did). 1-hour writes cost 60% more, so a session that
  never idles 5–60 min pays more.
- **Validate:** B4 rewrite share falls and claude-code cost units per call fall in the week after,
  compared with this baseline at the same session-length mix.

### P1-4 SLO + two-window burn rate inside `pressure`
- **Rule:** define the SLI on the outcome, page on a fast short window *and* a sustained long
  window, require a minimum count.
- **Evidence:** a single 15-min window over clustered failures (B3 = 2.6) is the case the PDF warns
  about. One window either flaps on a burst or reacts late.
- **Change:**
  - SLI = calls that completed (no transport, 5xx or `midstream_*` cause) ÷ eligible calls.
    Eligible excludes 4xx client errors.
  - SLO = 99%: the per-session-row rate is 99.6% and the overall rate 98.1%, so 99% is realistic.
  - Burn = bad fraction ÷ 0.01, computed over 5 min and 1 h with prefix sums (PDF drill s11).
  - Mapping: **additive only**. Every current trigger keeps its level:
    - a fresh retry-after → RED, with no sample floor (`pressure.py:251`)
    - a 429 or transport rate above the red ceiling → RED
    - at or above the amber floors → AMBER

    The burn windows can only *raise* a level: both burning > 14× → RED, both > 6× → AMBER.
  - The CLI, exit codes and the advice text stay the same.
- **Cost/risk:** more AMBER/RED than today (never less), so fan-outs get shed more often. The
  model-routing skill quotes the advice text verbatim, so the text stays.
- **Validate:** replay the 9 days of telemetry through old and new classifiers. Compare the number
  of level changes per day (flapping) and the lead time to RED on 10-03.

### P1-5 Record stream duration and the longest silent gap, then set a real idle timeout
- **Rule:** Little's law needs W; deadlines should match the work.
- **Evidence:** ReadTimeout fails at 603 s; ReadError p95 at 364 s (B2). httpx `read=600` is
  already a per-read idle timeout. Nothing records the largest normal gap between chunks, so a
  lower value can't be justified yet.
- **Change:** add `t_total_ms` (arrival → last byte), `bytes_out` and `max_chunk_gap_ms` to the
  event (measured in `body_stream`, no extra I/O). After a week, set `APEX_READ_TIMEOUT` to the
  p99.9 gap of successful streams × 2, by wire. Keep long-thinking models in mind: Anthropic sends
  pings, but check, don't assume.
- **Validate:** in-flight L = λ·W becomes computable for the pressure note. Time-to-fail for
  stalled streams drops from 600 s to the new bound, and successful streams never exceed it.

### P2-6 Telemetry off the hot path and lighter
- **Rule:** telemetry must fail open and never block the work it observes.
- **Change:**
  - `emit()` puts the row on a bounded `queue.Queue(10_000)`. One writer thread appends in
    batches; when the queue is full it counts drops (`dropped_rows` in the heartbeat).
  - Shrink `shadow.blocks`. R1 (`analytics/r1.py`) reads only the aggregate
    `shadow.bytes_by_class`. The tuner's json/xl watch (`tuner/readout.py:267`) iterates the
    per-block list, and silently reports zero when it's missing. So the order is:
    1. emit a `cells` aggregate (count, bytes_saved and emit count per cell) beside the blocks;
    2. switch `json_xl_watch` to it, and make it report "unavailable" rather than 0 when neither is
       present;
    3. only then drop the per-block list by default (`APEX_TELEMETRY_BLOCKS=1` keeps it).
- **Validate:** B5 row size mean falls from 8.2 KB to < 2 KB, P0-1's `loop_lag_ms` p99 doesn't
  rise, `dropped_rows` = 0 in normal use, and the dashboard and zeno run faster.

### P2-7 Count attempts per logical request
- **Rule:** attempts per logical request above ~1.1 during an incident is a metastable-failure warning.
- **Change:** `attempt_of`: link a request to a failed call made within 60 s with the same
  fingerprint. The fingerprint differs by wire:
  - **claude-code:** the matcher's message-hash chain and turn, already computed.
  - **codex:** its body has `input`, not `messages`, so the matcher never runs (`wire.py:32`). Use
    the head hash `cache_key.py` already derives (model, instructions, tools, first inputs) plus
    the input length. This part needs design. Without it, the codex outage — where the failures
    are — stays invisible.

  Report attempts per logical request in `zeno report` and the dashboard.
- **Validate:** on a replayed outage window, attempts per request > 1 becomes visible for both
  wires. Today it can't be measured: only 11 failed calls carry a session id.

### P2-8 Fan-out risk in the pressure note
- **Rule:** P(at least one of n fails) = 1 − pⁿ; fan-out turns a rare tail into a common one.
- **Change:** pressure's Agent/Workflow note adds one line. It uses the current per-call p (from
  P1-4's window) and the measured calls-per-session distribution (mean 51, median 1 — one number
  would mislead): "at this rate, n parallel
  subagents of that length: X% see at least one failed call". Advice only; it never blocks.
- **Validate:** unit test of the formula; the number matches `zeno horizon` for the same p.

## Order and dependencies

```
P0-1 stage timings ──► (decide B1 fix or budget)        P0-2 breaker (independent)
P1-5 duration/gap  ──► idle timeout ──► Little's L in P2-8
P1-4 burn rate  ──► P2-8 fan-out note
P1-3 cachesim ──► gateway check ──► (opt-in TTL)        P2-6 telemetry writer ── after P0-1 (loop lag shows the effect)
P2-7 attempts (independent)
```

Schema changes (P0-1, P1-5, P2-7) belong in **one** v10 bump. That's one doctor/contract-test
update, not three.

## Not adopting (and why)

| PDF primitive | Why not here |
|---|---|
| Quorum, Raft, fencing tokens, outbox | one process, one local SQLite in WAL; nothing is replicated or handed between leaders |
| DRR / per-tenant fair queues / token bucket admission | one user; 0 `http_429` rows in the window, so upstream limits are not binding |
| Hedged requests (a parallel duplicate after a deadline) | an LLM POST isn't idempotent or cheap: a hedge pays the tokens twice and can produce two completions. Sequential retries are a different tool; P0-2 handles those |
| Single-flight | no shared cacheable computation in the proxy; the az token mint is already single-flight under a lock (`az_auth.get_token`) |
| P2C + EWMA backend choice | one upstream per wire; xval's arm selection already uses P2C |
| Fleet histograms instead of raw rows | single instance, ~1,560 rows/day: exact nearest-rank on raw rows is cheaper and more precise. Revisit only if the proxy serves a team |
| Kurtosis alarms | the PDF's own conclusion: alert on the threshold fraction (P1-4), use shape only to diagnose |

## Re-measure

```bash
python3 scripts/sd_baseline.py            # B1–B6, human-readable
python3 scripts/sd_baseline.py --json     # for before/after diffs
python3 scripts/telemetry_dashboard.py    # the same data as plots: mean vs p95/p99 tails
```
