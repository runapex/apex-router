# Usage-data readout → decisions (2026-09)

> **UPDATE (2026-09-22) — Findings 1, 2, 4 actioned + validated end-to-end; 3 folded as evidence.**
> See "Resolution" at the bottom. Root cause for Finding 2 turned out to be **schema drift in the
> live `state.db`** (missing the §4 matcher columns), which `CREATE TABLE IF NOT EXISTS` never
> healed → every matcher call threw → 100% `matcher_event="unwired"`. Fixed with an additive
> migration; proxy restarted via `launchctl kickstart -k`; matcher now emits `new`/`extend`/
> `client_edit` on live traffic.


**Source:** live local telemetry, 2026-09-18 → 09-22 (3.7 days): `~/.apex/telemetry.jsonl`
(3,954 per-request rows + 5,010 heartbeats), `~/.apex/offload_telemetry.jsonl` (92 rows,
2026-08-03 → 09-17), `~/.apex-router/conformance.jsonl` (68 rows), `route_log.jsonl` (**absent**).
Every number below was computed from these files and cross-checked against the code that emits it.

**Traffic shape (context for all decisions):** codex/openai = 3,367 rows (85%), claude-code/anthropic
= 587 (15%). Models: gpt-5.6-sol 1,373 · gpt-6-astra 1,166 · opus-4-8 586 · gpt-5.6-terra 534 ·
luna 268. So the dominant surface by volume is **codex/OpenAI-wire**, and the two heaviest models are
the cross-vendor GPT tiers (sol = default, astra = the cross-validation reviewer).

---

## Finding 1 — `route_log.jsonl` does not exist → the outcome-router has ZERO training labels
**Data:** no `route_log.jsonl` anywhere under `$HOME`. `route_join`/`route-advise`/Phase-1 all read it.
**Code:** commit `276898b` (Sept 11) — *"feat(pi-route): auto-capture … route_log.jsonl was never
written — the outcome-router's training label had zero data"* — was supposed to fix exactly this;
`~/.pi/agent/settings.json` DOES reference `integrations/pi/apex-route.ts`. Yet 11 days later the file
is still absent. So the writer is registered but **not producing rows** (hook not firing, or it only
logs on provider-error escalations, which are rare — 2% error rate).
**Decision — FIX (highest leverage).** This is the #1 blocker: every downstream design
(Phase-1 outcome-router, `RESEARCH-FIT-BACKLOG.md` P3, the shadow-A/B labeler) is starved until this
one table has rows. **Action:** instrument `apex-route.ts` to confirm the `agent_end` handler fires
and writes; add a self-check (`apex-router route-advise` should warn "0 rows / writer silent" instead
of failing quietly). Verify with a single pi run → assert ≥1 row appears. *Confirmation step first:*
add one debug log line to the hook and do one run before assuming the handler is the fault vs. the
path/permissions.

## Finding 2 — the session matcher is inert on 100% of live traffic (bust detection is a no-op)
**Data:** `matcher_event = "unwired"` on **3,954/3,954** rows (both clients). `session_id` is null on
**3,379/3,379 codex rows (100%)** and 0/599 claude-code rows. claude-code survives only because its
`x-claude-code-session-id` header carries the id — the matcher itself never fired `extend`/`client_edit`
for **either** client.
**Code:** `wire.identify_into_store` runs (store IS passed, `app.py:149/153`) but returns None
(fail-open) on the OpenAI wire; `passthrough.py:94` only sets ids when the matcher returns non-None.
**Consequences, both real:** (a) **prefix-cache bust detection is disabled** — the reported "0 busts /
bust_cause=none on 3,955 rows" is a *silent detector*, not a healthy cache; the append-shape divergence
signal never computes. (b) codex (85% of spend) has **null session_id**, so per-session Phase-1
features, session-keyed joins, and the cache-handoff-nudge cannot key on it.
**Decision — INVESTIGATE-THEN-FIX.** First confirm *why* the matcher returns None on the OpenAI wire
(empty store? sys-hash mismatch? codex bodies lack the fields the matcher keys on?) — a 30-min trace,
not a guess. If it's a genuine OpenAI-wire gap, fixing it lights up bust detection on the majority
surface *and* unblocks Finding 1's session features. Do NOT ship a fix before the trace names the
None-cause (fail-open by design means the bug hides).

