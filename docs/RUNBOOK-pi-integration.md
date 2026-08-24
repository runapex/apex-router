# RUNBOOK — pi integration (per-task model/family switching)

Wire the [pi](https://github.com/earendil-works/pi) coding agent to apex-router so
that pi can **switch model and family per task**, and so every frontier/Kimi turn
flows through the apex-router measuring proxy while local turns go straight to
ollama.

There are two layers, usable independently or together:

| Layer | File | What it does |
|-------|------|--------------|
| **Proxy wiring** | `integrations/pi/models.json` | Points pi's `anthropic` + `moonshotai` providers at the apex-router proxy (`:8788`). pi always speaks its normal API; apex-router measures and can re-route underneath. |
| **Per-task router** | `integrations/pi/apex-route.ts` | A pi extension that switches the active model per task, by inline cue (`>>local …`) or sticky command (`/apex-route …`). |

## 1. Prerequisites

- pi installed (`pi --version`).
- The apex-router proxy running and healthy:
  ```bash
  apex-router serve            # starts the measuring proxy on :8788
  curl -s localhost:8788/status
  # {"status":"ok",...,"posture":"measure-only",...}
  ```
- ollama running with at least one Ornith tier pulled (for `>>local`):
  ```bash
  curl -s localhost:11434/api/tags | jq '.models[].name'
  ```

## 2. Install the proxy wiring

Merge the `providers` block from `integrations/pi/models.json` into
`~/.pi/agent/models.json` (create it if absent). If you have no other custom
providers you can copy the file wholesale:

```bash
cp integrations/pi/models.json ~/.pi/agent/models.json
```

Overriding only the `baseUrl` of the built-in `anthropic` and `moonshotai`
providers keeps pi's full model catalogue but sends the traffic through the
proxy. `~/.pi/agent/models.json` reloads whenever you open `/model` — no restart.

Verify pi sees the proxied models:

```bash
pi --list-models | grep -E 'anthropic|moonshotai'
```

## 3. Install the per-task router extension

```bash
pi install ~/.apex-router/integrations/pi/apex-route.ts     # persists in settings
# or, ad hoc for one session:
pi -e ~/.apex-router/integrations/pi/apex-route.ts
```

### Use it

**Inline cue** — prefix a single message with `>>`; only that task runs on the
family, and the prefix is stripped before the model sees it. (`>>` is used
rather than `@` because pi reserves `@` for file mentions.)

```
>>local    fix this flaky test          # local Ornith tier (ollama, no proxy hop)
>>kimi      summarise this diff          # Kimi K2 (via the apex proxy)
>>frontier design the migration plan     # Claude Sonnet (via the apex proxy)
>>deep     audit this for race hazards   # Claude Opus (via the apex proxy)
```

**Sticky switch** — changes the active model until you change it again:

```
/apex-route            # list families + show the active model
/apex-route local      # switch and stay there
```

The active family shows in the status bar (`⟿ local`).

### Customise the family table

Families resolve from the **shared model registry** `~/.apex-router/models.json` — the
same file codeqa's tier_router and `/learn` read, so a tier bump moves every component.
A family pins `{"provider","id"}`, references a tier `{"provider","tier"}` (resolved via
the registry's `tiers` map, optionally with `"effort"`), or — for `local` — follows the
ACTIVE ornith tier (`{"source":"ornith.env"}`, so `>>local` never loads a second tier).
Per-family overrides without touching the registry still work via
`~/.apex-router/pi-routes.json` (back-compat overlay):

```json
{
  "frontier": { "provider": "anthropic",  "id": "claude-sonnet-4-6" }
}
```

Keep explicit `id` values in sync with `pi --list-models`.

### Beyond switching

- `>>auto <task>` — `apex-router resolve` classifies the task and picks the model
  (adaptive core; static floor until gate cells promote).
- **Per-family effort** — a family's `"effort"` in the registry is applied to the
  anthropic payload per request (the cache-free output-cost dial).
- **Session attribution** — the extension adds `x-claude-code-session-id` (pi's session
  id) to every proxied request, so per-session cost reports include pi traffic.
- **Escalation auto-log** — a one-shot `>>cue` turn logs ok/escalated to `route-log`
  (observable failure only: provider error or empty answer).
- `/apex-offload codegen <spec> --tests <file>` — queue a gated-codegen job on the
  local tier (the lane that books frontier savings when tests pass).
- `apex-ground` (separate extension) — runs the deterministic grounding oracle on every
  assistant message that cites `file:line`; warns on STALE citations.

## 4. Test

```bash
# proxy reachable + measure-only posture
curl -s localhost:8788/status | jq '.status, .posture'

# models.json is valid and the proxied families load
pi --list-models | grep -E 'anthropic|moonshotai' | head

# the extension loads cleanly (no throw on startup) and registers its command
pi -e integrations/pi/apex-route.ts --list-models >/dev/null && echo "extension OK"
```

For an end-to-end check, start pi and run `>>local say hi` — the reply should come
from the Ornith tier, and `apex-router route-log` / `curl localhost:8788/stats`
should show the frontier turns that went through the proxy.

## 5. How it routes (mental model)

```
                 pi (/model, >>cue, /apex-route)
                 │
    ┌────────────┼─────────────────────────────┐
    │            │                              │
 >>local     >>kimi / >>frontier / >>deep    (built-in /model)
    │            │                              │
 ollama ──►  apex-router proxy :8788  ◄─────────┘
 (Ornith)        │  measure + route
                 ▼
        upstream frontier / Kimi backends
```

- pi owns the **manual** switch (`/model`, `Ctrl+P`) and the **per-task** switch
  (this extension).
- apex-router owns **measurement and routing underneath** a stable API surface —
  so you can change routing policy without touching pi config.
