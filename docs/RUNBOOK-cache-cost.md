# Runbook: cache-cost optimization

Four offline tools that turn the proxy's local telemetry into an answer to one
question: **where is prompt-cache read cost going, and is it worth reducing?**
Nothing here transmits anything off-box; every tool reads a local file and prints.

## The one thing to understand first

A large "cache read" cost line is usually caching **working**, not a regression.
Cache reads bill at ~0.1× base input, so a big read line means a large amount of
context is being re-sent at the cheapest possible rate. Confirm before "fixing":

- **input hit-rate** high (reads dominate reads+writes+fresh) → cache is doing its job
- **read:write ratio** high (each cached prefix is read many times before rewrite) → strong reuse
- **busts** near zero → nothing is invalidating prefixes prematurely

If all three hold, the cache is healthy. The only real lever left is **less context ×
fewer turns** — shorten long sessions, trim the fixed prefix. It is NOT cache tuning,
and bolting compaction onto a zero-bust cache can *cost* more (every summary rewrite is
a cache write + a potential bust).

---

## 1. Weekly cost + per-session ranking + offload ROI — `cache_report.py`

```bash
python scripts/cache_report.py --days 7            # text report
python scripts/cache_report.py --days 7 --json     # machine-readable
python scripts/cache_report.py --days 7 --check    # exit 2 if data span < window
```

Reads `~/.apex/telemetry.jsonl` (+ `~/.apex/offload_telemetry.jsonl`). Emits:

