# Concept A: River

A living Sankey. Traffic is a river. It flows left to right: Your agents → apex → AI providers → Records.

File: `index.html` (self-contained, mock data inline). States: `#ok`, `#problem`, `#detail`.

## Layout sketch (written before the build)

1. Top bar: one status sentence on the left. Three icons on the right: Changes (badge 2), Experiments, Tokens.
2. Left: 3 springs (pi, Claude Code, Codex). Band width = requests (log scale).
3. Centre: the apex gate. All bands join here. In Tokens mode the river forks: a narrow trunk "sent to provider" and a wide still lake "from cache".
4. Right: each band ends in a delta of 3 fingers at a provider (Claude models, GPT models, Models on this Mac).
5. Bottom: a thin stream drops from the gate to "Records" and folds (zigzag). Records stays folded on screen 1.

Budget check of the sketch: 8 nodes, 3 controls, 1 status sentence, 2 column names. No filter row.

## 10-second answer per state

| State | Answer the user gets in 10 seconds |
|---|---|
| `#ok` | "All good. My 3 agents flow through apex to 3 providers. pi is the widest band. Claude models get most traffic." |
| `#problem` | "Problem: the GPT band breaks in red spray before GPT models. 3 of 10 requests fail. Provider not reached. apex retries. Other bands are fine." |
| `#detail` | "apex is open. Of 1.2 M input tokens, 0.9 M came from cache (wide still lake). Only 0.3 M went to providers (narrow trunk)." The panel shows 5 facts and 1 action: "Show last 20 requests". |

## Counts on `#ok` (1440x900, from the DOM and the PNG)

| Item | Count | Budget | Content |
|---|---|---|---|
| Words | 24 | ≤ 25 | Status (5), "Your agents" (2), "AI providers" (2), pi, Claude Code, Codex, apex, Claude models, GPT models, Models on this Mac, Records |
| Numbers | 2 | ≤ 3 | "4" s ago, badge "2" on Changes |
| Controls | 3 | ≤ 4 | Changes, Experiments, Tokens (nodes are clickable, not counted as controls) |
| Nodes | 8 | ≤ 9 | 3 agents, apex, 3 providers, Records |
| Colours with meaning | 4 | ≤ 4 | flow `#7aa2f7`, focus `#7dcfff` (glow on the open node only), warn `#e0af68` ("waits until idle" tag), error `#f7768e` |

Encodings: band width = volume (log2, min 6 px). Moving dots = live traffic. Red break + spray = errors. Dim (35 %) = not in focus.
With `prefers-reduced-motion`, the dots and spray stop. The width, the red gradient, the ragged edge and static red streaks stay.

## Screenshots

| State | PNG |
|---|---|
| `#ok` 1440x900 | `ok.png` |
| `#problem` 1440x900 | `problem.png` |
| `#detail` 1440x900 | `detail.png` |
| `#ok` 1024x700 | `ok-1024.png` |

## 3 weaknesses

1. Log width hides ratios. pi (60) and Codex (18) look closer than they are. The cache fork uses a linear share (75 %) on purpose, so two scales exist on one page.
2. The delta fingers are decoration. They do not map to models or data. A user can think they mean 3 models per provider.
3. The red break shows where it fails, but not "when". A short spike and a long outage look the same. The time ("Started 6 min ago") is only in the panel.

Other notes: in Tokens mode the "Models on this Mac" finger is very thin (0.02 M). Records has no live signal except a few dots.
