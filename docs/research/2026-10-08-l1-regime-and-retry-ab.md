# L1: regime model (E2) and the retry-action A/B instrument (E1)

**Date:** 2026-10-08. **Map:** `RESEARCH-MAP.md` §3.2 and §4 item 1. **Code:** `src/apex_router/regime.py`
(`zeno report` section 1c), `proxy_engine/proxy/upstream.py` (`retry_assignment`), telemetry v11,
`src/apex_router/retry_ab.py` (`apex-router retry-ab report`).

## Question

The burst chain (zeno 1b) has a memory of one call. The map said a slower, day-level effect also exists,
which neither p^n nor the chain can fit. Two open items:

1. Is there a hidden **regime** (normal vs degraded upstream) in the proxy's call stream, and does modelling
   it predict held-out sessions better than p^n and the chain?
2. The chain suggests waiting before a retry rather than retrying at once. That claim is about an action, so
   only an A/B on the retry action can test it. Build the instrument without changing the default behaviour.

## 1. Regime model

**Models.**
- Plain 2-state HMM: hidden normal/degraded state, one fail probability per state, sticky transitions.
  Fitted by Baum–Welch on the time-ordered stream of all proxy calls (sessions and session-less rows
  together, because a regime belongs to a point in time, not to a session). Three fixed restarts, MAP
  pseudo-count 0.5, tolerance 1e-6.
- HMM with a burst chain per state: the fail probability depends on the state and on whether the previous
  call failed. The chain explains the bursts, so the hidden state is free to explain slower shifts.
- Day mixture as the EM-free baseline: a day counts as degraded when its Wilson lower bound is above the
  pooled rate. P(clean) = (1−w)(1−f_N)^n + w(1−f_D)^n.

**Verdict rule.** A model is `insufficient` below 200 consecutive call pairs or with no failures. Otherwise
it needs ΔBIC above 10 against the matching memoryless model (one Bernoulli rate for the plain HMM, one
burst chain for the burst variant). A fit that clears the bar but whose degraded state lasts fewer than 30
calls on average is reported as `bursts only`: that is the chain again, not a regime.

**Held-out protocol.** Same split as `markov.holdout`: sessions in order of first appearance, the first 70%
for training. iid and the chain are fitted on the training sessions, exactly as in 1b, and the code checks
that the numbers match. The HMM and the day mixture are fitted only on calls made before the first test
session starts. Each of the 41 test sessions is then scored on P(clean), by log-likelihood and Brier.
`*_filtered` is the version a router would actually run online: it filters the regime causally over every
call made before the session starts. That gives it more information than iid or the chain, and it is
labelled as such. Differences are paired per session, with a session-bootstrap 95% CI (2,000 resamples,
seed 0).

**Live result** (`zeno report`, worktree, 2026-10-07 ~20:40 PDT, read-only):

```
1c. horizon — regime model (hidden normal/degraded upstream state; 2-state HMM)
  plain HMM (one fail rate per state) — 28234 calls in time order, 23 EM iterations; ΔBIC vs memoryless 1903 → bursts only
    fail rate normal 0.64% vs degraded 82.69%
    stationary share degraded 2.8%; mean dwell 111 calls normal / 3.2 calls degraded; Viterbi: 667/28234 calls degraded in 155 episodes
  HMM + per-state burst chain (fail rate | state, previous call) — 28234 calls in time order, 72 EM iterations; ΔBIC vs memoryless 155 → regimes found
    fail rate normal 0.13% vs degraded 2.91% (after an ok call); after a failure 7.35% / 47.00%
    stationary share degraded 55.6%; mean dwell 340 calls normal / 425.6 calls degraded; Viterbi: 17327/28234 calls degraded in 5 episodes
    Viterbi-decoded fail rate normal 0.30% [0.22%, 0.42%] vs degraded 4.63% [4.33%, 4.96%] (conditional on the fit)
    days decoded degraded (≥ 50% of the day's calls): 2026-08-24, 2026-08-29, 2026-08-30, 2026-08-31, 2026-09-06, 2026-09-07, 2026-09-19, 2026-09-25, 2026-09-27, 2026-09-28, 2026-09-29, 2026-09-30, 2026-10-01, 2026-10-02
    partly degraded (5–50%): 2026-08-23 39%, 2026-10-05 21%, 2026-10-06 41%, 2026-10-07 38%
  day mixture: 6/26 days degraded, 52% of calls; fail 0.90% vs 4.84%
  held out (cut 2026-10-02 17:57; 94 train / 41 test sessions), Brier (vs iid | vs chain):
    iid 0.162 · markov 0.116 (+0.046 [+0.023, +0.071]) · day_mixture 0.157 (vs chain −0.041)
    hmm 0.113 (vs chain +0.003 [−0.001, +0.007]) · hmm_filtered 0.110 (vs chain +0.006 [+0.002, +0.011])
    hmm_burst 0.112 (vs chain +0.004 [+0.000, +0.008]) · hmm_burst_filtered 0.110 (vs chain +0.006 [+0.002, +0.011])
```

