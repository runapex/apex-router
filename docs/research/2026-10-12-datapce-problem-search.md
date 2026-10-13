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

### R2-a: harness capability for G1/G2 (claude-code-guide over official docs, 2026-10-12) [MEASURED per the agent]

- `PermissionRequest` hook fires when a tool call needs a permission decision; input has
  session_id, cwd, permission_mode, tool_name, tool_input; output `decision.behavior`
  allow|deny with optional `updatedInput`. No `ask`. The hook does not see the human's
  answer when it returns nothing.
- `PreToolUse` hook: output `permissionDecision` allow|deny|ask|defer plus
  `permissionDecisionReason` and `updatedInput`; default timeout 600 s; a timed-out hook
  does not block unless `onFailure: block` (v2.1.295+).
- `PermissionDenied` hook fires when auto mode denies a call (tool_name, tool_input).
- Rules: "A rule like `Bash(command:rm *)` would be bypassable by a compound command, so
  Claude Code ignores it and emits a startup warning." Content matching is hooks-only.
- Managed settings can ship hooks and deny rules org-wide (hooks need user security
  approval). Audit log covers settings changes, not per-session decisions. No dashboard of
  which rules matched in which sessions.

Reading: the human's allow/deny is reconstructable by joining `PermissionRequest` (no
decision returned) with the following `PostToolUse` (allowed) or the "user doesn't want
to proceed" rejection (denied). So G1's label stream exists for default-mode users. G2's
evaluator fits in `PreToolUse` with a reason string and can rewrite input (`updatedInput`),
which no rule can. The per-session decision ledger the docs lack is G3.

## Round 2 synthesis

G2 (computed predicates) is the core; G1 (approvals as labels) is its label source; G3
(per-decision evidence) is its export; G4 (spend breaker) is one policy family that needs the
proxy; G5 (layer above AGT/OPA/Cedar) is the positioning question. Problem statement after
round 2:

> Coding-agent harnesses decide tool calls with prefix rules, a prompt, or a classifier.
> Seven of nine public data-destruction incidents needed a predicate computed from the
> resolved command (path outside the tree, target is `$HOME` or a drive root, URL matches
> production); none of the shipped rule languages, including Microsoft's AGT, can express
> one. The human's prompt answers and the classifier's denials are the labels for the
> rules that should exist, and nobody learns from them. datapce computes the predicates,
> proposes the rules, records the evidence.

Round 3: (1) adversarial refutation of this statement by a fresh heavy-tier advisor;
(2) positioning variants (standalone plugin vs layer above AGT); (3) Anthropic-builds-it
risk; (4) buyer and price.

### R2-e: local feasibility for G2 (read 2026-10-12) [MEASURED]

- The datapce plugin uses the function-hook module API (`hooks/register.ts`,
  `on('tool.call', { tool: 'Bash' }, …)`). `hooks/router.ts:269` already consumes a `deny`
  result on a `tool.call` handler, so the API can block. `hooks/observe.ts:346` states the
  current contract: "never deny, never rewrite (§10)". Turning G2 on is a product decision
  to amend §10, not a platform gap.
- No shell parser on the machine (`shfmt`, `bashlex` absent); a resolution predicate needs
  a vetted parser or a conservative tokenizer (dependency-vetting applies).
- The plugin never records command text (§4), so the proposal engine (G1) would have to
  work on *derived features* (resolved path classes, boundary relations, tool, exit code),
  which is also the privacy-preserving design.

## Round 3

### R3-c: AGT internals (agent read a shallow clone, 2026-10-12) [MEASURED per the agent]

- The Claude Code plugin does not use the `governance.toolkit/v1` YAML engine. It has its
  own JSON policy (`toolPolicies`, `blockedToolCalls` with regex `commandPatterns`, a
  built-in `recursive-delete` parser) and its own audit log. Hooks: SessionStart,
  UserPromptSubmit, PreToolUse (node command hooks, 30 s timeout); no PostToolUse.
  `review` maps to `permissionDecision: ask`. Open issue #4178: the recursive-delete rule
  never matches `rm -rf` in the Claude plugin; #3831: hooks never fire on Windows.
- Plugin audit entries carry only timestamp, agentId, action (`tool.Bash`), decision,
  previousHash, hash: no tool input, no rule id, no reason, no policy version, no approver.
