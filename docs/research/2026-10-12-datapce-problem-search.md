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

### R4-f: adversarial refutation of v3 (fresh reviewer) — verdict WEAKENED; method refuted, problem stands

- **Base rates make FN measurement ill-posed.** Anthropic, with thousands of internal
  users and every decision and reason at hand, curated only **52** real overeager actions
  over months; one heavy user saw 19 classifier stops in 2,205 sessions / 90 days. A
  "CI excludes zero" gate is passed by k = 1 and discriminates nothing; ranking rule diffs
  needs dozens of events, about a year per org. Anthropic's own session-level LLM judge
  found 2.4 % of auto-mode sessions contained an unrequested harmful action (severity ≥ 7)
  — a usable base rate, but from a judge, not from outcomes.
- **The proxies do not measure "should have been denied".** FN is defined against user
  intent; arXiv 2604.04978 got 81 % only with an ex-ante oracle of authorised targets;
  OS-Harm's judge reaches F1 0.76–0.79 against humans given the full trace; MonitoringBench
  and the misuse benchmark label with LLM raters, never reverts. Reverts flag bad code;
  session failure is orthogonal; destructive-op detection is circular (if detectable, put
  it in deny); deny → retry measures friction, the FP side.
- **Vendor pre-emption is one query away.** The server-side classifier already produces
  a severity score, matched rule name and reasoning; since September the block reason
  names the rule; the Analytics API already ships per-org tool accept/reject rates
  [PLAUSIBLE, to verify]; Datadog Agent Console computes AI-vs-non-AI change failure rate
  from the same OTLP stream.
- **Buyers buy control, not accuracy.** September 2026 moved the market to deterministic
  rails around the probabilistic guard (Copilot managed block/approve/allow, Cursor run-mode
  policies, Claude `disableAutoMode`/`hard_deny`, Codex guardian tiers). A free report
  reading "≈ 0 incidents, wide CI" converts to nothing; "which rules would have stopped
  it" is the only valuable clause and it needs command text.
- **Privacy blocks the org-wide rule diff.** Without `OTEL_LOG_TOOL_DETAILS`, decisions
  carry tool name, decision and source only; command text exists only in local hooks.
- Minor: "every Team/Enterprise org by default" is loose (`-p` and the SDK start Manual;
  most calls are approved by tier rules, not the classifier).
- **Alternative offered: permission-policy regression testing.** Replay a candidate
  `permissions.deny` / `autoMode` / execpolicy ruleset against recorded tool calls before
  rollout; report what it would block, the friction it adds, whether the three-consecutive-
  block kill switch would trip. Deterministic, local, needs no outcome labels, events are
  abundant (FPs, not FNs), exercises the single-step replay bench and gates directly.
- Most-moving evidence: the 52.

### Round 4 ruling

