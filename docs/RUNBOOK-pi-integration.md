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
>>local    fix this flaky test           # active local family (ollama, no proxy hop)
>>kimi      summarise this diff           # Kimi K2 (via the apex proxy)
>>frontier  design the migration plan     # Claude Sonnet alias (medium effort)
>>deep      audit this for race hazards   # Claude Opus alias (high effort)
>>haiku     grep and summarise these files # Anthropic light tier (no effort knob)
>>sonnet    explore this subsystem         # Anthropic mid tier (medium effort)
>>opus      review this implementation     # Anthropic heavy tier (high effort)
>>fable     refute this load-bearing proof # Anthropic reasoning ceiling (max effort)
>>gpt-luna  locate the config loader       # GPT-5.6 Luna (Codex)
>>gpt-terra implement a feature           # GPT-5.6 Terra (Codex)
>>gpt-sol   audit a concurrency design    # GPT-5.6 Sol (Codex)
>>review    cross-validate this diff      # GPT-6.1 Sol via foundry-gpt, or openai-codex on a subscription setup
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
The Anthropic families follow the model-routing policy: `haiku` is the light,
mechanical tier (and receives no unsupported effort field), `sonnet` is mid/medium,
`opus` is heavy/high, and `fable` is the max-effort ceiling for exceptional pure
reasoning. `frontier` and `deep` remain compatibility aliases for `sonnet` and `opus`.
Routine independent cross-validation uses `review` — GPT-6.1 Sol, a different vendor than
the Claude families that write the code, so author and reviewer can disagree. `opus`/`deep`
stay the heavy Claude tier; Fable is opt-in when a review is explicitly escalated or
genuinely load-bearing.

The Claude families (and `/learn`) use pi's `foundry` provider: ids are Azure deployment
names, built as `provider_id_prefix[provider]` + the tier id (`it-entra-claude-sonnet-5-5`).
`review` uses a second provider, `foundry-gpt`, for Azure GPT through the proxy's
`/gpt5/openai` path. Add it to `~/.pi/agent/models.json`:

```json
"foundry-gpt": {
  "baseUrl": "http://127.0.0.1:8788/gpt5/openai",
  "api": "azure-openai-responses",
  "apiKey": "!az account get-access-token --resource https://cognitiveservices.azure.com --query accessToken -o tsv",
  "authHeader": true,
  "models": [{ "id": "it-entra-gpt-6.1-sol", "name": "GPT-6.1 Sol (foundry)", "reasoning": true,
               "input": ["text", "image"], "contextWindow": 272000, "maxTokens": 128000,
               "cost": { "input": 15, "output": 60, "cacheRead": 1.5, "cacheWrite": 0 },
               "thinkingLevelMap": { "off": null, "minimal": null, "max": null } }]
}
```

The gateway 404s on pi's default Azure `api-version` (`v1`), so pin the one Codex uses in
`~/.pi/agent/auth.json`:

```json
"foundry-gpt": {
  "type": "api_key",
  "key": "!az account get-access-token --resource https://cognitiveservices.azure.com --query accessToken -o tsv",
  "env": { "AZURE_OPENAI_API_VERSION": "2025-04-01-preview" }
}
```

Check: `pi -p --model foundry-gpt/it-entra-gpt-6.1-sol --thinking low "say pong"`.

**Subscription setup (no Foundry).** On a machine where Claude runs on a Claude Pro/Max
subscription (OAuth) and GPT on a ChatGPT/Codex subscription, pi cannot use the Claude
families: Anthropic bills a third-party client's subscription OAuth to extra usage and
rejects the request with `400 … Third-party apps now draw from your extra usage` when none
is available. Claude stays in Claude Code; pi's Claude-named families move to `openai-codex`.
Merge `integrations/pi/registry-overlay.subscription.json` into `~/.apex-router/models.json`,
replacing each family whole (a plain deep merge, `jq '.[0] * .[1]'`, leaves the old `tier` next
to the new `id`; the `id` wins, but the leftover is misleading):

```bash
cd ~/.apex-router && cp models.json models.json.bak
jq -s '.[0] as $b | (.[1] | del(._comment)) as $o | ($b * $o)
       | .pi_families = (($b.pi_families // {}) + $o.pi_families) | .learn = $o.learn' \
  models.json.bak integrations/pi/registry-overlay.subscription.json > models.json
```

