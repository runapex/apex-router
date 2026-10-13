# datapce business intelligence — independent analysis

Date: 2026-10-13, 00:20–01:05 PDT. **Iteration 1.** Separate loop from the research
log (`2026-10-12-datapce-problem-search.md`); that log's rulings are treated as one
opinion under test, not as inputs.

Evidence labels: **[MEASURED]** read on the primary page or in-repo data this session;
**[PLAUSIBLE]** from a search digest, third-party tracker or the research log's own
PLAUSIBLE rows; **[ASSUMED]** my inference. Revenue ranges are ASSUMED with the
assumptions stated.

## 1. Opportunity map

| market | buyer | budget trigger | WTP anchors | incumbents, pricing | one-builder capture |
|---|---|---|---|---|---|
| **A. Agent-permission governance** (coding/ops agents) | platform or dev-productivity lead; security after an incident | an incident (#95426, 600 GB, 2026-09, OPEN) [MEASURED via log]; auto mode default on Team/Enterprise since 2026-08-14; org-wide managed settings rollout | Aembit Teams $20/agent/mo, free to 3 agents [MEASURED, aembit.io/pricing]; one incident $14k–$50k [MEASURED/PLAUSIBLE] | vendor-native rules/classifier bundled in $20–$100/seat/mo Team and $20/seat+usage Enterprise [MEASURED, claude.com/pricing]; Microsoft AGT MIT, 6.4k stars, Public Preview, no commercial tier [MEASURED, github]; Nomos Apache-2.0, 19 stars [MEASURED] | **Product: low.** The enforcement point and the semantics belong to the vendor. **Services: medium** — an org that just lost data will pay for a measured ruleset rollout. **Licensing/acquisition: low** — Palo Alto bought a gateway (traffic), not a plugin. |
| **B. LLM cost / capacity governance** | platform eng, FinOps | budget overrun (Uber $1,500/employee/tool/mo cap) [MEASURED]; 429 storms on Team plans (#96872) [MEASURED] | Portkey Production $49/mo flat [MEASURED, portkey.ai/pricing]; TrueFoundry Pro $25/user/mo [MEASURED]; LiteLLM Enterprise custom, ~$250–$2,500/mo per competitor estimates [PLAUSIBLE]; Cloudflare AI Gateway core free, 5 % on credits [MEASURED] | consolidation: Portkey → Palo Alto (~$700M class, closes FY-Q4 2026) [PLAUSIBLE, thenewstack]; OpenRouter → Stripe (~$7B) [PLAUSIBLE]; LiteLLM OSS free; Ramp Router free through 2026 | **Product: ≈ 0 standalone.** Price ceiling is $25–$49/mo and falling; acquirers buy traffic and customers, which apex-router has none of. Value is as the **free credibility layer** under the plugin and as the first consumer of the gate engine. |
| **C. Compliance evidence for AI** | GRC lead / CISO; auditor-driven | SOC 2 renewal with AI questions (now); ISO 42001 (220 certs) [MEASURED via log]; Colorado SB 26-189 2027-01-01, 3-yr retention [MEASURED via log]; EU AI Act Annex III 2027-12-02 [MEASURED via Orrick per log; adoption status not re-verified tonight] | Vanta median ACV ~$20k, range $7.5k–$57k, Trust Center + VRM add-ons ~$17k/yr [PLAUSIBLE, Vendr via trackers]; IBM watsonx.governance $3,500/mo, Credo mid-five-figures/use case [PLAUSIBLE] | Vanta AI Governance (agent inventory via Trust Graph, boundary detection, ISO 42001/NIST AI RMF mapping; module price unpublished) [MEASURED, vanta.com]; Drata, Secureframe; AGT "Decision BOM" (audit entry lacks tool input, rule id, approver) [MEASURED via log R3-c] | **Highest for this builder.** GRC pays on a dated deadline and does not care who enforces; no harness vendor has an incentive to emit *cross-harness* per-decision evidence from developer laptops. Capture paths: paid evidence pack; **connector licensed to / partnered with a GRC platform**; services. |
| **D. Config-outcome / engineering intelligence** | VP Eng, CTO | "what is our AI coding spend buying" | Jellyfish median ACV $35,920, $20–$50/dev/mo, AWS Marketplace $30,800/15 seats [PLAUSIBLE, Vendr/Marketplace via digest]; DX → Atlassian $1B (350 customers, < $5M raised) [MEASURED, TechCrunch] | Jellyfish AI Impact (2026-10-07), DX/Atlassian, Faros, LinearB; Copilot metrics API with merge outcomes | **None.** This is a distribution business (DX's price per customer ≈ $2.9M); a local-first builder lacks the org PR data. Agree with the research loop. |
| **E. Policy / decision engines** | fintech/risk ops; platform security (OPA/Cedar) | regulated decisioning; authz re-platforming | Taktile $110M Series C [MEASURED via log]; Styra DAS ~$5k/mo, Immuta ~£80k/yr [PLAUSIBLE; styra.com unreachable tonight] | Taktile, GoRules, Styra/OPA, Cedar, Oso → Temporal (2026-10-06) [PLAUSIBLE; temporal.io URL 404] | **Not this builder's market.** The mechanism (draft → approve → replay → evidence of effect) is datapce's *engine pitch*, but the buyers, data and sales cycle are fintech. Agree with the loop. |
| **F. Stats "gate engine" library** (sequential gates + evidence card, stdlib Python + TS parity) | ML-platform and SRE engineers; vendors needing advise→enforce stats | none directly; vendor roadmaps listing "what-if/replay" (AGT ADR-0029) [MEASURED via log] | $0 as a library; experimentation engines were absorbed (Statsig → OpenAI → Amplitude; Eppo → Datadog ~$220M) [PLAUSIBLE; could not re-verify tonight] | statsmodels, Spotify confidence, evidently, MLflow thresholds, Argo/Kayenta | **OSS only.** Not a business, but it is the **proof asset** that makes models 2 and 3 below credible, and the cheapest thing to ship. |

Cross-cutting price fact [MEASURED]: Anthropic sells Claude Code inside Team seats at
$20 (standard) / $100 (premium) per month annual, Enterprise at $20/seat + API-rate
usage. Any per-developer governance add-on is priced against that: $5–$8/dev/mo
(25–40 % of a standard seat) is the ceiling a platform lead will defend without a
compliance deadline [ASSUMED].

## 2. Three business models that fit the assets

**Model 1 — Agent Decision Evidence: OSS plugin + paid evidence pack (market C, fed by A).**
The MIT plugin keeps recording derived features per tool decision locally (never
command text, per the current privacy contract). The paid layer is an org-level
*evidence pack*: per-decision records (policy version, source/matched rule, derived
inputs hash, decision, approver, outcome) as a superset of AGT's AuditEntry, aggregated
across Claude Code, Codex and Copilot, exported as SOC 2 CC6.1–CC7.2 / ISO 42001 /
EU AI Act Art. 12 narratives with counts and CIs.
- Who pays: security/GRC lead at a 20–300-developer org on Claude Code Team/Enterprise
  that is inside a SOC 2 or ISO 42001 cycle.
- Price: $5k–$15k/yr flat per org (≈ $6/dev/mo at 100 devs; below Vanta add-on
  levels, above Aembit's per-agent line) [ASSUMED from anchors].
- First 10: the datapce.com early-access list; SOC 2 audit firms as referral channel;
  Vanta/Drata integration marketplaces; the Claude Code plugin marketplace with the
  free report as top of funnel.
- 12-month revenue: **$0–$60k** (0–6 orgs × $5k–$10k; 6-month sales cycle; export
  exists by month 3) [ASSUMED].
- Biggest risk: Vanta already claims discovery "across laptops, code, harnesses"
  [MEASURED via log]; a Claude Code/Codex connector from them reduces Model 1 to their
  data source (see Model 3).

**Model 2 — Services first: "agent governance readiness" engagements, OSS as the instrument (markets A + B + C).**
A fixed-scope 2–4-week engagement: deploy the local report across the client's
developer fleet; deliver measured prompts/hour, destructive-class pass-through outside
boundaries, candidate ruleset replay (the 0.3 s replay already works), spend and 429
profile from the proxy, a proposed managed-settings ruleset with evidence cards, and an
evidence record the auditor can read.
- Who pays: platform lead after an incident, or CISO before an audit. Budget is
  incident or audit budget, not tooling budget — the only budget that exists *now*.
- Price: $12k–$30k per engagement; $3k/mo retainer for re-runs per harness release
  [ASSUMED; anchored on Credo "mid-five-figures per use case" and Vanta add-ons].
- First 10: publish the log's measured numbers as content (42.4 calls/hour, 24
  prompts/hour under a plausible ruleset, nine incidents → predicates); offer the free
  report; convert the orgs that forward it to a security lead; one SOC 2 auditor as
  referral partner.
- 12-month revenue: **$40k–$150k** (3–6 engagements + 1–2 retainers; ~40 % of one
  builder's time) [ASSUMED].
- Biggest risk: it does not compound; the product stalls while delivering. Mitigation:
  every engagement must leave a reusable report module.

**Model 3 — Engine licensed to a vendor / acquisition target (market F feeding B and C).**
Package `core/` (Wilson, BH, bootstrap, CUSUM, cell state machine) + a single verdict
schema + advise→enforce promotion as an embeddable, stdlib-only Python + TypeScript
library with parity fixtures, with the proxy's own retry/wait/shed decision as the
reference consumer and the evidence card as the artefact. Then approach vendors whose
roadmaps name "what-if / replay / promotion" but ship point thresholds.
- Who pays: a gateway (TrueFoundry, Portkey/Prisma AIRS), a GRC platform (Vanta,
  Drata) or an engineering-intelligence vendor (Jellyfish), as OEM licence or acqui-hire.
- Price: $30k–$100k/yr OEM, or acqui-hire [ASSUMED; no public comparables found].
- First 10: there are five conversations, not ten customers. Credibility comes from the
  public library with the proxy's receipts and from Model 2's client readouts.
- 12-month revenue: **$0–$50k**, binary [ASSUMED].
- Biggest risk: R7-lib's own finding — buyers accept a hand-rolled threshold and absorb
  the idea without a cheque, as Statsig and Eppo were absorbed.

Recommended combination: 2 funds 1; F is built anyway because 1, 2 and 3 all need it.

## 3. Where I disagree with the research loop

1. **"A free OSS incumbent exists" was used as a kill criterion; it is not one.**
   Nomos has 19 stars [MEASURED]; AGT is 6.4k stars but Public Preview with a headline
   rule that fails to match `rm -rf` (#4178) [MEASURED via log]. Meanwhile LiteLLM is
   free and Portkey still sold for a reported ~$700M [PLAUSIBLE], TrueFoundry charges
   $25/user/mo and Cloudflare bundles the same core for free [MEASURED]. Capability
   availability at $0 does not establish that the market is served; buyers pay for
   packaging, support, the evidence trail and someone to call. The loop narrowed this
   lesson after the cross-family review but never re-ran the rankings with it.

2. **Market C was folded into A and then died with A.** "C is the evidence export of
   G" was right, but when the *enforcement* wedge was refuted the *evidence* buyer was
   never re-examined. C has the only dated budget triggers in the search (2027-01-01,
   2027-12-02, every SOC 2 renewal), the highest price anchors (Vanta ~$20k median ACV,
   IBM $3.5k/mo) and the lowest pre-emption risk: Anthropic, OpenAI and GitHub have no
   reason to produce evidence about each other's harnesses. All four refutations
   attacked the *measurement* claim (FN rate, fidelity); none attacked "an auditor-ready
   decision record across harnesses", which needs counts and provenance, not harm labels.

3. **"Feature-sized" was treated as failure.** For one builder, local-first,
   feature-sized + OSS + services is the correct shape; the loop implicitly scored
   against a venture-scale product the owner's constraints exclude. Owner decision 1
   ("accept a feature-sized direction?") is the wrong question; the right one is
   "which feature has a buyer with a budget line this quarter".

4. **β (capacity replay) ranked first; I rank it last for revenue.** It is the cheapest
   experiment and belongs in the proxy, but its price ceiling is $25–$49/mo [MEASURED],
   its live Team-plan pain is subscription throttling whose only lever is ToS-exposed
   key rotation [MEASURED via log], and spike-0 showed replay does not beat persistence
   (error 969 vs 11, n=2). Its value is credibility and a reference consumer, not a customer.

5. **Services were never priced.** Every scan asked "would a vendor absorb it", never
   "would an org pay a person to run it once". After an incident or before an audit
   they do; that is the first revenue.

6. **Agreement:** D and E are correctly excluded, and the loop's verified corrections
   (FPR scopes, the unsupported "April 2026" date, the unsourced 73.8 %) raise my
   confidence in its facts even where I reject its verdicts.

## 4. Signals to watch (dated)

| when | signal | flips |
|---|---|---|
| monthly, from now | Anthropic Enterprise Analytics API adds Bash/MCP/auto-mode decisions or a per-decision audit log in managed settings | Model 1 loses its single-harness value; cross-harness value remains |
| monthly, from now | Vanta "AI Governance" product updates list a Claude Code / Codex / Copilot connector | Model 1 → partner-or-die; Model 3 conversation with Vanta becomes urgent |
| by 2026-10-31 | Palo Alto–Portkey close (FY-Q4 2026) and any repricing of the $49 Production tier under Prisma AIRS | confirms market B's price trajectory |
| 2026-11-30 → 12-04 | AWS re:Invent: AgentCore Policy (Cedar) updates, any data-lookup predicates | narrows the computed-predicate gap further |
| 2027-01-01 | Colorado SB 26-189 effective, 3-year record retention | first hard date for Model 1's evidence pack |
| 2027-Q1 | EU Digital Omnibus formal adoption status (high-risk date 2027-12-02) | Model 1's second date; a further slip weakens C |
| 2027-01 | Nomos stars (19 today), AGT leaving Public Preview, any Azure-hosted AGT with evidence export | OSS demand for ruleset replay; Microsoft commercial intent |
| ongoing | LiteLLM #44555 per-team token budgets; Anthropic Team-plan 429/concurrency changes (#96872) | β's live pain evaporates or persists |
| ongoing | Jellyfish publishes AI Impact pricing; Copilot metrics API adds policy/config version fields | D re-opens only if vendors export config versions |
| 2027-03-31 | **Own gate**: paid engagements signed (target ≥ 3) | go/no-go on datapce as a business versus a portfolio asset |

## 5. Recommendation to the owner

Stop choosing a market by capability gap, because for a one-builder, local-first entrant
every capability gap in agent operations is closed by a vendor or a free project within
a release; choose by who has a budget line with a date and cannot be pre-empted by the
harness vendor. That buyer is the security/GRC lead who must show an auditor what the
coding agents did, across harnesses, by 2027-01-01 and 2027-12-02 — and, today, after an
incident. Sell them services first (Model 2) using the plugin, the proxy telemetry and
the gate engine as the instruments; turn each engagement's output into the evidence pack
(Model 1); publish the gate engine as the OSS proof asset (F) with the proxy's capacity
decisions as its first consumer, which keeps β alive at zero marginal cost without
betting on it. Set one external gate: three paid engagements by 2027-03-31. If zero,
datapce is a portfolio asset, and the five vendor conversations (Model 3) are the exit.

## Iteration 2 — 2026-10-13 02:11 PDT

Research log check: nothing appended after 23:57 PDT (last commit c694d57); Iteration 1
already covered the cross-family review, Nomos, arXiv and round 8. The re-test is
therefore against primary pages fetched tonight (search quota exhausted; all direct fetches).

### (a) Re-test of Iteration 1 conclusions

| # | conclusion | ruling | reason |
|---|---|---|---|
| 1 | Market C (compliance evidence) is the best one-builder market | **KEPT, narrowed** | buyer and budget line survive (Strike Graph publishes $21.5k–$35k/yr GRC plans [MEASURED]); but the *regulatory* triggers fail, see 3 |
| 3 | Dated triggers 2027-01-01 (Colorado) and 2027-12-02 (EU Annex III) fund Model 1 | **DROPPED** | SB 26-189 is "Automated Decision-Making Technology" in *consequential decisions*, signed 2026-05-14, obligations from 2027-01-01, 3-yr records [MEASURED, leg.colorado.gov]; Annex III is 2027-12-02 [MEASURED, artificialintelligenceact.eu; adoption status not stated on-site]. Neither covers an org *using* coding agents. Only SOC 2 / ISO 27001 renewals and customer questionnaires remain as dated triggers [ASSUMED]. |
| 4 | "Harness vendor has no incentive to emit per-decision evidence" | **REVISED** | Claude Code already exports an OTel `claude_code.tool_decision` event per decision: `decision` accept/reject, `source` config/hook/user_permanent/user_temporary/user_abort/user_reject, `bash_command` with `OTEL_LOG_TOOL_DETAILS=1`; but "the event doesn't indicate which of these sources matched" and no policy version [MEASURED, code.claude.com/docs/en/monitoring-usage]. Enterprise Analytics API: accept/reject counts for four file-edit tools only, no Bash, no permission mode [MEASURED]. Codex: a Compliance API "for audit and investigation records", managed `requirements.toml`, and "MCP hooks do not provide a complete Compliance API audit trail" [MEASURED, learn.chatgpt.com]. Copilot audit log: UNCHECKED (404). Signal #1 of Iteration 1 has half-fired; Model 1's value is now **matched rule + policy version + cross-harness normalisation + auditor narrative**, not the raw decision stream. |
| 5 | Vanta is the pre-emption risk ("partner-or-die" signal) | **REVISED: it is Drata, now** | Drata "AI Agent Governance", Limited Availability, "shipping first for Anthropic": inline Sensor that "blocks violations inline", a **Trust Ladder "from Training to Recommendation to Active"** (advise→enforce promotion), "Chain of Custody" tamper-evident per-decision record mapped to SOC 2 / ISO 27001 / ISO 42001 / EU AI Act / AIUC-1; no price [MEASURED, drata.com/products/agent-governance]. Vanta AI Governance remains waitlist, names no coding assistant [MEASURED]. |
| 6 | Model 1 price $5k–$15k/yr | REVISED down | the price slot that exists in GRC budgets is "additional framework" $2k–$8k/yr or a pentest deliverable $3.5k–$10k [MEASURED, below] |
| 7 | Model 2 services-first, $12k–$30k/engagement | KEPT, re-anchored | pentest comparables ($3.5k autonomous, $10k standard, $20k enhanced) bound the fixed-scope readout; $12k–$30k holds only post-incident |
| 8 | Model 3 engine licence, binary | KEPT, target changed | Drata shipped the *concept* of promotion without the statistics; it and the AIUC-1 auditors are the conversations, not Vanta |
| 9 | OSS-incumbent point; β last; D, E excluded; gate 3 engagements by 2027-03-31 | KEPT | nothing new |

New, unforeseen in Iteration 1: **AIUC-1**, "the standard for AI agent security, safety and
reliability", maintained by the Artificial Intelligence Underwriting Company, updated
"Q4: Oct 8, 2026", audited by Schellman and Coalfire, certificate valid one year with
quarterly retests, Cursor/Harvey/Sierra/UiPath certified [MEASURED, aiuc-1.com]. Control
E015 (Mandatory, Universal): "Maintain logs of AI system processes, actions, and agent
outputs"; E004: document which changes "require formal review or approval" with
"supporting evidence" [MEASURED, standard.aiuc-1.com/accountability]. This is a dated,
recurring budget trigger that applies to companies that *sell* agents — a buyer I missed.

### (b) Market C, concrete

Ten first customers / customer types (trigger → who signs, control → artifact):

1. **Series B–C SaaS, 50–150 devs on Claude Code Team, SOC 2 Type II renewal in ≤ 2 quarters** → CISO/Head of Security signs (< $25k needs no procurement committee [ASSUMED]); CC8.1 change management, CC6.1/6.8 → per-period "AI-agent change evidence pack": every agent-authored change with decision, source, matched rule, approver.
2. **Agent vendor pursuing AIUC-1 (Cursor-class down to seed-stage)** → CTO signs; E015 + E004; auditor Schellman/Coalfire → E015 log evidence and E004 approval records for the agents that build their product, refreshed per quarterly retest.
3. **Fintech / payments under SOX ITGC** → controller + CISO; ITGC change-control testing by the external auditor → per-change decision record with human approval for AI-authored commits.
4. **Healthcare SaaS (HIPAA, HITRUST)** → security lead; BAA customer questionnaire "do AI tools touch PHI" → boundary evidence: counts of agent actions outside approved paths, derived features only.
5. **FedRAMP Moderate / CMMC L2 contractor** → ISSO; AU-2/AU-12 audit events, CM-3 change control; 3PAO/C3PAO assessment → AU-family evidence for agents on developer endpoints.
6. **ISO 42001 pursuer (220 certs)** → CISO; AIMS Annex A lifecycle/logging controls [ASSUMED IDs] → AI-tools-in-development evidence section.
7. **Org after an agent incident (#95426 class)** → VP Eng; incident budget → readiness engagement with 0.3 s ruleset replay and evidence cards (Model 2).
8. **EU deployer that is itself Annex III (credit, HR)** → DPO/compliance; Art. 12 logging, 2027-12-02 → only if their high-risk system is agent-built; minority [ASSUMED].
9. **SOC 2 audit firms and vCISO practices** → practice lead; clients asking the AI question → white-label report module; a channel more than a customer.
10. **Cyber insurers / underwriters** (AIUC itself is an underwriter) → underwriting lead; application questions on agent controls → attested evidence summary [ASSUMED, no page read].

Procurement reality: the buyer's TPRM applies to datapce too; local-first with no upload
means no subprocessor/DPA change — its strongest procurement argument [ASSUMED].

Comparables with real prices:
- **Strike Graph** Scale $21,500/yr, Enterprise $35,000/yr; additional framework
  $2k–$8k/yr; assessment services $4k–$8k/yr; pentest $10k, enhanced $20k;
  ISO 27001 internal audit $7k–$8k/yr [MEASURED, strikegraph.com/pricing].
- **IBM watsonx.governance** Risk & Compliance Basic from $3,500/mo, Advanced $6,450/mo,
  AWS Marketplace from $42,000 [MEASURED, ibm.com].
- **Cobalt** autonomous pentest $3,500/test (promo to 2026-12-31); credit price unpublished
  [MEASURED, cobalt.io/pricing]. Also: Aembit $20/agent/mo [MEASURED, Iteration 1];
  ISO 27001 audit $8k–$30k, surveillance $8k–$15k/yr [MEASURED, isms.online; ISO 42001
  ASSUMED similar]; SOC 2 Type II fee $10k–$50k [PLAUSIBLE; cost pages 404ed]. Vanta,
  Drata, Secureframe, Oneleet, Thoropass publish no prices [MEASURED].

### (c) Revenue, recommendation, two assumptions

12-month ranges [ASSUMED]: Model 1 evidence pack **$0–$40k** (was $0–$60k; 0–5 orgs at
$2k–$8k, Drata and OTel compress it). Model 1b AIUC-1 evidence kit for agent vendors
**$0–$30k** (new; $3k–$8k per cert cycle). Model 2 services **$30k–$120k** (was
$40k–$150k; 4–8 fixed-scope readouts at $3.5k–$10k plus 1–2 post-incident at $12k–$30k).
Model 3 **$0–$50k**, binary (unchanged; targets Drata, Schellman/AIUC, Vanta).
Total **$30k–$190k** (was $40k–$260k).

Recommendation (revised): keep services-first for the GRC buyer, but drop the regulatory
story — sell **audit-period evidence** (SOC 2 CC8.1, AIUC-1 E015/E004), and make the
first instrument a free **cross-harness decision normaliser**: Claude OTel `tool_decision`
+ Codex Compliance API + the plugin ledger → one record carrying the field the vendor's
event explicitly lacks (matched rule, policy version). Apply to Drata's early-access
program as a prospect to read the Chain of Custody record ($0). Gate unchanged.

Two assumptions whose failure flips this:
- **A1 — SOC 2 auditors now sample AI-agent changes and ask for approval evidence.** If
  false, Market C has no 2026 trigger and Model 1b (AIUC-1) is the only dated buyer. Test
  in two weeks, < $500: email five audit firms (Prescient, Johanson, Schellman, A-LIGN,
  Sensiba) and two GRC communities with one question — "in audits closed this year, did
  you request evidence for AI-coding-tool changes under CC8.1?"; buy one auditor hour
  (~$300) if replies are thin. Pass: ≥ 2 of 5 say yes with a specific request.
- **A2 — An org with Claude Code Enterprise + OTel cannot already satisfy that ask.** If
  the raw `tool_decision` stream passes an auditor, the normaliser is a free feature and
  Model 1 is $0. Test: two days to convert own OTel/ledger data into a one-page CC8.1
  evidence pack; send to three early-access security leads and one auditor with "would
  this pass; what would you pay"; cost $0–$200. Pass: ≥ 1 auditor says the matched-rule
  and cross-harness columns are needed, ≥ 1 lead names a price ≥ $2k.

## Iteration 3 — 2026-10-13 04:11 PDT

Research log unchanged since c694d57 (23:57 PDT). Search quota was exhausted (200/200), so
everything below is a direct page fetch tonight; the fetch tool summarises, so quotes are
short. Five cost pages (Vanta, Secureframe, Sprinto, A-LIGN, Thoropass) returned 404/500;
AICPA's AI page did not render; Shared Assessments is a login wall. Marked UNCHECKED, not filled.

### (a) Re-test of Iteration 2

| # | claim | ruling | reason |
|---|---|---|---|
| 1 | Drata AI Agent Governance: LA, Anthropic-first, Trust Ladder, Chain of Custody | **KEPT** | still "Limited Availability" / "Apply for Early Access", built "with select enterprises across financial services, healthcare, and software"; only Anthropic named — no Claude Code, Codex, Copilot; no price, date or customer [MEASURED, drata.com/products/agent-governance]; blog index to 2026-10-08 has no launch post [MEASURED]. GA date ASSUMED unknown. "Only 11% of vendors can substantively prove an audit trail for AI agent decisions" is a vendor survey, method unseen [PLAUSIBLE]. |
| 2 | AIUC-1 E015/E004 = dated recurring trigger | **KEPT; necessity REVISED down** | E015 (Mandatory, every 12 months) "should include" only input/output logging plus storage with retention — Datadog/Splunk settings pass; the agent record ("approval records with approver identity, timestamp, decision outcome", tool-call entries, tamper-evident hashing: E015.2/E015.4) is **"may include"** [MEASURED, standard.aiuc-1.com]. E004.1 accepts "approval logs from Jira/Linear/GitHub" [MEASURED]. The per-decision record is the strongest form, not a required one. |
| 3 | AIUC-1 mechanics | **KEPT; market sized** | Schellman "first accredited auditor" (2026-02-03), Coalfire named; 48-h gap assessment, 1,000–5,000 red-team scenarios run by AIUC, quarterly retests, certificate 1 year, "50+ page" report, "4–8 weeks" [MEASURED, aiuc-1.com; Schellman 2026-04-29]. **Cost unpublished** on aiuc-1.com, aiuc.com, schellman.com [MEASURED absence]. Certified: 7 named (Cursor, Fin, ElevenLabs, Harvey, UiPath 2026-03-09, KPMG, Sierra 2026-09-17) + MSCI "adopted"; AIUC raised $55M [MEASURED]. Model 1b is a channel via Schellman/AIUC, not a market. |
| 4 | Harness event lacks matched rule + policy version | **KEPT; one correction** | monitoring-usage still says "The event doesn't indicate which of these sources matched"; CHANGELOG to 2.1.296 has no matched-rule entry [MEASURED]. Correction: `managed_settings_resolved` (v2.1.274+) carries a SHA-256 digest of managed settings — a session-level **policy version exists**; the gap is the per-decision rule and the join [MEASURED]. |
| 5 | A2: Enterprise + OTel cannot already satisfy the ask | **KEPT; sharpened** | Claude Enterprise Compliance API returns Claude Code transcripts (`tool_use`/`tool_result`), retained **6 years**, but they "show what Claude was asked to do and what it returned, not what happened on the device" — no decision, mode or rule [MEASURED, compliance-sessions]. Codex Compliance API: `conversation_message`/`codex_log`; "Policy API support does not change Compliance API event coverage" [MEASURED]. Copilot enterprise audit log: 18 `copilot.*` events, all seat/settings [MEASURED]. Cursor Enterprise audit logs exclude agent actions; those go via OTel "Action Recording" [MEASURED]. No vendor's compliance surface carries decision + why-permitted + policy version. |
| 6–9 | Vanta waitlist; $2k–$8k slot; services $3.5k–$10k / $12k–$30k; regulatory triggers dropped | **KEPT** | Vanta still "Join the waitlist", no coding assistant named [MEASURED]; no new price comparables. |

### (b) Demand side: is anyone paying for agent-governance evidence now?

Plainly: thin. No job posting, RFP or audit program I could read names evidence of
coding-agent permission decisions. What exists sits one level up.

1. **ISACA Now, 2026-08-24** (Nik Kale): "What is the minimum evidence required to approve a production AI agent?"; logs must capture tool invocations and all write actions; "preserve evidence at each action boundary" [MEASURED]. ISACA Journal 2026 vol. 4, "Addressing Control Gaps in Agentic AI Governance" [MEASURED title]. The AI Audit Toolkit has no agent program [MEASURED].
2. **AICPA**: no SOC 2 AI/agent guidance verified. UNCHECKED; A1 untested.
3. **AIUC-1 Q4 (2026-10-08)**: the only standard text found naming "approval records with approver identity, timestamp, decision outcome" for agents (E015.2) [MEASURED]. Buyer: agent vendors; 7 certified.
4. **UiPath, 2026-03-09**: >2,000 scenarios, quarterly re-evaluation, Schellman; no customer or procurement quote [MEASURED].
5. **Cursor security page, 2026-08-25**: AIUC-1, ISO 42001, SOC 2 Type II; reports at trust.cursor.com [MEASURED]. Vendor-side proof; buyer demand inferred.
6. **Procurement**: aiuc-1.com says Salesforce "integrated AIUC-1 into vendor assessments" [PLAUSIBLE, vendor claim]; the only procurement-side statement found.
7. **CSA AICM v1.1, 2026-06-22**: 247 controls / 18 domains, AI-CAIQ for vendor evaluation, AIUC-1 mapping; STAR for AI Level 2 "Now Available" (ISO 42001 + Valid-AI-ted AI-CAIQ). "Agent" appears on neither page; no submission count [MEASURED]. SIG 2026: UNCHECKED.
8. **Job postings** (LinkedIn, 2026-10-13; "AI-powered search", totals inflated): MetLife "AVP, Audit – Data, AI Risk, Gov & Tools" (3 h); AKASA "Director, Compliance & AI Governance" (2 d); Vanguard "AI Validation, Senior Specialist" ×2 (3 d); USAA "Director Internal Audit – Data, Information & AI Infrastructure" (3 d); Anthropic "Security Risk & Compliance, Agent Security" (1 w) [MEASURED]. Financial-services buyers hire AI-audit heads; none names coding-agent evidence.
9. **Drata, 2026-08-14**: "71% responded that AI tools led to a failed audit or lapsed standard" — blog index only; post 404s [PLAUSIBLE].
10. **Vanta customer stories**: Synthesia ISO 42001 "with a two-person team"; none mentions agents [MEASURED].
11. **Service prices**: no consultancy publishes an "AI governance readiness" price. Anchors stay Strike Graph $4k–$8k/yr assessments, $2k–$8k/yr added framework (It-2); ISO 27001 audit $8k–$30k, surveillance $8k–$15k/yr [MEASURED, isms.online 2025-07-25]; Cobalt $3.5k.

Verdict: demand for **AI-governance headcount** and **agent-vendor certification** is MEASURED;
demand for **coding-agent decision evidence** is ASSUMED. The gap in vendors' compliance
surfaces (row 5) is MEASURED, but a gap is not a budget line.

### (c) What makes services-first obsolete within 6 months

1. Drata GA with Claude Code + Codex sensors and an exportable Chain of Custody: the pack becomes a feature of a $20k+ ACV platform; services survive only as implementation help, which Drata partners already sell.
2. Anthropic adds a matched-rule field to `tool_decision` and documents the join to the `managed_settings_resolved` digest: the single-harness normaliser becomes a jq script.
3. Codex Compliance API gains approval events, or Copilot adds agent-action events: cross-harness normalisation becomes a SIEM join.
4. AIUC-1 Q1-2027 (~2027-01-15 from the Jan 15 / Apr 15 / Jul 14 / Oct 8 cadence) moves E015.2 from "may" to "should": demand rises, but Drata is pre-mapped to AIUC-1.
5. Vanta GA naming coding assistants.

| cadence | page | watch for |
|---|---|---|
| weekly | https://drata.com/products/agent-governance ; https://drata.com/blog | LA badge gone; names beyond Anthropic; launch post |
| weekly | https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md | `tool_decision` + rule/matched (now 2.1.296) |
| monthly | https://code.claude.com/docs/en/monitoring-usage | "The event doesn't indicate which of these sources matched" |
| monthly | https://platform.claude.com/docs/en/manage-claude/compliance-sessions | "not what happened on the device" |
| monthly | https://learn.chatgpt.com/docs/enterprise/compliance-api ; https://learn.chatgpt.com/docs/changelog | approval/decision events (newest 2026-10-08) |
| monthly | https://www.vanta.com/products/ai-governance | "Join the waitlist" gone |
| monthly | https://docs.github.com/en/enterprise-cloud@latest/admin/monitoring-activity-in-your-enterprise/reviewing-audit-logs-for-your-enterprise/audit-log-events-for-your-enterprise ; https://cursor.com/docs/enterprise/compliance-and-monitoring | agent-action events |
| ~2027-01-15 | https://www.aiuc-1.com/ ; https://standard.aiuc-1.com/accountability/log-model-activity | "Last Updated"; E015.2 should/may |
| by 2026-12-31 | https://cloudsecurityalliance.org/star/ai | agent questions in AI-CAIQ |

### (d) Revenue and recommendation

12-month [ASSUMED]: Model 1 **$0–$30k** (was $0–$40k; gap confirmed, Drata window compressing);
Model 1b **$0–$20k** (was $0–$30k; 7 vendors, E015.2 optional); Model 2 **$30k–$100k**
(was $30k–$120k; no published readiness price, so sell at pentest tiers $3.5k / $10k / $20k,
post-incident $12k–$30k); Model 3 **$0–$50k**, binary. Total **$30k–$150k** (was
$30k–$190k); median ≈ $50k — most likely 2–4 paid readouts and no pack licences unless A1 passes.

Recommendation: shape unchanged — services first, the cross-harness normaliser as the free
instrument — but the demand evidence does not justify building the pack ahead of a paying
customer. Build the normaliser (it is also the A2 test), sell readouts, treat A1 as the gate;
2027-03-31 three-engagement gate stands.

First five working days (Tue 2026-10-14 → Mon 10-20), ≈ 4.5 builder-days, cash ≤ $300:

| day | step | cost | decision informed |
|---|---|---|---|
| 1 | A1 emails to Prescient, Johanson, Schellman, A-LIGN, Sensiba + ISACA author Kale: "in audits closed this year, did you request evidence for AI-coding-tool changes under CC8.1?" | $0, 2 h | Market C has a 2026 trigger (pass ≥ 2/5 yes with a specific request) |
| 1 | apply to Drata early access as a prospect; ask for the Chain of Custody schema and GA date | $0, 1 h | whether Model 1 has a 6-month window |
| 2–3 | normaliser v0: OTel `tool_decision` + `managed_settings_resolved` digest + plugin ledger → one JSONL record with `matched_rule` reconstructed by local replay of the resolved settings → one-page CC8.1/E015.2 pack from own data | $0, 2 d | A2; whether the matched rule can be reconstructed locally at all |
| 4 | pack to 3 early-access leads + 1 auditor hour: "would this pass; what would you pay" | $0–$300, ½ d | price slot $2k–$8k vs $0 |
| 5 | readout offer at $3.5k / $10k / $20k to the datapce.com list; count replies 2 weeks | $0, ½ d | first Model 2 booking by 2026-11-30, the earliest read on the 2027-03-31 gate |