## Finding 3 — Anthropic cache is extremely healthy; OpenAI slightly less — CONFIRMS the affinity thesis
**Data (corrected for wire semantics — anthropic `tokens_in`=fresh, openai `tokens_in`=total):**
- claude-code/anthropic: hit ≈ **1.000**, cache_read 95.3M, fresh_in **1,176**, cache_write 6.1M, **0 busts**.
- codex/openai: hit = **0.867**, cache_read 208.6M, fresh_in 32.1M, **0 busts** (but see Finding 2 caveat).
**Decision — NO CODE CHANGE; this is evidence, not a defect.** It is the first *live-data* confirmation
of the `DECISION-kimi-codex-routing.md` K4 call and the digest's "cache-value-aware affinity" hypothesis
(`RESEARCH-FIT-BACKLOG.md`): heavy-reuse multi-turn work on the Anthropic surface is where provider
caching already pays off. Fold these numbers into the affinity backlog entry as ground-truth support.

## Finding 4 — astra (the cross-validation reviewer) has the worst error rate: 3.3%, all connection faults
**Data:** gpt-6-astra 38 errors / 1,168 rows (3.3%) vs. 2.0% overall; causes = ConnectError (20) +
ReadError (7). Bursty (one cluster of 6 over 392s on 09-18; mostly singletons). *(Observed first-hand
this session — the astra cross-validate call dropped/looked hung.)*
**Decision — FIX (small, high-value).** astra calls are the long, expensive cross-validation reviews
where a dropped connection is most costly. **Action:** add bounded retry-with-backoff on
ConnectError/ReadError for the astra endpoint (idempotent read-only reviews are safe to retry), and
surface the error-cause in the doctor error panel. Scope: the proxy's upstream client, not the router
core.