- **Cache decomposition:** read / write / fresh / output tokens, input hit-rate,
  read:write ratio, bust count, modeled cost, and the no-cache counterfactual
  (what you'd pay with caching off).
- **`span_days_present`** — the ACTUAL span of in-window data. If it's less than the
  requested window, the report says so and weekly figures are labeled PROJECTIONS.
  `--check` exits 2 in that case, so a cron/gate can refuse to act on a short window.
- **Top sessions by cache-read**, with `$/req` — this is where you find the fat
  long-lived sessions that dominate the line.
- **Offload ROI gate** — per lane: `net = frontier_tokens_saved − escalated_completion`.
  A lane is "offload-positive" only when net > 0 **and** its rows are from the current
  local-model era (older eras are excluded so a new model isn't credited with old
  history). This answers "is routing this lane to the local model actually paying?".

**Interpretation table:**

| Field | Meaning | Good direction |
|---|---|---|
| `input_hit_rate` | reads / (reads + writes + fresh) | higher |
| `read_write_ratio` | reuse per cached prefix | higher |
| `busts` | premature prefix invalidations | lower (0 is ideal) |
| `top_sessions[].read_usd_per_req` | context re-read cost per request | lower; high = fat prefix |
| `offload_roi.by_lane[].net_tokens` | tokens the local model actually saved | > 0 to justify offload |

## 2. Prefix budget — `prefix_budget.py`

```bash
python scripts/prefix_budget.py --claude-md ~/.claude/CLAUDE.md \
    --project-md ./CLAUDE.md --tools tools.json --budget 8000
python scripts/prefix_budget.py --budget 8000 --check   # exit 2 if over budget
```

Measures the *fixed* prefix that a session re-reads every turn — the biggest
controllable input to per-turn cache-read cost. Ranks contributors so you know
what to trim (prompt-audit the CLAUDE.md, defer rarely-used tool schemas). Uses the
Anthropic SDK's `count_tokens` when available (exact), else a clearly-labeled
character estimate. Advisory — it edits nothing.

## 3. Session-handoff nudge — `cache-handoff-nudge.sh` (Stop hook)

An advisory Claude Code **Stop hook**. When a session's cumulative cache-read
crosses a threshold, it writes a handoff doc under `~/.claude/handoffs/<session>.md`
and nudges you to start fresh. It **never blocks and never resets** (Claude Code
can't be externally reset) — it emits `additionalContext` only.

- **Primary signal:** the proxy `session_id` is byte-identical to the Claude Code
  session id, so the hook joins the live session to its cumulative cache-read tokens.
- **Fallback:** if the session isn't in telemetry (no proxy in path), it counts
  assistant turns in the transcript instead.

Wire it into `~/.claude/settings.json`:

```json
{ "hooks": { "Stop": [ { "hooks": [
  { "type": "command", "command": "/ABSOLUTE/PATH/hooks/cache-handoff-nudge.sh" }
] } ] } }
```

Tunables (env, override in the hook's settings entry or your shell):

| Env var | Default | Meaning |
|---|---|---|
| `CACHE_HANDOFF_READ_THRESHOLD` | `100000000` | cache-read tokens that trigger a nudge |
| `CACHE_HANDOFF_MSG_THRESHOLD` | `200` | fallback: assistant-turn count (telemetry absent) |
| `CACHE_HANDOFF_DIR` | `~/.claude/handoffs` | where handoff docs are written |
| `APEX_TELEMETRY` | `~/.apex/telemetry.jsonl` | telemetry source |

> **Start aggressive, relax over time.** The default cap is deliberately LOW (~100M
> read tokens ≈ $50 of accumulated read cost) so it nudges early on the fat-tail
> sessions — an intentional policy choice, not a data-fit. Raise it (per repo / task
> stratum) only when signals show the nudges are premature. The intended evolution is
> a per-key adaptive threshold proposed nightly from `cache_report.py`'s measured
> per-session read distribution once ≥ 7 days of data exist — do not hard-code a high
> value off a short window. Override per repo/task today with the env var above.

## 3b. Project-memory compaction — `memory_compact.py` + `memory-compact-nudge.sh`

A Claude Code project accumulates memory files plus a `MEMORY.md` index that is
re-read into every session's prefix. `memory_compact.py` clusters the files, tiers
them (frontmatter `type`: `feedback`/`project` = keep-hot, `reference` = cold) and
freshness, and rolls cold files into a per-cluster archived line.

```bash
python scripts/memory_compact.py --dir ~/.claude/projects/<slug>/memory   # report (advisory)
python scripts/memory_compact.py --dir <memory> --write-proposed /tmp/idx.md   # proposed index, no mutation
python scripts/memory_compact.py --dir <memory> --apply    # MUTATES: archive cold files + compact index
```

**`--apply` is the only mutating path and is heavily guarded** (Codex-hardened):
refuses outside a clean git tree (so every change is `git`-reversible), never
overwrites an existing archived file, refuses a symlinked index/archive, and
**compacts the existing index in place** — it drops only the cold-file link lines
and preserves all your hand-written prose, headings, and ordering.

> **Honest expectation — the index shrink is usually SMALL.** On a real 87-file /
> 14.7 KB index, compaction reclaimed only ~0.9 KB (~6%), because a terse
> one-line-per-file index doesn't have much to squeeze and singleton clusters don't
> roll up. **The real value is decluttering** — moving stale `reference` files out
> of the working set — not shrinking the every-session prefix. Don't sell it as a
> cache-cost win; measure the actual delta (the tool prints it) before acting.

The **Stop hook** (`hooks/memory-compact-nudge.sh`, opt-in via
`install.sh --memory-compact-hook`) fires when the index ≥ ~8 KB or ≥ ~50 files,
writes a proposed index to `~/.claude/handoffs/memory-<slug>.md`, and nudges — it
**never mutates** the memory dir. Tunables: `MEMORY_COMPACT_INDEX_BYTES` (8192),
`MEMORY_COMPACT_FILE_COUNT` (50).

## 4. Codex per-session cache cost — `codex_session_report.py`

```bash
python scripts/codex_session_report.py --days 7
python scripts/codex_session_report.py --days 7 --json
```

Codex traffic can't be joined to the proxy telemetry (its rows carry a null session
id), so this reads Codex's own rollout files under `~/.codex/sessions/YYYY/MM/DD/`.
Each rollout's `token_count` records carry cumulative `total_token_usage` including
`cached_input_tokens` (cache reads), so each Codex session is priced with the **same**
schedule as `cache_report.py`. Ranks by read tokens.

> Note: `codex exec` runs are single-turn, so `$/turn` is not a length signal there —
> rank by total read tokens.

---

## One-shot checklist

```
[ ] cache_report.py --days 7 --check    → is the window long enough to trust?
[ ] read hit-rate / r:w / busts          → is the cache healthy (yes = don't "fix" it)?
[ ] top_sessions                         → which few sessions dominate the read line?
[ ] prefix_budget.py                     → is the fixed prefix bloated on those repos?
[ ] offload_roi.by_lane                  → is any lane actually net-positive to offload?
[ ] codex_session_report.py              → same questions for Codex sessions
```
