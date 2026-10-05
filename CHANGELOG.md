# Changelog

Notable changes to apex-router. Dates are the day the change landed on `main`.
Version numbers follow `pyproject.toml`; between tags, the heading is the version the
next tag will carry.

## 0.4.2 — unreleased

### Fixed
- The datapce plugin no longer repeats the band as a status entry (`⚠ datapce: apex ●GREEN $0.91`)
  in the terminal and desktop app. The status entry now appears only where the band can't draw:
  VS Code, mobile, `band: false`, or after Hide (at once, with the current cost). A status entry left
  by an earlier version is cleared on the next update.
- pi: `>>sonnet`, `>>frontier`, `>>opus`, `>>deep` and `>>gpt-sol` no longer fail with "model not
  found". pi's built-in model list has no `claude-sonnet-5-5`, `claude-opus-5-5` or `gpt-6.1-sol`, so
  the Claude shortcuts (and `>>haiku`) now go through the `foundry` provider (`it-entra-claude-*`, via
  the proxy), and `>>gpt-sol` is back on `gpt-5.6-sol`. `>>fable` is unchanged. New registry key
  `provider_id_prefix` maps a provider to the prefix its model ids carry (`foundry` → `it-entra-`).

## 0.4.1 — 2026-10-04

### Security
- 0.4.0 stored an unsalted repo hash that one list of candidate paths, hashed once, could match
  on any machine; 0.4.1 salts it per install and drops old values. The token is the first 16 hex
  of HMAC-SHA-256 of the path, keyed with a random salt drawn once and kept in the local plugin
  store, so tokens can't be matched across machines or against precomputed hashes; anyone who can
  read the local plugin store can still test candidate paths against them. Rows also keep the
  dispatch description (up to 120 characters, may name a repo or path) and the session id, which
  Claude Code's local transcript folders map back to a directory. Nothing leaves the machine. The
  repos-seen count restarts once; other counts are kept. Restart open sessions after upgrading: a
  session still running 0.4.0 keeps writing unsalted values until the next 0.4.1 session start
  drops them.

### Fixed
- The test suite no longer writes to the live install; a guard fails the run if it does.

### Changed
- Hook retirement moves from 0.4.1 to 0.4.2; the deprecated hooks keep running beside the plugin
  until then.

## 0.4.0 — 2026-10-04

One product, one install: datapce, a Claude Code plugin (alias apex-router), with the pip package
as its optional backend. Advise-only: it never changes the model you chose.

### Added
- `plugin/` — the datapce hooks module: a status band and the `/apex` pane with upstream pressure,
  session cost and every Agent and Workflow dispatch (tier requested and run, outcome, duration,
  tokens = input + output + cache reads + cache writes); `/apex handoff` and the handoff toast; a
  ≤ 2-line pressure note in the Agent and Workflow tool descriptions only when pressure is not
  GREEN; the one condensed `datapce` skill. One route row per dispatch (no prompt text, file
  contents or command text), finished at `turn.complete` as `ok` or `error` (`async` only for
  agents still running at session end). /clear and /resume re-identify the session on the next
  turn. Marketplace: `.claude-plugin/marketplace.json`.
- A ledger per task type × tier of completion and cost (n, ok %, error kind, `tok(all) μ`, `dur μ`).
  It is not answer quality: `QUALITY_LABELS` is off in v1, so no tier advice and no evidence table
  reach the planning skills; cells keep counting for v1.1's quality label.
- `apex_router.core` (pce-core): Welford, EWMA (float and Q16), Jacobi PCA, Q/T² with the χ²
  limit, Page CUSUM, the penalty and cell state machines, OLS cost and budget burn, with a
  TypeScript mirror held to generated parity fixtures (`python -m apex_router.core.fixtures`).
- `route-join` keeps one row per `(session_id, tool_use_id)` (finished over async, plugin over
  hook; `dispatch_deduped`) and reports `writer_parity`: plugin vs hook dispatches per UTC day,
  compared by `(session_id, tool_use_id)` key and filed under the plugin row's day, so a dispatch
  spanning 00:00 UTC still pairs (`parity_span_days`; `parity_days`, the days with plugin rows
  where the hook never disagreed, i.e. every hook dispatch also has a plugin row; `parity_until`;
  `gate_open`; a Workflow-only day is quiet, neither a break nor a count).
- A repository hygiene test.
- `python -m apex_router.ornith.ornith_client --probe-thinking` — proves `reasoning_effort:
  none` is honoured. Exit 0 = thinking off; 1 = evidence of thinking (reasoning present, an
  unterminated inline `<think>`, or an empty answer); 2 = inconclusive (busy, down, other error).

### Changed
- `apex_router.stats` re-exports from `apex_router.core.stats`; `bradley_terry` removed (no caller).
- `install.sh` adds the datapce marketplace from the install directory and installs
  `datapce@datapce`; `apex-router-skills` is no longer added by default (opt in with
  `--skills-marketplace runapex/apex-router-skills`).

### Deprecated
- `hooks/agent-route-log.sh` and `hooks/cache-handoff-nudge.sh`: they keep running beside the
  plugin (route-join counts each dispatch once). `--agent-route-log-hook` and
  `--cache-handoff-hook` still wire them, with a warning. Both are retired in 0.4.2, once
  `apex-router route-join --json` shows `stats.writer_parity.parity_span_days` ≥ 14
  and `parity_days` ≥ 10 and `parity_until` (the last streak day) is within the last 2 days
  (`stats.writer_parity.gate_open`).

