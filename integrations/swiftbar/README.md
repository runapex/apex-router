# Menu bar widget (SwiftBar)

A dot in the macOS menu bar: upstream pressure colour plus the number of active agents. Click it
for the detail — pressure and its causes, errors in the last 15 min, the 5h/7d limit meter, every
Claude Code / pi / Codex session seen in the last hour with its memory, cpu, disk io, tokens,
context size, subagents, child processes and models, the local worker, system GPU/load and
ollama, the proxy.

The plugin only runs `apex-router snapshot --menubar`. It starts no service and writes one file:
each refresh appends one small sample to `~/.apex-router/widget/history.jsonl` (see
[History](#history)); clicking a row writes and opens a detail page (see
[Clicking: detail pages](#clicking-detail-pages)). Each refresh runs `ps` once (plus one `ps -o pid=,args=` for at most 40
node/python/… processes inside agent trees), `ioreg` once, `launchctl list` once and — only when a
pi or Codex session is listed — `lsof` once, and makes two loopback calls: the proxy's `/healthz`
and ollama's `/api/ps`. All of these, the 0.25 s cpu/io sampling window and the subagent log scan
share one 0.45 s deadline; a source that would start after it is skipped and shown under System as
unavailable, so even a hung source keeps a refresh near 0.5 s. A refresh takes about 0.35 s.
Subagent logs are read only for the active sessions the menu shows (at most 8); 300 sessions with
40 subagents each refresh in about 0.15 s plus the sampling window. Every visible line is at most
110 characters (`--graph`: 120), and control characters (ESC, DEL, C1) never reach a line.

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

The number is the count of active agents: a Claude Code session whose own status
(`~/.claude/sessions/<pid>.json`) is `busy`; for a session without a status (pi, Codex) one whose
log changed in the last 5 minutes. A `⚠` after it (`● 2 ⚠`) means an agent needs a look: a
subagent still writing after more than 20 min, an error in the last 5 min or a 60-min error rate
of 5 % or more (main thread or subagent), or a context at 85 % of a known window. One old
transient error does not raise it. The `⚠` never changes the dot colour, which stays the
pressure rule above.

## Per-agent resources and the call graph

The Agents header sums everything: `Agents · 2 active · 2 idle · 1.2GB · cpu 18% · 74 req/h ·
out 120k/h · cache 98%` (memory and cpu of every agent process tree; requests, output tokens and
cache share of all proxy traffic in the last 60 min). Each active agent is one row in fixed-width
columns:

```
apex-router d5443384   busy    700MB  0.2%  286 req · out 297.8k · ctx 293k/1M 29% · r5 4.4/min · last 12s ⚠
```

- **label**: repo and the 8-char session id; a long repo name is shortened (`apex-r…/rsi`), the
  id never.
- **busy/idle**: Claude Code's own status from `~/.claude/sessions/<pid>.json` (pi / Codex show
  the log-mtime state). It also decides which rows are active and which fold under `idle`.
- **memory**: physical footprint (what Activity Monitor calls Memory) of the session process and
  every process below it. Resident size is in the tooltip only.
- **cpu**: measured now — the cpu time and disk bytes of the busiest 40 processes in the agent
  trees are read twice, about 250 ms apart. Disk MB/s now, lifetime disk io, cpu time and uptime
  are in the tooltip.
- **req · out**: requests and output tokens through the proxy in the last 60 min, main thread and
  all subagents together.
- **ctx**: the prompt size of the latest main-thread request (uncached input + cache read + cache
  write) against its window: Opus / Sonnet 4.6 and later and Fable have 1M, Haiku 4.5 has 200k,
  a `[1m]` suffix means 1M (checked against the pi 0.99.1 model catalog and a live 283k-token
  request). Any other model shows the size alone (`ctx 136k`) unless a successful request of the
  same thread and model went past 200k, which proves 1M.
- **r5**: requests per minute over the last 5 min, main thread and subagents — a runaway loop
  shows here first. **last**: time since the newest request.

The row's submenu:

```
Σ 60m  286 req · in 75.7k · cached 34.7M · write 1.1M · out 297.8k · cache 97% · 3 err 1%
main  28 req · out 22.5k · cache 95% · ctx 293k/1M 29% · ttft 2.8s
Subagents 60m · 5 · 1 running · 1 ⚠
▶ RSI iter 3: fix round…  36 req · out 57.8k · cache 96% · ctx 184k/1M 18% · last 2s
⚠ RSI eval round 2        55 req · out 29.5k · cache 96% · 3 err 5% · ctx 161k/1M 16% · last 9m
Processes · 3 · 308MB
  langserver.index    292MB  0%
Models 60m
  claude-opus-5-5   161 req
```

- `Σ` sums the main thread and every subagent before anything is cut, so the totals never shrink
  when rows are hidden.
- The main and subagent rows keep input / cached / cache-write tokens and run time in their
  tooltip; `Σ` shows the full split.
- Subagents: `▶` running (wrote in the last 60 s), `◦` quiet (< 5 min), `✓` done, `⚠` flagged
  (running > 20 min, an error in the last 5 min or an error rate ≥ 5 % — then with count and
  share — or context ≥ 85 % of a known window). `last` is the newer of its log write and its
  newest request. Run time is the subagent log's last write minus its spawn (the `meta.json`
  mtime). Ordered running, then erroring, then by output tokens; 8 are shown, the rest are one
  `… N more (x req, out y)` line. Error rates below 1 % read `<1%`.