A legacy `~/.apex-router/pi-routes.json` still wins per family — move it aside if it pins
`anthropic`.


| family | model (openai-codex) | effort |
|---|---|---|
| `haiku` | gpt-5.6-luna | low |
| `sonnet`, `frontier` | gpt-5.6-terra | medium |
| `opus`, `deep` | gpt-5.6-sol | high |
| `fable` | gpt-6.1-sol (272k ctx, not 1M) | xhigh |
| `review` | gpt-6.1-sol | high |
| `/learn` | validate gpt-5.6-terra, explain gpt-5.6-sol | — |

`>>auto` follows the same remap: a tier id from `resolve` (e.g. `claude-sonnet-5-5`) is sent
through that tier's family. The overlay keeps `tiers` on the Claude ids — Claude Code,
codeqa and route conformance still read them. An overlay family that pins an `id` replaces a
default `tier` (it no longer deep-merges into it). Check:
`pi -p --model openai-codex/gpt-6.1-sol --thinking low "say pong"`.

Outcome logging on this setup: the extension logs each pi turn with the model that ran as
`--start-tier` (e.g. `gpt-5.6-terra`) and the cued family as `--family` (e.g. `sonnet`). The
tier comes from the model, not the family name, so these rows land in the GPT tiers
`gpt-luna` < `gpt-terra` < `gpt-sol` < `gpt-sol-6.1`, never in a Claude tier. `route-readout`
and `route-join` show them as indented per-tier rows under each task type. The headline
per-task-type rate, which `route-advise` prices at Claude cost, leaves them out. Escalation
is ranked within one family only: a row that asked for a Claude tier and ran on GPT, or the
other way round, is marked `cross_family` and is left out of every rate. `route-check` does
not count a remapped family as drift: the extension checks the resolved id against the
family's id, and a pi row recorded without a verdict is checked against the family's id in
the active `models.json`.

The `gpt-*` families use Pi's `openai-codex` provider: `gpt-luna` (low
reasoning), `gpt-terra` (medium), and `gpt-sol` (high). They use the existing Codex
provider directly; they are not sent through the Anthropic/Kimi measuring proxy.

Per-family overrides without touching the registry still work via
`~/.apex-router/pi-routes.json` (back-compat overlay):

```json
{
  "frontier": { "provider": "anthropic",  "id": "claude-sonnet-4-6" }
}
```

Keep explicit `id` values in sync with `pi --list-models`.

#### Declaring a machine-local family (`local_families` overlay)

The `>>local` family resolves to whichever entry `LOCAL_FAMILY` names in the
`local_families` section of `~/.apex-router/models.json`. Ornith ships as the
default, but you can declare any ollama-served model here without touching source.

**Shape** (`~/.apex-router/models.json` fragment):

Add only the *new* family — the committed `ornith` family (with its measured tier
sizes) is already present and is merged under your overlay, so do **not** redeclare
it here. The bare-string form below is a convenience shorthand for `{"api_model": …}`;
it carries no `weights_gb`, so a shorthand tier is treated as unknown-size and skips
the RAM fit-check. Give a `weights_gb` (object form) for any tier you want the
capacity gate to guard.

```json
{
  "local_families": {
    "mymodel": {
      "small": "mymodel:8b-q4",
      "large": { "api_model": "mymodel:27b-q4", "weights_gb": 18 }
    }
  }
}
```

**Selection** — `~/.apex-router/ornith.env` pins the active family and tier:

```
LOCAL_FAMILY=mymodel
ORNITH_TIER=small
ORNITH_API_MODEL=mymodel:8b-q4
```

Switch via `apex-router ornith-tier --family mymodel small` (writes `ornith.env`
and restarts the Ornith launchd consumers (`com.ornith.worker`, `com.ornith.overnight`)).
Those units read `ornith.env` at startup — no plist edits required.

### Beyond switching

- `>>auto <task>` — `apex-router resolve` classifies the task and picks the model
  (adaptive core; static floor until gate cells promote).
- **Per-family effort** — a family's `"effort"` in the registry is applied through
  Pi's thinking-level API per request: `output_config.effort` for Anthropic and
  `reasoning_effort` for OpenAI/Codex (the cache-free output-cost dial).
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
pi --list-models | grep -E 'anthropic|moonshotai|openai-codex' | head

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
 >>local     >>kimi / Anthropic tier cues    (built-in /model)
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
