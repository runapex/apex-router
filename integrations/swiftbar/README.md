# Menu bar widget (SwiftBar)

A dot in the macOS menu bar: upstream pressure colour plus the number of active agents. Click it
for the detail — pressure and its causes, errors in the last 15 min, the 5h/7d limit meter, every
Claude Code / pi / Codex session seen in the last hour with its memory, cpu, disk io, subagents,
child processes and models, the local worker, system GPU/load, the proxy.

The plugin only runs `apex-router snapshot --menubar`. It is read-only: it writes no file and
starts no service. Each refresh runs `ps` once, `ioreg` once and — only when a pi or Codex session
is listed — `lsof` once (2 s timeout each), and makes two loopback calls: the proxy's `/healthz`
and ollama's `/api/ps`.

## Install

```sh
brew install --cask swiftbar          # pick a plugin folder on first launch
ln -s "$PWD/integrations/swiftbar/apex.1m.sh" "<your SwiftBar plugin folder>/apex.1m.sh"
```

`.1m.` in the file name is the refresh interval (one minute). The script looks for
`~/.local/bin/apex-router`, then `apex-router` on `PATH`; if neither runs it shows a gray dot.

## The dot

| colour | meaning |
|---|---|
| green | pressure GREEN on a sufficient sample (≥ 10 requests in 15 min) |
| orange | AMBER — shed one tier, cap heavy parallelism |
| red | RED — no new heavy fan-out |
| gray | not enough requests to tell, UNKNOWN, or the snapshot failed |

A model family that is RED or AMBER on its own rate colours the dot even when the overall level is calm.

The number is the count of agents whose session log changed in the last 5 minutes.

## Per-agent resources and the call graph

Each agent row carries its compact load, e.g. `claude · apex-router · active · 2m · d5443384 · busy ·
412MB · 14% · io 745/569MB`:

- **memory**: physical footprint (what Activity Monitor calls Memory) of the session process and
  every process below it; resident size when the footprint is not readable.
- **cpu**: the kernel's decayed `%cpu`, summed over the same process tree.
- **io**: disk megabytes read / written since each process started (lifetime totals, not a rate).
- **busy/idle**: Claude Code's own status, shown when it differs from the log-mtime state.

An active agent opens a submenu: proxy traffic of its main thread in the last 60 min (requests,
output tokens, errors, median time to first token), its subagents (type · description · traffic ·
active/idle), its three busiest child processes (these are the tool calls: shells, test runs, MCP
servers) and the models it called.

How processes are matched: a Claude Code session through `~/.claude/sessions/<pid>.json`, checked
against the live process's start time; a pi or Codex session through the process's working
directory, and only when exactly one such process and one such session share it — otherwise the
row says the process is not attributed.

What cannot be attributed:

- **GPU per agent.** macOS reports GPU use per process only to root. The System section shows the
  GPU utilisation and memory in use for the whole machine, and the VRAM of each model ollama has
  loaded (shown under the local worker, which is what calls ollama).
- **Memory/cpu per subagent.** Subagents run inside their session's process. Their load is their
  proxy traffic; the process figures belong to the session.
- **Traffic that skips the proxy.** Request counts only include calls routed through apex-router.

`apex-router snapshot --graph` prints the same relations as a tree:

```
claude · apex-router · d5443384 · active · busy · pid 2894 · 702MB · 2.1% · io 956/571MB
  spawned general-purpose · <description> · 19 req · 23.8k out · 0 err · active · depth 1
    calls claude-opus-5-5 ×19
  runs node (pid 18204) · 256MB · 0%
  calls claude-opus-5-5 ×8
```

`--json` has it under `graph` (`nodes` of kind session / subagent / process / model, `edges` of
kind spawned / runs / calls), capped at 30 sessions, 20 subagents and 5 processes per session.
Subagents sit directly under their session; `depth` is recorded but a depth-2 subagent's parent is
not known without reading transcripts, which the widget does not do.

## Adapters: your own menu sections

Any program can add a section by writing a JSON file into an adapters directory:

```json
{"title": "My metrics", "rows": ["queue 3", "last run ok"], "ts": 1791338846}
```

- `title`: string, the section header; `rows`: list of strings; `ts`: epoch seconds (or ms) of
  the data. The section shows its age and is marked stale after 24 h.
- Directories: `$DATAPCE_HOME/adapters/` (default `~/.datapce/adapters/`) and
  `~/.apex-router/adapters/`. A file name present in both is read from the datapce home.
  `APEX_ADAPTERS_DIR` replaces both.
- At most 5 files, 10 rows each, 120 characters per row; files that do not parse or lack
  `title`/`rows` are skipped. Write atomically (temp file + rename) so a half-written file is
  never read.

`apex-router snapshot --json` prints the same data as JSON.