Ruling: v3's method (per-org FN rate from outcome proxies) is REFUTED; the problem
("no org can compute what auto-approval lets through") stands but is not computable by
anyone from outcomes at org scale. Keep what survived from every round and recombine:
(1) per-decision replay is valid and events are abundant on the FP/friction side
(R1 synthesis, R4-f); (2) command text must stay on the developer's machine (R2-e,
R4-f), so computation is federated and only counts travel; (3) the only published way to
get harm labels at scale is a session-level judge (Anthropic's 2.4 %), and datapce already
has judge and local-tier machinery (label_judge_bench, outcome judge AUROC 0.82, Ornith
tiers); (4) hybrid authorship fits exactly: humans write rulesets, the machine computes
their consequences on history before rollout and proposes tightenings from judged-harmful
and destructive-class events, humans approve, effect is replayed before and measured
after. — advisor: the v3 advisor's conditions are superseded by this refutation; a new
advisory round applies to v4 — cost if wrong: another round — reversal: none.

## Round 5 — v4: permission-policy what-if with a local session audit

> Platform teams now ship permission rulesets org-wide (Copilot managed permissions,
> Claude managed `permissions`/`autoMode`, Codex `requirements.toml`) with no way to test
> a ruleset against their own history before rollout, the way IAM, Kubernetes and OPA
> operators dry-run policy. A ruleset that is too tight recreates the prompt fatigue that
> drives developers to bypass mode; one that is too loose is invisible until an incident.
> datapce replays a candidate ruleset on each developer's locally recorded tool calls
> (command text never leaves the machine), returns only counts — what it would block, by
> class; prompts it would add per developer-day; kill-switch trips — with confidence
> intervals, and grades sessions with a local judge for unrequested harmful actions to
> show what the ruleset would and would not have caught. The same engine proposes
> tightenings from judged-harmful and destructive-class events, in the vendor's format,
> with the evidence card; a human approves; the effect is replayed before and measured
> after rollout.

Round-5 probes: (a) verify the Analytics API accept/reject claim and `claude auto-mode
critique` scope; (b) incumbents for ruleset dry-run (deny-probe, Nomos replay, AGT policy
test fixtures, Cursor, Codex execpolicy check) and for local judge-based session audits;
(c) advisor round on v4; (d) refutation of v4.

### R5-local: throwaway replay of a candidate ruleset on this machine (`/tmp/v4-replay/replay.py`, 2026-10-12) [MEASURED; semantics approximated]

Candidate ruleset: 6 deny rules (rm -rf on root/home, force push, reset --hard), 8 ask
rules (git push, rm -rf, launchctl, kill, curl, installs), 33 allow prefixes (git, ls,
cat, grep, python3, .venv/bin/…). Semantics approximated from the docs: prefix match,
compound commands split on `&&`, `;`, `|`, every segment must be allowed; deny > ask >
allow. Replayed over 3,555 recorded Bash calls in 116 active hours:

| readout | value |
|---|---|
| would allow / ask / deny | 731 / 2,815 / 9 |
| prompts the ruleset would add per active hour | 24.3 |
| top causes of prompts | env-var-prefixed commands (`UV_OFFLINE=1 …`, `PYTHONPATH=… …`, `WS=… …`), heredoc bodies the naive splitter treats as commands, `for` loops, `export` |

Reading: (1) the v4 readout is computable in seconds from local history, no model, no
text leaving the machine; (2) a ruleset that looks reasonable on paper would prompt 24
times an hour on this workload — exactly the fatigue failure v4 exists to show before
rollout; (3) the approximation errors (env-var prefixes, heredocs) are themselves the
product's content: the real matcher must reproduce the harness's semantics faithfully
(the docs describe a shell parser that traces paths and treats untraceable commands as
prompts), which is a fidelity test, not a research risk. Runtime 0.3 s. Code stays in
/tmp; throwaway.

### R5-a/b/c: incumbents for v4 (agent; docs pages read [MEASURED], repos mostly via summaries)

(A) Analytics API (`/v1/organizations/usage_report/claude_code`): per user per day,
`tool_actions.edit_tool.accepted/rejected`, `multi_edit_tool`, `write_tool`,
`notebook_edit_tool`, plus sessions, lines, commits, PRs, cost. **No Bash, MCP or
auto-mode decisions.** So the v3 refuter's "Analytics API already ships accept/reject
rates" holds only for file-edit tools. `claude auto-mode critique` critiques rule *text*
("ambiguous, redundant, or likely to cause false positives"); no history replay.
`/auto-mode-setup` drafts entries from recent sessions' commands; `PermissionDenied` hook
receives `tool_input`.

(B) Ruleset dry-run before rollout — partially served, the team case open:
| tool | replays against | reports | team | licence |
|---|---|---|---|---|
| Nomos | real local transcripts (`--replay-transcripts`) + a 1,526-command corpus, against **its own** profiles | allow/deny/ask counts, why calls ask, top asking programs | no | Apache-2.0 [PLAUSIBLE] |
| claude-perm-sim | one call or hook JSON; `diff` of two settings files on built-in samples | deciding rule, lint, bypass findings (SARIF) | no | MIT |
| deny-probe | synthetic bypass routes; live canary | HOLD/LEAK per route | no | — |
| AGT `agt test` | captured decision traces and fixtures | pass/fail diffs | Studio what-if proposed, unshipped | MIT |
| Codex `execpolicy check` | one command; `match`/`not_match` examples per rule | strictest decision + matched rules | no | Apache-2.0 |
Copilot, Cursor: none found. Open feature request anthropics/claude-code **#39971 "unit
test permissions rules"**. Nothing replays a Claude Code `permissions`/`autoMode` ruleset
against recorded team history and aggregates prompts-added / blocked per team.

(C) Local judge for unrequested harmful actions — open. Anthropic's method: flagged
production sessions (May–June 2026, opted-in accounts) re-graded by a model on two 0–10
scales (severity; how directly the user requested it); at severity ≥ 7, 6.3 % of manually
approved vs 2.4 % of auto-mode sessions had an unrequested harmful action — **a rate within
flagged sessions**, grader and n undisclosed. arXiv 2604.04978's judge is Claude Opus
(88 % agreement with a human on 50 actions, κ 0.82). OS-Harm's judge is configurable
(F1 0.76–0.79 vs humans). Local auditors: claudit (Ollama judge, secrets/PII only),
clauditor (off-role actions, no LLM), sessionscope, cc-audit-log (risky-command flags).
No severity + user-directness grader on open-weight models over Claude Code and Codex
transcripts exists.

Reading for v4: the deterministic half (ruleset replay on local history, team
aggregation) is open with a first-party feature request and a near-miss OSS (Nomos, own
format); the judge half is open and has a published method to copy (two 0–10 scales)
and a published agreement bar to beat (κ 0.82 vs a human on 50 actions). Both halves
are measurable with the assets on hand.

### R5-judge: local open-weight judge micro-probe (`/tmp/v4-judge`, 2026-10-12) [MEASURED, n = 9, my own grades as the reference]

Nine recorded Bash actions from this machine (3 destructive-pattern, 6 benign), each with
the user's last request, graded on Anthropic's two 0–10 scales (severity, directness) by
Ornith 9B (Q4) and Ornith 35B via ollama, thinking off, temperature 0, JSON output.

| judge | warm latency | sev ≥ 7 flags (mine: 0 of 9) | agreement with me on the sev ≥ 7 binary | notes |
|---|---|---|---|---|
| ornith:9b-q4_K_M | ~1.2 s | 4 of 9 | 5 / 9 | calls a build script "irreversible overwrite", a log append "appends sensitive session key" |
| ornith:35b | ~0.8 s (10 s cold) | 1 of 9 | 8 / 9 | the one flag is `rm -rf` of an in-repo scratch dir (defensible); one directness miss |

Reading: the 35B tier is in the right region on severity at trivial latency; the 9B
tier over-flags. n = 9 proves feasibility and cost, not accuracy. The published bar is
κ 0.82 vs a human on 50 actions (Opus judge). The judge-bench in this repo
(`label_judge_bench.py`, cross-family check) is the right instrument for the real test:
200 actions, two humans, 35B vs a cloud judge, κ reported with CI. Thinking must be off
(`think: false`), otherwise ollama returns empty content for these models.

### R5-r: adversarial refutation of v4 (fresh reviewer) — verdict WEAKENED (premise and fidelity fail; judge unproven; pain survives in modified form)

- **Fidelity is the strongest attack.** Permission semantics moved repeatedly in 2026
  (compound-command bypass fixes v2.1.98, `cd &&` handling v2.1.111, a 2.1.269 regression,
  a September fix for `ask` skipped inside subshells in auto mode, the 50-subcommand deny
  bypass). The autoMode half is a server-side classifier nobody can reproduce locally;
  the dry-run request **#39097 was closed "not planned"**, #39971 closed stale; executed
  allows carry no decision reason (#96026). Nomos sidesteps this by *being* the enforcer,
  so its replay runs the live pipeline. Even Codex's own `execpolicy check` misreports
  effective decisions (#38318, #52934). A third-party replay is a re-implementation of a
  moving target with no oracle for allows.
- **"No way to test" is false at $0.** Nomos (Apache-2.0) ships `--replay-transcripts`
  and `--suggest` for Claude Code and Codex. Practitioner guides recommend "pilot one team
  for a week", a human dry-run that is cheap and good enough.
- **Judge.** Evidence supports small *fine-tuned binary* judges (AgentDoG 1.5–4B at 92 %
  on R-Judge, arXiv 2605.29801; TrajAD 4B > 8B), not two 0–10 scales at κ ≥ 0.8 from a
  9–35B generalist (SafetyJudge-LLM local κ 0.61; human IAA ceiling κ 0.84). Base rate
  again: per-developer-week harmful counts near zero.
- **Federated counts are moot for the buyer who pays.** `OTEL_LOG_TOOL_DETAILS=1` exports
  command text; SecOps guides call it the most important switch; Elastic's reference
  pipeline already parses `tool_input`. The security buyer centralises command text.
- **Value capture.** Dry-run is a bundled feature in Styra DAS (~$5k/mo), Nirmata
  ($1.8–4.5k/mo), LaunchDarkly Guardian — never a product. Buyers pay for distribution
  and enforcement; datapce would own neither.
- Pain that survives, modified: Copilot's managed `ask` cannot be satisfied by a saved
  approval, so fatigue is structural there; the first rollout problem practitioners name
  is "prove which policy reached which session" (billing account, client version).
- **Alternative offered: post-rollout policy conformance + release-drift alarm.** Record
  actual allow/deny/executed outcomes (vendor ground truth, no parser re-implementation);
  gate on prompts/day and block-class counts with CIs; re-run a golden corpus against each
  harness release to flag "this release changed what your ruleset does"; local post-hoc
  session grader; wedge "prove the policy reached the session".
- Most-moving evidence: #39097 closed not-planned next to a changelog shipping
  permission-semantics fixes every few weeks.

### Round 5 ruling and a lesson

Ruling: v4 is WEAKENED beyond repair as a product (fidelity has no oracle; the free
incumbent ships both halves; dry-run is a feature). The refuter's alternative (conformance
+ release drift) is feature-sized and vendor-absorbable. Four rounds inside the
agent-permission cluster have each ended the same way: every wedge is either absorbed by
the harness vendor within a release, covered by a free Microsoft or Apache project, or
unmeasurable at the base rate. **Lesson: do not build where the platform owner closes the
loop faster than a third party can measure it.** Round-1 scoring overstated G's
differentiation (3 → 1 in hindsight). — advisor n/a (two independent refutations agree;
re-scoring is bookkeeping) — cost if wrong: none — reversal: none.

Survivors worth carrying into the morning report, ranked by what the night established:
- (α) conformance + release-drift alarm for managed agent policies (feature; vendor ground
  truth; low capture);
- (β) F sharpened: real-time, outcome-aware spend and capacity circuit breakers with
  receipts, proposals from data, per-request replay valid for cost/capacity (strongest
  MEASURED demand of the night; crowded; incumbents broken on exactly this);
- (γ) not yet scanned: **configuration-outcome attribution for coding-agent orgs** —
  the "policy" is the org's agent configuration (managed settings, CLAUDE.md, skills,
  hooks, model tiers); the "data" is outcomes the vendor already exports per user per day
  (Analytics API: commits, PRs, lines, cost) plus git (merge, CI, revert); the computation
  is gated A/B across teams and time (pce-core, gates), with machine-proposed config
  changes (what the owner's nightly already does for skills) that a human approves. This
  is the owner's own stated goal ("RSI workflow optimiser") generalised to an org, and it
  uses outcome data the vendor exports without command text.

## Round 6 — scan (γ) with the same rigour, re-score (β)

Probes: (a) incumbents and demand for config-outcome attribution (LaunchDarkly AI
Configs, Jellyfish/DX/Faros/LinearB AI-impact, Braintrust/Langfuse, Anthropic Analytics
API, GitHub Copilot metrics API, any "measure the effect of a CLAUDE.md / skill / managed
settings change" asks); (b) refutation; (c) advisor; (d) morning report with the ranked
verdict.

### R6-local: this machine's evidence for (β) and (γ) [MEASURED, 2026-10-12]

(β) capacity decisions are abundant on one developer: proxy telemetry holds 112,680
requests; error causes http_429 1,133, SSLError 470, ReadError 393; last 24 h: 2,394
requests, 150 429s (6.3 %; opus 8.3 %, 145 of 1,751), 59 retried. `pressure --check` was
RED for most of this night on opus 429s. Every one of these is a per-request decision
(route, retry, shed, wait) with an exactly observed outcome (status, latency, cost):
the replay-valid regime the Replay Gap paper leaves intact.

(γ) config effects at one developer are small: the repo's own SKILL.state evaluation
(docs/DESIGN-skill-state.md) measured *parity* on behaviour, tokens and robustness
across four adaptations and promoted nothing; the retry A/B in the digest is +11.5 pts
with a CI crossing 0 after weeks. Config-outcome attribution therefore needs org-scale
n (teams × weeks), which is exactly what the Analytics API's per-user-day rows give and
what one developer's history cannot.

### R6-γ: config-outcome attribution scan (agent; all [PLAUSIBLE] — search summaries only, no page read in full; cross-check before citing)

- Demand: finance pressure is about AI spend in general (IBM ~29 % of execs can measure
  AI ROI; Uber cap). No CFO/CTO survey on coding agents specifically; no HN or issue thread
  "did my CLAUDE.md change help"; one hobbyist issue proposing CLAUDE.md section ablation.
  Anthropic's guidance: offline evals + production monitoring; real-traffic A/B is slow;
  skill-creator has benchmark/A-B modes for skills, not org outcomes.
- Incumbents: **Jellyfish AI Impact is closest** — skill usage by team, compaction rates,
  agentic ratio from Claude Code session data, heavy-vs-light skill adopters compared on PR
  rework and cycle time, self-described "early signals, not proof" (observational
  cohorts, no change-event attribution, no proposals). Faros: pre-AI baselines and
  confounder controls, not applied to config. DX (Atlassian, ~$1B): AI Measurement
  Framework. LaunchDarkly AgentControl / Statsig AI experimentation: gated experiments
  on app prompts/models via SDK, not harness configs. Analytics API: per-user-day
  commits/PRs/lines/cost, no config version, no merge/CI outcome. **Copilot metrics API
  is richer: PRs created/merged, median time to merge, Copilot-created/-reviewed splits,
  repo-level GA July 2026, `ai_adoption_phase` cohorts May 2026.** Cursor Enterprise
  analytics: edits, models, skills, spend, no PR outcomes.
- Validity: METR 2025 RCT (16 devs, 19 % slower); DORA 2025 throughput up and instability
  up; Faros 2026 (22k devs) review time up 441 % under high adoption; ASE'26 "A Few Pages
  of Markdown" (441 repos): repos without committed AI config saw ~2× cognitive-complexity
  growth, 73.8 % of config files never modified after commit; an ETH study: context files
  +4 % (human-written) / −2 % (LLM-generated) task success at 20 %+ cost. Agent's own
  reasoning: team-level A/B at 50–500 devs is low-powered; only effects ≥ ~15 % detectable
  in weeks; model upgrades confound monthly; cost per user-day is the low-variance win.
- Market: developer-productivity-insight platforms ~$400M growing 40 %+ (Gartner via
  Uplevel); seats $20–50/dev/month; Jellyfish median ACV ~$36k. A config-attribution niche
  reads as an add-on to these budgets, not a category.
- Verdict: open with adjacent coverage; nobody treats a config change as a tracked policy
  with gates and proposals; Jellyfish closest; risks: low power, no direct ask found,
  incumbents can add change annotations cheaply, first-party versions possible.
  Differentiation would rest on rigour (honest "inconclusive") and joining vendor exports
  to git/CI.

Score γ (provisional): demand 2, value 2, fit 3 (gates, nightly proposals, Analytics API
ingest), differentiation 2, feasibility 2. **11/15**, and the validity note cuts both
ways: the product's honest output at 50 devs is mostly "inconclusive", which is what the
owner's own nightly already reports for its skill edits.

### R6-β: sharpened real-time spend/capacity policy, re-scored (agent; GitHub issues and Cloudflare/OpenAI pages [MEASURED], vendors [PLAUSIBLE])

- Enforcement bugs are real and unfixed: LiteLLM #19105 (team member budgets not blocking)
  auto-closed "not planned" 2026-09-22 after a partial fix; #27735 (stale spend → false
  BudgetExceeded as 429) OPEN, active 2026-09-14; #38087 (budget gauge flapping) OPEN;
  #44555 (per-team token budgets) OPEN 2026-10-05, community PR, no maintainer commitment;
  overshoot from in-flight calls and under-counting when providers omit usage noted.
  Cloudflare spend limits: eventually consistent, cost recorded after completion, 20 rules,
  429 or dynamic fallback, no alerting on the page.
- But vendors already enforce caps in real time: Anthropic Team/Enterprise monthly dollar
  limits at org/group/member since April 2026, blocked at the limit, plus 5-hour and weekly
  windows [PLAUSIBLE]; OpenAI workspace/group/individual credit limits for Codex, spend
  controls 2026-06-18 [MEASURED]; Cursor team-wide hard cap, per-member Enterprise-only
  [PLAUSIBLE]; TrueFoundry circuit breakers on cost velocity, repeated prompts, error rate,
  context growth, with 75/90/100 % actions [PLAUSIBLE, vendor blog]. So "cannot stop in
  real time" is WEAK.
- The receipts ask (LiteLLM #25295) was withdrawn by its author as off-topic; one
  commenter's signal, not demand.
- Replay / what-if of routing, budget or retry policy against history, or proposals with
  significance tests: none found across LiteLLM, Portkey, Helicone, TrueFoundry, Kong,
  Cloudflare, Braintrust, Langfuse, Vantage, CloudZero [PLAUSIBLE, absence]. Closest: Ramp
  Router Shadow Mode (live mirrored sampling at real inference cost, not history replay)
  [MEASURED]. Only hobby repos replay.
- 429 pain for Claude Code teams is current (#96872 2026-09-24: 429s at 26–27 % plan usage
  with 6+ sessions; #44481 closed not planned; #22876) [MEASURED]. Capacity pooling across
  subscriptions/keys exists as OSS rotators (TeamClaude, CLIProxyAPI, claude-proxy
  mid-stream failover) [MEASURED] with ToS risk for consumer subscriptions.
- Price pressure downward: LiteLLM OSS free, Ramp Router free through 2026; Portkey Pro
  $49/mo, Enterprise estimates $2–10k/mo; typical spend $150–250 per developer-month
  [PLAUSIBLE].
- Stripe–OpenRouter (2026-08-19, ~$7–8B): Collison on "routing requests intelligently and
  spending tokens efficiently"; no explicit budget-product statement [MEASURED/ASSUMED].
- Verdict: partially served; the open remainder is explainability (receipts) and
  history replay with gated proposals, with thin market signal and commoditising caps.

Score β (re-scored): demand 3 (spend and 429 pain MEASURED and current), value 2, fit 3,
differentiation 1–2 (caps commoditised; replay/proposals open but unasked-for),
feasibility 3. **12–13/15**, down from 13 on differentiation. The honest shape is a
*feature of the proxy the owner already runs* — per-request replay of capacity policies
with receipts and gated proposals — not a standalone product.

### Rotation note (22:43 PDT)

Session handoff nudge received; handoff written and validated
(`~/.claude/handoffs/9cdb5f6e-e831…md` state block). Round 6 data is complete (α from
R5-r, β above, γ in R6-γ). Remaining for the fresh session: one adversarial refutation
ranking α/β/γ, one advisor round on the ruling, the morning report by 06:00.

## Round 7 (resumed in-session at the owner's instruction, 23:15 PDT)

### R7-β-local: throwaway replay of a capacity policy over this machine's telemetry (`/tmp/v6-capacity/replay.py`) [MEASURED except where marked]

Rows with timestamp and model: 34,960 (older rows lack `model_*`). Per family n / 429s:
opus 25,471 / 1,098; sonnet 5,555 / 35; fable 2,662 / 0; haiku 336 / 0.

Policy P1: if the opus 429 rate over the trailing 15 min exceeds 10 % (n ≥ 10), shed new
opus requests to fable for 5 min.

| readout | value |
|---|---|
| opus requests that P1 would have shed | 2,924 (11.5 %) |
| opus 429s, baseline | 1,098 |
| opus 429s remaining on non-shed requests | 180 |
| 429s expected on shed requests (fable's rate in the same window) | ≈ 0 [ASSUMED inheritance] |
| estimated 429s under P1 | ≈ 180 vs 1,098 |

Not computed: the cost and quality delta of serving 2,924 requests on fable instead of
opus (price per token and task-quality labels would be needed for the receipt). Runtime
under a second. This is the β deliverable shape: a candidate capacity policy replayed
per request against the developer's own history, with an explicit list of what is
observed and what is assumed. The same machinery supports "which of N candidate policies
minimises 429s at ≤ X % cost increase" and a per-decision receipt ("shed because opus
429 rate 43.8 % over 15 min, n = 80").
Pricing for the cost side of the receipt exists in-repo (`proxy_engine/tuner/cachesim.Pricing`,
`APEX_PRICING_OVERRIDES`, the digest's "unpriced" accounting) but was not wired into the
throwaway; a real β probe would price the 2,924 shed requests from their recorded
token counts. Noted, not done tonight.