- The YAML condition DSL is regex-matched, not parsed ("In production, would use a proper
  expression parser"); `and`/`or` split inside quoted literals (#4291); no functions,
  webhooks or data lookups; the caller must put everything into `context`. ADR-0004: no
  LLM in the decision loop. OPA/Cedar backends exist but `govern()` never consults them
  (#3911).
- Python `AuditEntry` has the right *shape* (policy_version, matched_rule, approver_did,
  arguments_hash, outcome, trace_id) but policy_version and approver_did are outside the
  v1.0 canonical hash; the Decision BOM is reconstructed after the fact (ADR-0018).
- Nothing proposes or tunes policies from decisions. ADR-0029 lists "test / replay /
  what-if" as a *proposed* AGT Studio simulator. One closed feature request, #3116
  "DecisionAssure Governance Learning Loop" (closed 2026-06-19).

Reading: the incumbent's breadth is real, its depth on the coding-agent surface is not
(a regex JSON policy whose headline rule does not match `rm -rf`). G5 as "layer above AGT"
loses force: there is little engine to sit on. What AGT does give is the *expected shape*
of an evidence record (policy_version, matched_rule, approver, arguments_hash, outcome) and
a signal that "replay / what-if" is wanted but unbuilt. datapce's evidence record should
be a superset of AGT's AuditEntry fields so a GRC buyer can map it.

### R3-b: demand in issue trackers + vendor shipping (agent; issue counts via GitHub API [MEASURED], vendor facts [PLAUSIBLE])

Issues (anthropics/claude-code unless noted; comments/reactions):
- Path-dependent rules: #28321 directory-scoped allow rules (closed stale, 5c/1r); #70711
  prompts inside sandbox-allowed paths (closed stale); #25137, #40076 rules vs paths outside
  the project (closed); **#95426 "deleted ~600 GB, rm -rf on a substitution resolving to a
  drive root", 2026-09-18, OPEN, 5c/0r, reporter wants resolved targets shown before
  confirmation.**
- Production guards: #89014 destructive command against a production host (2026-08-23,
  OPEN); #27063 wiped production data (closed stale); #14411 (closed autoclose); #36640,
  #9966, #100449 (2026-10-08) similar.
- Fewer prompts / learning: **#11380 "continually asks permission even after always
  allow" (82c/64r, closed completed)** — the only upvote-heavy item; #73587 "Allow always"
  saves exact strings that never re-match (12c/5r); #98159 persistent always-allow (OPEN,
  16r); openai/codex #1260 auto-approved commands via execpolicy (60r, completed), #47134
  excessive approval prompts (OPEN), #50572 "reduce approval fatigue with semantic command
  risk classification" (2026-10-03, OPEN).
- Audit of decisions: #96026 "report the deciding permission rule on every decision"
  (2026-09-22, OPEN, 0r); #95970 "no way to see which rule, hook or classifier decided a
  tool call" (OPEN, 0r); #40634, #32904, #16622, #37153, #38771 closed stale/duplicate —
  refiled about six times.
- Team/org: #89877 team-scoped managed settings (OPEN); #70184 (closed stale); #47741,
  #26714 governance configuration (completed).
- No Anthropic staff reply found on 11 checked issues; closures by stale bot.

Vendors 2026: Anthropic auto mode (classifier approver) shipped March, default since
2026-08-14, classifier moved server-side 2026-09-19, `autoMode.allow`/`soft_deny`,
`allowManagedPermissionRulesOnly`, OpenTelemetry `tool_decision` events. OpenAI Codex:
admin-enforced requirements.toml, permission profiles (04-30), auto-review free (10-06),
prefix-rule "rule amendments" = learned per-pattern allows. GitHub Copilot managed
permissions deny > ask > allow (09-09), per-team policies. Cursor auto-review (05-29),
workspace read mode with team read policy (10-01), admin command denylist. Antigravity
"Strict mode" deny/ask/allow + sandbox. No vendor roadmap statement found.

Reading, with the weakening it implies:
- "Nobody learns from decisions" is WEAKENED: Codex rule amendments are learned allows;
  every vendor has a classifier approver. The learning gap that remains is *rules with
  reasons a human can read and approve*, not auto-allow.
