# datapce as a Claude Code plugin (apex-router alias) — design

Date: 2026-10-03. Status: design for review. Supersedes the positioning in README.md §1 and the
`apex-router-skills` marketplace (to be archived).

## 1. Purpose and positioning

datapce observes what actually happens on this machine — which subagents run, on which model,
what they cost, whether they succeeded, how the upstream is behaving — and turns that into evidence
the tools people already use can act on. It does not plan; it makes existing planners and
dispatchers (superpowers' writing-plans and subagent-driven-development, the `Workflow` tool, the
`Agent` tool) choose better on *this* machine, and it shows every decision and why.

One product, one install: `claude plugin install datapce@datapce` (alias `apex-router`). The pip package becomes
the optional backend (local model lanes, cross-client proxy telemetry, nightly learning).

Listing (marketplace category `productivity`):

> An advise-only view of your subagents: a status band and pane with pressure, cost and every
> dispatch, a session-handoff prompt, and a short pressure note in Agent/Workflow planning. Its
> ledger records completion and cost, not answer quality; tier advice arrives with a quality
> label in v1.1. Optional local-model lanes.
> v1 wording and behaviour: see §18 (no tier advice until a quality label exists).

Non-goals (do not compete): a planner or plan format (superpowers owns it); a status line
(claude-hud); a proxy router (claude-code-router); exporting traces to vendors (dash0/langfuse).

## 2. Scope

v1 ships everything in §4–§9 including the formulas in §7. Later: SPRT as a sequential promotion
test; per-dispatch local-model mixing at `agent.spawn` (needs the backend to expose a model id the
engine accepts); Pi parity.

## 3. Architecture

```
apex-router/
├─ .claude-plugin/marketplace.json   marketplace "datapce", one plugin "datapce" → ./plugin
├─ plugin/
│  ├─ .claude-plugin/plugin.json     name "datapce", author "datapce", version (== pyproject), userConfig (§11)
│  ├─ hooks/hooks.json               { "modules": ["./register.ts"] }
│  ├─ hooks/register.ts              wiring only: imports the modules below
│  ├─ hooks/observe.ts               §4 event capture → rows + running stats
│  ├─ hooks/evidence.ts              §5 cells, Wilson floor, warm-up state, advice text
│  ├─ hooks/router.ts                §5 agent.spawn advise/enforce
│  ├─ hooks/inject.ts                §5 skill.prompt / tool.describe / prompt.compose
│  ├─ hooks/signals.ts               §7 burn-rate, EWMA, breaker, hysteresis, budget, Q/T²
│  ├─ hooks/band.tsx  pane.tsx       §6 drawing
│  ├─ hooks/backend.ts               §9 detection + process calls
│  ├─ types/index.d.ts               $.state / $.store contract
│  ├─ skills/datapce/SKILL.md        §8 the one skill
│  └─ tests/*.test.ts                §12
└─ src/apex_router/ …                backend (unchanged modules; retired hooks per §13)
```

Runtime: the plugin is a mods module (hooks-module API, GA in Claude Code 2.1.287). No DOM, no
Node; everything through `$`. Stdlib-only Python stays the rule for the backend.

Version: `pyproject.toml` is the source; `plugin/.claude-plugin/plugin.json` must match
(`tests/test_version_parity.py`). One tag per release covers both.

## 4. Observation layer (`observe.ts`)

What is captured, per event, counts and identifiers only — never prompt text, file contents, or
command text:

| Event | Fields kept |
|---|---|
| `agent.spawn` | tool_use_id, subagentType, description (≤120 chars, the dispatch label), model requested, model resolved, parentAgentId, ts |
| agent completion (`$.agent.list()` diff on `turn.step`) | agentId → ok/failed, duration, tokens from the step's usage |
| `turn.step` | model, effort, input/output/cache tokens, stopReason, toolUses names |
| `session.measure` | context %, rate-limit kind + percentUsed + resetsAt, cost usd |
| `tool.call` (`Bash` only) | which backend command ran (`apex-router pressure`, `apex-ornith`, `review-preread`), its exit, and whether its output matched a signal pattern (`OrnithBusy`, `timeout after`, `finish_reason=length`) |
| `skill.prompt` | skill name, ts (which workflows are in use on this machine) |
| `command.run` | the plugin's own commands only |

