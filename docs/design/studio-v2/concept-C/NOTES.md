# Concept C: Hub

A calm hub-and-spoke map of one Mac. apex is a ring in the centre. Your agents are on the left. AI providers are on the right.
Records hang below the hub. Open `index.html`. Switch the state with the hash: `#ok`, `#problem`, `#detail`.

## Layout sketch (written before the build)

1. Top line: status sentence on the left, 3 icon buttons on the right (Changes, Experiments, Tokens).
2. Left column "Your agents": pi, Claude Code, Codex. Each one has a spoke to the hub.
3. Centre: the apex ring. Ring fill = share served from cache. Ring colour = health. Text "apex" and "75% cache".
4. Right column "AI providers": Claude models, GPT models, Models on this Mac. A failing node shows one plain cause.
5. Below the hub: Records. The hub click shows its inner steps as a ring of 6 icons, plus one facts card.

Budget check of the sketch: 8 nodes, 3 controls, 2 numbers, 4 colours, about 25 words. Result: in budget.

## Encodings

| Item | Encoding |
|---|---|
| Spoke width | Requests a minute, log scale. |
| Moving dots | Live traffic. No dots on an idle spoke. No dots with `prefers-reduced-motion` (width and colour stay). |
| Red dashed spoke + red node border | Errors on this hop. About 1 in 3 dots stops and fades at the break. |
| Thin grey spoke, dim node | Off or idle (Models on this Mac). |
| Hub ring arc | Share of input tokens served from cache (0.9 M of 1.2 M = 75 %). |
| Hub ring colour | Health: cyan = OK (also the one focus glow), amber = degraded. |

Colours with meaning: blue = flow, cyan = focus / OK, amber = warn, red = error. Grey is neutral (track, idle).

## The 10-second answer per state

| State | Answer in 10 seconds |
|---|---|
| `#ok` | "All is OK. My 3 agents send traffic through apex to Claude and GPT models. pi is the busiest. 75 % comes from cache. The Mac models are idle." |
| `#problem` | "GPT models fail 30 %. The GPT spoke is red and broken. The provider is not reached. apex itself is amber, the rest still flows." |
| `#detail` | "apex is a local relay. It does 6 steps: who is calling, model choice, sign-in, send and retry, token count, savings test (off). 0.9 M of 1.2 M tokens came from cache." |

## Counts on `#ok` (1440x900, manual count from the PNG)

| Item | Count | Budget |
|---|---|---|
| Words | 25 (status 6, column heads 4, agents 4, providers 8, apex 1, "cache" 1, Records 1, "%" not counted) | ≤ 25 |
| Numbers | 2 ("4" s, "75" %) | ≤ 3 |
| Controls | 4 (3 icon buttons + the hub) | ≤ 4 |
| Nodes | 8 (3 agents, apex, 3 providers, Records) | ≤ 9 |
| Colours with meaning | 2 on `#ok` (blue flow, cyan focus); 4 in total with amber and red | ≤ 4 |

`#problem` has 25 words and 1 number (status "Problem: GPT models fail 30%." and cause "Not reached").
Hover on a spoke shows a title and 3 facts. Example: "apex → GPT models | 11 requests a minute | 3 of 10 fail: provider not reached | Typical wait 1.2 s".
`#detail` shows 5 facts and 1 action ("Open request log").

## Test

| Check | Command | Result |
|---|---|---|
| Screenshots | Chrome headless, 1440x900 for 3 states, 1024x700 for `#ok` | 4 PNGs, layout fits at both sizes |
| JS errors | Chrome `--dump-dom` with `--enable-logging=stderr` | 0 errors |
| Moving dots | Count `<circle r="3.4">` in the DOM, `#problem` | 26 normal, 0 with `--force-prefers-reduced-motion` |
| Hover | Temp copy in /tmp fires `mousemove` on 2 spokes | Title + 3 facts each |

## 3 weaknesses

1. The hub ring carries 2 meanings (fill = cache share, colour = health). A newcomer can read the amber ring as "cache is bad".
2. Spoke width shows requests, not tokens. A spoke with few large requests looks thin, but it can cost the most.
3. The inner-step ring is a static picture. It does not show which step failed. In `#problem` the cause shows only on the provider node.
