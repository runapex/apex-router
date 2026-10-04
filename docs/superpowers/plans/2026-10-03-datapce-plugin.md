# datapce Claude Code plugin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `datapce` (alias `apex-router`) as a Claude Code plugin — a hooks module that observes every subagent dispatch, keeps per-cell evidence, advises (and, only where proven, enforces) a model tier at `agent.spawn`, injects measurable evidence into the planning skills, and draws a band, a pane, a status entry and toasts — on top of one shared numeric core (`pce-core`) in stdlib Python with a line-for-line TypeScript mirror.

**Architecture:** `plugin/` is a mods plugin (hooks-module API, Claude Code 2.1.288): `register.ts` wires small modules (`observe`, `evidence`, `router`, `inject`, `backend`, `live`, `band`, `pane`) that share one `Runtime` object and the `$.state` atoms declared in `types/index.d.ts`. Pure logic lives in plain functions (unit-tested by importing them); hooks are thin and tested through the engine with an in-memory world beneath them. `src/apex_router/core/` is the Python numeric core (moved/reused code plus Welford, EWMA, Q/T², CUSUM, cell state machine); it is the oracle — `python -m apex_router.core.fixtures --write` generates TypeScript fixtures that `plugin/hooks/core/*.ts` must reproduce.

**Tech Stack:** Python ≥ 3.11 stdlib (repo venv 3.14.4, pytest); TypeScript run by `claude plugin test` / `claude plugin validate` (Claude Code 2.1.288, verified on this machine). No new third-party dependencies (none in Python, no npm packages at all). Optional type-check: `/opt/homebrew/bin/tsc` 6.0.3. Node v26.10.0 is present but not used (the plugin runs in the engine's own environment); `bun`, `tsx`, `deno` are not installed and are not needed.

**Spec:** `docs/superpowers/specs/2026-10-03-apex-router-plugin-design.md` (§1–§17, binding). Read it alongside this plan.

## Global Constraints

- Naming: plugin, marketplace and skill are `datapce` (alias `apex-router`); the backend CLI `apex-router`, the directory `~/.apex-router` and the package `src/apex_router` keep their names. The band/status prefix is `apex` and the command is `/apex` (spec §5, §6 verbatim).
- Version: "`pyproject.toml` is the source; `plugin/.claude-plugin/plugin.json` must match (`tests/test_version_parity.py`). One tag per release covers both." (§3)
- Runtime: "the plugin is a mods module (hooks-module API, GA in Claude Code 2.1.287). No DOM, no Node; everything through `$`. Stdlib-only Python stays the rule for the backend." (§3) `pce-core` is stdlib Python.
- Dependencies: no new third-party dependency in any task. If an executor believes one is needed, stop and run the `apex-workflow:dependency-vetting` skill before adding it.
- Privacy (§10): "Writes only under `~/.apex-router/` and `$.store`. Never stores prompt text, file contents, or command text; descriptions are truncated dispatch labels. No network calls. No telemetry to anyone." Descriptions are cut to 120 chars (§4). "the module's `tool.call` hook never denies or rewrites a call — it observes `Bash` output only."
- Efficiency (§17): "`skill.prompt` evidence section ≤ 25 lines and only on the four planning/dispatch skills; `tool.describe` ≤ 2 lines; `prompt.compose` ≤ 4 lines. Nothing in CLAUDE.md, nothing per turn. A COLD cell injects nothing." "injected text per session ≤ 2 KB at p95" — implemented as a hard per-session cap of 2048 bytes. "no hook adds > 5 ms to `agent.spawn` at p99." "all file reads are off the hot path on `$.clock.every`".
- Advise-only by default (§5, §17.5): enforce needs userConfig `enforce` or `/apex enforce on` AND a READY cell of its own. An explicit `model:` on the Agent call, forks, and Fable requests are never rewritten. Nothing "adaptive" acts without labels: ε-greedy exploration is not implemented (no labels exist yet); shedding and inherited advice stay advice until their own cell is READY.
- Measurable or omitted (§17.6): every injected section is logged with the session's arm (`evidence` 80% / `holdout` 20%, by session-id hash), every route_log row carries `inject_arm` and `injected`, and `apex-router injection-ab` reports pass rate per arm. `prompt.compose` is not hooked (it carries no decision and cannot be measured per dispatch).
- One condensed skill (`plugin/skills/datapce/SKILL.md`, ≤ 350 lines), not ten.
- Commits: one per task, message as given in the task, NO `Co-Authored-By` trailer.
- Green before every commit, run from `~/src/apex-router/.claude/worktrees/datapce-plugin`:
  `~/src/apex-router/.venv/bin/pytest -q` (baseline before Task 1: **1716 passed, 4 skipped**), `claude plugin test plugin` (all pass, once `plugin/` exists), `claude plugin validate plugin` (passes). Each task states the expected Python count; if it differs, re-count the tests you added — never edit a test to hit a number.
- One hook per event (engine rule, verified: a second `on('session.start', …)` without a matcher refuses the whole module with "registered twice without a matcher"). Events several modules need — `session.start`, `session.end`, `session.measure`, `skill.prompt` — are registered ONCE in `register.ts`, which calls each module's exported function in a fixed order (`identify` first). A module's own `install(on, rt)` registers only events no other module hooks, or distinct matchers (`ui.render` per component, `tool.call` `{ tool: 'Bash' }`, `command.run` `{ command: 'apex' }`).
- `$` never crosses an import (engine scan rule, verified: "$ is followed only into a function declared in this same file, never across an import"). `register.ts` binds every engine call the modules need into a `Host` object (`hostOf($)`, declared in `register.ts`, type in `host.ts`) — the reference plugin's pattern — and modules take `host: Host`. A module may still register its own hooks and use that hook's `$` inside the same file (render hooks, `turn.step`, `tool.call`).
- `on` is passed only to functions imported BY NAME (`import { install as observe } …; observe(on, rt)`), never to a namespace member like `observe.install(on, rt)` (engine scan rule, verified).
- `$.state`: a reference/atom is a const IN THE FILE that reads or writes it (engine scan rule, verified), never imported. Render files (`band.tsx`, `pane.tsx`) declare the atoms they `read`; `register.ts` declares the refs that `host.publish.*` writes with `$.state.set`. `$` is always spelled `$.noun.method(...)` at the call site.
- TypeScript imports use explicit `.ts` / `.tsx` suffixes (verified to load under `claude plugin test` 2.1.288). Types come from `'claude-code'` (import type) and the contract `'../types/index.d.ts'`.
- Parity tolerance: floats compare at 1e-9 relative (`tests/fixtures/close.ts`); integers, booleans and state names compare exactly.

## Review Focus

1. **API-key / gateway sessions** — `session.measure` with an empty `rateLimits` array and no `cost`: the band must draw only the context cell, pressure must come from burn/backend only, budget must stay off. (Tests: Task 1 "API-key sessions…", Task 16 "no rate limits".)
2. **Hot reload mid-session** — module variables (`rt`) start over while `$.state` and running subagents survive: a `turn.complete` for an agentId the new module never saw must be ignored without throwing or touching a cell. (Test: Task 13 "unknown agentId after a reload".)
3. **Corrupt or old persisted values** — a hand-edited or older-shape `$.store` value (`datapce.cells`, `datapce.stats`, `datapce.anomaly`, `datapce.profile`) must be dropped field by field, never crash `session.start`. (Tests: Task 12 "malformed store", Task 13 "malformed cells".)
4. **Unwritable or missing backend directory** — `tee -a` failing (exit ≠ 0) must keep rows in a bounded buffer (≤ 5000) and retry on the next flush; the plugin must keep working with no backend at all (band without `conf`/local cells). (Tests: Task 12 "flush failure keeps rows", Task 15 "no backend".)
5. **The same planning skill invoked many times in one session** — each invocation appends again; the 2 KB per-session cap must hold and the holdout arm must never inject. (Tests: Task 19 "session cap", "holdout".)

## API evidence (Claude Code 2.1.288 types)

Every hook/op this plan uses, with the declaration that permits it. Source: the engine-written `claude-code.d.ts` (first line `// Written by Claude Code 2.1.288.`; the plugin-authoring skill names its current path; once the mod loads, the same file is laid at `plugin/.claude-plugin/types/claude-code/index.d.ts`). Line numbers are for that build. Behaviours marked *probe* were confirmed by running `claude plugin test` on a scratch plugin before writing this plan.

| API | Declaration (line) | What the plan relies on |
|---|---|---|
| `agent.spawn` | `AgentSpawnInput` (239), `AgentSpawnResult` (331) | `model?` "A hook sets this to pick the subagent's model"; `fork`, `parentModel`, `parentAgentId`, `tool_use_id` pinned; result `{ model, agentId }` or `{ deny }`; "a hook that answers without `next` started none" → enforce is `next({ ...e, model })`, never a returned `{ model }` (*probe*: rewrite reached the bottom as `haiku`). |
| `turn.complete` | `TurnCompleteFields` (12478) | `agentId` "a subagent's id, as `$.agent.spawn` resolves it", `durationMs`, `usage`, `reason: 'answer'|'aborted'|'refusal'|'error'` → completion labels. |
| `turn.step` | `TurnStepInput` (12628), `TurnStepResult` (12688), `HookStream` (`AsyncGenerator` + `.result`) | streaming: hook is `async function*`; `for await` + `await stream.result` (*probe*). `model`, `effort`, `agentId`, `usage`, `stopReason`, `toolUses`. |
| `session.measure` | `SessionMeasureInput` (10410), `SessionRateLimit` (10597), `SessionCost` (10295) | `context.percent`, `rateLimits[{kind, percentUsed, resetsAt}]` ("empty off a subscription"), `cost?.usd`. |
| `skill.prompt` | `SkillPromptInput` (11195) / `SkillPromptResult` (11210) | `{ skill, text }` → `{ text }`. |
| `tool.describe` | `ToolDescribeInput` (12164) / `ToolDescribeResult` (12200); `InvalidatableEventName` (5252) | append to `description`, keep placement (`{ ...(await next(e)), description }`); `$.ui.invalidate('tool.describe')` to refresh (*probe*: append works). |
| `prompt.compose` | `PromptComposeInput` (7788) | exists; deliberately NOT used (see Global Constraints). |
| `tool.call` | `ToolCallResult` (`text` "set by core") | observe Bash output; return the result unchanged. |
| `command.run` / `$.command.register` | `CommandRunInput` (1610), `CommandRunResult` (1655: `text`, `context`), `CommandSpec` (`name`, `description`, `argumentHint`) | `/apex` and `/apex handoff` (template as `context`). |
| `ui.render` `AbovePrompt` | `RenderPropsOf.AbovePrompt` (9578): `hasSurvey`, `isWorking`, `maxRows`, `bodyColumns`, `scroll`, `view`; "Raised on the terminal and desktop surfaces only" | band (*probe*: drew from a `session.measure`-written atom). |
| `ui.render` `Pane`, `$.ui.open/close` | `Pane` props (after 9604), `PaneOpenArgs` (6933) | `/apex` pane, matcher `{ component: 'Pane', requestId: 'datapce' }`. |
| Elements | `Elements` (3594): terminal has `Raster`, `Image`, no `Svg`; desktop/vscode/mobile have `Svg`, no `Raster`/`Image`; `RasterProps` (8617) | bars are drawn as `Text` (`▇░`) so one tree validates on every surface; Raster/Image/Svg are not used (they would need per-surface trees for no added information). `Text` takes no `key` (*probe*): keys go on `Box`. |
| `$.ui.status/toast/invalidate` | `ui.status` (6550), `ui.toast` (6543, `timeoutMs`), `ui.invalidate` (6571) | status line, toasts. |
| `$.state`, `atom/read/update` | `state` (3176), `ReadFunction`; contract `PluginState` | drawing state; render hooks never write. |
| `$.store` | `store` (3143): JSON, ≤ 4 MiB total | `datapce.stats`, `datapce.cells`, `datapce.profile`, `datapce.anomaly`, `datapce.enforce`. |
| `$.fs` | `fs` (3015): `read`, `write` (creates dirs), `list`, `exists`, `stat` — no append | appends go through `$.process.run(['/usr/bin/tee','-a',path], { stdin })` (`ProcessRunInit.stdin`, 7538). |
| `$.process.run` | (3290) argv, no shell, 30 s default timeout | `tee -a`, `tail -n`, `rm -f`, `apex-router pressure --json`, `apex-router route-advise --json`. |
| `$.clock.every/now` | (3218) | 5 s flush, 60 s tick/poll. |
| `$.env.get('HOME')`, `$.session.id()` | `env` (3380, literal names), `session.id` (2592) | paths, arm. |
| globals | `performance.now()` (13896), `crypto.subtle.digest`, `TextEncoder` | latency, repo hash, byte counts. |
| `HookBudget` | `ms: 10_000` | hooks must stay far below; §17 sets 5 ms for `agent.spawn`. |
| testing kit | `'claude-code/testing'`: `test`, `describe`, `expect`, `mock` (store/clock/env), `$.ui.mount` (`MountTarget` 14792) | op hooks beneath answer `{ value }`/`{ deny }` (*probe*: `store.set` answered `{}` was refused with "returned neither { value } nor { deny }"); event bottoms answer the event's result. No `toBeCloseTo` → `close()` helper. |

Reference implementation consulted for structure: `~/.claude/plugins/marketplaces/claude-plugins-official/plugins/code-modernization/hooks/register.ts` (session.start registers commands and timers; `ui.render` hooks per site; tests seat an in-memory world beneath the plugin with `worldOf(on, files)`).

## File map

```
.claude-plugin/marketplace.json            Task 1   marketplace "datapce", one plugin → ./plugin
plugin/.claude-plugin/plugin.json          Task 1   name datapce, version == pyproject, userConfig (§11)
plugin/hooks/hooks.json                    Task 1   { "modules": ["./register.ts"] }
plugin/hooks/register.ts                   Task 1+  wiring only
plugin/hooks/runtime.ts                    Task 1   Options, Runtime, toastOnce, session identity
plugin/hooks/state.ts                      Task 1   $.state atoms + empty views
plugin/hooks/band.tsx                      Task 1, 17  band + status + handoff toast
plugin/types/index.d.ts                    Task 1   state contract + shared shapes
plugin/tsconfig.json                       Task 1   optional tsc
plugin/hooks/core/stats.ts linalg.ts       Task 7   pce-core mirror
plugin/hooks/core/anomaly.ts drift.ts cost.ts routing.ts   Task 8
plugin/hooks/classify.ts                   Task 9   classify_dispatch port
plugin/hooks/signals.ts                    Task 10  §7 formulas
plugin/hooks/tiers.ts evidence.ts          Task 11  cells → advice, rows, views
plugin/hooks/io.ts observe.ts              Task 12  §4 capture, flush, retention, profile
plugin/hooks/router.ts                     Task 13  §5 agent.spawn advise/enforce, labels
plugin/hooks/inject.ts                     Task 14  §5 skill.prompt / tool.describe, A/B arm
plugin/hooks/backend.ts                    Task 15  §9
plugin/hooks/live.ts                       Task 16  per-minute signals, anomaly, signal toasts
plugin/hooks/handoff.ts                    Task 17  structured handoff template
plugin/hooks/pane.tsx                      Task 18  /apex pane + command
plugin/skills/datapce/SKILL.md             Task 21  the one skill
plugin/tests/fixtures/{world,inputs,close}.ts   Task 1
plugin/tests/fixtures/{core,classify}.ts   Task 6   generated by apex_router.core.fixtures
plugin/tests/*.test.ts(x)                  every TS task
src/apex_router/core/__init__.py stats.py  Task 2   (moved + new)
src/apex_router/core/linalg.py             Task 3   (moved + new)
src/apex_router/core/anomaly.py drift.py   Task 4
src/apex_router/core/routing.py cost.py    Task 5
src/apex_router/core/fixtures.py           Task 6
src/apex_router/injection_ab.py            Task 20
tests/test_version_parity.py               Task 1
tests/test_core_*.py                       Tasks 2–6
tests/test_injection_ab.py                 Task 20
tests/test_datapce_skill.py                Task 21
tests/test_install_marketplace.py          Task 22
```

---

### Task 1: `plugin/` skeleton and a band drawn from `session.measure`

**Files:**
- Create: `.claude-plugin/marketplace.json`, `plugin/.claude-plugin/plugin.json`, `plugin/hooks/hooks.json`, `plugin/hooks/register.ts`, `plugin/hooks/host.ts`, `plugin/hooks/runtime.ts`, `plugin/hooks/state.ts`, `plugin/hooks/band.tsx`, `plugin/types/index.d.ts`, `plugin/tsconfig.json`
- Create: `plugin/tests/fixtures/world.ts`, `plugin/tests/fixtures/inputs.ts`, `plugin/tests/fixtures/close.ts`, `plugin/tests/band.test.tsx`, `tests/test_version_parity.py`
- Modify: `.gitignore` (append one line)

**Interfaces:**
- Consumes: nothing.
- Produces (used by every later TS task):
  - `types/index.d.ts`: `Level`, `Tier`, `TaskType`, `CellState`, `Arm`, `BreakerState`, `Welford`, `WelfordCov`, `Cell`, `LevelState`, `Breaker`, `TokenBucket`, `AnomalyModel`, `Measure`, `Dispatch`, `CellView`, `SignalsView`, `LaneStats`, `BackendView`, `ProfileStore`, `ProfileView`, `InjectView`, and `PluginState['datapce']`.
  - `host.ts`: `Host` — `now()`, `every(ms, fn): Timer`, `home()`, `sessionId()`, `read(path)`, `write(path, text)`, `exists(path)`, `list(path)`, `stat(path)`, `run(argv, init?)`, `storeGet(key)`, `storeSet(key, value)`, `toast(text)`, `status(text)`, `invalidate('tool.describe')`, `open(args)`, `close(id)`, `registerCommand(spec)`, `publish.{measure, dispatches, cells, signals, backend, profile, inject, enforce}(value)`.
  - `runtime.ts`: `Options`, `DEFAULTS`, `optionsOf(raw: PluginOptions): Options`, `MinuteBucket`, `BashEvent`, `Runtime`, `newRuntime(options): Runtime`, `expandHome(path, home): string`, `TOAST_EVERY_MS`, `toastOnce(host, rt, kind, text, now): boolean`, `identify(host, e: SessionStartInput, rt): Promise<void>`.
  - `state.ts`: `PLUGIN`, `PANE_ID`, `EMPTY_SIGNALS`, `EMPTY_BACKEND`, `EMPTY_PROFILE_STORE`, `EMPTY_INJECT`, `profileView(p): ProfileView`. No atoms (see the file's header).
  - Atom convention (every later task): a module that reads or writes `$.state` declares, in its own file, `const <name> = atom({ plugin: 'datapce', key: '<key>' } as const, <initial>)` with the key and initial value from this table: `measure` → `null as Measure | null`; `dispatches` → `[] as Dispatch[]`; `cells` → `[] as CellView[]`; `signals` → `EMPTY_SIGNALS`; `backend` → `EMPTY_BACKEND`; `profile` → `profileView(EMPTY_PROFILE_STORE)`; `inject` → `EMPTY_INJECT`; `enforce` → `false`; `bandHidden` → `false`.
  - `band.tsx`: `measureOf(e: SessionMeasureInput): Measure`, `meter(percent, width = 8): string`, `usd(x): string`, `measureCells(m: Measure): string[]`, `install(on, rt)` (registers `ui.render` `AbovePrompt` only).
  - `register.ts`: `hostOf($): Host` (file-private) and the shared events `session.start` / `session.measure` (later also `session.end`, `skill.prompt`, `agent.spawn`, `turn.complete`, `tool.describe`).
  - test fixtures: `worldOf(on, files?, sessionId?) → World` (seats `mock.clock` at `T0`; `world.clock.advance(ms)` runs timers), `T0`, `HOME`, `BACKEND`, `SESSION_ID`, `resolveModel(model)`, `drain(stream)`; `SESSION`, `MEASURE`, `BAND`, `PANE`, `command(name, args?)`, `spawnInput(over?)`, `stepInput(over?)`, `complete(over?)`; `close(actual, expected, tol?, label?)`.

- [ ] **Step 1: Write the failing Python version-parity test**

`tests/test_version_parity.py`:

```python
"""pyproject.toml is the version source; the plugin manifest and the marketplace must match it (spec §3)."""
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_plugin_json_matches_pyproject():
    manifest = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "datapce"
    assert manifest["version"] == _pyproject_version()


def test_marketplace_lists_the_one_plugin():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "datapce"
    [entry] = market["plugins"]
    assert entry["name"] == "datapce"
    assert entry["source"] == "./plugin"
    assert entry["category"] == "productivity"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_version_parity.py`
Expected: 2 failed (`FileNotFoundError` for `plugin/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`).

- [ ] **Step 3: Create the manifests**

`.claude-plugin/marketplace.json`:

```json
{
  "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
  "name": "datapce",
  "description": "datapce (apex-router): adaptive model routing you can see.",
  "owner": { "name": "datapce" },
  "plugins": [
    {
      "name": "datapce",
      "description": "Adaptive model routing you can see. A status bar and pane showing every subagent's tier, cost and why; routing advice that only enforces what it has measured on your machine; evidence injected into the planning tools you already use. Optional local-model lanes.",
      "author": { "name": "datapce" },
      "category": "productivity",
      "keywords": ["apex-router", "model-routing", "subagents", "evidence"],
      "source": "./plugin"
    }
  ]
}
```

`plugin/.claude-plugin/plugin.json` (version must equal `pyproject.toml`, currently `0.3.0`):

```json
{
  "name": "datapce",
  "version": "0.3.0",
  "description": "Adaptive model routing you can see (alias apex-router). Writes only under ~/.apex-router and the plugin store; never stores prompt text, file contents or command text; no network calls; no telemetry to anyone.",
  "author": { "name": "datapce" },
  "keywords": ["apex-router"],
  "types": "./types/index.d.ts",
  "userConfig": {
    "pane": {
      "type": "string",
      "title": "Pane",
      "description": "auto opens the /apex pane when a session starts (the engine seats it once the terminal is wide enough); command opens it only on /apex; off never opens it.",
      "default": "command",
      "options": ["auto", "command", "off"]
    },
    "enforce": {
      "type": "boolean",
      "title": "Enforce proven routes",
      "description": "Apply advice at dispatch, only for cells with 30+ labels whose Wilson lower bound clears the target. Off = advise only.",
      "default": false
    },
    "budgetUsd": {
      "type": "number",
      "title": "Daily budget (USD)",
      "description": "Spend per day the budget burn is measured against. 0 turns budget tracking off.",
      "default": 0
    },
    "band": {
      "type": "boolean",
      "title": "Status band",
      "description": "Show the one-line band above the prompt.",
      "default": true
    },
    "backendDir": {
      "type": "string",
      "title": "Backend directory",
      "description": "Where the optional apex-router backend keeps its files; the plugin writes its rows here too.",
      "default": "~/.apex-router"
    }
  }
}
```

`plugin/hooks/hooks.json`:

```json
{ "modules": ["./register.ts"] }
```

Append to `.gitignore`:

```
plugin/.claude-plugin/types/
```

- [ ] **Step 4: Run the Python test to verify it passes**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_version_parity.py`
Expected: `2 passed`.

- [ ] **Step 5: Write the state contract**

`plugin/types/index.d.ts`:

```ts
// datapce state contract: `claude plugin validate` holds every $.state key the module names to
// it. Also the value shapes the hooks modules share. Self-contained: no imports.

export type Level = 'GREEN' | 'AMBER' | 'RED'
export type Tier = 'haiku' | 'sonnet' | 'opus' | 'fable'
export type TaskType = 'explore' | 'review' | 'debug' | 'refactor' | 'generate' | 'mechanical' | 'synthesis' | 'other'
export type CellState = 'COLD' | 'WARMING' | 'READY' | 'DRIFTING'
export type Arm = 'evidence' | 'holdout'
export type BreakerState = 'closed' | 'open' | 'half-open'

/** Welford running moments: persist parameters (n, mean, M2), never rows. */
export type Welford = { n: number; mean: number; m2: number }
/** Multivariate Welford: mean vector and co-moment matrix. */
export type WelfordCov = { n: number; mean: number[]; c: number[][] }
/** One evidence cell (task_type × pressure × tier): the §5 state machine's whole state. */
export type Cell = {
  n: number
  pass: number
  state: CellState
  winN: number
  winPass: number
  below: number
  above: number
  cusum: number
  p0: number | null
}
export type LevelState = { level: Level; up: number; down: number }
export type Breaker = { fails: number; openedAt: number | null }
export type TokenBucket = { rate: number; burst: number; tokens: number; t: number }
/** Session-anomaly model: parameters only (A9). */
export type AnomalyModel = {
  cov: WelfordCov
  vectors: number[][]
  values: number[]
  sd: number[]
  fittedAt: number
  qHist: number[]
  t2Hist: number[]
}

export type Measure = {
  ctxPercent: number | null
  limitKind: string | null
  limitPercent: number | null
  resetsAt: string | null
  costUsd: number | null
}

export type Dispatch = {
  toolUseId: string
  agentId: string | null
  description: string
  subagentType: string
  taskType: TaskType
  requested: string
  resolved: string | null
  advised: Tier | null
  advisedState: 'COLD' | 'WARMING' | 'READY' | null
  applied: boolean
  basis: string
  outcome: 'running' | 'ok' | 'failed'
  startedAt: number
  durationMs: number | null
  tokens: number | null
  level: Level
}

export type CellView = {
  key: string
  taskType: string
  level: Level
  tier: Tier
  state: CellState
  n: number
  pass: number
  wilsonLo: number
  tokMean: number | null
}

export type SignalsView = {
  level: Level
  burnShort: number | null
  burnLong: number | null
  budgetBurn: number | null
  budgetBurnShort: number | null
  budgetBurnLong: number | null
  minutesToExhaust: number | null
  breakers: Record<string, BreakerState>
  anomaly: { score: number; term: 'Q' | 'T2' } | null
  fanoutRisk: number | null
  admissionRate: number
}

export type LaneStats = Record<string, { n: number; ok: number; escalated: number }>

export type BackendView = {
  present: boolean
  conformance: number | null
  families: Record<string, Level>
  lanes: LaneStats | null
  drainEtaS: number | null
  health: number | null
  verdicts: Record<string, string>
  handoffTokens: number | null
}

export type ProfileStore = {
  repos: string[]
  taskMix: Record<string, number>
  skills: Record<string, number>
  hours: number[]
  backend: boolean
}

export type ProfileView = {
  taskMix: Record<string, number>
  skills: Record<string, number>
  repos: number
  hours: number[]
  backend: boolean
}

export type InjectView = { arm: Arm; bytes: number; sections: number }

declare module 'claude-code' {
  interface PluginState {
    datapce: {
      measure: Measure | null
      dispatches: Dispatch[]
      cells: CellView[]
      signals: SignalsView
      backend: BackendView
      profile: ProfileView
      inject: InjectView
      enforce: boolean
      bandHidden: boolean
    }
  }
}
```

- [ ] **Step 6: Write `host.ts`, `state.ts` and `runtime.ts`**

`plugin/hooks/host.ts`:

```ts
// Every engine call the modules make, as plain functions. register.ts binds it from `$` (hostOf):
// the engine's scan follows `$` only into functions declared in the same file, so modules take a
// Host and never see `$` (the reference plugin's pattern).
import type { CommandSpec, FsEntry, FsStat, PaneOpenArgs, ProcessRunInit, ProcessRunResult, Timer } from 'claude-code'

import type { BackendView, CellView, Dispatch, InjectView, Measure, ProfileView, SignalsView } from '../types/index.d.ts'

export type Host = {
  now(): Promise<number>
  every(ms: number, fn: () => void): Timer
  home(): Promise<string>
  sessionId(): Promise<string>
  read(path: string): Promise<string>
  write(path: string, text: string): Promise<void>
  exists(path: string): Promise<boolean>
  list(path: string): Promise<FsEntry[]>
  stat(path: string): Promise<FsStat>
  run(argv: readonly string[], init?: ProcessRunInit): Promise<ProcessRunResult>
  storeGet(key: string): Promise<unknown>
  storeSet(key: string, value: unknown): Promise<void>
  toast(text: string): void
  status(text: string | undefined): void
  invalidate(event: 'tool.describe'): void
  open(args: PaneOpenArgs): Promise<unknown>
  close(id: string): Promise<void>
  registerCommand(spec: CommandSpec): Promise<unknown>
  publish: {
    measure(v: Measure | null): Promise<void>
    dispatches(v: Dispatch[]): Promise<void>
    cells(v: CellView[]): Promise<void>
    signals(v: SignalsView): Promise<void>
    backend(v: BackendView): Promise<void>
    profile(v: ProfileView): Promise<void>
    inject(v: InjectView): Promise<void>
    enforce(v: boolean): Promise<void>
  }
}
```

`plugin/hooks/state.ts`:

```ts
// Constants and empty views only. NO atoms here: the engine's scan requires every `atom(...)` that
// `read`/`update` use to be a const of the SAME file (verified: a shared atoms file fails to load
// with "the state library's update takes a source the scan can read ... written there or in a
// const of this file"). Each module declares the atoms it uses, with the same literal plugin/key.

import type { BackendView, InjectView, ProfileStore, ProfileView, SignalsView } from '../types/index.d.ts'

export const PLUGIN = 'datapce'
export const PANE_ID = 'datapce'

export const EMPTY_SIGNALS: SignalsView = {
  level: 'GREEN',
  burnShort: null,
  burnLong: null,
  budgetBurn: null,
  budgetBurnShort: null,
  budgetBurnLong: null,
  minutesToExhaust: null,
  breakers: {},
  anomaly: null,
  fanoutRisk: null,
  admissionRate: 10,
}

export const EMPTY_BACKEND: BackendView = {
  present: false,
  conformance: null,
  families: {},
  lanes: null,
  drainEtaS: null,
  health: null,
  verdicts: {},
  handoffTokens: null,
}

export const EMPTY_PROFILE_STORE: ProfileStore = {
  repos: [],
  taskMix: {},
  skills: {},
  hours: Array.from({ length: 24 }, () => 0),
  backend: false,
}

export const profileView = (p: ProfileStore): ProfileView => ({
  taskMix: p.taskMix,
  skills: p.skills,
  repos: p.repos.length,
  hours: p.hours,
  backend: p.backend,
})

export const EMPTY_INJECT: InjectView = { arm: 'evidence', bytes: 0, sections: 0 }
```

`plugin/hooks/runtime.ts`:

```ts
import type { PluginOptions, SessionStartInput } from 'claude-code'

import type {
  AnomalyModel,
  Arm,
  BackendView,
  Breaker,
  Cell,
  Dispatch,
  LevelState,
  ProfileStore,
  TokenBucket,
  Welford,
} from '../types/index.d.ts'
import type { Host } from './host.ts'
import { EMPTY_BACKEND, EMPTY_PROFILE_STORE } from './state.ts'

export type Options = {
  pane: 'auto' | 'command' | 'off'
  enforce: boolean
  budgetUsd: number
  band: boolean
  backendDir: string
}

export const DEFAULTS: Options = { pane: 'command', enforce: false, budgetUsd: 0, band: true, backendDir: '~/.apex-router' }

/** userConfig (§11) as typed options; anything malformed falls back to its default. */
export function optionsOf(raw: PluginOptions): Options {
  const pane = raw.pane === 'auto' || raw.pane === 'off' ? raw.pane : 'command'
  const budget = typeof raw.budgetUsd === 'number' && Number.isFinite(raw.budgetUsd) && raw.budgetUsd > 0 ? raw.budgetUsd : 0
  const dir = typeof raw.backendDir === 'string' && raw.backendDir.trim() !== '' ? raw.backendDir.trim() : DEFAULTS.backendDir
  return { pane, enforce: raw.enforce === true, budgetUsd: budget, band: raw.band !== false, backendDir: dir }
}

/** One minute of the live signals' raw counts. */
export type MinuteBucket = { steps: number; failed: number; spend: number }

/** A Bash call to a backend command, as observe classified it — never its text. */
export type BashEvent = { cmd: string; signal: string | null; isError: boolean; t: number }

/** Module-local session state shared by every hooks module (lost on hot reload; $.state is not). */
export type Runtime = {
  options: Options
  sessionId: string
  home: string
  backendDir: string
  surface: string | null
  arm: Arm
  level: LevelState
  enforce: boolean
  cells: Record<string, Cell>
  cellsDirty: boolean
  stats: Record<string, Record<string, Welford>>
  statsDirty: boolean
  profile: ProfileStore
  profileDirty: boolean
  rows: string[]
  routeRows: string[]
  dispatches: Map<string, Dispatch>
  byAgent: Map<string, string>
  injectedBytes: number
  injectedSections: number
  toastAt: Map<string, number>
  minute: MinuteBucket
  minutes: MinuteBucket[]
  lastCostUsd: number | null
  cacheRead: number
  stepFeatures: number[][]
  bashEvents: BashEvent[]
  breakers: Record<string, Breaker>
  bucket: TokenBucket
  heavySpawns: number[]
  limitHistory: { t: number; pct: number }[]
  anomaly: AnomalyModel | null
  backend: BackendView
  startedAt: number
}

export function newRuntime(options: Options): Runtime {
  return {
    options,
    sessionId: '',
    home: '',
    backendDir: options.backendDir,
    surface: null,
    arm: 'evidence',
    level: { level: 'GREEN', up: 0, down: 0 },
    enforce: options.enforce,
    cells: {},
    cellsDirty: false,
    stats: {},
    statsDirty: false,
    profile: EMPTY_PROFILE_STORE,
    profileDirty: false,
    rows: [],
    routeRows: [],
    dispatches: new Map(),
    byAgent: new Map(),
    injectedBytes: 0,
    injectedSections: 0,
    toastAt: new Map(),
    minute: { steps: 0, failed: 0, spend: 0 },
    minutes: [],
    lastCostUsd: null,
    cacheRead: 0,
    stepFeatures: [],
    bashEvents: [],
    breakers: {},
    bucket: { rate: 10, burst: 2, tokens: 2, t: 0 },
    heavySpawns: [],
    limitHistory: [],
    anomaly: null,
    backend: EMPTY_BACKEND,
    startedAt: 0,
  }
}

export const expandHome = (path: string, home: string): string =>
  path === '~' ? home : path.startsWith('~/') ? `${home}${path.slice(1)}` : path

export const TOAST_EVERY_MS = 600_000

/** §6: each toast kind at most once per 10 minutes. */
export function toastOnce(host: Host, rt: Runtime, kind: string, text: string, now: number): boolean {
  const last = rt.toastAt.get(kind)
  if (last !== undefined && now - last < TOAST_EVERY_MS) return false
  rt.toastAt.set(kind, now)
  host.toast(text)
  return true
}

/** session.start, first: who and where this session is. register.ts calls it before any module. */
export async function identify(host: Host, e: SessionStartInput, rt: Runtime): Promise<void> {
  rt.home = await host.home()
  rt.backendDir = expandHome(rt.options.backendDir, rt.home)
  rt.sessionId = await host.sessionId()
  rt.surface = e.surface
  rt.startedAt = await host.now()
}
```

- [ ] **Step 7: Write the test fixtures**

`plugin/tests/fixtures/close.ts`:

```ts
import { expect } from 'claude-code/testing'

/** Float parity at `tol` relative (absolute below 1). */
export function close(actual: number | null | undefined, expected: number, tol = 1e-9, label = ''): void {
  const ok = typeof actual === 'number' && Math.abs(actual - expected) <= tol * Math.max(1, Math.abs(expected))
  expect(ok, `${label} ${String(actual)} is not within ${tol} of ${expected}`).toBe(true)
}
```

`plugin/tests/fixtures/inputs.ts`:

```ts
import type { AgentSpawnInput, RenderInput, SessionMeasureInput, SessionStartInput, TurnCompleteInput, TurnStepInput } from 'claude-code'

export const SESSION: SessionStartInput = { cwd: '/work', surface: 'terminal', isInteractive: true }

export const MEASURE: SessionMeasureInput = {
  context: { window: 200000, tokens: 122000, percent: 61 },
  rateLimits: [
    { kind: 'seven_day', percentUsed: 20 },
    { kind: 'five_hour', percentUsed: 62, resetsAt: '2026-10-03T20:00:00Z' },
  ],
  cost: { usd: 4.12 },
  changed: ['context', 'rateLimits', 'cost'],
}

export const BAND = {
  component: 'AbovePrompt',
  requestId: 'band',
  viewport: { columns: 160, rows: 48 },
  props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 140, scroll: { offset: 0, bodyRows: 10 }, view: {} },
} as const satisfies Omit<RenderInput<'AbovePrompt'>, 'surface'>

export const PANE = {
  component: 'Pane',
  requestId: 'datapce',
  viewport: { columns: 180, rows: 48, isFullscreen: true },
  props: { title: 'datapce', isFocused: false, bodyColumns: 72, placement: 'dock', scroll: { offset: 0, bodyRows: 44 }, view: {} },
} as const satisfies Omit<RenderInput<'Pane'>, 'surface'>

export const command = (name: string, args = '') => ({
  command: name,
  args,
  origin: { kind: 'composer' as const },
  presentation: { isFullscreen: true, columns: 180 },
})

export const spawnInput = (over: Partial<AgentSpawnInput> = {}): AgentSpawnInput => ({
  tool_use_id: 'toolu_1',
  prompt: 'Find where the pressure gate reads its state file',
  description: 'Find pressure gate state file',
  subagentType: 'Explore',
  provider: { plugin: 'engine', tier: 'core' } as AgentSpawnInput['provider'],
  parentModel: 'claude-opus-5-5',
  background: false,
  fork: false,
  ...over,
})

export const stepInput = (over: Partial<TurnStepInput> = {}): TurnStepInput => ({
  turnId: 'turn-1',
  index: 0,
  model: 'claude-opus-5-5',
  messageCount: 3,
  ...over,
})

export const complete = (over: Partial<TurnCompleteInput> = {}): TurnCompleteInput =>
  ({
    answer: 'done',
    durationMs: 41000,
    isAborted: false,
    turnId: 'sub-turn-1',
    agentId: 'agent-1',
    reason: 'answer',
    usage: { input_tokens: 30000, output_tokens: 11000, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, model: 'claude-sonnet-5-5' },
    ...over,
  }) as TurnCompleteInput
```

`plugin/tests/fixtures/world.ts`:

```ts
import type { On, TurnStepInput, TurnStepResult } from 'claude-code'
import { mock } from 'claude-code/testing'
import type { MockClock } from 'claude-code/testing'

export const HOME = '/home/u'
/** 2026-10-03T12:00:00Z — the world's clock starts here (a mock: it moves only on advance). */
export const T0 = Date.UTC(2026, 9, 3, 12, 0, 0)
export const BACKEND = `${HOME}/.apex-router`
export const SESSION_ID = 'sess-0001'

export type Answer = { exitCode: number; stdout: string }

/** The world beneath the plugin, in memory, and a record of what the plugin asked of it. */
export type World = {
  clock: MockClock
  files: Map<string, string>
  mtimes: Map<string, number>
  appended: Map<string, string[]>
  runs: string[][]
  asked: string[]
  store: Map<string, unknown>
  toasts: string[]
  statuses: (string | undefined)[]
  opened: string[]
  closed: string[]
  commands: string[]
  invalidated: string[]
  spawned: { model: string | undefined; subagentType: string }[]
  denySpawn: boolean
  bashOutput: string
  step: Partial<TurnStepResult>
  answer: (argv: readonly string[]) => Answer | null
}

export const RESOLVED: Record<string, string> = {
  haiku: 'claude-haiku-4-5',
  sonnet: 'claude-sonnet-5-5',
  opus: 'claude-opus-5-5',
  fable: 'claude-fable-5',
}

export function resolveModel(model: string): string {
  const low = model.toLowerCase()
  for (const [tier, id] of Object.entries(RESOLVED)) if (low.includes(tier)) return id
  return model
}

const run = (stdout: string, exitCode = 0) => ({
  value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})

export function worldOf(on: On, files: Readonly<Record<string, string>> = {}, sessionId = SESSION_ID): World {
  const world: World = {
    clock: mock.clock(on, { now: T0 }),
    files: new Map(Object.entries(files)),
    mtimes: new Map(),
    appended: new Map(),
    runs: [],
    asked: [],
    store: new Map(),
    toasts: [],
    statuses: [],
    opened: [],
    closed: [],
    commands: [],
    invalidated: [],
    spawned: [],
    denySpawn: false,
    bashOutput: '',
    step: {},
    answer: () => null,
  }
  const isDir = (p: string) => [...world.files.keys()].some(f => f.startsWith(`${p}/`))

  on('fs.read', ($, e) => {
    world.asked.push(`read ${e.path}`)
    const text = world.files.get(e.path)
    return text === undefined ? { deny: `ENOENT: ${e.path}` } : { value: text }
  })
  on('fs.exists', ($, e) => {
    world.asked.push(`exists ${e.path}`)
    return { value: world.files.has(e.path) || isDir(e.path) }
  })
  on('fs.write', ($, e) => {
    world.files.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.stat', ($, e) => {
    world.asked.push(`stat ${e.path}`)
    const text = world.files.get(e.path)
    if (text !== undefined) return { value: { kind: 'file' as const, size: text.length, mtimeMs: world.mtimes.get(e.path) ?? 0, isLink: false } }
    return isDir(e.path) ? { value: { kind: 'dir' as const, size: 0, mtimeMs: 0, isLink: false } } : { deny: `ENOENT: ${e.path}` }
  })
  on('fs.list', ($, e) => {
    world.asked.push(`list ${e.path}`)
    const names = new Map<string, 'file' | 'dir'>()
    for (const file of world.files.keys()) {
      if (!file.startsWith(`${e.path}/`)) continue
      const rest = file.slice(e.path.length + 1)
      names.set(rest.split('/')[0] ?? '', rest.includes('/') ? 'dir' : 'file')
    }
    if (names.size === 0 && !isDir(e.path)) return { deny: `ENOENT: ${e.path}` }
    return {
      value: [...names.entries()].sort().map(([name, kind]) => ({
        name,
        kind,
        size: kind === 'file' ? (world.files.get(`${e.path}/${name}`)?.length ?? 0) : 0,
        mtimeMs: world.mtimes.get(`${e.path}/${name}`) ?? 0,
        isLink: false,
      })),
    }
  })
  on('process.run', ($, e) => {
    const argv = [...e.argv]
    world.runs.push(argv)
    const custom = world.answer(argv)
    if (custom !== null) return run(custom.stdout, custom.exitCode)
    const stdin = e.init?.stdin ?? ''
    if (argv[0] === '/usr/bin/tee' && argv[1] === '-a' && argv[2] !== undefined) {
      const lines = stdin.split('\n').filter(l => l !== '')
      world.appended.set(argv[2], [...(world.appended.get(argv[2]) ?? []), ...lines])
      return run(stdin)
    }
    if (argv[0] === '/usr/bin/tail' && argv[1] === '-n') {
      const text = world.files.get(argv[3] ?? '')
      if (text === undefined) return run('', 1)
      const lines = text.split('\n').filter(l => l !== '')
      return run(`${lines.slice(-Number(argv[2])).join('\n')}\n`)
    }
    if (argv[0] === '/bin/rm' && argv[1] === '-f') {
      for (const p of argv.slice(2)) world.files.delete(p)
      return run('')
    }
    return run('', 127)
  })
  on('store.get', ($, e) => ({ value: world.store.get(e.key) }))
  on('store.set', ($, e) => {
    world.store.set(e.key, e.value)
    return { value: undefined }
  })
  on('env.get', ($, e) => ({ value: e.name === 'HOME' ? HOME : undefined }))
  on('session.id', () => ({ value: sessionId }))
  on('ui.toast', ($, e) => {
    world.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', ($, e) => {
    world.statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.open', ($, e) => {
    world.opened.push(e.id)
    return { value: { isPlaced: true } } as never
  })
  on('ui.close', ($, e) => {
    world.closed.push(e.id)
    return { value: undefined }
  })
  on('ui.invalidate', ($, e) => {
    world.invalidated.push(e.event)
    return { value: undefined }
  })
  on('command.register', ($, e) => {
    world.commands.push(e.name)
    return { value: { command: e.name } } as never
  })

  // Engine bottoms for the events the plugin hooks (a site the plugin passes on draws nothing).
  on('ui.render', () => ({ type: 'Box', children: [] }) as never)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.end', ($, e) => ({ sessionId: e.sessionId }))
  on('session.measure', ($, e) => ({ changed: e.changed }))
  on('agent.spawn', ($, e) => {
    world.spawned.push({ model: e.model, subagentType: e.subagentType })
    if (world.denySpawn) return { deny: 'denied by policy' }
    return { model: resolveModel(e.model ?? e.parentModel), agentId: `agent-${world.spawned.length}` }
  })
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('skill.prompt', ($, e) => ({ text: e.text }))
  on('tool.describe', ($, e) => ({ description: e.description }))
  on('tool.call', () => ({ result: { stdout: world.bashOutput, stderr: '' } }) as never)
  on('turn.step', async function* ($, e: TurnStepInput) {
    const result: TurnStepResult = {
      turnId: e.turnId,
      index: e.index,
      answer: '',
      toolUses: [],
      stopReason: 'end_turn',
      usage: { input_tokens: 100, output_tokens: 50, cache_read_input_tokens: 1000, cache_creation_input_tokens: 0, model: resolveModel(e.model) },
      ...world.step,
    }
    yield { kind: 'stop', stopReason: result.stopReason, usage: result.usage } as never
    return result
  })
  return world
}

export async function drain(stream: AsyncIterable<unknown>): Promise<void> {
  for await (const _chunk of stream) {
    // read to the end so the plugin's generator finishes
  }
}
```

- [ ] **Step 8: Write the failing band test**

`plugin/tests/band.test.tsx`:

```tsx
import { describe, expect, test } from 'claude-code/testing'

import { measureCells, measureOf, meter } from '../hooks/band.tsx'
import { optionsOf } from '../hooks/runtime.ts'
import { BAND, MEASURE, SESSION } from './fixtures/inputs.ts'
import { worldOf } from './fixtures/world.ts'

const SURFACES = ['terminal', 'desktop'] as const

describe('options', () => {
  test('userConfig defaults; malformed values fall back', () => {
    const d = { pane: 'command', enforce: false, budgetUsd: 0, band: true, backendDir: '~/.apex-router' }
    expect(optionsOf({})).toEqual(d)
    expect(optionsOf({ pane: 'nope', budgetUsd: -3, backendDir: '  ' })).toEqual(d)
    expect(optionsOf({ pane: 'auto', enforce: true, budgetUsd: 10, band: false, backendDir: '/x' })).toEqual({
      pane: 'auto', enforce: true, budgetUsd: 10, band: false, backendDir: '/x',
    })
  })
})

describe('band from session.measure', () => {
  test('meter clamps and rounds', () => {
    expect(meter(61)).toBe('▇▇▇▇▇░░░')
    expect(meter(0)).toBe('░░░░░░░░')
    expect(meter(140)).toBe('▇▇▇▇▇▇▇▇')
  })

  test('measureOf keeps the tightest rate-limit window', () => {
    const m = measureOf(MEASURE)
    expect(m).toEqual({ ctxPercent: 61, limitKind: 'five_hour', limitPercent: 62, resetsAt: '2026-10-03T20:00:00Z', costUsd: 4.12 })
    expect(measureCells(m)).toEqual(['5h:62%', 'ctx ▇▇▇▇▇░░░ 61%', '$4.12'])
  })

  test('API-key sessions (no rate limits, no cost) draw only what they have', () => {
    const m = measureOf({ context: { window: 200000, tokens: 20000, percent: 10 }, rateLimits: [], changed: ['context'] })
    expect(m).toEqual({ ctxPercent: 10, limitKind: null, limitPercent: null, resetsAt: null, costUsd: null })
    expect(measureCells(m)).toEqual(['ctx ▇░░░░░░░ 10%'])
  })

  test('nothing draws before the first measurement', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ plugin: 'datapce', surface, ...BAND })
      expect(await ui.find({ key: 'datapce-band-line' })).toBeUndefined()
      await ui.unmount()
    }
  })

  test('draws on terminal and desktop once measured; Hide hides it', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure(MEASURE)
    for (const surface of SURFACES) {
      const ui = await $.ui.mount({ plugin: 'datapce', surface, ...BAND })
      expect((await ui.find({ key: 'datapce-band-line' }))?.text).toBe('apex  5h:62%  ctx ▇▇▇▇▇░░░ 61%  $4.12')
      await ui.unmount()
    }
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'terminal', ...BAND })
    await ui.press({ key: 'datapce-hide' })
    expect(await ui.find({ key: 'datapce-band-line' })).toBeUndefined()
    await ui.unmount()
  })

  test('a survey holds the band', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure(MEASURE)
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'terminal', ...BAND, props: { ...BAND.props, hasSurvey: true } })
    expect(await ui.find({ key: 'datapce-band-line' })).toBeUndefined()
    await ui.unmount()
  })

  test('band: false never draws', { options: { band: false } }, async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure(MEASURE)
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'terminal', ...BAND })
    expect(await ui.find({ key: 'datapce-band-line' })).toBeUndefined()
    await ui.unmount()
  })
})
```

- [ ] **Step 9: Run it to verify it fails**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/band.tsx` cannot be resolved (module not found).

- [ ] **Step 10: Write `band.tsx` and `register.ts`**

`plugin/hooks/band.tsx`:

```tsx
import type { On, SessionMeasureInput } from 'claude-code'
import { atom, read, update } from 'claude-code'

import type { Measure } from '../types/index.d.ts'
import type { Runtime } from './runtime.ts'

const measure = atom({ plugin: 'datapce', key: 'measure' } as const, null as Measure | null)
const bandHidden = atom({ plugin: 'datapce', key: 'bandHidden' } as const, false)

const LIMIT_LABEL: Record<string, string> = { five_hour: '5h', seven_day: '7d', spend_limit: 'spend' }

/** The figures the band draws, from one session.measure: the tightest window, context, cost. */
export function measureOf(e: SessionMeasureInput): Measure {
  let tight: SessionMeasureInput['rateLimits'][number] | undefined
  for (const w of e.rateLimits) if (tight === undefined || w.percentUsed > tight.percentUsed) tight = w
  return {
    ctxPercent: e.context.percent ?? null,
    limitKind: tight?.kind ?? null,
    limitPercent: tight?.percentUsed ?? null,
    resetsAt: tight?.resetsAt ?? null,
    costUsd: e.cost?.usd ?? null,
  }
}

export function meter(percent: number, width = 8): string {
  const clamped = Math.max(0, Math.min(100, percent))
  const full = Math.round((clamped / 100) * width)
  return '▇'.repeat(full) + '░'.repeat(width - full)
}

export const usd = (x: number): string => `$${x.toFixed(2)}`

export function measureCells(m: Measure): string[] {
  const out: string[] = []
  if (m.limitPercent !== null) out.push(`${LIMIT_LABEL[m.limitKind ?? ''] ?? m.limitKind}:${Math.round(m.limitPercent)}%`)
  if (m.ctxPercent !== null) out.push(`ctx ${meter(m.ctxPercent)} ${Math.round(m.ctxPercent)}%`)
  if (m.costUsd !== null) out.push(usd(m.costUsd))
  return out
}

export function install(on: On, rt: Runtime): void {
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!rt.options.band || e.props.hasSurvey) return next(e)
    const m = await read($, measure)
    if (m === null || (await read($, bandHidden))) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const line = ['apex', ...measureCells(m)].join('  ')
    return (
      <Box key="datapce-band" flexDirection="row" gap={1}>
        <Box key="datapce-band-line">
          <Text wrap="truncate-end">{line}</Text>
        </Box>
        <Button key="datapce-hide" label="Hide" plain onPress={() => update($, bandHidden, () => true)} />
      </Box>
    )
  })
}
```

`plugin/hooks/register.ts`:

```ts
import type { EngineInterface, Register } from 'claude-code'

import { install as band, measureOf } from './band.tsx'
import type { Host } from './host.ts'
import { identify, newRuntime, optionsOf } from './runtime.ts'

// Wiring only. Shared events are registered once here (engine rule) and every engine call the
// modules make is bound into a Host here (the engine follows `$` only within this file).

const MEASURE = { plugin: 'datapce', key: 'measure' } as const
const DISPATCHES = { plugin: 'datapce', key: 'dispatches' } as const
const CELLS = { plugin: 'datapce', key: 'cells' } as const
const SIGNALS = { plugin: 'datapce', key: 'signals' } as const
const BACKEND = { plugin: 'datapce', key: 'backend' } as const
const PROFILE = { plugin: 'datapce', key: 'profile' } as const
const INJECT = { plugin: 'datapce', key: 'inject' } as const
const ENFORCE = { plugin: 'datapce', key: 'enforce' } as const

function hostOf($: EngineInterface): Host {
  return {
    now: () => $.clock.now(),
    every: (ms, fn) => $.clock.every(ms, fn),
    home: async () => (await $.env.get('HOME')) ?? '',
    sessionId: () => $.session.id(),
    read: path => $.fs.read(path),
    write: (path, text) => $.fs.write(path, text),
    exists: path => $.fs.exists(path),
    list: path => $.fs.list(path),
    stat: path => $.fs.stat(path),
    run: (argv, init) => $.process.run(argv, init),
    storeGet: key => $.store.get(key),
    storeSet: (key, value) => $.store.set(key, value),
    toast: text => $.ui.toast(text),
    status: text => $.ui.status(text),
    invalidate: event => $.ui.invalidate(event),
    open: args => $.ui.open(args),
    close: id => $.ui.close({ id }),
    registerCommand: spec => $.command.register(spec),
    publish: {
      measure: async v => void (await $.state.set(MEASURE, v)),
      dispatches: async v => void (await $.state.set(DISPATCHES, v)),
      cells: async v => void (await $.state.set(CELLS, v)),
      signals: async v => void (await $.state.set(SIGNALS, v)),
      backend: async v => void (await $.state.set(BACKEND, v)),
      profile: async v => void (await $.state.set(PROFILE, v)),
      inject: async v => void (await $.state.set(INJECT, v)),
      enforce: async v => void (await $.state.set(ENFORCE, v)),
    },
  }
}

export const register: Register = (on, raw) => {
  const rt = newRuntime(optionsOf(raw))

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    await identify(hostOf($), e, rt)
    return r
  })

  on('session.measure', async ($, e, next) => {
    await hostOf($).publish.measure(measureOf(e))
    return next(e)
  })

  band(on, rt)
}
```

`plugin/tsconfig.json` (optional `tsc`; the engine lays `.claude-plugin/types` when it loads the plugin from this folder):

```json
{
  "compilerOptions": {
    "target": "es2023", "lib": ["es2023"], "types": [],
    "module": "esnext", "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "strict": true, "noUncheckedIndexedAccess": true,
    "noEmit": true, "skipLibCheck": true,
    "jsx": "react", "jsxFactory": "h", "jsxFragmentFactory": "Fragment"
  },
  "include": [".claude-plugin/types", "hooks", "types", "tests"]
}
```

- [ ] **Step 11: Run TS tests and validation**

Run: `claude plugin test plugin`
Expected: `8 pass`, `0 fail`.

Run: `claude plugin validate plugin`
Expected: ends `✔ Validation passed`; the hooks line lists `session.start, session.measure, ui.render{component=AbovePrompt}`; state reads/writes list `datapce.measure`, `datapce.bandHidden`.

Run: `claude plugin validate .`
Expected: the marketplace validates (`✔ Validation passed`, warnings allowed).

Optional (only if `plugin/.claude-plugin/types/` exists, i.e. after a `claude --plugin-dir plugin` session loaded it): `tsc -p plugin` → no errors.

- [ ] **Step 12: Full suite and commit**

Run: `~/src/apex-router/.venv/bin/pytest -q`
Expected: `1718 passed, 4 skipped`.

```bash
git add .claude-plugin/marketplace.json plugin .gitignore tests/test_version_parity.py
git commit -m "feat(plugin): datapce skeleton — manifests, state contract, band from session.measure"
```

---

### Task 2: `pce-core` stats — move Wilson/BH/bootstrap, add Welford, EWMA, one nearest-rank

**Files:**
- Create: `src/apex_router/core/__init__.py`, `src/apex_router/core/stats.py`, `tests/test_core_stats.py`
- Modify: `src/apex_router/stats.py` (becomes a re-export shim; `bradley_terry` removed per spec §16 "Bradley-Terry (no caller; removed)"), `src/apex_router/ornith/state_bench.py:101-109` (`wilson_ci` delegates to core), `tests/test_amr_stats.py` (drop the six `test_bt_*` tests and their header)

**Interfaces:**
- Consumes: existing `apex_router.stats` functions (moved verbatim).
- Produces (`apex_router.core.stats`): `wilson_ci(k, n, z=1.96) -> (lo, hi)` (raises on n ≤ 0), `benjamini_hochberg(pvalues, alpha=0.05) -> list[bool]`, `paired_bootstrap_ci(...)`, `paired_bootstrap_pvalue(...)`, `welford_new() -> {"n","mean","m2"}`, `welford_push(w, x) -> dict`, `welford_variance(w) -> float`, `welford_cov_new(d) -> {"n","mean","c"}`, `welford_cov_push(s, x) -> dict`, `covariance(s) -> list[list[float]]`, `ewma(prev, x, a=0.2)`, `half_life(a)`, `Q16 = 65536`, `to_q16(x) -> int`, `ewma_q16(prev, x, a_q16=13107) -> int`, `nearest_rank(sorted_values, q) -> value | None` (q in (0, 1]).

Dedupe scope (resolution of §16 "dedupes 2 Wilson, 6 percentile, 3 bootstrap-index copies"): copies with identical semantics are deduped (the two Wilsons; the bootstrap index lives only in the moved `paired_bootstrap_ci`). Different estimators are NOT merged because merging would change published numbers: `proxy_engine/tuner/readout.py:_percentile` (linear interpolation), `chain_bench.py:58-59` (floor index). `scripts/handoff_threshold.py:nearest_rank_percentile` runs standalone under the system Python (no package import guarantee), so it keeps its copy and a parity test pins it to `core.nearest_rank`.

- [ ] **Step 1: Write the failing tests**

`tests/test_core_stats.py`:

```python
"""pce-core stats: moved functions are re-exported unchanged; Welford/EWMA/nearest-rank match
their reference formulas (two-pass moments, s = a·x + (1-a)·s, s[ceil(q·n)-1])."""
import importlib.util
import math
from pathlib import Path

from apex_router import stats as shim
from apex_router.core import stats as core

ROOT = Path(__file__).resolve().parents[1]
XS = [10, 11, 9, 10, 50, 52, 49, 51, 10, 10]


def test_shim_reexports_the_moved_functions():
    assert shim.wilson_ci is core.wilson_ci
    assert shim.benjamini_hochberg is core.benjamini_hochberg
    assert shim.paired_bootstrap_ci is core.paired_bootstrap_ci
    assert shim.paired_bootstrap_pvalue is core.paired_bootstrap_pvalue


def test_bradley_terry_removed():
    assert not hasattr(shim, "bradley_terry")
    assert not hasattr(core, "bradley_terry")


def test_welford_matches_two_pass():
    w = core.welford_new()
    for x in XS:
        w = core.welford_push(w, x)
    mean = sum(XS) / len(XS)
    var = sum((x - mean) ** 2 for x in XS) / (len(XS) - 1)
    assert w["n"] == 10
    assert math.isclose(w["mean"], mean, rel_tol=1e-12)
    assert math.isclose(core.welford_variance(w), var, rel_tol=1e-12)


def test_welford_variance_needs_two_samples():
    assert core.welford_variance(core.welford_push(core.welford_new(), 5.0)) == 0.0


def test_welford_covariance_matches_two_pass():
    s = core.welford_cov_new(2)
    for row in ([1, 2], [2, 4], [3, 7]):
        s = core.welford_cov_push(s, row)
    cov = core.covariance(s)
    assert math.isclose(cov[0][0], 1.0)
    assert math.isclose(cov[1][1], 19 / 3)
    assert math.isclose(cov[0][1], 2.5) and math.isclose(cov[1][0], 2.5)


def test_ewma_reference_series_alpha_0_2():
    s, out = None, []
    for x in XS:
        s = core.ewma(s, x, 0.2)
        out.append(round(s, 1))
    assert out == [10.0, 10.2, 10.0, 10.0, 18.0, 24.8, 29.6, 33.9, 29.1, 25.3]


def test_half_life():
    assert round(core.half_life(0.2), 1) == 3.1
    assert round(core.half_life(0.5), 1) == 1.0


def test_ewma_q16_tracks_the_float_ewma():
    q = f = None
    for x in XS:
        q = core.ewma_q16(q, x)
        f = core.ewma(f, x, 0.2)
        assert abs(q / core.Q16 - f) < 1e-3


def test_to_q16_rounds_half_up_not_to_even():
    assert core.to_q16(0.5 / core.Q16) == 1
    assert core.to_q16(2.5 / core.Q16) == 3
    assert core.to_q16(-0.5 / core.Q16) == 0


def test_nearest_rank_textbook_example():
    v = [15, 20, 35, 40, 50]
    assert [core.nearest_rank(v, q) for q in (0.05, 0.3, 0.4, 0.5, 1.0)] == [15, 20, 20, 35, 50]
    assert core.nearest_rank([], 0.5) is None


def test_wilson_all_pass_lower_bound_is_n_over_n_plus_z_squared():
    # The READY boundary in §5: with target 0.9, n/(n+z²) first clears 0.9 at n = 35.
    for n in (30, 34, 35):
        lo, _ = core.wilson_ci(n, n)
        assert math.isclose(lo, n / (n + 1.96 ** 2), rel_tol=1e-12)
    assert core.wilson_ci(34, 34)[0] < 0.9 <= core.wilson_ci(35, 35)[0]


def test_state_bench_wilson_delegates_to_core():
    from apex_router.ornith import state_bench
    assert state_bench.wilson_ci(0, 0) == (0.0, 1.0)
    assert state_bench.wilson_ci(7, 10) == core.wilson_ci(7, 10)


def test_handoff_script_percentile_matches_core():
    spec = importlib.util.spec_from_file_location("handoff_threshold", ROOT / "scripts" / "handoff_threshold.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    v = [3, 1, 4, 1, 5, 9, 2, 6]
    s = sorted(v)
    for pct in (5, 30, 40, 50, 80, 100):
        assert script.nearest_rank_percentile(s, pct) == core.nearest_rank(s, pct / 100.0)
```

- [ ] **Step 2: Run to verify failure**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_core_stats.py`
Expected: collection error — `ModuleNotFoundError: No module named 'apex_router.core'`.

- [ ] **Step 3: Create the core package and the new stats module**

`src/apex_router/core/__init__.py`:

```python
"""pce-core: the one numeric core (stdlib only).

stats · linalg · anomaly · drift · routing · cost. plugin/hooks/core/*.ts mirrors the parts that
run in-engine; `python -m apex_router.core.fixtures --write` generates the parity fixtures.
"""
```

`src/apex_router/core/stats.py` (the `# @@MOVED@@` line is replaced in Step 4):

```python
"""pce-core stats — the one copy of the numeric primitives (stdlib only).

Moved here unchanged from apex_router.stats (which re-exports them): wilson_ci,
benjamini_hochberg, paired_bootstrap_ci, paired_bootstrap_pvalue.
New: Welford running moments (scalar and covariance), EWMA (float and Q16 fixed point), and the
one nearest-rank percentile. plugin/hooks/core/stats.ts mirrors the in-engine subset.
"""
from __future__ import annotations

import math
import random
from typing import List, Tuple

# @@MOVED@@


# --- Welford: persist parameters (n, mean, M2), never rows ---------------------------------------

def welford_new() -> dict:
    return {"n": 0, "mean": 0.0, "m2": 0.0}


def welford_push(w: dict, x: float) -> dict:
    n = w["n"] + 1
    delta = x - w["mean"]
    mean = w["mean"] + delta / n
    return {"n": n, "mean": mean, "m2": w["m2"] + delta * (x - mean)}


def welford_variance(w: dict) -> float:
    """Sample variance M2/(n-1); 0.0 below two samples."""
    return w["m2"] / (w["n"] - 1) if w["n"] > 1 else 0.0


def welford_cov_new(d: int) -> dict:
    return {"n": 0, "mean": [0.0] * d, "c": [[0.0] * d for _ in range(d)]}


def welford_cov_push(s: dict, x) -> dict:
    """Multivariate Welford: C += (x - mean_old)(x - mean_new)ᵀ."""
    n = s["n"] + 1
    delta = [xi - mi for xi, mi in zip(x, s["mean"])]
    mean = [mi + di / n for mi, di in zip(s["mean"], delta)]
    after = [xi - mi for xi, mi in zip(x, mean)]
    c = [[cij + delta[i] * after[j] for j, cij in enumerate(row)] for i, row in enumerate(s["c"])]
    return {"n": n, "mean": mean, "c": c}


def covariance(s: dict) -> list:
    """Sample covariance C/(n-1); zeros below two samples."""
    return [[cij / (s["n"] - 1) if s["n"] > 1 else 0.0 for cij in row] for row in s["c"]]


# --- EWMA: s = a·x + (1-a)·s ----------------------------------------------------------------------

def ewma(prev, x: float, a: float = 0.2) -> float:
    return x if prev is None else a * x + (1 - a) * prev


def half_life(a: float) -> float:
    """Samples until a step's weight halves: ln(0.5)/ln(1-a) (a=0.2 → 3.1)."""
    return math.log(0.5) / math.log(1 - a)


Q16 = 65536


def to_q16(x: float) -> int:
    """Round half up (not Python's half-to-even round()) so the TS mirror matches bit for bit."""
    return math.floor(x * Q16 + 0.5)


def ewma_q16(prev, x: float, a_q16: int = 13107) -> int:
    """Fixed-point EWMA: integer state scaled by 2^16; a_q16 = round(0.2·65536)."""
    xq = to_q16(x)
    return xq if prev is None else prev + int((xq - prev) * a_q16 / Q16)


# --- the one nearest-rank percentile --------------------------------------------------------------

def nearest_rank(sorted_values, q: float):
    """s[ceil(q·n) − 1] with the rank clamped to [1, n]; q in (0, 1]. Empty → None.
    Never average per-family percentiles: merge the samples (or histograms) first."""
    if not sorted_values:
        return None
    rank = math.ceil(q * len(sorted_values))
    rank = max(1, min(rank, len(sorted_values)))
    return sorted_values[rank - 1]
```

- [ ] **Step 4: Move the four functions verbatim (cut, do not retype)**

Run from the worktree root:

```bash
~/src/apex-router/.venv/bin/python - <<'PY'
import ast, pathlib
src_path = pathlib.Path("src/apex_router/stats.py")
src = src_path.read_text()
lines = src.splitlines(keepends=True)
defs = {n.name: n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
keep = ["wilson_ci", "benjamini_hochberg", "paired_bootstrap_ci", "paired_bootstrap_pvalue"]
moved = "\n\n".join("".join(lines[defs[k].lineno - 1:defs[k].end_lineno]) for k in keep)
core = pathlib.Path("src/apex_router/core/stats.py")
text = core.read_text()
assert text.count("# @@MOVED@@\n") == 1
core.write_text(text.replace("# @@MOVED@@\n", moved))
PY
```

Then replace the whole of `src/apex_router/stats.py` with:

```python
"""Back-compat shim — the implementations moved to apex_router.core.stats (pce-core).

bradley_terry was removed: it had no caller outside its own tests (spec §16)."""
from apex_router.core.stats import (  # noqa: F401  (re-exported API)
    benjamini_hochberg,
    paired_bootstrap_ci,
    paired_bootstrap_pvalue,
    wilson_ci,
)
```

- [ ] **Step 5: Point the state-bench Wilson at core and drop the Bradley-Terry tests**

In `src/apex_router/ornith/state_bench.py`, replace the body of `wilson_ci` (lines 101-109) so the function reads:

```python
def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% interval for a binomial proportion. n=0 -> (0.0, 1.0) (no information).
    Otherwise pce-core's interval (apex_router.core.stats.wilson_ci)."""
    if n <= 0:
        return (0.0, 1.0)
    from apex_router.core.stats import wilson_ci as _core_wilson
    return _core_wilson(k, n, z)
```

Remove the six Bradley-Terry tests and their section header from `tests/test_amr_stats.py`:

```bash
~/src/apex-router/.venv/bin/python - <<'PY'
import ast, pathlib
p = pathlib.Path("tests/test_amr_stats.py")
src = p.read_text()
lines = src.splitlines(keepends=True)
drop = set()
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name.startswith("test_bt_"):
        drop.update(range(n.lineno - 1, n.end_lineno))
out = [l for i, l in enumerate(lines) if i not in drop]
text = "".join(out)
header = ("# --------------------------------------------------------------------------- #\n"
          "# bradley_terry(pairwise) -> dict[str, float]   pairwise[(winner, loser)] = count\n"
          "# --------------------------------------------------------------------------- #\n")
assert header in text
text = text.replace(header, "")
text = text.replace("- bradley_terry    : transitive strengths from pairwise judge wins (no cycles)\n", "")
p.write_text(text)
PY
grep -c "def test_bt_" tests/test_amr_stats.py   # expect 0
```

- [ ] **Step 6: Run the new tests**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_core_stats.py tests/test_amr_stats.py tests/test_state_codegen.py`
Expected: all pass (13 in `test_core_stats.py`).

- [ ] **Step 7: Full suite and commit**

Run: `~/src/apex-router/.venv/bin/pytest -q`
Expected: `1725 passed, 4 skipped` (1718 − 6 Bradley-Terry + 13 new).

```bash
git add src/apex_router/core src/apex_router/stats.py src/apex_router/ornith/state_bench.py tests/test_core_stats.py tests/test_amr_stats.py
git commit -m "feat(core): pce-core stats — move Wilson/BH/bootstrap, add Welford, EWMA, nearest-rank"
```

---

### Task 3: `pce-core` linalg — move the OLS solve, add Jacobi eigen and principal angles

**Files:**
- Create: `src/apex_router/core/linalg.py`, `tests/test_core_linalg.py`
- Modify: `src/apex_router/proxy_engine/tuner/readout.py` (delete `_solve_normal_equations` and `_polyfit_slope`, import them from core under the same names)

**Interfaces:**
- Consumes: nothing new.
- Produces (`apex_router.core.linalg`): `dot(a, b)`, `solve_normal_equations(X, y, ridge=1e-6) -> list[float]` (moved; the hard-coded `1e-6` cushion becomes `ridge`), `residual_trend(idx, resid) -> float` (moved `_polyfit_slope`), `jacobi_eigen(A, tol=1e-12, max_sweeps=64) -> (values_desc, vectors)` (vectors are rows; first nonzero component positive), `has_gap(values, k, rel=1e-6) -> bool`, `principal_angle(u, w) -> float` (radians, `arccos |u·w|/(|u||w|)`).

- [ ] **Step 1: Write the failing tests**

`tests/test_core_linalg.py`:

```python
"""pce-core linalg against reference results: textbook eigenpairs, A·v = λ·v residuals,
orthonormality, trace, known angles, exact least-squares lines."""
import math

from apex_router.core import linalg

S = 1 / math.sqrt(2)
M4 = [[4.0, 1.0, 2.0, 0.5], [1.0, 3.0, 0.0, 1.0], [2.0, 0.0, 5.0, 1.5], [0.5, 1.0, 1.5, 2.0]]


def _close_vec(a, b, tol=1e-9):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_jacobi_2x2_textbook():
    values, vectors = linalg.jacobi_eigen([[2.0, 1.0], [1.0, 2.0]])
    assert _close_vec(values, [3.0, 1.0])
    assert _close_vec(vectors[0], [S, S]) and _close_vec(vectors[1], [S, -S])


def test_jacobi_diagonal_sorts_descending():
    values, vectors = linalg.jacobi_eigen([[4.0, 0, 0], [0, 1.0, 0], [0, 0, 9.0]])
    assert values == [9.0, 4.0, 1.0]
    assert vectors == [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


def test_jacobi_4x4_eigenpairs_orthonormal_and_trace():
    values, vectors = linalg.jacobi_eigen(M4)
    for lam, v in zip(values, vectors):
        av = [linalg.dot(row, v) for row in M4]
        assert max(abs(a - lam * x) for a, x in zip(av, v)) < 1e-9
    for i, vi in enumerate(vectors):
        for j, vj in enumerate(vectors):
            assert abs(linalg.dot(vi, vj) - (1.0 if i == j else 0.0)) < 1e-9
    assert math.isclose(sum(values), 14.0, rel_tol=1e-12)
    assert values == sorted(values, reverse=True)


def test_jacobi_sign_normalised():
    _, vectors = linalg.jacobi_eigen(M4)
    for v in vectors:
        lead = next(x for x in v if abs(x) > 1e-12)
        assert lead > 0


def test_has_gap():
    assert linalg.has_gap([3.0, 1.0], 1)
    assert not linalg.has_gap([2.0, 2.0], 1)
    assert not linalg.has_gap([3.0, 1.0], 0) and not linalg.has_gap([3.0, 1.0], 2)


def test_principal_angle_known_values():
    assert math.isclose(linalg.principal_angle([1, 0], [0, 1]), math.pi / 2)
    assert math.isclose(linalg.principal_angle([1, 0], [1, 1]), math.pi / 4)
    assert linalg.principal_angle([1, 0], [-2, 0]) == 0.0
    assert linalg.principal_angle([0, 0], [1, 0]) == math.pi / 2


def test_solve_exact_line():
    X = [[1.0, x] for x in (1, 2, 3, 4, 5)]
    y = [2 + 3 * x for x in (1, 2, 3, 4, 5)]
    assert _close_vec(linalg.solve_normal_equations(X, y, ridge=0.0), [2.0, 3.0])
    assert _close_vec(linalg.solve_normal_equations(X, y), [2.0, 3.0], tol=1e-4)


def test_solve_collinear_does_not_raise():
    X = [[1.0, 2.0, 4.0], [1.0, 3.0, 6.0], [1.0, 4.0, 8.0]]
    beta = linalg.solve_normal_equations(X, [1.0, 2.0, 3.0])
    assert len(beta) == 3 and all(math.isfinite(b) for b in beta)


def test_residual_trend():
    assert math.isclose(linalg.residual_trend([0, 1, 2, 3], [0, 1, 2, 3]), 1.0)
    assert linalg.residual_trend([0, 1, 2], [5, 5, 5]) == 0.0
    assert linalg.residual_trend([0], [1]) == 0.0


def test_readout_uses_the_moved_helpers():
    from apex_router.proxy_engine.tuner import readout
    assert readout._solve_normal_equations is linalg.solve_normal_equations
    assert readout._polyfit_slope is linalg.residual_trend
```

- [ ] **Step 2: Run to verify failure**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_core_linalg.py`
Expected: `ImportError: cannot import name 'linalg' from 'apex_router.core'`.

- [ ] **Step 3: Write `linalg.py` (moved solve + trend, new Jacobi/angles)**

`src/apex_router/core/linalg.py`:

```python
"""pce-core linalg — small dense linear algebra, stdlib only (d ≤ 8: no SVD needed).

Moved from proxy_engine/tuner/readout.py: solve_normal_equations (was _solve_normal_equations,
its fixed 1e-6 ridge cushion is now the `ridge` parameter) and residual_trend (was
_polyfit_slope). New: cyclic Jacobi eigen-decomposition, eigengap test, principal angle.
plugin/hooks/core/linalg.ts mirrors this file.
"""
from __future__ import annotations

import math


def dot(a, b) -> float:
    s = 0.0
    for x, y in zip(a, b):
        s += x * y
    return s


def solve_normal_equations(X: list[list[float]], y: list[float], ridge: float = 1e-6) -> list[float]:
    """Solve min‖Xβ − y‖ via the normal equations (XᵀX + ridge·I)β = Xᵀy with Gaussian elimination
    + partial pivoting. A singular/degenerate system falls back on the ridge cushion so the fit never
    throws — the caller reports R² so a poor fit is visible, not hidden."""
    n = len(X)
    p = len(X[0]) if n else 0
    A = [[0.0] * p for _ in range(p)]
    b = [0.0] * p
    for i in range(n):
        xi = X[i]
        yi = y[i]
        for a in range(p):
            b[a] += xi[a] * yi
            xia = xi[a]
            row = A[a]
            for c in range(p):
                row[c] += xia * xi[c]
    for a in range(p):
        A[a][a] += ridge
    for col in range(p):
        piv = max(range(col, p), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-12:
            continue
        if piv != col:
            A[col], A[piv] = A[piv], A[col]
            b[col], b[piv] = b[piv], b[col]
        pivval = A[col][col]
        for r in range(p):
            if r == col:
                continue
            factor = A[r][col] / pivval
            if factor == 0.0:
                continue
            for c in range(col, p):
                A[r][c] -= factor * A[col][c]
            b[r] -= factor * b[col]
    return [b[i] / A[i][i] if abs(A[i][i]) > 1e-12 else 0.0 for i in range(p)]


def residual_trend(idx: list[float], resid: list[float]) -> float:
    """Slope of the least-squares line resid ~ a·idx + b (the residual trend). Closed form."""
    n = len(idx)
    if n < 2:
        return 0.0
    mx = sum(idx) / n
    my = sum(resid) / n
    num = sum((idx[i] - mx) * (resid[i] - my) for i in range(n))
    den = sum((idx[i] - mx) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0


def jacobi_eigen(A, tol: float = 1e-12, max_sweeps: int = 64):
    """Cyclic Jacobi for a symmetric matrix. Stops when the off-diagonal mass is ≤ tol² of the
    Frobenius mass (relative tolerance). Returns (values descending, vectors as rows), each vector
    sign-normalised so its first nonzero component is positive."""
    n = len(A)
    a = [[float(x) for x in row] for row in A]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(max_sweeps):
        off = 0.0
        for p in range(n):
            for q in range(p + 1, n):
                off += a[p][q] * a[p][q]
        total = 0.0
        for i in range(n):
            for j in range(n):
                total += a[i][j] * a[i][j]
        if total == 0.0 or off <= tol * tol * total:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                apq = a[p][q]
                if apq == 0.0:
                    continue
                theta = (a[q][q] - a[p][p]) / (2.0 * apq)
                t = (1.0 if theta >= 0 else -1.0) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for k in range(n):
                    akp, akq = a[k][p], a[k][q]
                    a[k][p] = c * akp - s * akq
                    a[k][q] = s * akp + c * akq
                for k in range(n):
                    apk, aqk = a[p][k], a[q][k]
                    a[p][k] = c * apk - s * aqk
                    a[q][k] = s * apk + c * aqk
                for k in range(n):
                    vkp, vkq = v[k][p], v[k][q]
                    v[k][p] = c * vkp - s * vkq
                    v[k][q] = s * vkp + c * vkq
    order = sorted(range(n), key=lambda i: -a[i][i])
    values = [a[i][i] for i in order]
    vectors = []
    for i in order:
        col = [v[k][i] for k in range(n)]
        lead = next((x for x in col if abs(x) > 1e-12), 0.0)
        if lead < 0:
            col = [-x for x in col]
        vectors.append(col)
    return values, vectors


def has_gap(values, k: int, rel: float = 1e-6) -> bool:
    """True when λ_k − λ_{k+1} (1-based k) exceeds rel·|λ_1|: the top-k subspace is well defined."""
    if k <= 0 or k >= len(values):
        return False
    scale = max(abs(values[0]), 5e-324)
    return values[k - 1] - values[k] > rel * scale


def principal_angle(u, w) -> float:
    """θ = arccos(|u·w| / (|u||w|)) in radians; a zero vector is orthogonal to everything."""
    nu = math.sqrt(dot(u, u))
    nw = math.sqrt(dot(w, w))
    if nu == 0.0 or nw == 0.0:
        return math.pi / 2
    c = min(1.0, abs(dot(u, w)) / (nu * nw))
    return math.acos(c)
```

- [ ] **Step 4: Make `readout.py` import the moved helpers**

```bash
~/src/apex-router/.venv/bin/python - <<'PY'
import ast, pathlib
p = pathlib.Path("src/apex_router/proxy_engine/tuner/readout.py")
src = p.read_text()
lines = src.splitlines(keepends=True)
drop = set()
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in ("_solve_normal_equations", "_polyfit_slope"):
        drop.update(range(n.lineno - 1, n.end_lineno))
text = "".join(l for i, l in enumerate(lines) if i not in drop)
anchor = "from dataclasses import dataclass, field\n"
assert text.count(anchor) == 1
text = text.replace(anchor, anchor + "\nfrom apex_router.core.linalg import residual_trend as _polyfit_slope  # noqa: E402\n"
                    "from apex_router.core.linalg import solve_normal_equations as _solve_normal_equations  # noqa: E402\n")
p.write_text(text)
PY
grep -n "_solve_normal_equations\|_polyfit_slope" src/apex_router/proxy_engine/tuner/readout.py
```

Expected grep output: the two import lines plus the two call sites (`beta = _solve_normal_equations(X, ys)`, `trend = _polyfit_slope(idx, resid)`), no `def`.

- [ ] **Step 5: Run the tests**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_core_linalg.py tests/proxy_engine`
Expected: all pass (10 in `test_core_linalg.py`; readout's own tests unchanged).

- [ ] **Step 6: Full suite and commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1735 passed, 4 skipped`.

```bash
git add src/apex_router/core/linalg.py src/apex_router/proxy_engine/tuner/readout.py tests/test_core_linalg.py
git commit -m "feat(core): pce-core linalg — move OLS solve and residual trend, add Jacobi eigen, principal angle"
```

---

### Task 4: `pce-core` anomaly and drift — Q, T² with χ² limit, explain card, CUSUM, penalty machine

**Files:**
- Create: `src/apex_router/core/anomaly.py`, `src/apex_router/core/drift.py`, `tests/test_core_anomaly_drift.py`

**Interfaces:**
- Consumes: `apex_router.core.linalg.dot`, `jacobi_eigen`, `principal_angle`.
- Produces:
  - `apex_router.core.anomaly`: `CHI2_99: dict[int, float]` (k = 1..5), `WARMUP_N = 50`, `HIST_MAX = 200`, `max_abs_scales(rows) -> list[float]`, `prescale(x, scales)`, `zscore(x, mean, sd)`, `median(xs)`, `mad(xs)`, `robust_z(x, med, mad_value)`, `correlation_of(cov) -> (corr, sd)`, `pca_fit(matrix, k) -> (values, vectors)`, `q_residual(xc, V) -> float`, `t_squared(xc, V, values) -> float`, `t2_limit(k) -> float | None`, `ecdf(value, history) -> float`, `explain(q, t2, q_hist, t2_hist) -> {"score","term","q","t2"}`.
  - `apex_router.core.drift`: `variance_l1(l0, l1) -> float`, `cusum_lower(s, z, kappa=0.5, h=4.0) -> (s_new, alarm)`, `penalty_ramp(T, t_lo=2.0, t_hi=6.0, p_max=10) -> int`, `penalty_run(scores, enter_streak=2, exit_streak=3, warm_after=3) -> list[(T, state, penalty)]`, `subspace_angle` (= `linalg.principal_angle`).

Reference formulas (spec §7, §16): `Q = ‖x_c − V_k V_kᵀ x_c‖²`; `T² = Σ z_i²/λ_i`; T² limit = χ²_k(0.99) (k=2: −2·ln 0.01 = 9.2103); modified z = `0.6745·|x−med|/MAD`, outlier when > 3.5; Page CUSUM on standardized residuals with κ = 0.5, h = 4; penalty ramp `int(p_max·clamp((T−t_lo)/(t_hi−t_lo), 0, 1))` with enter/exit streaks 2/3 after a 3-sample warm-up.

- [ ] **Step 1: Write the failing tests**

`tests/test_core_anomaly_drift.py`:

```python
"""pce-core anomaly + drift against hand-derived reference values."""
import math

from apex_router.core import anomaly, drift


def test_chi2_limit_k2_is_minus_two_ln_alpha():
    assert math.isclose(anomaly.CHI2_99[2], -2 * math.log(0.01), rel_tol=1e-12)
    assert anomaly.t2_limit(2) == anomaly.CHI2_99[2]
    assert anomaly.t2_limit(9) is None


def test_max_abs_scales_and_zscore():
    assert anomaly.max_abs_scales([[1, -4, 0], [2, 2, 0]]) == [2, 4, 1]
    assert anomaly.prescale([1, -4, 0], [2, 4, 1]) == [0.5, -1.0, 0.0]
    assert anomaly.zscore([3.0, 5.0], [1.0, 5.0], [2.0, 0.0]) == [1.0, 0.0]


def test_median_mad_robust_z():
    xs = [1, 2, 3, 4, 100]
    assert anomaly.median(xs) == 3
    assert anomaly.median([1, 2, 3, 4]) == 2.5
    assert anomaly.mad(xs) == 1
    assert anomaly.robust_z(100, 3, 1) > 3.5
    assert math.isclose(anomaly.robust_z(4, 3, 1), 0.6745)
    assert anomaly.robust_z(9, 3, 0) == 0.0


def test_q_and_t2_handmade():
    V = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert math.isclose(anomaly.q_residual([2.0, 1.0, 3.0], V), 9.0)
    assert math.isclose(anomaly.t_squared([2.0, 1.0, 3.0], V, [4.0, 1.0]), 2.0)
    assert math.isclose(anomaly.t_squared([2.0, 1.0, 3.0], V, [4.0, 0.0]), 1.0)


def test_correlation_and_pca_fit():
    corr, sd = anomaly.correlation_of([[4.0, 2.0], [2.0, 9.0]])
    assert sd == [2.0, 3.0]
    assert math.isclose(corr[0][1], 1 / 3) and corr[0][0] == 1.0
    values, vectors = anomaly.pca_fit(corr, 1)
    assert len(values) == 1 and len(vectors) == 1
    assert math.isclose(values[0], 4 / 3)


def test_ecdf_and_explain():
    assert anomaly.ecdf(2.5, [1, 2, 3, 4]) == 0.5
    assert anomaly.ecdf(1.0, []) == 0.0
    card = anomaly.explain(10.0, 1.0, [1, 2, 3, 4, 5, 6, 7, 8, 9, 11], [1.0, 2.0])
    assert card == {"score": 0.9, "term": "Q", "q": 0.9, "t2": 0.5}
    card = anomaly.explain(0.0, 3.0, [1.0], [1.0, 2.0])
    assert card["term"] == "T2" and card["score"] == 1.0


def test_variance_l1():
    assert math.isclose(drift.variance_l1([3.0, 1.0], [2.0, 2.0]), 0.5)
    assert drift.variance_l1([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cusum_lower_trace():
    s, trace = 0.0, []
    for z in [-1.5] * 5:
        s, alarm = drift.cusum_lower(s, z)
        trace.append((s, alarm))
    assert trace == [(1.0, False), (2.0, False), (3.0, False), (4.0, False), (5.0, True)]
    assert drift.cusum_lower(2.0, 0.0) == (1.5, False)
    assert drift.cusum_lower(0.2, 1.0) == (0.0, False)


def test_penalty_ramp():
    assert [drift.penalty_ramp(t) for t in (1, 2, 4, 5, 6, 9)] == [0, 0, 5, 7, 10, 10]


def test_penalty_run_trace():
    trace = drift.penalty_run([1, 9, 1, 1.5, 3, 5, 7, 1, 1, 1, 1])
    assert trace == [
        (1, "COLD", 0), (9, "COLD", 0), (1, "WARM", 0), (1.5, "WARM", 0), (3, "WARM", 0),
        (5, "DRIFTING", 7), (7, "DRIFTING", 10), (1, "DRIFTING", 0), (1, "DRIFTING", 0),
        (1, "WARM", 0), (1, "WARM", 0),
    ]


def test_subspace_angle_is_the_principal_angle():
    assert math.isclose(drift.subspace_angle([1, 0], [1, 1]), math.pi / 4)
```

- [ ] **Step 2: Run to verify failure**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_core_anomaly_drift.py`
Expected: `ImportError: cannot import name 'anomaly' from 'apex_router.core'`.

- [ ] **Step 3: Write `anomaly.py`**

`src/apex_router/core/anomaly.py`:

```python
"""pce-core anomaly — session anomaly score (spec §7): standardize with stored (μ, σ), PCA k=2 from
stored (V_k, Λ_k), Q (residual) and T² (in-model), each normalized by its stored empirical CDF
before combining; the explain card names the term. Parameters only, never rows (A9).
plugin/hooks/core/anomaly.ts mirrors this file."""
from __future__ import annotations

import math

from apex_router.core.linalg import dot, jacobi_eigen

# χ²_k(0.99) quantiles: the T² control limit for k retained components.
CHI2_99 = {1: 6.634896601021214, 2: 9.210340371976184, 3: 11.344866730144373,
           4: 13.276704135987622, 5: 15.08627246938899}
WARMUP_N = 50     # PCA disabled until n ≥ 50 turns (rank(Σ) ≤ min(d, n−1))
HIST_MAX = 200    # stored score history for the empirical CDF


def max_abs_scales(rows) -> list:
    d = len(rows[0]) if rows else 0
    scales = []
    for j in range(d):
        m = 0.0
        for row in rows:
            m = max(m, abs(row[j]))
        scales.append(m if m > 0 else 1)
    return scales


def prescale(x, scales) -> list:
    return [xi / s for xi, s in zip(x, scales)]


def zscore(x, mean, sd) -> list:
    return [(xi - m) / (s if s > 0 else 1.0) for xi, m, s in zip(x, mean, sd)]


def median(xs) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def mad(xs) -> float:
    med = median(xs)
    return median([abs(x - med) for x in xs])


def robust_z(x: float, med: float, mad_value: float) -> float:
    """Modified z = 0.6745·(x − med)/MAD; an outlier when |z| > 3.5 (descriptive, benches only)."""
    return 0.6745 * (x - med) / mad_value if mad_value > 0 else 0.0


def correlation_of(cov):
    sd = [math.sqrt(cov[i][i]) if cov[i][i] > 0 else 0.0 for i in range(len(cov))]
    corr = [[cov[i][j] / ((sd[i] or 1.0) * (sd[j] or 1.0)) for j in range(len(cov))] for i in range(len(cov))]
    return corr, sd


def pca_fit(matrix, k: int):
    values, vectors = jacobi_eigen(matrix)
    return values[:k], vectors[:k]


def q_residual(xc, V) -> float:
    proj = [dot(v, xc) for v in V]
    total = 0.0
    for j in range(len(xc)):
        recon = 0.0
        for i, v in enumerate(V):
            recon += proj[i] * v[j]
        total += (xc[j] - recon) ** 2
    return total


def t_squared(xc, V, values) -> float:
    t = 0.0
    for v, lam in zip(V, values):
        if lam > 0:
            s = dot(v, xc)
            t += s * s / lam
    return t


def t2_limit(k: int):
    return CHI2_99.get(k)


def ecdf(value: float, history) -> float:
    if not history:
        return 0.0
    count = 0
    for h in history:
        if h <= value:
            count += 1
    return count / len(history)


def explain(q: float, t2: float, q_hist, t2_hist) -> dict:
    rq = ecdf(q, q_hist)
    rt = ecdf(t2, t2_hist)
    if rq >= rt:
        return {"score": rq, "term": "Q", "q": rq, "t2": rt}
    return {"score": rt, "term": "T2", "q": rq, "t2": rt}
```

- [ ] **Step 4: Write `drift.py`**

`src/apex_router/core/drift.py`:

```python
"""pce-core drift — regime change, not anomalies (A7): principal-angle and variance-share drift,
Page CUSUM on standardized residuals (κ=0.5, h=4), and the penalty state machine (ramp +
enter/exit streaks). plugin/hooks/core/drift.ts mirrors this file."""
from __future__ import annotations

from apex_router.core.linalg import principal_angle as subspace_angle  # noqa: F401  (re-exported)


def variance_l1(l0, l1) -> float:
    """L1 distance between the two spectra's variance shares."""
    s0 = 0.0
    for x in l0:
        s0 += x
    s1 = 0.0
    for x in l1:
        s1 += x
    if s0 <= 0 or s1 <= 0:
        return 0.0
    total = 0.0
    for a, b in zip(l0, l1):
        total += abs(a / s0 - b / s1)
    return total


def cusum_lower(s: float, z: float, kappa: float = 0.5, h: float = 4.0):
    """One-sided (downward) Page CUSUM: S ← max(0, S − z − κ); alarm when S > h."""
    nxt = max(0.0, s - z - kappa)
    return nxt, nxt > h


def penalty_ramp(T: float, t_lo: float = 2.0, t_hi: float = 6.0, p_max: int = 10) -> int:
    return int(p_max * min(1.0, max(0.0, (T - t_lo) / (t_hi - t_lo))))


def penalty_run(scores, enter_streak: int = 2, exit_streak: int = 3, warm_after: int = 3):
    """COLD until warm_after samples; WARM→DRIFTING after enter_streak scores ≥ 2.0;
    DRIFTING→WARM after exit_streak scores < 2.0. Penalty is the ramp while DRIFTING, else 0."""
    state, streak, seen, out = "COLD", 0, 0, []
    for T in scores:
        seen += 1
        if state == "COLD" and seen >= warm_after:
            state = "WARM"
        elif state == "WARM":
            streak = streak + 1 if T >= 2.0 else 0
            if streak >= enter_streak:
                state, streak = "DRIFTING", 0
        elif state == "DRIFTING":
            streak = streak + 1 if T < 2.0 else 0
            if streak >= exit_streak:
                state, streak = "WARM", 0
        pen = penalty_ramp(T) if state == "DRIFTING" else 0
        out.append((T, state, pen))
    return out
```

- [ ] **Step 5: Run the tests**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_core_anomaly_drift.py`
Expected: `11 passed`.

- [ ] **Step 6: Full suite and commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1746 passed, 4 skipped`.

```bash
git add src/apex_router/core/anomaly.py src/apex_router/core/drift.py tests/test_core_anomaly_drift.py
git commit -m "feat(core): pce-core anomaly (Q, T², χ² limit, explain) and drift (CUSUM, penalty machine)"
```

---

### Task 5: `pce-core` routing and cost — the cell state machine, reused gate/break-even, OLS cost, budget burn

**Files:**
- Create: `src/apex_router/core/routing.py`, `src/apex_router/core/cost.py`, `tests/test_core_routing_cost.py`

**Interfaces:**
- Consumes: `core.stats.wilson_ci`, `core.drift.cusum_lower`, `core.linalg.solve_normal_equations`, `residual_trend`; existing `apex_router.gate.run_gate`, `apex_router.route_advise._break_even` (re-exported, not rewritten).
- Produces:
  - `apex_router.core.routing`: `MIN_N = 30`, `TARGET = 0.9`, `WINDOW = 10`, `ENTER = 2`, `EXIT = 3`, `REBASE = 3`, `P0_CAP = 0.98`, `cell_new() -> dict`, `cell_observe(cell, passed, min_n=MIN_N, target=TARGET, window=WINDOW) -> dict`, `run_gate`, `break_even`. Cell dict keys: `n, pass, state, win_n, win_pass, below, above, cusum, p0`.
  - `apex_router.core.cost`: `fit_cost(xs, ys) -> {"a","b","r2","trend","n"} | None`, `budget_burn(spent_usd, minutes, budget_usd) -> {"burn","minutes_to_exhaust"} | None`.

The §5 cell state machine, exactly (window = 10 labels; the TS mirror in Task 8 copies it):

```
every label: n += 1, pass += x, win_n += 1, win_pass += x
when win_n reaches WINDOW (window closes):
  rate = win_pass / win_n ; below = rate < target
  if state ∈ {READY, DRIFTING} and p0 known:
      p0c = min(p0, 0.98) ; se = sqrt(p0c(1−p0c)/win_n)
      cusum, alarm = cusum_lower(cusum, (rate − p0c)/se)          # κ=0.5, h=4
  READY:    below → below += 1 else below = 0 ; below ≥ 2 or alarm → DRIFTING (advise only)
  DRIFTING: below → below += 1, above = 0 ; else above += 1, below = 0
            above ≥ 3 → READY (cusum = 0)                          # exit streak (p08: exit 3)
            below ≥ 3 → rebaseline: the cell starts over (COLD)    # stable new regime (A7)
  win_n = win_pass = 0
if state ∈ {COLD, WARMING}:
  n ≥ min_n and Wilson-lo(pass, n) ≥ target → READY, p0 = pass/n, cusum = 0
  else → COLD if n = 0 else WARMING
```

One failing label inside an otherwise good window is an anomaly and moves nothing; the window-level CUSUM catches a sustained small regression the streak rule misses (e.g. 9/10 windows when p0 = 1.0).

- [ ] **Step 1: Write the failing tests**

`tests/test_core_routing_cost.py`:

```python
"""Cell state machine at its boundaries (n = 29/30/31, Wilson, streaks, CUSUM, rebaseline) and the
cost/budget formulas against hand-computed values."""
import math

from apex_router.core import cost, routing


def run(seq, cell=None, **kw):
    c = cell if cell is not None else routing.cell_new()
    states = []
    for x in seq:
        c = routing.cell_observe(c, bool(x), **kw)
        states.append(c["state"])
    return c, states


def ready_cell():
    c, states = run([1] * 40)
    assert states[34] == "READY" and c["p0"] == 1.0
    return c


def test_new_cell_is_cold():
    assert routing.cell_new()["state"] == "COLD"


def test_all_pass_needs_35_at_target_0_9():
    _, states = run([1] * 35)
    assert set(states[:34]) == {"WARMING"}          # 30/30 → Wilson-lo 0.886 < 0.9
    assert states[34] == "READY"                     # 35/(35+1.96²) = 0.9011


def test_floor_29_30_31_at_target_0_85():
    _, states = run([1] * 31, target=0.85)
    assert states[28] == "WARMING" and states[29] == "READY" and states[30] == "READY"


def test_one_failure_is_an_anomaly_not_a_regime():
    c, states = run([1] * 9 + [0], cell=ready_cell())
    assert states[-1] == "READY"


def test_two_bad_windows_demote_then_three_good_restore():
    c, states = run(([1] * 8 + [0] * 2) * 2, cell=ready_cell())
    assert states[-1] == "DRIFTING"
    c, states = run([1] * 30, cell=c)
    assert states[-1] == "READY" and c["cusum"] == 0.0


def test_cusum_demotes_a_small_sustained_regression():
    c, states = run(([1] * 9 + [0]) * 3, cell=ready_cell())
    assert states[-1] == "READY"
    c, states = run([1] * 9 + [0], cell=c)
    assert states[-1] == "DRIFTING" and c["below"] == 0


def test_stable_new_regime_rebaselines():
    c, _ = run([1] * 5 + [0] * 5, cell=ready_cell())
    assert c["state"] == "DRIFTING"
    c, states = run(([1] * 5 + [0] * 5) * 3, cell=c)
    assert states[-1] == "COLD" and c["n"] == 0 and c["p0"] is None


def test_reexports_reuse_the_existing_gate():
    from apex_router import gate
    assert routing.run_gate is gate.run_gate
    assert math.isclose(routing.break_even(5.0), 0.8)


def test_fit_cost_exact_line():
    xs = [1000, 2000, 3000, 4000, 5000, 6000]
    ys = [0.02 + 0.00003 * x for x in xs]
    fit = cost.fit_cost(xs, ys)
    assert abs(fit["a"] - 0.02) < 1e-6 and abs(fit["b"] - 0.00003) < 1e-9
    assert fit["r2"] > 0.999999 and abs(fit["trend"]) < 1e-7 and fit["n"] == 6  # ridge 1e-6 bias
    assert cost.fit_cost([1, 2], [1, 2]) is None


def test_budget_burn():
    b = cost.budget_burn(5.0, 360.0, 10.0)
    assert math.isclose(b["burn"], 2.0) and math.isclose(b["minutes_to_exhaust"], 360.0)
    assert cost.budget_burn(5.0, 360.0, 0.0) is None
    assert cost.budget_burn(0.0, 10.0, 10.0) == {"burn": 0.0, "minutes_to_exhaust": None}
```

- [ ] **Step 2: Run to verify failure**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_core_routing_cost.py`
Expected: `ImportError: cannot import name 'cost' from 'apex_router.core'`.

- [ ] **Step 3: Write `routing.py` and `cost.py`**

`src/apex_router/core/routing.py`:

```python
"""pce-core routing — the evidence cell state machine (spec §5) plus the existing promotion gate
and break-even, re-exported (reuse, not rewrite). ε-greedy exploration is deliberately absent:
it would act without labels (advise-only until cells are READY).
plugin/hooks/core/routing.ts mirrors cell_new/cell_observe."""
from __future__ import annotations

import math

from apex_router.core.drift import cusum_lower
from apex_router.core.stats import wilson_ci
from apex_router.gate import run_gate  # noqa: F401  (re-exported)
from apex_router.route_advise import _break_even as break_even  # noqa: F401  (re-exported)

MIN_N = 30      # sample floor (same as route-advise)
TARGET = 0.9    # promotion target — a prior, revisit after 30 days of labels (§15)
WINDOW = 10     # labels per evaluation window
ENTER = 2       # READY → DRIFTING after 2 consecutive windows below target
EXIT = 3        # DRIFTING → READY after 3 consecutive windows at/above target
REBASE = 3      # DRIFTING + 3 more windows below → stable new regime: start over
P0_CAP = 0.98   # keeps the CUSUM's standard error away from 0 when the baseline is 100%


def cell_new() -> dict:
    return {"n": 0, "pass": 0, "state": "COLD", "win_n": 0, "win_pass": 0,
            "below": 0, "above": 0, "cusum": 0.0, "p0": None}


def cell_observe(cell: dict, passed: bool, min_n: int = MIN_N, target: float = TARGET,
                 window: int = WINDOW) -> dict:
    x = 1 if passed else 0
    c = dict(cell)
    c["n"] += 1
    c["pass"] += x
    c["win_n"] += 1
    c["win_pass"] += x
    alarm = False
    if c["win_n"] >= window:
        rate = c["win_pass"] / c["win_n"]
        below = rate < target
        if c["state"] in ("READY", "DRIFTING") and c["p0"] is not None:
            p0 = min(c["p0"], P0_CAP)
            se = math.sqrt(p0 * (1 - p0) / c["win_n"])
            c["cusum"], alarm = cusum_lower(c["cusum"], (rate - p0) / se)
        if c["state"] == "READY":
            c["below"] = c["below"] + 1 if below else 0
            if c["below"] >= ENTER or alarm:
                c["state"], c["below"], c["above"] = "DRIFTING", 0, 0
        elif c["state"] == "DRIFTING":
            if below:
                c["below"] += 1
                c["above"] = 0
            else:
                c["above"] += 1
                c["below"] = 0
            if c["above"] >= EXIT:
                c["state"], c["above"], c["cusum"] = "READY", 0, 0.0
            elif c["below"] >= REBASE:
                return cell_new()
        c["win_n"] = 0
        c["win_pass"] = 0
    if c["state"] in ("COLD", "WARMING"):
        if c["n"] >= min_n and wilson_ci(c["pass"], c["n"])[0] >= target:
            c["state"], c["p0"], c["cusum"], c["below"], c["above"] = "READY", c["pass"] / c["n"], 0.0, 0, 0
        else:
            c["state"] = "COLD" if c["n"] == 0 else "WARMING"
    return c
```

`src/apex_router/core/cost.py`:

```python
"""pce-core cost — per-cell OLS `cost ≈ a + b·input_tokens` with R² and the residual-trend alarm
(the readout's tokens-on-bytes fit, generalised), and the budget burn of spec §7.
plugin/hooks/core/cost.ts mirrors this file."""
from __future__ import annotations

from apex_router.core.linalg import residual_trend, solve_normal_equations


def fit_cost(xs, ys):
    n = len(xs)
    if n < 3 or len(ys) != n:
        return None
    X = [[1.0, float(x)] for x in xs]
    a, b = solve_normal_equations(X, [float(y) for y in ys])
    resid = [ys[i] - (a + b * xs[i]) for i in range(n)]
    mean_y = 0.0
    for y in ys:
        mean_y += y
    mean_y /= n
    ss_res = 0.0
    ss_tot = 0.0
    for i in range(n):
        ss_res += resid[i] ** 2
        ss_tot += (ys[i] - mean_y) ** 2
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"a": a, "b": b, "r2": r2, "trend": residual_trend(list(range(n)), resid), "n": n}


def budget_burn(spent_usd: float, minutes: float, budget_usd: float):
    """burn = spend_rate / (budget/1440); minutes_to_exhaust = remaining / spend_rate."""
    if budget_usd <= 0 or minutes <= 0:
        return None
    rate = spent_usd / minutes
    burn = rate / (budget_usd / 1440)
    mte = max(0.0, budget_usd - spent_usd) / rate if rate > 0 else None
    return {"burn": burn, "minutes_to_exhaust": mte}
```

- [ ] **Step 4: Run the tests**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_core_routing_cost.py`
Expected: `10 passed`.

- [ ] **Step 5: Full suite and commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1756 passed, 4 skipped`.

```bash
git add src/apex_router/core/routing.py src/apex_router/core/cost.py tests/test_core_routing_cost.py
git commit -m "feat(core): pce-core routing cell state machine and cost/budget formulas"
```

---

### Task 6: Parity fixtures — Python is the oracle, TypeScript must reproduce it

**Files:**
- Create: `src/apex_router/core/fixtures.py`, `tests/test_core_fixtures.py`
- Create (generated, committed): `plugin/tests/fixtures/core.ts`, `plugin/tests/fixtures/classify.ts`

**Interfaces:**
- Consumes: every `apex_router.core.*` function from Tasks 2–5; `apex_router.route_log.classify_dispatch`.
- Produces: `build_core() -> dict`, `build_classify() -> list`, `render_ts(name, obj) -> str`, `main(argv) -> int` (`--write` writes both files, `--check` exits 1 when stale). The TS side imports `CORE` and `CLASSIFY`. Shape of `CORE` (keys used by Tasks 7–9): `welford{xs,n,mean,m2,var}`, `welford_cov{rows,n,mean,c,cov}`, `ewma{a,xs,out,q16}`, `wilson[[k,n,lo,hi]]`, `nearest_rank{values,cases[[q,v]]}`, `jacobi[{A,values,vectors}]`, `principal_angle[[u,w,angle]]`, `has_gap[[values,k,bool]]`, `solve{X,y,ridge,beta}`, `residual_trend{idx,resid,slope}`, `anomaly{V,values,cases[[xc,q,t2]],cov,corr,sd,pca{values,vectors},scales{rows,scales},mad{xs,median,mad,robust[[x,z]]},explain[[q,t2,qHist,t2Hist,card]]}`, `drift{cusum{zs,trace[[s,alarm]]},penalty{scores,trace[[T,state,pen]]},variance_l1[[l0,l1,v]]}`, `cells{<name>{min_n,target,seq,states,final}}` (final uses the TS field names `n, pass, state, winN, winPass, below, above, cusum, p0`), `cost{xs,ys,fit,budget[[spent,minutes,budget,result]]}` (result `{burn, minutesToExhaust}` or null). `CLASSIFY`: `[[subagentType, description, promptHead, expected]]`.

- [ ] **Step 1: Write the failing freshness test**

`tests/test_core_fixtures.py`:

```python
"""The committed TS parity fixtures are exactly what the Python oracle generates now."""
from apex_router.core import fixtures


def test_core_fixture_is_current():
    expected = fixtures.render_ts("CORE", fixtures.build_core())
    actual = (fixtures.FIXTURE_DIR / "core.ts").read_text()
    assert actual == expected, "stale: run `python -m apex_router.core.fixtures --write`"


def test_classify_fixture_is_current():
    expected = fixtures.render_ts("CLASSIFY", fixtures.build_classify())
    actual = (fixtures.FIXTURE_DIR / "classify.ts").read_text()
    assert actual == expected, "stale: run `python -m apex_router.core.fixtures --write`"
```

- [ ] **Step 2: Run to verify failure**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_core_fixtures.py`
Expected: `ImportError: cannot import name 'fixtures'`.

- [ ] **Step 3: Write the generator**

`src/apex_router/core/fixtures.py`:

```python
"""Parity fixtures: pce-core (Python) is the oracle; plugin/hooks/core/*.ts and
plugin/hooks/classify.ts must reproduce these values (tests/core_*.test.ts, classify.test.ts).

    python -m apex_router.core.fixtures --write    # regenerate plugin/tests/fixtures/{core,classify}.ts
    python -m apex_router.core.fixtures --check    # exit 1 when the committed files are stale
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from apex_router.core import anomaly, cost, drift, linalg, routing, stats
from apex_router.route_log import classify_dispatch

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = ROOT / "plugin" / "tests" / "fixtures"

XS = [10, 11, 9, 10, 50, 52, 49, 51, 10, 10]
M4 = [[4.0, 1.0, 2.0, 0.5], [1.0, 3.0, 0.0, 1.0], [2.0, 0.0, 5.0, 1.5], [0.5, 1.0, 1.5, 2.0]]


def _camel_cell(c: dict) -> dict:
    return {"n": c["n"], "pass": c["pass"], "state": c["state"], "winN": c["win_n"],
            "winPass": c["win_pass"], "below": c["below"], "above": c["above"],
            "cusum": c["cusum"], "p0": c["p0"]}


def _cells() -> dict:
    ready = [1] * 40
    scenarios = {
        "floor_29": ([1] * 29, 30, 0.9),
        "all_pass_35": ([1] * 36, 30, 0.9),
        "floor_target_085": ([1] * 31, 30, 0.85),
        "anomaly_not_regime": (ready + [1] * 9 + [0], 30, 0.9),
        "streak_then_restore": (ready + ([1] * 8 + [0] * 2) * 2 + [1] * 30, 30, 0.9),
        "cusum_small_regression": (ready + ([1] * 9 + [0]) * 4, 30, 0.9),
        "rebaseline": (ready + ([1] * 5 + [0] * 5) * 4 + [1] * 3, 30, 0.9),
    }
    out = {}
    for name, (seq, min_n, target) in scenarios.items():
        c = routing.cell_new()
        states = []
        for x in seq:
            c = routing.cell_observe(c, bool(x), min_n=min_n, target=target)
            states.append(c["state"])
        out[name] = {"min_n": min_n, "target": target, "seq": seq, "states": states, "final": _camel_cell(c)}
    return out


def build_core() -> dict:
    w = stats.welford_new()
    for x in XS:
        w = stats.welford_push(w, x)
    cov_rows = [[1.0, 2.0], [2.0, 4.0], [3.0, 7.0], [4.0, 4.0], [5.0, 1.0]]
    cs = stats.welford_cov_new(2)
    for r in cov_rows:
        cs = stats.welford_cov_push(cs, r)
    ew, q16, s, sq = [], [], None, None
    for x in XS:
        s = stats.ewma(s, x, 0.2)
        sq = stats.ewma_q16(sq, x)
        ew.append(s)
        q16.append(sq)
    rank_values = [15, 20, 35, 40, 50]
    jac = []
    for A in ([[2.0, 1.0], [1.0, 2.0]], [[4.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 9.0]], M4):
        values, vectors = linalg.jacobi_eigen(A)
        jac.append({"A": A, "values": values, "vectors": vectors})
    X = [[1.0, float(x)] for x in (1, 2, 3, 4, 5, 6)]
    y = [2.1, 4.9, 8.2, 10.9, 14.1, 17.0]
    V = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    vals = [4.0, 1.0]
    cov3 = [[4.0, 2.0, 0.5], [2.0, 9.0, 1.0], [0.5, 1.0, 1.0]]
    corr, sd = anomaly.correlation_of(cov3)
    pv, pvec = anomaly.pca_fit(corr, 2)
    mad_xs = [1.0, 2.0, 3.0, 4.0, 100.0]
    med, madv = anomaly.median(mad_xs), anomaly.mad(mad_xs)
    explain_cases = [
        [10.0, 1.0, [1, 2, 3, 4, 5, 6, 7, 8, 9, 11], [1.0, 2.0]],
        [0.0, 3.0, [1.0], [1.0, 2.0]],
        [5.0, 5.0, [], []],
    ]
    zs = [-1.5] * 5 + [0.0] * 3 + [-3.0, 2.0]
    trace, sc = [], 0.0
    for z in zs:
        sc, alarm = drift.cusum_lower(sc, z)
        trace.append([sc, alarm])
    scores = [1, 9, 1, 1.5, 3, 5, 7, 1, 1, 1, 1]
    cost_xs = [1000, 2000, 3000, 4000, 5000, 6000]
    cost_ys = [0.051, 0.079, 0.112, 0.139, 0.171, 0.198]
    budget_cases = [[5.0, 360.0, 10.0], [0.0, 10.0, 10.0], [12.0, 60.0, 10.0], [1.0, 0.0, 10.0]]
    budgets = []
    for spent, minutes, budget in budget_cases:
        r = cost.budget_burn(spent, minutes, budget)
        budgets.append([spent, minutes, budget, None if r is None else {"burn": r["burn"], "minutesToExhaust": r["minutes_to_exhaust"]}])
    return {
        "welford": {"xs": XS, "n": w["n"], "mean": w["mean"], "m2": w["m2"], "var": stats.welford_variance(w)},
        "welford_cov": {"rows": cov_rows, "n": cs["n"], "mean": cs["mean"], "c": cs["c"], "cov": stats.covariance(cs)},
        "ewma": {"a": 0.2, "xs": XS, "out": ew, "q16": q16},
        "wilson": [[k, n, *stats.wilson_ci(k, n)] for k, n in
                   [(0, 1), (1, 1), (5, 10), (27, 30), (30, 30), (34, 34), (35, 35), (99, 100)]],
        "nearest_rank": {"values": rank_values,
                         "cases": [[q, stats.nearest_rank(rank_values, q)] for q in (0.05, 0.3, 0.4, 0.5, 0.95, 1.0)]},
        "jacobi": jac,
        "principal_angle": [[u, v, linalg.principal_angle(u, v)] for u, v in
                            ([[1.0, 0.0], [0.0, 1.0]], [[1.0, 0.0], [1.0, 1.0]], [[1.0, 2.0, 3.0], [1.0, 2.0, 3.1]], [[0.0, 0.0], [1.0, 0.0]])],
        "has_gap": [[v, k, linalg.has_gap(v, k)] for v, k in ([[3.0, 1.0], 1], [[2.0, 2.0], 1], [[3.0, 1.0], 0], [[5.0, 4.0, 1.0], 2])],
        "solve": {"X": X, "y": y, "ridge": 1e-6, "beta": linalg.solve_normal_equations(X, y, 1e-6)},
        "residual_trend": {"idx": [0, 1, 2, 3, 4], "resid": [0.1, -0.2, 0.05, 0.3, -0.1],
                           "slope": linalg.residual_trend([0, 1, 2, 3, 4], [0.1, -0.2, 0.05, 0.3, -0.1])},
        "anomaly": {
            "V": V, "values": vals,
            "cases": [[xc, anomaly.q_residual(xc, V), anomaly.t_squared(xc, V, vals)]
                      for xc in ([2.0, 1.0, 3.0], [0.5, -1.0, 0.2], [0.0, 0.0, 0.0])],
            "cov": cov3, "corr": corr, "sd": sd, "pca": {"values": pv, "vectors": pvec},
            "scales": {"rows": [[1.0, -4.0, 0.0], [2.0, 2.0, 0.0]], "scales": anomaly.max_abs_scales([[1.0, -4.0, 0.0], [2.0, 2.0, 0.0]])},
            "mad": {"xs": mad_xs, "median": med, "mad": madv,
                    "robust": [[x, anomaly.robust_z(x, med, madv)] for x in mad_xs]},
            "explain": [[q, t2, qh, th, anomaly.explain(q, t2, qh, th)] for q, t2, qh, th in explain_cases],
        },
        "drift": {
            "cusum": {"zs": zs, "trace": trace},
            "penalty": {"scores": scores, "trace": [list(t) for t in drift.penalty_run(scores)]},
            "variance_l1": [[a, b, drift.variance_l1(a, b)] for a, b in ([[3.0, 1.0], [2.0, 2.0]], [[5.0, 3.0, 2.0], [4.0, 4.0, 2.0]], [[0.0], [1.0]])],
        },
        "cells": _cells(),
        "cost": {"xs": cost_xs, "ys": cost_ys, "fit": cost.fit_cost(cost_xs, cost_ys), "budget": budgets},
    }


CLASSIFY_CASES = [
    ("Explore", "anything at all", None),
    (None, "Review the diff for bugs", None),
    ("general-purpose", "Implement backend Task 3: pressure", None),
    ("general-purpose", "Why does the test fail", None),
    (None, "Rename the helpers", None),
    (None, "Plan the migration", None),
    (None, "audit the auth flow", None),
    (None, "verify claims in the report", None),
    (None, "fix flaky test", None),
    (None, "root cause the crash", None),
    (None, "summarize the report", None),
    ("code-reviewer", "look at this", None),
    (None, "Debug the proxy", "and then refactor it"),
    (None, "x", "please review the change"),
    (None, None, None),
    ("Plan", "anything", None),
    ("debug", "", None),
    (None, "Build the CLI", None),
    (None, "rewrite the parser", None),
    (None, "reviewing PR 12", None),
    ("general-purpose", "Generate fixtures", None),
    (None, "explain the cache", "why is it slow"),
]


def build_classify() -> list:
    return [[st, desc, head, classify_dispatch(st, desc, head)] for st, desc, head in CLASSIFY_CASES]


def render_ts(name: str, obj) -> str:
    body = json.dumps(obj, indent=1, sort_keys=True, allow_nan=False)
    return ("// Generated by `python -m apex_router.core.fixtures --write` from pce-core. Do not edit.\n"
            f"export const {name} = {body}\n")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    files = {"core.ts": render_ts("CORE", build_core()), "classify.ts": render_ts("CLASSIFY", build_classify())}
    if "--write" in argv:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (FIXTURE_DIR / name).write_text(text)
        return 0
    stale = [n for n, t in files.items() if not (FIXTURE_DIR / n).exists() or (FIXTURE_DIR / n).read_text() != t]
    if stale:
        print("stale fixtures: " + ", ".join(stale) + " — run python -m apex_router.core.fixtures --write")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Generate the fixtures and run the test**

```bash
PYTHONPATH=src ~/src/apex-router/.venv/bin/python -m apex_router.core.fixtures --write
PYTHONPATH=src ~/src/apex-router/.venv/bin/python -m apex_router.core.fixtures --check && echo fresh
~/src/apex-router/.venv/bin/pytest -q tests/test_core_fixtures.py
```

Expected: `fresh`, then `2 passed`. Spot-check `plugin/tests/fixtures/core.ts`: `cells.all_pass_35.states[34]` is `"READY"`; `drift.penalty.trace[5]` is `[5, "DRIFTING", 7]`. In `classify.ts` the `"Implement backend Task 3: pressure"` row ends in `"generate"`.

- [ ] **Step 5: Full suite, plugin tests, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1758 passed, 4 skipped`. Run `claude plugin test plugin` → still `8 pass` (fixtures are not test files).

```bash
git add src/apex_router/core/fixtures.py tests/test_core_fixtures.py plugin/tests/fixtures/core.ts plugin/tests/fixtures/classify.ts
git commit -m "test(core): generated TS parity fixtures from the pce-core oracle"
```

---

### Task 7: TS mirror — `core/stats.ts` and `core/linalg.ts` with parity tests

**Files:**
- Create: `plugin/hooks/core/stats.ts`, `plugin/hooks/core/linalg.ts`, `plugin/tests/core_stats.test.ts`

**Interfaces:**
- Consumes: `CORE` (Task 6), `close` (Task 1), contract types `Welford`, `WelfordCov`.
- Produces: `core/stats.ts`: `welfordNew()`, `welfordPush(w, x)`, `welfordVariance(w)`, `welfordCovNew(d)`, `welfordCovPush(s, x)`, `covariance(s)`, `ewma(prev, x, a = 0.2)`, `halfLife(a)`, `Q16`, `toQ16(x)`, `ewmaQ16(prev, x, aQ16 = 13107)`, `wilson(k, n, z = 1.96): [lo, hi]` (throws `RangeError('n must be positive')` like the Python), `nearestRank(sorted, q)`. `core/linalg.ts`: `dot(a, b)`, `solveNormalEquations(X, y, ridge = 1e-6)`, `residualTrend(idx, resid)`, `jacobiEigen(A, tol = 1e-12, maxSweeps = 64): { values, vectors }`, `hasGap(values, k, rel = 1e-6)`, `principalAngle(u, w)`.

- [ ] **Step 1: Write the failing parity test**

`plugin/tests/core_stats.test.ts`:

```ts
import { describe, expect, test } from 'claude-code/testing'

import { hasGap, jacobiEigen, principalAngle, residualTrend, solveNormalEquations } from '../hooks/core/linalg.ts'
import {
  covariance, ewma, ewmaQ16, halfLife, nearestRank, welfordCovNew, welfordCovPush, welfordNew, welfordPush, welfordVariance, wilson,
} from '../hooks/core/stats.ts'
import { close } from './fixtures/close.ts'
import { CORE } from './fixtures/core.ts'

describe('core/stats ≡ apex_router.core.stats', () => {
  test('welford', () => {
    let w = welfordNew()
    for (const x of CORE.welford.xs) w = welfordPush(w, x)
    expect(w.n).toBe(CORE.welford.n)
    close(w.mean, CORE.welford.mean)
    close(w.m2, CORE.welford.m2)
    close(welfordVariance(w), CORE.welford.var)
  })

  test('welford covariance', () => {
    let s = welfordCovNew(2)
    for (const row of CORE.welford_cov.rows) s = welfordCovPush(s, row)
    expect(s.n).toBe(CORE.welford_cov.n)
    CORE.welford_cov.mean.forEach((m, i) => close(s.mean[i], m))
    const cov = covariance(s)
    CORE.welford_cov.cov.forEach((row, i) => row.forEach((v, j) => close(cov[i]![j], v)))
  })

  test('ewma float and Q16 (bit-exact)', () => {
    let s: number | null = null
    let q: number | null = null
    CORE.ewma.xs.forEach((x, i) => {
      s = ewma(s, x, CORE.ewma.a)
      q = ewmaQ16(q, x)
      close(s, CORE.ewma.out[i]!)
      expect(q).toBe(CORE.ewma.q16[i]!)
    })
    expect(halfLife(0.2).toFixed(1)).toBe('3.1')
  })

  test('wilson', () => {
    for (const [k, n, lo, hi] of CORE.wilson) {
      const [l, h] = wilson(k!, n!)
      close(l, lo!)
      close(h, hi!)
    }
    expect(() => wilson(0, 0)).toThrow('n must be positive')
  })

  test('nearest rank', () => {
    for (const [q, v] of CORE.nearest_rank.cases) expect(nearestRank(CORE.nearest_rank.values, q!)).toBe(v!)
    expect(nearestRank([], 0.5)).toBeNull()
  })
})

describe('core/linalg ≡ apex_router.core.linalg', () => {
  test('jacobi eigenpairs', () => {
    for (const fx of CORE.jacobi) {
      const { values, vectors } = jacobiEigen(fx.A)
      fx.values.forEach((v, i) => close(values[i], v))
      fx.vectors.forEach((vec, i) => vec.forEach((x, j) => close(vectors[i]![j], x)))
    }
  })

  test('principal angle and gap', () => {
    for (const [u, w, angle] of CORE.principal_angle) close(principalAngle(u as number[], w as number[]), angle as number)
    for (const [values, k, gap] of CORE.has_gap) expect(hasGap(values as number[], k as number)).toBe(gap as boolean)
  })

  test('normal-equations solve and residual trend', () => {
    const beta = solveNormalEquations(CORE.solve.X, CORE.solve.y, CORE.solve.ridge)
    CORE.solve.beta.forEach((b, i) => close(beta[i], b))
    close(residualTrend(CORE.residual_trend.idx, CORE.residual_trend.resid), CORE.residual_trend.slope)
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/core/linalg.ts` cannot be resolved.

- [ ] **Step 3: Write the mirrors**

`plugin/hooks/core/stats.ts`:

```ts
// pce-core stats — TypeScript mirror of src/apex_router/core/stats.py (the in-engine subset).
// Parity: tests/core_stats.test.ts against fixtures generated by apex_router.core.fixtures.
import type { Welford, WelfordCov } from '../../types/index.d.ts'

export const welfordNew = (): Welford => ({ n: 0, mean: 0, m2: 0 })

export function welfordPush(w: Welford, x: number): Welford {
  const n = w.n + 1
  const delta = x - w.mean
  const mean = w.mean + delta / n
  return { n, mean, m2: w.m2 + delta * (x - mean) }
}

export const welfordVariance = (w: Welford): number => (w.n > 1 ? w.m2 / (w.n - 1) : 0)

export function welfordCovNew(d: number): WelfordCov {
  return {
    n: 0,
    mean: Array.from({ length: d }, () => 0),
    c: Array.from({ length: d }, () => Array.from({ length: d }, () => 0)),
  }
}

export function welfordCovPush(s: WelfordCov, x: readonly number[]): WelfordCov {
  const n = s.n + 1
  const delta = x.map((xi, i) => xi - s.mean[i]!)
  const mean = s.mean.map((m, i) => m + delta[i]! / n)
  const after = x.map((xi, i) => xi - mean[i]!)
  const c = s.c.map((row, i) => row.map((cij, j) => cij + delta[i]! * after[j]!))
  return { n, mean, c }
}

export const covariance = (s: WelfordCov): number[][] => s.c.map(row => row.map(cij => (s.n > 1 ? cij / (s.n - 1) : 0)))

export const ewma = (prev: number | null, x: number, a = 0.2): number => (prev === null ? x : a * x + (1 - a) * prev)

export const halfLife = (a: number): number => Math.log(0.5) / Math.log(1 - a)

export const Q16 = 65536

export const toQ16 = (x: number): number => Math.floor(x * Q16 + 0.5)

export function ewmaQ16(prev: number | null, x: number, aQ16 = 13107): number {
  const xq = toQ16(x)
  return prev === null ? xq : prev + Math.trunc(((xq - prev) * aQ16) / Q16)
}

export function wilson(k: number, n: number, z = 1.96): [number, number] {
  if (n <= 0) throw new RangeError('n must be positive')
  const pHat = k / n
  const zSq = z * z
  const center = (pHat + zSq / (2 * n)) / (1 + zSq / n)
  const half = (z / (1 + zSq / n)) * Math.sqrt((pHat * (1 - pHat)) / n + zSq / (4 * n * n))
  return [Math.max(0, center - half), Math.min(1, center + half)]
}

export function nearestRank(sorted: readonly number[], q: number): number | null {
  if (sorted.length === 0) return null
  let rank = Math.ceil(q * sorted.length)
  rank = Math.max(1, Math.min(rank, sorted.length))
  return sorted[rank - 1]!
}
```

`plugin/hooks/core/linalg.ts`:

```ts
// pce-core linalg — TypeScript mirror of src/apex_router/core/linalg.py.

export function dot(a: readonly number[], b: readonly number[]): number {
  let s = 0
  for (let i = 0; i < Math.min(a.length, b.length); i++) s += a[i]! * b[i]!
  return s
}

export function solveNormalEquations(X: readonly (readonly number[])[], y: readonly number[], ridge = 1e-6): number[] {
  const n = X.length
  const p = n ? X[0]!.length : 0
  const A = Array.from({ length: p }, () => Array.from({ length: p }, () => 0))
  const b = Array.from({ length: p }, () => 0)
  for (let i = 0; i < n; i++) {
    const xi = X[i]!
    const yi = y[i]!
    for (let a = 0; a < p; a++) {
      b[a]! += xi[a]! * yi
      const xia = xi[a]!
      const row = A[a]!
      for (let c = 0; c < p; c++) row[c]! += xia * xi[c]!
    }
  }
  for (let a = 0; a < p; a++) A[a]![a]! += ridge
  for (let col = 0; col < p; col++) {
    let piv = col
    for (let r = col + 1; r < p; r++) if (Math.abs(A[r]![col]!) > Math.abs(A[piv]![col]!)) piv = r
    if (Math.abs(A[piv]![col]!) < 1e-12) continue
    if (piv !== col) {
      ;[A[col], A[piv]] = [A[piv]!, A[col]!]
      ;[b[col], b[piv]] = [b[piv]!, b[col]!]
    }
    const pivval = A[col]![col]!
    for (let r = 0; r < p; r++) {
      if (r === col) continue
      const factor = A[r]![col]! / pivval
      if (factor === 0) continue
      for (let c = col; c < p; c++) A[r]![c]! -= factor * A[col]![c]!
      b[r]! -= factor * b[col]!
    }
  }
  return Array.from({ length: p }, (_, i) => (Math.abs(A[i]![i]!) > 1e-12 ? b[i]! / A[i]![i]! : 0))
}

export function residualTrend(idx: readonly number[], resid: readonly number[]): number {
  const n = idx.length
  if (n < 2) return 0
  let mx = 0
  let my = 0
  for (let i = 0; i < n; i++) {
    mx += idx[i]!
    my += resid[i]!
  }
  mx /= n
  my /= n
  let num = 0
  let den = 0
  for (let i = 0; i < n; i++) {
    num += (idx[i]! - mx) * (resid[i]! - my)
    den += (idx[i]! - mx) ** 2
  }
  return den > 0 ? num / den : 0
}

export function jacobiEigen(A: readonly (readonly number[])[], tol = 1e-12, maxSweeps = 64): { values: number[]; vectors: number[][] } {
  const n = A.length
  const a = A.map(row => row.map(Number))
  const v = Array.from({ length: n }, (_, i) => Array.from({ length: n }, (_, j): number => (i === j ? 1 : 0)))
  for (let sweep = 0; sweep < maxSweeps; sweep++) {
    let off = 0
    for (let p = 0; p < n; p++) for (let q = p + 1; q < n; q++) off += a[p]![q]! * a[p]![q]!
    let total = 0
    for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) total += a[i]![j]! * a[i]![j]!
    if (total === 0 || off <= tol * tol * total) break
    for (let p = 0; p < n - 1; p++) {
      for (let q = p + 1; q < n; q++) {
        const apq = a[p]![q]!
        if (apq === 0) continue
        const theta = (a[q]![q]! - a[p]![p]!) / (2 * apq)
        const t = (theta >= 0 ? 1 : -1) / (Math.abs(theta) + Math.sqrt(theta * theta + 1))
        const c = 1 / Math.sqrt(t * t + 1)
        const s = t * c
        for (let k = 0; k < n; k++) {
          const akp = a[k]![p]!
          const akq = a[k]![q]!
          a[k]![p] = c * akp - s * akq
          a[k]![q] = s * akp + c * akq
        }
        for (let k = 0; k < n; k++) {
          const apk = a[p]![k]!
          const aqk = a[q]![k]!
          a[p]![k] = c * apk - s * aqk
          a[q]![k] = s * apk + c * aqk
        }
        for (let k = 0; k < n; k++) {
          const vkp = v[k]![p]!
          const vkq = v[k]![q]!
          v[k]![p] = c * vkp - s * vkq
          v[k]![q] = s * vkp + c * vkq
        }
      }
    }
  }
  const order = Array.from({ length: n }, (_, i) => i).sort((i, j) => a[j]![j]! - a[i]![i]!)
  const values = order.map(i => a[i]![i]!)
  const vectors = order.map(i => {
    const col = Array.from({ length: n }, (_, k) => v[k]![i]!)
    const lead = col.find(x => Math.abs(x) > 1e-12) ?? 0
    return lead < 0 ? col.map(x => -x) : col
  })
  return { values, vectors }
}

export function hasGap(values: readonly number[], k: number, rel = 1e-6): boolean {
  if (k <= 0 || k >= values.length) return false
  const scale = Math.max(Math.abs(values[0]!), Number.MIN_VALUE)
  return values[k - 1]! - values[k]! > rel * scale
}

export function principalAngle(u: readonly number[], w: readonly number[]): number {
  const nu = Math.sqrt(dot(u, u))
  const nw = Math.sqrt(dot(w, w))
  if (nu === 0 || nw === 0) return Math.PI / 2
  const c = Math.min(1, Math.abs(dot(u, w)) / (nu * nw))
  return Math.acos(c)
}
```

- [ ] **Step 4: Run the tests**

Run: `claude plugin test plugin`
Expected: `16 pass`, `0 fail` (8 band + 8 core).

- [ ] **Step 5: Full suite, validate, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1758 passed, 4 skipped`; `claude plugin validate plugin` → passed.

```bash
git add plugin/hooks/core/stats.ts plugin/hooks/core/linalg.ts plugin/tests/core_stats.test.ts
git commit -m "feat(plugin): pce-core TS mirror — stats and linalg, parity with the Python oracle"
```

---

### Task 8: TS mirror — `core/anomaly.ts`, `core/drift.ts`, `core/cost.ts`, `core/routing.ts`

**Files:**
- Create: `plugin/hooks/core/anomaly.ts`, `plugin/hooks/core/drift.ts`, `plugin/hooks/core/cost.ts`, `plugin/hooks/core/routing.ts`, `plugin/tests/core_models.test.ts`

**Interfaces:**
- Consumes: Task 7 (`dot`, `jacobiEigen`, `principalAngle`, `solveNormalEquations`, `residualTrend`, `wilson`); contract type `Cell`.
- Produces:
  - `core/anomaly.ts`: `CHI2_99`, `WARMUP_N = 50`, `HIST_MAX = 200`, `maxAbsScales(rows)`, `prescale(x, scales)`, `zscore(x, mean, sd)`, `median(xs)`, `mad(xs)`, `robustZ(x, med, mad)`, `correlationOf(cov): { corr, sd }`, `pcaFit(matrix, k): { values, vectors }`, `qResidual(xc, V)`, `tSquared(xc, V, values)`, `t2Limit(k)`, `ecdf(value, history)`, `explain(q, t2, qHist, t2Hist): Card` where `Card = { score: number; term: 'Q' | 'T2'; q: number; t2: number }`.
  - `core/drift.ts`: `varianceL1(l0, l1)`, `cusumLower(s, z, kappa = 0.5, h = 4): [number, boolean]`, `penaltyRamp(T, lo = 2, hi = 6, pMax = 10)`, `PenaltyState`, `penaltyRun(scores, enter = 2, exit = 3, warmAfter = 3): [number, PenaltyState, number][]`, `subspaceAngle`.
  - `core/cost.ts`: `CostFit = { a; b; r2; trend; n }`, `fitCost(xs, ys): CostFit | null`, `Budget = { burn: number; minutesToExhaust: number | null }`, `budgetBurn(spentUsd, minutes, budgetUsd): Budget | null`.
  - `core/routing.ts`: `MIN_N`, `TARGET`, `WINDOW`, `ENTER`, `EXIT`, `REBASE`, `P0_CAP`, `cellNew(): Cell`, `cellObserve(cell, passed, minN = MIN_N, target = TARGET, window = WINDOW): Cell`.

- [ ] **Step 1: Write the failing parity test**

`plugin/tests/core_models.test.ts`:

```ts
import { describe, expect, test } from 'claude-code/testing'

import type { Cell } from '../types/index.d.ts'
import {
  correlationOf, explain, mad, maxAbsScales, median, pcaFit, qResidual, robustZ, tSquared, t2Limit,
} from '../hooks/core/anomaly.ts'
import { budgetBurn, fitCost } from '../hooks/core/cost.ts'
import { cusumLower, penaltyRun, varianceL1 } from '../hooks/core/drift.ts'
import { cellNew, cellObserve } from '../hooks/core/routing.ts'
import { close } from './fixtures/close.ts'
import { CORE } from './fixtures/core.ts'

describe('core/anomaly ≡ apex_router.core.anomaly', () => {
  test('Q and T² cases', () => {
    for (const [xc, q, t2] of CORE.anomaly.cases) {
      close(qResidual(xc as number[], CORE.anomaly.V), q as number)
      close(tSquared(xc as number[], CORE.anomaly.V, CORE.anomaly.values), t2 as number)
    }
    close(t2Limit(2), -2 * Math.log(0.01))
  })

  test('correlation and PCA', () => {
    const { corr, sd } = correlationOf(CORE.anomaly.cov)
    CORE.anomaly.sd.forEach((s, i) => close(sd[i], s))
    CORE.anomaly.corr.forEach((row, i) => row.forEach((v, j) => close(corr[i]![j], v)))
    const { values, vectors } = pcaFit(corr, 2)
    CORE.anomaly.pca.values.forEach((v, i) => close(values[i], v))
    CORE.anomaly.pca.vectors.forEach((vec, i) => vec.forEach((x, j) => close(vectors[i]![j], x)))
  })

  test('scales, median/MAD, robust z, explain card', () => {
    expect(maxAbsScales(CORE.anomaly.scales.rows)).toEqual(CORE.anomaly.scales.scales)
    close(median(CORE.anomaly.mad.xs), CORE.anomaly.mad.median)
    close(mad(CORE.anomaly.mad.xs), CORE.anomaly.mad.mad)
    for (const [x, z] of CORE.anomaly.mad.robust) close(robustZ(x!, CORE.anomaly.mad.median, CORE.anomaly.mad.mad), z!)
    for (const [q, t2, qh, th, card] of CORE.anomaly.explain) {
      expect(explain(q as number, t2 as number, qh as number[], th as number[])).toEqual(card as never)
    }
  })
})

describe('core/drift ≡ apex_router.core.drift', () => {
  test('cusum trace', () => {
    let s = 0
    CORE.drift.cusum.zs.forEach((z, i) => {
      const [next, alarm] = cusumLower(s, z)
      s = next
      close(next, CORE.drift.cusum.trace[i]![0] as number)
      expect(alarm).toBe(CORE.drift.cusum.trace[i]![1] as boolean)
    })
  })

  test('penalty state machine trace', () => {
    expect(penaltyRun(CORE.drift.penalty.scores)).toEqual(CORE.drift.penalty.trace as never)
  })

  test('variance share L1', () => {
    for (const [l0, l1, v] of CORE.drift.variance_l1) close(varianceL1(l0 as number[], l1 as number[]), v as number)
  })
})

describe('core/cost ≡ apex_router.core.cost', () => {
  test('OLS cost fit and budget burn', () => {
    const fit = fitCost(CORE.cost.xs, CORE.cost.ys)!
    const ref = CORE.cost.fit
    close(fit.a, ref.a)
    close(fit.b, ref.b)
    close(fit.r2, ref.r2)
    close(fit.trend, ref.trend, 1e-6)
    expect(fit.n).toBe(ref.n)
    expect(fitCost([1, 2], [1, 2])).toBeNull()
    for (const [spent, minutes, budget, result] of CORE.cost.budget) {
      const r = budgetBurn(spent as number, minutes as number, budget as number)
      if (result === null) expect(r).toBeNull()
      else {
        const want = result as { burn: number; minutesToExhaust: number | null }
        close(r!.burn, want.burn)
        if (want.minutesToExhaust === null) expect(r!.minutesToExhaust).toBeNull()
        else close(r!.minutesToExhaust, want.minutesToExhaust)
      }
    }
  })
})

describe('core/routing ≡ apex_router.core.routing (cell state machine)', () => {
  test('every scenario reproduces state by state', () => {
    for (const [name, fx] of Object.entries(CORE.cells)) {
      let c: Cell = cellNew()
      fx.seq.forEach((x, i) => {
        c = cellObserve(c, x === 1, fx.min_n, fx.target)
        expect(c.state, `${name}[${i}]`).toBe(fx.states[i] as Cell['state'])
      })
      expect({ ...c, cusum: 0 }, name).toEqual({ ...(fx.final as Cell), cusum: 0 })
      close(c.cusum, fx.final.cusum, 1e-9, name)
    }
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/core/anomaly.ts` cannot be resolved.

- [ ] **Step 3: Write the four mirrors**

`plugin/hooks/core/anomaly.ts`:

```ts
// pce-core anomaly — TypeScript mirror of src/apex_router/core/anomaly.py.
import { dot, jacobiEigen } from './linalg.ts'

export const CHI2_99: Readonly<Record<number, number>> = {
  1: 6.634896601021214,
  2: 9.210340371976184,
  3: 11.344866730144373,
  4: 13.276704135987622,
  5: 15.08627246938899,
}
export const WARMUP_N = 50
export const HIST_MAX = 200

export type Card = { score: number; term: 'Q' | 'T2'; q: number; t2: number }

export function maxAbsScales(rows: readonly (readonly number[])[]): number[] {
  const d = rows[0]?.length ?? 0
  const scales: number[] = []
  for (let j = 0; j < d; j++) {
    let m = 0
    for (const row of rows) m = Math.max(m, Math.abs(row[j]!))
    scales.push(m > 0 ? m : 1)
  }
  return scales
}

export const prescale = (x: readonly number[], scales: readonly number[]): number[] => x.map((xi, j) => xi / scales[j]!)

export const zscore = (x: readonly number[], mean: readonly number[], sd: readonly number[]): number[] =>
  x.map((xi, j) => (xi - mean[j]!) / (sd[j]! > 0 ? sd[j]! : 1))

export function median(xs: readonly number[]): number {
  if (xs.length === 0) return 0
  const s = [...xs].sort((a, b) => a - b)
  const m = Math.floor(s.length / 2)
  return s.length % 2 ? s[m]! : (s[m - 1]! + s[m]!) / 2
}

export function mad(xs: readonly number[]): number {
  const med = median(xs)
  return median(xs.map(x => Math.abs(x - med)))
}

export const robustZ = (x: number, med: number, madValue: number): number => (madValue > 0 ? (0.6745 * (x - med)) / madValue : 0)

export function correlationOf(cov: readonly (readonly number[])[]): { corr: number[][]; sd: number[] } {
  const sd = cov.map((row, i) => (row[i]! > 0 ? Math.sqrt(row[i]!) : 0))
  const corr = cov.map((row, i) => row.map((cij, j) => cij / ((sd[i] || 1) * (sd[j] || 1))))
  return { corr, sd }
}

export function pcaFit(matrix: readonly (readonly number[])[], k: number): { values: number[]; vectors: number[][] } {
  const { values, vectors } = jacobiEigen(matrix)
  return { values: values.slice(0, k), vectors: vectors.slice(0, k) }
}

export function qResidual(xc: readonly number[], V: readonly (readonly number[])[]): number {
  const proj = V.map(v => dot(v, xc))
  let total = 0
  for (let j = 0; j < xc.length; j++) {
    let recon = 0
    V.forEach((v, i) => {
      recon += proj[i]! * v[j]!
    })
    total += (xc[j]! - recon) ** 2
  }
  return total
}

export function tSquared(xc: readonly number[], V: readonly (readonly number[])[], values: readonly number[]): number {
  let t = 0
  V.forEach((v, i) => {
    const lam = values[i]!
    if (lam > 0) {
      const s = dot(v, xc)
      t += (s * s) / lam
    }
  })
  return t
}

export const t2Limit = (k: number): number | null => CHI2_99[k] ?? null

export function ecdf(value: number, history: readonly number[]): number {
  if (history.length === 0) return 0
  let count = 0
  for (const h of history) if (h <= value) count++
  return count / history.length
}

export function explain(q: number, t2: number, qHist: readonly number[], t2Hist: readonly number[]): Card {
  const rq = ecdf(q, qHist)
  const rt = ecdf(t2, t2Hist)
  return rq >= rt ? { score: rq, term: 'Q', q: rq, t2: rt } : { score: rt, term: 'T2', q: rq, t2: rt }
}
```

`plugin/hooks/core/drift.ts`:

```ts
// pce-core drift — TypeScript mirror of src/apex_router/core/drift.py.
export { principalAngle as subspaceAngle } from './linalg.ts'

export function varianceL1(l0: readonly number[], l1: readonly number[]): number {
  let s0 = 0
  for (const x of l0) s0 += x
  let s1 = 0
  for (const x of l1) s1 += x
  if (s0 <= 0 || s1 <= 0) return 0
  let total = 0
  for (let i = 0; i < Math.min(l0.length, l1.length); i++) total += Math.abs(l0[i]! / s0 - l1[i]! / s1)
  return total
}

export function cusumLower(s: number, z: number, kappa = 0.5, h = 4): [number, boolean] {
  const next = Math.max(0, s - z - kappa)
  return [next, next > h]
}

export const penaltyRamp = (T: number, lo = 2, hi = 6, pMax = 10): number =>
  Math.trunc(pMax * Math.min(1, Math.max(0, (T - lo) / (hi - lo))))

export type PenaltyState = 'COLD' | 'WARM' | 'DRIFTING'

export function penaltyRun(scores: readonly number[], enter = 2, exit = 3, warmAfter = 3): [number, PenaltyState, number][] {
  let state: PenaltyState = 'COLD'
  let streak = 0
  let seen = 0
  const out: [number, PenaltyState, number][] = []
  for (const T of scores) {
    seen += 1
    if (state === 'COLD' && seen >= warmAfter) state = 'WARM'
    else if (state === 'WARM') {
      streak = T >= 2 ? streak + 1 : 0
      if (streak >= enter) {
        state = 'DRIFTING'
        streak = 0
      }
    } else if (state === 'DRIFTING') {
      streak = T < 2 ? streak + 1 : 0
      if (streak >= exit) {
        state = 'WARM'
        streak = 0
      }
    }
    out.push([T, state, state === 'DRIFTING' ? penaltyRamp(T) : 0])
  }
  return out
}
```

`plugin/hooks/core/cost.ts`:

```ts
// pce-core cost — TypeScript mirror of src/apex_router/core/cost.py.
import { residualTrend, solveNormalEquations } from './linalg.ts'

export type CostFit = { a: number; b: number; r2: number; trend: number; n: number }
export type Budget = { burn: number; minutesToExhaust: number | null }

export function fitCost(xs: readonly number[], ys: readonly number[]): CostFit | null {
  const n = xs.length
  if (n < 3 || ys.length !== n) return null
  const [a = 0, b = 0] = solveNormalEquations(xs.map(x => [1, x]), ys)
  const resid = ys.map((y, i) => y - (a + b * xs[i]!))
  let meanY = 0
  for (const y of ys) meanY += y
  meanY /= n
  let ssRes = 0
  let ssTot = 0
  for (let i = 0; i < n; i++) {
    ssRes += resid[i]! ** 2
    ssTot += (ys[i]! - meanY) ** 2
  }
  const r2 = ssTot > 0 ? 1 - ssRes / ssTot : 0
  return { a, b, r2, trend: residualTrend(xs.map((_, i) => i), resid), n }
}

export function budgetBurn(spentUsd: number, minutes: number, budgetUsd: number): Budget | null {
  if (budgetUsd <= 0 || minutes <= 0) return null
  const rate = spentUsd / minutes
  const burn = rate / (budgetUsd / 1440)
  return { burn, minutesToExhaust: rate > 0 ? Math.max(0, budgetUsd - spentUsd) / rate : null }
}
```

`plugin/hooks/core/routing.ts`:

```ts
// pce-core routing — TypeScript mirror of cell_new/cell_observe in src/apex_router/core/routing.py.
import type { Cell } from '../../types/index.d.ts'
import { cusumLower } from './drift.ts'
import { wilson } from './stats.ts'

export const MIN_N = 30
export const TARGET = 0.9
export const WINDOW = 10
export const ENTER = 2
export const EXIT = 3
export const REBASE = 3
export const P0_CAP = 0.98

export const cellNew = (): Cell => ({ n: 0, pass: 0, state: 'COLD', winN: 0, winPass: 0, below: 0, above: 0, cusum: 0, p0: null })

export function cellObserve(cell: Cell, passed: boolean, minN = MIN_N, target = TARGET, window = WINDOW): Cell {
  const x = passed ? 1 : 0
  const c: Cell = { ...cell, n: cell.n + 1, pass: cell.pass + x, winN: cell.winN + 1, winPass: cell.winPass + x }
  let alarm = false
  if (c.winN >= window) {
    const rate = c.winPass / c.winN
    const below = rate < target
    if ((c.state === 'READY' || c.state === 'DRIFTING') && c.p0 !== null) {
      const p0 = Math.min(c.p0, P0_CAP)
      const se = Math.sqrt((p0 * (1 - p0)) / c.winN)
      ;[c.cusum, alarm] = cusumLower(c.cusum, (rate - p0) / se)
    }
    if (c.state === 'READY') {
      c.below = below ? c.below + 1 : 0
      if (c.below >= ENTER || alarm) {
        c.state = 'DRIFTING'
        c.below = 0
        c.above = 0
      }
    } else if (c.state === 'DRIFTING') {
      if (below) {
        c.below += 1
        c.above = 0
      } else {
        c.above += 1
        c.below = 0
      }
      if (c.above >= EXIT) {
        c.state = 'READY'
        c.above = 0
        c.cusum = 0
      } else if (c.below >= REBASE) return cellNew()
    }
    c.winN = 0
    c.winPass = 0
  }
  if (c.state === 'COLD' || c.state === 'WARMING') {
    if (c.n >= minN && wilson(c.pass, c.n)[0] >= target) {
      c.state = 'READY'
      c.p0 = c.pass / c.n
      c.cusum = 0
      c.below = 0
      c.above = 0
    } else c.state = c.n === 0 ? 'COLD' : 'WARMING'
  }
  return c
}
```

- [ ] **Step 4: Run the tests**

Run: `claude plugin test plugin`
Expected: `24 pass`, `0 fail`.

- [ ] **Step 5: Full suite, validate, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1758 passed, 4 skipped`; `claude plugin validate plugin` → passed.

```bash
git add plugin/hooks/core plugin/tests/core_models.test.ts
git commit -m "feat(plugin): pce-core TS mirror — anomaly, drift, cost, cell state machine"
```

---

### Task 9: `classify.ts` — port `route_log.classify_dispatch`, Python kept as the oracle

**Files:**
- Create: `plugin/hooks/classify.ts`, `plugin/tests/classify.test.ts`

**Interfaces:**
- Consumes: `CLASSIFY` (Task 6).
- Produces: `PROMPT_HEAD = 200`, `promptHeadOf(prompt: string): string`, `classifyDispatch(subagentType?, description?, promptHead?): TaskType` — precedence exactly as the Python: `Explore` subagent → explore; a whole-word marker in description + prompt head + subagent type, checked in order `debug, review, refactor, generate, explore, plan(→explore)`; then the fallback regexes `\b(review|audit|verif)` → review, `\b(implement|write|build|fix)` → generate, `\b(debug|root[ -]cause|why)\b` → debug, `\b(refactor|rename)` → refactor; else explore. The Python oracle never returns `mechanical`, `synthesis` or `other`; neither does the port (parity is binding — those task types stay reserved until the oracle adds them). The prompt head is used in memory only; it is never stored (§10).

- [ ] **Step 1: Write the failing parity test**

`plugin/tests/classify.test.ts`:

```ts
import { describe, expect, test } from 'claude-code/testing'

import { classifyDispatch, promptHeadOf } from '../hooks/classify.ts'
import { CLASSIFY } from './fixtures/classify.ts'

describe('classify ≡ route_log.classify_dispatch', () => {
  test('every oracle case', () => {
    for (const [st, desc, head, want] of CLASSIFY) {
      expect(classifyDispatch(st, desc, head), `${String(st)} | ${String(desc)} | ${String(head)}`).toBe(want as string)
    }
  })

  test('prompt head is trimmed and cut at 200', () => {
    expect(promptHeadOf(`  ${'a'.repeat(300)}  `)).toBe('a'.repeat(200))
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/classify.ts` cannot be resolved.

- [ ] **Step 3: Write the port**

`plugin/hooks/classify.ts`:

```ts
// Port of apex_router.route_log.classify_dispatch (+ classify.classify_request's marker table).
// The Python is the oracle: tests/classify.test.ts replays plugin/tests/fixtures/classify.ts.
import type { TaskType } from '../types/index.d.ts'

const MARKERS: readonly (readonly [string, TaskType])[] = [
  ['debug', 'debug'],
  ['review', 'review'],
  ['refactor', 'refactor'],
  ['generate', 'generate'],
  ['explore', 'explore'],
  ['plan', 'explore'],
]

const FALLBACK: readonly (readonly [RegExp, TaskType])[] = [
  [/\b(review|audit|verif)/, 'review'],
  [/\b(implement|write|build|fix)/, 'generate'],
  [/\b(debug|root[ -]cause|why)\b/, 'debug'],
  [/\b(refactor|rename)/, 'refactor'],
]

export const PROMPT_HEAD = 200

export const promptHeadOf = (prompt: string): string => prompt.trim().slice(0, PROMPT_HEAD)

export function classifyDispatch(subagentType?: string | null, description?: string | null, promptHead?: string | null): TaskType {
  try {
    const st = typeof subagentType === 'string' ? subagentType.trim().toLowerCase() : ''
    if (st === 'explore') return 'explore'
    const text = [description, promptHead]
      .filter((x): x is string => typeof x === 'string')
      .join(' ')
      .toLowerCase()
    const words = new Set(text.match(/[a-z]+/g) ?? [])
    if (st !== '') words.add(st)
    for (const [marker, type] of MARKERS) if (words.has(marker)) return type
    for (const [pattern, type] of FALLBACK) if (pattern.test(text)) return type
  } catch {
    // never raises, like the oracle
  }
  return 'explore'
}
```

- [ ] **Step 4: Run the tests**

Run: `claude plugin test plugin`
Expected: `26 pass`, `0 fail`.

- [ ] **Step 5: Full suite, validate, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1758 passed, 4 skipped`; `claude plugin validate plugin` → passed.

```bash
git add plugin/hooks/classify.ts plugin/tests/classify.test.ts
git commit -m "feat(plugin): classify_dispatch port with Python-oracle parity"
```

---

### Task 10: `signals.ts` — the §7 formulas, each against its reference numbers

**Files:**
- Create: `plugin/hooks/signals.ts`, `plugin/tests/signals.test.ts`

**Interfaces:**
- Consumes: contract types `Breaker`, `BreakerState`, `Level`, `LevelState`, `TokenBucket`.
- Produces: `Bucket = readonly [total, bad]`, `Burn = { alert; short; long }`, `SLO_429 = 0.98`, `SLO_TRANSPORT = 0.97`, `SHORT_MIN = 5`, `LONG_MIN = 60`, `SHORT_LIMIT = 10`, `LONG_LIMIT = 6`, `burnAlert(buckets, slo, short?, long?, shortLimit?, longLimit?)`, `spendBurn(spend, budgetUsd, w)`, `worst(levels)`, `levelStep(s, observed, enter = 2, exit = 3)`, `limitLevel(percentUsed)` (≥ 90 RED, ≥ 70 AMBER), `routingBurn(pass, total, target = 0.9)`, `BREAKER_THRESHOLD = 3`, `BREAKER_COOL_MS = 600_000`, `breakerNew()`, `breakerState(b, now, coolMs?)`, `breakerRecord(b, now, ok, threshold?)`, `bucketNew(rate, burst, t = 0)`, `bucketTake(b, now, cost = 1): { allowed, bucket }`, `admissionRate(spawnsPerMin, slopePctPerMin, headroomPct, minutesToReset)`, `fanoutRisk(p, n)`, `availability(mtbf, mttr)`, `series(...parts)`, `drainEta(backlog, cap, arrive)`.

Reference numbers (spec §7/§13 "burn (True, 11.5, 7.0), EWMA half-life 3.1, breaker opens at 3, bucket allows 10/15 then 5"): burn over buckets `[(1000,1),(1000,1),(1000,10),(1000,12),(1000,11)]`, slo 0.999, short 2, long 5 → short (23/2000)/0.001 = 11.5, long (35/5000)/0.001 = 7.0, alert. Breaker threshold 3 / cooldown 10: failures at t = 0, 2, 4 open it; t = 6..12 fast-fail; t = 14 half-open probe succeeds. Bucket rate 5/s burst 10: 10 of 15 at t = 0, 5 of 15 at t = 1 s. Fan-out p = 0.01: n = 10 → 9.6%, n = 100 → 63.4%. Availability MTBF 720 h / MTTR 0.5 h → 0.99931; series 0.9999·0.999·0.999 → 0.99790. Drain: backlog 135 000 at cap 2200/s vs 1850/s arrivals → 386 s.

Spec ambiguity resolved here: "Admission: rate from rate-limit `percentUsed` slope, burst 2". Rate (heavy spawns per minute) = clamp(`spawnsPerMin × (headroom% / minutesToReset) / slope%perMin`, 0.2, 10): the current heavy-spawn pace scaled by sustainable-over-observed burn; no slope or no reset time → 10 (unthrottled). A refused take is advised "serialize", never denied.

- [ ] **Step 1: Write the failing tests**

`plugin/tests/signals.test.ts`:

```ts
import { describe, expect, test } from 'claude-code/testing'

import type { Level, LevelState } from '../types/index.d.ts'
import {
  admissionRate, availability, breakerNew, breakerRecord, breakerState, bucketNew, bucketTake, burnAlert, drainEta,
  fanoutRisk, levelStep, limitLevel, routingBurn, series, spendBurn, worst,
} from '../hooks/signals.ts'
import { close } from './fixtures/close.ts'

describe('§7 signals against their reference numbers', () => {
  test('multi-window burn rate (True, 11.5, 7.0)', () => {
    const b = burnAlert([[1000, 1], [1000, 1], [1000, 10], [1000, 12], [1000, 11]], 0.999, 2, 5)
    expect(b.alert).toBe(true)
    expect(b.short!.toFixed(2)).toBe('11.50')
    expect(b.long!.toFixed(2)).toBe('7.00')
  })

  test('no traffic is no evidence, never an alert', () => {
    expect(burnAlert([], 0.97)).toEqual({ alert: false, short: null, long: null })
    expect(burnAlert([[0, 0], [0, 0]], 0.97)).toEqual({ alert: false, short: null, long: null })
  })

  test('spend burn = spend rate over budget/1440', () => {
    close(spendBurn(Array.from({ length: 5 }, () => 10 / 720), 10, 5), 2.0)
    expect(spendBurn([1, 2], 0, 5)).toBeNull()
    expect(spendBurn([], 10, 5)).toBeNull()
  })

  test('level hysteresis: enter after 2 windows, exit after 3', () => {
    let s: LevelState = { level: 'GREEN', up: 0, down: 0 }
    const seen: Level[] = []
    for (const observed of ['AMBER', 'AMBER', 'GREEN', 'GREEN', 'GREEN', 'RED', 'GREEN', 'RED', 'RED'] as Level[]) {
      s = levelStep(s, observed)
      seen.push(s.level)
    }
    expect(seen).toEqual(['GREEN', 'AMBER', 'AMBER', 'AMBER', 'GREEN', 'GREEN', 'GREEN', 'GREEN', 'RED'])
  })

  test('worst level and rate-limit level', () => {
    expect(worst([])).toBe('GREEN')
    expect(worst(['GREEN', 'RED', 'AMBER'])).toBe('RED')
    expect([null, 50, 70, 89.9, 90].map(limitLevel)).toEqual(['GREEN', 'GREEN', 'AMBER', 'AMBER', 'RED'])
  })

  test('lane breaker opens at 3, fast-fails for 10 minutes, half-open probe closes it', () => {
    let b = breakerNew()
    const out: string[] = []
    for (const t of [0, 2, 4, 6, 8, 10, 12, 14]) {
      const now = t * 60_000
      if (breakerState(b, now) === 'open') {
        out.push('FAST-FAIL')
        continue
      }
      const up = t >= 12
      b = breakerRecord(b, now, up)
      out.push(up ? 'ok' : 'error')
    }
    expect(out).toEqual(['error', 'error', 'error', 'FAST-FAIL', 'FAST-FAIL', 'FAST-FAIL', 'FAST-FAIL', 'ok'])
    expect(breakerState(b, 15 * 60_000)).toBe('closed')
  })

  test('token bucket allows 10 of 15, then 5 of 15 a second later', () => {
    let b = bucketNew(5, 10)
    let allowed = 0
    for (let i = 0; i < 15; i++) {
      const r = bucketTake(b, 0)
      b = r.bucket
      if (r.allowed) allowed++
    }
    expect(allowed).toBe(10)
    allowed = 0
    for (let i = 0; i < 15; i++) {
      const r = bucketTake(b, 1)
      b = r.bucket
      if (r.allowed) allowed++
    }
    expect(allowed).toBe(5)
  })

  test('admission rate scales the heavy-spawn pace by sustainable/observed burn', () => {
    close(admissionRate(2, 1.0, 40, 100), 0.8)
    expect(admissionRate(2, 0, 40, 100)).toBe(10)
    expect(admissionRate(0, 1.0, 40, 100)).toBe(0.2)
    expect(admissionRate(100, 0.01, 90, 10)).toBe(10)
  })

  test('fan-out tail, availability, series, drain ETA, routing burn', () => {
    expect(fanoutRisk(0.01, 10).toFixed(3)).toBe('0.096')
    expect(fanoutRisk(0.01, 100).toFixed(3)).toBe('0.634')
    expect(availability(720, 0.5).toFixed(5)).toBe('0.99931')
    expect(series(0.9999, 0.999, 0.999).toFixed(5)).toBe('0.99790')
    expect(Math.round(drainEta(135000, 2200, 1850)!)).toBe(386)
    expect(drainEta(10, 5, 6)).toBeNull()
    close(routingBurn(34, 36), (1 - 34 / 36) / 0.1)
    expect(routingBurn(0, 0)).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/signals.ts` cannot be resolved.

- [ ] **Step 3: Write `signals.ts`**

`plugin/hooks/signals.ts`:

```ts
// §7 signals as pure functions; tests/signals.test.ts pins each to its reference numbers.
// Descriptive statistics (kurtosis, medians) are never alerts (§7 "explicit non-rules").
import type { Breaker, BreakerState, Level, LevelState, TokenBucket } from '../types/index.d.ts'

export type Bucket = readonly [total: number, bad: number]
export type Burn = { alert: boolean; short: number | null; long: number | null }

export const SLO_429 = 0.98
export const SLO_TRANSPORT = 0.97
export const SHORT_MIN = 5
export const LONG_MIN = 60
export const SHORT_LIMIT = 10
export const LONG_LIMIT = 6

/** Prefix sums over 1-min buckets; burn(w) = (bad_w/tot_w)/(1−slo); alert iff both windows burn hot. */
export function burnAlert(
  buckets: readonly Bucket[],
  slo: number,
  short = SHORT_MIN,
  long = LONG_MIN,
  shortLimit = SHORT_LIMIT,
  longLimit = LONG_LIMIT,
): Burn {
  const tot = [0]
  const bad = [0]
  for (const [t, b] of buckets) {
    tot.push(tot[tot.length - 1]! + t)
    bad.push(bad[bad.length - 1]! + b)
  }
  const end = tot.length - 1
  const burn = (w: number): number | null => {
    const start = Math.max(0, end - w)
    const t = tot[end]! - tot[start]!
    return t === 0 ? null : (bad[end]! - bad[start]!) / t / (1 - slo)
  }
  const s = burn(short)
  const l = burn(long)
  return { alert: s !== null && l !== null && s > shortLimit && l > longLimit, short: s, long: l }
}

/** Budget burn over the last w minutes: (spend per minute) / (budget / 1440). */
export function spendBurn(spend: readonly number[], budgetUsd: number, w: number): number | null {
  if (budgetUsd <= 0 || spend.length === 0) return null
  const window = spend.slice(-w)
  let total = 0
  for (const x of window) total += x
  return total / window.length / (budgetUsd / 1440)
}

const RANK: Record<Level, number> = { GREEN: 0, AMBER: 1, RED: 2 }

export const worst = (levels: readonly Level[]): Level => levels.reduce<Level>((a, b) => (RANK[b] > RANK[a] ? b : a), 'GREEN')

/** GREEN/AMBER/RED with enter_streak 2, exit_streak 3 over 1-minute windows. */
export function levelStep(s: LevelState, observed: Level, enter = 2, exit = 3): LevelState {
  if (RANK[observed] > RANK[s.level]) {
    const up = s.up + 1
    return up >= enter ? { level: observed, up: 0, down: 0 } : { level: s.level, up, down: 0 }
  }
  if (RANK[observed] < RANK[s.level]) {
    const down = s.down + 1
    return down >= exit ? { level: observed, up: 0, down: 0 } : { level: s.level, up: 0, down }
  }
  return { level: s.level, up: 0, down: 0 }
}

export function limitLevel(percentUsed: number | null): Level {
  if (percentUsed === null) return 'GREEN'
  return percentUsed >= 90 ? 'RED' : percentUsed >= 70 ? 'AMBER' : 'GREEN'
}

/** Routing SLI burn per cell: (1 − pass/total)/(1 − target). */
export const routingBurn = (pass: number, total: number, target = 0.9): number | null => (total > 0 ? (1 - pass / total) / (1 - target) : null)

export const BREAKER_THRESHOLD = 3
export const BREAKER_COOL_MS = 600_000

export const breakerNew = (): Breaker => ({ fails: 0, openedAt: null })

export function breakerState(b: Breaker, now: number, coolMs = BREAKER_COOL_MS): BreakerState {
  if (b.openedAt === null) return 'closed'
  return now - b.openedAt < coolMs ? 'open' : 'half-open'
}

/** Open after `threshold` consecutive failures; a success (the half-open probe included) resets. */
export function breakerRecord(b: Breaker, now: number, ok: boolean, threshold = BREAKER_THRESHOLD): Breaker {
  if (ok) return { fails: 0, openedAt: null }
  const fails = b.fails + 1
  return { fails, openedAt: fails >= threshold ? now : b.openedAt }
}

export const bucketNew = (rate: number, burst: number, t = 0): TokenBucket => ({ rate, burst, tokens: burst, t })

export function bucketTake(b: TokenBucket, now: number, cost = 1): { allowed: boolean; bucket: TokenBucket } {
  const tokens = Math.min(b.burst, b.tokens + (now - b.t) * b.rate)
  return tokens >= cost
    ? { allowed: true, bucket: { ...b, tokens: tokens - cost, t: now } }
    : { allowed: false, bucket: { ...b, tokens, t: now } }
}

/** Heavy spawns per minute the rate-limit window can sustain (burst 2 set by the caller). */
export function admissionRate(spawnsPerMin: number, slopePctPerMin: number, headroomPct: number, minutesToReset: number): number {
  if (slopePctPerMin <= 0 || minutesToReset <= 0) return 10
  return Math.min(10, Math.max(0.2, (spawnsPerMin * (headroomPct / minutesToReset)) / slopePctPerMin))
}

/** P(any of n parallel calls is slow) = 1 − (1 − p)^n; shown when n ≥ 3. */
export const fanoutRisk = (p: number, n: number): number => 1 - (1 - p) ** n

export const availability = (mtbf: number, mttr: number): number => (mtbf + mttr > 0 ? mtbf / (mtbf + mttr) : 1)

export const series = (...parts: readonly number[]): number => parts.reduce((x, y) => x * y, 1)

export const drainEta = (backlog: number, cap: number, arrive: number): number | null => (cap > arrive ? backlog / (cap - arrive) : null)
```

- [ ] **Step 4: Run the tests**

Run: `claude plugin test plugin`
Expected: `35 pass`, `0 fail`.

- [ ] **Step 5: Full suite, validate, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1758 passed, 4 skipped`; `claude plugin validate plugin` → passed.

```bash
git add plugin/hooks/signals.ts plugin/tests/signals.test.ts
git commit -m "feat(plugin): §7 signals — burn rate, hysteresis, breaker, admission, fan-out, availability"
```

---

### Task 11: `tiers.ts` and `evidence.ts` — cells to advice, rows and views

**Files:**
- Create: `plugin/hooks/tiers.ts`, `plugin/hooks/evidence.ts`, `plugin/tests/evidence.test.ts`

**Interfaces:**
- Consumes: `cellNew`, `cellObserve`, `MIN_N`, `TARGET` (Task 8); `wilson` (Task 7); contract types.
- Produces:
  - `tiers.ts`: `TIERS`, `tierOf(name): Tier | null` (mirror of `route_log.tier_of`: first of haiku, sonnet, opus, fable contained in the lower-cased name), `rankOf(t)`, `tiersBelow(t): Tier[]` (cheapest first).
  - `evidence.ts`: `Stats = Record<string, Record<string, Welford>>`, `Advice = { tier; effort: null; confidence: 'COLD' | 'WARMING' | 'READY'; own: boolean; basis: string }`, `EvidenceRow = { taskType; tier; n; passPct; tokMean; state }`, `SHED_TASKS`, `LEVELS`, `cellKey(taskType, level, tier)`, `parseKey(key)`, `wilsonLo(pass, n)`, `tokMean(stats, key)`, `kTok(t)`, `basisOf(requested, tier, cell, level, tok)`, `adviceFor(cells, stats, taskType, level, requested): Advice | null`, `recordOutcome(cells, key, passed): { before, after }`, `cellViews(cells, stats): CellView[]`, `evidenceRows(cells, stats, level): EvidenceRow[]`, `asCells(v): Record<string, Cell>`, `asStats(v): Stats`.

Cell key = `task_type|LEVEL|tier`: the evidence "this task type, under this pressure, on this tier, passed". `effort` is always `null` in v1 (no effort labels exist; spec §5 lists it in Advice — kept as a field, never invented). Advice order: (1) cheapest lower tier whose OWN cell is READY → `confidence READY, own true` (the only advice enforce may apply); (2) a COLD/absent own cell inherits its parents' evidence (task_type across levels, then all task types) → `WARMING, own false`; (3) AMBER/RED shedding for explore/mechanical one tier down → the shed cell's state, `own false`; (4) else no advice.

- [ ] **Step 1: Write the failing tests**

`plugin/tests/evidence.test.ts`:

```ts
import { describe, expect, test } from 'claude-code/testing'

import type { Cell, Level, Tier } from '../types/index.d.ts'
import { welfordNew, welfordPush } from '../hooks/core/stats.ts'
import {
  adviceFor, asCells, asStats, cellKey, cellViews, evidenceRows, parseKey, recordOutcome, type Stats,
} from '../hooks/evidence.ts'
import { tierOf, tiersBelow } from '../hooks/tiers.ts'

function feed(cells: Record<string, Cell>, task: string, level: Level, tier: Tier, passes: number, fails = 0): string {
  const key = cellKey(task, level, tier)
  for (let i = 0; i < passes; i++) recordOutcome(cells, key, true)
  for (let i = 0; i < fails; i++) recordOutcome(cells, key, false)
  return key
}

function tokens(stats: Stats, key: string, mean: number): void {
  stats[key] = { tokens: welfordPush(welfordNew(), mean) }
}

describe('tiers', () => {
  test('tierOf mirrors route_log.tier_of', () => {
    expect(tierOf('claude-sonnet-5-5')).toBe('sonnet')
    expect(tierOf('OPUS')).toBe('opus')
    expect(tierOf('gpt-5')).toBeNull()
    expect(tierOf(undefined)).toBeNull()
    expect(tiersBelow('opus')).toEqual(['haiku', 'sonnet'])
    expect(tiersBelow('haiku')).toEqual([])
  })
})

describe('evidence', () => {
  test('keys round-trip; malformed keys are rejected', () => {
    expect(parseKey(cellKey('explore', 'AMBER', 'sonnet'))).toEqual({ taskType: 'explore', level: 'AMBER', tier: 'sonnet' })
    expect(parseKey('explore|PURPLE|sonnet')).toBeNull()
    expect(parseKey('explore|GREEN')).toBeNull()
  })

  test('no evidence, no advice', () => {
    expect(adviceFor({}, {}, 'explore', 'GREEN', 'opus')).toBeNull()
    expect(adviceFor({}, {}, 'explore', 'GREEN', null)).toBeNull()
  })

  test('an own READY cell advises its tier with a one-clause basis', () => {
    const cells: Record<string, Cell> = {}
    const stats: Stats = {}
    const key = feed(cells, 'explore', 'GREEN', 'sonnet', 35)
    tokens(stats, key, 41000)
    expect(adviceFor(cells, stats, 'explore', 'GREEN', 'opus')).toEqual({
      tier: 'sonnet', effort: null, confidence: 'READY', own: true, basis: 'opus→sonnet: 35/35 pass, 41k tok; GREEN now',
    })
  })

  test('the cheapest READY tier wins', () => {
    const cells: Record<string, Cell> = {}
    feed(cells, 'explore', 'GREEN', 'sonnet', 35)
    feed(cells, 'explore', 'GREEN', 'haiku', 35)
    expect(adviceFor(cells, {}, 'explore', 'GREEN', 'opus')?.tier).toBe('haiku')
  })

  test('a COLD cell inherits from the same task type at other levels — advice only', () => {
    const cells: Record<string, Cell> = {}
    feed(cells, 'explore', 'AMBER', 'sonnet', 35)
    const a = adviceFor(cells, {}, 'explore', 'GREEN', 'opus')
    expect(a?.tier).toBe('sonnet')
    expect(a?.confidence).toBe('WARMING')
    expect(a?.own).toBe(false)
    expect(a?.basis).toBe('opus→sonnet: inherited from explore all levels, 35/35 pass')
  })

  test('a DRIFTING own cell is neither advised nor allowed to inherit', () => {
    const cells: Record<string, Cell> = {}
    feed(cells, 'review', 'AMBER', 'sonnet', 35)
    const key = feed(cells, 'review', 'GREEN', 'sonnet', 40)
    for (let i = 0; i < 20; i++) recordOutcome(cells, key, i % 10 < 8)
    expect(cells[key]?.state).toBe('DRIFTING')
    expect(adviceFor(cells, {}, 'review', 'GREEN', 'opus')).toBeNull()
  })

  test('AMBER sheds explore one tier down as COLD advice; GREEN and review do not shed', () => {
    expect(adviceFor({}, {}, 'explore', 'AMBER', 'opus')).toEqual({
      tier: 'sonnet', effort: null, confidence: 'COLD', own: false, basis: 'AMBER shed: opus→sonnet',
    })
    expect(adviceFor({}, {}, 'explore', 'GREEN', 'opus')).toBeNull()
    expect(adviceFor({}, {}, 'review', 'AMBER', 'opus')).toBeNull()
    expect(adviceFor({}, {}, 'explore', 'RED', 'haiku')).toBeNull()
  })

  test('evidence rows: COLD omitted, cheapest READY chosen, WARMING shown with n', () => {
    const cells: Record<string, Cell> = {}
    const stats: Stats = {}
    tokens(stats, feed(cells, 'explore', 'GREEN', 'sonnet', 35), 41000)
    feed(cells, 'review', 'GREEN', 'opus', 12)
    feed(cells, 'debug', 'AMBER', 'sonnet', 5)
    expect(evidenceRows(cells, stats, 'GREEN')).toEqual([
      { taskType: 'explore', tier: 'sonnet', n: 35, passPct: 100, tokMean: 41000, state: 'READY' },
      { taskType: 'review', tier: 'opus', n: 12, passPct: 100, tokMean: null, state: 'WARMING' },
    ])
  })

  test('cell views are sorted and carry the Wilson lower bound', () => {
    const cells: Record<string, Cell> = {}
    feed(cells, 'review', 'GREEN', 'opus', 3)
    feed(cells, 'explore', 'GREEN', 'sonnet', 35)
    const views = cellViews(cells, {})
    expect(views.map(v => v.key)).toEqual(['explore|GREEN|sonnet', 'review|GREEN|opus'])
    expect(views[0]!.wilsonLo).toBeGreaterThan(0.9)
  })

  test('malformed persisted cells and stats are dropped entry by entry', () => {
    const good = asCells({ 'explore|GREEN|sonnet': { n: 3, pass: 3, state: 'WARMING', winN: 3, winPass: 3, below: 0, above: 0, cusum: 0, p0: null } })
    expect(Object.keys(good)).toEqual(['explore|GREEN|sonnet'])
    expect(asCells({ 'bad key': { n: 1 }, 'review|GREEN|opus': { n: 2, pass: 5, state: 'WARMING' } })).toEqual({})
    expect(asCells('nonsense')).toEqual({})
    expect(asStats({ k: { tokens: { n: 2, mean: 10, m2: 1 }, broken: { n: 'x' } } })).toEqual({ k: { tokens: { n: 2, mean: 10, m2: 1 } } })
    expect(asStats(null)).toEqual({})
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/evidence.ts` cannot be resolved.

- [ ] **Step 3: Write `tiers.ts` and `evidence.ts`**

`plugin/hooks/tiers.ts`:

```ts
import type { Tier } from '../types/index.d.ts'

/** Frontier tier order (route_log.TIER_RANK): a strictly higher rank is an escalation. */
export const TIERS: readonly Tier[] = ['haiku', 'sonnet', 'opus', 'fable']

export function tierOf(name: string | null | undefined): Tier | null {
  if (typeof name !== 'string') return null
  const low = name.toLowerCase()
  for (const t of TIERS) if (low.includes(t)) return t
  return null
}

export const rankOf = (t: Tier): number => TIERS.indexOf(t)

export const tiersBelow = (t: Tier): Tier[] => TIERS.slice(0, rankOf(t))
```

`plugin/hooks/evidence.ts`:

```ts
// §5 evidence: cells (task_type × pressure × tier) → advice {tier, effort, confidence, basis}.
import type { Cell, CellState, CellView, Level, Tier, Welford } from '../types/index.d.ts'
import { cellNew, cellObserve, MIN_N, TARGET } from './core/routing.ts'
import { wilson } from './core/stats.ts'
import { rankOf, TIERS, tiersBelow } from './tiers.ts'

export type Stats = Record<string, Record<string, Welford>>
export type Advice = { tier: Tier; effort: null; confidence: 'COLD' | 'WARMING' | 'READY'; own: boolean; basis: string }
export type EvidenceRow = { taskType: string; tier: Tier; n: number; passPct: number; tokMean: number | null; state: CellState }

export const SHED_TASKS: ReadonlySet<string> = new Set(['explore', 'mechanical'])
export const LEVELS: readonly Level[] = ['GREEN', 'AMBER', 'RED']
const STATES: readonly CellState[] = ['COLD', 'WARMING', 'READY', 'DRIFTING']

export const cellKey = (taskType: string, level: Level, tier: Tier): string => `${taskType}|${level}|${tier}`

export function parseKey(key: string): { taskType: string; level: Level; tier: Tier } | null {
  const [taskType, level, tier, extra] = key.split('|')
  if (!taskType || extra !== undefined || !LEVELS.includes(level as Level) || !TIERS.includes(tier as Tier)) return null
  return { taskType, level: level as Level, tier: tier as Tier }
}

export const wilsonLo = (pass: number, n: number): number => (n > 0 ? wilson(pass, n)[0] : 0)

export function tokMean(stats: Stats, key: string): number | null {
  const w = stats[key]?.tokens
  return w !== undefined && w.n > 0 ? w.mean : null
}

export const kTok = (t: number | null): string => (t === null ? '—' : `${Math.round(t / 1000)}k`)

export function basisOf(requested: Tier, tier: Tier, c: Cell, level: Level, tok: number | null): string {
  return `${requested}→${tier}: ${c.pass}/${c.n} pass${tok === null ? '' : `, ${kTok(tok)} tok`}; ${level} now`
}

function aggregate(cells: Record<string, Cell>, match: (p: { taskType: string; level: Level; tier: Tier }) => boolean): { n: number; pass: number } {
  let n = 0
  let pass = 0
  for (const [key, c] of Object.entries(cells)) {
    const p = parseKey(key)
    if (p !== null && match(p)) {
      n += c.n
      pass += c.pass
    }
  }
  return { n, pass }
}

export function adviceFor(cells: Record<string, Cell>, stats: Stats, taskType: string, level: Level, requested: Tier | null): Advice | null {
  if (requested === null) return null
  const lower = tiersBelow(requested)
  for (const t of lower) {
    const key = cellKey(taskType, level, t)
    const c = cells[key]
    if (c?.state === 'READY') return { tier: t, effort: null, confidence: 'READY', own: true, basis: basisOf(requested, t, c, level, tokMean(stats, key)) }
  }
  for (const t of lower) {
    const own = cells[cellKey(taskType, level, t)]
    if (own !== undefined && own.state !== 'COLD') continue
    const parents: [string, (p: { taskType: string; tier: Tier }) => boolean][] = [
      [`${taskType} all levels`, p => p.taskType === taskType && p.tier === t],
      ['all tasks', p => p.tier === t],
    ]
    for (const [scope, match] of parents) {
      const agg = aggregate(cells, match)
      if (agg.n >= MIN_N && wilsonLo(agg.pass, agg.n) >= TARGET) {
        return { tier: t, effort: null, confidence: 'WARMING', own: false, basis: `${requested}→${t}: inherited from ${scope}, ${agg.pass}/${agg.n} pass` }
      }
    }
  }
  if (level !== 'GREEN' && SHED_TASKS.has(taskType) && lower.length > 0) {
    const t = lower[lower.length - 1]!
    const c = cells[cellKey(taskType, level, t)]
    const confidence = c === undefined || c.state === 'COLD' ? 'COLD' : 'WARMING'
    return { tier: t, effort: null, confidence, own: false, basis: `${level} shed: ${requested}→${t}${c ? ` (${c.n}/${MIN_N})` : ''}` }
  }
  return null
}

export function recordOutcome(cells: Record<string, Cell>, key: string, passed: boolean): { before: CellState; after: CellState } {
  const prev = cells[key] ?? cellNew()
  const next = cellObserve(prev, passed)
  cells[key] = next
  return { before: prev.state, after: next.state }
}

export function cellViews(cells: Record<string, Cell>, stats: Stats): CellView[] {
  const views: CellView[] = []
  for (const [key, c] of Object.entries(cells)) {
    const p = parseKey(key)
    if (p === null) continue
    views.push({ key, taskType: p.taskType, level: p.level, tier: p.tier, state: c.state, n: c.n, pass: c.pass, wilsonLo: wilsonLo(c.pass, c.n), tokMean: tokMean(stats, key) })
  }
  return views.sort(
    (a, b) => a.taskType.localeCompare(b.taskType) || LEVELS.indexOf(a.level) - LEVELS.indexOf(b.level) || rankOf(a.tier) - rankOf(b.tier),
  )
}

/** One row per task type at `level`: the cheapest READY tier, else the busiest non-COLD cell. COLD → no row. */
export function evidenceRows(cells: Record<string, Cell>, stats: Stats, level: Level): EvidenceRow[] {
  const best = new Map<string, CellView>()
  for (const v of cellViews(cells, stats)) {
    if (v.level !== level || v.state === 'COLD') continue
    const cur = best.get(v.taskType)
    const better =
      cur === undefined ||
      (v.state === 'READY' && (cur.state !== 'READY' || rankOf(v.tier) < rankOf(cur.tier))) ||
      (v.state !== 'READY' && cur.state !== 'READY' && v.n > cur.n)
    if (better) best.set(v.taskType, v)
  }
  return [...best.values()].map(v => ({
    taskType: v.taskType,
    tier: v.tier,
    n: v.n,
    passPct: v.n > 0 ? Math.round((100 * v.pass) / v.n) : 0,
    tokMean: v.tokMean,
    state: v.state,
  }))
}

const isCount = (x: unknown): x is number => typeof x === 'number' && Number.isInteger(x) && x >= 0
const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x)

export function asCells(v: unknown): Record<string, Cell> {
  const out: Record<string, Cell> = {}
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return out
  for (const [key, raw] of Object.entries(v as Record<string, unknown>)) {
    if (parseKey(key) === null || raw === null || typeof raw !== 'object') continue
    const c = raw as Record<string, unknown>
    const ok =
      isCount(c.n) && isCount(c.pass) && c.pass <= c.n && STATES.includes(c.state as CellState) &&
      isCount(c.winN) && isCount(c.winPass) && isCount(c.below) && isCount(c.above) && isNum(c.cusum) &&
      (c.p0 === null || isNum(c.p0))
    if (ok) out[key] = c as unknown as Cell
  }
  return out
}

export function asStats(v: unknown): Stats {
  const out: Stats = {}
  if (v === null || typeof v !== 'object' || Array.isArray(v)) return out
  for (const [key, metrics] of Object.entries(v as Record<string, unknown>)) {
    if (metrics === null || typeof metrics !== 'object') continue
    const kept: Record<string, Welford> = {}
    for (const [metric, w] of Object.entries(metrics as Record<string, unknown>)) {
      const r = w as Record<string, unknown> | null
      if (r !== null && typeof r === 'object' && isCount(r.n) && isNum(r.mean) && isNum(r.m2)) kept[metric] = { n: r.n, mean: r.mean, m2: r.m2 }
    }
    if (Object.keys(kept).length > 0) out[key] = kept
  }
  return out
}
```

- [ ] **Step 4: Run the tests**

Run: `claude plugin test plugin`
Expected: `46 pass`, `0 fail`.

- [ ] **Step 5: Full suite, validate, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1758 passed, 4 skipped`; `claude plugin validate plugin` → passed.

```bash
git add plugin/hooks/tiers.ts plugin/hooks/evidence.ts plugin/tests/evidence.test.ts
git commit -m "feat(plugin): evidence cells → advice with inheritance and pressure shedding"
```

---

### Task 12: `io.ts` and `observe.ts` — §4 capture, 5 s flush, 30-day retention, profile

**Files:**
- Create: `plugin/hooks/io.ts`, `plugin/hooks/observe.ts`, `plugin/tests/observe.test.ts`
- Modify: `plugin/hooks/register.ts` (wire `observe`)

**Interfaces:**
- Consumes: `Runtime`, `BashEvent` (Task 1), `measureOf` (Task 1), `welfordNew/welfordPush` (Task 7), `asStats`, `Stats` (Task 11), `tierOf` (Task 11), `EMPTY_PROFILE_STORE`, `profileView` (Task 1).
- Produces:
  - `io.ts`: `appendLines($, path, lines): Promise<boolean>` (one `/usr/bin/tee -a` per batch, O_APPEND), `ensureDir($, dir)`, `readText($, path): Promise<string | null>`, `readJson($, path): Promise<unknown>`, `tailLines($, path, n): Promise<string[]>`, `jsonLines(lines): Record<string, unknown>[]`.
  - `observe.ts`: `DESC_MAX = 120`, `RETENTION_DAYS = 30`, `FLUSH_MS = 5000`, `MAX_PENDING = 5000`, `ROW_MAX_BYTES = 2048`, `Row`, `dayOf(ms)`, `observeDir(backendDir)`, `observePath(backendDir, ms)`, `routeLogPath(backendDir)`, `spawnRow(e, taskType, resolved, ts)`, `stepRow(e, r, ttftMs, stepMs, ts)`, `measureRow(m, ts)`, `BACKEND_COMMANDS`, `SIGNALS`, `bashEventOf(command, output, isError, t): BashEvent | null`, `bashRow(b)`, `skillRow(skill, ts)`, `completeRow(agentId, ok, durationMs, tokens, ts)`, `outputOf(result): string`, `expiredFiles(names, now, days?)`, `record(rt, row)`, `pushStat(stats, key, metric, x)`, `noteStep(rt, e, r, ttftMs, stepMs, ts)`, `asProfile(v): ProfileStore`, `repoHash(cwd): Promise<string>`, `flushAll($, rt): Promise<void>`, and the register.ts entry points `start($, e, rt)`, `measure(rt, e, now)`, `skill(rt, name, now)`; `install(on, rt)` registers only `turn.step` and `tool.call` `{ tool: 'Bash' }`.

Storage per §4: rows go to `<backendDir>/observe/<YYYY-MM-DD>.jsonl` (UTC day), one JSON object per event, flushed every 5 s and on `session.end`; files older than 30 days are removed at `session.start`. Running stats (`datapce.stats`), cells (`datapce.cells`, written by the router) and the profile (`datapce.profile`) are persisted to `$.store` by the same flush, only when dirty. The `tool.call` hook (matcher `Bash`) awaits `next(e)` and returns its result untouched; the row keeps only which backend command ran, whether it errored, and which signal pattern matched — never the command or output text.

- [ ] **Step 1: Write the failing tests**

`plugin/tests/observe.test.ts`:

```ts
import { describe, expect, test } from 'claude-code/testing'

import {
  bashEventOf, expiredFiles, measureRow, observePath, record, spawnRow, ROW_MAX_BYTES,
} from '../hooks/observe.ts'
import { newRuntime, optionsOf } from '../hooks/runtime.ts'
import { MEASURE, SESSION, spawnInput, stepInput } from './fixtures/inputs.ts'
import { BACKEND, T0, drain, worldOf, type World } from './fixtures/world.ts'

const DAY = observePath(BACKEND, T0)
const rowsOf = (world: World, path = DAY): Record<string, unknown>[] => (world.appended.get(path) ?? []).map(l => JSON.parse(l))

describe('observe: pure rows', () => {
  test('spawn rows keep the dispatch label (≤ 120 chars), never the prompt', () => {
    const row = spawnRow(spawnInput({ description: 'd'.repeat(300), prompt: 'SECRET PROMPT' }), 'explore', 'claude-sonnet-5-5', 1)
    expect(String(row.description)).toHaveLength(120)
    expect(JSON.stringify(row)).not.toContain('SECRET')
    expect(row).toMatchObject({ ev: 'spawn', tool_use_id: 'toolu_1', task_type: 'explore', model_resolved: 'claude-sonnet-5-5' })
  })

  test('measure rows', () => {
    expect(measureRow({ ctxPercent: 61, limitKind: 'five_hour', limitPercent: 62, resetsAt: null, costUsd: 4.12 }, 7)).toEqual({
      ev: 'measure', ts: 7, ctx_pct: 61, limit_kind: 'five_hour', limit_pct: 62, resets_at: null, cost_usd: 4.12,
    })
  })

  test('backend commands and signal patterns are classified; anything else is ignored', () => {
    expect(bashEventOf('apex-router pressure --check', 'GREEN', false, 1)).toEqual({ cmd: 'pressure', signal: null, isError: false, t: 1 })
    expect(bashEventOf('apex-ornith review x', 'OrnithBusy: queue full', true, 2)).toEqual({ cmd: 'ornith', signal: 'OrnithBusy', isError: true, t: 2 })
    expect(bashEventOf('apex-router review-preread', 'timeout after 30s', false, 3)?.signal).toBe('timeout')
    expect(bashEventOf('apex-ornith gen', 'finish_reason=length', false, 4)?.signal).toBe('truncation')
    expect(bashEventOf('ls -la', 'OrnithBusy', false, 5)).toBeNull()
  })

  test('retention keeps 30 UTC days and ignores other files', () => {
    expect(expiredFiles(['2026-09-01.jsonl', '2026-09-03.jsonl', '2026-10-03.jsonl', '.keep', 'notes.txt'], T0)).toEqual(['2026-09-01.jsonl'])
  })

  test('an oversized row is replaced by a stub, never split', () => {
    const rt = newRuntime(optionsOf({}))
    record(rt, { ev: 'spawn', ts: 1, description: 'x'.repeat(5000) })
    expect(rt.rows[0]).toBe('{"ev":"spawn","ts":1,"truncated":true}')
    expect(new TextEncoder().encode(rt.rows[0]!).length).toBeLessThanOrEqual(ROW_MAX_BYTES)
  })
})

describe('observe: hooks', () => {
  test('session.start removes expired day files', async ($, on) => {
    const old = `${BACKEND}/observe/2026-09-01.jsonl`
    const kept = `${BACKEND}/observe/2026-09-20.jsonl`
    const world = worldOf(on, { [old]: '{}\n', [kept]: '{}\n' })
    await $.session.start(SESSION)
    expect(world.files.has(old)).toBe(false)
    expect(world.files.has(kept)).toBe(true)
  })

  test('session.start creates the observe directory when it is missing', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    expect(world.files.has(`${BACKEND}/observe/.keep`)).toBe(true)
  })

  test('a step and a measurement land as rows after the 5 s flush — counts only', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    world.step = { toolUses: [{ name: 'Agent', input: { prompt: 'SECRET PROMPT' } }] }
    await drain($.turn.step(stepInput()))
    await $.session.measure(MEASURE)
    expect(rowsOf(world)).toEqual([])
    await world.clock.advance(5000)
    const rows = rowsOf(world)
    expect(rows.map(r => r.ev)).toEqual(['step', 'measure'])
    expect(rows[0]).toMatchObject({ model: 'claude-opus-5-5', output: 50, cache_read: 1000, stop: 'end_turn', tools: ['Agent'] })
    expect(JSON.stringify(rows)).not.toContain('SECRET')
  })

  test('Bash backend commands become signal rows; results pass through untouched', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    world.bashOutput = 'error: OrnithBusy (queue full)'
    const r = await $.tool.call({ tool: 'Bash', command: 'apex-router pressure --check --token=SECRET' } as never)
    expect((r as { result?: unknown }).result).toEqual({ stdout: 'error: OrnithBusy (queue full)', stderr: '' })
    await $.tool.call({ tool: 'Bash', command: 'ls -la' } as never)
    await world.clock.advance(5000)
    expect(rowsOf(world)).toEqual([{ ev: 'bash', ts: T0, cmd: 'pressure', signal: 'OrnithBusy', error: false }])
    expect(JSON.stringify(rowsOf(world))).not.toContain('SECRET')
  })

  test('skills in use are counted in the profile', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.skill.prompt({ skill: 'superpowers:writing-plans', text: 'PLAN' })
    await world.clock.advance(5000)
    const p = world.store.get('datapce.profile') as { skills: Record<string, number>; repos: string[] }
    expect(p.skills['superpowers:writing-plans']).toBe(1)
    expect(p.repos).toHaveLength(1)
    expect(p.repos[0]).toMatch(/^[0-9a-f]{16}$/)
  })

  test('a failed append keeps the rows and retries on the next flush', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    world.answer = argv => (argv[0] === '/usr/bin/tee' ? { exitCode: 1, stdout: '' } : null)
    await drain($.turn.step(stepInput()))
    await world.clock.advance(5000)
    expect(rowsOf(world)).toEqual([])
    world.answer = () => null
    await world.clock.advance(5000)
    expect(rowsOf(world).map(r => r.ev)).toEqual(['step'])
  })

  test('malformed persisted values are dropped, not fatal', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.stats', 'garbage')
    world.store.set('datapce.profile', { repos: 5, hours: 'x', skills: { a: 'b' } })
    await $.session.start(SESSION)
    await drain($.turn.step(stepInput()))
    await world.clock.advance(5000)
    const p = world.store.get('datapce.profile') as { repos: string[]; hours: number[]; skills: Record<string, number> }
    expect(p.repos).toHaveLength(1)
    expect(p.hours).toHaveLength(24)
    expect(p.skills).toEqual({})
    expect(Object.keys(world.store.get('datapce.stats') as object)).toEqual(['step|opus'])
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/observe.ts` cannot be resolved.

- [ ] **Step 3: Write `io.ts`**

`plugin/hooks/io.ts`:

```ts
// File and process helpers over the Host. Every call fails open: a write that cannot happen returns
// false or null, never throws into a hook. Appends go through tee -a (O_APPEND): $.fs has no append.
import type { Host } from './host.ts'

export async function appendLines(host: Host, path: string, lines: readonly string[]): Promise<boolean> {
  if (lines.length === 0) return true
  try {
    const r = await host.run(['/usr/bin/tee', '-a', path], { stdin: `${lines.join('\n')}\n`, timeoutMs: 5000 })
    return r.exitCode === 0
  } catch {
    return false
  }
}

export async function ensureDir(host: Host, dir: string): Promise<void> {
  try {
    if (!(await host.exists(dir))) await host.write(`${dir}/.keep`, '')
  } catch {
    // fail open: the next append reports the failure and keeps its rows
  }
}

export async function readText(host: Host, path: string): Promise<string | null> {
  try {
    return await host.read(path)
  } catch {
    return null
  }
}

export async function readJson(host: Host, path: string): Promise<unknown> {
  const text = await readText(host, path)
  if (text === null) return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    return null
  }
}

export async function tailLines(host: Host, path: string, n: number): Promise<string[]> {
  try {
    const r = await host.run(['/usr/bin/tail', '-n', String(n), path], { timeoutMs: 5000 })
    return r.exitCode === 0 ? r.stdout.split('\n').filter(l => l !== '') : []
  } catch {
    return []
  }
}

export function jsonLines(lines: readonly string[]): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = []
  for (const line of lines) {
    try {
      const v = JSON.parse(line) as unknown
      if (v !== null && typeof v === 'object' && !Array.isArray(v)) out.push(v as Record<string, unknown>)
    } catch {
      // a torn or foreign line is skipped
    }
  }
  return out
}
```

- [ ] **Step 4: Write `observe.ts`**

`plugin/hooks/observe.ts`:

```ts
// §4 observation: counts and identifiers only — never prompt text, file contents or command text.
import type { AgentSpawnInput, On, SessionMeasureInput, SessionStartInput, TurnStepInput, TurnStepResult } from 'claude-code'

import type { Measure, ProfileStore } from '../types/index.d.ts'
import { measureOf } from './band.tsx'
import { welfordNew, welfordPush } from './core/stats.ts'
import { asStats, type Stats } from './evidence.ts'
import type { Host } from './host.ts'
import { appendLines, ensureDir } from './io.ts'
import type { BashEvent, Runtime } from './runtime.ts'
import { EMPTY_PROFILE_STORE, profileView } from './state.ts'
import { tierOf } from './tiers.ts'

export const DESC_MAX = 120
export const RETENTION_DAYS = 30
export const FLUSH_MS = 5_000
export const MAX_PENDING = 5_000
export const ROW_MAX_BYTES = 2_048
const LIMIT_HISTORY_MS = 15 * 60_000

export type Row = Record<string, unknown>

export const dayOf = (ms: number): string => new Date(ms).toISOString().slice(0, 10)
export const observeDir = (backendDir: string): string => `${backendDir}/observe`
export const observePath = (backendDir: string, ms: number): string => `${observeDir(backendDir)}/${dayOf(ms)}.jsonl`
export const routeLogPath = (backendDir: string): string => `${backendDir}/route_log.jsonl`

export function spawnRow(
  e: Pick<AgentSpawnInput, 'tool_use_id' | 'subagentType' | 'description' | 'model' | 'parentAgentId'>,
  taskType: string,
  resolved: string | null,
  ts: number,
): Row {
  return {
    ev: 'spawn',
    ts,
    tool_use_id: e.tool_use_id,
    subagent_type: e.subagentType,
    description: e.description.slice(0, DESC_MAX),
    task_type: taskType,
    model_requested: e.model ?? null,
    model_resolved: resolved,
    parent_agent_id: e.parentAgentId ?? null,
  }
}

export function stepRow(e: Pick<TurnStepInput, 'model' | 'effort' | 'agentId'>, r: TurnStepResult, ttftMs: number | null, stepMs: number, ts: number): Row {
  return {
    ev: 'step',
    ts,
    agent_id: e.agentId ?? null,
    model: e.model,
    effort: e.effort ?? null,
    input: r.usage?.input_tokens ?? null,
    output: r.usage?.output_tokens ?? null,
    cache_read: r.usage?.cache_read_input_tokens ?? null,
    cache_write: r.usage?.cache_creation_input_tokens ?? null,
    stop: r.stopReason,
    tools: r.toolUses.map(t => t.name),
    ttft_ms: ttftMs === null ? null : Math.round(ttftMs),
    step_ms: Math.round(stepMs),
  }
}

export const measureRow = (m: Measure, ts: number): Row => ({
  ev: 'measure',
  ts,
  ctx_pct: m.ctxPercent,
  limit_kind: m.limitKind,
  limit_pct: m.limitPercent,
  resets_at: m.resetsAt,
  cost_usd: m.costUsd,
})

export const BACKEND_COMMANDS: readonly (readonly [RegExp, string])[] = [
  [/\bapex-router\s+pressure\b/, 'pressure'],
  [/\breview-preread\b/, 'review-preread'],
  [/\bapex-ornith\b/, 'ornith'],
]

export const SIGNALS: readonly (readonly [RegExp, string])[] = [
  [/OrnithBusy/, 'OrnithBusy'],
  [/timeout after/, 'timeout'],
  [/finish_reason=length/, 'truncation'],
]

export function bashEventOf(command: string, output: string, isError: boolean, t: number): BashEvent | null {
  const cmd = BACKEND_COMMANDS.find(([re]) => re.test(command))?.[1]
  if (cmd === undefined) return null
  const signal = SIGNALS.find(([re]) => re.test(output))?.[1] ?? null
  return { cmd, signal, isError, t }
}

export const bashRow = (b: BashEvent): Row => ({ ev: 'bash', ts: b.t, cmd: b.cmd, signal: b.signal, error: b.isError })
export const skillRow = (skill: string, ts: number): Row => ({ ev: 'skill', ts, skill })
export const completeRow = (agentId: string, ok: boolean, durationMs: number, tokens: number | null, ts: number): Row => ({
  ev: 'complete',
  ts,
  agent_id: agentId,
  ok,
  duration_ms: durationMs,
  tokens,
})

/** The text a tool result carries: core's `text`, else a hook's `{ result: { stdout, stderr } }`. */
export function outputOf(r: unknown): string {
  if (r === null || typeof r !== 'object') return ''
  const o = r as { text?: unknown; result?: unknown }
  if (typeof o.text === 'string') return o.text
  if (o.result !== null && typeof o.result === 'object') {
    const res = o.result as { stdout?: unknown; stderr?: unknown }
    return `${typeof res.stdout === 'string' ? res.stdout : ''}${typeof res.stderr === 'string' ? res.stderr : ''}`
  }
  return typeof o.result === 'string' ? o.result : ''
}

export function expiredFiles(names: readonly string[], now: number, days = RETENTION_DAYS): string[] {
  const cutoff = dayOf(now - days * 86_400_000)
  return names.filter(n => /^\d{4}-\d{2}-\d{2}\.jsonl$/.test(n) && n.slice(0, 10) < cutoff)
}

const bytes = (s: string): number => new TextEncoder().encode(s).length

export function record(rt: Runtime, row: Row): void {
  let line = JSON.stringify(row)
  if (bytes(line) > ROW_MAX_BYTES) line = JSON.stringify({ ev: row.ev, ts: row.ts, truncated: true })
  rt.rows.push(line)
  if (rt.rows.length > MAX_PENDING) rt.rows.splice(0, rt.rows.length - MAX_PENDING)
}

export function pushStat(stats: Stats, key: string, metric: string, x: number | null | undefined): void {
  if (typeof x !== 'number' || !Number.isFinite(x)) return
  const m = (stats[key] ??= {})
  m[metric] = welfordPush(m[metric] ?? welfordNew(), x)
}

export function noteStep(rt: Runtime, e: TurnStepInput, r: TurnStepResult, ttftMs: number | null, stepMs: number, ts: number): void {
  record(rt, stepRow(e, r, ttftMs, stepMs, ts))
  rt.minute.steps += 1
  if (r.stopReason === null) rt.minute.failed += 1
  const tier = tierOf(r.usage?.model ?? e.model) ?? 'other'
  pushStat(rt.stats, `step|${tier}`, 'output', r.usage?.output_tokens)
  pushStat(rt.stats, `step|${tier}`, 'cache_read', r.usage?.cache_read_input_tokens)
  rt.statsDirty = true
  if (e.agentId === undefined && r.usage !== null) {
    rt.cacheRead += r.usage.cache_read_input_tokens
    rt.stepFeatures.push([r.usage.cache_read_input_tokens, r.usage.output_tokens, ttftMs ?? stepMs, r.toolUses.length, stepMs])
    if (rt.stepFeatures.length > 500) rt.stepFeatures.splice(0, rt.stepFeatures.length - 500)
  }
}

const isCount = (x: unknown): x is number => typeof x === 'number' && Number.isInteger(x) && x >= 0

function counts(v: unknown): Record<string, number> {
  const out: Record<string, number> = {}
  if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
    for (const [k, n] of Object.entries(v as Record<string, unknown>)) if (isCount(n)) out[k] = n
  }
  return out
}

export function asProfile(v: unknown): ProfileStore {
  const p = v !== null && typeof v === 'object' ? (v as Record<string, unknown>) : {}
  const repos = Array.isArray(p.repos) ? p.repos.filter((r): r is string => typeof r === 'string').slice(-200) : []
  const hours = Array.isArray(p.hours) && p.hours.length === 24 && p.hours.every(isCount) ? (p.hours as number[]) : EMPTY_PROFILE_STORE.hours
  return { repos, taskMix: counts(p.taskMix), skills: counts(p.skills), hours, backend: p.backend === true }
}

/** A path hash: the profile counts repos without recording where they are. */
export async function repoHash(cwd: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(cwd))
  return [...new Uint8Array(digest)]
    .slice(0, 8)
    .map(b => b.toString(16).padStart(2, '0'))
    .join('')
}

function keep(into: string[], rows: string[]): void {
  into.unshift(...rows)
  if (into.length > MAX_PENDING) into.splice(0, into.length - MAX_PENDING)
}

export async function flushAll(host: Host, rt: Runtime): Promise<void> {
  const now = await host.now()
  const rows = rt.rows.splice(0)
  if (rows.length > 0 && !(await appendLines(host, observePath(rt.backendDir, now), rows))) keep(rt.rows, rows)
  const route = rt.routeRows.splice(0)
  if (route.length > 0 && !(await appendLines(host, routeLogPath(rt.backendDir), route))) keep(rt.routeRows, route)
  try {
    if (rt.statsDirty) {
      rt.statsDirty = false
      await host.storeSet('datapce.stats', rt.stats)
    }
    if (rt.cellsDirty) {
      rt.cellsDirty = false
      await host.storeSet('datapce.cells', rt.cells)
    }
    if (rt.profileDirty) {
      rt.profileDirty = false
      await host.storeSet('datapce.profile', rt.profile)
      await host.publish.profile(profileView(rt.profile))
    }
  } catch {
    // fail open: the next change marks the value dirty again
  }
}

/** session.start (from register.ts, after identify): directory, retention, persisted state, flush timer. */
export async function start(host: Host, e: SessionStartInput, rt: Runtime): Promise<void> {
  try {
    const dir = observeDir(rt.backendDir)
    await ensureDir(host, dir)
    const old = expiredFiles((await host.list(dir)).map(entry => entry.name), await host.now())
    if (old.length > 0) await host.run(['/bin/rm', '-f', ...old.map(n => `${dir}/${n}`)], { timeoutMs: 5000 })
  } catch {
    // retention is best effort
  }
  try {
    rt.stats = asStats(await host.storeGet('datapce.stats'))
    const p = asProfile(await host.storeGet('datapce.profile'))
    const hash = await repoHash(e.cwd)
    rt.profile = p.repos.includes(hash) ? p : { ...p, repos: [...p.repos, hash].slice(-200) }
    rt.profileDirty = true
  } catch {
    // a store that cannot be read starts empty
  }
  host.every(FLUSH_MS, () => {
    void flushAll(host, rt)
  })
}

/** session.measure (from register.ts): the row, the spend delta, the rate-limit sample. */
export function measure(rt: Runtime, e: SessionMeasureInput, now: number): void {
  try {
    const m = measureOf(e)
    record(rt, measureRow(m, now))
    if (m.costUsd !== null) {
      if (rt.lastCostUsd !== null) rt.minute.spend += Math.max(0, m.costUsd - rt.lastCostUsd)
      rt.lastCostUsd = m.costUsd
    }
    if (m.limitPercent !== null) {
      rt.limitHistory.push({ t: now, pct: m.limitPercent })
      rt.limitHistory = rt.limitHistory.filter(s => now - s.t <= LIMIT_HISTORY_MS)
    }
  } catch {
    // observation never breaks a measurement
  }
}

/** skill.prompt (from register.ts): which workflows are in use on this machine. */
export function skill(rt: Runtime, name: string, now: number): void {
  try {
    record(rt, skillRow(name, now))
    rt.profile = { ...rt.profile, skills: { ...rt.profile.skills, [name]: (rt.profile.skills[name] ?? 0) + 1 } }
    rt.profileDirty = true
  } catch {
    // observation never breaks a skill
  }
}

/** Events only observe hooks: turn.step (streaming) and tool.call on Bash. */
export function install(on: On, rt: Runtime): void {
  on('turn.step', async function* ($, e, next) {
    const t0 = performance.now()
    let ttft: number | null = null
    const stream = next(e)
    for await (const chunk of stream) {
      if (ttft === null) ttft = performance.now() - t0
      yield chunk
    }
    const result = await stream.result
    try {
      noteStep(rt, e, result, ttft, performance.now() - t0, await $.clock.now())
    } catch {
      // observation never breaks a turn
    }
    return result
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const r = await next(e)
    try {
      const command = e.tool === 'Bash' ? e.command : ''
      const ev = bashEventOf(command, outputOf(r), 'isError' in r && r.isError === true, await $.clock.now())
      if (ev !== null) {
        rt.bashEvents.push(ev)
        record(rt, bashRow(ev))
      }
    } catch {
      // never deny, never rewrite (§10)
    }
    return r
  })
}
```

`plugin/hooks/register.ts` — replace the import block (every line above `// Wiring only.`) with:

```ts
import type { EngineInterface, Register } from 'claude-code'

import { install as band, measureOf } from './band.tsx'
import type { Host } from './host.ts'
import {
  flushAll,
  install as observe,
  measure as observeMeasure,
  skill as observeSkill,
  start as observeStart,
} from './observe.ts'
import { identify, newRuntime, optionsOf } from './runtime.ts'
```

`plugin/hooks/register.ts` — replace everything from `export const register` to the end of the file with (the state refs and `hostOf` above it stay as they are):

```ts
export const register: Register = (on, raw) => {
  const rt = newRuntime(optionsOf(raw))

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await identify(host, e, rt)
    await observeStart(host, e, rt)
    return r
  })

  on('session.end', async ($, e, next) => {
    await flushAll(hostOf($), rt)
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const host = hostOf($)
    await host.publish.measure(measureOf(e))
    observeMeasure(rt, e, await host.now())
    return next(e)
  })

  on('skill.prompt', async ($, e, next) => {
    const r = await next(e)
    observeSkill(rt, e.skill, await hostOf($).now())
    return r
  })

  band(on, rt)
  observe(on, rt)
}
```

- [ ] **Step 5: Run the tests**

Run: `claude plugin test plugin`
Expected: `58 pass`, `0 fail`.

Run: `claude plugin validate plugin`
Expected: passed; `calls:` now include `$.fs.exists`, `$.fs.list`, `$.fs.write`, `$.process.run`, `$.store.get`, `$.store.set`, `$.clock.every`; hooks include `turn.step`, `tool.call{tool=Bash}`, `skill.prompt`, `session.end`.

- [ ] **Step 6: Full suite and commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1758 passed, 4 skipped`.

```bash
git add plugin/hooks/io.ts plugin/hooks/observe.ts plugin/hooks/register.ts plugin/tests/observe.test.ts
git commit -m "feat(plugin): observe — §4 rows, Welford stats, 5 s flush, retention, profile"
```

---

### Task 13: `router.ts` — `agent.spawn` advise/enforce, hard limits, route_log rows, completion labels

**Files:**
- Create: `plugin/hooks/router.ts`, `plugin/tests/router.test.ts`
- Modify: `plugin/hooks/register.ts` (imports; `session.start` calls `routerStart`; new `agent.spawn` and `turn.complete` hooks)

**Interfaces:**
- Consumes: `classifyDispatch`, `promptHeadOf` (Task 9); `adviceFor`, `asCells`, `cellKey`, `cellViews`, `recordOutcome`, `Advice`, `Stats` (Task 11); `tierOf` (Task 11); `bucketTake` (Task 10); `record`, `spawnRow`, `completeRow`, `pushStat`, `DESC_MAX` (Task 12); `toastOnce`, `Runtime` (Task 1); `Host` (Task 1).
- Produces: `SpawnContext`, `Decision = { taskType; requested; requestedTier; advice; apply: Tier | null; serialize: boolean; hardLimit: string | null }`, `hardLimitOf(e, requestedTier)`, `decideSpawn(e, ctx): Decision` (pure, no I/O — the §17 5 ms budget is measured on it in Task 19), `routeRow(e, d, resolved, agentId, rt, ts)`, `beforeSpawn(rt, e, now): Decision`, `afterSpawn(host, rt, e, d, res, now)`, `onComplete(host, rt, e, now)`, `start(host, rt)`.

Behaviour (spec §5, §17.5): advise mode (default) passes `e` to the engine unchanged. Enforce (userConfig `enforce`, or `/apex enforce on` persisted as `datapce.enforce`) calls `next({ ...e, model: advice.tier })` ONLY when the advice comes from the cell's own READY state and no hard limit applies; a hook answering `{ model }` without `next` would start nothing (types: `AgentSpawnResult`). Hard limits, never overridden: an explicit `model` on the Agent call (the model or the user named it), forks (they inherit), Fable requests, unknown tiers. A denied spawn under enforce counts against its cell immediately. Every resolved spawn appends one `route_log.jsonl` row in the existing schema (`label_pending: true`, `outcome: "async"`, `surface: "claude-code"`, optional fields strings only) so `route-join` / `route-advise` / `labeled_table` keep working; extra string fields `applied`, `advice_tier`, `advice_state`, `inject_arm`, `injected` feed the self A/B (Task 20). Completion: `turn.complete` carries the subagent's `agentId`; `reason === 'answer'` is a pass, anything else a fail, and the label goes to cell `task_type|level-at-spawn|tier-that-ran`. (Spec §4 suggested a `$.agent.list()` diff on `turn.step`; `turn.complete`'s `agentId` is the direct signal the types document, so it is used instead.)

Open question §15 ("Whether `Workflow` scripts expose per-agent model in a way `agent.spawn` sees"): Step 6 below verifies it manually; if Workflow agents do not raise `agent.spawn`, the plugin still guides them through `tool.describe` (Task 14).

- [ ] **Step 1: Write the failing tests**

`plugin/tests/router.test.ts`:

```ts
import { describe, expect, test } from 'claude-code/testing'

import type { Cell } from '../types/index.d.ts'
import { cellKey } from '../hooks/evidence.ts'
import { newRuntime, optionsOf } from '../hooks/runtime.ts'
import { decideSpawn, routeRow, type SpawnContext } from '../hooks/router.ts'
import { complete, SESSION, spawnInput } from './fixtures/inputs.ts'
import { BACKEND, worldOf, type World } from './fixtures/world.ts'

const READY: Cell = { n: 35, pass: 35, state: 'READY', winN: 5, winPass: 5, below: 0, above: 0, cusum: 0, p0: 1 }
const ALMOST: Cell = { n: 34, pass: 34, state: 'WARMING', winN: 4, winPass: 4, below: 0, above: 0, cusum: 0, p0: null }
const ctx = (over: Partial<SpawnContext> = {}): SpawnContext => ({
  level: 'GREEN', cells: { [cellKey('explore', 'GREEN', 'sonnet')]: READY }, stats: {}, enforce: false, admitted: true, ...over,
})
const routeRows = (world: World): Record<string, unknown>[] => (world.appended.get(`${BACKEND}/route_log.jsonl`) ?? []).map(l => JSON.parse(l))

describe('router: decisions', () => {
  test('advise mode never applies, even with a READY cell', () => {
    const d = decideSpawn(spawnInput(), ctx())
    expect(d.taskType).toBe('explore')
    expect(d.requested).toBe('inherit')
    expect(d.requestedTier).toBe('opus')
    expect(d.advice?.tier).toBe('sonnet')
    expect(d.apply).toBeNull()
  })

  test('enforce applies only an own READY cell', () => {
    expect(decideSpawn(spawnInput(), ctx({ enforce: true })).apply).toBe('sonnet')
    expect(decideSpawn(spawnInput(), ctx({ enforce: true, cells: {} })).apply).toBeNull()
  })

  test('hard limits: explicit model, fork, Fable, unknown tier', () => {
    const on = ctx({ enforce: true })
    expect(decideSpawn(spawnInput({ model: 'opus' }), on)).toMatchObject({ apply: null, hardLimit: 'explicit model', requested: 'opus' })
    expect(decideSpawn(spawnInput({ fork: true }), on).apply).toBeNull()
    expect(decideSpawn(spawnInput({ parentModel: 'claude-fable-5' }), on).hardLimit).toBe('Fable is never downshifted silently')
    expect(decideSpawn(spawnInput({ parentModel: 'gpt-5' }), on).apply).toBeNull()
  })

  test('RED or a refused admission advises serializing heavy fan-out', () => {
    expect(decideSpawn(spawnInput(), ctx({ level: 'RED' })).serialize).toBe(true)
    expect(decideSpawn(spawnInput(), ctx({ admitted: false })).serialize).toBe(true)
    expect(decideSpawn(spawnInput({ parentModel: 'claude-sonnet-5-5' }), ctx({ level: 'RED' })).serialize).toBe(false)
  })

  test('route rows keep the route_join schema: optional fields are strings, never null', () => {
    const rt = newRuntime(optionsOf({}))
    rt.sessionId = 'sess-1'
    const e = spawnInput()
    const row = routeRow(e, decideSpawn(e, ctx({ enforce: true })), 'claude-sonnet-5-5', 'agent-1', rt, 1791028800000)
    expect(row).toEqual({
      ts: 1791028800, task_type: 'explore', model: 'sonnet', passed: null, escalated: false, note: 'agent:Explore',
      label_pending: true, outcome: 'async', surface: 'claude-code', tool_use_id: 'toolu_1', start_tier: 'sonnet',
      description: 'Find pressure gate state file', resolved_model: 'claude-sonnet-5-5', applied: 'yes',
      inject_arm: 'evidence', injected: 'no', session_id: 'sess-1', agent_id: 'agent-1', advice_tier: 'sonnet', advice_state: 'READY',
    })
  })
})

describe('router: hooks', () => {
  test('advise mode passes the spawn through unchanged and logs a complete route row', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    expect(world.spawned).toEqual([{ model: undefined, subagentType: 'Explore' }])
    await world.clock.advance(5000)
    expect(routeRows(world)).toEqual([expect.objectContaining({ model: 'inherit', start_tier: 'inherit', resolved_model: 'claude-opus-5-5', agent_id: 'agent-1', applied: 'no' })])
  })

  test('enforce rewrites the model of a READY cell through next()', { options: { enforce: true } }, async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', { [cellKey('explore', 'GREEN', 'sonnet')]: READY })
    await $.session.start(SESSION)
    const res = await $.agent.spawn(spawnInput())
    expect(world.spawned[0]?.model).toBe('sonnet')
    expect(res).toEqual({ model: 'claude-sonnet-5-5', agentId: 'agent-1' })
    await world.clock.advance(5000)
    expect(routeRows(world)[0]).toMatchObject({ model: 'sonnet', start_tier: 'sonnet', applied: 'yes' })
  })

  test('a denied spawn under enforce counts against its cell at once', { options: { enforce: true } }, async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', { [cellKey('explore', 'GREEN', 'sonnet')]: READY })
    await $.session.start(SESSION)
    world.denySpawn = true
    expect(await $.agent.spawn(spawnInput())).toEqual({ deny: 'denied by policy' })
    await world.clock.advance(5000)
    const cell = (world.store.get('datapce.cells') as Record<string, Cell>)[cellKey('explore', 'GREEN', 'sonnet')]
    expect(cell).toMatchObject({ n: 36, pass: 35 })
    expect(routeRows(world)).toEqual([])
  })

  test('a completed subagent labels the cell of the tier that ran', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    await $.turn.complete(complete())
    await world.clock.advance(5000)
    const cells = world.store.get('datapce.cells') as Record<string, Cell>
    expect(cells[cellKey('explore', 'GREEN', 'opus')]).toMatchObject({ n: 1, pass: 1, state: 'WARMING' })
    const stats = world.store.get('datapce.stats') as Record<string, Record<string, { mean: number }>>
    expect(stats[cellKey('explore', 'GREEN', 'opus')]?.tokens?.mean).toBe(41000)
  })

  test('the first promotion to READY toasts once', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', { [cellKey('explore', 'GREEN', 'opus')]: ALMOST })
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    await $.turn.complete(complete())
    expect(world.toasts).toEqual(['datapce: explore|GREEN|opus READY — enforce-eligible (/apex enforce on)'])
  })

  test('unknown agentId after a reload is ignored', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.turn.complete(complete({ agentId: 'agent-from-before-reload' }))
    await world.clock.advance(5000)
    expect(world.store.get('datapce.cells')).toBeUndefined()
  })

  test('malformed persisted cells do not break dispatch', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', 'garbage')
    await $.session.start(SESSION)
    expect(await $.agent.spawn(spawnInput())).toEqual({ model: 'claude-opus-5-5', agentId: 'agent-1' })
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/router.ts` cannot be resolved.

- [ ] **Step 3: Write `router.ts`**

`plugin/hooks/router.ts`:

```ts
// §5 routing at agent.spawn: advise by default; enforce only an own READY cell; hard limits never
// overridden. All I/O goes through the Host; decideSpawn is pure (the §17 latency budget).
import type { AgentSpawnInput, AgentSpawnResult, TurnCompleteInput } from 'claude-code'

import type { Cell, Dispatch, Level, TaskType, Tier } from '../types/index.d.ts'
import { classifyDispatch, promptHeadOf } from './classify.ts'
import { adviceFor, asCells, cellKey, cellViews, recordOutcome, type Advice, type Stats } from './evidence.ts'
import type { Host } from './host.ts'
import { completeRow, DESC_MAX, pushStat, record, spawnRow } from './observe.ts'
import { toastOnce, type Runtime } from './runtime.ts'
import { bucketTake } from './signals.ts'
import { tierOf } from './tiers.ts'

export type SpawnContext = { level: Level; cells: Record<string, Cell>; stats: Stats; enforce: boolean; admitted: boolean }

export type Decision = {
  taskType: TaskType
  requested: string
  requestedTier: Tier | null
  advice: Advice | null
  apply: Tier | null
  serialize: boolean
  hardLimit: string | null
}

const HEAVY: ReadonlySet<Tier> = new Set<Tier>(['opus', 'fable'])
const HEAVY_WINDOW_MS = 10 * 60_000
const DISPATCH_VIEW_MAX = 200

type SpawnFields = Pick<AgentSpawnInput, 'subagentType' | 'description' | 'prompt' | 'model' | 'parentModel' | 'fork'>

export function hardLimitOf(e: Pick<AgentSpawnInput, 'model' | 'fork'>, requestedTier: Tier | null): string | null {
  if (e.fork) return 'a fork inherits the parent model'
  if (e.model !== undefined) return 'explicit model'
  if (requestedTier === 'fable') return 'Fable is never downshifted silently'
  if (requestedTier === null) return 'unknown tier'
  return null
}

export function decideSpawn(e: SpawnFields, ctx: SpawnContext): Decision {
  const taskType = classifyDispatch(e.subagentType, e.description, promptHeadOf(e.prompt))
  const requestedTier = tierOf(e.model ?? e.parentModel)
  const advice = adviceFor(ctx.cells, ctx.stats, taskType, ctx.level, requestedTier)
  const hardLimit = hardLimitOf(e, requestedTier)
  const apply = ctx.enforce && hardLimit === null && advice !== null && advice.own && advice.confidence === 'READY' ? advice.tier : null
  const heavy = requestedTier !== null && HEAVY.has(requestedTier)
  return {
    taskType,
    requested: e.model?.toLowerCase() ?? 'inherit',
    requestedTier,
    advice,
    apply,
    serialize: heavy && (ctx.level === 'RED' || !ctx.admitted),
    hardLimit,
  }
}

/** One route_log.jsonl row in the schema route_join parses: optional fields are strings or absent. */
export function routeRow(e: AgentSpawnInput, d: Decision, resolved: string, agentId: string | null, rt: Runtime, ts: number): Record<string, unknown> {
  const start = d.apply ?? d.requested
  const row: Record<string, unknown> = {
    ts: ts / 1000,
    task_type: d.taskType,
    model: start,
    passed: null,
    escalated: false,
    note: `agent:${e.subagentType || 'general-purpose'}`,
    label_pending: true,
    outcome: 'async',
    surface: 'claude-code',
    tool_use_id: e.tool_use_id,
    start_tier: start,
    description: e.description.slice(0, DESC_MAX),
    resolved_model: resolved,
    applied: d.apply !== null ? 'yes' : 'no',
    inject_arm: rt.arm,
    injected: rt.injectedSections > 0 ? 'yes' : 'no',
  }
  if (rt.sessionId !== '') row.session_id = rt.sessionId
  if (agentId !== null) row.agent_id = agentId
  if (e.parentAgentId !== undefined) row.parent_agent_id = e.parentAgentId
  if (d.advice !== null) {
    row.advice_tier = d.advice.tier
    row.advice_state = d.advice.confidence
  }
  return row
}

/** Before next(): admission bookkeeping for heavy spawns, then the pure decision. */
export function beforeSpawn(rt: Runtime, e: AgentSpawnInput, now: number): Decision {
  const requestedTier = tierOf(e.model ?? e.parentModel)
  let admitted = true
  if (requestedTier !== null && HEAVY.has(requestedTier)) {
    const r = bucketTake(rt.bucket, now / 60_000)
    rt.bucket = r.bucket
    admitted = r.allowed
    rt.heavySpawns = [...rt.heavySpawns.filter(t => now - t <= HEAVY_WINDOW_MS), now]
  }
  return decideSpawn(e, { level: rt.level.level, cells: rt.cells, stats: rt.stats, enforce: rt.enforce, admitted })
}

async function label(host: Host, rt: Runtime, key: string, passed: boolean, now: number): Promise<void> {
  const { before, after } = recordOutcome(rt.cells, key, passed)
  rt.cellsDirty = true
  if (after === 'READY' && before !== 'READY') {
    toastOnce(host, rt, 'promoted', `datapce: ${key} READY — ${rt.enforce ? 'enforced from now on' : 'enforce-eligible (/apex enforce on)'}`, now)
  }
  if (before === 'READY' && after === 'DRIFTING') toastOnce(host, rt, 'demoted', `datapce: ${key} demoted (DRIFTING) — advise only`, now)
  await host.publish.cells(cellViews(rt.cells, rt.stats))
}

const dispatchList = (rt: Runtime): Dispatch[] => [...rt.dispatches.values()].slice(-DISPATCH_VIEW_MAX)

/** After next(): the dispatch view, the observe row, the route row; a denied enforced spawn is a fail. */
export async function afterSpawn(host: Host, rt: Runtime, e: AgentSpawnInput, d: Decision, res: AgentSpawnResult, now: number): Promise<void> {
  try {
    const resolved = res.deny === undefined ? res.model : null
    const agentId = res.deny === undefined ? (res.agentId ?? null) : null
    const notes = [
      d.advice?.basis,
      d.serialize ? 'serialize heavy fan-out' : undefined,
      d.advice !== null && d.hardLimit !== null ? `advise only: ${d.hardLimit}` : undefined,
    ].filter((x): x is string => x !== undefined)
    rt.dispatches.set(e.tool_use_id, {
      toolUseId: e.tool_use_id,
      agentId,
      description: e.description.slice(0, DESC_MAX),
      subagentType: e.subagentType,
      taskType: d.taskType,
      requested: d.requested,
      resolved,
      advised: d.advice?.tier ?? null,
      advisedState: d.advice?.confidence ?? null,
      applied: d.apply !== null,
      basis: notes.join('; '),
      outcome: resolved === null ? 'failed' : 'running',
      startedAt: now,
      durationMs: null,
      tokens: null,
      level: rt.level.level,
    })
    if (agentId !== null) rt.byAgent.set(agentId, e.tool_use_id)
    record(rt, spawnRow(e, d.taskType, resolved, now))
    if (resolved !== null) rt.routeRows.push(JSON.stringify(routeRow(e, d, resolved, agentId, rt, now)))
    else if (d.apply !== null) await label(host, rt, cellKey(d.taskType, rt.level.level, d.apply), false, now)
    rt.profile = { ...rt.profile, taskMix: { ...rt.profile.taskMix, [d.taskType]: (rt.profile.taskMix[d.taskType] ?? 0) + 1 } }
    rt.profileDirty = true
    await host.publish.dispatches(dispatchList(rt))
  } catch {
    // fail open: the spawn already happened
  }
}

/** turn.complete of a subagent: its outcome becomes the label of the cell that ran it. */
export async function onComplete(host: Host, rt: Runtime, e: TurnCompleteInput, now: number): Promise<void> {
  try {
    if (e.agentId === undefined) return
    const id = rt.byAgent.get(e.agentId)
    const d = id === undefined ? undefined : rt.dispatches.get(id)
    if (id === undefined || d === undefined || d.outcome !== 'running') return
    const ok = e.reason === 'answer'
    const tokens = e.usage === undefined ? null : e.usage.input_tokens + e.usage.output_tokens
    rt.dispatches.set(id, { ...d, outcome: ok ? 'ok' : 'failed', durationMs: e.durationMs, tokens })
    const tier = tierOf(d.resolved)
    if (tier !== null) {
      const key = cellKey(d.taskType, d.level, tier)
      pushStat(rt.stats, key, 'tokens', tokens)
      pushStat(rt.stats, key, 'duration_ms', e.durationMs)
      rt.statsDirty = true
      await label(host, rt, key, ok, now)
    }
    record(rt, completeRow(e.agentId, ok, e.durationMs, tokens, now))
    await host.publish.dispatches(dispatchList(rt))
  } catch {
    // fail open: a label lost is a label lost, never a broken turn
  }
}

/** session.start (after observe.start): persisted cells and the enforce switch. */
export async function start(host: Host, rt: Runtime): Promise<void> {
  try {
    rt.cells = asCells(await host.storeGet('datapce.cells'))
    rt.enforce = rt.options.enforce || (await host.storeGet('datapce.enforce')) === true
    await host.publish.cells(cellViews(rt.cells, rt.stats))
    await host.publish.enforce(rt.enforce)
  } catch {
    // a store that cannot be read starts with no cells, advise only
  }
}
```

- [ ] **Step 4: Wire the router into `register.ts`**

`plugin/hooks/register.ts` — replace the import block (every line above `// Wiring only.`) with:

```ts
import type { EngineInterface, Register } from 'claude-code'

import { install as band, measureOf } from './band.tsx'
import type { Host } from './host.ts'
import {
  flushAll,
  install as observe,
  measure as observeMeasure,
  skill as observeSkill,
  start as observeStart,
} from './observe.ts'
import { afterSpawn, beforeSpawn, onComplete, start as routerStart, type Decision } from './router.ts'
import { identify, newRuntime, optionsOf } from './runtime.ts'
```

`plugin/hooks/register.ts` — replace everything from `export const register` to the end of the file with (the state refs and `hostOf` above it stay as they are):

```ts
export const register: Register = (on, raw) => {
  const rt = newRuntime(optionsOf(raw))

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await identify(host, e, rt)
    await observeStart(host, e, rt)
    await routerStart(host, rt)
    return r
  })

  on('session.end', async ($, e, next) => {
    await flushAll(hostOf($), rt)
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const host = hostOf($)
    await host.publish.measure(measureOf(e))
    observeMeasure(rt, e, await host.now())
    return next(e)
  })

  on('skill.prompt', async ($, e, next) => {
    const r = await next(e)
    observeSkill(rt, e.skill, await hostOf($).now())
    return r
  })

  on('agent.spawn', async ($, e, next) => {
    const host = hostOf($)
    let d: Decision | null = null
    try {
      d = beforeSpawn(rt, e, await host.now())
    } catch {
      d = null
    }
    if (d === null) return next(e)
    const res = await next(d.apply !== null ? { ...e, model: d.apply } : e)
    await afterSpawn(host, rt, e, d, res, await host.now())
    return res
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await onComplete(host, rt, e, await host.now())
    return r
  })

  band(on, rt)
  observe(on, rt)
}
```

- [ ] **Step 5: Run the tests**

Run: `claude plugin test plugin`
Expected: `70 pass`, `0 fail`.

- [ ] **Step 6: Manual check of §15 (Workflow → `agent.spawn`)**

In a scratch session: `claude --plugin-dir plugin`, run a small `Workflow` script that starts one agent, then `tail -n 3 ~/.apex-router/observe/$(date -u +%F).jsonl`. Record in the commit message body whether an `"ev":"spawn"` row appeared for the Workflow agent (`Workflow agents raise agent.spawn: yes|no`). No code change either way: `tool.describe` (Task 14) carries the tier guidance for Workflow regardless.

- [ ] **Step 7: Full suite, validate, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1758 passed, 4 skipped`; `claude plugin validate plugin` → passed, hooks list includes `agent.spawn`, `turn.complete`.

```bash
git add plugin/hooks/router.ts plugin/hooks/register.ts plugin/tests/router.test.ts
git commit -m "feat(plugin): router — advise/enforce at agent.spawn, hard limits, route rows, completion labels"
```

---

### Task 14: `inject.ts` — evidence into the planning skills and `Agent`/`Workflow`, with a self A/B arm

**Files:**
- Create: `plugin/hooks/inject.ts`, `plugin/tests/inject.test.ts`
- Modify: `plugin/hooks/register.ts` (imports; `session.start` calls `injectStart`; `skill.prompt` returns the section; new `tool.describe` hook)

**Interfaces:**
- Consumes: `evidenceRows`, `kTok`, `EvidenceRow` (Task 11); `record` (Task 12); `Host`, `Runtime` (Task 1).
- Produces: `INJECT_SKILLS`, `DESCRIBE_TOOLS`, `MAX_SECTION_LINES = 25`, `MAX_ROWS = 18`, `MAX_DESCRIBE_LINES = 2`, `SESSION_CAP_BYTES = 2048`, `HOLDOUT_PCT = 20`, `INVALIDATE_EVERY_MS = 600_000`, `RULE`, `bareSkill(skill)`, `fnv1a(s)`, `armOf(sessionId): Arm`, `utf8Bytes(s)`, `budgetLeft(rt)`, `evidenceSection(rows, level, budgetLeftUsd): string | null`, `describeText(level, rows): string | null`, `skillSection(host, rt, skill, text, now): Promise<string>`, `describe(host, rt, tool, description, now): Promise<string>`, `start(host, rt)`.

Rules (spec §5 "Injection", §17.2–17.3, §17.6): the section goes only to `superpowers:writing-plans`, `superpowers:subagent-driven-development`, `superpowers:dispatching-parallel-agents`, `superpowers:executing-plans` and the plugin's own `datapce` skill (matched on the name after the last `:`); it is a table (`task_type | tier | n pass% | tok μ | state`) plus one rule line, ≤ 25 lines; no evidence (all COLD) → no text at all. `tool.describe` on `Agent`/`Workflow` appends ≤ 2 lines (pressure when not GREEN; tiers per task type when any cell is non-COLD). Every injection decision is logged as an `inject` row with the session's arm; holdout sessions (20%, FNV-1a of the session id) get nothing and log `withheld: true`; the per-session total is capped at 2048 bytes (`capped: true` when hit). `tool.describe` is re-rendered by `$.ui.invalidate('tool.describe')` only when its text changed and at most once per 10 minutes (each change re-prices the prompt cache). `prompt.compose` is not hooked (see Global Constraints).

Spec ambiguity resolved here: §5's evidence row says "$ p50" — v1 shows the cell's mean tokens (`tok μ`, from the persisted Welford moments). A dollar p50 would need either stored rows (forbidden by A9) or a price table the plugin does not have; the label says what the number is.

- [ ] **Step 1: Write the failing tests**

`plugin/tests/inject.test.ts`:

```ts
import { describe, expect, test } from 'claude-code/testing'

import type { Cell } from '../types/index.d.ts'
import { cellKey } from '../hooks/evidence.ts'
import { armOf, bareSkill, describeText, evidenceSection, fnv1a, INJECT_SKILLS } from '../hooks/inject.ts'
import { complete, SESSION, spawnInput } from './fixtures/inputs.ts'
import { BACKEND, T0, worldOf, type World } from './fixtures/world.ts'

const READY: Cell = { n: 35, pass: 35, state: 'READY', winN: 5, winPass: 5, below: 0, above: 0, cusum: 0, p0: 1 }
const PROVIDER = { plugin: 'engine', tier: 'core' } as never
const observed = (world: World): Record<string, unknown>[] =>
  (world.appended.get(`${BACKEND}/observe/2026-10-03.jsonl`) ?? []).map(l => JSON.parse(l))
const seedReady = (world: World): void => {
  world.store.set('datapce.cells', { [cellKey('explore', 'GREEN', 'sonnet')]: READY })
}

describe('inject: pure', () => {
  test('FNV-1a and the 80/20 arm split', () => {
    expect(fnv1a('')).toBe(2166136261)
    expect(fnv1a('a')).toBe(3826002220)
    expect(armOf('sess-0001')).toBe('evidence')
    expect(armOf('sess-0002')).toBe('holdout')
  })

  test('skills match on the name after the last colon', () => {
    expect(bareSkill('superpowers:writing-plans')).toBe('writing-plans')
    expect(INJECT_SKILLS.has(bareSkill('datapce:datapce'))).toBe(true)
    expect(INJECT_SKILLS.has(bareSkill('commit'))).toBe(false)
  })

  test('no evidence, no section', () => {
    expect(evidenceSection([], 'GREEN', null)).toBeNull()
  })

  test('the section is a table plus one rule', () => {
    const text = evidenceSection(
      [
        { taskType: 'explore', tier: 'sonnet', n: 35, passPct: 100, tokMean: 41000, state: 'READY' },
        { taskType: 'review', tier: 'opus', n: 12, passPct: 92, tokMean: null, state: 'WARMING' },
      ],
      'AMBER',
      5.88,
    )
    expect(text).toBe(
      [
        '## datapce evidence (this machine)',
        'pressure AMBER · budget $5.88 left',
        '| task_type | tier | n pass% | tok μ | state |',
        '|---|---|---|---|---|',
        '| explore | sonnet | 35 100% | 41k | READY |',
        '| review | opus | 12 92% | — | WARMING 12/30 |',
        'rule: name the tier per task in the plan; datapce applies it at dispatch.',
      ].join('\n'),
    )
  })

  test('≤ 25 lines however many task types', () => {
    const rows = Array.from({ length: 40 }, (_, i) => ({ taskType: `t${i}`, tier: 'sonnet' as const, n: 3, passPct: 100, tokMean: null, state: 'WARMING' as const }))
    expect(evidenceSection(rows, 'GREEN', null)!.split('\n').length).toBeLessThanOrEqual(25)
  })

  test('describe text: ≤ 2 lines; nothing when GREEN and COLD', () => {
    expect(describeText('GREEN', [])).toBeNull()
    expect(describeText('RED', [{ taskType: 'explore', tier: 'sonnet', n: 35, passPct: 100, tokMean: null, state: 'READY' }])).toBe(
      'datapce: pressure RED — serialize heavy fan-out\ndatapce tiers per task_type: explore→sonnet(READY)',
    )
  })
})

describe('inject: hooks', () => {
  test('COLD: planning skills get nothing appended and nothing is logged', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    expect((await $.skill.prompt({ skill: 'superpowers:writing-plans', text: 'PLAN' })).text).toBe('PLAN')
    await world.clock.advance(5000)
    expect(observed(world).filter(r => r.ev === 'inject')).toEqual([])
  })

  test('evidence arm: planning skills only, logged, and stamped on route rows', async ($, on) => {
    const world = worldOf(on)
    seedReady(world)
    await $.session.start(SESSION)
    const plan = await $.skill.prompt({ skill: 'superpowers:writing-plans', text: 'PLAN' })
    expect(plan.text.startsWith('PLAN\n\n## datapce evidence (this machine)\npressure GREEN\n')).toBe(true)
    expect(plan.text).toContain('| explore | sonnet | 35 100% | — | READY |')
    expect((await $.skill.prompt({ skill: 'commit', text: 'COMMIT' })).text).toBe('COMMIT')
    await $.agent.spawn(spawnInput())
    await world.clock.advance(5000)
    const inject = observed(world).filter(r => r.ev === 'inject')
    expect(inject).toEqual([expect.objectContaining({ site: 'skill', skill: 'superpowers:writing-plans', arm: 'evidence' })])
    expect(inject[0]!.bytes as number).toBeGreaterThan(0)
    const route = (world.appended.get(`${BACKEND}/route_log.jsonl`) ?? []).map(l => JSON.parse(l))
    expect(route[0]).toMatchObject({ inject_arm: 'evidence', injected: 'yes' })
  })

  test('holdout arm: nothing appended; the withheld section is logged', async ($, on) => {
    const world = worldOf(on, {}, 'sess-0002')
    seedReady(world)
    await $.session.start(SESSION)
    expect((await $.skill.prompt({ skill: 'superpowers:executing-plans', text: 'EXEC' })).text).toBe('EXEC')
    await world.clock.advance(5000)
    expect(observed(world).filter(r => r.ev === 'inject')).toEqual([
      { ev: 'inject', ts: T0, site: 'skill', skill: 'superpowers:executing-plans', arm: 'holdout', bytes: 0, withheld: true },
    ])
  })

  test('tool.describe on Agent and Workflow appends the tiers line', async ($, on) => {
    const world = worldOf(on)
    seedReady(world)
    await $.session.start(SESSION)
    for (const tool of ['Agent', 'Workflow']) {
      const r = await $.tool.describe({ tool, description: 'Launch work', provider: PROVIDER })
      expect(r.description).toBe('Launch work\ndatapce tiers per task_type: explore→sonnet(READY)')
    }
  })

  test('a changed tiers line invalidates tool.describe at most once per 10 minutes', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput({ subagentType: 'general-purpose', description: 'Review the diff' }))
    await $.turn.complete(complete())
    await world.clock.advance(9 * 60_000)
    expect(world.invalidated).toEqual([])
    await world.clock.advance(60_000)
    expect(world.invalidated).toEqual(['tool.describe'])
    await world.clock.advance(5 * 60_000)
    expect(world.invalidated).toEqual(['tool.describe'])
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/inject.ts` cannot be resolved.

- [ ] **Step 3: Write `inject.ts`**

`plugin/hooks/inject.ts`:

```ts
// §5 injection under the §17 contract: only at decision time, only what changes the decision, a
// table not prose, nothing when there is no evidence — and every injection measured (self A/B).
import type { Arm, Level } from '../types/index.d.ts'
import { evidenceRows, kTok, type EvidenceRow } from './evidence.ts'
import type { Host } from './host.ts'
import { record } from './observe.ts'
import type { Runtime } from './runtime.ts'

export const INJECT_SKILLS: ReadonlySet<string> = new Set([
  'writing-plans',
  'subagent-driven-development',
  'dispatching-parallel-agents',
  'executing-plans',
  'datapce',
])
export const DESCRIBE_TOOLS = ['Agent', 'Workflow'] as const
export const MAX_SECTION_LINES = 25
export const MAX_ROWS = 18
export const MAX_DESCRIBE_LINES = 2
export const SESSION_CAP_BYTES = 2048
export const HOLDOUT_PCT = 20
export const INVALIDATE_EVERY_MS = 600_000
export const RULE = 'rule: name the tier per task in the plan; datapce applies it at dispatch.'
const MAX_TIERS = 12

export const bareSkill = (skill: string): string => skill.slice(skill.lastIndexOf(':') + 1)

export function fnv1a(s: string): number {
  let h = 0x811c9dc5
  for (const b of new TextEncoder().encode(s)) {
    h ^= b
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h
}

export const armOf = (sessionId: string): Arm => (fnv1a(sessionId) % 100 < HOLDOUT_PCT ? 'holdout' : 'evidence')

export const utf8Bytes = (s: string): number => new TextEncoder().encode(s).length

export const budgetLeft = (rt: Runtime): number | null =>
  rt.options.budgetUsd > 0 && rt.lastCostUsd !== null ? Math.max(0, rt.options.budgetUsd - rt.lastCostUsd) : null

const stateCell = (r: EvidenceRow): string => (r.state === 'READY' ? 'READY' : `${r.state} ${r.n}/30`)

export function evidenceSection(rows: readonly EvidenceRow[], level: Level, budgetLeftUsd: number | null): string | null {
  if (rows.length === 0) return null
  const lines = [
    '## datapce evidence (this machine)',
    `pressure ${level}${budgetLeftUsd === null ? '' : ` · budget $${budgetLeftUsd.toFixed(2)} left`}`,
    '| task_type | tier | n pass% | tok μ | state |',
    '|---|---|---|---|---|',
    ...rows.slice(0, MAX_ROWS).map(r => `| ${r.taskType} | ${r.tier} | ${r.n} ${r.passPct}% | ${kTok(r.tokMean)} | ${stateCell(r)} |`),
    RULE,
  ]
  return lines.slice(0, MAX_SECTION_LINES).join('\n')
}

export function describeText(level: Level, rows: readonly EvidenceRow[]): string | null {
  const lines: string[] = []
  if (level !== 'GREEN') lines.push(`datapce: pressure ${level}${level === 'RED' ? ' — serialize heavy fan-out' : ''}`)
  const tiers = rows.slice(0, MAX_TIERS).map(r => `${r.taskType}→${r.tier}(${stateCell(r)})`)
  if (tiers.length > 0) lines.push(`datapce tiers per task_type: ${tiers.join(' ')}`)
  return lines.length > 0 ? lines.slice(0, MAX_DESCRIBE_LINES).join('\n') : null
}

const rowsNow = (rt: Runtime): EvidenceRow[] => evidenceRows(rt.cells, rt.stats, rt.level.level)

/** One injection decision: arm, cap, log, publish. Returns the bytes appended (0 = nothing). */
async function admit(host: Host, rt: Runtime, site: string, name: string, text: string, now: number): Promise<number> {
  const bytes = utf8Bytes(text) + 1
  const base = { ev: 'inject', ts: now, site, skill: name, arm: rt.arm }
  if (rt.arm === 'holdout') {
    record(rt, { ...base, bytes: 0, withheld: true })
    return 0
  }
  if (rt.injectedBytes + bytes > SESSION_CAP_BYTES) {
    record(rt, { ...base, bytes: 0, capped: true })
    return 0
  }
  rt.injectedBytes += bytes
  rt.injectedSections += 1
  record(rt, { ...base, bytes })
  await host.publish.inject({ arm: rt.arm, bytes: rt.injectedBytes, sections: rt.injectedSections })
  return bytes
}

export async function skillSection(host: Host, rt: Runtime, skill: string, text: string, now: number): Promise<string> {
  try {
    if (!INJECT_SKILLS.has(bareSkill(skill))) return text
    const section = evidenceSection(rowsNow(rt), rt.level.level, budgetLeft(rt))
    if (section === null) return text
    return (await admit(host, rt, 'skill', skill, section, now)) > 0 ? `${text}\n\n${section}` : text
  } catch {
    return text
  }
}

export async function describe(host: Host, rt: Runtime, tool: string, description: string, now: number): Promise<string> {
  try {
    const lines = describeText(rt.level.level, rowsNow(rt))
    if (lines === null) return description
    return (await admit(host, rt, 'describe', tool, lines, now)) > 0 ? `${description}\n${lines}` : description
  } catch {
    return description
  }
}

/** session.start (after the router loaded cells): the arm, and the rate-limited describe refresh. */
export async function start(host: Host, rt: Runtime): Promise<void> {
  try {
    rt.arm = armOf(rt.sessionId)
    await host.publish.inject({ arm: rt.arm, bytes: 0, sections: 0 })
    if (rt.arm === 'holdout') return
    let last = describeText(rt.level.level, rowsNow(rt))
    let lastInvalidate = await host.now()
    host.every(60_000, () => {
      void (async () => {
        const now = await host.now()
        const text = describeText(rt.level.level, rowsNow(rt))
        if (text !== last && now - lastInvalidate >= INVALIDATE_EVERY_MS) {
          last = text
          lastInvalidate = now
          host.invalidate('tool.describe')
        }
      })()
    })
  } catch {
    // no arm, no injection: evidence arm by default and nothing scheduled
  }
}
```

- [ ] **Step 4: Wire it into `register.ts`**

`plugin/hooks/register.ts` — replace the import block (every line above `// Wiring only.`) with:

```ts
import type { EngineInterface, Register } from 'claude-code'

import { install as band, measureOf } from './band.tsx'
import type { Host } from './host.ts'
import { describe as injectDescribe, skillSection, start as injectStart } from './inject.ts'
import {
  flushAll,
  install as observe,
  measure as observeMeasure,
  skill as observeSkill,
  start as observeStart,
} from './observe.ts'
import { afterSpawn, beforeSpawn, onComplete, start as routerStart, type Decision } from './router.ts'
import { identify, newRuntime, optionsOf } from './runtime.ts'
```

`plugin/hooks/register.ts` — replace everything from `export const register` to the end of the file with (the state refs and `hostOf` above it stay as they are):

```ts
export const register: Register = (on, raw) => {
  const rt = newRuntime(optionsOf(raw))

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await identify(host, e, rt)
    await observeStart(host, e, rt)
    await routerStart(host, rt)
    await injectStart(host, rt)
    return r
  })

  on('session.end', async ($, e, next) => {
    await flushAll(hostOf($), rt)
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const host = hostOf($)
    await host.publish.measure(measureOf(e))
    observeMeasure(rt, e, await host.now())
    return next(e)
  })

  on('skill.prompt', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    const now = await host.now()
    observeSkill(rt, e.skill, now)
    return { ...r, text: await skillSection(host, rt, e.skill, r.text, now) }
  })

  on('tool.describe', { tool: ['Agent', 'Workflow'] }, async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    return { ...r, description: await injectDescribe(host, rt, e.tool, r.description, await host.now()) }
  })

  on('agent.spawn', async ($, e, next) => {
    const host = hostOf($)
    let d: Decision | null = null
    try {
      d = beforeSpawn(rt, e, await host.now())
    } catch {
      d = null
    }
    if (d === null) return next(e)
    const res = await next(d.apply !== null ? { ...e, model: d.apply } : e)
    await afterSpawn(host, rt, e, d, res, await host.now())
    return res
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await onComplete(host, rt, e, await host.now())
    return r
  })

  band(on, rt)
  observe(on, rt)
}
```

- [ ] **Step 5: Run the tests**

Run: `claude plugin test plugin`
Expected: `81 pass`, `0 fail`.

- [ ] **Step 6: Full suite, validate, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1758 passed, 4 skipped`; `claude plugin validate plugin` → passed; hooks include `tool.describe{tool=Agent|Workflow}` alongside `skill.prompt`.

```bash
git add plugin/hooks/inject.ts plugin/hooks/register.ts plugin/tests/inject.test.ts
git commit -m "feat(plugin): inject — measurable evidence for planning skills and Agent/Workflow descriptions"
```

---

### Task 15: `backend.ts` — §9 detection and the off-hot-path poll

**Files:**
- Create: `plugin/hooks/backend.ts`, `plugin/tests/backend.test.ts`
- Modify: `plugin/hooks/register.ts` (imports; `session.start` calls `backendStart`)

**Interfaces:**
- Consumes: `readJson`, `tailLines`, `jsonLines` (Task 12); `availability`, `drainEta`, `series`, `breakerState` (Task 10); `EMPTY_BACKEND` (Task 1); `Host`, `Runtime`.
- Produces: `POLL_MS = 60_000`, `ADVISE_EVERY_MS = 600_000`, `detectPaths(backendDir, home)`, `apexCandidates(backendDir, home)`, `parsePressure(v): Record<string, Level>`, `conformanceRate(rows): number | null`, `parseVerdicts(v): Record<string, string>`, `parseHandoff(v): number | null`, `laneStatsOf(rows, nowSec): LaneStats | null`, `Sample = { t: number; inbox: number }`, `drainEtaOf(history, completedLastHour, now): number | null` (seconds), `healthOf(up: Record<string, boolean[]>): number | null`, `Raw` (the polled inputs), `viewOf(raw): BackendView`, `start(host, rt)`.

§9: present when any of `<backendDir>/ornith.env`, `<backendDir>/pressure.json`, `~/.apex/telemetry.jsonl` exists. When present, the plugin runs only `apex-router pressure --json` (each poll) and `apex-router route-advise --json` (every 10 minutes) through `host.run` with timeouts, and reads `conformance.jsonl` (last 200 lines), `handoff_threshold.json`, `~/.apex/offload_telemetry.jsonl` (last 2000 lines; the backend's default sink) and the queue directories `<backendDir>/queue/jobs/{inbox,done,failed}`. `review-preread` is run by the skill, never by hooks. The binary is the first existing of `<backendDir>/.venv/bin/apex-router`, `~/.local/bin/apex-router`; without one, files are still read. Absent backend: an empty view (band/pane cells read `—`, lanes hidden), pressure from `session.measure` only. Health per §7 "`A = MTBF/(MTBF+MTTR)`; series proxy × ollama × worker": each poll samples each component up/down — `proxy` (the pressure call or a pressure.json younger than 10 min), `ornith` (its lane breaker not open), `worker` (inbox empty or a job finished in the last hour); with equal sampling intervals the up-fraction is MTBF/(MTBF+MTTR), and the components multiply in series. Drain ETA per §7 `backlog/(cap − arrive)`: cap = jobs finished in the last hour per minute; arrive = cap + inbox growth per minute over the samples of the last hour.

- [ ] **Step 1: Write the failing tests**

`plugin/tests/backend.test.ts`:

```ts
import { describe, expect, test } from 'claude-code/testing'

import {
  conformanceRate, drainEtaOf, healthOf, laneStatsOf, parseHandoff, parsePressure, parseVerdicts, viewOf,
} from '../hooks/backend.ts'
import { SESSION } from './fixtures/inputs.ts'
import { BACKEND, HOME, T0, worldOf } from './fixtures/world.ts'

const PRESSURE = JSON.stringify({
  families: {
    fable: { level: 'GREEN', requests: 25 },
    opus: { level: 'AMBER', requests: 353, transport_rate: 0.0312 },
    bogus: { level: 'PURPLE' },
  },
})

describe('backend: parsing', () => {
  test('pressure families keep valid levels only', () => {
    expect(parsePressure(JSON.parse(PRESSURE))).toEqual({ fable: 'GREEN', opus: 'AMBER' })
    expect(parsePressure('garbage')).toEqual({})
  })

  test('conformance rate over rows that carry a boolean matched', () => {
    expect(conformanceRate([{ matched: true }, { matched: false }, { matched: true }, { note: 'x' }])).toBe(2 / 3)
    expect(conformanceRate([])).toBeNull()
  })

  test('route-advise verdicts and the handoff threshold', () => {
    expect(parseVerdicts({ explore: { verdict: 'cost_favors_cheap_start' }, review: { verdict: 3 } })).toEqual({ explore: 'cost_favors_cheap_start' })
    expect(parseHandoff({ threshold_tokens: 43860328 })).toBe(43860328)
    expect(parseHandoff({})).toBeNull()
  })

  test('lane stats over the last 24 h', () => {
    const now = T0 / 1000
    const rows = [
      { ts: now - 60, lane: 'codegen', ok: true, escalated: false },
      { ts: now - 120, lane: 'codegen', ok: false, escalated: true },
      { ts: now - 90000, lane: 'codegen', ok: true, escalated: false },
      { ts: now - 30, lane: 'preread', ok: true, escalated: true },
    ]
    expect(laneStatsOf(rows, now)).toEqual({ codegen: { n: 2, ok: 1, escalated: 1 }, preread: { n: 1, ok: 1, escalated: 1 } })
    expect(laneStatsOf([], now)).toBeNull()
  })

  test('drain ETA = backlog / (cap − arrive)', () => {
    const shrinking = [{ t: T0 - 3_600_000, inbox: 160 }, { t: T0, inbox: 100 }]
    expect(drainEtaOf(shrinking, 120, T0)).toBe(6000)
    const growing = [{ t: T0 - 3_600_000, inbox: 100 }, { t: T0, inbox: 160 }]
    expect(drainEtaOf(growing, 120, T0)).toBeNull()
    expect(drainEtaOf([{ t: T0, inbox: 0 }], 0, T0)).toBeNull()
  })

  test('health is the series product of per-component availability', () => {
    expect(healthOf({ proxy: [true, true, true, false], worker: [true, true] })).toBe(0.75)
    expect(healthOf({})).toBeNull()
  })

  test('the view keeps absent things absent', () => {
    expect(viewOf({ present: false, families: {}, conformance: null, verdicts: {}, handoff: null, lanes: null, drainEtaS: null, health: null })).toEqual({
      present: false, conformance: null, families: {}, lanes: null, drainEtaS: null, health: null, verdicts: {}, handoffTokens: null,
    })
  })
})

describe('backend: hooks', () => {
  test('no backend: nothing is run, the profile says absent', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await world.clock.advance(5000)
    expect(world.runs.some(argv => argv.some(a => a.endsWith('apex-router')))).toBe(false)
    expect((world.store.get('datapce.profile') as { backend: boolean }).backend).toBe(false)
  })

  test('backend present: pressure each minute, route-advise every 10 minutes, files read', async ($, on) => {
    const bin = `${BACKEND}/.venv/bin/apex-router`
    const world = worldOf(on, {
      [`${BACKEND}/pressure.json`]: PRESSURE,
      [`${BACKEND}/conformance.jsonl`]: '{"matched": true}\n{"matched": false}\n',
      [`${BACKEND}/handoff_threshold.json`]: '{"threshold_tokens": 1000}',
      [`${HOME}/.apex/offload_telemetry.jsonl`]: `{"ts": ${T0 / 1000 - 10}, "lane": "codegen", "ok": true, "escalated": false}\n`,
      [bin]: '#!/bin/sh\n',
    })
    world.answer = argv => {
      if (argv[0] !== bin) return null
      if (argv[1] === 'pressure') return { exitCode: 0, stdout: PRESSURE }
      if (argv[1] === 'route-advise') return { exitCode: 0, stdout: '{"explore": {"verdict": "hold"}}' }
      return null
    }
    await $.session.start(SESSION)
    await world.clock.advance(5000)
    const calls = () => world.runs.filter(argv => argv[0] === bin).map(argv => argv.slice(1).join(' '))
    expect(calls()).toEqual(['pressure --json', 'route-advise --json'])
    await world.clock.advance(60_000)
    expect(calls()).toEqual(['pressure --json', 'route-advise --json', 'pressure --json'])
    expect((world.store.get('datapce.profile') as { backend: boolean }).backend).toBe(true)
    expect(world.asked).toContain(`read ${BACKEND}/handoff_threshold.json`)
  })

  test('a failing backend command falls back to the files and never throws', async ($, on) => {
    const bin = `${HOME}/.local/bin/apex-router`
    const world = worldOf(on, { [`${BACKEND}/pressure.json`]: PRESSURE, [bin]: '#!/bin/sh\n' })
    world.answer = argv => (argv[0] === bin ? { exitCode: 2, stdout: 'boom' } : null)
    await $.session.start(SESSION)
    await world.clock.advance(60_000)
    expect(world.asked).toContain(`read ${BACKEND}/pressure.json`)
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/backend.ts` cannot be resolved.

- [ ] **Step 3: Write `backend.ts`**

`plugin/hooks/backend.ts`:

```ts
// §9 backend boundary: detected, never required; polled every 60 s off the hot path; every call
// fails open. Commands: `apex-router pressure --json`, `apex-router route-advise --json` only.
import type { BackendView, LaneStats, Level } from '../types/index.d.ts'
import type { Host } from './host.ts'
import { jsonLines, readJson, tailLines } from './io.ts'
import type { Runtime } from './runtime.ts'
import { availability, breakerState, drainEta, series } from './signals.ts'

export const POLL_MS = 60_000
export const ADVISE_EVERY_MS = 600_000
const LEVELS: readonly Level[] = ['GREEN', 'AMBER', 'RED']
const HOUR_MS = 3_600_000
const SAMPLES_MAX = 120

export const detectPaths = (backendDir: string, home: string): string[] => [
  `${backendDir}/ornith.env`,
  `${backendDir}/pressure.json`,
  `${home}/.apex/telemetry.jsonl`,
]

export const apexCandidates = (backendDir: string, home: string): string[] => [`${backendDir}/.venv/bin/apex-router`, `${home}/.local/bin/apex-router`]

const obj = (v: unknown): Record<string, unknown> | null => (v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null)

export function parsePressure(v: unknown): Record<string, Level> {
  const out: Record<string, Level> = {}
  const families = obj(obj(v)?.families)
  if (families === null) return out
  for (const [name, f] of Object.entries(families)) {
    const level = obj(f)?.level
    if (LEVELS.includes(level as Level)) out[name] = level as Level
  }
  return out
}

export function conformanceRate(rows: readonly Record<string, unknown>[]): number | null {
  const judged = rows.filter(r => typeof r.matched === 'boolean')
  return judged.length === 0 ? null : judged.filter(r => r.matched === true).length / judged.length
}

export function parseVerdicts(v: unknown): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [tt, rec] of Object.entries(obj(v) ?? {})) {
    const verdict = obj(rec)?.verdict
    if (typeof verdict === 'string') out[tt] = verdict
  }
  return out
}

export function parseHandoff(v: unknown): number | null {
  const t = obj(v)?.threshold_tokens
  return typeof t === 'number' && Number.isFinite(t) && t > 0 ? t : null
}

export function laneStatsOf(rows: readonly Record<string, unknown>[], nowSec: number): LaneStats | null {
  const out: LaneStats = {}
  for (const r of rows) {
    if (typeof r.ts !== 'number' || nowSec - r.ts > 86_400) continue
    const lane = typeof r.lane === 'string' ? r.lane : 'unknown'
    const s = (out[lane] ??= { n: 0, ok: 0, escalated: 0 })
    s.n += 1
    if (r.ok === true) s.ok += 1
    if (r.escalated === true) s.escalated += 1
  }
  return Object.keys(out).length > 0 ? out : null
}

export type Sample = { t: number; inbox: number }

/** Seconds until the inbox drains: backlog / (cap − arrive), both per minute over the last hour. */
export function drainEtaOf(history: readonly Sample[], completedLastHour: number, now: number): number | null {
  const recent = history.filter(s => now - s.t <= HOUR_MS)
  const first = recent[0]
  const last = recent[recent.length - 1]
  if (first === undefined || last === undefined || last.inbox === 0) return null
  const cap = completedLastHour / 60
  const minutes = (last.t - first.t) / 60_000
  const arrive = cap + (minutes > 0 ? (last.inbox - first.inbox) / minutes : 0)
  const eta = drainEta(last.inbox, cap, arrive)
  return eta === null ? null : Math.round(eta * 60)
}

export function healthOf(up: Record<string, readonly boolean[]>): number | null {
  const parts: number[] = []
  for (const samples of Object.values(up)) {
    if (samples.length === 0) continue
    const ups = samples.filter(Boolean).length
    parts.push(availability(ups, samples.length - ups))
  }
  return parts.length === 0 ? null : series(...parts)
}

export type Raw = {
  present: boolean
  families: Record<string, Level>
  conformance: number | null
  verdicts: Record<string, string>
  handoff: number | null
  lanes: LaneStats | null
  drainEtaS: number | null
  health: number | null
}

export const viewOf = (raw: Raw): BackendView => ({
  present: raw.present,
  conformance: raw.conformance,
  families: raw.families,
  lanes: raw.lanes,
  drainEtaS: raw.drainEtaS,
  health: raw.health,
  verdicts: raw.verdicts,
  handoffTokens: raw.handoff,
})

async function firstExisting(host: Host, paths: readonly string[]): Promise<string | null> {
  for (const p of paths) {
    try {
      if (await host.exists(p)) return p
    } catch {
      // keep looking
    }
  }
  return null
}

async function jsonOf(host: Host, argv: readonly string[], timeoutMs: number): Promise<unknown> {
  try {
    const r = await host.run(argv, { timeoutMs })
    return r.exitCode === 0 ? (JSON.parse(r.stdout) as unknown) : null
  } catch {
    return null
  }
}

async function finishedSince(host: Host, dir: string, since: number): Promise<number> {
  try {
    return (await host.list(dir)).filter(e => e.kind === 'file' && e.mtimeMs >= since).length
  } catch {
    return 0
  }
}

type PollState = { inbox: Sample[]; up: Record<string, boolean[]>; lastAdvise: number; verdicts: Record<string, string> }

async function poll(host: Host, rt: Runtime, st: PollState): Promise<void> {
  const now = await host.now()
  const present = (await firstExisting(host, detectPaths(rt.backendDir, rt.home))) !== null
  if (!present) {
    await publish(host, rt, viewOf({ present: false, families: {}, conformance: null, verdicts: {}, handoff: null, lanes: null, drainEtaS: null, health: null }))
    return
  }
  const bin = await firstExisting(host, apexCandidates(rt.backendDir, rt.home))
  let pressure = bin === null ? null : await jsonOf(host, [bin, 'pressure', '--json'], 15_000)
  const proxyUp = pressure !== null
  if (pressure === null) pressure = await readJson(host, `${rt.backendDir}/pressure.json`)
  if (bin !== null && now - st.lastAdvise >= ADVISE_EVERY_MS) {
    const advice = await jsonOf(host, [bin, 'route-advise', '--json'], 20_000)
    if (advice !== null) st.verdicts = parseVerdicts(advice)
    st.lastAdvise = now
  }
  const conformance = conformanceRate(jsonLines(await tailLines(host, `${rt.backendDir}/conformance.jsonl`, 200)))
  const handoff = parseHandoff(await readJson(host, `${rt.backendDir}/handoff_threshold.json`))
  const lanes = laneStatsOf(jsonLines(await tailLines(host, `${rt.home}/.apex/offload_telemetry.jsonl`, 2000)), now / 1000)
  const jobs = `${rt.backendDir}/queue/jobs`
  let inbox: number | null = null
  try {
    inbox = (await host.list(`${jobs}/inbox`)).length
  } catch {
    inbox = null
  }
  const finished = (await finishedSince(host, `${jobs}/done`, now - HOUR_MS)) + (await finishedSince(host, `${jobs}/failed`, now - HOUR_MS))
  if (inbox !== null) st.inbox = [...st.inbox, { t: now, inbox }].slice(-SAMPLES_MAX)
  const sample = (name: string, up: boolean): void => {
    st.up[name] = [...(st.up[name] ?? []), up].slice(-SAMPLES_MAX)
  }
  sample('proxy', proxyUp || Object.keys(parsePressure(pressure)).length > 0)
  const ornith = rt.breakers.ornith
  sample('ornith', ornith === undefined || breakerState(ornith, now) !== 'open')
  if (inbox !== null) sample('worker', inbox === 0 || finished > 0)
  await publish(
    host,
    rt,
    viewOf({
      present: true,
      families: parsePressure(pressure),
      conformance,
      verdicts: st.verdicts,
      handoff,
      lanes,
      drainEtaS: drainEtaOf(st.inbox, finished, now),
      health: healthOf(st.up),
    }),
  )
}

async function publish(host: Host, rt: Runtime, view: BackendView): Promise<void> {
  rt.backend = view
  if (rt.profile.backend !== view.present) {
    rt.profile = { ...rt.profile, backend: view.present }
    rt.profileDirty = true
  }
  await host.publish.backend(view)
}

/** session.start: one poll now (with route-advise), then every 60 s. */
export async function start(host: Host, rt: Runtime): Promise<void> {
  const st: PollState = { inbox: [], up: {}, lastAdvise: Number.NEGATIVE_INFINITY, verdicts: {} }
  try {
    await poll(host, rt, st)
  } catch {
    // fail open: an absent or broken backend is an absent backend
  }
  host.every(POLL_MS, () => {
    void poll(host, rt, st).catch(() => undefined)
  })
}
```

- [ ] **Step 4: Wire it into `register.ts`**

`plugin/hooks/register.ts` — replace the import block (every line above `// Wiring only.`) with:

```ts
import type { EngineInterface, Register } from 'claude-code'

import { start as backendStart } from './backend.ts'
import { install as band, measureOf } from './band.tsx'
import type { Host } from './host.ts'
import { describe as injectDescribe, skillSection, start as injectStart } from './inject.ts'
import {
  flushAll,
  install as observe,
  measure as observeMeasure,
  skill as observeSkill,
  start as observeStart,
} from './observe.ts'
import { afterSpawn, beforeSpawn, onComplete, start as routerStart, type Decision } from './router.ts'
import { identify, newRuntime, optionsOf } from './runtime.ts'
```

In the same file, the `session.start` hook becomes:

```ts
  on('session.start', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await identify(host, e, rt)
    await observeStart(host, e, rt)
    await routerStart(host, rt)
    await injectStart(host, rt)
    await backendStart(host, rt)
    return r
  })
```

- [ ] **Step 5: Run the tests**

Run: `claude plugin test plugin`
Expected: `91 pass`, `0 fail`.

- [ ] **Step 6: Full suite, validate, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1758 passed, 4 skipped`; `claude plugin validate plugin` → passed.

```bash
git add plugin/hooks/backend.ts plugin/hooks/register.ts plugin/tests/backend.test.ts
git commit -m "feat(plugin): backend boundary — detection, pressure/route-advise poll, lanes, drain, health"
```

---

### Task 16: `live.ts` — the per-minute tick: pressure level, burn, budget, breakers, admission, anomaly

**Files:**
- Create: `plugin/hooks/live.ts`, `plugin/tests/live.test.ts`
- Modify: `plugin/hooks/register.ts` (imports; a `lastMeasure` variable kept by `session.measure`; `session.start` calls `liveStart`)

**Interfaces:**
- Consumes: Task 8 anomaly (`correlationOf`, `pcaFit`, `qResidual`, `tSquared`, `explain`, `zscore`, `WARMUP_N`, `HIST_MAX`, `Card`), Task 7 (`welfordCovNew`, `welfordCovPush`, `covariance`), Task 8 cost (`budgetBurn`), Task 10 signals, `toastOnce`, `EMPTY_SIGNALS`, `Host`, `Runtime`.
- Produces: `TICK_MS = 60_000`, `REFIT_EVERY = 50`, `FEATURES = 5`, `anomalyStep(model, x): { model; card }`, `asAnomaly(v): AnomalyModel | null`, `tick(rt, now, measure, prev): { view: SignalsView; toasts: [kind, text][] }`, `start(host, rt, measureNow)`.

What one tick does (spec §6–§7): closes the current minute into the 60-minute window; transport burn over step failures (`stopReason === null`) with slo 0.97, alert iff burn(5m) > 10 ∧ burn(60m) > 6; observed level = worst of the rate-limit level (≥ 70% AMBER, ≥ 90% RED), the burn alert (AMBER) and the backend's per-family levels (429 pressure comes from the backend; the plugin alone cannot tell a 429 from a transport failure); the level moves with enter 2 / exit 3 hysteresis; non-GREEN minutes count into the profile's hour-of-day histogram; budget burn (short 5 min, long 60 min, and the whole session's minutes to exhaustion) when `budgetUsd > 0`; lane breakers from the Bash events observe queued (failure = a signal pattern or an error); the admission rate from the rate-limit slope; the session anomaly from the main-loop step features `[cache_read, tokens_out, ttft, tool_calls, step_ms]` (PCA disabled until 50 samples, refit every 50, persisted as parameters only every 10 ticks); fan-out risk when ≥ 3 dispatches run. Toasts (each kind at most once per 10 minutes): RED pressure, a lane breaker opening, budget burn(short) > 10 and burn(long) > 6.

- [ ] **Step 1: Write the failing tests**

`plugin/tests/live.test.ts`:

```ts
import { describe, expect, test } from 'claude-code/testing'

import type { Measure } from '../types/index.d.ts'
import { anomalyStep, asAnomaly, tick } from '../hooks/live.ts'
import { newRuntime, optionsOf } from '../hooks/runtime.ts'
import { EMPTY_SIGNALS } from '../hooks/state.ts'
import { MEASURE, SESSION, stepInput } from './fixtures/inputs.ts'
import { T0, drain, worldOf } from './fixtures/world.ts'

const M = (limitPercent: number | null, costUsd: number | null = null): Measure => ({
  ctxPercent: 10, limitKind: limitPercent === null ? null : 'five_hour', limitPercent, resetsAt: '2026-10-03T20:00:00Z', costUsd,
})
const rtAt = (raw = {}) => {
  const rt = newRuntime(optionsOf(raw))
  rt.startedAt = T0
  return rt
}
const steady = (i: number): number[] => [1000 + (i % 7) * 10, 50 + (i % 5) * 3, 800 + (i % 3) * 20, i % 2, 900 + (i % 4) * 15]

describe('live: anomaly', () => {
  test('silent through warm-up, then an outlier scores at the top with its term', () => {
    let model = null
    for (let i = 0; i < 49; i++) {
      const r = anomalyStep(model, steady(i))
      expect(r.card).toBeNull()
      model = r.model
    }
    for (let i = 49; i < 70; i++) model = anomalyStep(model, steady(i)).model
    const out = anomalyStep(model, [90000, 5000, 9000, 40, 60000])
    expect(out.card!.score).toBe(1)
    expect(['Q', 'T2']).toContain(out.card!.term)
    expect(out.model.vectors).toHaveLength(2)
    expect(out.model.cov.n).toBe(71)
  })

  test('persisted models are validated', () => {
    expect(asAnomaly('garbage')).toBeNull()
    const { model } = anomalyStep(null, steady(0))
    expect(asAnomaly(JSON.parse(JSON.stringify(model)))).toEqual(model)
  })
})

describe('live: tick', () => {
  test('rate-limit pressure: RED after two windows, one toast', () => {
    const rt = rtAt()
    let v = tick(rt, T0 + 60_000, M(95), EMPTY_SIGNALS)
    expect(v.view.level).toBe('GREEN')
    expect(v.toasts).toEqual([])
    v = tick(rt, T0 + 120_000, M(95), v.view)
    expect(v.view.level).toBe('RED')
    expect(v.toasts).toEqual([['red', 'datapce: pressure RED — serialize heavy fan-out']])
    expect(rt.profileDirty).toBe(true)
  })

  test('no rate limits (API key): level from burn and backend only', () => {
    const rt = rtAt()
    expect(tick(rt, T0 + 60_000, M(null), EMPTY_SIGNALS).view.level).toBe('GREEN')
    rt.backend = { ...rt.backend, families: { opus: 'AMBER' } }
    tick(rt, T0 + 120_000, M(null), EMPTY_SIGNALS)
    expect(tick(rt, T0 + 180_000, M(null), EMPTY_SIGNALS).view.level).toBe('AMBER')
  })

  test('three lane failures open the breaker and toast', () => {
    const rt = rtAt()
    for (let i = 0; i < 3; i++) rt.bashEvents.push({ cmd: 'ornith', signal: 'OrnithBusy', isError: true, t: T0 + i })
    const v = tick(rt, T0 + 60_000, M(10), EMPTY_SIGNALS)
    expect(v.view.breakers).toEqual({ ornith: 'open' })
    expect(v.toasts).toContainEqual(['breaker:ornith', 'datapce: ornith lane breaker open — escalate for 10 min'])
  })

  test('budget burn over both windows toasts', () => {
    const rt = rtAt({ budgetUsd: 10 })
    let v = { view: EMPTY_SIGNALS, toasts: [] as [string, string][] }
    for (let i = 1; i <= 5; i++) {
      rt.minute.spend = 0.5
      v = tick(rt, T0 + i * 60_000, M(10, 0.5 * i), v.view)
    }
    expect(v.view.budgetBurnShort!).toBeGreaterThan(10)
    expect(v.view.budgetBurnLong!).toBeGreaterThan(6)
    expect(v.toasts).toContainEqual(['budget', 'datapce: budget burning 72× (5 min) / 72× (60 min)'])
    expect(v.view.minutesToExhaust).not.toBeNull()
    expect(v.view.minutesToExhaust!).toBeGreaterThanOrEqual(0)
  })

  test('admission rate follows the rate-limit slope', () => {
    const rt = rtAt()
    rt.limitHistory = [{ t: T0, pct: 40 }, { t: T0 + 600_000, pct: 50 }]
    rt.heavySpawns = [1, 2, 3, 4, 5].map(i => T0 + i * 60_000)
    const v = tick(rt, T0 + 600_000, M(50), EMPTY_SIGNALS)
    expect(v.view.admissionRate).toBe(0.2)
    expect(rt.bucket.rate).toBe(0.2)
  })
})

describe('live: hooks', () => {
  test('a RED rate limit toasts once from the timer', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure({ ...MEASURE, rateLimits: [{ kind: 'five_hour', percentUsed: 95, resetsAt: '2026-10-03T20:00:00Z' }] })
    await world.clock.advance(60_000)
    expect(world.toasts).toEqual([])
    await world.clock.advance(60_000)
    expect(world.toasts).toEqual(['datapce: pressure RED — serialize heavy fan-out'])
    await world.clock.advance(180_000)
    expect(world.toasts).toEqual(['datapce: pressure RED — serialize heavy fan-out'])
  })

  test('the anomaly model is persisted as parameters every 10 ticks', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await drain($.turn.step(stepInput()))
    await world.clock.advance(9 * 60_000)
    expect(world.store.get('datapce.anomaly')).toBeUndefined()
    await world.clock.advance(60_000)
    const saved = world.store.get('datapce.anomaly') as { cov: { n: number }; qHist: number[] }
    expect(saved.cov.n).toBe(1)
    expect(saved.qHist).toEqual([])
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/live.ts` cannot be resolved.

- [ ] **Step 3: Write `live.ts`**

`plugin/hooks/live.ts`:

```ts
// Live signals (§6–§7): one tick per minute closes the counters into windows and recomputes what
// the band, pane, router and injection read. Toasts here: RED pressure, a lane breaker opening,
// budget burn(short) > 10 ∧ burn(long) > 6 — each at most once per 10 minutes (toastOnce).
import type { AnomalyModel, Level, Measure, SignalsView } from '../types/index.d.ts'
import { correlationOf, explain, HIST_MAX, pcaFit, qResidual, tSquared, WARMUP_N, zscore, type Card } from './core/anomaly.ts'
import { budgetBurn } from './core/cost.ts'
import { covariance, welfordCovNew, welfordCovPush } from './core/stats.ts'
import type { Host } from './host.ts'
import { toastOnce, type Runtime } from './runtime.ts'
import {
  admissionRate, breakerNew, breakerRecord, breakerState, burnAlert, fanoutRisk, levelStep, limitLevel, LONG_LIMIT, SHORT_LIMIT,
  SLO_TRANSPORT, spendBurn, worst,
} from './signals.ts'
import { EMPTY_SIGNALS } from './state.ts'

export const TICK_MS = 60_000
export const REFIT_EVERY = 50
export const FEATURES = 5
const WINDOW_MIN = 60
const SLOPE_MS = 10 * 60_000

export function anomalyStep(model: AnomalyModel | null, x: readonly number[]): { model: AnomalyModel; card: Card | null } {
  const base: AnomalyModel = model ?? { cov: welfordCovNew(x.length), vectors: [], values: [], sd: [], fittedAt: 0, qHist: [], t2Hist: [] }
  const cov = welfordCovPush(base.cov, x)
  let m: AnomalyModel = { ...base, cov }
  if (cov.n < WARMUP_N) return { model: m, card: null }
  if (m.vectors.length === 0 || cov.n - m.fittedAt >= REFIT_EVERY) {
    const { corr, sd } = correlationOf(covariance(cov))
    const { values, vectors } = pcaFit(corr, 2)
    m = { ...m, values, vectors, sd, fittedAt: cov.n }
  }
  const z = zscore(x, cov.mean, m.sd)
  const q = qResidual(z, m.vectors)
  const t2 = tSquared(z, m.vectors, m.values)
  const card = explain(q, t2, m.qHist, m.t2Hist)
  return { model: { ...m, qHist: [...m.qHist, q].slice(-HIST_MAX), t2Hist: [...m.t2Hist, t2].slice(-HIST_MAX) }, card }
}

const nums = (v: unknown): v is number[] => Array.isArray(v) && v.every(x => typeof x === 'number' && Number.isFinite(x))
const matrix = (v: unknown): v is number[][] => Array.isArray(v) && v.every(nums)

export function asAnomaly(v: unknown): AnomalyModel | null {
  if (v === null || typeof v !== 'object') return null
  const m = v as Record<string, unknown>
  const cov = m.cov as Record<string, unknown> | null
  const ok =
    cov !== null && typeof cov === 'object' && typeof cov.n === 'number' && nums(cov.mean) && cov.mean.length === FEATURES &&
    matrix(cov.c) && matrix(m.vectors) && nums(m.values) && nums(m.sd) && typeof m.fittedAt === 'number' && nums(m.qHist) && nums(m.t2Hist)
  return ok ? (v as AnomalyModel) : null
}

export function tick(rt: Runtime, now: number, m: Measure | null, prev: SignalsView): { view: SignalsView; toasts: [string, string][] } {
  const toasts: [string, string][] = []
  rt.minutes = [...rt.minutes, rt.minute].slice(-WINDOW_MIN)
  rt.minute = { steps: 0, failed: 0, spend: 0 }

  const burn = burnAlert(rt.minutes.map(b => [b.steps, b.failed] as const), SLO_TRANSPORT)
  const observed: Level = worst([limitLevel(m?.limitPercent ?? null), burn.alert ? 'AMBER' : 'GREEN', worst(Object.values(rt.backend.families))])
  const before = rt.level.level
  rt.level = levelStep(rt.level, observed)
  if (rt.level.level === 'RED' && before !== 'RED') toasts.push(['red', 'datapce: pressure RED — serialize heavy fan-out'])
  if (rt.level.level !== 'GREEN') {
    const hours = [...rt.profile.hours]
    const h = new Date(now).getHours()
    hours[h] = (hours[h] ?? 0) + 1
    rt.profile = { ...rt.profile, hours }
    rt.profileDirty = true
  }

  const wasOpen = new Set(Object.entries(rt.breakers).filter(([, b]) => breakerState(b, now) === 'open').map(([k]) => k))
  for (const ev of rt.bashEvents.splice(0)) rt.breakers[ev.cmd] = breakerRecord(rt.breakers[ev.cmd] ?? breakerNew(), ev.t, ev.signal === null && !ev.isError)
  const breakers = Object.fromEntries(Object.entries(rt.breakers).map(([k, b]) => [k, breakerState(b, now)]))
  for (const [k, state] of Object.entries(breakers)) {
    if (state === 'open' && !wasOpen.has(k)) toasts.push([`breaker:${k}`, `datapce: ${k} lane breaker open — escalate for 10 min`])
  }

  const budget = rt.options.budgetUsd
  const spend = rt.minutes.map(b => b.spend)
  const short = spendBurn(spend, budget, 5)
  const long = spendBurn(spend, budget, WINDOW_MIN)
  const whole = m?.costUsd != null ? budgetBurn(m.costUsd, Math.max(1, (now - rt.startedAt) / 60_000), budget) : null
  if (short !== null && long !== null && short > SHORT_LIMIT && long > LONG_LIMIT) {
    toasts.push(['budget', `datapce: budget burning ${short.toFixed(0)}× (5 min) / ${long.toFixed(0)}× (60 min)`])
  }

  const hist = rt.limitHistory.filter(s => now - s.t <= SLOPE_MS)
  const first = hist[0]
  const last = hist[hist.length - 1]
  const slope = first !== undefined && last !== undefined && last.t > first.t ? (last.pct - first.pct) / ((last.t - first.t) / 60_000) : 0
  const minutesToReset = m?.resetsAt != null ? (Date.parse(m.resetsAt) - now) / 60_000 : 0
  const spawnsPerMin = rt.heavySpawns.filter(t => now - t <= SLOPE_MS).length / 10
  const rate = admissionRate(spawnsPerMin, slope, 100 - (m?.limitPercent ?? 0), minutesToReset)
  rt.bucket = { ...rt.bucket, rate }

  let card: Card | null = null
  for (const x of rt.stepFeatures.splice(0)) {
    if (x.length !== FEATURES) continue
    const r = anomalyStep(rt.anomaly, x)
    rt.anomaly = r.model
    card = r.card ?? card
  }

  let steps = 0
  let failed = 0
  for (const b of rt.minutes) {
    steps += b.steps
    failed += b.failed
  }
  const running = [...rt.dispatches.values()].filter(d => d.outcome === 'running').length

  return {
    view: {
      level: rt.level.level,
      burnShort: burn.short,
      burnLong: burn.long,
      budgetBurn: whole?.burn ?? null,
      budgetBurnShort: short,
      budgetBurnLong: long,
      minutesToExhaust: whole?.minutesToExhaust ?? null,
      breakers,
      anomaly: card === null ? prev.anomaly : { score: card.score, term: card.term },
      fanoutRisk: running >= 3 ? fanoutRisk(steps > 0 ? failed / steps : 0, running) : null,
      admissionRate: rate,
    },
    toasts,
  }
}

/** session.start (after backend): restore the anomaly parameters, then tick every minute. */
export async function start(host: Host, rt: Runtime, measureNow: () => Measure | null): Promise<void> {
  try {
    rt.anomaly = asAnomaly(await host.storeGet('datapce.anomaly'))
  } catch {
    rt.anomaly = null
  }
  let prev: SignalsView = EMPTY_SIGNALS
  let ticks = 0
  host.every(TICK_MS, () => {
    void (async () => {
      const now = await host.now()
      const { view, toasts } = tick(rt, now, measureNow(), prev)
      prev = view
      for (const [kind, text] of toasts) toastOnce(host, rt, kind, text, now)
      await host.publish.signals(view)
      ticks += 1
      if (ticks % 10 === 0 && rt.anomaly !== null) await host.storeSet('datapce.anomaly', rt.anomaly)
    })().catch(() => undefined)
  })
}
```

- [ ] **Step 4: Wire it into `register.ts`**

`plugin/hooks/register.ts` — replace the import block (every line above `// Wiring only.`) with:

```ts
import type { EngineInterface, Register } from 'claude-code'

import type { Measure } from '../types/index.d.ts'
import { start as backendStart } from './backend.ts'
import { install as band, measureOf } from './band.tsx'
import type { Host } from './host.ts'
import { describe as injectDescribe, skillSection, start as injectStart } from './inject.ts'
import { start as liveStart } from './live.ts'
import {
  flushAll,
  install as observe,
  measure as observeMeasure,
  skill as observeSkill,
  start as observeStart,
} from './observe.ts'
import { afterSpawn, beforeSpawn, onComplete, start as routerStart, type Decision } from './router.ts'
import { identify, newRuntime, optionsOf } from './runtime.ts'
```

`plugin/hooks/register.ts` — replace everything from `export const register` to the end of the file with (the state refs and `hostOf` above it stay as they are):

```ts
export const register: Register = (on, raw) => {
  const rt = newRuntime(optionsOf(raw))
  let lastMeasure: Measure | null = null

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await identify(host, e, rt)
    await observeStart(host, e, rt)
    await routerStart(host, rt)
    await injectStart(host, rt)
    await backendStart(host, rt)
    await liveStart(host, rt, () => lastMeasure)
    return r
  })

  on('session.end', async ($, e, next) => {
    await flushAll(hostOf($), rt)
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const host = hostOf($)
    lastMeasure = measureOf(e)
    await host.publish.measure(lastMeasure)
    observeMeasure(rt, e, await host.now())
    return next(e)
  })

  on('skill.prompt', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    const now = await host.now()
    observeSkill(rt, e.skill, now)
    return { ...r, text: await skillSection(host, rt, e.skill, r.text, now) }
  })

  on('tool.describe', { tool: ['Agent', 'Workflow'] }, async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    return { ...r, description: await injectDescribe(host, rt, e.tool, r.description, await host.now()) }
  })

  on('agent.spawn', async ($, e, next) => {
    const host = hostOf($)
    let d: Decision | null = null
    try {
      d = beforeSpawn(rt, e, await host.now())
    } catch {
      d = null
    }
    if (d === null) return next(e)
    const res = await next(d.apply !== null ? { ...e, model: d.apply } : e)
    await afterSpawn(host, rt, e, d, res, await host.now())
    return res
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await onComplete(host, rt, e, await host.now())
    return r
  })

  band(on, rt)
  observe(on, rt)
}
```

- [ ] **Step 5: Run the tests**

Run: `claude plugin test plugin`
Expected: `100 pass`, `0 fail`.

- [ ] **Step 6: Full suite, validate, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1758 passed, 4 skipped`; `claude plugin validate plugin` → passed.

```bash
git add plugin/hooks/live.ts plugin/hooks/register.ts plugin/tests/live.test.ts
git commit -m "feat(plugin): live signals — level hysteresis, burn, budget, breakers, admission, session anomaly"
```

---

### Task 17: The full band, the status entry, and the handoff toast

**Files:**
- Create: `plugin/hooks/handoff.ts`, `plugin/tests/band_full.test.tsx`, `tests/test_handoff_template_plugin.py`
- Modify: `plugin/hooks/band.tsx` (whole file), `plugin/tests/band.test.tsx` (one expected string), `plugin/hooks/register.ts` (imports; `session.measure` refreshes the status and checks the handoff threshold)

**Interfaces:**
- Consumes: `tierOf` (Task 11), `toastOnce` (Task 1), contract views, `Host`, `Runtime`.
- Produces:
  - `band.tsx`: everything from Task 1 plus `BandInput`, `bandLine(i: BandInput): string`, `statusText(level, costUsd): string`, `refreshStatus(host, rt, m)`.
  - `handoff.ts`: `HANDOFF_TEMPLATE` (byte-for-byte `apex_router.handoff_state.render_template()`), `fmtTok(t)`, `handoffDue(cacheRead, threshold)`, `handoffToast(threshold)`, `checkHandoff(host, rt, now)`.

Band (spec §6), one line, appears on the first signal (a measurement or a dispatch), `Hide` button, yields to a survey: `apex ●LEVEL <tightest window>%  ctx <meter> N%  $cost[/budget]  routes n (local n ✓ok ↑esc) ▲differs  conf N%  enforce:READY/total`. Cells with nothing behind them are omitted (no backend → no `local`, no `conf`, per §12 "terminal, no backend: ✓ (no conf/local)"). `▲` counts dispatches whose advised tier differs from the tier that ran. Status (`$.ui.status`) carries `apex ●LEVEL $cost` on every surface that draws (§12: not under `claude -p`, whose `surface` is `null`). Handoff toast (§6, replaces the Stop-hook nudge): when the backend's `handoff_threshold.json` exists and this session's main-loop cache reads pass it; `/apex handoff` (Task 18) hands the model the same structured-state block the hook embedded.

- [ ] **Step 1: Write the failing tests**

`plugin/tests/band_full.test.tsx`:

```tsx
import { describe, expect, test } from 'claude-code/testing'

import type { CellView, Dispatch } from '../types/index.d.ts'
import { bandLine, statusText } from '../hooks/band.tsx'
import { fmtTok, handoffDue, HANDOFF_TEMPLATE } from '../hooks/handoff.ts'
import { EMPTY_BACKEND, EMPTY_SIGNALS } from '../hooks/state.ts'
import { BAND, MEASURE, SESSION, spawnInput, stepInput } from './fixtures/inputs.ts'
import { BACKEND, drain, worldOf } from './fixtures/world.ts'

const dispatch = (advised: Dispatch['advised'], resolved: string): Dispatch => ({
  toolUseId: 't', agentId: null, description: 'd', subagentType: 'Explore', taskType: 'explore', requested: 'inherit', resolved,
  advised, advisedState: advised === null ? null : 'READY', applied: false, basis: '', outcome: 'running', startedAt: 0,
  durationMs: null, tokens: null, level: 'GREEN',
})
const cell = (state: CellView['state']): CellView => ({ key: 'k', taskType: 'explore', level: 'GREEN', tier: 'sonnet', state, n: 1, pass: 1, wilsonLo: 0, tokMean: null })

describe('band line', () => {
  test('reproduces the spec §6 example', () => {
    const line = bandLine({
      m: { ctxPercent: 61, limitKind: 'five_hour', limitPercent: 62, resetsAt: null, costUsd: 4.12 },
      s: { ...EMPTY_SIGNALS, level: 'AMBER' },
      ds: [
        dispatch('sonnet', 'claude-opus-5-5'), dispatch('haiku', 'claude-sonnet-5-5'), dispatch('sonnet', 'claude-sonnet-5-5'),
        dispatch(null, 'claude-opus-5-5'), dispatch(null, 'claude-opus-5-5'), dispatch(null, 'claude-opus-5-5'), dispatch(null, 'claude-opus-5-5'),
      ],
      b: { ...EMPTY_BACKEND, present: true, conformance: 0.86, lanes: { codegen: { n: 4, ok: 3, escalated: 1 } } },
      cells: [cell('READY'), cell('READY'), ...Array.from({ length: 6 }, () => cell('WARMING'))],
      budgetUsd: 10,
    })
    expect(line).toBe('apex ●AMBER 5h:62%  ctx ▇▇▇▇▇░░░ 61%  $4.12/10  routes 7 (local 4 ✓3 ↑1) ▲2  conf 86%  enforce:2/8')
  })

  test('no backend: no local, no conf', () => {
    expect(bandLine({ m: null, s: EMPTY_SIGNALS, ds: [dispatch(null, 'claude-opus-5-5')], b: EMPTY_BACKEND, cells: [], budgetUsd: 0 })).toBe('apex ●GREEN  routes 1')
  })

  test('status text and token formatting', () => {
    expect(statusText('AMBER', 4.12)).toBe('apex ●AMBER $4.12')
    expect(statusText('GREEN', null)).toBe('apex ●GREEN')
    expect(fmtTok(43_860_328)).toBe('43.9M')
    expect(fmtTok(1000)).toBe('1k')
    expect(handoffDue(10, null)).toBe(false)
    expect(handoffDue(1000, 1000)).toBe(true)
  })

  test('the handoff template carries all six fields', () => {
    for (const f of ['goal', 'constraints', 'decisions', 'files_touched', 'open_issues', 'next_action']) expect(HANDOFF_TEMPLATE).toContain(`- **${f}**: _…_`)
  })
})

describe('band, status, handoff: hooks', () => {
  test('pressure reaches the band through the live tick', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure({ ...MEASURE, rateLimits: [{ kind: 'five_hour', percentUsed: 95, resetsAt: '2026-10-03T20:00:00Z' }] })
    await world.clock.advance(120_000)
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'terminal', ...BAND })
    expect((await ui.find({ key: 'datapce-band-line' }))?.text).toBe('apex ●RED 5h:95%  ctx ▇▇▇▇▇░░░ 61%  $4.12')
    await ui.unmount()
  })

  test('a dispatch alone shows the band', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    const ui = await $.ui.mount({ plugin: 'datapce', surface: 'desktop', ...BAND })
    expect((await ui.find({ key: 'datapce-band-line' }))?.text).toBe('apex ●GREEN  routes 1')
    await ui.unmount()
  })

  test('status on a drawing surface, none under claude -p', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.session.measure(MEASURE)
    expect(world.statuses).toEqual(['apex ●GREEN $4.12'])
  })

  test('claude -p (no surface) gets no status', async ($, on) => {
    const world = worldOf(on)
    await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
    await $.session.measure(MEASURE)
    expect(world.statuses).toEqual([])
  })

  test('crossing the backend handoff threshold toasts the handoff', async ($, on) => {
    const world = worldOf(on, { [`${BACKEND}/pressure.json`]: '{"families": {}}', [`${BACKEND}/handoff_threshold.json`]: '{"threshold_tokens": 1000}' })
    await $.session.start(SESSION)
    await drain($.turn.step(stepInput()))
    await $.session.measure(MEASURE)
    expect(world.toasts).toEqual(["datapce: this session's cache reads passed 1k tokens (your p80) — run /apex handoff, fill the block, start fresh"])
  })
})
```

In `plugin/tests/band.test.tsx`, the expected band text of the test "draws on terminal and desktop once measured; Hide hides it" becomes:

```ts
      expect((await ui.find({ key: 'datapce-band-line' }))?.text).toBe('apex ●GREEN 5h:62%  ctx ▇▇▇▇▇░░░ 61%  $4.12')
```

`tests/test_handoff_template_plugin.py`:

```python
"""The plugin's /apex handoff block is byte-for-byte apex_router.handoff_state.render_template()."""
from pathlib import Path

from apex_router.handoff_state import render_template

ROOT = Path(__file__).resolve().parents[1]
START = "export const HANDOFF_TEMPLATE = `"
END = "`\n// END HANDOFF_STATE_TEMPLATE"


def test_plugin_embed_matches_module():
    text = (ROOT / "plugin" / "hooks" / "handoff.ts").read_text()
    embedded = text[text.index(START) + len(START):text.index(END)]
    assert embedded == render_template(), "re-embed: python -m apex_router.handoff_state template"
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin` → FAIL (`../hooks/handoff.ts` cannot be resolved).
Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_handoff_template_plugin.py` → FAIL (`FileNotFoundError`).

- [ ] **Step 3: Write `handoff.ts`**

`plugin/hooks/handoff.ts`:

```ts
// The structured handoff (spec §6: replaces the Stop-hook nudge; same structured-state instruction).
import type { Host } from './host.ts'
import { toastOnce, type Runtime } from './runtime.ts'

// BEGIN HANDOFF_STATE_TEMPLATE — byte-for-byte apex_router.handoff_state.render_template();
// tests/test_handoff_template_plugin.py guards it.
export const HANDOFF_TEMPLATE = `## Structured continuation state

Fill this in BEFORE stopping — once FILLED, it is the only context the fresh session
needs. Record current state, not history: discard reasoning, keep decisions and facts.
If any field is still _…_ the handoff is INCOMPLETE — do not paste it; fill it
first. Check with: python -m apex_router.handoff_state validate <this file>.
(SKILL.state handoff — schema: apex_router.handoff_state.FIELDS)

- **goal**: _…_ one sentence: what this session is trying to achieve
- **constraints**: _…_ hard requirements and the do-not-touch list
- **decisions**: _…_ decisions ALREADY made, each with its reason — do not re-litigate
- **files_touched**: _…_ paths created/modified, one per line, with what changed
- **open_issues**: _…_ unresolved problems, failing tests, blockers
- **next_action**: _…_ the single step the fresh session should take FIRST
`
// END HANDOFF_STATE_TEMPLATE

export const fmtTok = (t: number): string => (t >= 1e6 ? `${(t / 1e6).toFixed(1)}M` : `${Math.round(t / 1e3)}k`)

export const handoffDue = (cacheRead: number, threshold: number | null): boolean => threshold !== null && cacheRead >= threshold

export const handoffToast = (threshold: number): string =>
  `datapce: this session's cache reads passed ${fmtTok(threshold)} tokens (your p80) — run /apex handoff, fill the block, start fresh`

export function checkHandoff(host: Host, rt: Runtime, now: number): void {
  const threshold = rt.backend.handoffTokens
  if (threshold !== null && handoffDue(rt.cacheRead, threshold)) toastOnce(host, rt, 'handoff', handoffToast(threshold), now)
}
```

- [ ] **Step 4: Replace `band.tsx`**

`plugin/hooks/band.tsx`:

```tsx
import type { On, SessionMeasureInput } from 'claude-code'
import { atom, read, update } from 'claude-code'

import type { BackendView, CellView, Dispatch, Level, Measure, SignalsView } from '../types/index.d.ts'
import type { Host } from './host.ts'
import type { Runtime } from './runtime.ts'
import { EMPTY_BACKEND, EMPTY_SIGNALS } from './state.ts'
import { tierOf } from './tiers.ts'

const measure = atom({ plugin: 'datapce', key: 'measure' } as const, null as Measure | null)
const signals = atom({ plugin: 'datapce', key: 'signals' } as const, EMPTY_SIGNALS)
const dispatches = atom({ plugin: 'datapce', key: 'dispatches' } as const, [] as Dispatch[])
const backend = atom({ plugin: 'datapce', key: 'backend' } as const, EMPTY_BACKEND)
const cells = atom({ plugin: 'datapce', key: 'cells' } as const, [] as CellView[])
const bandHidden = atom({ plugin: 'datapce', key: 'bandHidden' } as const, false)

const LIMIT_LABEL: Record<string, string> = { five_hour: '5h', seven_day: '7d', spend_limit: 'spend' }

/** The figures the band draws, from one session.measure: the tightest window, context, cost. */
export function measureOf(e: SessionMeasureInput): Measure {
  let tight: SessionMeasureInput['rateLimits'][number] | undefined
  for (const w of e.rateLimits) if (tight === undefined || w.percentUsed > tight.percentUsed) tight = w
  return {
    ctxPercent: e.context.percent ?? null,
    limitKind: tight?.kind ?? null,
    limitPercent: tight?.percentUsed ?? null,
    resetsAt: tight?.resetsAt ?? null,
    costUsd: e.cost?.usd ?? null,
  }
}

export function meter(percent: number, width = 8): string {
  const clamped = Math.max(0, Math.min(100, percent))
  const full = Math.round((clamped / 100) * width)
  return '▇'.repeat(full) + '░'.repeat(width - full)
}

export const usd = (x: number): string => `$${x.toFixed(2)}`

const limitCell = (m: Measure): string | null =>
  m.limitPercent === null ? null : `${LIMIT_LABEL[m.limitKind ?? ''] ?? m.limitKind}:${Math.round(m.limitPercent)}%`

export function measureCells(m: Measure): string[] {
  const out: string[] = []
  const lim = limitCell(m)
  if (lim !== null) out.push(lim)
  if (m.ctxPercent !== null) out.push(`ctx ${meter(m.ctxPercent)} ${Math.round(m.ctxPercent)}%`)
  if (m.costUsd !== null) out.push(usd(m.costUsd))
  return out
}

export type BandInput = {
  m: Measure | null
  s: SignalsView
  ds: readonly Dispatch[]
  b: BackendView
  cells: readonly CellView[]
  budgetUsd: number
}

/** §6: apex ●LEVEL 5h:62%  ctx ▇▇▇▇▇░░░ 61%  $4.12/10  routes 7 (local 4 ✓3 ↑1) ▲2  conf 86%  enforce:2/8 */
export function bandLine(i: BandInput): string {
  const lim = i.m === null ? null : limitCell(i.m)
  const out = [`apex ●${i.s.level}${lim === null ? '' : ` ${lim}`}`]
  if (i.m !== null && i.m.ctxPercent !== null) out.push(`ctx ${meter(i.m.ctxPercent)} ${Math.round(i.m.ctxPercent)}%`)
  if (i.m !== null && i.m.costUsd !== null) out.push(i.budgetUsd > 0 ? `${usd(i.m.costUsd)}/${i.budgetUsd}` : usd(i.m.costUsd))
  if (i.ds.length > 0 || i.b.lanes !== null) {
    let local = ''
    if (i.b.lanes !== null) {
      let n = 0
      let ok = 0
      let esc = 0
      for (const lane of Object.values(i.b.lanes)) {
        n += lane.n
        ok += lane.ok
        esc += lane.escalated
      }
      local = ` (local ${n} ✓${ok} ↑${esc})`
    }
    const differs = i.ds.filter(d => d.advised !== null && d.advised !== tierOf(d.resolved)).length
    out.push(`routes ${i.ds.length}${local}${differs > 0 ? ` ▲${differs}` : ''}`)
  }
  if (i.b.conformance !== null) out.push(`conf ${Math.round(100 * i.b.conformance)}%`)
  if (i.cells.length > 0) out.push(`enforce:${i.cells.filter(c => c.state === 'READY').length}/${i.cells.length}`)
  return out.join('  ')
}

export const statusText = (level: Level, costUsd: number | null): string => `apex ●${level}${costUsd === null ? '' : ` ${usd(costUsd)}`}`

/** §12: every drawing surface gets the status entry; `claude -p` (surface null) does not. */
export function refreshStatus(host: Host, rt: Runtime, m: Measure | null): void {
  if (rt.surface !== null) host.status(statusText(rt.level.level, m?.costUsd ?? null))
}

export function install(on: On, rt: Runtime): void {
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!rt.options.band || e.props.hasSurvey) return next(e)
    const m = await read($, measure)
    const ds = await read($, dispatches)
    if ((m === null && ds.length === 0) || (await read($, bandHidden))) return next(e)
    const line = bandLine({ m, s: await read($, signals), ds, b: await read($, backend), cells: await read($, cells), budgetUsd: rt.options.budgetUsd })
    const { Box, Text, Button } = $.ui.resolve(e)
    return (
      <Box key="datapce-band" flexDirection="row" gap={1}>
        <Box key="datapce-band-line">
          <Text wrap="truncate-end">{line}</Text>
        </Box>
        <Button key="datapce-hide" label="Hide" plain onPress={() => update($, bandHidden, () => true)} />
      </Box>
    )
  })
}
```

- [ ] **Step 5: Wire status and handoff into `register.ts`**

In `plugin/hooks/register.ts`, change the band import line and add the handoff import:

```ts
import { install as band, measureOf, refreshStatus } from './band.tsx'
import { checkHandoff } from './handoff.ts'
```

and the `session.measure` hook becomes:

```ts
  on('session.measure', async ($, e, next) => {
    const host = hostOf($)
    const now = await host.now()
    lastMeasure = measureOf(e)
    await host.publish.measure(lastMeasure)
    observeMeasure(rt, e, now)
    refreshStatus(host, rt, lastMeasure)
    checkHandoff(host, rt, now)
    return next(e)
  })
```

- [ ] **Step 6: Run the tests**

Run: `claude plugin test plugin` → `109 pass`, `0 fail`.
Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_handoff_template_plugin.py` → `1 passed`.

- [ ] **Step 7: Full suite, validate, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1759 passed, 4 skipped`; `claude plugin validate plugin` → passed.

```bash
git add plugin/hooks/band.tsx plugin/hooks/handoff.ts plugin/hooks/register.ts plugin/tests/band.test.tsx plugin/tests/band_full.test.tsx tests/test_handoff_template_plugin.py
git commit -m "feat(plugin): full band, status entry, handoff toast with the structured-state block"
```

---

### Task 18: `pane.tsx` and `/apex` — the five sections, enforce on/off, handoff

**Files:**
- Create: `plugin/hooks/pane.tsx`, `plugin/tests/pane.test.tsx`
- Modify: `plugin/hooks/register.ts` (imports; `session.start` calls `paneStart`; `pane(on, rt)` installed)

**Interfaces:**
- Consumes: `HANDOFF_TEMPLATE` (Task 17), `meter` (Task 1), `kTok`, `MIN_N`/`TARGET` (Tasks 8/11), `record` (Task 12), `tierOf`, `PANE_ID`, empty views, `Host`, `Runtime`.
- Produces: `PaneInput`, `Section = { id: string; title: string; rows: string[] }`, `paneSections(i: PaneInput): Section[]`, `summaryText(sections): string`, `waitingFor(cell, enforce): string`, `start(host, rt)` (registers `/apex`; opens the pane when `pane = auto` on a terminal), `install(on, rt)` (registers `command.run` `{ command: 'apex' }` and `ui.render` `{ component: 'Pane', requestId: 'datapce' }`).

Pane (spec §6; `userConfig.pane = auto|command|off`, default `command`): 1 *Dispatches* — newest first, description · task_type · requested → tier that ran · outcome, duration, tokens, and `▲ advised: basis` when advice differed; a first row with advise-vs-requested agreement and pass rate (§17 acceptance). Dispatches are listed newest first rather than grouped by turn: a spawn's turn id is not on `agent.spawn`'s input (types: `AgentSpawnInput`), so grouping would need a guess. 2 *Signals* — pressure and burn(5m)/burn(60m), per-family levels (backend), breaker per lane, budget burn and time to exhaustion, admission rate, fan-out risk, the session anomaly score with its explain card. 3 *Lanes 24h* — backend only: per lane ok/escalated bars, drain ETA. 4 *Evidence* — per cell: state, n/30 bar, Wilson-lo vs target, tok μ, and what enforce is waiting for. 5 *Profile* — task mix, skills in use, repos, backend health, injection arm and bytes. `/apex` arguments: (none)/`open`, `close`, `enforce on`, `enforce off`, `handoff` (adds `HANDOFF_TEMPLATE` as context the model reads), `json`. With `pane = off`, `/apex` answers the same sections as text and never opens a pane.

- [ ] **Step 1: Write the failing tests**

`plugin/tests/pane.test.tsx`:

```tsx
import { describe, expect, test } from 'claude-code/testing'

import type { Cell, CellView, Dispatch } from '../types/index.d.ts'
import { cellKey } from '../hooks/evidence.ts'
import { HANDOFF_TEMPLATE } from '../hooks/handoff.ts'
import { paneSections, waitingFor, type PaneInput } from '../hooks/pane.tsx'
import { EMPTY_BACKEND, EMPTY_INJECT, EMPTY_PROFILE_STORE, EMPTY_SIGNALS, profileView } from '../hooks/state.ts'
import { command, PANE, SESSION, spawnInput } from './fixtures/inputs.ts'
import { worldOf } from './fixtures/world.ts'

const READY: Cell = { n: 35, pass: 35, state: 'READY', winN: 5, winPass: 5, below: 0, above: 0, cusum: 0, p0: 1 }
const view = (over: Partial<CellView>): CellView => ({ key: 'explore|GREEN|sonnet', taskType: 'explore', level: 'GREEN', tier: 'sonnet', state: 'WARMING', n: 12, pass: 12, wilsonLo: 0.76, tokMean: 41000, ...over })
const empty = (over: Partial<PaneInput> = {}): PaneInput => ({
  ds: [], s: EMPTY_SIGNALS, b: EMPTY_BACKEND, cells: [], profile: profileView(EMPTY_PROFILE_STORE), inject: EMPTY_INJECT, enforce: false, ...over,
})
const d = (over: Partial<Dispatch>): Dispatch => ({
  toolUseId: 't', agentId: 'a', description: 'Find X', subagentType: 'Explore', taskType: 'explore', requested: 'inherit',
  resolved: 'claude-opus-5-5', advised: null, advisedState: null, applied: false, basis: '', outcome: 'ok', startedAt: 0,
  durationMs: 41000, tokens: 12000, level: 'GREEN', ...over,
})

describe('pane: sections', () => {
  test('empty: honest placeholders; no Lanes without a backend', () => {
    const s = paneSections(empty())
    expect(s.map(x => x.title)).toEqual(['Dispatches', 'Signals', 'Evidence', 'Profile'])
    expect(s[0]!.rows).toEqual(['no dispatches yet'])
    expect(s[2]!.rows).toEqual(['no evidence yet — every cell is COLD (advice only)'])
  })

  test('dispatch rows, the ▲ basis, agreement and pass rate', () => {
    const s = paneSections(empty({
      ds: [
        d({ advised: 'sonnet', advisedState: 'READY', basis: 'opus→sonnet: 35/35 pass; GREEN now' }),
        d({ description: 'Review diff', taskType: 'review', outcome: 'failed', resolved: 'claude-sonnet-5-5', requested: 'sonnet' }),
      ],
    }))
    expect(s[0]!.rows).toEqual([
      'agreement 0/1 with advice · pass 1/2',
      'Review diff · review · sonnet→sonnet · failed 41s 12k',
      'Find X · explore · inherit→opus · ok 41s 12k ▲ sonnet: opus→sonnet: 35/35 pass; GREEN now',
    ])
  })

  test('what enforce is waiting for', () => {
    expect(waitingFor(view({ n: 12 }), false)).toBe('needs 18 more labels')
    expect(waitingFor(view({ n: 30, pass: 30, wilsonLo: 0.886 }), false)).toBe('needs Wilson-lo ≥ 0.90 (all-pass reaches it at n = 35)')
    expect(waitingFor(view({ state: 'READY', n: 35 }), false)).toBe('eligible — /apex enforce on')
    expect(waitingFor(view({ state: 'READY', n: 35 }), true)).toBe('enforced')
    expect(waitingFor(view({ state: 'DRIFTING' }), true)).toBe('demoted — advice only until 3 good windows')
  })

  test('lanes appear with a backend', () => {
    const s = paneSections(empty({ b: { ...EMPTY_BACKEND, present: true, lanes: { codegen: { n: 4, ok: 3, escalated: 1 } }, drainEtaS: 386 } }))
    const lanes = s.find(x => x.title === 'Lanes 24h')!
    expect(lanes.rows).toEqual(['codegen ▇▇▇▇▇▇░░ 3/4 ok · 1 escalated', 'queue drains in 386 s'])
  })
})

describe('pane: hooks', () => {
  test('session.start registers /apex and opens nothing by default', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    expect(world.commands).toEqual(['apex'])
    expect(world.opened).toEqual([])
  })

  test('pane = auto opens it at start on a terminal', { options: { pane: 'auto' } }, async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    expect(world.opened).toEqual(['datapce'])
  })

  test('/apex opens the pane, which draws its sections on terminal and desktop', async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    await $.agent.spawn(spawnInput())
    const r = await $.command.run(command('apex'))
    expect(world.opened).toEqual(['datapce'])
    expect(r.text).toBe('datapce: pane open (/apex close closes it)')
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: 'datapce', surface, ...PANE })
      expect(await ui.find({ type: 'Text', text: 'Dispatches' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: /Find pressure gate state file · explore · inherit→opus · running/ })).toBeDefined()
      await ui.unmount()
    }
  })

  test('/apex enforce on persists and takes effect on the next spawn', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', { [cellKey('explore', 'GREEN', 'sonnet')]: READY })
    await $.session.start(SESSION)
    const r = await $.command.run(command('apex', 'enforce on'))
    expect(r.text).toBe('datapce: enforce on — READY cells apply at dispatch; everything else stays advice')
    expect(world.store.get('datapce.enforce')).toBe(true)
    await $.agent.spawn(spawnInput())
    expect(world.spawned[0]?.model).toBe('sonnet')
  })

  test('/apex handoff hands the model the structured block', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    const r = await $.command.run(command('apex', 'handoff'))
    expect(r.context).toEqual([HANDOFF_TEMPLATE])
  })

  test('pane = off answers as text and never opens', { options: { pane: 'off' } }, async ($, on) => {
    const world = worldOf(on)
    await $.session.start(SESSION)
    const r = await $.command.run(command('apex'))
    expect(world.opened).toEqual([])
    expect(r.text?.startsWith('Dispatches\n  no dispatches yet')).toBe(true)
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test plugin`
Expected: FAIL — `../hooks/pane.tsx` cannot be resolved.

- [ ] **Step 3: Write `pane.tsx`**

`plugin/hooks/pane.tsx`:

```tsx
// §6 pane (/apex): Dispatches · Signals · Lanes 24h (backend) · Evidence · Profile.
import type { On } from 'claude-code'
import { atom, read, update } from 'claude-code'

import type { BackendView, CellView, Dispatch, InjectView, ProfileView, SignalsView } from '../types/index.d.ts'
import { meter } from './band.tsx'
import { MIN_N, TARGET } from './core/routing.ts'
import { kTok } from './evidence.ts'
import { HANDOFF_TEMPLATE } from './handoff.ts'
import type { Host } from './host.ts'
import { record } from './observe.ts'
import type { Runtime } from './runtime.ts'
import { EMPTY_BACKEND, EMPTY_INJECT, EMPTY_PROFILE_STORE, EMPTY_SIGNALS, PANE_ID, profileView } from './state.ts'
import { tierOf } from './tiers.ts'

const dispatches = atom({ plugin: 'datapce', key: 'dispatches' } as const, [] as Dispatch[])
const signals = atom({ plugin: 'datapce', key: 'signals' } as const, EMPTY_SIGNALS)
const backend = atom({ plugin: 'datapce', key: 'backend' } as const, EMPTY_BACKEND)
const cells = atom({ plugin: 'datapce', key: 'cells' } as const, [] as CellView[])
const profile = atom({ plugin: 'datapce', key: 'profile' } as const, profileView(EMPTY_PROFILE_STORE))
const inject = atom({ plugin: 'datapce', key: 'inject' } as const, EMPTY_INJECT)
const enforce = atom({ plugin: 'datapce', key: 'enforce' } as const, false)

const DISPATCH_ROWS = 20
const TERM: Record<'Q' | 'T2', string> = { Q: 'off the usual pattern', T2: 'usual pattern, extreme size' }

export type PaneInput = {
  ds: readonly Dispatch[]
  s: SignalsView
  b: BackendView
  cells: readonly CellView[]
  profile: ProfileView
  inject: InjectView
  enforce: boolean
}

export type Section = { id: string; title: string; rows: string[] }

const secs = (ms: number | null): string => (ms === null ? '' : ` ${Math.round(ms / 1000)}s`)
const fixed = (x: number | null, digits = 1): string => (x === null ? '—' : x.toFixed(digits))

function dispatchRows(ds: readonly Dispatch[]): string[] {
  if (ds.length === 0) return ['no dispatches yet']
  const advised = ds.filter(d => d.advised !== null)
  const agree = advised.filter(d => d.advised === tierOf(d.resolved)).length
  const done = ds.filter(d => d.outcome !== 'running')
  const rows = [`agreement ${agree}/${advised.length} with advice · pass ${done.filter(d => d.outcome === 'ok').length}/${done.length}`]
  for (const d of [...ds].reverse().slice(0, DISPATCH_ROWS)) {
    const ran = tierOf(d.resolved) ?? d.resolved ?? '—'
    const tok = d.tokens === null ? '' : ` ${kTok(d.tokens)}`
    const delta = d.advised !== null && d.advised !== tierOf(d.resolved) ? ` ▲ ${d.advised}: ${d.basis}` : ''
    rows.push(`${d.description} · ${d.taskType} · ${d.requested}→${ran} · ${d.outcome}${secs(d.durationMs)}${tok}${delta}`)
  }
  return rows
}

function signalRows(s: SignalsView, b: BackendView): string[] {
  const rows = [`pressure ${s.level} · burn 5m ${fixed(s.burnShort)} / 60m ${fixed(s.burnLong)}`]
  const families = Object.entries(b.families)
  if (families.length > 0) rows.push(`families: ${families.map(([f, l]) => `${f} ${l}`).join(', ')}`)
  const lanes = Object.entries(s.breakers)
  if (lanes.length > 0) rows.push(`breakers: ${lanes.map(([l, st]) => `${l} ${st}`).join(', ')}`)
  if (s.budgetBurn !== null) rows.push(`budget burn ${fixed(s.budgetBurn)}× · exhausts in ${s.minutesToExhaust === null ? '—' : `${Math.round(s.minutesToExhaust)} min`}`)
  rows.push(`heavy spawns ≤ ${fixed(s.admissionRate)}/min`)
  if (s.fanoutRisk !== null) rows.push(`fan-out: P(any slow) ${(100 * s.fanoutRisk).toFixed(1)}%`)
  rows.push(s.anomaly === null ? 'session anomaly: warming up (50 turns)' : `session anomaly ${s.anomaly.score.toFixed(2)} (${s.anomaly.term}: ${TERM[s.anomaly.term]})`)
  return rows
}

function laneRows(b: BackendView): string[] {
  const rows = Object.entries(b.lanes ?? {}).map(([lane, x]) => `${lane} ${meter(x.n > 0 ? (100 * x.ok) / x.n : 0)} ${x.ok}/${x.n} ok · ${x.escalated} escalated`)
  if (b.drainEtaS !== null) rows.push(`queue drains in ${b.drainEtaS} s`)
  return rows.length > 0 ? rows : ['no lane activity in 24 h']
}

export function waitingFor(c: CellView, enforceOn: boolean): string {
  if (c.state === 'READY') return enforceOn ? 'enforced' : 'eligible — /apex enforce on'
  if (c.state === 'DRIFTING') return 'demoted — advice only until 3 good windows'
  if (c.n < MIN_N) return `needs ${MIN_N - c.n} more labels`
  return `needs Wilson-lo ≥ ${TARGET.toFixed(2)} (all-pass reaches it at n = 35)`
}

function evidenceRows(cs: readonly CellView[], enforceOn: boolean): string[] {
  if (cs.length === 0) return ['no evidence yet — every cell is COLD (advice only)']
  return cs.map(
    c =>
      `${c.key} ${c.state} ${meter((100 * Math.min(c.n, MIN_N)) / MIN_N)} ${c.n}/${MIN_N} · Wilson ${c.wilsonLo.toFixed(2)} vs ${TARGET.toFixed(2)} · tok μ ${kTok(c.tokMean)} · ${waitingFor(c, enforceOn)}`,
  )
}

function profileRows(p: ProfileView, b: BackendView, inj: InjectView): string[] {
  const top = (m: Record<string, number>): string =>
    Object.entries(m)
      .sort((x, y) => y[1] - x[1])
      .slice(0, 5)
      .map(([k, n]) => `${k} ${n}`)
      .join(', ') || '—'
  return [
    `task mix: ${top(p.taskMix)}`,
    `skills: ${top(p.skills)}`,
    `repos seen: ${p.repos}`,
    `backend: ${b.present ? `present${b.health === null ? '' : ` · health A=${b.health.toFixed(3)}`}` : 'absent'}`,
    `injection: ${inj.arm} arm · ${inj.bytes} B in ${inj.sections} sections`,
  ]
}

export function paneSections(i: PaneInput): Section[] {
  const out: Section[] = [
    { id: 'dispatches', title: 'Dispatches', rows: dispatchRows(i.ds) },
    { id: 'signals', title: 'Signals', rows: signalRows(i.s, i.b) },
  ]
  if (i.b.present) out.push({ id: 'lanes', title: 'Lanes 24h', rows: laneRows(i.b) })
  out.push({ id: 'evidence', title: 'Evidence', rows: evidenceRows(i.cells, i.enforce) })
  out.push({ id: 'profile', title: 'Profile', rows: profileRows(i.profile, i.b, i.inject) })
  return out
}

export const summaryText = (sections: readonly Section[]): string => sections.map(s => [s.title, ...s.rows.map(r => `  ${r}`)].join('\n')).join('\n')

/** session.start (last): register /apex; open the pane unasked only when pane = auto. */
export async function start(host: Host, rt: Runtime): Promise<void> {
  try {
    await host.registerCommand({
      name: 'apex',
      description: 'datapce: dispatches, signals, lanes, evidence, profile',
      argumentHint: '[open|close|enforce on|enforce off|handoff|json]',
      immediate: true,
    })
    if (rt.options.pane === 'auto' && rt.surface === 'terminal') await host.open({ id: PANE_ID, title: 'datapce' })
  } catch {
    // no command, no pane: the band and status still work
  }
}

export function install(on: On, rt: Runtime): void {
  on('command.run', { command: 'apex' }, async ($, e) => {
    const args = e.args.trim().toLowerCase()
    record(rt, { ev: 'command', ts: await $.clock.now(), command: 'apex', args: args.split(/\s+/)[0] ?? '' })
    if (args === 'enforce on' || args === 'enforce off') {
      rt.enforce = args === 'enforce on'
      await $.store.set('datapce.enforce', rt.enforce)
      await update($, enforce, () => rt.enforce)
      return {
        text: rt.enforce
          ? 'datapce: enforce on — READY cells apply at dispatch; everything else stays advice'
          : 'datapce: enforce off — advice only',
      }
    }
    if (args === 'handoff') {
      return { text: 'datapce: handoff block added — fill every field, then start a fresh session.', context: [HANDOFF_TEMPLATE] }
    }
    const sections = paneSections({
      ds: await read($, dispatches),
      s: await read($, signals),
      b: await read($, backend),
      cells: await read($, cells),
      profile: await read($, profile),
      inject: await read($, inject),
      enforce: rt.enforce,
    })
    if (args === 'json') return { text: JSON.stringify({ enforce: rt.enforce, sections }) }
    if (args === 'close') {
      await $.ui.close({ id: PANE_ID })
      return { text: 'datapce: pane closed' }
    }
    if (rt.options.pane === 'off' || (args !== '' && args !== 'open')) return { text: summaryText(sections) }
    await $.ui.open({ id: PANE_ID, title: 'datapce', focus: true, closeOnEscape: true })
    return { text: 'datapce: pane open (/apex close closes it)' }
  })

  on('ui.render', { component: 'Pane', requestId: 'datapce' }, async ($, e) => {
    const sections = paneSections({
      ds: await read($, dispatches),
      s: await read($, signals),
      b: await read($, backend),
      cells: await read($, cells),
      profile: await read($, profile),
      inject: await read($, inject),
      enforce: await read($, enforce),
    })
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box key="datapce-pane" flexDirection="column">
        {sections.map(s => (
          <Box key={`datapce-pane-${s.id}`} flexDirection="column" marginBottom={1}>
            <Text bold>{s.title}</Text>
            {s.rows.map(r => (
              <Text wrap="truncate-end">{r}</Text>
            ))}
          </Box>
        ))}
      </Box>
    )
  })
}
```

- [ ] **Step 4: Wire it into `register.ts`**

In `plugin/hooks/register.ts`, add to the import block:

```ts
import { install as pane, start as paneStart } from './pane.tsx'
```

In the `session.start` hook, add `await paneStart(host, rt)` as the last call before `return r`; and after `observe(on, rt)` at the end of `register`, add:

```ts
  pane(on, rt)
```

- [ ] **Step 5: Run the tests**

Run: `claude plugin test plugin`
Expected: `119 pass`, `0 fail`.

- [ ] **Step 6: Full suite, validate, commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1759 passed, 4 skipped`; `claude plugin validate plugin` → passed; hooks include `command.run{command=apex}`, `ui.render{component=Pane,requestId=datapce}`.

```bash
git add plugin/hooks/pane.tsx plugin/hooks/register.ts plugin/tests/pane.test.tsx
git commit -m "feat(plugin): /apex pane — dispatches, signals, lanes, evidence, profile; enforce on/off; handoff"
```

---

### Task 19: §17 efficiency contract as tests, a static privacy guard, and the canonical `register.ts`

**Files:**
- Create: `plugin/tests/efficiency.test.ts`, `tests/test_plugin_contract.py`
- Verify (no behaviour change): `plugin/hooks/register.ts` reads exactly as listed in Step 3.

**Interfaces:**
- Consumes: `decideSpawn`, `SpawnContext` (Task 13); `evidenceSection`, `SESSION_CAP_BYTES`, `MAX_SECTION_LINES`, `MAX_DESCRIBE_LINES` (Task 14); `cellKey` (Task 11); the world fixture.
- Produces: tests only.

§17 acceptance encoded: "injected text per session ≤ 2 KB" (hard cap — holds at p100, so at p95); COLD injects nothing; section ≤ 25 lines; `tool.describe` ≤ 2 lines; "no hook adds > 5 ms to `agent.spawn` at p99" — measured on the pure decision with a full cell table (the only work before `next`; the hook does no file or process I/O before `next`, asserted); every injection decision is logged with its arm. Static guard (§10, §17.6): the hooks never use the network (`$.http`, `fetch(`) and never hook `prompt.compose`.

- [ ] **Step 1: Write the tests**

`plugin/tests/efficiency.test.ts`:

```ts
import { describe, expect, test } from 'claude-code/testing'

import type { Cell } from '../types/index.d.ts'
import { cellKey } from '../hooks/evidence.ts'
import { MAX_DESCRIBE_LINES, MAX_SECTION_LINES, SESSION_CAP_BYTES } from '../hooks/inject.ts'
import { decideSpawn, type SpawnContext } from '../hooks/router.ts'
import { SESSION, spawnInput } from './fixtures/inputs.ts'
import { BACKEND, worldOf, type World } from './fixtures/world.ts'

const READY: Cell = { n: 35, pass: 35, state: 'READY', winN: 5, winPass: 5, below: 0, above: 0, cusum: 0, p0: 1 }
const PLANNING = [
  'superpowers:writing-plans',
  'superpowers:subagent-driven-development',
  'superpowers:dispatching-parallel-agents',
  'superpowers:executing-plans',
  'datapce:datapce',
]
const PROVIDER = { plugin: 'engine', tier: 'core' } as never
const bytes = (s: string): number => new TextEncoder().encode(s).length
const manyReady = (n: number): Record<string, Cell> =>
  Object.fromEntries(Array.from({ length: n }, (_, i) => [cellKey(`task${String(i).padStart(2, '0')}`, 'GREEN', 'sonnet'), READY]))
const injectRows = (world: World): Record<string, unknown>[] =>
  (world.appended.get(`${BACKEND}/observe/2026-10-03.jsonl`) ?? []).map(l => JSON.parse(l)).filter(r => r.ev === 'inject')

describe('§17 efficiency contract', () => {
  test('COLD: zero bytes injected anywhere', async ($, on) => {
    worldOf(on)
    await $.session.start(SESSION)
    for (const skill of PLANNING) expect((await $.skill.prompt({ skill, text: 'X' })).text).toBe('X')
    for (const tool of ['Agent', 'Workflow']) expect((await $.tool.describe({ tool, description: 'D', provider: PROVIDER })).description).toBe('D')
  })

  test('the per-session cap holds however often the planning skills run; every decision is logged', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', manyReady(18))
    await $.session.start(SESSION)
    let appended = 0
    let sections = 0
    for (let i = 0; i < 10; i++) {
      const text = (await $.skill.prompt({ skill: PLANNING[i % PLANNING.length]!, text: 'X' })).text
      if (text !== 'X') {
        sections += 1
        const section = text.slice(3)
        expect(section.split('\n').length).toBeLessThanOrEqual(MAX_SECTION_LINES)
        appended += bytes(text) - 1
      }
    }
    expect(sections).toBeGreaterThan(0)
    expect(appended).toBeLessThanOrEqual(SESSION_CAP_BYTES)
    await world.clock.advance(5000)
    const rows = injectRows(world)
    expect(rows).toHaveLength(10)
    expect(rows.filter(r => r.capped === true).length).toBe(10 - sections)
  })

  test('tool.describe appends at most two lines', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', manyReady(18))
    await $.session.start(SESSION)
    const d = (await $.tool.describe({ tool: 'Agent', description: 'D', provider: PROVIDER })).description
    expect(d.split('\n').length - 1).toBeLessThanOrEqual(MAX_DESCRIBE_LINES)
  })

  test('agent.spawn does no file or process I/O on the hot path', async ($, on) => {
    const world = worldOf(on)
    world.store.set('datapce.cells', manyReady(18))
    await $.session.start(SESSION)
    const asked = world.asked.length
    const runs = world.runs.length
    for (let i = 0; i < 50; i++) await $.agent.spawn(spawnInput({ tool_use_id: `toolu_${i}` }))
    expect(world.asked.length).toBe(asked)
    expect(world.runs.length).toBe(runs)
  })

  test('the spawn decision stays under 5 ms at p99 with a full cell table', () => {
    const cells: Record<string, Cell> = {}
    for (const task of ['explore', 'review', 'debug', 'refactor', 'generate', 'mechanical', 'synthesis', 'other']) {
      for (const level of ['GREEN', 'AMBER', 'RED'] as const) {
        for (const tier of ['haiku', 'sonnet', 'opus'] as const) cells[cellKey(task, level, tier)] = { ...READY, state: tier === 'haiku' ? 'WARMING' : 'READY' }
      }
    }
    const ctx: SpawnContext = { level: 'AMBER', cells, stats: {}, enforce: true, admitted: true }
    const times: number[] = []
    for (let i = 0; i < 1000; i++) {
      const t0 = performance.now()
      decideSpawn(spawnInput({ description: `Review change ${i}`, prompt: 'p'.repeat(4000) }), ctx)
      times.push(performance.now() - t0)
    }
    times.sort((a, b) => a - b)
    expect(times[989]!).toBeLessThan(5)
  })
})
```

`tests/test_plugin_contract.py`:

```python
"""Static guards on the hooks module (spec §10 privacy, §17.6 measurable-or-omitted)."""
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "plugin" / "hooks"


def _sources() -> str:
    return "\n".join(p.read_text() for p in sorted(HOOKS.rglob("*")) if p.suffix in (".ts", ".tsx"))


def test_no_network_calls():
    src = _sources()
    assert "$.http" not in src
    assert "fetch(" not in src


def test_prompt_compose_is_not_hooked():
    assert "prompt.compose" not in _sources()


def test_tool_call_hook_never_denies():
    observe = (HOOKS / "observe.ts").read_text()
    assert "deny:" not in observe and "{ deny" not in observe
```

- [ ] **Step 2: Run them**

Run: `claude plugin test plugin` → `124 pass`, `0 fail`.
Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_plugin_contract.py` → `3 passed`.

If the p99 test fails, profile `adviceFor` (the only loop over cells) before touching the bound — the budget is the spec's, not the test's.

- [ ] **Step 3: Check `register.ts` against its canonical form**

After Tasks 1–18, `plugin/hooks/register.ts` must read exactly as below (imports sorted by path). If it differs, make it match — this is the only file several tasks edited.

`plugin/hooks/register.ts`:

```ts
import type { EngineInterface, Register } from 'claude-code'

import type { Measure } from '../types/index.d.ts'
import { start as backendStart } from './backend.ts'
import { install as band, measureOf, refreshStatus } from './band.tsx'
import { checkHandoff } from './handoff.ts'
import type { Host } from './host.ts'
import { describe as injectDescribe, skillSection, start as injectStart } from './inject.ts'
import { start as liveStart } from './live.ts'
import {
  flushAll,
  install as observe,
  measure as observeMeasure,
  skill as observeSkill,
  start as observeStart,
} from './observe.ts'
import { install as pane, start as paneStart } from './pane.tsx'
import { afterSpawn, beforeSpawn, onComplete, start as routerStart, type Decision } from './router.ts'
import { identify, newRuntime, optionsOf } from './runtime.ts'

// Wiring only. Shared events are registered once here (engine rule) and every engine call the
// modules make is bound into a Host here (the engine follows `$` only within this file).

const MEASURE = { plugin: 'datapce', key: 'measure' } as const
const DISPATCHES = { plugin: 'datapce', key: 'dispatches' } as const
const CELLS = { plugin: 'datapce', key: 'cells' } as const
const SIGNALS = { plugin: 'datapce', key: 'signals' } as const
const BACKEND = { plugin: 'datapce', key: 'backend' } as const
const PROFILE = { plugin: 'datapce', key: 'profile' } as const
const INJECT = { plugin: 'datapce', key: 'inject' } as const
const ENFORCE = { plugin: 'datapce', key: 'enforce' } as const

function hostOf($: EngineInterface): Host {
  return {
    now: () => $.clock.now(),
    every: (ms, fn) => $.clock.every(ms, fn),
    home: async () => (await $.env.get('HOME')) ?? '',
    sessionId: () => $.session.id(),
    read: path => $.fs.read(path),
    write: (path, text) => $.fs.write(path, text),
    exists: path => $.fs.exists(path),
    list: path => $.fs.list(path),
    stat: path => $.fs.stat(path),
    run: (argv, init) => $.process.run(argv, init),
    storeGet: key => $.store.get(key),
    storeSet: (key, value) => $.store.set(key, value),
    toast: text => $.ui.toast(text),
    status: text => $.ui.status(text),
    invalidate: event => $.ui.invalidate(event),
    open: args => $.ui.open(args),
    close: id => $.ui.close({ id }),
    registerCommand: spec => $.command.register(spec),
    publish: {
      measure: async v => void (await $.state.set(MEASURE, v)),
      dispatches: async v => void (await $.state.set(DISPATCHES, v)),
      cells: async v => void (await $.state.set(CELLS, v)),
      signals: async v => void (await $.state.set(SIGNALS, v)),
      backend: async v => void (await $.state.set(BACKEND, v)),
      profile: async v => void (await $.state.set(PROFILE, v)),
      inject: async v => void (await $.state.set(INJECT, v)),
      enforce: async v => void (await $.state.set(ENFORCE, v)),
    },
  }
}

export const register: Register = (on, raw) => {
  const rt = newRuntime(optionsOf(raw))
  let lastMeasure: Measure | null = null

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await identify(host, e, rt)
    await observeStart(host, e, rt)
    await routerStart(host, rt)
    await injectStart(host, rt)
    await backendStart(host, rt)
    await liveStart(host, rt, () => lastMeasure)
    await paneStart(host, rt)
    return r
  })

  on('session.end', async ($, e, next) => {
    await flushAll(hostOf($), rt)
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const host = hostOf($)
    const now = await host.now()
    lastMeasure = measureOf(e)
    await host.publish.measure(lastMeasure)
    observeMeasure(rt, e, now)
    refreshStatus(host, rt, lastMeasure)
    checkHandoff(host, rt, now)
    return next(e)
  })

  on('skill.prompt', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    const now = await host.now()
    observeSkill(rt, e.skill, now)
    return { ...r, text: await skillSection(host, rt, e.skill, r.text, now) }
  })

  on('tool.describe', { tool: ['Agent', 'Workflow'] }, async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    return { ...r, description: await injectDescribe(host, rt, e.tool, r.description, await host.now()) }
  })

  on('agent.spawn', async ($, e, next) => {
    const host = hostOf($)
    let d: Decision | null = null
    try {
      d = beforeSpawn(rt, e, await host.now())
    } catch {
      d = null
    }
    if (d === null) return next(e)
    const res = await next(d.apply !== null ? { ...e, model: d.apply } : e)
    await afterSpawn(host, rt, e, d, res, await host.now())
    return res
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    const host = hostOf($)
    await onComplete(host, rt, e, await host.now())
    return r
  })

  band(on, rt)
  observe(on, rt)
  pane(on, rt)
}
```

Run: `claude plugin test plugin` → still `124 pass`; `claude plugin validate plugin` → passed.

- [ ] **Step 4: Full suite and commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1762 passed, 4 skipped`.

```bash
git add plugin/tests/efficiency.test.ts tests/test_plugin_contract.py plugin/hooks/register.ts
git commit -m "test(plugin): §17 efficiency budgets and §10 static guards"
```

---

### Task 20: `apex-router injection-ab` — the self A/B report (§17.6)

**Files:**
- Create: `src/apex_router/injection_ab.py`, `tests/test_injection_ab.py`
- Modify: `src/apex_router/cli.py` (one subparser, one dispatch branch)

**Interfaces:**
- Consumes: `apex_router.core.stats.wilson_ci`; `apex_router.route_log.default_log_path`, `default_labeled_path`; the plugin's route rows (`inject_arm`, `tool_use_id`, Task 13) and route-join's labeled rows (`tool_use_id`, `escalated`).
- Produces: `ARMS`, `MIN_N = 30`, `ALPHA = 0.05`, `arm_outcomes(route_rows, labeled_rows) -> {arm: {"n","pass"}}`, `two_proportion_p(k1, n1, k2, n2) -> float | None`, `report(route_rows, labeled_rows, min_n=MIN_N, alpha=ALPHA) -> dict` (`arms`, `diff`, `p_value`, `verdict` ∈ {insufficient, evidence_helps, evidence_hurts, no_effect}, `action`), `main(argv) -> int`; CLI `apex-router injection-ab [--json] [--log PATH] [--labeled PATH] [--min-n N]`.

Intention-to-treat: a dispatch belongs to its session's arm whether or not a section was actually appended (sessions are randomized by id hash, the appended/not split is not). Pass = route-join's label `escalated == false`. Two-sided two-proportion z-test; a verdict needs ≥ 30 labeled dispatches per arm. `no_effect` and `evidence_hurts` recommend removing the injection (§17.6).

- [ ] **Step 1: Write the failing tests**

`tests/test_injection_ab.py`:

```python
"""Self A/B of the plugin's evidence injection: arms joined to route-join labels by tool_use_id."""
import json

from apex_router import cli, injection_ab as ab


def _rows(arm, n, passes, start=0):
    route = [{"tool_use_id": f"t{start + i}", "inject_arm": arm, "task_type": "explore", "model": "inherit", "escalated": False}
             for i in range(n)]
    labeled = [{"tool_use_id": f"t{start + i}", "escalated": i >= passes} for i in range(n)]
    return route, labeled


def test_arm_outcomes_join_by_tool_use_id():
    route = [{"tool_use_id": "a", "inject_arm": "evidence"}, {"tool_use_id": "b", "inject_arm": "holdout"},
             {"tool_use_id": "c"}, {"tool_use_id": 5, "inject_arm": "evidence"}]
    labeled = [{"tool_use_id": "a", "escalated": False}, {"tool_use_id": "a", "escalated": True},
               {"tool_use_id": "b", "escalated": True}, {"tool_use_id": "c", "escalated": False},
               {"tool_use_id": "b", "escalated": "no"}]
    assert ab.arm_outcomes(route, labeled) == {"evidence": {"n": 1, "pass": 1}, "holdout": {"n": 1, "pass": 0}}


def test_two_proportion_p():
    assert ab.two_proportion_p(45, 50, 30, 50) < 0.001
    assert ab.two_proportion_p(10, 20, 10, 20) == 1.0
    assert ab.two_proportion_p(1, 0, 1, 1) is None


def test_verdicts():
    e_r, e_l = _rows("evidence", 50, 45)
    h_r, h_l = _rows("holdout", 50, 30, start=100)
    assert ab.report(e_r + h_r, e_l + h_l)["verdict"] == "evidence_helps"
    e_r, e_l = _rows("evidence", 50, 30)
    h_r, h_l = _rows("holdout", 50, 45, start=100)
    hurts = ab.report(e_r + h_r, e_l + h_l)
    assert hurts["verdict"] == "evidence_hurts" and hurts["action"] == "remove the injection"
    e_r, e_l = _rows("evidence", 40, 36)
    h_r, h_l = _rows("holdout", 40, 36, start=100)
    assert ab.report(e_r + h_r, e_l + h_l)["verdict"] == "no_effect"
    e_r, e_l = _rows("evidence", 10, 9)
    assert ab.report(e_r, e_l)["verdict"] == "insufficient"


def test_report_shape():
    e_r, e_l = _rows("evidence", 4, 3)
    r = ab.report(e_r, e_l)
    assert r["arms"]["evidence"]["rate"] == 0.75 and len(r["arms"]["evidence"]["ci"]) == 2
    assert r["arms"]["holdout"] == {"n": 0, "pass": 0, "rate": None, "ci": None}
    assert r["diff"] is None and r["p_value"] is None


def test_cli_json(tmp_path, capsys):
    e_r, e_l = _rows("evidence", 3, 3)
    log = tmp_path / "route_log.jsonl"
    labeled = tmp_path / "labeled_table.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in e_r) + "not json\n")
    labeled.write_text("".join(json.dumps(r) + "\n" for r in e_l))
    assert cli.main(["injection-ab", "--json", "--log", str(log), "--labeled", str(labeled)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["arms"]["evidence"]["n"] == 3 and out["verdict"] == "insufficient"
```

- [ ] **Step 2: Run to verify failure**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_injection_ab.py`
Expected: `ImportError: cannot import name 'injection_ab'`.

- [ ] **Step 3: Write `injection_ab.py`**

`src/apex_router/injection_ab.py`:

```python
"""Self A/B for the datapce plugin's evidence injection (spec §17.6).

The plugin assigns every session an arm — `evidence` (sections injected) or `holdout` (none, 20%
by session-id hash) — and stamps it on each dispatch row it appends to route_log.jsonl
(`inject_arm`). route-join resolves those rows into labels in labeled_table.jsonl. This report
joins the two by tool_use_id (intention-to-treat) and compares pass rates per arm. An injection
that does not move outcomes is removed.

    apex-router injection-ab [--json] [--log PATH] [--labeled PATH] [--min-n N]
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from apex_router.core.stats import wilson_ci
from apex_router.route_log import default_labeled_path, default_log_path

ARMS = ("evidence", "holdout")
MIN_N = 30
ALPHA = 0.05
ACTIONS = {
    "insufficient": "keep collecting",
    "evidence_helps": "keep the injection",
    "evidence_hurts": "remove the injection",
    "no_effect": "remove the injection (§17.6: an injection that does not move outcomes is removed)",
}


def _rows(path) -> list:
    out = []
    try:
        with Path(path).open(errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    out.append(d)
    except OSError:
        pass
    return out


def arm_outcomes(route_rows, labeled_rows) -> dict:
    arm_of = {}
    for r in route_rows:
        arm, tuid = r.get("inject_arm"), r.get("tool_use_id")
        if arm in ARMS and isinstance(tuid, str):
            arm_of[tuid] = arm
    out = {a: {"n": 0, "pass": 0} for a in ARMS}
    seen = set()
    for lab in labeled_rows:
        tuid, esc = lab.get("tool_use_id"), lab.get("escalated")
        if isinstance(tuid, str) and tuid in arm_of and tuid not in seen and isinstance(esc, bool):
            seen.add(tuid)
            cell = out[arm_of[tuid]]
            cell["n"] += 1
            cell["pass"] += 0 if esc else 1
    return out


def two_proportion_p(k1: int, n1: int, k2: int, n2: int):
    """Two-sided two-proportion z-test p-value (pooled); None when an arm is empty."""
    if n1 == 0 or n2 == 0:
        return None
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (k1 / n1 - k2 / n2) / se
    return math.erfc(abs(z) / math.sqrt(2))


def report(route_rows, labeled_rows, min_n: int = MIN_N, alpha: float = ALPHA) -> dict:
    arms = arm_outcomes(route_rows, labeled_rows)
    out = {"arms": {}, "min_n": min_n}
    for a, c in arms.items():
        out["arms"][a] = {**c, "rate": c["pass"] / c["n"] if c["n"] else None,
                          "ci": list(wilson_ci(c["pass"], c["n"])) if c["n"] else None}
    e, h = arms["evidence"], arms["holdout"]
    out["diff"] = e["pass"] / e["n"] - h["pass"] / h["n"] if e["n"] and h["n"] else None
    out["p_value"] = two_proportion_p(e["pass"], e["n"], h["pass"], h["n"])
    if e["n"] < min_n or h["n"] < min_n:
        verdict = "insufficient"
    elif out["p_value"] < alpha and out["diff"] > 0:
        verdict = "evidence_helps"
    elif out["p_value"] < alpha and out["diff"] < 0:
        verdict = "evidence_hurts"
    else:
        verdict = "no_effect"
    out["verdict"] = verdict
    out["action"] = ACTIONS[verdict]
    return out


def _fmt(x, spec: str) -> str:
    return "—" if x is None else format(x, spec)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="apex-router injection-ab",
                                 description="pass rate with vs without the datapce evidence section")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--log", type=Path, default=None)
    ap.add_argument("--labeled", type=Path, default=None)
    ap.add_argument("--min-n", type=int, default=MIN_N)
    args = ap.parse_args(argv)
    log = args.log or default_log_path()
    labeled = args.labeled or default_labeled_path(log)
    r = report(_rows(log), _rows(labeled), min_n=args.min_n)
    if args.json:
        print(json.dumps(r, indent=2, sort_keys=True))
        return 0
    for a in ARMS:
        c = r["arms"][a]
        print(f"{a:9s} n={c['n']:5d} pass={c['pass']:5d} rate={_fmt(c['rate'], '.3f')}")
    print(f"diff={_fmt(r['diff'], '+.3f')} p={_fmt(r['p_value'], '.4f')} verdict={r['verdict']} -> {r['action']}")
    return 0
```

In `src/apex_router/cli.py`, add the subparser right after the `pressure` subparser (before `args, extra = ap.parse_known_args(argv)`):

```python
    # Self A/B of the datapce plugin's evidence injection (spec §17.6). Args forwarded.
    sub.add_parser("injection-ab", help="pass rate with vs without the datapce evidence section "
                                        "(intention-to-treat by session arm)", add_help=False)
```

and the dispatch branch right after the `if args.cmd == "pressure":` branch:

```python
    if args.cmd == "injection-ab":
        from .injection_ab import main as _injection_ab
        return _injection_ab(extra)
```

- [ ] **Step 4: Run the tests**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_injection_ab.py`
Expected: `5 passed`.

- [ ] **Step 5: Full suite and commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1767 passed, 4 skipped`; `claude plugin test plugin` → `124 pass`.

```bash
git add src/apex_router/injection_ab.py src/apex_router/cli.py tests/test_injection_ab.py
git commit -m "feat: apex-router injection-ab — self A/B of the datapce evidence section"
```

---

### Task 21: The one skill — `plugin/skills/datapce/SKILL.md`

**Files:**
- Create: `plugin/skills/datapce/SKILL.md`, `tests/test_datapce_skill.py`

**Interfaces:**
- Consumes: the plugin's injected section format (Task 14), `/apex` (Task 18), `apex-router injection-ab` (Task 20).
- Produces: the skill `datapce` (plugin skill; its prompt is one of the five that receive the evidence section).

Spec §8: ≤ 350 lines; sections Route, Verify, Review, Ship; backend commands appear once, in Route; the ten retired skills' names stay in the description for discoverability. §17.7 "Supersede quietly": the band already shows pressure, so the skill only asks for `apex-router pressure --check` where there is no band.

- [ ] **Step 1: Write the failing test**

`tests/test_datapce_skill.py`:

```python
"""Spec §8: one condensed skill, ≤ 350 lines, Route/Verify/Review/Ship, backend commands once (in Route)."""
import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "datapce" / "SKILL.md"
RETIRED = ["model-routing", "disciplined-execution", "verify-claims", "evidence-labels", "cross-validate",
           "change-classification", "local-references", "public-repo-hygiene", "dependency-vetting", "unattended-loop"]
COMMANDS = ["apex-router pressure", "apex-router route-advise", "apex-router review-preread", "apex-router injection-ab"]
LABELS = ["PASS", "FAIL", "PARTIAL", "BLOCKED", "INCONCLUSIVE", "STALE", "ASSUMED", "N/A", "WAIVED"]


def _text() -> str:
    return SKILL.read_text()


def _sections(text: str) -> dict:
    parts = re.split(r"^## ", text, flags=re.M)
    return {p.split("\n", 1)[0].strip(): p for p in parts[1:]}


def test_frontmatter_and_length():
    text = _text()
    assert text.startswith("---\nname: datapce\n")
    assert len(text.splitlines()) <= 350
    description = text.split("\n---\n", 1)[0]
    for name in RETIRED:
        assert name in description, name


def test_sections_in_order():
    assert list(_sections(_text()))[:4] == ["Route", "Verify", "Review", "Ship"]


def test_backend_commands_once_and_only_in_route():
    text = _text()
    route = _sections(text)["Route"]
    for cmd in COMMANDS:
        assert text.count(cmd) == 1, cmd
        assert cmd in route, cmd


def test_verify_carries_the_five_gates_and_nine_labels():
    verify = _sections(_text())["Verify"]
    for gate in ["Scope before work", "Evidence before reasoning", "Reason adversarially",
                 "Verify before declaring done", "Report calibrated"]:
        assert gate in verify, gate
    for label in LABELS:
        assert f"**{label}**" in verify, label


def test_route_reads_the_injected_evidence_and_respects_explicit_models():
    route = _sections(_text())["Route"]
    assert "## datapce evidence (this machine)" in _text()
    assert "explicit `model:`" in route
    assert "/apex" in route


def test_no_personal_paths():
    text = _text()
    assert "/Users/" not in text and "~/src" not in text
```

- [ ] **Step 2: Run to verify failure**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_datapce_skill.py`
Expected: 6 failed (`FileNotFoundError`).

- [ ] **Step 3: Write the skill**

`plugin/skills/datapce/SKILL.md`:

```markdown
---
name: datapce
description: Use when planning work that will be delegated, dispatching subagents or a Workflow, before a parallel fan-out, when verifying or reporting a result, when reviewing a change, and before shipping to a shared repository. One condensed workflow (Route, Verify, Review, Ship) backed by what datapce measured on this machine. Replaces the apex-router-skills set; former names, kept for discovery — model-routing, disciplined-execution, verify-claims, evidence-labels, cross-validate, change-classification, local-references, public-repo-hygiene, dependency-vetting, unattended-loop.
---

# datapce

datapce watches every subagent dispatch on this machine — task type, tier, outcome, cost, upstream
pressure — and turns it into evidence. The plugin decides the mechanical parts for you (task type,
pressure level, whether a route is proven) and shows them in the band (`apex ●LEVEL …`) and the
pane (`/apex`). This skill covers what only you can do with that evidence.

## Route

Pick who does the work, and on what tier, before you start it.

- **Delegate, don't switch.** Changing your own model mid-session evicts the prompt cache. Hand
  uneven subtasks to subagents with a tier each instead.
- **Read the evidence section.** Planning and dispatch skills may end with
  `## datapce evidence (this machine)`: one row per task type — tier, labels, pass rate, mean
  tokens, state. `READY` means proven here (30+ labels, Wilson lower bound ≥ 0.90). `WARMING n/30`
  means not yet. No section means no evidence yet: use your own judgement, nothing is proven.
- **Name the tier per task in the plan** (`explore → sonnet`, `review → opus`). datapce applies a
  READY tier at dispatch only when enforce is on (`/apex enforce on`); otherwise it advises and the
  pane shows `▲` where its advice differed from what ran.
- **Never override an explicit `model:`.** If the user (or the task) names a model, pass it; datapce
  never rewrites it, and neither should you. Fable requests are never downshifted silently.
- **Pressure before fan-out.** The band shows the level. GREEN: fan out. AMBER: send explore and
  mechanical work one tier down, keep the fan-out small. RED: serialize heavy (opus/fable) work;
  the pane's Signals section shows the admission rate. No band (headless `claude -p`)? Run
  `apex-router pressure --check` first: exit 0 GREEN, 1 AMBER, 2 RED, 3 UNKNOWN.
- **Lane breakers.** When the pane shows a local lane's breaker `open`, escalate that work to a
  frontier tier for 10 minutes instead of retrying the lane.
- **Local lanes (only with the backend installed).**
  - A local pre-read before a heavy review: `apex-router review-preread` (claims to verify — a
    reviewer still verifies them).
  - The measured cost verdict per task type: `apex-router route-advise --json`.
  - Whether the evidence section itself moves outcomes: `apex-router injection-ab`.

Default tiers when there is no evidence:

| task type | tier | why |
|---|---|---|
| explore / search / summarize | sonnet (haiku for mechanical lookups) | breadth, low stakes |
| generate / refactor | sonnet; opus when the design is unsettled | volume vs judgement |
| debug | opus | the first theory is often wrong |
| review / verification | opus, independent of the author | disagreement is the signal |
| synthesis / final report | the session's model | it holds the context |

## Verify

Five gates, in order, for anything where the first idea might be wrong. Skip them for a one-line
edit; never skip them for a claim someone will act on.

1. **Scope before work** — restate the goal, the constraints and what "done" means; list unknowns.
2. **Evidence before reasoning** — read the data, the code, the log; do not reason from memory
   about things you can look at.
3. **Reason adversarially** — attack your own answer: what would make it wrong, what did you not
   check, which number would change the conclusion.
4. **Verify before declaring done** — run the check that would fail if you were wrong, after the
   last edit; grep the number back to its source.
5. **Report calibrated** — say what you checked, how, and what you did not.

Grep the number: before quoting a count, ID, filename or metric that came from a model (a subagent,
a local model, or you), find it in the source. A fluent figure is not a fact.

Label every claim in a handoff, PR description or "done" message:

| label | means |
|---|---|
| **PASS** | the check ran on the final state and passed |
| **FAIL** | the check ran and failed |
| **PARTIAL** | ran on a subset; say which |
| **BLOCKED** | could not run; say why |
| **INCONCLUSIVE** | ran, result does not decide the question |
| **STALE** | ran before the last edit |
| **ASSUMED** | not checked; believed for a stated reason |
| **N/A** | does not apply here; say why |
| **WAIVED** | skipped by an explicit decision; say whose |

A gap in verification must never read as a pass.

## Review

- **Independent reviewer.** For a non-trivial change or a report someone will act on, dispatch a
  fresh reviewer (no shared context, opus tier) with the diff, the requirements and the test output,
  and ask it to find what is wrong — not to confirm. Fix or rebut each finding in writing.
- **Panel for risky changes.** For high blast radius, ask several independent models to classify
  the change on three axes — change class, requirement fit, blast radius. Trust where they agree;
  look hardest where they disagree.
- **Pre-read is optional.** With the backend, the local pre-read (see Route) hands the reviewer
  claims to check; it never replaces the reviewer.
- **Ground in your own sources.** When a book, paper or code sample you have locally covers the
  question, cite it rather than paraphrasing from memory.

## Ship

- **Public-repo hygiene.** Before pushing to a shared or public repository, scan the added lines for
  secrets, internal identifiers and personal paths, and check the committer identity. Deleting a
  leak later does not remove it from history.
- **Dependency vetting.** Before adding, replacing or upgrading any third-party package (runtime,
  test or build), check: official index; exact pinned and hash-locked version; maintained (a release
  in the last 24 months); permissive OSI license; no known vulnerability per an archived OSV query;
  scoped to the component that needs it. Re-scan the whole lock before a release.
- **Unattended loops.** When running without a person (nightly job, `/loop`): on a blocker, document
  it, take one advisor round, re-check, act, verify. Never loosen a failing gate to go green. Hard
  limits — no deletion of data, no force-push, no spending, no publishing — are never unlocked by
  agreement. Commit scoped changes on a dated branch and leave a morning report.
```

- [ ] **Step 4: Run the tests**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_datapce_skill.py`
Expected: `6 passed`.

Run: `claude plugin validate plugin` → passed (the validator lists the skill). `claude plugin test plugin` → `124 pass`.

- [ ] **Step 5: Full suite and commit**

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1773 passed, 4 skipped`.

```bash
git add plugin/skills/datapce/SKILL.md tests/test_datapce_skill.py
git commit -m "feat(plugin): the one datapce skill — Route, Verify, Review, Ship"
```

---

### Task 22: Migration and 0.4.0 — retire the two hooks, install the plugin, rewrite README §1, CHANGELOG, version

**Files:**
- Delete: `hooks/agent-route-log.sh`, `hooks/cache-handoff-nudge.sh`
- Modify: `tests/test_agent_route_log.py` (drop `TestHookSubprocess` and `HOOK`), `tests/test_handoff_state.py` (drop the shell-heredoc drift guard — Task 17's plugin guard replaces it), `install.sh`, `README.md`, `CHANGELOG.md`, `pyproject.toml` (version), `plugin/.claude-plugin/plugin.json` (version), `.claude-plugin/marketplace.json` (privacy sentence in the listing)
- Create: `tests/test_release_0_4_0.py`

**Interfaces:**
- Consumes: everything above. `route_log.hook_main` and `route_log.classify_dispatch` stay (route-join, the classify oracle and the remaining tests use them); only the shell wrapper is retired.
- Produces: release 0.4.0 (spec §14).

- [ ] **Step 1: Write the failing release test**

`tests/test_release_0_4_0.py`:

```python
"""Spec §14 migration and §10 'stated verbatim in the listing and README'."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVACY = ("never stores prompt text, file contents, or command text; descriptions are truncated "
           "dispatch labels. No network calls. No telemetry to anyone.")


def test_retired_hooks_are_gone():
    assert not (ROOT / "hooks" / "agent-route-log.sh").exists()
    assert not (ROOT / "hooks" / "cache-handoff-nudge.sh").exists()


def test_install_adds_this_marketplace_and_the_plugin():
    text = (ROOT / "install.sh").read_text()
    assert 'APEX_DEFAULT_PLUGIN="datapce@datapce"' in text
    assert '_marketplace_add "${APEX_PUBLIC_MARKETPLACE:-$INSTALL_DIR}"' in text
    assert "retired" in text and "/apex handoff" in text
    assert subprocess.run(["bash", "-n", str(ROOT / "install.sh")]).returncode == 0


def test_privacy_is_stated_in_readme_and_listing():
    assert PRIVACY in (ROOT / "README.md").read_text()
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert PRIVACY in market["plugins"][0]["description"]


def test_readme_leads_with_the_plugin_install():
    head = "\n".join((ROOT / "README.md").read_text().splitlines()[:30])
    assert head.startswith("# datapce (apex-router)")
    assert "claude plugin install datapce@datapce" in head


def test_changelog_has_0_4_0():
    assert "## 0.4.0" in (ROOT / "CHANGELOG.md").read_text()
```

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_release_0_4_0.py` → 5 failed.

- [ ] **Step 2: Retire the two hooks and their shell tests**

~~~bash
git rm hooks/agent-route-log.sh hooks/cache-handoff-nudge.sh
~/src/apex-router/.venv/bin/python - <<'PY'
import ast, pathlib
p = pathlib.Path("tests/test_agent_route_log.py")
src = p.read_text()
lines = src.splitlines(keepends=True)
drop = set()
for n in ast.parse(src).body:
    if isinstance(n, ast.ClassDef) and n.name == "TestHookSubprocess":
        drop.update(range(n.lineno - 1, n.end_lineno))
    if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "HOOK" for t in n.targets):
        drop.update(range(n.lineno - 1, n.end_lineno))
p.write_text("".join(l for i, l in enumerate(lines) if i not in drop))

p = pathlib.Path("tests/test_handoff_state.py")
src = p.read_text()
lines = src.splitlines(keepends=True)
drop = set()
tree = ast.parse(src)
for n in tree.body:
    if isinstance(n, ast.FunctionDef) and n.name == "_extract_hook_template":
        drop.update(range(n.lineno - 1, n.end_lineno))
    if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "HOOK" for t in n.targets):
        drop.update(range(n.lineno - 1, n.end_lineno))
    if isinstance(n, ast.ClassDef) and n.name == "TestTemplate":
        for m in n.body:
            if isinstance(m, ast.FunctionDef) and m.name == "test_hook_embed_matches_module":
                drop.update(range(m.lineno - 1, m.end_lineno))
text = "".join(l for i, l in enumerate(lines) if i not in drop)
text = text.replace(
    "The hook (hooks/cache-handoff-nudge.sh) embeds the template in a heredoc",
    "The datapce plugin (plugin/hooks/handoff.ts) embeds the template; tests/test_handoff_template_plugin.py guards it.\nIt used to be the hook (hooks/cache-handoff-nudge.sh), which embedded the template in a heredoc",
)
p.write_text(text)
PY
grep -c "TestHookSubprocess\|HOOK =" tests/test_agent_route_log.py tests/test_handoff_state.py || true   # expect :0 for both files
~~~

- [ ] **Step 3: `install.sh` — install the datapce plugin; retire the two flags**

~~~bash
~/src/apex-router/.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("install.sh")
s = p.read_text()

def rep(old, new):
    global s
    assert s.count(old) == 1, old[:70]
    s = s.replace(old, new)

def retire(fn, flag, replacement):
    global s
    start = s.index(f"{fn}() {{\n")
    end = s.index("\n}\n", start) + 3
    s = s[:start] + (
        f"{fn}() {{\n"
        f"  # Retired in 0.4.0: {replacement}\n"
        f"  [ \"${flag}\" = \"1\" ] && warn \"  this hook is retired: {replacement}\"\n"
        "  return 0\n"
        "}\n"
    ) + s[end:]

rep('APEX_PUBLIC_MARKETPLACE="runapex/apex-router-skills"   # public default (github owner/repo)',
    'APEX_PUBLIC_MARKETPLACE=""   # empty = the datapce marketplace in this install ($INSTALL_DIR/.claude-plugin)')
rep('APEX_DEFAULT_PLUGIN="apex-workflow@apex-router-skills" # plugin@marketplace to install by default',
    'APEX_DEFAULT_PLUGIN="datapce@datapce" # plugin@marketplace to install by default')
rep('    _marketplace_add "$APEX_PUBLIC_MARKETPLACE"\n',
    '    _marketplace_add "${APEX_PUBLIC_MARKETPLACE:-$INSTALL_DIR}"\n')
rep('&& ok "  installed $APEX_DEFAULT_PLUGIN (model-routing, cross-validate, verify-claims, disciplined-execution, change-classification, local-references, public-repo-hygiene)"',
    '&& ok "  installed $APEX_DEFAULT_PLUGIN (band, /apex pane, routing evidence, the datapce skill)"')
rep("#         --cache-handoff-hook  wire the cache-cost session-handoff Stop hook into ~/.claude/settings.json",
    "#         --cache-handoff-hook  retired in 0.4.0: the datapce plugin's handoff toast and /apex handoff replace it")
rep("#         --agent-route-log-hook  wire the Agent-dispatch outcome-label PostToolUse hook into ~/.claude/settings.json",
    "#         --agent-route-log-hook  retired in 0.4.0: the datapce plugin logs every dispatch at agent.spawn")
rep("#         --skills-marketplace URL  add another Claude Code skill marketplace (repeatable). The public\n#                                   apex-router-skills marketplace is added by default.",
    "#         --skills-marketplace URL  add another Claude Code skill marketplace (repeatable). The datapce\n#                                   marketplace (this repository) is added by default.")
retire("install_cache_handoff_hook", "$DO_CACHE_HANDOFF", "the datapce plugin's handoff toast and /apex handoff replace it")
retire("install_agent_route_log_hook", "$DO_AGENT_ROUTE_LOG", "the datapce plugin logs every dispatch at agent.spawn")
p.write_text(s)
PY
bash -n install.sh && echo syntax-ok
~~~

If `settings.json` on a machine already wires the retired hooks, they now point at deleted files and Claude Code reports them as failing: print the two entries for the user to remove (`grep -n "agent-route-log.sh\|cache-handoff-nudge.sh" ~/.claude/settings.json`) — do not edit their settings from this task.

- [ ] **Step 4: README §1, CHANGELOG, listing, version**

~~~bash
~/src/apex-router/.venv/bin/python - <<'PY'
import json, pathlib, re

readme = pathlib.Path("README.md")
s = readme.read_text()
end = s.index("> and [CHANGELOG.md](CHANGELOG.md).\n") + len("> and [CHANGELOG.md](CHANGELOG.md).\n")
intro = """# datapce (apex-router)

datapce observes what actually happens on this machine — which subagents run, on which model,
what they cost, whether they succeeded, how the upstream is behaving — and turns that into
evidence the tools people already use can act on. It does not plan; it makes existing planners
and dispatchers (superpowers' writing-plans and subagent-driven-development, the `Workflow` tool,
the `Agent` tool) choose better on *this* machine, and it shows every decision and why.

One product, one install (alias `apex-router`):

```
claude plugin marketplace add runapex/apex-router
claude plugin install datapce@datapce
```

> Adaptive model routing you can see. A status bar and pane showing every subagent's tier, cost
> and why; routing advice that only enforces what it has measured on your machine; evidence
> injected into the planning tools you already use. Optional local-model lanes.

**Privacy.** datapce writes only under `~/.apex-router/` and its own plugin store. It never stores prompt text, file contents, or command text; descriptions are truncated dispatch labels. No network calls. No telemetry to anyone.

The pip package `apex-router` documented below is the optional backend: local model lanes,
cross-client proxy telemetry, nightly learning. The plugin works without it.
"""
s = intro + s[end:]
old = s[s.index("**Public workflow skills.**"):s.index("/plugin install apex-workflow@apex-router-skills\n```\n") + len("/plugin install apex-workflow@apex-router-skills\n```\n")]
s = s.replace(old, """**Workflow skills.** The ten former `apex-router-skills` skills — `model-routing`,
`cross-validate`, `verify-claims`, `disciplined-execution`, `public-repo-hygiene`,
`local-references`, `change-classification`, `evidence-labels`, `unattended-loop`,
`dependency-vetting` — are condensed into the one `datapce` skill that ships with the plugin
(Route · Verify · Review · Ship); their names stay as aliases in its description. The
`apex-router-skills` marketplace is deprecated.
""")
readme.write_text(s)

ch = pathlib.Path("CHANGELOG.md")
c = ch.read_text()
entry = """## 0.4.0 — unreleased

The theme of this release is one product, one install: datapce, a Claude Code plugin (alias
apex-router), with the pip package as its optional backend.

### Added
- `plugin/`: the datapce hooks module — observation rows and Welford stats, evidence cells with a
  Wilson-gated READY state and CUSUM demotion, advise-by-default routing at `agent.spawn` (enforce
  applies proven cells only), measurable evidence injection into the planning skills (session A/B
  arm), band, `/apex` pane, status entry and toasts; the one condensed `datapce` skill. The
  marketplace lives at `.claude-plugin/marketplace.json`.
- `apex_router.core` (pce-core): Welford, EWMA (float and Q16), Jacobi PCA, Q/T² with the χ²
  limit, Page CUSUM, the penalty state machine, the cell state machine, OLS cost and budget burn,
  with a TypeScript mirror held to generated parity fixtures (`python -m apex_router.core.fixtures`).
- `apex-router injection-ab`: pass rate with vs without the evidence section.

### Changed
- `apex_router.stats` re-exports from `apex_router.core.stats`; `bradley_terry` removed (no caller).
- `install.sh` adds the datapce marketplace from the install directory and installs `datapce@datapce`.

### Removed
- `hooks/agent-route-log.sh` and `hooks/cache-handoff-nudge.sh`: the plugin's router and handoff
  toast replace them; `--agent-route-log-hook` and `--cache-handoff-hook` now print a notice.

"""
ch.write_text(c.replace("## 0.3.0 — 2026-10-02\n", entry + "## 0.3.0 — 2026-10-02\n", 1))

py = pathlib.Path("pyproject.toml")
py.write_text(re.sub(r'^version = "0\.3\.0"$', 'version = "0.4.0"', py.read_text(), count=1, flags=re.M))

for path in ("plugin/.claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
    p = pathlib.Path(path)
    d = json.loads(p.read_text())
    if "version" in d:
        d["version"] = "0.4.0"
    if "plugins" in d:
        d["plugins"][0]["description"] += (" datapce writes only under ~/.apex-router/ and its own plugin store. "
                                           "It never stores prompt text, file contents, or command text; descriptions "
                                           "are truncated dispatch labels. No network calls. No telemetry to anyone.")
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
PY
~~~

Follow-up outside this repository (spec §14, separate repo and commit): in `apex-router-skills`, add a README pointer to `datapce@datapce` and mark the marketplace entry deprecated.

- [ ] **Step 5: Run the release tests and both suites**

Run: `~/src/apex-router/.venv/bin/pytest -q tests/test_release_0_4_0.py tests/test_version_parity.py tests/test_agent_route_log.py tests/test_handoff_state.py tests/test_handoff_template_plugin.py`
Expected: all pass.

Run: `~/src/apex-router/.venv/bin/pytest -q` → `1770 passed, 4 skipped` (1773 − 7 shell-hook tests − 1 heredoc guard + 5 release tests).
Run: `claude plugin test plugin` → `124 pass`; `claude plugin validate plugin` and `claude plugin validate .` → passed.

- [ ] **Step 6: Manual verification in a live session (spec §13 "Manual")**

1. `claude --plugin-dir ~/src/apex-router/.claude/worktrees/datapce-plugin/plugin` in a scratch repo.
2. Ask for one subagent (e.g. "use an Explore agent to find where X is defined"). Expect the band (`apex ●GREEN … routes 1`), a row in `~/.apex-router/observe/<UTC day>.jsonl` with `"ev":"spawn"` and no prompt text, and one `route_log.jsonl` row with `"surface":"claude-code"`.
3. `/apex` → the pane with five sections (four without the backend); `/apex enforce on` → toast-free confirmation; `/apex handoff` → the six-field block in context.
4. Edit any hook file and save: the module hot-reloads; the band survives (state is the host's).
5. Screenshot the band and the pane to `docs/datapce-band.png` and `docs/datapce-pane.png`.

- [ ] **Step 7: Commit**

```bash
git add -A hooks tests install.sh README.md CHANGELOG.md pyproject.toml plugin/.claude-plugin/plugin.json .claude-plugin/marketplace.json docs/datapce-band.png docs/datapce-pane.png
git commit -m "release: 0.4.0 — datapce plugin replaces the route-log and handoff hooks; README §1 and CHANGELOG"
```

---

## Self-review (done while writing; kept for the reviewer)

**Spec coverage.** §1 positioning/listing → Tasks 1, 22. §2 scope (SPRT, per-dispatch local mixing, Pi parity deferred) → not built, by spec. §3 layout → File map; differences: `host.ts`, `state.ts`, `runtime.ts`, `tiers.ts`, `io.ts`, `live.ts`, `handoff.ts` added, `signals.ts` split from `live.ts` (pure vs wired); version parity → Task 1. §4 observation → Task 12 (rows, retention, Welford stats, profile), Task 13 (completion via `turn.complete`), Task 9 (classify parity). §5 cells/states/hysteresis/rebaseline → Tasks 5, 8, 11; `agent.spawn` advise/enforce/hard limits/shedding → Tasks 11, 13; injection → Task 14. §6 band/pane/toasts/status → Tasks 1, 16, 17, 18. §7 formulas → Tasks 10, 16 (and core Tasks 2–5, 7–8). §8 skill → Task 21. §9 backend → Task 15. §10 privacy → Tasks 12, 19, 22. §11 userConfig → Task 1. §12 degradation → Tasks 15, 17 (no backend, `-p`), mobile/vscode have no band site (engine: AbovePrompt is terminal/desktop only). §13 testing → every task; manual → Task 22. §14 migration → Task 22. §15 open question → Task 13 Step 6. §16 pce-core → Tasks 2–8 (S4b conditional permutation test deferred to v1.1 per §16). §17 → Tasks 14, 19, 20.

**Type consistency.** `Host`, `Runtime`, contract types are defined once (Task 1) and imported everywhere; module entry points named in each task's Interfaces block match the canonical `register.ts` in Task 19.

**Verification of this plan.** Every task was executed from this document, in order, in a scratch copy of the branch before it was written down (code blocks extracted verbatim; the scripted edits run as written). End state: `pytest -q` → 1770 passed, 4 skipped; `claude plugin test plugin` → 124 pass; `claude plugin validate plugin` and `claude plugin validate .` → passed; `tsc` 6.0.3 against the engine's `claude-code.d.ts` → no errors. Three engine rules surfaced only by running the code, and the plan is built around them (Global Constraints): one hook per event, `$` never crosses an import, `$.state` refs live in the file that uses them.