Storage:
- Rows: `~/.apex-router/observe/<YYYY-MM-DD>.jsonl`, one JSON per event, rotated daily, 30-day
  retention. The existing `route_log.jsonl` schema is kept and written by `router.ts` (complete
  rows: resolved model known), so `route-join`/`route-advise`/`labeled_table` keep working.
- Running stats: Welford `(n, mean, M2)` per metric per cell in `$.store` (`datapce.stats`),
  persisted across sessions; never raw rows. Per `math.md A9`: persist parameters, not data.
- Machine profile (derived, `$.store` `datapce.profile`): repos seen (path hash), task_type
  mix, skills in use, hours-of-day pressure histogram, backend present/absent. Read by §5 and §8.

Task-type classification: `route_log.classify_dispatch` rules (description + subagentType →
`explore | review | debug | refactor | generate | mechanical | synthesis | other`) ported to TS,
with the Python kept as the oracle (`tests/classify_parity.test.ts` runs both on the fixture set).

## 5. Evidence and routing

**Cell** = `task_type × pressure_level` (GREEN/AMBER/RED). Each cell holds: n labels, pass count,
Wilson lower bound (same floor as `route-advise`: n ≥ 30), EWMA cost and duration, and a state:

```
COLD      n = 0            → advise only, cell inherits its parent's advice (task_type → family → global)
WARMING   0 < n < 30       → advise only; pane shows "WARMING 18/30"
READY     n ≥ 30, Wilson-lo ≥ target → eligible for enforce
DRIFTING  READY cell with enter_streak (2) consecutive windows below target → demoted to advise
```

Transitions use the hysteresis of `p08_drift_penalty.py` (enter 2, exit 3). Drift vs anomaly per
`math.md A7`: one failure is an anomaly; a streak is a regime change; a stable new regime is
re-baselined (stats reset for that cell), never patched.

**Advice** for a cell = `{tier, effort, confidence: COLD|WARMING|READY, basis}` where basis is the
one-line evidence string (`"opus→sonnet: 34/36 pass, p50 41s, $0.12; AMBER now"`).

**`agent.spawn` (`router.ts`):**
- advise mode (default): `next(e)` unchanged; log the complete label row; if advice ≠ requested,
  the band shows ▲ and the pane row says what would have changed.
- enforce mode (`/apex enforce on`, per-cell automatic once READY, toast on first promotion):
  return `{ model: advised }` for READY cells only; all other cells behave as advise. A denied
  or failed spawn under enforce counts against the cell immediately.
- Pressure shedding (AMBER: explore/mechanical one tier down; RED: no new heavy fan-out) is a
  policy cell like any other — visible, auditable, and subject to the same enforce gate.
- Hard limits never overridden: an explicit `model:` the user typed in the prompt; `Fable` requests
  are never downshifted silently (advise only).

**Injection (`inject.ts`) — strengthen, don't compete:**
- `skill.prompt` on `superpowers:writing-plans`, `superpowers:subagent-driven-development`,
  `superpowers:dispatching-parallel-agents`, `superpowers:executing-plans`, and the plugin's own
  skill: append a fenced section `## datapce evidence (this machine)` — per task_type advice
  with confidence, pressure level and recommendation, budget remaining, and the one rule the
  skill should follow (`name the tier per task in the plan; apex applies it at dispatch`). ≤ 25
  lines. Other skills untouched.
- `tool.describe` on `Agent` and `Workflow`: append two lines: current pressure and "tiers per
  task_type: …". Keeps placement.
