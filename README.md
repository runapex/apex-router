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
│  codeqa: grounded code-Q&A + freshness gate   ornith: local MLX client        │
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
| Ornith MLX server | **Apple Silicon only** | local bench / codegen / review; skipped elsewhere with a notice |
| starter route table | always | empty → resolves to your static defaults until a bench fills it |
| background watchers | **only with `--watch`** | drain worker + daily report (see below) |
| measuring proxy | **only with `--proxy`** | the `[proxy]`/`[tuner]` extras; installed, not auto-started |

Flags: `--no-ornith` (skip the large model download), `--no-embed`, `--watch` (install
watchers at first run), `--proxy` (install the measuring proxy + its extra),
`--proxy-config <file>` (wire Claude Code through a proxy), `--skills-marketplace <git-url>`
(print the wiring for a private team skill marketplace — see below), `--dir PATH`,
`--verify-only`.

No model gateway required — the target uses its own Claude + Codex subscriptions.

### Verify

```bash
apex-router status     # which tiers are live (routing / embedding / ornith)
apex-router verify     # exits 0 if routing works
```

### Platform support

- **Routing core + codeqa + offload client:** macOS and Linux.
- **Local model server (Ornith MLX):** Apple Silicon only. On Linux/Intel the routing,
  embedding, and codeqa-retrieval tiers still install; local bench/codegen/review are skipped.
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

## Team skills (private marketplace)

apex-router ships **no skills** and hardcodes no private URL. Internal skills (internal
ops, team workflows) belong in a **private** Claude Code plugin marketplace — a separate
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

## Troubleshooting

| Symptom | Likely cause & fix |
|---|---|
| `apex-router: command not found` | Not on PATH. `export PATH="$HOME/.apex-router/.venv/bin:$PATH"`. |
| `apex-router status` shows `ornith=unavailable` | Local model server isn't running (or non-Apple-Silicon). Start it (`serve-ornith.sh`) or ignore if you don't need local offload. |
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

That removes everything apex-router created. ollama and the Ornith model (if installed)
are left in place — remove them with their own tooling if you want (`brew uninstall ollama`
/ delete the HF model cache). apex-router never modified system files outside its own dir
and the user-level watcher units.

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
