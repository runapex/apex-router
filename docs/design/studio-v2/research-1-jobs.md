# Studio v2 research 1: users, jobs, 10-second questions

Status: research input. Date: 2026-10. Method: desk review (read only), no user sessions.
Sources: `README.md`, `docs/STUDIO.md`, `docs/RESEARCH-MAP.md` (skim), `src/apex_router/studio/flow.py`,
`integrations/swiftbar/README.md`, screenshots `~/.apex-router/studio-shots/tokyo-*.png`, owner feedback.

## 1. Findings in short

1. The page shows 21 cards of equal weight in 5 columns. No card answers "is it OK?".
2. The labels are code names (`shadow`, `cue >>family`, `xval bandit`). A newcomer cannot read them.
3. Each card shows a different unit (turns, req, resolves, rows, pulls, % err, ms p50).
4. The flowing lines are the only part the owner likes. Keep them. Make them carry the story.
5. The one amber dot (`upstream + retry`, 3.1 % err) has no plain cause and no next step.

## 2. Personas

| | P1 Owner: "the agent operator" | P2 Teammate: "the new installer" |
|---|---|---|
| Goal | Keep many agents (pi, Claude Code, Codex) fast, cheap and unblocked on one Mac. | Find out what apex does to his agents, and that it is safe. |
| Moment of use | Menu-bar dot turns orange, an agent hangs, or before a large fan-out. Also after a settings change. | First minute after install. Later only when something looks wrong. |
| Fear | A change breaks running agents. A silent failure wastes tokens or hours. | apex reads or sends his prompts and keys. apex breaks his normal tools. |
| "Good" looks like | One glance: green, traffic flows, nothing waits. One click to the cause. | One picture: "my agents → apex → AI providers". One sentence per part. |
| Tolerance | Knows the internals. Has no time for them during an incident. | Knows AI agents. Does not know proxy, plist, telemetry or STE. |

## 3. Jobs to be done (ranked)

Score: frequency (1-5) x pain when not done (1-5). The owner is the main user, so his values set the rank.

| Rank | Job | Persona | Freq | Pain | Score |
|---|---|---|---|---|---|
| 1 | Check that all agents and providers work now. | P1, P2 | 5 | 4 | 20 |
| 2 | Find where a slow or failed request stopped, and why. | P1 | 4 | 5 | 20 |
| 3 | See which agents use which AI model now, and how much. | P1 | 4 | 3 | 12 |
| 4 | Change one setting without a break of running agents. | P1 | 2 | 5 | 10 |
| 5 | Check which model a prompt will get, and why. | P1 | 3 | 3 | 9 |
| 6 | Understand what apex does, and that my data stays local. | P2 | 1 (first use) | 5 | 5 (P2: rank 1) |

Rank 1 and 2 have the same score. Rank 1 comes first because it is the entry to rank 2.

## 4. The question in the first 10 seconds

| Rank | Question the user asks | The page must show (no click) | Today |
|---|---|---|---|
| 1 | "Is everything OK right now?" | One status word with color: OK / Slow / Problem, and the time of the last request. | Missing. 21 green dots, `GREEN 43s old` in the last column. |
| 2 | "Where does it break?" | The broken part of the flow in red, with one plain cause ("OpenAI: 3 of 100 requests failed"). | Partly. Amber dot, `3.1 % err`, no cause, no next step. |
| 3 | "Who is busy, and on which model?" | Each agent as a line to its provider. Line width = traffic. | Partly. Counts per source, but no agent-to-provider link. |
| 4 | "Is it safe to change this now?" | Per setting: "applies now" or "needs restart, waits for idle", and the number of busy requests. | Partly. Badges `live` / `restart` / `locked`, text in the Review dialog only. |
| 5 | "Which model will this prompt get?" | One path in light: agent → model → provider, and one short reason. | Present, but the reason is a code string (`source=static_default_low_confidence`). |
| 6 | "What is this, and is my data safe?" | One sentence: "apex sits between your agents and AI providers. It counts. It keeps nothing of your prompts." | Missing on the page. Only in `README.md`. |

## 5. Plain names for the flow stages

| Today (lane) | Plain name | One-line meaning for a newcomer |
|---|---|---|
| Sources | Your agents | The AI coding tools you run: pi, Claude Code, Codex. |
| Client steps | Agent add-ons | Small helpers inside pi that act before a message leaves. |
| Proxy | apex | The local relay on this Mac that passes and counts each request. |
| Upstreams | AI providers | The services that answer: Anthropic, OpenAI, local models. |
| Sinks | Records | What apex writes down on this Mac: counts, health, history. |

