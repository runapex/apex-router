# datapce problem search — RSI-style loop

Date: 2026-10-12. Owner brief: datapce = data + policy computation engine (PCE), hybrid
authorship (machine proposes, human approves; human writes, machine replays). Find a
significant, high-value, high-demand problem for it. Loop: generate variants → score →
pick → deepen → repeat until one problem survives its own kill criteria.

Evidence labels: [MEASURED] read on the page; [PLAUSIBLE] from a search summary or an
agent's digest; [ASSUMED] my inference. Numbers quoted from agents are PLAUSIBLE until I
open the page myself.

## Scoring rubric (0–3 each, 15 max)

| axis | 0 | 3 |
|---|---|---|
| demand | nobody asks | incidents, regulation dates, repeated public asks |
| value | nice-to-have | measurable money or liability at stake, willingness to pay shown |
| fit | needs a new stack | runs on the proxy, hooks, pce-core stats, gates, replay, ledger |
| differentiation | crowded, same feature | nobody ships computed predicates + hybrid authorship + evidence |
| feasibility | team-years | one builder, local-first, Python + TS, months |

## Round 0 — candidate clusters

| id | cluster | one-line problem |
|---|---|---|
| G | governance of autonomous coding/ops agents | what may an agent do, decided by computed policy with evidence |
| F | LLM cost/capacity/routing governance | spend and quotas governed by statistically gated policy |
| C | compliance evidence for AI systems | prove which model saw what under which policy with what outcome |
| D | data + policy engines outside agent ops | decision engines, policy-as-code, agent data access, what-if |

Round 1 = four parallel demand scans (one per cluster). Results below as they land.

### R1-G: governance of autonomous agents (agent digest; labels as reported)

