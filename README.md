# apex-router

Adaptive model routing — measured, per-task-class model selection — plus a measuring
**proxy**, a local **offload** subsystem (review / codegen / code-Q&A on a local model),
and the freshness toolkit the routing evidence is built from. Adopt any layer alone; the
routing core stays **pure-stdlib** and every heavy piece is an optional extra.

Instead of a hand-authored "use model X for task Y" table, `apex-router` learns which
model is actually best for each kind of task from evidence, behind a statistically
sound promotion gate, and routes to it — falling back to your hand-authored defaults
whenever the evidence is thin or uncertain. It is a **strict superset** of static
routing: it never routes to a model your machine can't run, and defaults to your static
choice on any uncertainty.

---

## Table of contents

- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Install](#install)
- [The measuring proxy](#the-measuring-proxy-optional-proxy-extra)
- [The offload subsystem](#the-offload-subsystem)
- [Background watchers](#background-watchers)
- [Proxy client setup](#proxy-client-setup)
- [Team skills (private marketplace)](#team-skills-private-marketplace)
- [Telemetry — reading and sharing it](#telemetry--reading-and-sharing-it)
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
  - **review** — a recall pre-filter (measured ~1/5 precision), so it **always escalates**
    for frontier triage and its tokens never count as "saved"; keeps partial findings if
    the local answer is truncated.
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
   box with only the Claude and Codex CLIs and no model server.

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
| measuring proxy | **only with `--proxy`** | the `[proxy]`/`[tuner]` extras; installed, not auto-started |

Flags: `--no-ornith` (skip the local model pulls), `--ornith-tier small|large|both` (which tier to
pull and activate; default `small`), `--ornith-serve` (macOS: install the Ornith queue worker +
nightly cycle as launchd agents), `--no-embed`, `--watch` (install watchers at first run), `--proxy` (install
the measuring proxy + its extra), `--proxy-config <file>` (wire Claude Code through a proxy),
`--skills-marketplace <git-url>` (print the wiring for a private team skill marketplace — see
below), `--dir PATH`, `--verify-only`.

### Local model tiers (Ornith 1.5)

Local inference runs on **ollama** (`:11434`) — the same instance that serves `nomic-embed-text`.
The old MLX server (`mlx_lm.server` on `:8080`, `com.ornith.server`) is **retired**: it pinned one
model at process start, which made switching sizes a restart-and-reload, and it confined local
inference to Apple Silicon.

Two tiers, one resident at a time:

| Tier | Model | Weights | Shape |
|---|---|---|---|
| `small` | `hf.co/ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M` | ~5.6 GB | dense 9B — bulk/triage, coexists with everything else |
| `large` | `hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M` | ~21 GB | 35B-A3B MoE, 3B active/token — fidelity, synthesis, codegen |

> There is **no 27B**. Upstream Ornith 1.5 ships 9B, 35B-A3B and 397B; 397B does not fit a single
> workstation. The 35B-A3B is the practical "big" tier — being MoE, it decodes near a 3B model.

```bash
apex-router ornith-tier            # what's configured, pulled, and actually resident
apex-router ornith-tier large      # unload the old tier, write the new one, warm it, restart consumers
apex-router ornith-tier --unload   # free the RAM without changing the configured tier
apex-router ornith-tier --json     # machine-readable
```

The active tier lives in `~/.apex-router/ornith.env` and is the single source of truth: the launchd
units carry **no** model id, so switching never means editing a plist. Switching is never implicit —
`model_router.select()` reports `needs_switch` and names the model it wants, but will not trigger a
multi-GB load as a side effect of asking for a route.

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

# review pre-filter — always escalates for triage, tokens never counted as saved
python -m apex_router.ornith.queue_task --lane review --diff-file change.diff

# adhoc chat
python -m apex_router.ornith.queue_task --task "summarize this" --context notes.md

# read the economics anytime
python -m apex_router.ornith.offload_report
```

The worker picks up anything in `jobs/inbox/` within 5s, serialized (single GPU).

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
`>>frontier design the migration`) or with a sticky `/apex-route` command. Frontier and
Kimi turns flow through the measuring proxy; local turns go straight to the Ornith tiers on
ollama. Two drop-in pieces live under [`integrations/pi/`](integrations/pi/):

```bash
cp integrations/pi/models.json ~/.pi/agent/models.json     # route anthropic+moonshotai via the proxy
pi install integrations/pi/apex-route.ts                    # the per-task router extension
```

Full instructions, the family table, and testing steps:
[`docs/RUNBOOK-pi-integration.md`](docs/RUNBOOK-pi-integration.md).

## Team skills (private marketplace)

apex-router ships **no skills** and hardcodes no private URL. Internal skills (team
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

## Cache-cost optimization toolkit (`scripts/`)

Four offline, measure-first tools for understanding and reducing prompt-cache
read cost. They read the telemetry the proxy already writes (and Codex's own
rollout files) — no proxy restart, no model call, nothing transmitted. Full
guide: [`docs/RUNBOOK-cache-cost.md`](docs/RUNBOOK-cache-cost.md).

| Tool | What it answers |
|---|---|
| `scripts/cache_report.py` | Where does cache-read cost go this week? Per-session ranking + offload ROI gate. |
| `scripts/prefix_budget.py` | How big is the re-read-every-turn prefix (CLAUDE.md + tool schemas)? |
| `scripts/cache-handoff-nudge.sh` | Stop hook: nudge to start a fresh session before its prefix gets expensive. |
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
```

That removes everything apex-router created under its own dir. ollama and the Ornith
model (if installed) are left in place — remove them with their own tooling if you want
(`brew uninstall ollama` / delete the HF model cache).

Some opt-in features write **outside** the apex-router dir, into `~/.claude/settings.json`
(each leaves a `.apex-bak` backup): proxy client wiring (`--proxy-config` / `setup-proxy`),
the cache-handoff Stop hook (`--cache-handoff-hook`), and the memory-compact Stop hook
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
