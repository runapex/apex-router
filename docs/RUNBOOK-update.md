# Runbook: update apex-router on a teammate's machine

Pull the latest apex-router (new tools, hooks, fixes) onto a machine that already
has it. **No restart of anything is required** — see "Why nothing restarts" at the
end. One command for most people, plus opt-in flags for the new Stop hooks.

## The one command (packaged install — the common case)

If apex-router was installed with `install.sh`, `~/.apex-router` **is** the git
clone, so re-running the installer pulls the latest and re-applies everything
idempotently (anything already present is skipped):

```bash
curl -fsSL https://raw.githubusercontent.com/runapex/apex-router/main/install.sh | bash
```

Or, equivalently, just pull the clone in place:

```bash
git -C ~/.apex-router pull --ff-only
```

Both bring the new `scripts/` (cache_report, prefix_budget, codex_session_report,
memory_compact) and `hooks/` files. Nothing else is needed to *use* the scripts.

## Turn on the new advisory hooks (opt-in)

Two new **Stop hooks** ship but are **off by default** — they only wire in if you
ask. Both are advisory: they nudge, never block, never mutate.

```bash
# re-run the installer with the hook flags (idempotent; safe to add anytime)
curl -fsSL https://raw.githubusercontent.com/runapex/apex-router/main/install.sh \
  | bash -s -- --cache-handoff-hook --memory-compact-hook
```

- `--cache-handoff-hook` — nudges to start a fresh session when the current one's
  cache-read prefix gets expensive.
- `--memory-compact-hook` — nudges to compact a large project `MEMORY.md`.

Each merges one entry into `~/.claude/settings.json` (a `.apex-bak` backup is
written) and is picked up on the **next** Stop event — **no Claude Code restart**.
Full behavior + tunables: [`RUNBOOK-cache-cost.md`](RUNBOOK-cache-cost.md).

## Verify the update took

```bash
# scripts present and importable
for s in cache_report prefix_budget codex_session_report memory_compact; do
  ~/.apex-router/.venv/bin/python ~/.apex-router/scripts/$s.py --help >/dev/null 2>&1 \
    && echo "  ok: $s" || echo "  MISSING: $s"
done

# hooks wired (if you enabled them)
python3 -c "import json,os; s=json.load(open(os.path.expanduser('~/.claude/settings.json'))); \
print('Stop hooks:', [x['command'].split('/')[-1] for g in s.get('hooks',{}).get('Stop',[]) for x in g.get('hooks',[])])"

# first real report (measure-only; reads local telemetry, ships nothing)
~/.apex-router/.venv/bin/python ~/.apex-router/scripts/cache_report.py --days 7
```

## Source-checkout / dev machines (the exception)

If someone runs apex-router from a **source checkout** (e.g. `~/dev/apex-router`)
rather than the packaged `~/.apex-router` clone, `~/.apex-router/scripts/` may be
empty. Two adjustments:

```bash
# update the source instead
git -C ~/dev/apex-router pull --ff-only

# point the tools/hooks at the source engine (the hooks default to the packaged path)
export MEMORY_COMPACT_ENGINE="$HOME/dev/apex-router/scripts/memory_compact.py"
export MEMORY_COMPACT_PYTHON="$HOME/dev/apex-router/.venv/bin/python"
```

The memory-compact hook **degrades gracefully** if the engine path is missing — it
still nudges with raw index sizes, just without writing a proposed index. So a dev
box with the default path won't error; it's just less useful until the env var
points at the real engine.

## Skills update (separate repo)

The workflow-discipline skills live in a **different** repo. To get the latest
(including the new `public-repo-hygiene` skill):

```
/plugin marketplace update runapex/apex-router-skills
```

(or `/plugin marketplace add runapex/apex-router-skills` if not yet added, then
`/plugin install apex-workflow@apex-router-skills`).

## Why nothing restarts

| Component | Needs restart? | Why |
|---|---|---|
| The measuring proxy (`apex-router serve`) | **No** | The new scripts live in `scripts/`, are **not** imported by the proxy package (`src/`), and don't touch its code path. A running proxy is unaffected. |
| Stop hooks | **No** | Each is a shell script spawned fresh per Stop event — no daemon, no persistent state beyond a per-session stamp file. A newly-wired hook runs on the next Stop. |
| The report scripts | **No** | Offline readers — each run re-reads the current telemetry/memory files. No cached state. |
| Background watchers (drain/daily) | **No** | Untouched by the new tools. |

The update is pull-and-go: new files on disk, new hooks active on the next event,
running services undisturbed.

## One-shot checklist

```
[ ] git -C ~/.apex-router pull --ff-only   (or re-run install.sh)
[ ] (optional) add --cache-handoff-hook / --memory-compact-hook
[ ] verify: the 4 scripts --help cleanly; Stop hooks list shows what you enabled
[ ] cache_report.py --days 7 renders (reads local telemetry, ships nothing)
[ ] dev box only: export MEMORY_COMPACT_ENGINE / _PYTHON to the source paths
[ ] skills: /plugin marketplace update runapex/apex-router-skills
```
