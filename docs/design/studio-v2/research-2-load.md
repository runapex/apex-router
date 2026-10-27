# Studio v2 – Research 2: Cognitive load audit

Source: `src/apex_router/studio/ui.html` and `~/.apex-router/studio-shots/tokyo-*.png` (1600x940).
Method: manual count from each screenshot. One token = one word. Code tokens and env names count as one word.
Owner input: "it isn't clear what it does". Keep the idea of data that flows. Re-work all other parts.

## 1. Load per screenshot

| Screenshot | Words | Numbers | Controls | Colours with meaning |
|---|---|---|---|---|
| tokyo-overview | ~94 | ~27 | 14 + 23 clickable nodes | 7 |
| tokyo-branches | ~103 | ~28 | 14 + 23 clickable nodes | 10 |
| tokyo-node-upstream | ~180 | ~34 | 28 + 20 clickable nodes | 10 |
| tokyo-test | ~125 | ~31 | 15 + 23 clickable nodes | 9 |
| tokyo-review | ~250 | ~20 | 8 (canvas blocked) | 6 |

Colour meanings in use: green dot = ok, amber dot = warn, grey dot = off/locked, hollow ring = no data, green toggle = on,
blue edge = flow, cyan = selected/active, magenta = lane tick AND test path AND draft AND focus, lilac/amber/red dashed = branch types.
Magenta has 4 meanings. This breaks "one colour, one meaning".

## 2. Nielsen heuristics (4 = good, 0 = fails)

| # | Heuristic | Score | Example |
|---|---|---|---|
| 1 | Visibility of system status | 2 | Status dots show state, but a hollow ring vs grey dot has no legend. |
| 2 | Match with the real world | 1 | Labels "xval bandit", "cue >>family", "ms p50", "codex cache key" are internal jargon. |
| 3 | User control and freedom | 3 | Draft, Undo, Discard draft and Rollback last apply give a safe way back. |
| 4 | Consistency and standards | 2 | Units change per node: turns, req, resolves, rows, % err, ms p50, pulls total. |
| 5 | Error prevention | 3 | Review step and "restart when idle" prevent a broken request. |
| 6 | Recognition rather than recall | 1 | Shortcuts "(/)" and "b" are hidden in a placeholder and a tooltip. No legend. |
| 7 | Flexibility and efficiency | 3 | Test bar and keyboard keys give a fast path for experts. |
| 8 | Aesthetic and minimalist design | 1 | 23 nodes each show a name, a number and a unit at the same weight. |
| 9 | Recognize and recover from errors | 2 | "3.1 % err" shows a problem. The cause ("502 unreachable") shows only with Branches on. |
| 10 | Help and documentation | 1 | The first screen does not say what the tool does. The owner did not understand it. |

Total: 19 of 40.

## 3. First-screen elements (tokyo-overview)

