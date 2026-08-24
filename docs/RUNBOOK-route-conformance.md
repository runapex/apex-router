# Runbook: tier-conformance log (`route-check`)

The conformance log records, per dispatch, the tier that was requested and
whether the model that actually ran matched it. It is a **companion** to the
escalation `route_log` (`apex-router route-log`): where `route_log` measures
when a low-confidence resolve climbs the escalation ladder, the conformance log
measures whether the resolved model is the one the tier table says it should be.

**Nothing here blocks a dispatch.** Like `route_log`, the conformance log is
fail-safe and measure-only: a write failure returns `False` silently, and the
dispatch continues. It never mutates routing.

---

## The three surfaces and their observability

| Surface | When used | `resolved_model` | `matched` | Counts toward drift rate? |
|---|---|---|---|---|
| `resolve` | `apex-router resolve` call resolves a task type to a model | the actual model id returned | `True`/`False` | yes |
| `pi` | Pi extension logs the model it handed to the API | the actual model id in the request | `True`/`False` | yes |
| `agent` | Claude Code `Agent(...)` dispatch | `None` (unobservable) | `None` (unobservable) | **no** |

### Why `agent` is intent-only

When Claude Code launches a subagent via `Agent(subagent_type=...)`, the
harness decides which model runs it — the calling code cannot observe what model
was ultimately selected. `log_agent_dispatch` records the task type and the
*requested* tier so there is a trace that a dispatch happened and what tier was
intended, but `resolved_model` and `matched` are `None` by construction.

`read_conformance` excludes rows where `matched is None` from the `observed`
count and from the drift-rate denominator. An agent-surface row therefore
**never inflates or deflates a drift number** — it is a dispatch witness, not a
conformance verdict.

### `>>local` in Pi is definitionally conformant

In the Pi extension, a `>>local` cue resolves to the on-box model. This is
always conformant by definition: the cue's intent is to route locally, and the
extension delivers that. `>>local` rows always carry `matched=True` by
definition — the active local family IS the resident model, so no expected-set
lookup is performed and a local cue can never register drift.

### Pi cue rows are bucketed under `task_type="cue"`

A bare `>><family>` cue is an explicit family switch, not a classified task, so every Pi
conformance row is logged under `task_type="cue"` (there is no per-task classification to
record) — which means `route-check` cannot give a per-task drift breakdown for the Pi
surface; use the `resolve` surface for per-task-type conformance.

---

## Reading `apex-router route-check`

```
surface  task_type              n  obs   drift
resolve  synthesis              8    8    0.00
resolve  debugging              4    4    0.25
pi       cue                   12   12    0.00
agent    explore                5    -  unobservable
agent    synthesis              3    -  unobservable
```

Column meanings:

| Column | Meaning |
|---|---|
| `surface` | `resolve`, `pi`, or `agent` |
| `task_type` | the task-type label passed at dispatch (e.g. `synthesis`, `debugging`) |
| `n` | total rows logged for this (surface, task_type) pair |
| `obs` | rows where `matched` is a bool (i.e. the resolved model was observable) |
| `drift` | `mismatches / observed`; blank (`-`) and labeled `unobservable` when `observed == 0` |

A non-zero drift rate on a `resolve` or `pi` row means that surface dispatched a
task type to a model outside the tier's expected set — something is misconfigured
or the tier table diverged from the actual model ids in use.

### `--json` flag

```bash
apex-router route-check --json
```

Emits a JSON object keyed by `"surface\ttask_type"` strings, each with:

```json
{
  "resolve\tsynthesis": {
    "n": 8,
    "observed": 8,
    "mismatches": 0,
    "drift_rate": 0.0
  },
  "agent\texplore": {
    "n": 5,
    "observed": 0,
    "mismatches": 0,
    "drift_rate": 0.0
  }
}
```

For agent rows: `observed == 0` and `drift_rate == 0.0` are structural — the
zero does not mean "no drift"; it means drift is unobservable. Always check
`observed` before interpreting `drift_rate`.

---

## Example: catching drift on the `resolve` surface

Suppose `opus` was remapped in `models.json` but the conformance log still shows
the old model id being resolved:

```
surface  task_type    n  obs  drift
resolve  synthesis    10  10   0.50
```

`route-check --json` gives:

```json
{
  "resolve\tsynthesis": {
    "n": 10,
    "observed": 10,
    "mismatches": 5,
    "drift_rate": 0.5
  }
}
```

Means 5 of the last 10 `synthesis` resolves returned a model not in the `opus`
tier's expected set. Check `models.json` and the `model_registry` tier table —
the alias is stale.

---

## Log location

Default: `~/.apex-router/conformance.jsonl`

Override: `APEX_CONFORMANCE_LOG=/path/to/custom.jsonl`

Each line is one JSON record:

```json
{
  "ts": 1753920000.0,
  "surface": "resolve",
  "task_type": "synthesis",
  "requested_tier": "opus",
  "resolved_model": "claude-opus-4-5",
  "matched": true,
  "note": ""
}
```

For an agent-surface row:

```json
{
  "ts": 1753920001.0,
  "surface": "agent",
  "task_type": "explore",
  "requested_tier": "sonnet",
  "resolved_model": null,
  "matched": null,
  "note": ""
}
```

---

## Emitting rows from code

### Resolve surface

```python
from apex_router.route_conformance import log_resolve_conformance

# called after resolve() returns a model id
log_resolve_conformance("synthesis", "opus", resolved_model="claude-opus-4-5")
```

`matched` is computed automatically: `resolved_model ∈ expected_models(requested_tier)`.
An unrecognized tier logs `matched=None` (no false mismatch).

### Agent surface

```python
from apex_router.route_conformance import log_agent_dispatch

# called before / alongside an Agent(...) dispatch
log_agent_dispatch("explore", "sonnet")
```

No `resolved_model` argument exists — the harness does not expose it. This is
intentional: we never claim a conformance verdict we cannot observe.

### Pi surface (via `route-check --record`)

The Pi extension calls the CLI's hidden `--record` path to avoid importing the
Python package directly:

```bash
apex-router route-check --record '{"surface":"pi","task_type":"cue","requested_tier":"deep","resolved_model":"claude-opus-4-8","matched":true}'
```

Fail-open: malformed JSON or a bad dict is a no-op; `route-check` exits 0.

---

## Relationship to the escalation route_log

| Log | Measures | Drift signal | Blocks dispatch? |
|---|---|---|---|
| `conformance.jsonl` (this runbook) | did the resolved model match the requested tier? | `drift_rate > 0` on `resolve`/`pi` | never |
| `route_log.jsonl` (`route-log` / `route-log --readout`) | did a low-confidence resolve escalate? | escalation rate | never |

Both logs are read-only observability tools. Neither mutates routing. They work
alongside `route-advise` (the recommendation layer), which can surface a
different tier suggestion but never forces a change.

---

## One-shot checklist

```
[ ] apex-router route-check                  — any drift on resolve or pi?
[ ] apex-router route-check --json           — machine-readable aggregate
[ ] check agent rows: observed==0 expected   — if observed > 0, the log is corrupted
[ ] drift_rate > 0 on resolve/pi             — check models.json tier table
[ ] drift_rate == 0, observed == n           — all dispatches conformant
[ ] update models.json tier table if needed  — apex-router resolve <task_type> to verify
```