### Fixed
- ornith: inference lock is bounded (`ORNITH_LOCK_TIMEOUT_SECS`, 120 s) → `OrnithBusy`
  escalates instead of hanging a turn.
- apex-ornith review: thinking OFF, 1024-token budget, bounded `git diff`; a busy or failed
  local tier now exits 3 (unavailable) / advisory 0 instead of a traceback, and truncated
  reviews keep their partial findings.
- pressure: connect-retried-then-succeeded rows no longer count as transport faults (they
  pushed flaky-link boxes to AMBER/RED and shed work for no upstream reason).
- connect-retry backoff test no longer depends on wall-clock timing.
- handoff nudge: threshold is the clamped median (25M–100M) of per-session cache reads,
  not a p80 that rose with every long session.

## 0.3.0 — 2026-10-02

The theme of this release is closing the loop between what the proxy measures and how an
agent dispatches work: a pressure signal before a fan-out, a local pre-read before a heavy
review, and a label stream for Claude Code subagent dispatches that the outcome router was
blind to.

### Added
- `apex-router pressure [--check]`: GREEN / AMBER / RED / UNKNOWN from the last 15 minutes
  of proxy telemetry, with an exit-code contract (0/1/2/3, 4 = usage error), a sample
  floor, a `retry-after` override, and an atomically written `~/.apex-router/pressure.json`.
- `apex-router review-preread`: the local Ornith tier reads a diff and returns claims to
  verify for an independent heavy reviewer. Nonce-delimited untrusted input, deterministic
  injection-marker scan, own telemetry lane (`preread`), explicit exit codes (0/2/3).
- `hooks/agent-route-log.sh` + `install.sh --agent-route-log-hook`: a fail-safe
  `PostToolUse` (matcher `Agent`) hook that appends one label-pending route-log row per
  Claude Code subagent dispatch. `apex-router route-join` infers escalations offline and
  joins proxy telemetry on `(session_id, agent_id)`; nightly runs the join.
- Proxy: bounded transient-transport retries before the first client byte, and after the
  body was sent only on a fast-fail (`APEX_RETRY_FAST_FAIL_MS`, default 3000) against a
  stateless completion endpoint. Hard caps on retry count and cumulative backoff.
- Telemetry schema 8 (`upstream_rejected`, `connect_retries`), schema 7 (upstream error
  bodies + rate-limit headers), schema 5 (`error_cause`, pricing aliases incl. Foundry and
  GPT-6). Shared `apex_router.telemetry_path` resolution (`APEX_TELEMETRY` →
  `APEX_HOME/telemetry.jsonl` → `~/.apex/telemetry.jsonl`).
- `scripts/change_classifier.py`: a multi-model panel that classifies a diff on change
  class, requirement fit, and blast radius, and reports where members disagree. Ships a
  working four-member, three-vendor example panel; validates panel results and exposes
  incomplete evidence.
- codeqa: `CODEQA_JUDGE_MODE=screen` by default when a judge is pinned (cheap tier screens,
  the pinned judge adjudicates only struck or uncertain claims).
- skill-bench: nightly over-time automation with disjoint-window slicing; SkillOpt's
  quality axis behind apex's gate (measure-only).
- Pi: per-tier cues (`>>sonnet`, `>>opus`, `>>fable`, `>>auto`), escalation outcomes
  auto-captured on every turn, GPT-5.6 tier aliases, within-Kimi-family routing.
- Tuner: break-even / minimum-value gate primitive (un-wired by decision, see
  `docs/DESIGN-breakeven-gate-wiring.md`); SKILL.state driver, A/B benches, drift and
  transferability experiments (shipped default-off, see `docs/DESIGN-skill-state.md`).
- Route conformance log and `apex-router route-check` readout (agent surface is
  intent-only and never counts toward drift).
- Installer: version-controlled cold-load-stall fix (keepwarm agent + guard debounce);
  `--ornith-serve` persistent local stack.

### Fixed
- store: hardened concurrent WAL initialization.
- session: heal `state.db` schema drift that silently disabled the matcher.
- doctor: schema v5 was missing from `SUPPORTED_SCHEMA`, dropping every fresh row.
- compile: stale validators path after the `apex` → `proxy_engine` rename.
- proxy: preflight port bind on `serve` with a single-owner message.
- ornith: debounced version guard stops drain restart thrash.
- metrics: Codex cached tokens are a subset of input (corrected downshift eligibility and
  cost ratio); Kimi pricing.

### Docs
- Runbooks: pressure, review-preread, route-conformance, pressure-aware pi integration.
- Phase-0 outcome-router report (`docs/phase0-outcome-router/`) and the cache-routing
  research note. Design records for the shadow-A/B labeler, break-even gate wiring,
  SKILL.state, and the learn chain. Local-serving threat-model note.
- Public landing page (`docs/index.html`, GitHub Pages).

### Companion
- [apex-router-skills](https://github.com/runapex/apex-router-skills) 0.7.0: `model-routing`
  checks `pressure` before a fan-out; `cross-validate` takes the `review-preread` claims
  list and records pre-read recall; new `evidence-labels`, `unattended-loop`, and
  `dependency-vetting` skills (0.7.0).

## 0.2.0 — 2026-08-23

- Toolkit migration: codeqa + ornith offload subsystem merged into apex-router; shared
  model registry; live `resolve` consumer; nightly adaptivity pass; measuring proxy as an
  optional `[proxy]` extra.
