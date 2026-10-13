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