- Demand is real but diffuse: incidents keep recurring (#95426 in Sept 2026 is incident
  #3's pattern again), audit requests are refiled six times, but reactions are near zero
  outside the fatigue complaint. The buyer is not the upvoting individual; it is the
  platform owner after an incident.
- "Nobody ships resolved-target semantics" STANDS across all five vendors (Cursor read
  policy nearest). This is the clause to build on, and it is cheap for a vendor to copy
  (show resolved targets before confirmation is a UI change). Defensibility must come from
  the data side (evidence, proposals, replay), not the predicate alone.

### R3-a: adversarial refutation of the round-2 statement (fresh heavy-tier reviewer) — verdict WEAKENED, two clauses REFUTED

Refuted, and re-checked by me on the official docs 2026-10-12 [MEASURED]:
- "No shipped rule language can express a resolved-path predicate." Claude Code defines
  *critical paths* (root, home, drive roots, cwd and parents, `rm -rf "$VAR"/*`) and
  "`rm` and `rmdir` removals targeting a critical path, which no allow rule or `PreToolUse`
  hook `"allow"` approves" (permission-modes.md:41). It also traces paths with a shell
  parser ("a command the shell parser can't trace … prompts the same way", :45), blocks
  reads outside working directories (`blockReadsOutsideWorkingDirectories`, v2.1.257+),
  and auto mode blocks instance-metadata credential reads, reverse shells, host
  credentials and sibling-container scans by default (:415–422). AGT's Rego backend can
  express `startswith` on a path. The predicate gap is narrower than claimed and the
  vendor is iterating on it fast.
- "Nobody learns from decisions." Codex Smart approvals propose a `prefix_rule` during
  escalation; Claude Code's built-in `/fewer-permission-prompts` mines the last 50
  transcripts and proposes an allowlist for human approval. Both learn *allow* rules only.
- "Seven of nine incidents" → about four are path predicates (1, 3, 5, 7), three are
  credential/scope failures (2, 8, 9) where credential scoping is the simpler fix, two are
  not catchable pre-call (4, 6). Adversa's own conclusion is layered defence, not a rule
  language.
- Demand: #25972 is one user, closed stale. Aembit and Rein sell enterprise agent
  identity/runtime security, not developer-harness policy; not comparable buyers. Gartner's
  guardian-agent guide recommends controls "independent of AI agent platforms", which
  favours a vendor product, not a local plugin.
- Capturability: a solo plugin competes with AGT (free, Microsoft) and vendor-native
  managed deny/hard_deny on trust and on being the enforcement point.

Surviving kernel per the reviewer: no harness learns *deny* rules from classifier denials
or outcomes, and none records per-decision evidence locally. Reviewer's alternative: the
uncontested gap is **post hoc** — Anthropic's own auto-mode numbers (0.4 % false positive,
17 % false negative, per its engineering post [PLAUSIBLE until read]); AGT discussion #276
(decisions not sealed, portable evidence missing); sequence-level risk that per-call gates
cannot see; "log commands not just outputs". Use the proxy telemetry, the outcome-label
store and the statistical gates to measure which classifier/sandbox/rule decisions were
wrong, and emit evidence: the Evidence leg of datapce, not the Policy leg.

### Round 3 ruling (pivot)