- `prompt.compose`: one short session-scoped section: backend present/absent, enforce state,
  where the pane is (`/apex`). No policy prose — that lives in the skill.

## 6. Status bar, pane, toasts

**Band** (`ui.render AbovePrompt`, one line, appears on first signal, `Hide` button, hidden when
`hasSurvey`):

```
apex ●AMBER 5h:62%  ctx ▇▇▇▇▇░░░ 61%  $4.12/10  routes 7 (local 4 ✓3 ↑1) ▲2  conf 86%  enforce:2/8
```

Cells: pressure level + the tightest rate-limit %; context % (`session.measure`); cost vs budget
(§7); dispatches this session (local n, passed, escalated) and ▲ advice-differs count;
conformance % from `conformance.jsonl` when the backend exists; enforce = READY cells / total.
EWMA-smoothed where noted in §7. `$.ui.status` carries `apex ●AMBER $4.12` for surfaces without a
band (VS Code, `-p`).

**Pane** (`/apex`, `$.ui.open`; `userConfig.pane = auto|command|off`, default `command`):
1. *Dispatches* — this session's spawns grouped by turn: description, task_type, requested →
   resolved tier, effort, outcome, duration, cost, advice ▲ and its basis.
2. *Signals* — pressure per family with burn(short)/burn(long); breaker state per lane; budget
   time-to-exhaustion; session anomaly score with its explain card (§7).
3. *Lanes 24h* (backend only) — codegen ok/escalated bars, review/preread escalation, truncation
   rate, drain ETA.
4. *Evidence* — per cell: state, n/30 bar, Wilson-lo vs target, EWMA cost; "what enforce is
   waiting for".
5. *Profile* — task mix, skills in use, backend health (availability chain, §7).

**Toasts** (each at most once per 10 min): RED pressure; breaker opened for a lane; budget
burn(short) > 10 and burn(long) > 6; handoff threshold crossed (replaces the Stop-hook nudge;
same structured-state instruction); first cell promoted to READY; a READY cell demoted.

## 7. Formulas (`signals.ts`) — from the reference examples, copied exactly

| Signal | Formula / rule | Source |
|---|---|---|
| Pressure alert | prefix sums over 1-min buckets; `burn(w) = (bad_w/tot_w)/(1-slo)`; alert iff `burn(5m) > 10 ∧ burn(60m) > 6`; slo = 0.98 for 429, 0.97 for transport | `s11_burn_rate_alert.py` |
| Level transitions | GREEN/AMBER/RED with enter_streak 2, exit_streak 3 over 1-min windows | `p08_drift_penalty.py` |
| Smoothing | EWMA `s = a·x + (1-a)·s`, a = 0.2 (half-life ≈ 3.1 samples) for TTFT, cost/turn, cache-read/turn | `s07_ewma.py` |
| Budget | `budget = userConfig.budgetUsd` per day; `burn = spend_rate/(budget/1440)`; `minutes_to_exhaust = remaining/spend_rate` | `s10_error_budget.py` |
| Routing SLI | per cell `SLI = pass/total`; `burn = (1-SLI)/(1-target)`, target 0.9 | `s18_slo_counts_histograms.py` |
| Percentiles | nearest rank `s[ceil(p·n)-1]`; merge histograms, never average per-family percentiles | `s18`, `s01` |
| Lane breaker | open after 3 consecutive `OrnithBusy`/timeout/truncation; cooldown 10 min, fast-fail (escalate) while open; half-open one probe; success resets | `s24_circuit_breaker.py` |
| Admission | token bucket on heavy spawns: rate from rate-limit `percentUsed` slope, burst 2; a refused spawn is advised "serialize", never denied | `s09_token_bucket.py` |
| Fan-out risk | `P(any slow) = 1-(1-p)^n` with p = measured 429+transport rate; shown when n ≥ 3 | `s03_fanout_tail.py` |
| Session anomaly | features `[cache_read, tokens_out, ttft, tool_calls, turn_len]` standardized with stored `(μ, σ)`; PCA k=2 from stored `(V_k, Λ_k)`; `Q = ‖x_c − V_k V_kᵀ x_c‖²`, `T² = Σ z_i²/λ_i`; each normalized to 0..1 by stored percentile before combining; score shown with the explain card (which term) | `m01_covariance_pca.py`, `math.md A5–A8` |
| Warm-up | PCA disabled until n ≥ 50 turns; `rank(Σ) ≤ min(d, n−1)`; Welford running stats; persist `(μ, σ, V_k, Λ_k)` only | `math.md A9` |
| Drift | baseline window vs rolling window; principal-angle `θ = arccos|v₀·v₁|` on PC1; rebaseline after persistence | `m03_subspace_drift.py`, `A7` |
| Robust outliers | MAD, modified z `0.6745·|x−med|/MAD > 3.5` for lane latency in benches only | `s19_median_robust.py` |
| Backend health | `A = MTBF/(MTBF+MTTR)`; series proxy × ollama × worker | `s20_availability.py` |
| Drain ETA | `backlog/(cap − arrive)` for the queue worker | `s12_backlog_drain.py` |
| Explicit non-rules | kurtosis and medians are descriptive, never alerts | `s17`, `s19` |

