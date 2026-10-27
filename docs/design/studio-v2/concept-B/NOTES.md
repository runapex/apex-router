# Concept B: Metro

A subway map. Each agent is one coloured line: pi (blue), Claude Code (purple), Codex (cyan).
Each line runs through fixed stations: the agent, then apex (one big interchange), then the provider terminus.
Records is a dotted grey spur below apex.

Open `index.html` in a browser. Switch the state with the URL hash: `#ok`, `#problem`, `#detail`.

## Layout sketch (written before the build)

1. Top bar: one status sentence on the left. 4 icon buttons on the right: Recent, Changes, Experiments, Tokens.
2. Left column: 3 agent stations, one line colour each. Caption "Your agents".
3. Centre: one interchange "apex". 5 small dots in it are the inner steps. A click opens them.
4. Right column: 3 provider termini. Caption "AI providers". Records is a dotted spur below apex.
5. Line width = traffic (log). White trains = live traffic. A red break with one cause = disruption.

Budget check of the sketch: 24 words, 1 number, 4 controls, 8 nodes. The sketch was in budget.

## Encodings

| Encoding | Meaning |
|---|---|
| Line colour (blue, purple, cyan) | Flow. The colour only says "which agent". It has no state. |
| Line width | Requests in the last 15 min, log scale, 3 to 16 px. |
| White trains (dots) | Live traffic. More trains on busy lines. With `prefers-reduced-motion`, there are no trains. Width and colour stay. |
| Red dashed break + red ring + 2 lines of text | Error ("Service disruption", one plain cause). Some red trains stop at the break. |
| White glow, other lines at 15 % | Focus: one journey is lit. |
| Grey, dotted | Neutral (Records spur). |

## 10-second answer per state

| State | Answer the user gets in 10 seconds |
|---|---|
| `#ok` | "All good. My 3 agents ride through apex to their providers. pi is the busiest and goes mostly to Claude models." |
| `#problem` | "GPT models fail 3 of 10 requests. Only the Codex line breaks. The cause is: provider not reached. The other lines run." |
| `#detail` | "apex is an interchange with 5 inner steps. My last request went pi → apex → Claude models. apex keeps no prompt text." |

## First-screen counts (`#ok`, 1440x900)

| Item | Count | Budget | What |
|---|---|---|---|
| Words | 24 | ≤ 25 | Status 6, captions 4, station names 14 (Claude Code, pi, Codex, apex, Claude models, Models on this Mac, GPT models, Records). |
| Numbers | 2 | ≤ 3 | "4" (s ago), "2" (Changes badge). |
| Controls | 4 | ≤ 4 | Recent, Changes, Experiments, Tokens (icon buttons, tooltip names). Stations are clickable map nodes. |
| Nodes | 8 | ≤ 9 | 3 agents, 1 apex, 3 providers, 1 Records. |
| Colours with meaning | 4 | ≤ 4 | Flow (3 line hues), focus (white glow), warn (amber, not used on `#ok`), error (red, not used on `#ok`). |

The `#problem` state adds 4 words in the disruption marker and has 3 numbers in the status line ("3 of 10", no age).

## Secondary areas (one icon each, closed on screen 1)

| Icon | Content |
|---|---|
| Recent (clock) | 5 last requests. A click lights that journey and dims the rest. In `#problem` the Codex row shows "failed". |
| Changes (sliders, badge 2) | "Retries per request 3 → 2: applies now". "Local relay new address: waits until idle". Busy requests: 3. |
| Experiments (flask) | JEPA paused. Gold labels 85 % checked, provisional. Savings estimate off. Second-opinion scores collecting. |
| Tokens (stack) | 12 blocks of 0.1 M: 9 filled (from cache), 3 hollow (sent new). "Of 1.2 M input tokens, 0.9 M came from cache." |

## 3 weaknesses

1. Three line hues all mean "flow". A strict reader counts 6 hues on the page (3 lines, white, amber, red), not 4.
   The agent colours also compete with red for attention when a line is purple or cyan near a disruption.
2. The pi line is one band up to the fork, then splits into 2 widths. The split reads as "two routes", but the
   share (40 vs 6) is hard to compare by eye. Small width steps on a log scale are hard to see (Codex vs Claude Code).
3. The map has no "slow" state. Amber exists in the palette but no line shows it. The inner steps of apex hide
   behind 5 dots; a newcomer may not know that a click opens them. The fixed octilinear layout needs hand work for a 4th agent or provider.

## Test record

| Check | Command | Result |
|---|---|---|
| Screenshots | Chrome headless, `--window-size=1440,900` (and `1024,700`), `--virtual-time-budget=4000`, new `--user-data-dir` per shot | 4 PNGs, looked at each one. Fixed: label clip at the edge, weak dim (CSS transition), tiny trains, amber badge. |
| JS errors and panels | `--dump-dom` with a test hook: open all 4 panels, select Codex, click GPT station in `#problem` | No error. All 4 panels have text. 4 routes dimmed. GPT panel opens. |
| Reduced motion | `--force-prefers-reduced-motion`, `#problem` | No trains. Width, colour and red break stay. |