Ruling: drop G2 (pre-call computed predicates) as the wedge; the vendor already ships the
core predicates and is adding more per release. Pivot the candidate to the Evidence leg:
**decision-quality measurement and policy proposals for harness permission systems** —
consume the decisions the harnesses already emit (Claude Code OTel `tool_decision`,
hooks, Codex logs), join outcomes (datapce's existing label machinery), compute error
rates with confidence (pce-core), propose rule diffs in the vendor's own formats
(managed `permissions.deny`, `autoMode.hard_deny`/`soft_deny`, Codex `execpolicy`/prefix
rules, Copilot managed permissions), a human approves, the effect is measured. The
enforcement point stays with the vendor; datapce owns the data and the computation.
This is literally "data + policy computation engine, hybrid authored".
Advisor round: pending (one fresh heavy-tier read-only advisor on this ruling after the
R4 facts land). Cost if wrong: a night of research; reversal: none needed.

## Round 4 — stress the pivot

Probes: (a) what the harnesses export per decision (OTel fields; any first-party org
analytics on decisions); (b) who already measures decision quality post hoc
(observability vendors, Anthropic's own post, Codex Guardian); (c) advisor round on the
pivot; (d) refutation of statement v3.

### R4-b: who measures decision quality post hoc (agent; Anthropic and OpenAI posts fetched [MEASURED], vendor dashboards via search summaries)

Answer: an open gap with the ingredients served. No vendor or OSS joins auto-mode decisions
to outcomes or reports an org-level false-negative/false-positive rate; none proposes
deny rules from incidents.

- Anthropic, "How we built Claude Code auto mode" [MEASURED, fetched]: full pipeline
  0.4 % FPR on real traffic (n = 10,000); **17 % FNR on real overeager actions (n = 52,
  curated)**; 5.7 % FNR on synthetic exfiltration (n = 1,000). "The honest number." Most
  misses: the classifier saw the danger but misjudged whether consent covered the action.
  Nothing on per-org measurement, audit, or learning from customer data; eval data was
  employee traffic plus an internal incident log. Ask: "tell us when auto mode gets
  things wrong".
- Independent: arXiv 2604.04978v2 (2026-04-28): **81.0 % end-to-end FNR (CI 73.8–87.4)**
  and 19.8 % FPR on 253 deliberately ambiguous scope-escalation actions (Sonnet 4.6);
  36.8 % of actions were Edit/Write calls the classifier never sees. Code public.
- Auto mode is the default on Team and Enterprise since 2026-08-14 (R3-b).
- OTel `tool_decision` event: tool_name, decision accept|reject, `source`, `tool_use_id`,
  `prompt.id`; classifier verdicts fall under `source: "config"` with no reason field;
  `tool_result` shares `tool_use_id`; beta trace spans carry `success`/`error_class`;
  rejected calls produce no tool_result. Outcome joins are possible. [MEASURED per agent]
- Dashboards: Datadog Agent Console Claude Code dashboard shows accepts vs rejects and
  DORA-style change-failure for AI vs non-AI work (org stability, not per-decision
  quality); Grafana community dashboards show decisions by tool/source (counts, edit
  tools only). [MEASURED via summaries]
- Proposers: Nomos `--replay-transcripts --suggest` proposes allow rules from approved
  prompts, no deny, no outcomes; deny-probe is a static bypass check of permissions.deny;
  built-in `claude auto-mode critique` / `config` and the local "Recently denied" tab.
  A 90-day log grep over 2,205 sessions found 19 classifier stops in 7,266 operations
  (0.26 %) — blocks only, no FNR. Backslash: logged blocks are "denials issued, not
  denials that held" because agents reroute through other tools. [MEASURED via summaries]
- OpenAI Codex auto-review [MEASURED, fetched]: 99.1 % approval on real escalations,
  synthetic recall 90.3 % overeagerness; no FPR/FNR, no org-level reporting, no customer
  loop.
- Public asks: claude-code #95370 (classifier denies authorised security work, verdicts
  inconsistent across identical actions), #83611 (allow-listed tool denied by classifier).
  No HN/Reddit/X thread asking for a decision-quality dashboard found.

Implications recorded by the agent and adopted: FN ground truth must come from our own
labels (reverts, git history, destructive-op detection, session failure) joined by
tool_use_id; FP proxies are deny → user retry/approve or the same action allowed later;
the unclassified Edit/Write lane needs its own coverage; compare to 17 % only with the
n = 52 caveat; proposals ship as `permissions.deny` (hard) and `autoMode.hard_deny` /
`soft_deny`.

### Statement v3 (after rounds 3–4)

> Since 2026-08-14 a classifier approves most tool calls for every Team and Enterprise
> Claude Code org by default; Codex, Copilot and Cursor ship the same pattern. The
> vendor's own miss rate is 17 % on its curated overeager set and an independent study
> measured 81 % on ambiguous scope escalations; the vendor exposes decisions
> (`tool_decision`) but no reasons, no outcomes, and no per-org accuracy. No org can
> answer "how often does auto-approval let a bad action through here, and which rules
> would have stopped it" — the question a platform lead must answer before, and an auditor
> after, an incident. datapce joins the decisions the harnesses already export to
> outcome labels, computes per-org error rates with confidence, proposes deny/allow rule
> diffs in the vendor's own formats with the evidence that justifies each, and measures
> the effect after a human ships them. Enforcement stays with the vendor; the data and
> the computation are the product.

### R4-a: what the harness exports per decision (claude-code-guide over official docs) [MEASURED per the agent]

- `claude_code.tool_decision`: `decision` accept|reject; `source` config|hook|
  user_permanent|user_temporary|user_abort|user_reject; `tool_name`, `tool_use_id`,
  `tool_source`; `tool_parameters` only when `OTEL_LOG_TOOL_DETAILS=1`. `tool_result`
  carries `decision_source`. No matched-rule or classifier-reason field anywhere.
- OTel is enabled by env (`CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_LOGS_EXPORTER=otlp`,
  endpoint/headers); an org can force it through the managed `env` block.
- `autoMode` keys: `environment` (prose trust descriptions), `allow`, `soft_deny`,
  `hard_deny`, `classifyAllShell`; lives in user settings or managed settings, never
  project settings. Classifier verdicts are not exposed in logs, UI or `/permissions`.
- `/fewer-permission-prompts` reads transcripts (commands run, not message text) and
  proposes allow rules only.
- Managed settings ship `autoMode` and `permissions.deny` org-wide; a developer's local
  `allow` can override an org `soft_deny`. Enterprise Analytics API and dashboard report
  usage, PRs, lines, cost — not permission outcomes. No per-org classifier accuracy, no
  decision distribution, published or exposed.

Fit check for v3: the data source exists org-wide without installing anything per
developer (managed `env` → OTLP collector → datapce ingest), and locally with richer
derived features via the plugin's hooks. The proposal targets exist (`permissions.deny`,
`autoMode.hard_deny`, `autoMode.soft_deny`, `autoMode.allow`). The one thing the vendor
withholds — reasons and matched rules — datapce reconstructs from its own derived features
and outcome joins, which is the computation the product sells.