## 8. The one skill (`skills/datapce/SKILL.md`, ≤ 350 lines)

Sections: **Route** (delegate rather than switch your own model; read the evidence section the
plugin injects; pressure gate before fan-out; local lanes when the backend exists), **Verify**
(five gates + nine labels + grep-the-number — merged from disciplined-execution, verify-claims,
evidence-labels), **Review** (cross-validate: single reviewer or panel; pre-read optional),
**Ship** (public-repo hygiene, dependency vetting, unattended-loop rules). Backend commands appear
once, in Route. The ten skills in `apex-router-skills` are retired; their names stay as aliases in
the skill description for discoverability.

## 9. Backend boundary (`backend.ts`)

Detected by `~/.apex-router/ornith.env`, `pressure.json`, `~/.apex/telemetry.jsonl` existence.
When present the plugin calls, via `$.process`, only: `apex-router pressure --json`,
`apex-router route-advise --json`, `apex-router review-preread` (from the skill, not hooks). It
reads `conformance.jsonl`, `labeled_table.jsonl`, `offload_telemetry.jsonl`, `handoff_threshold.json`.
Absent: those cells read `—`, local lanes are hidden, pressure comes from `session.measure` rate
limits only.

## 10. Privacy and safety

Writes only under `~/.apex-router/` and `$.store`. Never stores prompt text, file contents, or
command text; descriptions are truncated dispatch labels. No network calls. No telemetry to anyone.
Stated verbatim in the listing and README. Mods run unsandboxed; the module's `tool.call` hook
never denies or rewrites a call — it observes `Bash` output only.

## 11. userConfig

`pane` (auto|command|off, default command), `enforce` (bool, default false), `budgetUsd` (number,
default 0 = off), `band` (bool, default true), `backendDir` (default `~/.apex-router`).

## 12. Degradation matrix

| Surface / state | Band | Pane | Status | Routing | Injection |
|---|---|---|---|---|---|
| terminal, backend | ✓ | ✓ | ✓ | ✓ | ✓ |
| terminal, no backend | ✓ (no conf/local) | ✓ (no Lanes) | ✓ | ✓ | ✓ |
| VS Code / Desktop web | — | — | ✓ | ✓ | ✓ |
| `claude -p` | — | — | — | ✓ (logs) | ✓ |

## 13. Testing

- `claude plugin test plugin/`: `observe` (rows from fixture events; nothing textual stored),
  `evidence` (state machine at n = 29/30/31, Wilson boundary, DRIFTING enter/exit streaks,
  rebaseline), `router` (advise never changes `e`; enforce only READY; hard limits), `inject`
  (section appended only to listed skills; ≤ 25 lines), `signals` (each formula against the
  numbers in the reference examples: burn (True, 11.5, 7.0), EWMA half-life 3.1, breaker
  opens at 3, bucket allows 10/15 then 5), `band`/`pane` render from fixture state, `classify`
  parity with the Python oracle.
