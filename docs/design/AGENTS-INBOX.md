# Agent inbox in the terminal (owner decisions D24, D25, 2026-10-30)

Status: contract. Owner order: everything in the terminal; the HTML studio and the SwiftBar menu bar retire
after this works.

## 1. Goal

One list of all sub-agents, as in Cursor. Each row shows running, needs you or done. The blockers of a done
agent stay visible until the owner resolves them.

## 2. Sources

| Kind | Source | State | Blockers |
|---|---|---|---|
| `pi-job` | `~/.apex-router/pi-jobs/<id>/meta.json` + `result.md` | `meta.state` | The `## OPEN` footer of `result.md`. Else the OPEN, BLOCKED, WARNING or CAUTION section. Else none. |
| `claude-sub` | `~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl` | running when the file changed in the last 120 s; else done | The OPEN section of the last assistant text, if any |
| `claude-bg` | `~/.claude/jobs/<id>/state.json` | `state` (`running`, `blocked`, `done`, other) | `needs` (one item per numbered part), `detail` |

## 3. Result footer (all new briefs)

```
## OPEN
- [P1] <one line>
- [P2] <one line>
VERDICT: GO | NO-GO | BLOCKED | DONE
```

`## OPEN` with no items, or `- none`, means no blockers. The parser reads only the last 8 KiB of
`result.md`.

## 4. `apex-router agents --json`

```json
{"now": 1793300000.0, "complete": true,
 "counts": {"running": 2, "needs": 3, "done": 41},
 "items": [
  {"id": "20261030224430-o41s", "kind": "pi-job|claude-sub|claude-bg", "role": "xval|worker|reviewer|scout|claude|other",
   "group": "needs|running|done", "stale": false, "state": "running|done|failed|capped|blocked|cancelled|timed_out",
   "verdict": "GO|NO-GO|BLOCKED|DONE|null", "label": "<masked task label, max 60>",
   "cwd_short": "apex-router", "branch": "apexjob/mvtuzw5r|null", "ahead": 3,
   "start": 1793299000.0, "end": 1793299500.0, "cost": 1.23,
   "open": [{"key": "<sha256 16 hex>", "pri": "P1|P2|P3|null", "text": "<masked, max 160>", "resolved": false}],
   "now": {"tool": "bash", "short": "<commands.mask_short, 60>"} ,
   "resume": "cd <cwd> && pi --session-id <sid>"}
 ]}
```

Rules:
1. `group`:
   - **needs** if the state is failed, capped, blocked, cancelled or timed_out;
   - **needs** if the item has an unresolved `open` item;
   - **needs** if the verdict is NO-GO or BLOCKED and the owner did not resolve it;
   - an ended item with one of these needs reasons and an end older than 48 hours is **done** with
     `"stale": true` (decision D27). Its blockers stay in `open`;
   - **running** if it runs;
   - **done** in all other cases.
2. `label` is the first `TASK:` line, masked with `commands.mask_short`, or "repo · branch". It never holds prompt text.
3. `commands.mask_secrets` masks `open[].text`. The text has one line and at most 160 characters.
4. Only a running item has `now`. It shows the newest running call (`commands.running_now`).
5. Bounds:
   - 2000 newest pi job dirs, 200 Claude Code sessions, 200 background jobs;
   - an 8 KiB tail of each result file;
   - 3 s for the whole call. When the deadline stops a read, `complete` is false.
6. Order: needs first (newest first), then running (oldest start first), then done (newest first). At most 200 items.

## 5. Resolve

`apex-router agents resolve <id> [<key>|--all]` writes `~/.apex-router/agent-acks.json`:

```json
{"<id>": {"keys": ["<key>"], "all": false, "at": 1793300000}}
```

- The write uses a lock and an atomic replace.
- The file holds at most 2000 ids. It drops the oldest first.
- A new `open` key or a new verdict after the resolve shows again as unresolved.
- `apex-router agents resolve <id> --undo` removes the entry.

## 6. Terminal surfaces

| Surface | Content |
|---|---|
| pi overlay (`apex-agents.ts`, pi-tui) | Right half of the window (`width "50%"`, `minWidth 60`). Full width under 120 columns. One key opens and closes it. List on top, the selected item's detail below. |
| pi widget (above the editor) | One line, shown only while an item runs or needs you: `● 2 running  ⚠ 3 need you`. |
| `apex-router agents` | A plain table: needs first, then running, then done. |
| `apex-router agents show <id>` | Blockers, verdict, label, branch, cost, resume command. |
| `apex-router agents --line` | `● 2 ⚠ 3`, for the Claude Code status line. It uses a 1 s deadline and a 5 s cache. |
| `apex-router agents --new-blockers` | For the Claude Code `Stop` hook. It prints new unresolved blockers since the last call, at most 5 lines. Its state file is `~/.apex-router/agent-inbox-seen.json`. |

Overlay keys:

| Key | Action |
|---|---|
| ↑ ↓ | Move |
| ⏎ | Expand |
| Tab | Filter: Needs / Running / Done / All |
| `r` | Resolve the selected blocker |
| `R` | Resolve all blockers of the item |
| `c` | Copy the resume command |
| `o` | Show `result.md` (last 200 lines) |
| Esc | Close |

## 7. Style

| Meaning | Colour |
|---|---|
| Running | Blue |
| Needs you | Amber |
| Failed | Red |
| Done | Green |

- The colours come from the pi theme, with no fixed RGB values. Bold is the fallback when the terminal has no colour.
- Only the four colours above carry meaning.
- Every line fits the width. pi-tui `visibleWidth` and `truncateToWidth` are the only measure.

## 8. Masking (`commands.py`, from the D19 Commands contract)

Short text: one line, at most 80 characters, made by `commands.mask_short` only:
   a. a read, edit or write call shows the tool and the file name (no folder);
   b. a bash call shows an allow-list: the plain words of the first line, up to the first word that
      is not plain. A plain word has no quote, backslash, `#`, `(`, `)`, backtick or `$` (a `$NAME`
      or `${NAME}` reference is plain). The cut shows `<text>`. A script flag (a single-dash word with
      `c`, `e`, `C` or `E`, `--eval`, `--command`), a heredoc or more lines show `<script>`;
   c. secret-like text becomes `[hidden]`: `NAME=value` where NAME holds KEY, TOKEN, SECRET,
      PASSWORD or AUTH; `Bearer …`; `sk-…`, `ghp_…`, `xox?-…`, `AKIA…`, `eyJ…`; any run of 32 or
      more base64 or hex characters, without `/` (a long path is not a secret: with `/`, 57 % of
      875 real commands had a path hidden);
   d. `masked` is true when step b or c changed the text.
   e. `NAME=value` is a check per word: when the text before the first `=` of a word holds a
      secret-like name, the mask hides the rest of the word. `NAME = value` hides the next word. A value that
      is not one plain word hides the rest of the text.

Boundary: only the short text (rules a, b) is a security boundary. Rule c on free text (blocker lines,
`needs`) is best effort. There is no full-text output (D24).

## 9. Security

- No prompt text, no full commands and no tokens appear in any output.
- All text goes through `commands.mask_short` or `commands.mask_secrets`.
- The overlay calls the CLI with `execFile` (no shell) and a 3 s timeout. It reads JSON from stdout, up to 2 MiB.