- `Processes · N · MB` counts and sums the child processes listed under it; the whole tree
  including the session process is in its tooltip.
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
- **Context percent for a model outside the table.** No window is guessed. The plugin's `measure`
  events carry `ctx_pct` but no session id, so they are not attributed to a row.
- **Subagent logs of sessions not shown.** Idle sessions and active ones past the first 8 list
  only the subagents seen in proxy traffic (their totals are unaffected).

`apex-router snapshot --graph` prints the same relations as a tree:

```
⚠ claude · apex-router · d5443384 · busy · pid 2894 · 700MB · 0.4% · 0.0MB/s · 286 req/h · out 297.8k/h · cache 97%
    ctx 293k/1M 29% · r5 4.4/min · last 13s
  spawned general-purpose · RSI iter 3: fix round-2 findings · 36 req · in 24.6k · cached 4.7M · write 177.1k
      out 57.8k · cache 96% · ctx 184k/1M 18% · running · run 9m · last 2s · depth 1
    calls claude-opus-5-5 ×36
  runs pyright-langserver (pid 18188) · 15MB · 0%
  calls claude-opus-5-5 ×19
```

Lines longer than 120 characters continue on the next line, indented four more.
`--json` has it under `graph` (`nodes` of kind session / subagent / more / process / model,
`edges` of kind spawned / runs / calls), capped at 30 sessions, 8 subagents (plus one `more` node
with the rest's summed traffic and model calls) and 5 processes per session. Subagents sit
directly under their session; `depth` is recorded but a depth-2 subagent's parent is not known
without reading transcripts, which the widget does not do.

## Graphs in the menu

Each active session's submenu starts with `Open details ↗` and a block of sparklines (Menlo,
`▁▂▃▄▅▆▇█`):

```
cpu  ▁▂▃▅▇▆▅▃▂▁▁▂                    0.5%
mem  ▅▅▆▆▇▇▇▇▇▇▇█                    705MB
io   ▁▁▁▃▁▁▁▁▁▁▁▁                    0.1MB/s
ctx  ▂▃▄▅▆▇▇▇▇███                    31% 293k
req  ▁▁▃▇▂▁▁▁▂▃▅▇                    22 per 5 min · 60 min
tok  ▁▁▂▅▂▁▁▁▂▂▄▆                    out 9.8k per 5 min
```