### R4-c: mini-pilot on this machine's transcripts (script, 2026-10-12) [MEASURED]

| quantity | value |
|---|---|
| session files scanned | 46 |
| Bash tool calls | 3,526 |
| `rm -rf` calls | 41 (relative target 28, /tmp 7, absolute non-tmp 4) |
| `rm -rf` followed within two rows by a rejection or classifier denial | 10 of 41 |
| other destructive classes | kill/pkill 17, launchctl unload 2, git checkout -- . 1 |
| absolute non-tmp `rm -rf` targets | a `.superpowers/sdd` dir in-repo, `~/src/jkn-tools`, `~/exports/…`, `~/Downloads/kt/render` |

Reading: derived features (destructive verb class, target class: relative / scratch /
absolute-outside-tree) and decision labels (rejection, classifier denial) are
computable from transcripts alone, without command text leaving the machine. On one
auto-mode user, a quarter of `rm -rf` calls drew a denial; two targets outside the tree
and outside scratch went through. That is the shape of the per-org readout v3 promises
(denial rate by class, pass-through outside boundary), at n small enough to be honest
about: this is a feasibility datum, not a rate.

### Candidate outline for v3 (draft, revised after the advisor and refutation)

- **Buyer**: platform or developer-productivity lead at a Team/Enterprise org running
  Claude Code, Codex or Copilot agents with auto-approval on; second buyer GRC/security
  after an incident or before an audit. Not the individual developer.
- **Wedge deliverable**: the per-org "auto-approval safety readout": accepted-by-config
  calls by derived risk class, pass-through rate outside boundaries, denial false-positive
  proxies (deny → same action allowed later), drift across harness versions, with CIs.
  Delivered from the org's own OTLP stream (managed `env`), no per-developer install;
  richer features with the plugin.
- **Proposal engine**: rule diffs (`permissions.deny`, `autoMode.hard_deny`/`soft_deny`,
  `autoMode.allow`, Codex execpolicy) each with an evidence card (n, rate, CI, classes,
  what it would have changed on the org's own history — per-decision replay is valid
  here). Human approves; shipped via managed settings; effect measured; retired when
  insignificant. Hybrid authorship by construction.
