# Changelog

Notable changes to apex-router. Dates are the day the change landed on `main`.
Version numbers follow `pyproject.toml`; between tags, the heading is the version the
next tag will carry.

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
- `python -m apex_router.ornith.ornith_client --probe-thinking` — proves `reasoning_effort:
  none` is honoured.

### Fixed
- store: hardened concurrent WAL initialization.
- session: heal `state.db` schema drift that silently disabled the matcher.
- doctor: schema v5 was missing from `SUPPORTED_SCHEMA`, dropping every fresh row.
- compile: stale validators path after the `apex` → `proxy_engine` rename.
- proxy: preflight port bind on `serve` with a single-owner message.
- ornith: debounced version guard stops drain restart thrash.
- metrics: Codex cached tokens are a subset of input (corrected downshift eligibility and
  cost ratio); Kimi pricing.
- ornith: inference lock is bounded (`ORNITH_LOCK_TIMEOUT_SECS`, 120 s) → `OrnithBusy`
  escalates instead of hanging a turn.
- apex-ornith review: thinking OFF, 1024-token budget, bounded `git diff`.
- pressure: connect-retried-then-succeeded rows no longer count as transport faults (they
  pushed flaky-link boxes to AMBER/RED and shed work for no upstream reason).
- handoff nudge: threshold is the clamped median (25M–100M) of per-session cache reads,
  not a p80 that rose with every long session.

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
