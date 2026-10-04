# datapce (apex-router)

datapce shows what is actually happening on this machine while you work with subagents:
upstream **pressure** (rate limits and transport faults, GREEN/AMBER/RED), what the session and
each subagent **cost** (tokens including cache reads and writes, duration), and **every
dispatch** — tier requested and tier that ran, outcome, time. It does not plan; it puts those
numbers where existing planners and dispatchers (superpowers' writing-plans and
subagent-driven-development, the `Workflow` tool, the `Agent` tool) and you can see them.

One product, one install (alias `apex-router`):

```
claude plugin marketplace add runapex/apex-router
claude plugin install datapce@datapce
```

> An advise-only view of your subagents: a status band and an `/apex` pane with pressure, cost
> and the dispatch list, a session-handoff prompt, and a short pressure note in Agent/Workflow
> planning when the upstream is under pressure. It never changes the model you chose. Its
> ledger per task type and tier records completion and cost — not answer quality; tier advice
> arrives with a quality label in v1.1. Optional local-model lanes.

**Privacy.** datapce writes only under `~/.apex-router/` and its own plugin store. It never stores prompt text, file contents, or command text; descriptions are truncated dispatch labels. No network calls. No telemetry to anyone.

The pip package `apex-router` documented below is the optional backend: local model lanes,
cross-client proxy telemetry, nightly learning. The plugin works without it.

> **New in 0.4:** the datapce plugin. The `agent-route-log` and `cache-handoff` hooks are
> deprecated and keep running beside it until 0.4.1; see [CHANGELOG.md](CHANGELOG.md).

---

## Table of contents

- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Install](#install)
- [The measuring proxy](#the-measuring-proxy-optional-proxy-extra)
- [The offload subsystem](#the-offload-subsystem)
- [SKILL.state execution state (measured, default-off)](#skillstate-execution-state-measured-default-off)
- [Background watchers](#background-watchers)
- [Proxy client setup](#proxy-client-setup)
- [Team skills (private marketplace)](#team-skills-private-marketplace)
- [Telemetry — reading and sharing it](#telemetry--reading-and-sharing-it)
- [Pressure gate, review pre-read, and the route-label hook](#pressure-gate-review-pre-read-and-the-route-label-hook)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Security posture](#security-posture)
- [License](#license)

---

## Architecture

Four layers, deliberately decoupled — you can adopt any one without the others. Only the
routing core is required and pure-stdlib; the proxy and its tuner are optional extras.

```
┌─ routing core (pure stdlib, zero deps — the only required layer) ─────────────┐
│  task → classify → cell → route table → resolve (fallback to static) → model  │
│                              ▲                                                 │
│   corpus steps → replay bench → gate (out-of-sample, FDR-corrected) → table   │
└───────────────────────────────────────────────────────────────────────────────┘
┌─ measuring proxy  [proxy] extra ─────────────────────────────────────────────┐
│  client → proxy (byte-identical forward) → provider; tees usage → telemetry   │
│           offline tuner compiles a signed policy from the captured corpus      │
└───────────────────────────────────────────────────────────────────────────────┘
┌─ offload subsystem (local model does the work, measured) ────────────────────┐
│  queue_task → jobs/inbox → worker → dispatch(lane) → local model → telemetry  │
│                 lanes: codegen (gated by tests) · review (pre-filter) · adhoc  │
└───────────────────────────────────────────────────────────────────────────────┘
┌─ toolkit (the evidence source) ──────────────────────────────────────────────┐
│  codeqa: grounded code-Q&A + freshness gate   ornith: local tiered client     │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Routing core (`apex_router`)
- `classify.py` — two-signal task classifier (free request-marker prior + optional
  embedding refinement) → one of debug/explore/review/refactor/generate.
- `gate.py` — promotion gate: sample floor, out-of-sample confirmation split,
  Benjamini-Hochberg FDR across cells, replication across capture windows.
- `route_table.py` / `consumer.py` — `resolve()` reads the table, falls back to your
  static default on any miss/ambiguity, and never names a model the machine can't run.
- `stats.py`, `store.py`, `bench.py`, `embed.py` — pure-stdlib supporting pieces.

### Offload subsystem (`apex_router.ornith`)
The layer that actually runs work on a local model and **measures whether it paid off**:
- `queue_task.py` — enqueue a job (lane: `adhoc` / `codegen` / `review`).
- `ornith_worker.py` — polls `jobs/inbox`, dispatches one at a time (single-GPU serialized),
  routes to `jobs/done` / `jobs/failed`, emits one telemetry row per job.
- `dispatch.py` — routes a job by lane to the right handler.
- `offload_lanes.py` — the lane logic:
  - **codegen** — generates code (thinking-OFF), **runs the caller's tests**, escalates on
    failure. Only a passing gated run counts as frontier work saved.
  - **review** — a recall pre-filter (measured ~1/5 precision) that **always escalates**
    for frontier triage. Its only possible benefit (cheaper frontier triage) is unmeasured
    while its tokens are booked pure cost, so the lane is **default-off** (measured
    net-negative): review jobs escalate immediately without spending local tokens.
    `ORNITH_REVIEW_LANE=on` re-enables the local pre-filter.
  - **adhoc** — a raw thinking-OFF chat.
- `offload_telemetry.py` — per-lane JSONL; `frontier_completion_tokens_saved` counts a call
  **only if gated AND ok AND NOT escalated** (see decisions below).
- `offload_report.py` — per-lane economics + reads codeqa's own logs into one view.

### Toolkit (`apex_router.codeqa`)
Grounded code-Q&A (ripgrep retrieval + local model answering with `file:line` citations)
and a freshness gate that checks a doc/digest's claims against live code.

---

## Design decisions

The non-obvious calls, and why:

1. **Static default is the floor, always.** A data-starved routing cell defers to your
   hand-authored default. "Adaptive" is *earned* from evidence, never assumed on install.
   This makes adopting apex-router strictly safe — worst case, you get your static routing.

2. **Promotions need statistical evidence, not a win count.** Out-of-sample confirmation +
   FDR correction + replication across windows. A model that looks better on the sample it
   was picked on does not get promoted; this is the difference between measurement and
   confirmation bias.

3. **The savings metric refuses to flatter itself.** `frontier_completion_tokens_saved`
   counts a local call only when it was **gated** (a real correctness check ran), **ok**
   (it passed), and **not escalated** (it wasn't also sent upstream). A raw completion, or
   a review that always escalates, contributes **zero**. Anything looser would inflate the
   number — the whole point is an honest "did local offload actually save frontier work?"

4. **Local codegen is gated by the caller's tests, or it doesn't count.** A wrong local
   answer the frontier must redo costs *more* than not offloading. So the codegen lane runs
   the tests and escalates on failure; the token saving is only booked when the code passes.

5. **No agentic grading of untrusted code.** codeqa's frontier judge is opt-in and
   HTTP-only — never routed through the local `claude`/`codex` CLIs, which are agentic
   (tools/hooks/MCP) and could execute code if scanned source is adversarial.

6. **Two watchers, not one.** The drain worker (always-on daemon) and the daily report
   (scheduled one-shot) have different lifecycles; merging them would break one or the
   other. They install together but run independently.

7. **Pure-stdlib core.** The routing decision has zero third-party deps so it runs on a
   box with only the Claude, Codex, and Kimi CLIs and no model server. The harness
   deploys to **both the Claude CLI and Pi** — the same `models.json` registry and
   `ornith.env` state serve both.

8. **Imported ideas are benched, not believed.** SKILL.state (arXiv:2608.26263 — replace
   growing transcripts with explicit execution state) was adapted in four places, each
   behind a flag with an A/B harness. Measured across three settings (local codegen, GPT
   tool loop, drift recovery): behavioral parity everywhere, token parity at apex's real
   horizons. So everything ships **default-off** — the harnesses are the product; re-run
   them when the model, corpus, or horizon changes. See
   [docs/DESIGN-skill-state.md](docs/DESIGN-skill-state.md).

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/runapex/apex-router/main/install.sh | bash
```

Arch-aware and idempotent:

| Component | Installed | Notes |
|---|---|---|
| Python ≥3.11, uv | if missing | required |
| `apex-router` package | always | pure stdlib, near-instant |
| ollama + `nomic-embed-text` | if missing | embedding-refinement classifier (optional) |
| Ornith 1.5 tiers (via ollama) | any platform ollama supports | local bench / codegen / review. Pulls the small tier by default; `--ornith-tier large\|both` for the big one, `--ornith-serve` for the queue-worker launchd agents |
| starter route table | always | empty → resolves to your static defaults until a bench fills it |
| background watchers | **only with `--watch`** | drain worker + daily report (see below) |
| datapce plugin | default (needs the `claude` CLI) | adds the datapce marketplace from the install directory and installs `datapce@datapce`; no longer adds `apex-router-skills` |
| measuring proxy | **only with `--proxy`** | the `[proxy]`/`[tuner]` extras; installed, not auto-started |

Flags: `--no-ornith` (skip the local model pulls), `--ornith-tier small|large|both` (which tier to
pull and activate; default `small`), `--ornith-serve` (macOS: install the Ornith queue worker +
nightly cycle as launchd agents), `--no-embed`, `--watch` (install watchers at first run), `--proxy` (install
the measuring proxy + its extra), `--proxy-config <file>` (wire Claude Code through a proxy),
`--skills-marketplace <git-url>` (add another skill marketplace, e.g. `runapex/apex-router-skills`, or print the wiring for a private team one — see
below), `--dir PATH`, `--verify-only`.

### Local model families (default: Ornith 1.5)

A **family** is a named group of tiers (typically `small` and `large`) declared in the
`~/.apex-router/models.json` overlay under the `local_families` key. The active family
and tier are selected by `~/.apex-router/ornith.env` via two variables:

- `LOCAL_FAMILY` — which family is active (e.g. `ornith`, `mymodel`, …)
- `ORNITH_TIER` — which tier within that family is active (`small` or `large`)

**Ornith 1.5 ships as the default family.** Adding another local model is a config edit
(`local_families` entry in the overlay + `apex-router ornith-tier --family <name> <tier>`)
— not a source change.

Local inference runs on **ollama** (`:11434`) — the same instance that serves `nomic-embed-text`.
The old single-model server is **retired** (it pinned one model at process start, which made
switching sizes a restart-and-reload); ollama serves both tiers and switches on demand.

Two tiers in the default Ornith 1.5 family, one resident at a time:

| Tier | Model | Weights | Shape |
|---|---|---|---|
| `small` | `hf.co/ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M` | ~5.6 GB | dense 9B — bulk/triage, coexists with everything else |
| `large` | `hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M` | ~21 GB | 35B-A3B MoE, 3B active/token — fidelity, synthesis, codegen |

> There is **no 27B**. Upstream Ornith 1.5 ships 9B, 35B-A3B and 397B; 397B does not fit a single
> workstation. The 35B-A3B is the practical "big" tier — being MoE, it decodes near a 3B model.

```bash
# default Ornith family commands (back-compat; --family defaults to "ornith"):
apex-router ornith-tier                         # what's configured, pulled, and actually resident
apex-router ornith-tier large                   # unload old tier, write the new one, warm it, restart consumers
apex-router ornith-tier --unload                # free RAM without changing the configured tier
apex-router ornith-tier --json                  # machine-readable

# with --family, the same command manages any declared local family:
apex-router ornith-tier --family mymodel small  # switch the active family+tier for "mymodel"
```

`LOCAL_FAMILY` and `ORNITH_TIER` in `~/.apex-router/ornith.env` are the single source of
truth: the launchd units carry **no** model id, so switching never means editing a plist.
Switching is never implicit — `model_router.select()` reports `needs_switch` and names the
model it wants, but will not trigger a multi-GB load as a side effect of asking for a route.

Capacity is checked against **physical RAM**, not a hardcoded ceiling, and a switch unloads the
outgoing tier *before* warming the incoming one — both tiers resident is ~27 GB.

**Persistent local Ornith (macOS).** Pass `--ornith-serve` to install two launchd agents:
`com.ornith.worker` (the offload queue) and `com.ornith.overnight` at 01:30 (nightly maintenance;
a no-op unless you've queued training data). Model *serving* is ollama's own service — apex-router
does not supervise it. The **worker is not auto-started** (it would drain the queue while the tier
is still cold); warm the tier, then kick it off once:

```bash
apex-router ornith-tier small
launchctl kickstart gui/$(id -u)/com.ornith.worker
```

Manage: `launchctl kickstart -k` (restart) / `bootout` (stop) any `com.ornith.*` label.

No model gateway required — the target uses its own Claude + Codex subscriptions.

### Verify

```bash
apex-router status     # which tiers are live (routing / embedding / ornith)
apex-router verify     # exits 0 if routing works
```

### Platform support

- **Routing core + codeqa + offload client:** macOS and Linux.
- **Local model tiers (Ornith 1.5 via ollama):** anywhere ollama runs — macOS *and* Linux, Apple
  Silicon or not. This used to be Apple-Silicon-only because it required MLX; retiring the MLX
  server removed that constraint. `--ornith-serve` (macOS) adds the queue-worker launchd agents.
- **Watchers:** launchd on macOS, systemd `--user` on Linux.

---

## The offload subsystem

Run work on the local model, off the interactive path, and measure whether it saved
frontier tokens.

```bash
# start the worker (or install it as a watcher — see below)
python -m apex_router.ornith.ornith_worker

# gated codegen — the ONLY lane that books savings (must pass the tests)
python -m apex_router.ornith.queue_task --lane codegen \
  --spec "write clamp(x, lo, hi)" --tests-file test_clamp.py

# review pre-filter — DEFAULT-OFF (measured net-negative): escalates without local
# tokens unless ORNITH_REVIEW_LANE=on
python -m apex_router.ornith.queue_task --lane review --diff-file change.diff

# adhoc chat
python -m apex_router.ornith.queue_task --task "summarize this" --context notes.md

# read the economics anytime
python -m apex_router.ornith.offload_report
```

The worker picks up anything in `jobs/inbox/` within 5s, serialized (single GPU).

---

## SKILL.state execution state (measured, default-off)

Adaptation of arXiv:2608.26263 (replace an append-only conversation with an explicit,
mutable execution state per step: prompt = immutable spec + state + latest observation;
reasoning discarded after each validated state update). Four adaptations, each with an
A/B harness — and, per the project doctrine, each **off by default** because the
measurements said so:

| piece | flag / command | measured result |
|---|---|---|
| state codegen lane (local) | `ORNITH_CODEGEN_STATE_LANE=on` | 17/17 first-attempt passes both arms → repair loop never engages; pure overhead today |
| codegen A/B bench | `python -m apex_router.ornith.state_bench [--suite s.jsonl]` | pass-rate CIs, tokens/pass, taxonomy (paper §5.7 labels) |
| (P,Σ,O) frontier driver + bench | `python -m apex_router.proxy_engine.tuner.driver_bench [--live]` | behavior parity; token parity at 4-round horizon |
| GPT bench via codex exec | `python -m apex_router.proxy_engine.tuner.codex_driver_bench [--drift]` | identical refs/answers both arms; **drift: both recovered** — no anchoring on a frontier model |
| structured session handoff | automatic (datapce handoff toast; the cache-handoff-nudge hook is deprecated in 0.4.0, retired in 0.4.1) | 6-field state block replaces prose handoff; `python -m apex_router.handoff_state validate <file>` |
| `/learn` chain contract | automatic (pi extension) | VALIDATE emits a JSON verdict Σ; EXPLAIN consumes (P, Σ); fail-open to legacy |

The design record — including the negative results and when to re-run — lives in
[docs/DESIGN-skill-state.md](docs/DESIGN-skill-state.md).

---

## Background watchers

Two jobs, one command, cross-platform:

```bash
apex-router watch install     # launchd (macOS) or systemd --user (Linux)
apex-router watch status
apex-router watch uninstall
```

- **drain** — always-on worker draining the local job queue (KeepAlive / Restart=always).
- **daily** — once-a-day report + codeqa freshness refresh (calendar-triggered 09:00).

Install is idempotent, reversible, and pins the installing interpreter (a venv install
stays self-contained). The installer can do this at first run with `--watch`; it never
auto-starts a daemon without that consent.

---

## The measuring proxy (optional `[proxy]` extra)

apex-router bundles a measuring **proxy** (`apex_router.proxy_engine`) that fronts your model
provider, forwards every request **byte-identically**, and measures composition/usage for an
offline policy tuner — a strict superset of a plain passthrough. It is **opt-in and isolated**:
the routing core stays pure-stdlib, and the proxy's heavy deps ship only in the extra.

```bash
pip install 'apex-router[proxy]'        # starlette, uvicorn, httpx, brotli, numpy
pip install 'apex-router[proxy,tuner]'  # + the offline tuner (scipy, tiktoken)

apex-router serve                        # run the proxy (127.0.0.1:8788 by default)
apex-router proxy doctor                 # cache-cost report from telemetry
apex-router proxy --help                 # serve / doctor / compile / readout / ask
```

Point it at your provider (or a gateway) via env — nothing internal is hardcoded:

```bash
export APEX_ANTHROPIC_UPSTREAM=https://api.anthropic.com   # default; set to your gateway if any
export APEX_OPENAI_UPSTREAM=https://api.openai.com
export APEX_PORT=8788
```

Without the extra, `apex-router serve` prints a one-line install hint instead of a traceback,
and the pure-stdlib routing core is completely unaffected.

> ⚠️ **Local data persistence.** The proxy keeps a local SQLite store (under `~/.apex/` by
> default, `APEX_HOME` to relocate) that persists **request/response content bytes** for
> cache-freeze and divergence analysis. This never leaves your machine — there is no telemetry
> egress, and the emitted JSONL records only token counts, byte-class sizes, and timing (no prompt
> text). But the on-disk store DOES contain plaintext content; treat `~/.apex/` as sensitive,
> and set a short `APEX_RETENTION_DAYS` (default 14) if you don't want it retained.

## Proxy client setup

If your machine routes Claude Code through a local proxy (e.g. a measuring/routing proxy in
front of your model backend), apex-router can **replicate that client wiring** into
`~/.claude/settings.json` — but it hardcodes **nothing**. The values come from your
environment or a config file:

```bash
cp proxy.env.example proxy.env      # then edit proxy.env with YOUR proxy url / model ids
./install.sh --proxy-config proxy.env
# or, if the keys are already in your environment:
apex-router setup-proxy             # merges them; apex-router setup-proxy --dry-run to preview
```

It **merges** (never overwrites) — your existing `permissions`, `hooks`, `enabledPlugins`,
and unrelated `env` keys are preserved, and a `.apex-bak` backup is written before any edit.
The keys it manages are non-secret client wiring (`CLAUDE_CODE_USE_FOUNDRY`,
`ANTHROPIC_FOUNDRY_BASE_URL`, the `ANTHROPIC_DEFAULT_*_MODEL` mappings, prompt-cache flag).
**Any auth your proxy itself needs lives in the proxy's own environment, never here** —
`proxy.env` is gitignored so a filled-in copy is never committed.

### pi integration (per-task model/family switching)

The [pi](https://github.com/earendil-works/pi) coding agent can point at the apex-router
proxy and switch **model and family per task** — inline (`>>local fix this test`,
`>>sonnet explore this`, `>>opus cross-validate this`, `>>fable refute this proof`, or
`>>auto <task>` letting the adaptive core pick) or with a sticky `/apex-route` command.
The Anthropic cues mirror the routing tiers: Haiku (light), Sonnet (mid), Opus (heavy
coding and routine cross-validation), and opt-in Fable (maximum pure reasoning).
Frontier and Kimi turns flow through the measuring proxy;
local turns go straight to the Ornith tiers on ollama. Families resolve from the shared
model registry (`~/.apex-router/models.json`) — the same file codeqa and `/learn` read.
The extensions also: attribute pi traffic per-session through the proxy, apply per-family
reasoning effort, auto-log cue outcomes to `route-log`, ground citations on every
assistant message (`apex-ground`), and queue offload jobs (`/apex-offload`). Drop-in
pieces live under [`integrations/pi/`](integrations/pi/):

```bash
cp integrations/pi/models.json ~/.pi/agent/models.json     # route anthropic+moonshotai via the proxy
pi install integrations/pi/apex-route.ts                    # the per-task router extension
pi install integrations/pi/apex-ground.ts                   # citation grounding on every turn
```

Full instructions, the family table, and testing steps:
[`docs/RUNBOOK-pi-integration.md`](docs/RUNBOOK-pi-integration.md).

### Local book references (booksearch)

Index a folder of PDF books locally and, while working on a problem, pull the **Top-K
most relevant books with a one-line reason each** — retrieval by local `nomic-embed`,
reasoning by the local Ornith tier, storage in one SQLite file. Nothing leaves the box.

```bash
./install.sh --books-index          # [books] extra + `booksearch` wrapper + pi/claude commands
booksearch ingest                   # one-time index of ~/books (incremental, resumable)
booksearch query "how do B-trees stay balanced?" -k 5
```

Callable from pi (`/books <problem>`) or Claude Code (`/books <problem>`). Full guide:
[`docs/RUNBOOK-booksearch.md`](docs/RUNBOOK-booksearch.md).

## Team skills (private marketplace)

apex-router ships **no skills in this repo** and hardcodes no private URL. Internal skills (team
ops, workflows) belong in a **private** Claude Code plugin marketplace — a separate
git repo you control — so they never land in this public repo.

Point the installer at yours (URL taken from a flag/env, never baked in):

```bash
./install.sh --skills-marketplace ssh://YOUR-GIT-HOST/team/skills-bundler.git
# or: export APEX_SKILLS_MARKETPLACE=ssh://... && ./install.sh
```

It prints the two commands to run inside Claude Code:

```
/plugin marketplace add <your-private-git-url>
/plugin install <plugin>@<marketplace-name>     # e.g. team-ops@skills-bundler
```

A marketplace repo is just `.claude-plugin/marketplace.json` listing plugins, each a
folder of `SKILL.md` bundles — Claude Code's native mechanism, so updates propagate on
`git pull`. Keep internal-only content in that private repo, never here.

**Workflow skills.** The ten former `apex-router-skills` skills — `model-routing`,
`cross-validate`, `verify-claims`, `disciplined-execution`, `public-repo-hygiene`,
`local-references`, `change-classification`, `evidence-labels`, `unattended-loop`,
`dependency-vetting` — are condensed into the one `datapce` skill that ships with the plugin
(Route · Verify · Review · Ship); their names stay in its description so they still trigger.
The `apex-router-skills` marketplace stays available (and is how Pi gets them):

```
./install.sh --skills-marketplace runapex/apex-router-skills
```

## Telemetry — reading and sharing it

The offload subsystem writes measure-first telemetry locally. **Nothing is transmitted
anywhere by default** — apex-router has no phone-home.

**Where it lives:**
- `~/.apex-router/logs/` — watcher stdout/stderr.
- The offload telemetry JSONL (per-lane token/verdict rows) and the daily digest
  (`offload_daily.md`) under your apex-router home.

**Read it:**
```bash
python -m apex_router.ornith.offload_report      # per-lane NET-POSITIVE / MEASURE-ONLY verdicts
```

**Share it with your team (opt-in, manual):** the telemetry is content-free by design —
it records token counts, lane, and pass/fail verdicts, **never source text or prompts**.
To share:
1. `python -m apex_router.ornith.offload_report > offload-report.txt` — the aggregate only.
2. Or hand off the raw JSONL if your team pools measurements; scrub paths first if any
   job embedded one. There is no built-in uploader — sharing is a deliberate copy, so
   telemetry never leaves the machine unless you send it.

---

## Pressure gate, review pre-read, and the route-label hook

Three small tools that close the loop between what the proxy measures and how an agent
dispatches work. All three are measure-first: none of them blocks a dispatch or edits a
routing decision.

### `apex-router pressure` — is upstream pushing back right now?

Reads the last 15 minutes of proxy telemetry (backwards from the tail, never the whole
file) and reports the 429 rate, transport-error rate, retried connects, p50
time-to-first-token and in-flight agents, per model family and overall, then names a level:

| Level | Condition (overall) | Recommendation |
|---|---|---|
| GREEN | 429 rate < 2 % **and** transport < 3 % | dispatch as planned |
| AMBER | 429 rate 2–10 % **or** transport 3–10 % | shed mechanical/exploration agents one tier down; cap parallel heavy agents at 2 |
| RED | either rate > 10 %, **or** a `retry-after` seen in the last 2 min | no new heavy fan-out; serialize; wait the `retry-after` before retrying heavy |
| UNKNOWN | telemetry missing or unreadable | no signal — dispatch conservatively, as for AMBER |

`--check` turns the level into an exit code (0 GREEN / 1 AMBER / 2 RED / 3 UNKNOWN; 4 is a
usage error) so a hook or a skill can gate a fan-out on it:

```bash
apex-router pressure                    # table; also written to ~/.apex-router/pressure.json
apex-router pressure --check || echo "shed or hold"
```

Under 10 requests in the window a rate is noise, so the level reads
`GREEN (insufficient sample)` — except that a fresh `retry-after` always forces RED, because
that is the provider asking us to back off. The `model-routing` skill consults it before
every fan-out of two or more agents. Runbook:
[`docs/RUNBOOK-pressure.md`](docs/RUNBOOK-pressure.md).

### `apex-router review-preread` — a local first pass, phrased as claims

```bash
git diff HEAD~1 | apex-router review-preread --markdown
apex-router review-preread change.diff --requirements req.md --max-findings 8
```

The local Ornith tier reads a unified diff and lists **claims to verify** (`P1…Pn`, each
with file, line hint, severity, and how to check it). An independent heavy reviewer then
confirms or refutes each one and adds what it missed. Because the pre-read comes from a
different producer than the code's author, it can sit beside the diff without leaking the
author's reasoning into the review. It is advisory: local review precision has measured
at roughly 1 in 5, so the reviewer still reads the whole diff, and the only number that can
justify the cost is **pre-read recall** (confirmed claims ÷ all confirmed findings), which
the `cross-validate` skill records in each review note.

The diff is untrusted input: it sits between nonce delimiters, and a deterministic scan
flags text aimed at the reviewer (`injection_markers`) before the model sees it. Cost is
booked on its own telemetry lane (`lane="preread"`) and counted as pure cost until a
recall/lift measurement shows a benefit. Exit codes: 0 answered (zero findings is a valid
answer), 2 empty diff, 3 model call failed — treat 3 as "no pre-read", never as "no
findings". Runbook: [`docs/RUNBOOK-review-preread.md`](docs/RUNBOOK-review-preread.md).

### The route-label hook — one row per Claude Code subagent dispatch

The outcome router's blind spot was Claude Code itself: subagent dispatches never reached
the route log, so there was nothing to label. `hooks/agent-route-log.sh` (deprecated in 0.4.0; the datapce
plugin replaces it; retired in 0.4.1) is a `PostToolUse`
hook (matcher `Agent`) that appends one label-pending row per dispatch. `apex-router
route-join` then infers escalations offline (a later same-description dispatch at a
strictly higher tier) and joins the proxy telemetry on `(session_id, agent_id)`; the
nightly pass runs the join. The hook is retired in 0.4.1 once `route-join --json` reports
`stats.writer_parity.gate_open` (span ≥ 14 days, ≥ 10 matched days, last matched day within 2 days).

```bash
./install.sh --agent-route-log-hook     # wires the hook into ~/.claude/settings.json
apex-router route-join                  # or wait for nightly
apex-router route-check                 # tier-conformance readout
```

The hook is fail-safe by contract: it never blocks, prints nothing, always exits 0, and a
3 s alarm bounds its body. Keep a `timeout` on the hook entry so interpreter startup is
bounded too. Runbook:
[`docs/RUNBOOK-route-conformance.md`](docs/RUNBOOK-route-conformance.md).

### Proxy: bounded transport retries

The proxy retries a transient connect-phase failure (`SSLError`, `ReadError`,
`RemoteProtocolError`, `ConnectError`) only while no byte has reached the client, and after
the body was sent only when the attempt fast-failed (under `APEX_RETRY_FAST_FAIL_MS`,
default 3000) on a stateless completion endpoint (`/v1/messages`, `/v1/chat/completions`,
`/responses`; never `/batches` or `/files`). Hard caps bound retries and cumulative backoff.
Retried rows carry `connect_retries > 0`, and telemetry schema 8 adds `upstream_rejected`,
so a flaky upstream shows up in `pressure` even when the request eventually succeeded.

---

## Cache-cost optimization toolkit (`scripts/`)

Four offline, measure-first tools for understanding and reducing prompt-cache
read cost. They read the telemetry the proxy already writes (and Codex's own
rollout files) — no proxy restart, no model call, nothing transmitted. Full
guide: [`docs/RUNBOOK-cache-cost.md`](docs/RUNBOOK-cache-cost.md).

| Tool | What it answers |
|---|---|
| `scripts/cache_report.py` | Where does cache-read cost go this week? Per-session ranking + offload ROI gate. |
| `scripts/prefix_budget.py` | How big is the re-read-every-turn prefix (CLAUDE.md + tool schemas)? |
| `scripts/cache-handoff-nudge.sh` | Stop hook (deprecated in 0.4.0; the datapce plugin replaces it; retired in 0.4.1): nudge to start a fresh session before its prefix gets expensive. |
| `scripts/codex_session_report.py` | Same per-session cache-cost view, for Codex sessions (reads `~/.codex/sessions`). |
| `scripts/memory_compact.py` | Hierarchically compact a project-memory dir (cluster + tier + freshness); advisory, `--apply` auto-creates a reversible git checkpoint (or `--no-init-git` to require an existing repo). |
| `scripts/memory-compact-nudge.sh` | Stop hook: nudge to compact a large project `MEMORY.md` (advisory; never mutates). |

```bash
python scripts/cache_report.py --days 7           # weekly cost + top sessions + offload ROI
python scripts/cache_report.py --days 7 --check   # exit 2 if the data span can't support a weekly claim
python scripts/prefix_budget.py --budget 8000     # measure the fixed prefix vs a budget
python scripts/codex_session_report.py --days 7   # Codex per-session cache-read cost
```

**Honesty guard:** `cache_report.py` reports the *actual* data span and refuses to
present a short window as a full week (`--check` exits non-zero). Cost figures use
the caching price schedule (read 0.1×, write 1.25×, fresh input 1×, output 5×).

**The lever these tools point at is `less context × fewer turns`, not cache tuning** —
a high cache-read line at a high hit-rate / low bust-rate is caching *working*. See
the runbook for the interpretation guide.

**Updating an existing install** (pull the latest tools/hooks — no restart of anything):
[`docs/RUNBOOK-update.md`](docs/RUNBOOK-update.md).

---

## Multi-model change classifier (`scripts/change_classifier.py`)

A per-change risk read from a **panel of independent models**. Given a diff + the
test/CI output + the requirements, each panel member classifies the change on three
axes — **change class**, **requirement fit**, **blast-radius damage** — using one
fixed JSON schema, and the tool reports every member's read plus their
**divergence**. Agreement is supporting evidence, not proof of safety; where the
panel splits (one says blast `low`, another `high`) is exactly where to look. It is a *measurement*, never a gate — it emits no pass/fail and takes
no action. Pairs with a develop→test→cross-validate loop; see the
`change-classification` skill in [apex-router-skills](https://github.com/runapex/apex-router-skills).

The panel is **config-driven** — a member is just `{"name", "cmd"}` where `cmd`
reads the prompt on stdin and prints one JSON object; no model id is hardcoded. Span
different model families for genuine independence.

```bash
python3 scripts/change_classifier.py \
  --repo . --base HEAD \
  --tests test_run.log \
  --reqs spec.md,acceptance.md \
  --panel panel.json \
  --out report.json
```

`scripts/change_classifier_panel.example.json` is a working four-member,
three-vendor panel; copy it to `~/.apex-router/change_classifier_panel.json`
(`chmod 600`) or point `--panel`/`CLASSIFIER_PANEL` at it. Extra keys such as
`auth` are ignored by the loader and only document credentials:

| Member | Command | Credential (names only) |
|---|---|---|
| `claude-opus-5-5` (Anthropic) | `claude -p --tools "" --setting-sources "" …` | Claude Code login (keychain OAuth) or `ANTHROPIC_API_KEY`; follows `ANTHROPIC_BASE_URL`, so it can go through the proxy. Do not use `--bare`, which ignores keychain auth. |
| `gpt-5.6-sol` (OpenAI) | `codex exec -s read-only --ephemeral -` | `codex login` (ChatGPT/Codex subscription) or `OPENAI_API_KEY` |
| `kimi-k3` (Moonshot) | inline `python3 -c` POST to `$MOONSHOT_BASE_URL/chat/completions` (default `https://api.moonshot.ai/v1`) | `MOONSHOT_API_KEY`, else pi's stored `moonshotai` key (`pi auth print-api-key`). Call Moonshot directly: the proxy's `/v1` forwards to `api.openai.com`, so Kimi through `:8788` gets a 401. |
| Ornith 35B-A3B (local) | inline `python3 -c` to ollama `/api/chat` at `$OLLAMA_HOST_URL` | none; zero-cost extra opinion, not an extra vendor |

Keep at least two hosted vendors in the panel. Same-family agreement is not
vendor independence. Each member has a 300 s timeout. A failed or missing
credential shows up as `ok:false` for that member and does not stop the others.
Use `--diff change.diff` (or `-` for
stdin) instead of `--repo` for a curated diff. `--repo` includes tracked changes
only; `--include-untracked` explicitly adds untracked files. Review inputs for
secrets before using a hosted panel. Failed commands and malformed classifications
are reported as `ok:false`, never counted as agreement. If every member fails,
the CLI exits 1 after emitting the report; risk labels themselves never affect
exit status. Clipped inputs are flagged as `INCOMPLETE INPUT` with omitted counts
in `input_clipped_chars`. Treat reports as sensitive, including diagnostic tails.

Pure-stdlib; only your configured panel commands call out, receiving the supplied
diff, requirements, and test output. With no `--panel`/`CLASSIFIER_PANEL` it prints
an example and exits.

---

## Troubleshooting

| Symptom | Likely cause & fix |
|---|---|
| `apex-router: command not found` | Not on PATH. `export PATH="$HOME/.apex-router/.venv/bin:$PATH"`. |
| `apex-router status` shows `ornith=unavailable` | ollama isn't running. Start it (`ollama serve`), then `apex-router ornith-tier` to check the tier — or ignore if you don't need local offload. |
| Scheduled codeqa/ask "asked 0 questions" | The watcher's minimal PATH lacks `rg`/`uv`. Ensure `/opt/homebrew/bin` (macOS) or the ripgrep dir is on the unit's PATH — the shipped units include it, but a custom setup may not. |
| Every local job lands in `jobs/failed/` with `finish_reason=length` empty | Thinking-ON runaway. Codegen/adhoc must run thinking-OFF (the worker forces this); if you hand-craft jobs, don't set `enable_thinking` on codegen. |
| Review jobs fail on large diffs | Diffs over ~100 KB are skipped by the hook (size guard); truncated reviews keep partial findings and still escalate — check `detail` for `(truncated)`. |
| `watch install` did nothing on Linux | Needs `systemd --user` (a user session bus). On headless boxes enable lingering: `loginctl enable-linger $USER`. |
| Routing always returns the static default | Expected until you capture a corpus and run the replay bench — the table ships empty by design. |

Logs to check: `~/.apex-router/logs/com.apex-router.{drain,daily}.{log,err}` (macOS) or
`journalctl --user -u apex-router-drain` (Linux).

---

## Uninstall

```bash
apex-router watch uninstall            # remove the launchd/systemd units first
rm -rf "$HOME/.apex-router"            # package, venv, logs, route tables, telemetry
claude plugin uninstall datapce@datapce   # the datapce plugin
claude plugin marketplace remove datapce  # and its marketplace
```

That removes everything apex-router created under its own dir. ollama and the Ornith
model (if installed) are left in place — remove them with their own tooling if you want
(`brew uninstall ollama` / delete the HF model cache).

Some opt-in features write **outside** the apex-router dir, into `~/.claude/settings.json`
(each leaves a `.apex-bak` backup): proxy client wiring (`--proxy-config` / `setup-proxy`),
the cache-handoff Stop hook (`--cache-handoff-hook`; deprecated), and the memory-compact Stop hook
(`--memory-compact-hook`). If you enabled any, remove its entry from
`~/.claude/settings.json` by hand (or restore the `.apex-bak`). Both hooks write advisory
docs under `~/.claude/handoffs/` — delete that dir to clear them. `memory_compact.py --apply`
is the only thing that moves memory files, and it does so inside git (revert with git).

---

## Security posture

**No agentic grading of untrusted code.** codeqa's frontier "judge"/verifier is opt-in and
HTTP-only: point `CODEQA_JUDGE_BASE` at an Anthropic-messages endpoint you control. It does
**not** route grading through the local `claude`/`codex` CLIs — those are agentic (tools,
hooks, MCP), and scanned source may be adversarial, so feeding it to an agentic CLI could
trigger code execution. With no endpoint configured, codeqa uses its **local verifier**. The
HTTP path strips credentials on cross-origin redirects, bounds response size, and warns on
plaintext `http://`.

**No telemetry egress.** All measurement is local JSONL; there is no uploader or phone-home.

---

## License

MIT.
