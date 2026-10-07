# Plan: workflow graph → Markov valuation → recommended (then fired) workflows

**Status:** plan only, nothing implemented. **Date:** 2026-10-07.
**Goal:** from what apex-router already logs (tasks, tools, models, retries, outcomes), build graphs
with capacities, find the bottlenecks and single points of failure, value each candidate
workflow for a task with absorbing Markov chains, and — once that valuation is shown to beat the
current default — recommend and then fire the best workflow from the main session.

The order matters: the valuation is only as good as the outcome labels, and today there are
almost none (below). So the plan collects labels first, builds read-only analytics second,
recommends in shadow third, and fires last, each behind a measured gate.

## What exists today (measured 2026-10-07)

| Source | Volume | What it gives the graph | Gap |
|---|---|---|---|
| pi sessions (`~/.pi/agent/sessions`) | 107 sessions; newest 60 = 9,929 tool calls, p50 116 / p90 409 per session | action sequences (bash 80%, read, edit, write); tool errors 232/9,925 | no task type, no outcome |
| Claude Code transcripts | 22 sessions, 1,661 tool calls; 5 subagent logs | action sequences incl. Agent / Task / Skill | same |
| Codex rollouts | 1,471 in 14 d (mostly `codex_exec` = xval); p50 20 calls | review workflows; `spawn_agent` 147× | outcome only via xval log |
| proxy telemetry | 8,209 rows, 90 sessions | model edges: requests, tokens, ttft, errors, retries, wire bytes (v10) | **57% rows have no session id** (Codex before today's fix); **no total duration** |
| `route_log.jsonl` | 79 rows / 2 days, 22 with session id | task_type classification + escalation | thin; escalations 4/4 were *provider errors*, not capability |
| `labeled_table.jsonl` | 66 rows (61 easy, 2 hard, 3 ok) | supervised labels | too few, near-constant |
| `conformance.jsonl` | 153 rows | requested tier vs resolved model | 24 h drift 0 |
| `xval_runs.jsonl` | 22 runs | review workflow outcome (verdict, cost, bandit arm, propensity-ready) | — |
| `codeqa_impact.jsonl` | 458 asks | grounded / stale / hallucinated citations | — |

Day-0 prototype (a throwaway script, not in the repo; becomes `graph_mine.py` in P1) over 78 pi + Claude sessions,
13,034 tool calls, actions normalized to classes:

| class | share (stationary) | PageRank (d=.85) | self-loop → mean run |
|---|---|---|---|
| search | 0.290 | 0.241 | 0.48 → 1.9 |
| run | 0.201 | 0.165 | 0.48 → 1.9 |
| read | 0.188 | 0.160 | 0.30 → 1.4 |
| edit | 0.107 | 0.094 | 0.24 → 1.3 |
| vcs | 0.101 | 0.107 | 0.56 → 2.3 |
| remote / build / test / plan / delegate | 0.04 / 0.03 / 0.005 / 0.008 / 0.000 | | |

Expected calls START→END (absorbing): **167**. Strongest loop: search ⇄ read (890 / 759).

Three lessons from the prototype that shape the plan:
1. **The ontology decides everything.** A first-word bash classifier called 44% of calls "shell"
   (`echo`, `timeout`, `cd`, `VAR=` prefixes); a segment-scanning one, 2%. It still calls 61 calls
   "test" while 335 commands mention pytest/rspec (`.venv/bin/pytest`, `python -m pytest` land in
   "run"). The classifier gets a labeled validation set before any graph is trusted.
2. **Uniform PageRank ≈ visit frequency.** On a near-complete action graph it ranks what you do
   most, not what matters. Useful rankings need outcome-personalized variants (P1).
3. **The action graph has no bridges or articulation points** — every class reaches every other.
   Bridges are meaningful on the *resource* graph (one proxy, one gateway per wire, one local
   GPU) and on *success paths* (dominators: steps every successful trajectory passes through).

Retries, measured (system-design plan B2/B3, `markov.py`): proxy transport retries rescued 2 of
~14.8k calls; 68/68 connect failures exhausted both retries; failures are bursty (lag-1
autocorrelation 0.33–0.45, dispersion 2.8). Retrying at the call layer barely helps; the graph
has to model retries as *state-dependent*, not independent.

## The three graphs

**G1 process graph (per task type).** Nodes = action classes + `START` + absorbing states
`SUCCESS`, `FAIL`, `ESCALATE`, `ABANDON`. Edges = observed transitions with counts, tokens,
wall time, error rate. One chain per (task_type, workflow template), Dirichlet-smoothed toward
the pooled chain (hierarchical: sparse cells borrow from the pool, not from zero).

**G2 resource graph.** Nodes = session → subagent → client → proxy → endpoint/gateway → model,
plus local: ollama runner, GPU, worker queue. Edge **capacity** = measured limits:
- throughput: requests/min and tokens/min per endpoint and model (telemetry), concurrency =
  arrival rate × duration (Little's law — needs total duration, P0);
- headroom: 5h/7d limit meters (`measure` events), `retry-after`, pressure level (`pressure.py`);
- local: ollama is one model slot at a time, the worker queue drains one job at a time.

**G3 workflow graph.** Templates as DAGs of steps, each step = (role, model tier, tool scope,
budget). Initial set, all already runnable here:

| id | template | exists as |
|---|---|---|
| W0 | solo in main session | default |
| W1 | cheap-start → escalate on failure | `route-log` / cheap-start tiers |
| W2 | N explore subagents (parallel) → synthesize | Agent / Workflow tool |
| W3 | implement → independent review (xval) → fix | `apex-router xval`, code-change-loop |
| W4 | retrieve (codeqa ground) → implement | `codeqa ask --verify` |
| W5 | local offload lane → escalate | Ornith worker queue |

## Math, and what each piece is for

| Technique | On | Answers | Note |
|---|---|---|---|
| Absorbing chain, fundamental matrix N = (I−Q)⁻¹ | G1 | expected steps N·1, **P(SUCCESS) = (N·R)**, expected tokens/time N·c, variance | the workflow *value*; the core of P2 |
| Value = P(success) − λ·E[tokens] − μ·E[wall] | G1 per (task, template) | which template to pick | λ, μ from the user's budget/limit meter, not hand-set |
| Stationary distribution / limits | G1 ergodic (END→START) | long-run share of time per action | readout; "where does the time go" |
| Personalized PageRank (restart at START) + **reverse PageRank from SUCCESS** | G1 | which actions lead to success (≈ value function), not just frequent | plain PageRank only as a baseline |
| HITS hubs / authorities | G2 | which models/tools are central vs which are relied on | readout |
| Dominator tree from START to SUCCESS | G1 | steps every successful run passes through (e.g. verify) | candidate mandatory steps in templates |
| Bridges / articulation points (Tarjan) | G2 undirected | single points of failure (proxy, gateway, GPU) | resilience readout, alert |
| Max-flow / min-cut | G2 with capacities | the bottleneck that caps parallel fan-out | sets W2's N |
| Markov order test (BIC: order 1 vs 2 vs variable) | G1 | is first-order enough? | failures are bursty → maybe not |
| Retry modelling: 2-state burst chain (`markov.py`) on edges | G1/G2 | P(retry succeeds \| previous failed) | gates "retry vs switch model vs stop" |
| Zeno progress test: geometric decay of per-step progress, limit v_∞; spectral radius ρ(Q) of the transient block | G1 per running task | is the agent converging *short* of the goal (steps-to-goal → ∞)? | switch / escalate / stop trigger; spec in RESEARCH-FIT-BACKLOG P6 |
| Frequent sub-sequence mining (PrefixSpan) on successful runs | G1 | recurring macro-steps → new templates | options/macro-actions |
| Contextual bandit (Thompson on Beta success × cost) | G3 | which template to fire; exploration | reuse `xval` P2C pattern + `route_advise` significance gate |
| Off-policy evaluation (IPS / doubly robust) | logged decisions | would the policy have beaten W0? | needs logged propensity (P3) |

## Phases and gates

### P0 — Labels and joins (instrumentation; no behaviour change)
- **Outcome events** `~/.apex-router/outcomes.jsonl`: `{session, task_id, task_type, workflow,
  outcome ∈ success|fail|escalate|abandon, evidence}`. Evidence, strongest first: tests ran and
  passed after the last edit; commit made; xval verdict GO; user `ok`/`bad` (`/apex outcome`); none.
  Infra failures (provider error, 5xx) recorded apart from capability failures — today all 4
  escalations were infra.
- **Task boundaries + ids**: a `task_id` per user turn in the main session (hook), propagated to
  subagents and, via a header, to proxy telemetry, with `workflow` / `step`.
- **Telemetry**: total duration (enables Little's law), `retry_of` linkage (system-design B6).
- **Action classifier** `actions.py` with a 300-call hand-labeled validation set; target ≥ 95%
  agreement, explicit `test` detection through wrappers.
- **Session coverage**: Codex fixed today (`session-id` header); verify pi and the null-session
  share; target ≥ 90% rows with a session.
- **Gate:** ≥ 200 labeled task outcomes across ≥ 3 task types, classifier ≥ 95%, session
  coverage ≥ 90%. At ~25 routed tasks/day this is 2–3 weeks.

### P1 — Graph build + readout (read-only)
- `graph_mine.py`: builds G1/G2 from transcripts + telemetry + outcomes; pure functions on numpy
  (no networkx: Tarjan, dominators, max-flow on ≤ 30 nodes are a few dozen lines each).
- `apex-router graph report [--task-type T] [--json]`: absorbing-chain table per (task, template),
  PageRank variants, dominators, bridges, min-cut, Markov-order test.
- Widget: one line under Quality — e.g. `paths debug: P(success) 0.82 · 41 steps · bottleneck
  opus 5h meter`.
- **Gate:** held-out check — fit on the first 70% of tasks, predict P(success) and steps on the
  rest; calibration error ≤ 0.1, and beats the pooled base rate (Brier skill > 0).

### P2 — Workflow valuation
- Score W0–W5 per task type by value; mine new templates from frequent successful sub-sequences.
- `apex-router graph advise <task description>`: classify → rank templates with value, P(success)
  CI, expected tokens/time, and *why* (dominating steps, bottleneck).
- **Gate:** a template beats W0 for a task type with a CI that excludes zero (the `route_advise`
  gate), else W0 stays the recommendation.

### P3 — Shadow recommendation
- On every main-session task the hook computes the recommendation and logs it with its
  propensity; nothing is shown. What the user actually did is logged alongside.
- **Gate:** off-policy estimate (doubly robust) says the policy beats observed behaviour on value
  with a CI excluding zero over ≥ 2 weeks.

### P4 — Suggest, then fire with confirmation
- The hook shows one line: `suggest W3 implement→xval: P(success) 0.86 vs solo 0.74 · +40k tok`.
  Accept → fired through the Workflow / Agent tools; decline is logged as a label.
- Firing guards: pressure GREEN (AMBER caps fan-out at the min-cut, RED = no fan-out), limit
  meter headroom > expected tokens × 2, no destructive tools in subagents, per-day token cap.
- **Gate:** acceptance ≥ 50% and accepted tasks' realized value ≥ predicted − 0.05.

### P5 — Auto-fire for whitelisted task types
- Thompson sampling over templates per task type, ε floor for exploration, guards as P4;
  per-type whitelist only after P4's gate holds for that type. Kill switch: one env var.

## Risks

- **Labels are the bottleneck.** Without P0 every downstream number is a guess; PageRank on
  unlabeled actions is a frequency chart.
- **Goodhart.** "Tests passed" can be gamed by a workflow that writes weaker tests; outcome evidence
  stays multi-source and xval-checked samples audit it.
- **Selection bias.** The user picks workflows for hard tasks → naive comparisons flatter solo.
  Hence propensity logging and off-policy estimates, not raw rates.
- **Non-stationarity.** Model upgrades (opus 4.8 → 5 → 5.5 in the pi logs) shift every rate;
  chains are fit on a sliding window with a model-version covariate.
- **Cost of firing.** Workflows multiply tokens; the value includes cost and the guards cap it.
- **Privacy.** Only action classes, counts and ids leave a transcript — never prompt text,
  commands or file contents (same rule as the widget and datapce).

## Reuse, not rebuild

`markov.py` (burst chain), `zeno.py` (p^n horizon), `route_advise.py` (significance gate),
`chain_planner.py` (ε-exploration with propensity), `xval_router.py` (P2C bandit + feedback),
`pressure.py` (fan-out gating), `quality.py` + snapshot widget (readout), Claude Code
Workflow / Agent tools (execution).

## Next step

P0 only: outcome log + task ids + action classifier with its validation set. Nothing in P1–P5
should start until P0's gate is met.