- cpu / mem / io / ctx come from the history file: the last 30 refreshes (about 30 min at one
  refresh a minute). cpu, io: scale 0..max; memory and context: min..max (the tooltip says
  which). A missing sample is a gap; a series with no sample yet is left out.
- req / tok come straight from proxy telemetry: twelve 5-min buckets over the last 60 min (main
  thread and subagents together), so they work without any history.
- The System section adds `gpu ▁▁▃▂ 6%` (system-wide GPU from the history).
- When the menu is short of room a session falls back to cpu and req only, then to none.

## Clicking: detail pages

Every session row, every subagent row, every idle row and the top `Open dashboard ↗` item run

```
<apex-router binary> snapshot --detail <id>      (terminal=false refresh=false)
```

`<id>` is a session id, a subagent id or `all`. A row that has a submenu (an active session) opens
the submenu on hover as usual; macOS does not fire an action for such a row, so use its first item,
`Open details ↗` (the sparkline lines also click through).

Safety: the click runs only the apex-router binary that the plugin found (`APEX_ROUTER_BIN`, exported
by `apex.1m.sh`; else `~/.local/bin/apex-router`, else `apex-router` on PATH), and only when it is
a plain absolute path. The three arguments are fixed except the id, which must match
`^[0-9a-f-]{8,36}$` (session) or `^a[0-9a-f]{8,32}$` (subagent); anything else gets no action.
No description, repo, process or model name ever reaches a command parameter.

`apex-router snapshot --detail <id> [--no-open]` writes one self-contained HTML page and opens it
with `open`:

- `~/.apex-router/widget/detail-<id8>.html` for a session (a subagent click opens its session's
  page with that subagent first, anchored at `#a…`), `detail-all.html` for the dashboard.
- Session page: header (repo, session id, status, pid, uptime, models, memory / cpu / disk now,
  context X/W %, cache share); cpu %, memory, disk MB/s and context tokens over the history;
  requests, tokens stacked (input / cached read / cache write / output) and output tokens per
  5 min over the last 6 h with errors marked by cause; context size per request for the main
  thread and the busiest subagents against the window (1M / 200k); a subagent timeline (spawn →
  last write, coloured running / quiet / done / flagged); a call graph (session → threads /
  subagents → models with edge width ~ requests, session → child processes with MB and %cpu);
  subagent, process and model tables.
- Dashboard: every session with small multiples (cpu, memory, requests), system GPU %, GPU memory
  and load history, all proxy requests, ollama models.
- Charts are inline SVG; hovering a point or bar shows its value (SVG tooltips, no JavaScript).
  The page loads nothing from anywhere — no scripts, stylesheets, fonts or images, no network —
  and a Content-Security-Policy says so. Light and dark follow the system setting. No dollar
  amounts.

## History

`snapshot --menubar` appends one compact JSON line per run to `~/.apex-router/widget/history.jsonl`
(`$APEX_ROUTER_HOME` moves it): per session its id, kind, status, memory footprint, sampled cpu %,
disk MB/s, context tokens and 5-min request count, its listed subagents' ids and states, and the
system GPU %, GPU memory and 1-min load. No descriptions, names, paths or prompt text.

- Bounded: at most 24 h and 2 MB; when either is exceeded the file is rewritten (temp file +
  rename) to the newest lines within 24 h and 1.5 MB.
- Concurrent refreshes take a lock (`history.jsonl.lock`); a run that cannot get it within 0.1 s
  skips its sample. A failure to write never affects the menu.
- `--json` and `--graph` never write. `apex-router snapshot --menubar --no-history` (or
  `APEX_WIDGET_NO_HISTORY=1`) turns the history off; the sparklines from history then disappear.

## Privacy

Everything stays on this machine: the history file and the detail pages live in
`~/.apex-router/widget/` (created private: directory 0700, files 0600) and are never sent
anywhere. The detail page shows subagent descriptions and process names (HTML-escaped)
the same way the menu does; delete the directory to remove them.

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