- **Evidence record**: per decision, a superset of AGT's AuditEntry fields (policy
  version, matched rule or source, derived inputs hash, decision, approver, outcome),
  exportable for ISO 42001 / EU AI Act Art. 12 / SOC 2 CC6.1–CC7.2 narratives.
- **What stays with the vendor**: enforcement, the classifier, the prompts.
- **Defensibility**: vendor-neutral (four harnesses), outcome labels the vendor does not
  have (reverts, destructive-op detection, session failure), and the proposal/approval
  ledger. Vendor risk is real: Anthropic could add per-org decision analytics to the
  Enterprise dashboard; the moat is outcomes and proposals, not counts.
- **Kill criteria for the candidate** (pre-declared): (K1) outcome labels cannot be
  derived with precision ≥ 0.8 on a 200-decision hand-labelled slice; (K2) measured
  per-org pass-through-outside-boundary rate is indistinguishable from zero on two pilot
  orgs (nothing to fix → no product); (K3) no pilot org agrees to ship a proposed rule
  within 30 days; (K4) Anthropic ships per-org decision quality with outcomes before the
  pilot ends.

### R4-d: advisor round on the pivot (fresh heavy-tier, read-only) — AGREE, conditional

Agrees (a) beats (b) and (c): (c) is crowded and its counterfactual is unobservable;
(b) is being absorbed by the vendor and identity vendors; (a) sits where the vendor is
structurally weak (no per-org measurement, no customer-data loop, never grades Codex
alongside Claude Code). Two corrections, adopted:
1. Demand is latent, not demonstrated (0-reaction issues, stale closures). Do not build
   a product first; build a **free local report** (`datapce decisions report`) and see
   whether anyone forwards it to a security lead. Gate investment on that.
2. The label join is the hard, unvalidated part. The ~220 gold labels are task-outcome
   labels, not per-decision labels; reverts and session failure are weak proxies for
   "should have been denied"; deny → retry is a weak FP proxy. With a 0.4 % FPR and a ~93 %
   approval base rate, per-org reject counts are tiny and FN CIs wide. Require a
   **two-week spike** on the owner's own OTel/hook stream: decisions per day, fraction
   joinable to any outcome, and whether the bootstrap CI on the FN rate ever excludes 0.
   If not, (a) degrades to "deny-rule proposals from destructive-op detection" without the
   quality metric — still useful, a different claim.
Moat per the advisor: the outcome store + cross-harness stats core, not the dashboard.

Ruling: adopt (a) with both corrections — advisor AGREE-conditional — cost if wrong: two
weeks of spike effort — reversal: stop the spike. Next: adversarial refutation of v3 with
the corrections folded in; then the morning write-up.

### R4-e: base-rate check on the advisor's spike criterion (my arithmetic, Wilson 95 %) [MEASURED inputs: 42.4 calls per active hour from R2-c; the rest ASSUMED]

Assume 3 active hours per developer-day; a "bad action" base rate among decisions of
1e-4 or 1e-3 (unknown; Anthropic's 52 curated cases came from an internal incident log);
true FN rate 17 %.

| developers | decisions/day | bad rate | bad actions/week | FN CI at 17 % |
|---|---|---|---|---|
| 1 | 127 | 1e-4 … 1e-3 | 0.1 … 0.9 | undefined |
| 50 | 6,360 | 1e-4 | 4.5 | [0.05, 0.70] |
| 50 | 6,360 | 1e-3 | 44.5 | [0.08, 0.29] |
| 500 | 63,600 | 1e-4 | 44.5 | [0.08, 0.29] |
| 500 | 63,600 | 1e-3 | 445 | [0.14, 0.21] |

Consequence: the advisor's spike criterion ("the FN CI excludes 0 on the owner's own
stream") **cannot pass for one developer** by base rate alone, whatever the product's
merit. The spike must instead measure what one stream *can* show: (S1) decisions per
day and the fraction joinable to an outcome by `tool_use_id`; (S2) precision of the
derived labels against a hand-labelled 200-decision slice; (S3) the destructive-class
pass-through count outside boundaries. The FN-rate CI is an org-scale deliverable
(≥ ~50 developers at 1e-3, ≥ ~500 at 1e-4) and belongs to the pilot, not the spike.
Spike criterion rewritten accordingly in the outline (K1 stays; the CI test moves to K2
at pilot scale).