- Math parity: `signals.ts` PCA/drift/penalty must reproduce the fixtures of
  the reference telemetry implementation (PCA, drift) and the p08 state-machine
  trace, so a later shared `pce-core` extraction is a move, not a rewrite.
- Python: existing suite + `test_version_parity.py`.
- Manual: hot-reload in a session; screenshots of band and pane in `docs/`.

## 14. Migration

- `hooks/agent-route-log.sh` and `hooks/cache-handoff-nudge.sh` retired (router.ts and the toast
  replace them); `ground-check.sh`, `memory-compact-nudge.sh`, `ornith-review-enqueue.sh` stay
  backend-side.
- `install.sh`: adds the marketplace from this repo and installs the plugin; backend steps as now.
- `apex-router-skills`: final commit adds a README pointer; marketplace entry deprecated.
- README §1 rewritten to §1 above; CHANGELOG 0.4.0.

## 15. Open questions

- Promotion target per cell (0.9 pass) is a prior; revisit after 30 days of labels.
- Whether `Workflow` scripts expose per-agent model in a way `agent.spawn` sees (verify in the
  first task; fall back to `tool.describe` guidance only).

## 16. ML core (`pce-core`) — added 2026-10-03

One numeric core, stdlib Python (`src/apex_router/core/`, later shared with other tools) with a
line-for-line TypeScript mirror (`plugin/hooks/core/`) for what runs in-engine. Parity: the TS
mirror passes the Python fixtures (the reference cross-check suites, and the
reference example outputs).

| Package | Contents | Source today | Product use |
|---|---|---|---|
| `linalg` | cyclic Jacobi (relative tol, sign-normalised, gap test); normal-equations solve with ridge; principal angles | reference `eigen`, `drift`; apex `tuner/readout.py:153` | PCA, drift, OLS |
| `stats` | Welford `(n, mean, M2)` **new**; EWMA float + fixed-point Q16 **new**; Wilson; Benjamini-Hochberg; paired bootstrap; ONE nearest-rank percentile | apex `stats.py` (dedupes 2 Wilson, 6 percentile, 3 bootstrap-index copies) | cells, band smoothing, promotion, benches |
| `anomaly` | max-abs prescale, z / robust-MAD scaling, PCA window, `Q = ‖x_c − V_kV_kᵀx_c‖²` **new**, `T²` with χ²_k(0.99) limit **new**, explain-card term split | reference `stats`, `pca` | session anomaly score (§7) |
| `drift` | principal-angle + `variance_l1` drift; Page CUSUM on standardized residuals (κ=0.5, h=4) **new**; S4b penalty state machine (ramp + enter/exit streaks) | reference `drift`; S4b spec (unimplemented) | cell regression (DRIFTING), rebaseline |
| `routing` | cells, promotion/confirmation gate + BH, ε-greedy 0.05, break-even direction | apex `gate.py`, `chain_planner.py`, `route_advise.py` | advise→enforce |
| `cost` | per-cell OLS `cost ≈ a + b·input_tokens` with R² and residual-trend alarm; budget burn | apex `readout.py:233` (tokens-on-bytes) generalised | "expected $" in advice; budget toasts |

Not added: SVD (Jacobi on ≤ 8×8 suffices), full-inverse Mahalanobis, bandits (no reward stream at
n), Bradley-Terry (no caller; removed), neural models (explainability is the product).
v1: all of the above except the S4b conditional permutation test (v1.1, needs Stage 4b data).

## 17. Efficiency contract — what the model actually needs (added 2026-10-03)

The plugin lives inside Claude Code and spends Claude's context. Every byte it injects is cache
and attention taken from the task, so the contract is:

