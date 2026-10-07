# Menu bar widget (SwiftBar)

A dot in the macOS menu bar: upstream pressure colour plus the number of active agents. Click it
for the detail — pressure and its causes, errors in the last 15 min, the 5h/7d limit meter, every
Claude Code / pi / Codex session seen in the last hour with its memory, cpu, disk io, tokens,
context size, subagents, child processes and models, the local worker, system GPU/load and
ollama, the proxy.

The plugin only runs `apex-router snapshot --menubar`. It is read-only: it writes no file and
starts no service. Each refresh runs `ps` once (plus one `ps -o pid=,args=` for at most 40
node/python/… processes inside agent trees), `ioreg` once, `launchctl list` once and — only when a
pi or Codex session is listed — `lsof` once, and makes two loopback calls: the proxy's `/healthz`
and ollama's `/api/ps`. All of these share one 1.5 s deadline; a source that would start after it
is skipped and shown under System as unavailable. A refresh takes about 0.35 s, 0.25 s of which is
the cpu/io sampling window.

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

The number is the count of agents whose session log changed in the last 5 minutes. A `⚠` after
it (`● 2 ⚠`) means an agent needs a look: a subagent still writing after more than 20 min, any
errors in the last 60 min (main thread or subagent), or a context window known to be 85 % full.
The `⚠` never changes the dot colour, which stays the pressure rule above.

## Per-agent resources and the call graph

The Agents header sums everything: `Agents · 2 active · 2 idle · 1.2GB · cpu 18% · 74 req/h ·
out 120k/h · cache 98%` (memory and cpu of every agent process tree; requests, output tokens and
cache share of all proxy traffic in the last 60 min). Each active agent is one row in fixed-width
columns:

```
apex-router d5443384    busy     699MB   1.2%   0.0MB/s  161 req · out 185.7k · ctx 274.6k
```

- **busy/idle**: Claude Code's own status from `~/.claude/sessions/<pid>.json` (pi / Codex show
  the log-mtime state).
- **memory**: physical footprint (what Activity Monitor calls Memory) of the session process and
  every process below it. Resident size is in the tooltip only.
- **cpu** and **MB/s**: measured now — the cpu time and disk bytes of the busiest 40 processes in
  the agent trees are read twice, about 250 ms apart. Lifetime disk io, cpu time and uptime are in
  the tooltip.
- **req · out**: requests and output tokens through the proxy in the last 60 min, main thread and
  all subagents together.
- **ctx**: the prompt size of the latest main-thread request (uncached input + cache read + cache
  write). A percent is added only when the request names its window (a `[1m]` model suffix);
  otherwise no window is assumed.

The row's submenu:

```
Σ 60m  161 req · in 35.5k · cached 18.7M · write 722.7k · out 185.7k · cache 96%
main  19 req · in 36 · cached 3.9M · write 274.6k · out 16.4k · cache 93% · ctx 274.6k · ttft 3.8s
Subagents 60m · 3 · 1 running
▶ RSI iter 2: polish pe…  55 req · … · ctx 194.7k  run 13m · last 0s
✓ RSI eval round 1        39 req · … · ctx 90.1k   run 4m · done 14m
Processes · 5 · 699MB
  pyright-langserver   15MB  0%
Models 60m
  claude-opus-5-5   161 req
```

- `Σ` sums the main thread and every subagent before anything is cut, so the totals never shrink
  when rows are hidden.
- Subagents: `▶` running (wrote in the last 60 s), `◦` quiet (< 5 min), `✓` done, `⚠` flagged
  (running > 20 min, errors in 60 min with count and share, or context ≥ 85 % of a known window).
  Run time is the subagent log's last write minus its spawn (the `meta.json` mtime). Ordered
  running, then erroring, then by output tokens; 8 are shown, the rest are one
  `… N more (x req, out y)` line.
- Processes are named by the basename of the script a node/python/ruby process runs
  (`pyright-langserver`); nothing else from the command line is read into the menu.
- Caps: 8 active sessions (ranked by output tokens, then cpu; the rest become one line with their
  totals), 8 subagents per session, 8 idle sessions; the menu stays near 80 lines, giving the
  top-ranked sessions the most detail. Idle sessions fold under `idle (N) · 548MB held`.
- When the widget runs inside a session's terminal it leaves out its own process, its children
  and the shell that started it.
- The widget shows no dollar amounts.

How processes are matched: a Claude Code session through `~/.claude/sessions/<pid>.json`, checked
against the live process's start time; a pi or Codex session through the process's working
directory, and only when exactly one such process and one such session share it — otherwise the
row says the process is not attributed.

What cannot be attributed:

- **GPU per agent.** macOS reports GPU use per process only to root. The System section shows the
  GPU utilisation and memory in use for the whole machine, and each model ollama has loaded with
  its VRAM and when it unloads.
- **Memory/cpu per subagent.** Subagents run inside their session's process. Their load is their
  proxy traffic; the process figures belong to the session.
- **Traffic that skips the proxy.** Request and token counts only include calls routed through
  apex-router.
- **Context percent without a named window.** The plugin's `measure` events carry `ctx_pct` but
  no session id, so they are not attributed to a row.

`apex-router snapshot --graph` prints the same relations as a tree:

```
claude · apex-router · d5443384 · busy · pid 2894 · 698MB · 0.3% · 0.0MB/s · 162 req/h · out 186.3k/h · cache 96% · ctx 274.6k
  spawned general-purpose · <description> · 56 req · in 19.5k · cached 7.7M · write 230.6k · out 88.7k · cache 97% · ctx 196.9k · running · run 13m · last 0s · depth 1
    calls claude-opus-5-5 ×56
  runs pyright-langserver (pid 18188) · 15MB · 0%
  calls claude-opus-5-5 ×19
```

`--json` has it under `graph` (`nodes` of kind session / subagent / more / process / model,
`edges` of kind spawned / runs / calls), capped at 30 sessions, 8 subagents (plus one `more` node
with the rest's summed traffic and model calls) and 5 processes per session. Subagents sit
directly under their session; `depth` is recorded but a depth-2 subagent's parent is not known
without reading transcripts, which the widget does not do.

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