| Element | Decision | Reason |
|---|---|---|
| Brand "apex flow studio" | Keep | Identity. Add a 6-word subtitle that says what it does. |
| PROXY toggle :8788 main / :8789 local | Hide behind a click | Users change the proxy rarely. Put it in a settings menu. |
| WINDOW toggle 15m / 1h / 24h | Hide behind a click | A default of 15m is enough. Put it in the same menu. |
| Test input + placeholder | Keep | "Send a prompt, see its path" is the core idea. Make the placeholder short. |
| Test button | Merge | Merge into the input. Enter starts the test. |
| Branches button | Delete | Show a branch only when traffic uses it (faint line, then bright). |
| "Draft · 2 changes" chip | Merge | Merge with Review into one button "Review (2)". |
| Review button | Keep | Show it only when the draft has changes. |
| Lane headers (5) | Keep | Use plain words: Agents, Router, Proxy, Models, Logs. |
| Magenta lane tick | Delete | Decoration. It takes the colour that marks the test path. |
| Empty top space in each lane | Delete | ~120 px of empty space pushes nodes down. |
| Node pi | Keep | Source of the flow. |
| Node claude | Keep | Source of the flow. |
| Node codex | Keep | Source of the flow. |
| Node cue >>family | Merge | Merge with auto resolve into one "Router" node. |
| Node auto resolve | Merge | Same step as cue for a user: "pick a model". |
| Node STE prompt | Hide behind a click | A setting. It shows no live value ("–"). |
| Node tool guards | Hide behind a click | A setting with 1 event. Show it in the Router or Proxy panel. |
| Node compaction | Hide behind a click | A setting with 0 runs. |
| Node sub-agents | Merge | Merge into the source that starts them (badge on pi). |
| Node detect client | Delete | Internal step. Same count as shadow. It always passes. |
| Node shadow | Hide behind a click | Off by default. A setting, not a flow step. |
| Node codex cache key | Hide behind a click | A setting for one client. |
| Node auth injection | Hide behind a click | Locked. It shows no value. |
| Node upstream + retry | Merge | Show the error state on the edge to the model, not as a node. |
| Node response + usage | Merge | Show latency as edge speed. Show tokens on hover. |
| Proxy lane (6 nodes) | Merge | One "Proxy" node. A click opens its 6 steps. |
| Node foundry / anthropic | Keep | Destination. Edge width shows its share. |
| Node openai | Keep | Destination. |
| Node local ollama | Keep | Destination. |
| Node telemetry | Merge | Merge with route log and widget history into one "Logs" node. |
| Node route log | Merge | Same reason. |
| Node widget history | Merge | Same reason. |
| Node pressure | Merge | Show it as one global health light in the header. |
| Node xval bandit | Hide behind a click | Internal eval data. Show it in the Logs panel. |
| Status dot on each node | Merge | Show only a problem: amber or red node border. No dot when ok. |
| Toggles on nodes (4) | Hide behind a click | Settings go to the side panel. Off nodes show dim. |
| Lock icon | Hide behind a click | Show it in the panel. |
| Number + unit on each node | Hide behind a click | Show on hover. Edge width shows volume. |
| Edge label "claude, codex: direct to proxy" | Delete | The line shows this. |
| Edge label ">>auto" | Delete | The Router merge removes this edge. |
| Animated flow dashes | Keep | The owner likes it. Make it bigger: speed = rate, colour = health. |
| Grid background | Keep | Low contrast. It does not add load. |

## 4. Text that a visual can replace

| # | Today (text) | Visual proposal |
|---|---|---|
| 1 | "24 req", "8 req", "1 req" on each node | Edge width and particle density show volume per path. |
| 2 | "3.1 % err" + amber dot; "502 unreachable" label | Edge turns red. Particles stop or bounce back at the failure point. |
| 3 | "1.4k ms p50" | Particle speed shows latency. Slow = long trip. |
| 4 | "GREEN 43s old" (pressure) | One ambient health light. It fades when the data is old. |
| 5 | Toggle on/off + "–" value | Off step shows dim and the flow line bypasses it (shape change). |

Extra: the test "why:" line (`source=static_default_low_confidence, confidence 0.40, ...`) can be a lit path plus a confidence bar.

## 5. First-screen budget

| Item | Today (overview) | Budget | Rule |
|---|---|---|---|
| Words | ~94 | ≤ 25 | Lane names, node names, one subtitle, input placeholder. |
| Numbers | ~27 | ≤ 3 | Only numbers that show a problem. All other numbers on hover. |
| Controls | 14 + 4 node toggles | ≤ 4 | Test input, Review (only with a draft), settings menu, theme. |
| Nodes | 23 | ≤ 9 | 3 agents, 1 router, 1 proxy, 3 models, 1 logs. |
| Colours with meaning | 7 (10 with branches) | ≤ 4 | Flow (blue), test path, warn (amber), error (red). One meaning each. |
| Lanes | 5 | 5 | Keep the left-to-right flow. |

## Open items

1. Counts are manual from screenshots. Error is about ±10 %.
2. Hover and keyboard paths for hidden data need a usability test with 3–5 users.
3. A user with reduced motion gets no flow animation (`prefers-reduced-motion` hides `.flow`). Width and colour must carry the state alone.
