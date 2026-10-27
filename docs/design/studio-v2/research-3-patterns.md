# Studio v2 research 3: visual patterns for a clear data flow

Status: research input. Date: 2026-10. Method: desk review from known products (own knowledge, no web search).
Input: `research-1-jobs.md`, `src/apex_router/studio/ui.html`. Rule: the data flow is the one idea to keep.

## 1. The 8 patterns

| # | Source | What it shows | Visual device | Why it reads fast |
|---|---|---|---|---|
| P1 | Sankey diagram (energy, budget flows) | Volume that splits and joins across stages. | Band width = volume. Position left → right = stage order. | The eye compares widths before it reads numbers. The widest band is the main story. |
| P2 | Datadog service map / Grafana node graph | Services as nodes, calls as edges, health on each node. | Node ring colour = health. Ring arc = share of errors. Edge thickness = call rate. | One red ring stands out among green ones. No legend is necessary. |
| P3 | Subway map (London, Tokyo Metro) | Fixed routes through stations. | Straight lines, 45° angles, one colour per line, dots for stops. | The layout is simple and stable. The user follows one colour from start to end. |
| P4 | n8n / Node-RED run view | Path of one item through a workflow. | Visited nodes glow. Edges show an item count. Failed node turns red with a badge. | The user sees "it went here, it stopped here" without a log. |
| P5 | Little Snitch network map | Apps on this Mac and the servers they talk to. | Radial layout, app in centre. Live connections animate. Blocked links show as cut or red. | "Who talks to whom, now" is visible in one picture. It fits a local, private tool. |
| P6 | Apple Screen Time / Activity Monitor | Use per app and per category over time. | One stacked bar per period. Category colour. One total in large type. | One number and one bar answer "how much". Details stay behind a tap. |
| P7 | Stripe dashboard / Vercel deploy | Key numbers with trend; deploy steps with status. | Big number + sparkline + delta in colour. Step list with check marks and promote/rollback buttons. | Trend shape reads before the value. A clear status chip shows "safe / live / failed". |
| P8 | Linear / Tailscale admin | Lists of items with state; machines with online state. | Quiet monochrome base. One small status dot per row. Accent colour only for action. | Low noise lets the one accent colour carry meaning. |

## 2. Pattern to apex job

| apex job | Main pattern | Support pattern | How it looks in apex studio |
|---|---|---|---|
| Is traffic healthy | P2 service map | P8 quiet base | Each stage node has a health ring. All green = no text. One red ring + one plain cause line. |
| Where did my request go | P4 run view | P3 subway map | Select one request. Its path lights up agent → apex → provider → record. Other paths dim to 20 %. |
| What does cache save me | P1 Sankey | P6 Screen Time | Token band splits at apex: "from cache" (teal) and "sent to provider" (blue). One total: "saved 41 %". |
| Change a setting safely | P7 Vercel deploy | P2 ring | Setting chip shows "applies now" or "waits for idle". After apply, the affected node pulses once, then shows result. |
| How are my experiments | P7 Stripe big number | P1 split band | Band splits into arm A and arm B. One verdict word per arm: better / same / worse. |
| (all, overview) | P5 Little Snitch | P3 subway map | Fixed left → right layout: agents, apex, providers, records. Same place every visit. |

## 3. Pre-attentive encodings

| # | Encoding | Means | Misuse to avoid |
|---|---|---|---|
| E1 | Edge width | Request or token volume (log scale). | Do not use width for latency or cost too. One meaning only. Cap max width, so one agent does not hide others. |
| E2 | Moving dots / pulse on edge | Live traffic in the last 60 s. Speed = rate. | Do not animate idle edges. Do not loop motion forever on a static page. Respect `prefers-reduced-motion`. |
| E3 | Red break or red dash on edge | Errors on this hop. | Do not use red for "high volume" or for accent style. Red is for errors only. Amber is for "slow". |
| E4 | Hue per stage (agent, apex, provider, record) | Category, not value. | Max 5 hues. Do not use neon magenta/cyan on many items. Keep neon for one focus item. |
| E5 | Brightness / dim | Focus. Selected path bright, rest dim. | Do not dim data the user needs for the current job. Dim, do not hide. |
| E6 | Position (left → right column) | Stage order in the flow. | Do not reorder nodes with each refresh. Do not use a force layout that moves. |

## 4. Progressive disclosure rules

| # | Rule | Screen 1 (no action) | Hover | Click |
|---|---|---|---|---|
| R1 | One status word first. | "OK" / "Slow" / "Problem" + age of last request. | — | Opens cause list. |
| R2 | The flow is the page. | Nodes, edges, widths, health rings. Max 1 number per node. | Edge: rate, error %, p50 in a small tip. | Edge: last 20 requests on this hop. |
| R3 | Numbers only where they answer a job. | 3 numbers max: requests/min, cache saved %, errors. | Node: 3 to 5 facts in plain words. | Node: detail panel with trend and the plain cause. |
| R4 | Filters and time window stay out of view. | Default window 15 m. No filter row. | — | One "Filter" button opens agent, provider and window choices. |
| R5 | Changes and experiments are a second step. | One small badge: "2 changes waiting", "1 experiment running". | Badge: names of items. | Opens review panel with diff, effect and rollback. |

## 5. Anti-patterns (present or risk)

| # | Anti-pattern | Status | Effect | Fix |
|---|---|---|---|---|
| A1 | Card wall: many cards of equal weight (21 cards, 5 columns). | Present. | No entry point. "Is it OK?" has no answer. | Replace cards with the flow. Put card facts in hover and click panels. |
| A2 | Code names and mixed units (`shadow`, `xval bandit`, turns, req, rows). | Present. | Newcomer cannot read the page. Values cannot be compared. | Use plain stage names from research 1. Use one unit per encoding. |
| A3 | Cyberpunk overload: glow, neon and motion on all items. | Risk. | Accent loses meaning. Errors do not stand out. Eye strain. | Tokyo Night base. One neon accent for focus or live. Motion only on live edges. |
| A4 | Spaghetti graph: branches (fallback, retry, offload) all on by default, or a moving layout. | Risk (branch toggle `b` exists). | Lines cross. The main path is lost. | Keep branches off by default. Show a branch only when it carries traffic or errors. |

## 6. Summary

1. Use a fixed left → right flow (P3, P5) as the only main view.
2. Encode volume with width (P1), health with rings (P2), live with motion (E2), errors with red only (E3).
3. Show one status word and max 3 numbers on screen 1. Put the rest in hover and click.
4. Use the same flow for cache, settings and experiments: split bands and one verdict word.