## Finding 5 — review-lane token burn is HISTORICAL and already resolved (no action)
**Data:** the 40 review-lane calls that burned local tokens (15,110 completion + 93,546 prompt, all
escalated → zero measured frontier savings) are **all 2026-08-03 → 08-21**. `ORNITH_REVIEW_LANE` is
unset in `~/.apex-router/ornith.env` (default-off), and `offload_telemetry` has no token-burning review
rows since. **Decision — NONE.** The default-off call (README design decision #3) already fixed this;
noting it so it isn't re-litigated. The offload subsystem as a whole shows **1 frontier-saving call in
92** — economics remain thin, but that's a known measured state, not a new defect.

## Finding 6 — conformance.jsonl is stale (last row 2026-08-24, all `matched=false`)
**Data:** 68 rows, none since Aug 24, every row `matched=false`. **Decision — LOW PRIORITY.** The
`resolve` surface stopped emitting conformance rows a month ago; since Phase-1's join needs conformance
features, this compounds Finding 1. Fold into the Finding 1 fix (both are "the label/feature writers
went quiet").

---

## Priority ordering (what to actually do)
1. **Finding 1 — route_log writer silent.** Unblocks the entire outcome-router / research backlog. Confirm-then-fix.
2. **Finding 2 — matcher inert on OpenAI wire.** Lights up bust detection on 85% of traffic + session features. Trace-then-fix.
3. **Finding 4 — astra retry/backoff.** Small, immediately valuable for the cross-validation workflow.
4. **Finding 6 — conformance writer** (fold into #1). **Findings 3 & 5 — no change** (evidence / already-resolved).

## Honesty notes
- "0 busts" is **not** validated cache health while Finding 2 stands — the detector is inert. Don't cite it as a win.
- Findings 1, 2, 6 are all "a writer that should be emitting is silent" — likely a shared root (hooks/handlers not on the live path), worth one combined investigation.
- Every fix above needs the tree's standard gate (out-of-sample where statistical; Codex/astra design pass for anything touching routing) before it's trusted — this memo decides *what* to fix, not that the fix is correct.

---

## Resolution (2026-09-22)

**Finding 2 — root cause + fix (the big one).** The trace (not a guess) showed the live
`~/.apex/state.db` (created Jul 9) still had the **pre-matcher `sessions` schema** — missing
`sys_prompt_hash`, `agent_id`, `project_id`, `client_session_id`, `wire_hint`, `turn`. `Store`'s
`CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so `candidate_sessions` raised
`no such column: sys_prompt_hash` on **every** request; `wire.identify_into_store`'s fail-open
swallowed it → 19,027 rows at 100% `matcher_event="unwired"`, null session_id on all codex traffic.
The matcher CODE was correct all along (verified: returns `new`/`extend` on a fresh Store). Fix:
an **additive, idempotent schema-reconcile** in `Store.__init__` (`_ADDITIVE_COLUMNS` +
`_reconcile_additive_columns`, `store.py`) — a drifted table self-heals with `ALTER TABLE ADD
COLUMN` on open; a fresh DB is untouched. Tests: `test_m0_store.py` (migration heals, unblocks the
matcher end-to-end, idempotent on current schema). **Validated live:** after `launchctl kickstart -k`,
`state.db` gained all 12 columns; a codex request through the proxy produced `matcher_event: new`
then `extend` on the same session_id — the identity chain bust-detection needs is live; real
concurrent traffic now shows `new`/`extend`/`client_edit` instead of 100% `unwired`.

**Finding 1 — writer works; the hook just hadn't fired.** `route-log` CLI (both the dev tree and
the prod `~/.local/bin/apex-router`) writes `route_log.jsonl` correctly on first call. The file was
absent only because the pi `agent_end` hook is the sole producer and pi hasn't been the active
surface (traffic is codex/claude-code through the proxy). No code defect. (Finding 6, conformance
staleness, is the same shape — a quiet producer, not a broken writer.) **No code change**; smoke-test
rows were cleaned so the label stream stays honest.

**Finding 3 — folded as evidence.** Live cache health (anthropic hit ≈1.000 / openai 0.867, 0 busts)
is recorded here and in `RESEARCH-FIT-BACKLOG.md` as ground-truth support for the K4 affinity thesis.
Caveat retained: "0 busts" was NOT validated health while Finding 2 stood — now that the matcher is
live, bust detection will actually compute going forward, so re-read this metric on the next window.

**Finding 4 — connect-only retry, shipped.** `upstream.send_stream` now retries **ConnectError/
ConnectTimeout only** (bounded, exp-backoff; `upstream_connect_retries`/`upstream_connect_backoff_s`
in config). A ConnectError is pre-socket → the upstream never got the POST → retry cannot
double-submit. ReadError is deliberately **not** retried (request may be in flight → duplicate-
completion risk). Tests: `test_upstream_connect_retry.py` (retry-then-succeed, exhaust-then-raise,
ReadError-not-retried). Targets astra's 3.3% connection-fault rate (20 ConnectError of 27).

**Proxy restart doctrine (the recurring pain).** `com.apex-router.serve` is launchd-managed
(`RunAtLoad`+`KeepAlive`, runs from `PYTHONPATH=.../src` so edits load on restart). Restart with
`launchctl kickstart -k gui/$UID/com.apex-router.serve` — NOT `kill` (KeepAlive just respawns it) —
then validate: `/healthz` + `/status` (posture `measure-only`), `launchctl print … | grep state`
(must be `running`/`active`), and a real request → fresh telemetry row with a non-`unwired`
`matcher_event`. The `httpx.ReadError` in the err log at restart is the OLD process's stream being
cut by `-k` (it appears BEFORE the "serving on" line) — not a new-process fault. State backed up to
`state.db.pre-migration-bak` before the restart (migration is additive/safe; belt-and-suspenders).

**Test status:** full `tests/proxy_engine/` suite 537 passed, 1 skipped, 0 regressions.

## astra cross-validation (2026-09-22) — 4 findings, all CONFIRMED and fixed

An independent `it-entra-gpt-6-astra` adversarial pass (read-only, prompted to refute) returned
**BUGS-FOUND** with 4 reproduced findings. Each was verified at ground truth; fixes authored here
(not ported from astra), each with a regression test:

- **F1 — concurrent Store opens can abort startup.** PRAGMA-read and `ALTER` are not atomic, so a
  second opener (separate connection — the RLock only serializes one Store) can add a column between
  our snapshot and our ALTER → `duplicate column name`. **Fix:** the ALTER now swallows
  *duplicate-column* `OperationalError` (idempotent — the column now exists, which is the goal) and
  re-raises any other. Test: `test_migration_duplicate_column_race_is_swallowed`,
  `test_migration_reraises_non_duplicate_operational_error`.
- **F2 — case-differing legacy column bricked startup.** A pre-existing `SYS_PROMPT_HASH` (SQLite
  identifiers are case-insensitive) was compared case-sensitively → tried to ADD a duplicate every
  restart. **Fix:** case-fold the column-set compare. Test:
  `test_migration_tolerates_case_differing_existing_column`. (Reproduced directly before the fix.)
- **F3 — unbounded retry budget.** `APEX_CONNECT_BACKOFF=inf` would hang; a huge retry count made
  `2**i` sleep astronomically. **Fix:** clamp at point-of-use — non-finite/negative backoff → 0,
  retries capped at `_MAX_CONNECT_RETRIES=10`, each sleep at `_MAX_CONNECT_BACKOFF_S=30s`. Tests:
  `test_retry_budget_is_clamped_against_hostile_config`, `test_negative_backoff_does_not_break_retry`.
- **F4 — retry backoff was billed as upstream latency.** The backoff sleep is inside `send_stream`,
  which both handlers time as the upstream window → recovered connect failures silently inflated
  `t_upstream_ttfb_ms`/`upstream_error_wait_ms` and vanished from `apex_added_ms`. In a
  measurement-first proxy that's a real contract violation. **Fix:** `send_stream` reports slept
  backoff via an optional `stats` out-param (backward-compatible); both handlers move it from the
  upstream window into `apex_added_ms` (both fields already exist — no schema change). `ttft_ms`
  (client-observed) still includes it. Tests: `test_stats_records_backoff_for_latency_attribution`
  and end-to-end `test_{passthrough,shadow}_bills_connect_backoff_to_apex_not_upstream`.

astra's *findings* were adopted; its proposed fixes (e.g. `BEGIN IMMEDIATE` for F1) were NOT ported —
the idempotent-ALTER approach is simpler and also subsumes F2. Two astra observations were correctly
scoped as *boundaries to preserve, not current bugs*: the no-double-submit guarantee holds only for
the shipping bytes-body + stock-httpcore config (a streamed body or a custom event-hook would break
it) — now documented in `send_stream`. **Full suite: 545 passed, 1 skipped, 0 regressions.** Proxy
restarted via `kickstart -k`; matcher confirmed still live post-fix (`new`/`extend`).

**Improvements astra proposed (ranked, for the backlog — not done here):** (1) transactional
migration with failure cleanup + a real concurrency test; (2) jittered retry delay + total-duration
budget cap + transport-level tests; (3) **structured attempt/matcher-failure telemetry** — record
recovered connect failures, backoff time, and matcher identification errors as first-class fields
instead of hiding them behind a success row or `unwired`. #3 is the highest-value follow-up: it would
have surfaced *both* the schema-drift matcher outage AND the retry attribution as telemetry, rather
than requiring this investigation to find them.

## Improvements implemented + 2nd astra pass (2026-09-22)

The three backlog improvements were then **implemented** and put through a second astra pass, which
returned **BUGS-FOUND** with 4 more findings — all confirmed at ground truth and fixed:

**Implemented (improvements 1–3):**
- **Transactional migration + interrupt-safe cleanup** (`store.py`): reconcile runs inside
  `BEGIN IMMEDIATE` (serializes concurrent openers DB-wide, atomic rollback of the ALTER set); a
  `BaseException` guard rolls back + closes on any post-connect failure (incl. interrupt).
- **Jittered backoff + total-duration budget** (`upstream.py`): equal-jitter per sleep (no
  thundering herd) + `_MAX_CONNECT_TOTAL_BACKOFF_S=60s` cumulative cap.
- **Structured telemetry v6** (`events.py`/`wire.py`/both handlers/`doctor.py`): `matcher_error` +
  `matcher_event="error"` surface a matcher OUTAGE (vs. the ambiguous `unwired`); `connect_retries`/
  `connect_backoff_ms` make a flaky-upstream recovery provable. SUPPORTED_SCHEMA now includes 6.

**2nd-pass astra findings (all fixed, own fixes):**
- **F2-1 — interrupt leaked the writer lock.** `except Exception` missed `BaseException`; a
  KeyboardInterrupt mid-migration left a dangling `BEGIN IMMEDIATE` holding the lock (reproduced:
  "database is locked"). Fix: `except BaseException` + best-effort rollback + close. Test:
  `test_store_cleanup_on_interrupt_mid_migration`.
- **F2-2 — non-chat requests mislabeled as matcher outages.** A `GET /v1/models` (empty/non-JSON
  body) raised `JSONDecodeError` inside the matcher try → `matcher_event="error"` with zero matcher
  calls, polluting the outage signal. Fix: split parse phase (not-applicable → None, no error) from
  the match/persist phase (real error → `matcher_error`). Tests: `test_nonjson_body_is_not_a_matcher_error`.
- **F2-3 — backoff billed scheduled, not elapsed, sleep.** Recorded `delay` before awaiting it;
  under loop contention the real sleep is longer, so F4's subtraction under-removed and re-billed
  the slack to upstream. Fix: measure elapsed with `perf_counter` around the sleep (budget cap still
  uses scheduled delay — its correct currency).
- **F2-4 — the secrets canary went VACUOUS (my regression).** Adding `stats` to `send_stream` broke
  the canary's fake signatures → `TypeError` → 502 *before* the canary body ran, so a security test
  passed without exercising its paths. Fix: updated the fakes + added `up.called` assertions so a
  silent early-502 can never make the canary vacuous again. This is the highest-value catch — it
  restored a security guard I'd unknowingly disabled.

astra's *findings* were adopted; fixes authored here. astra's non-findings were also useful: it
confirmed `BEGIN IMMEDIATE` serializes separate connections DB-wide (not just threads), that reconcile
is atomic (the bootstrap CREATE is not — acceptable, it's idempotent), and that the v6 bump has no
lingering v5 consumer. **Full suite: 1428 passed, 4 skipped, 0 regressions.** Proxy restarted via
`kickstart -k` → schema v6 live, matcher firing (`new`/`extend`), new fields present.

**Still backlog (astra's 2nd-pass improvements, not done):** disk-backed WAL concurrency tests
(held-writer-beyond-timeout, first-time WAL activation); a single explicit bootstrap-transaction
boundary over CREATE+ALTER with a post-migration column-presence assertion; doctor summaries that
split matcher-stage failures and recovered retries from upstream request errors.

**Not done (deliberately):** the routing-adjacent claim (which surface produces route_log labels)
still merits its own independent pass before any downstream design leans on it.
