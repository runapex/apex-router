# DECISION — Kimi & Codex venue routing (2026-08-24)

Status: **accepted, implemented** (registry venue policies, `resolve --venue`, codex
route table, nightly context watch, pi `kimi-code` family).

## Measured basis (live telemetry, `~/.apex/telemetry.jsonl`)

- Codex-venue traffic is **100% kimi-k3**: 252 requests, 2 sessions, 37.9M cached-read
  + 38.6M fresh input tokens, 137k output. Modeled cost at pi-catalog list rates
  (k3: $3 in / $0.30 cache-read / $0 cache-write / $15 out per 1M): **≈ $129**.
- **Per-request context: p50 = 346k, p95 = 495k, max = 513k; 73.8% of requests > 250k.**
- Claude-side traffic is cache-healthy (96.5% hit rate, 0 busts) — a different regime.
- Kimi cache economics: cache writes are FREE, cache reads ~10% of input price →
  **cache hygiene is not the Kimi lever** (unlike Anthropic traffic).

List rates (pi model catalog, 2026-08): k3 $3/$0.30/$15 · k2.7-code $0.95/$0.19/$4 ·
k2.6 $0.95/$0.16/$4 · sonnet-5 $3/$0.30/$15 (+$3.75 cache-write) · opus-4-8 $15/$1.50/$75.

## Decisions

**K1 — Codex venue default stays kimi-k3.** Its 1M window is load-bearing: 73.8% of
codex requests exceed the 262k window of every cheaper Kimi model. k3 is also
list-price-identical to sonnet-5 with free cache writes, so there is no cheaper
model that can serve the workload *as it is shaped today*.

**K2 — The codex cost lever is CONTEXT REDUCTION, and it is now instrumented.**
Under 250k context, `kimi-k2.7-code` (code-specialized, 262k window) serves the same
traffic at **≈ $44.5 vs $129 (2.9× cheaper)** on the measured window. Policy:
- venue policy `downshift_model=kimi-k2.7-code`, `downshift_ctx_ceiling=250_000`
  (registry `venues.codex`; surfaced by `apex-router resolve --venue codex`);
- the nightly **codex context watch** reports p50/p95, the downshift-eligible share
  (25% at decision time), and approaches to the 1M ceiling — so the K1 premise is
  re-measured every night and K1 flips to k2.7-code the day the workload fits;
- codex sessions should be handed off / compacted before 250k ctx (the adaptive
  handoff nudge applies here too — its threshold is venue-agnostic by design).

**K3 — pi interactive Kimi routing:** `>>kimi` stays `kimi-k2.6` (cheapest general,
262k is ample for single-shot); new `>>kimi-code` family = `kimi-k2.7-code` for
code-shaped one-shots; k3 is reserved for genuinely >250k contexts or explicit
override. **Turbo/highspeed variants are never defaults** (2× price, latency-only).

**K4 — Cross-vendor:** k3 ≡ sonnet-5 at list price, so cost does not choose between
them — capability/affinity does (codex wire → k3; Claude Code multi-turn with heavy
reuse → sonnet, where Anthropic caching is already working). No change to Claude
venue defaults.

## What was deliberately NOT done

- **No forced model rewrite on the wire.** The proxy measures; it does not silently
  re-model requests (billing-visible changes belong to the client, per the measure-first
  doctrine). The venue policy recommends; clients adopt.
- **No turbo/highspeed routing** without a measured latency requirement.
- **No cache optimization work for the Kimi venue** — writes are free, reads ~10%;
  the money is in context size and model window, and this decision says so in the
  nightly digest so nobody "fixes" the wrong thing.

## Reversal triggers

- Nightly watch shows the downshift-eligible share > 70% for a sustained period →
  flip the codex default to k2.7-code (K1's premise is gone).
- A >262k-window Kimi code model at k2.7-code pricing ships → re-run this analysis.
- k3 price changes relative to sonnet-5 → revisit K4.