Top problems:
1. Static allow/ask/deny rules cannot express the predicates that decide safety ("is this
   DB production", "is this path outside the repo", "did this token come from an unrelated
   file"), so incumbents prompt too often or let the disaster through. Claude Code docs:
   rules match tool/prefix/path/domain only, command-content rules are ignored as bypassable
   [MEASURED, https://code.claude.com/docs/en/permissions]. Copilot managed permissions:
   shell/file/network only, no conditions [MEASURED, github.blog 2026-09-09]. MCP maintainers:
   annotations "aren't enforcement", risk depends on the rest of the session [MEASURED,
   blog.modelcontextprotocol.io 2026-03-16]. Nine data-destruction incidents Jun-2025 to
   Jul-2026, about half with guardrails off, one with permissions ON (claude-code #10077)
   [MEASURED, https://adversa.ai/blog/ai-coding-agent-incidents/].
2. Per-action prompting produces approval fatigue (about 100 prompts/hour → rubber-stamping
   or skip-permissions); people want a layer that decides most calls and escalates only the
   ambiguous ones with reasons. claude-code #25972 closed stale without reply [MEASURED].
3. Nobody ships the closed loop observed actions → machine-proposed policy → human approval
   → enforced rule → evidence record. Permit MCP Gateway generates policy from tool trust
   levels, not observed behaviour [MEASURED]; Microsoft Agent Governance Toolkit (6.4k
   stars, MIT, YAML/OPA/Cedar, require_approval, Merkle audit) has no policy suggestion
   [MEASURED, github.com/microsoft/agent-governance-toolkit]; AgentGuardian (arXiv
   2601.10440) and Progent have the pieces in academia [PLAUSIBLE].

Demand: Replit/SaaStr Jul-2025, PocketOS/Cursor Apr-2026 (prod volume + backups deleted in
9 s via a token found in an unrelated file) [PLAUSIBLE]; Bedrock key leak $10/mo → $14k/day,
DN42 agent $6.5k with 24 h billing lag [MEASURED, infoq 2026/07]; Gartner first guardian-agent
Market Guide 2026-02-25, 10–15 % of agentic spend by 2030 [MEASURED via thehackernews];
Rein $25M A, Geordie $30M, Comp AI $34M, Keycard $38M [MEASURED/PLAUSIBLE]; EU AI Act
Art. 12/19 logging, high-risk deadline moved to 2027-12-02 [PLAUSIBLE].

Incumbents: Claude Code rules + hooks + auto-mode classifier; Codex sandbox × approval
policy; Copilot deny/ask/allow; AWS AgentCore Policy (Cedar, GA 2026-03-03, Cedar cannot
look up data) [PLAUSIBLE]; Permit MCP Gateway (OPA, approval Enterprise-only); Aembit
$20/agent/mo [MEASURED]; Okta for AI Agents GA 2026-04 [PLAUSIBLE]. Common gap: all evaluate
static attributes of the call; none compute predicates from data; none propose policies
from evidence.

WTP anchors: $20/agent/mo (Aembit) [MEASURED]; one incident $14k–$50k [MEASURED/PLAUSIBLE].

Score G: demand 3, value 3, fit 3 (PreToolUse hook + ledger + gates exist), differentiation
3 (computed predicates + hybrid loop absent everywhere), feasibility 2 (content predicates
need a local evaluator in the hook path). **14/15.**

### R1-F: LLM cost/capacity/routing governance (agent digest; labels as reported)

Top problems:
1. Nobody can prove a cheaper tier is "good enough" for a team's agentic workload: the
   counterfactual is never observed and static replay scores the wrong world. TrueFoundry
   blog states the counterfactual is invisible [MEASURED]; "The Replay Gap" (arXiv
   2608.08239): ~900 branched SWE-bench rollouts, swaps rewrite 61–94 % of post-fork actions,
   log-stitching replay mispredicted every success-relevant outcome [MEASURED]; vllm
   semantic-router #1749 asks for a shadow-mode switch gate before enforcement [MEASURED].
2. Budgets are post-mortem: 20 % of 107 enterprises cannot halt agent spend in real time,
   25 % built custom gateway plumbing [MEASURED, VentureBeat 2026-08-20]; LiteLLM #25295:
   without a signed receipt for why a run continued, "the budget number is mostly postmortem
   data" [MEASURED]; Uber blew its 2026 AI-coding budget in four months, now caps
   $1,500/employee/tool/month overridable with permission [MEASURED, TechCrunch 2026-06-02].
3. Commercial smart routers are vendor-measured and, independently benchmarked, over-select
   expensive models: RouterArena (ICLR 2026, 12 routers), NotDiamond ranks #12 [MEASURED].

Demand: per-developer tokens up 18.6× in nine months (Jellyfish via TechCrunch 2026-06-05)
[MEASURED]; State of FinOps 2026 (n=1,192): 98 % manage AI spend, top ask is request-level
token monitoring that "commercial tooling hasn't delivered" [MEASURED, secondary]; McKinsey:
93 % exceeded token budgets [PLAUSIBLE]; AI FinOps job postings name Anthropic Console and
Cursor Organizations [MEASURED]; LiteLLM #44555 per-team token budgets unanswered [MEASURED].

Incumbents: LiteLLM (dollar budgets, enforcement bugs #19105/#27735/#38087 [MEASURED]);
Portkey (acquired by Palo Alto, drifting to security) [PLAUSIBLE]; Helicone maintenance
mode [PLAUSIBLE]; OpenRouter ~$160M ARR, Stripe acquiring ~$7B [PLAUSIBLE]; Cloudflare AI
Gateway spend limits, 20 rules, no alerts or eval integration [MEASURED]; TrueFoundry
per-agent circuit breakers [PLAUSIBLE]; Braintrust/Langfuse evals without routing policy
[PLAUSIBLE]; Ramp Router "shadow models" mirror traffic to alternates for offline comparison,
no gate/approval loop [MEASURED]. Common gap: no statistically gated routing, no
outcome-derived labels, no approved proposals, no counterfactual replay.

WTP/size: gateway market $0.88B → $1.33B 2025→2026 [PLAUSIBLE]; Accenture n=750: $2.5B on
tokens last year [PLAUSIBLE]; Uber cap implies ≤ $36k/engineer/yr acceptable [MEASURED].
Downshift evidence contested (Haiku 4.5 burned more tokens than Sonnet 4.6 in a 90-trial
study) [PLAUSIBLE] — supports per-task, outcome-labelled measurement over rate-card routing.

Score F: demand 3, value 3, fit 3 (proxy, ledger, gates, advise exist; this IS the current
thesis), differentiation 2 (crowded gateways; the counterfactual/gate piece is open but the
Replay Gap paper says offline replay does not work, so the honest product is online
shadow/A-B with gates, which Ramp Router half-has), feasibility 2. **13/15.**
Note: the Replay Gap finding also kills the "what-if by replay" reading 3 from the
brainstorm for trajectory-level policies; replay stays valid only for single-step cells
(bench.py already restricts itself to that, finding #8).

### R1-C: compliance evidence for AI systems (agent digest; labels as reported)

Top problems:
1. No enterprise can answer "which model saw which data, under which policy version, with
   what outcome" from computed evidence; the answer today is a questionnaire. ValidMind
   2026-05-06: regulators want "which policy version was in force, which rules fired, who
   approved exceptions" [MEASURED]; Arcade.dev: governance-as-PDF answers intent, not "what
   an agent actually did at 03:14 on a Tuesday" [MEASURED]; ISACA 2026-08-24: log tool
   invocations, data domains, write actions; alert on boundary crossings even with valid
   credentials [MEASURED].
2. Git has no provenance for AI-authored code; no ratified schema. Linux kernel mandates
   `Assisted-by:` and forbids agent Signed-off-by [MEASURED, docs.kernel.org]; census of
   180M repos: bot lookup finds 3.3 % of Claude Code commits vs 850k by multi-method
   detection, agents > 320k commits/month [MEASURED, arXiv 2606.24429].
3. Agent adoption outruns governance ~2×; self-assessed readiness ~3× actual. Schellman
   n=525: 74 % believe they'd pass an AI audit, 27 % mature [MEASURED]; OneTrust n=1,200:
   87 % encourage agents, 47 % govern them, 86 % had an AI incident [MEASURED]; Gartner: by
   2027 40 % demote agents after governance failures surface post-incident [MEASURED via
   Zylo].

Dates: EU AI Act Digital Omnibus moved Annex III high-risk (Art. 12 logging, Art. 14
oversight, Art. 26(6) six-month deployer log retention) to 2027-12-02 [MEASURED, Orrick];
GPAI fines live since 2026-08-02 [MEASURED]. Colorado SB 26-189 effective 2027-01-01 with
3-year record retention [MEASURED]. ISO 42001: 220 announced certificates [MEASURED].
NIST CAISI agent initiative 2026-02-17 [PLAUSIBLE]. SOC 2: auditors ask "walk me through a
prompt change to production" [PLAUSIBLE].

Incumbents: Vanta AI Governance (waitlist; discovers agents across laptops/code/harnesses,
detects out-of-boundary activity; policy format unspecified, runtime enforcement
unconfirmed) [MEASURED]; IBM watsonx.governance $3,500/mo [PLAUSIBLE]; Credo mid-five-figures
per use case [PLAUSIBLE]; Gartner MQ June 2026 frames the shift to "enforceable technical
controls, continuous monitoring" [MEASURED via Zylo]. Gap: inventory + assessment + document
evidence; none evaluates machine-readable policy per request with computed, replayable
evidence [ASSUMED from the above].

Size: AI governance platforms $492M (2026) → > $1B (2030) [MEASURED via Nemko]; AI
governance jobs +150 % YoY [PLAUSIBLE].

Score C: demand 3 (regulatory dates slipped to 2027, but procurement and SOC 2 ask now),
value 3, fit 2 (the ledger exists; evidence export and policy versioning do not),
differentiation 2 (Vanta is heading here with discovery; the per-decision computed evidence
is open), feasibility 2. **12/15.** Insight: C is not a separate product; it is the
*evidence export* of G. The buyer for G (eng platform) and C (GRC) differ; G produces what C
needs.

### R1-D: data + policy engines outside agent ops (agent digest; labels as reported)

Top problems (cross-cluster, and they generalise the other three scans):
1. No engine replays a proposed policy change against its *own* decision history and
   reports measured consequences before enforcement. Google IAM Policy Simulator is the
   only shipping replay-against-logs product: 5,000 logs / 90 days, allow policies only
   [MEASURED]; Cedar Analysis proves equivalence over all requests, never real traffic
   [MEASURED]; Databricks Unity Gateway policies have no dry-run [MEASURED].
2. Machine-drafted rules reach ~50–90 % correctness and no incumbent carries
   draft → human approval → evidence-of-effect end to end. NL→Rego 50.3 % end-to-end
   [MEASURED, arXiv 2609.24036]; FICO Assistant "90 %+ component-level", no independent
   benchmark [MEASURED]; Red Hat Rego generator defaults to dry-run for 1–2 weeks with sign-off
   [MEASURED].
3. Policy engines evaluate attributes; none compute predicates from live data with
   statistical confidence. The Cedar pattern is `context.confidence_score >= 85` with the
   score computed elsewhere [PLAUSIBLE]; design guides say "policies should evaluate
   attributes, not compute them" [PLAUSIBLE]; no OPA/Cedar/OpenFGA issue on learned
   predicates found [ASSUMED from absence].

Demand/size: Taktile $110M C (2026-06-22, Goldman) [MEASURED]; Oso absorbed by Temporal
2026-10-06 citing "96 % of permissions go unused" [MEASURED]; Styra DAS $5k/mo, Immuta
~£80k/yr connectors, LaunchDarkly ~$72k median [PLAUSIBLE]; GoRules approval gates
Enterprise-only [MEASURED]; decision-intelligence market $11.5–20.7B 2026 [PLAUSIBLE].

Score D as a market: demand 3, value 3, fit 1 (financial decisioning needs a different
stack and buyer), differentiation 2, feasibility 1. **10/15.** But D's three problem
statements are the *mechanism* every other cluster lacks; they name the engine.

## Round 1 synthesis — pick

| cluster | score | role |
|---|---|---|
| G governance of coding/ops agents | 14 | the wedge: buyer is the platform/eng lead who already runs Claude Code / Codex |
| F cost/capacity | 13 | one policy family inside G (spend circuit-breaker with receipt); the current thesis, not the product |
| C compliance evidence | 12 | the evidence export of G; second buyer (GRC) later |
| D decision engines | 10 | not the market; its three gaps define the engine |

Convergent problem statement (draft, to be stress-tested in round 2):

> Agent policy today is static attributes plus a human prompt. The decisions that matter
> need predicates computed from data (is this path outside the repo, is this host
> production, did tests run since the last edit, is this spend slope off-plan), and the
> hundred prompts an hour the human answers are labels nobody learns from. Nobody ships the
> loop: compute predicates locally → decide most calls with a reason → learn rule proposals
> from the human's own approvals → human approves the rule → evidence per decision.

Kill reading 3 of the brainstorm (what-if by replay) for trajectory policies: the Replay Gap
paper shows offline replay mispredicts outcomes once a swap changes the trajectory.
Replay stays valid for *per-decision* policies (allow/deny/ask on a recorded tool call is a
single-step cell): a proposed permission rule can be replayed against the recorded call
stream exactly, which is what Google's IAM simulator does for IAM. This is a point in favour
of G over F: G's unit of decision is replayable, F's is not.

## Round 2 — deepen G (variants)

| id | variant | why it might win | kill question |
|---|---|---|---|
| G1 | approvals-as-labels: every permission prompt the human answers becomes a label; the engine proposes rules from them; human approves the rule; prompts drop | turns the fatigue complaint into the training signal; hybrid by construction | does the harness expose the human's allow/deny decision to a hook or log? |
| G2 | computed-predicate policy evaluator in the PreToolUse path (path-in-repo, prod-host, secret-in-diff, tests-since-edit, spend slope, freeze window) | directly addresses "rules can't express what decides safety" | latency budget and data access inside the hook; false-positive rate |
| G3 | per-decision evidence record (policy version, rules fired, computed inputs, approver, outcome) + commit trailer | C's buyer, cheap once G2 exists | does anyone pay before the 2027 dates? |
| G4 | spend circuit-breaker with signed receipt | F's sharpest live pain (20 % can't halt) | needs real-time usage, which only the proxy sees; the plugin alone cannot |

Round-2 probes: (a) harness capability check for G1 (which hooks see permission decisions,
what fields); (b) verify Microsoft Agent Governance Toolkit scope from its README;
(c) measure prompt rate on this machine's own sessions (MEASURED number for the pitch);
(d) list the predicates the nine incidents would have needed.

### R2-b: Microsoft Agent Governance Toolkit (README read 2026-10-12) [MEASURED unless noted]

"Policy enforcement, identity, sandboxing, and SRE for autonomous AI agents." SDK/middleware,
not a proxy: enforces "at the application middleware layer". Policy YAML
`governance.toolkit/v1` with expression conditions such as
`action.type in ['drop', 'delete', 'truncate']`; OPA and Cedar also listed; Rust-core
decision engine "stateless, deterministic, fail-closed". Harness plugins for Claude Code,
Copilot CLI, Codex CLI, OpenCode, VS Code; adapters for most agent frameworks; an MCP
gateway. Audit: tamper-evident records of "what policy was active, what the agent
requested, and why it was allowed or denied"; Merkle audit; "Decision BOM"; compliance
mappings (OWASP, NIST AI RMF, EU AI Act, SOC 2); `agt verify --evidence` for CI.
`require_approval` takes an `approvers` list; collection mechanism not on the page.
MIT, 6.4k stars, 1.1k forks, Public Preview, Python v4.1.0.
Not present: computed or statistical predicates, data lookups in conditions, learning or
proposing policies from behaviour ("Shadow AI Discovery" and RL penalties are not policy
generation).

Consequence: enforcement plumbing, audit chain and compliance mapping are commoditised by a
Microsoft MIT project with a Claude Code plugin. datapce must not compete on plumbing.
New variant **G5: datapce as the computation and learning layer above an enforcement
engine** — compute predicates the engine cannot (data, statistics, session state), emit
policies in the engine's language (AGT YAML / Rego / Cedar), consume its decision log as
the data for proposals and replay, and ship evidence of effect. Pending: AGT schema and
extension points (R2 agent running).

### R2-c: this machine's own sessions (MEASURED, script over ~/.claude/projects, 2026-10-12)

| quantity | value |
|---|---|
| session files (two project directories) | 44 |
| tool_use blocks | 5,040 |
| active hours (distinct UTC hours with a tool call) | 119 |
| tool calls per active hour | 42.4 |
| user rows by permissionMode | none 5,246; auto 840; acceptEdits 20; default 11; bypass 1 |
| tool results reading as a denial/refusal (regex, first 300 chars) | 97, of which Bash 76 |
| explicit "user doesn't want to proceed" rejections | 4 |
| auto-mode classifier denials ("Permission to use Bash with command …") | 3, all `rm -rf /tmp/<scratch>` cleanups |

Reading: on an auto-mode user the human label stream is thin (7 decisions in 5,040 calls),
so G1 "approvals as labels" is only rich for default-mode users (the 100-prompts-per-hour
complaint). The three classifier denials were false positives on scratch-dir cleanups the
session itself created: a computed predicate "target created by this session under a
scratch root" would have allowed them with a reason. Both the fatigue case and the
false-positive case point to G2 (computed predicates) as the core, with G1 as its label
source where available.

### R2-d: predicates the nine incidents needed (Adversa roundup, read 2026-10-12) [MEASURED]

| # | agent, date | guardrails | predicate that would have caught it |
|---|---|---|---|
| 1 | Cursor YOLO, 2025-06 | off | delete target resolves outside the working tree |
| 2 | Replit, 2025-07-18 | freeze instruction ignored | destructive DB op while freeze flag set |
| 3 | Claude Code #10077, 2025-10-21 | ON | post-expansion path set includes `/` or `$HOME` |
| 4 | Gemini CLI, 2025-11 | exit code misparsed | destination exists before each move |
| 5 | Antigravity Turbo, 2025-11 | off | resolved target is a drive root / outside workspace |
| 6 | Cursor Plan Mode, 2025-12 | ON, mode bug | any shell execution while mode is plan-only |
| 7 | Claude Code, 2025-12-08 | not stated | expanded rm argument equals `$HOME` |
| 8 | Amazon Kiro, 2025-12 | two-person gate bypassed | production target AND approvals < 2 |
| 9 | Claude Code/Opus 5, 2026-07 | autonomous | shadow/reset DB URL matches production pattern |

Pattern: 7 of 9 are *resolution* predicates (expand the command, resolve the path or URL,
compare to a boundary) that no prefix/glob rule can express and that the harness itself
could compute. 2 of 9 (freeze, two approvals) are *state* predicates over data the harness
does not hold. None needs an LLM to evaluate; all need computation before the call. This is
the strongest single argument for G2: the incidents were not rule-authoring failures, they
were rule-expressiveness failures.