The same report restricted to the last 4.8 days (`--since-days 4.8`, so 10-03 onwards, 11,635 calls): both
HMMs say `bursts only` (ΔBIC 272 and 22, degraded dwell 3.2–3.3 calls), no day decodes as degraded, and the
held-out comparison has too few sessions (22 train / 10 test).

**Reading.**
- On bursty data the plain HMM uses its second state for the bursts themselves: 83% fail, about 3 calls
  long. That is the 1b chain again. Only the burst variant finds a slow regime.
- The slow regime switches on **10-02/10-03**, and that is when telemetry v8 shipped: SSLError/ReadError
  became retryable, and the dead pooled-connection failures (~300/day on 09-30 and 10-01) stopped reaching
  clients. Per-day error causes confirm it: SSLError + ReadError went from 266–300/day before to 9–56/day
  after. **The "degraded regime" is the proxy before the fix, not an upstream outage.** After v8, nothing
  regime-like is left.
- Held out, every Markov-family model beats p^n by about 0.05 Brier, with CIs that exclude 0. The regime
  models beat the chain by only 0.003–0.006. The filtered variant's gain has a CI that excludes 0, but the
  test sessions all fall after the cut (10-02 17:57), so what it measures is adapting to the v8 level shift.
  That is useful behaviour for a router, but the shift was self-inflicted.
- The day mixture barely beats iid (+0.004) and loses to the chain. Day buckets are too coarse for
  sessions that run for hours.
- The map's Codex day effect (69 failures on 10-02, 1 on 10-07) came from Codex pseudo-sessions, not proxy
  rows (under 1k Codex rows in proxy telemetry), and it is **untested here**.

**Evidence level: E2.** Pilot measured, with held-out scoring. It is not E3, because the only regime
found is an instrument change. Re-run 1c after the next real upstream incident.

**Not done.** More than 2 states. Per-client streams (the Codex rows are too few). A stored Codex
pseudo-session split (still owed from the map).

## 2. Retry-action A/B instrument

**Behaviour.** The default is unchanged. `APEX_RETRY_POLICY` takes one of three values:
- `immediate` (default): today's jittered backoff, about 125–250 ms at the defaults.
- `wait`: each sleep before a retry becomes `max(today's delay, APEX_RETRY_WAIT_MS)`. The wait defaults to
  1000 ms and is clamped to [0, 2000] ms. It still counts toward the existing 60 s total backoff cap and is
  billed to `apex_added_ms` like any other backoff.
- `switch`: a placeholder. There is one upstream per wire today, so it behaves like `immediate` and only
  logs the arm. The real switch lands with a second endpoint.

`APEX_RETRY_AB=1` draws the arm uniformly over `APEX_RETRY_AB_ARMS` (default `immediate,wait`). The draw
happens once per request, at its first retry, so only requests that retry carry an arm, and the arm cannot
depend on how the retry turned out.

**Telemetry v11.** New fields: `retry_policy` (the configured policy, or `ab`), `retry_arm`, and
`retry_propensity` (1.0 when the arm is not randomised). They are set only on rows that retried. The
doctor accepts v11, and both handlers record the fields on the success path and on the raise path.

**Readout.** `apex-router retry-ab report [--since-days N] [--min-n 30] [--json]`. It uses randomised rows
(`retry_policy == "ab"`) only. Per arm it reports:
- P(recovered), meaning the request got an upstream response after retrying, with a Wilson CI.
- P(clean).
- Mean backoff.

It also reports arm − immediate with a Newcombe hybrid CI. The verdict is `INCONCLUSIVE` while any arm has
fewer than 30 rows, and `no detectable difference` when the CI includes 0. Rows from a fixed (non-random)
policy and pre-v11 retried rows are shown as context and never pooled into the comparison.

**Live, today:** there are no randomised rows yet. Pre-v11 retried requests: 374, of which 73.3%
[68.6%, 77.5%] recovered and 69.0% were clean. Over the last 7 days, about 53 requests per day retried.

**What it still needs before it can conclude**
- An owner decision to set `APEX_RETRY_AB=1` on the serving proxy. That is a launchd/env change and is not
  made here.
- Traffic. 30 rows per arm (the INCONCLUSIVE floor) takes about 1–2 days at 53 retries/day, but at a 73%
  base rate 30/arm can only detect a difference of about 32 points. Detecting 10 points needs about 308
  rows per arm, roughly 12 days at the current rate. Retry volume follows upstream trouble, so a calm week
  stretches this.
- A pre-declared stopping rule: fix the target n per arm before looking, and do not peek and stop early.
- An answer to whether a 1 s wait is worth it. The readout logs the mean backoff per arm, so recovery gained
  can be weighed against added latency.
- For `switch`: a second upstream. Until one exists, `switch` must stay out of `APEX_RETRY_AB_ARMS` or it
  just burns traffic on a duplicate of `immediate`.