| Today (node) | Plain name |
|---|---|
| pi / claude / codex | pi / Claude Code / Codex |
| cue >>family | Model choice (`>>` tag) |
| auto resolve | Automatic model choice |
| STE prompt | Plain-English rules |
| tool guards | Safety blocks |
| compaction | Memory summary |
| sub-agents | Helper agents |
| detect client | Who is calling |
| shadow | Savings estimate (test only) |
| codex cache key | Codex cache helper |
| auth injection | Sign-in (Azure) |
| upstream + retry | Send and retry |
| response + usage | Answer and token count |
| foundry / anthropic, openai, local ollama | Claude models, GPT models, Models on this Mac |
| telemetry, route log | Request log, Model-choice log |
| pressure | Provider load (OK / Busy / Overloaded) |
| widget history, xval bandit | Menu-bar history, Second-opinion scores |

## 6. Concepts a newcomer cannot understand

| Term on the page | Why it fails | Suggested fix |
|---|---|---|
| `:8788 main` / `:8789 local` | Port numbers, two proxies with no reason given. | "Cloud relay" / "Local relay". Hide ports in details. |
| Sources, Client steps, Upstreams, Sinks | Data-pipeline jargon. | Section 5 names. |
| `cue >>family`, `>>auto`, `>>local`, `>>opus` | Private pi syntax. | "Model choice". Show `>>` tags only in the prompt test. |
| `shadow` | Unknown meaning. The card shows 32 req but the switch is off. | "Savings estimate (test only)". Gray when off. |
| `STE` | Unknown standard (ASD-STE100). | "Plain-English rules". |
| `compaction`, `0 runs` | pi-internal idea. | "Memory summary". Hide when 0. |
| `codex cache key`, `prompt_cache_key`, shards | Internal tuning. | Move to details. Not on the main view. |
| `auth injection`, Azure AD token | Sounds like an attack. | "Sign-in (Azure)". Lock icon with "managed". |
| `upstream + retry`, `502 unreachable` | HTTP terms. | "Send and retry", "Provider not reached". |
| `ms p50`, `ttft`, `% err` | Stats notation. | "Typical wait 1.4 s", "3 of 100 failed". |
| `telemetry`, `route log`, `rows` | Storage terms; row counts mean nothing. | "Records". Show "last written 10 s ago". |
| `xval bandit`, `pulls total` | Research jargon (multi-armed bandit). | "Second-opinion scores", or hide from the main view. |
| `pressure GREEN`, `43s old` | Level name without meaning. | "Providers: OK". Age only when stale. |
| `foundry`, `ornith`, `it-entra-claude-opus-5-5` | Company and model codes. | Show the model family ("Claude Opus"). Full id in details. |
| `source=static_default_low_confidence`, `cell task:debug`, `embedding off`, `confidence 0.40` | Resolver internals. | "Task looks like debugging. Not sure (40 %), so the default model is used." |
| `tier`, `effort`, `escalated`, `fallback` | Routing vocabulary. | "Model size", "Thinking depth", "Moved to a bigger model", "Default used". |
| Env names (`APEX_CONNECT_RETRIES` etc.) as titles | Code names as labels. | Plain title first. Env name small, below. |
| `live` / `restart` / `locked`, plist, `launchctl`, `bootout`, `/reload` | macOS and pi internals. | "Applies now", "Applies after a safe restart", "Fixed". Commands in details. |
| `Draft · 2 changes`, `Review`, `Branches` | Unclear what a draft is, and what branches are. | "2 unsaved changes". "Show other paths". |
| Mixed units: turns, req, resolves, events, runs, rows, pulls | No common scale. | One unit on the main view: requests in the window. |

## 7. Implications for the v2 design (input to next steps)

1. Put one status sentence at the top. It answers job 1 for both personas.
2. Show 3 main columns by default: Your agents → apex → AI providers. Fold add-ons and records.
3. Let the line color carry health. Let the line width carry traffic. Remove most numbers from cards.
4. Show a cause and a next step on each red or amber part (job 2).
5. Keep all code names, ports, env names and file paths in the detail panel only.
6. Show a first-use card for P2: what apex does, what it stores, how to turn it off.

## 8. Limits of this research

- No interview and no usability test. The ranks are estimates from documents and owner feedback.
- Frequency for P2 is a guess. The standalone app does not exist yet.
- Next: test the 6 questions with the owner and one teammate (5-second test on a mockup).