1. **Decide, don't describe.** Mechanical decisions the model would otherwise guess at — task
   type, tier, pressure level, whether a cell is READY — are computed by the plugin and applied at
   `agent.spawn`. The model is told the result in one line, never asked to reason it out.
2. **Inject only at decision time, only what changes the decision.** `skill.prompt` evidence
   section ≤ 25 lines and only on the four planning/dispatch skills; `tool.describe` ≤ 2 lines;
   `prompt.compose` ≤ 4 lines. Nothing in CLAUDE.md, nothing per turn. A COLD cell injects
   nothing (no evidence → no text).
3. **Structured over prose.** Evidence rows are `task_type | tier effort | n ok% | tok(all) μ | dur μ | state` (v1 shows the ledger, not advice; see §18)
   — a table the model scans, not advice it must interpret. Basis strings are one clause.
4. **Fail open, never block.** A hook that cannot decide calls `next(e)`; the model never waits
   on the plugin (all file reads are off the hot path on `$.clock.every`, cached in `$.state`).
5. **Offer, then enforce only what is proven.** Advise mode is the default; enforce is per-cell,
   earned by labels, demoted by CUSUM. The model keeps `model:` it was explicitly given.
6. **Close the loop so the model evolves.** Every spawn's outcome becomes a label (v1: completion only; quality labels per §18); labels change
   the next evidence row. The plugin's own value is measured the same way: each injection is
   logged with the dispatch it preceded, so `route-advise` can report pass-rate with vs without
   the evidence section (self A/B). An injection that does not move outcomes is removed.
7. **Supersede quietly.** Where the plugin can see what a skill asks the model to do by hand
   (`apex-router pressure --check` before fan-out), it does it and shows the result in the band,
   and the skill text shrinks accordingly. The condensed skill is the floor, not the ceiling.

Acceptance for v1: injected text per session ≤ 2 KB at p95; advise-vs-requested agreement and
pass-rate reported in the pane (superseded for v1 by §18); no hook adds > 5 ms to `agent.spawn` at p99.

## 18. v1 quality gate and v1.1 quality labels (added 2026-10-04)

Supersedes, for v1, §1's listing wording and the READY/enforce parts of §5 and §17.

**Why.** The only completion label v1 sees is `turn.complete` reason `answer` and not aborted. On
real traffic that is ≈ 100% (the observed failures come from the auto-mode permission classifier
and never reach `agent.spawn`), so a cell reaches READY on completion alone in days
(review|sonnet ≈ 85 dispatches a week). READY on completion would reward the cheapest tier for
finishing.

**v1 behaviour.** `QUALITY_LABELS = false` (`plugin/hooks/evidence.ts`): no READY or inherited tier
advice, no evidence table in the planning skills; cells keep counting so the state exists when a
label arrives. Pressure shedding stays (policy, not evidence). The ledger shows completion and
cost: n, ok %, error kind, `tok(all) μ` (input + output + cache reads + cache writes), `dur μ`.
Tier cost comparisons are confounded by allocation (harder work goes to bigger tiers) and are a
record, not a recommendation.

**v1.1 quality-label candidates.** Each must yield both outcomes on this machine's traffic before
it can turn `QUALITY_LABELS` on:
1. **Parent's immediate same-task re-dispatch.** The parent dispatches the same normalized
   description again at a higher tier soon after (route-join's escalation inference, ≤ 2 h, same
   session). The earlier dispatch is labelled "insufficient".
2. **Test exit codes after codegen.** A `Bash` test command (pytest, npm test, cargo test, go test)
   whose exit code follows a generate/codegen dispatch in the same session, attributed to that
   dispatch: exit 0 → good, non-zero → bad. Observed through the existing `tool.call` Bash hook
   (exit code only; never command text).
3. **Operator verdict from the pane.** An explicit good/bad mark on a dispatch row in `/apex`,
   stored as a label on the route row.

A cell's READY state then needs `n ≥ 30` *quality* labels and Wilson lower bound ≥ 0.9, as §5
specifies.
