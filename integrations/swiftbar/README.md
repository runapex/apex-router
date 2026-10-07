# Menu bar widget (SwiftBar)

A dot in the macOS menu bar: upstream pressure colour plus the number of active agents. Click it
for the detail — pressure and its causes, errors in the last 15 min, the 5h/7d limit meter, every
Claude Code / pi / Codex session seen in the last hour, the local worker, the proxy.

The plugin only runs `apex-router snapshot --menubar`. It is read-only: it writes no file, starts
no service and makes no network call beyond the loopback proxy's `/healthz`.

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

The number is the count of agents whose session log changed in the last 5 minutes.

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
