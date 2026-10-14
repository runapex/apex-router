# datapce multi-loop programme — charter (2026-10-13)

Drafted by six role agents (dist-significant, dist-mean, ml-significant, ml-mean, bi-demand, bi-landscape) and reconciled by an orchestrator agent; validation roles added by the session lead. Status: DRAFT awaiting owner approval; the loops do not run until the open questions below are answered.

## Shared target

ONE TARGET: a decision that a human can ratify as a RULE (policy-shaped), computed by datapce from data the buyer already has (logs, scores, counts, timings, metadata; never prompt/response/message text; datapce never inline in the customer's data path), where machine-proposes/human-ratifies with a receipt changes an outcome someone already pays for. Three tests, all must pass at cycle 3 with MEASURED numbers.
VALUE (either lens form): SIGNIFICANT lens = one wrong instance >= $25k direct cost, SLA credit, or breach cost, OR >= 500 GPU-hours, OR >= 10 engineer-days, OR a named regulatory clause with a dated enforcement entry (>= 10 entries in 24 months in a public database); >= 3 independent public instances in 24 months, each naming the rule that would have changed the outcome. MEAN lens = frequency x per-instance cost >= $10k/yr avoidable per 50-practitioner org with a 95% CI, per-instance >= 1 practitioner-hour or >= $50 compute, and a pain ratio (hand-set value demonstrably wrong) with Wilson lower bound > 5%. BI form = >= 2 MEASURED price anchors from different vendors, list price >= $5k/yr/org (or >= $10/seat/mo with >= 1,000 customers), and a one-builder attainable slice >= $100k in 12 months at <= 1% share through a procurement path that needs no seller SOC 2.
DEMAND: SIGNIFICANT = exposure: >= 500 orgs exposed for GO (>= 200 to keep past cycle 1), recurrence >= 1 instance/org/year (>= 1/quarter scores demand 3). MEAN = >= weekly per team in >= 30% of teams on a mainstream stack (K8s, Postgres/MySQL, Kafka, Prometheus, GitHub Actions, Airflow/dbt; vLLM/litellm/lm-eval/promptfoo/langfuse/MLflow/W&B), from two independent source types (survey/corpus + script/API count). Every candidate in any lens needs >= 1 demand signal independent of the instances and of vendor marketing: >= 50 issue reactions, >= 20 job postings/90 days naming the problem, a dated regulation/standard clause, >= 5 RFP/questionnaire/insurer templates, or an RFC/KIP.
DATAPCE FIT (pass/fail floors, not scored until BI correlation): (1) a rule a human ratifies, not pure judgment or pure automation; (2) computable from text-free data with the stdlib core (Wilson, BH, paired bootstrap, CUSUM, mSPRT/e-values) and the math loop has not flagged the method invalid for that data; (3) data source reachable READ-ONLY (query-history table, audit-log export, metrics API, CI run history, telemetry) without datapce sitting in the data path and without a DB/platform-vendor partnership; (4) oracle-available: the counterfactual is computable from that log alone; (5) the platform owner holding the data has not shipped or announced propose+ratify+evidence for that decision in the last 6 months (dated page); (6) spike in <= 10 builder-days, adapter in weeks, one builder. 'Fits today's proxy/plugin' is a tie-breaker in cycle 3 only.
UNITS: USD/instance; instances/org/year; orgs exposed; occurrences/team-week; USD/yr/org or USD/seat/month list price; practitioner-hours; GPU-hours; effect size with 95% CI and n; days-to-min-n (MIN_N = 30); builder-days. LABELS: MEASURED (page read in full or data computed, URL+date, artefact archived under /tmp/<role>/), PLAUSIBLE (search digest, secondary), ASSUMED (own arithmetic), SYNTHETIC, UNSOURCED (dropped on sight); checks use the nine evidence labels (PASS/FAIL/PARTIAL/BLOCKED/INCONCLUSIVE/STALE/ASSUMED/N/A/WAIVED). Only MEASURED is load-bearing at cycle 3. RUBRIC: prior 0-3 x5 (demand, value, fit, differentiation, feasibility); floors fit >= 1 and feasibility >= 1; SIGNIFICANT loops weight value x2, MEAN loops weight demand x2, BI scores demand+value only with fit blank until correlation; differentiation may not exceed 1 without a gap quoted from the incumbent's own page.
OVERRULED: dist-significant's $100k floor lowered to $25k (so the two significant lenses compare; $100k = value 3); dist-significant's 1,000-org exposure and ml-significant's 200 lowered/raised to 500 for GO; ml-significant's 1/org/quarter recurrence relaxed to 1/year as the keep floor; BI-demand's $25k/yr/org and BI-landscape's $5k/yr reconciled to $5k floor plus the $100k attainable test; ml-mean's 'weekly per practitioner' relaxed to 'weekly per team'; per the 10-13 correlation, 'free OSS exists' is a price-ceiling input not a kill, and feature-sized candidates are acceptable and ranked on which funds the next cycle; dist-mean's K6 (zero-dollar mass demand) is an OSS-asset cap, not a kill.

## Cycle 1 — BREADTH. Fill one shared candidate table with stable IDs across four lenses; every row carries at least one MEASURED number; pick top 3 per research loop and top 6 per BI loop; pre-register kill criteria before any depth work.

### dist-significant
- **Looking for:** 8-12 policy-shaped decision points in storage/streaming/databases/pipelines/multi-tenant/DR/residency where one wrong instance >= $25k or a named regulatory clause; outside the three dead clusters and pseudonymisation; C-shaped rows labelled, not scored as wins.
- **Deliverable:** Ranked table (ID, decision, who takes it, $/instance with dated primary source, incumbent + whether it ships replay/confidence-predicate/evidence, policy-shaped? naming the rule, data source reachable read-only?); top 3 with one-line reason and a pre-registered kill criterion each.
- **Evidence bar:** Each kept seed: one MEASURED instance (cost, date, source) and one incumbent gap verified on a docs page read in full; else dropped with reason. Fit >= 1, feasibility >= 1.

### dist-mean
- **Looking for:** >= 12 recurring hand-set numeric-policy classes (retries, timeouts, alert thresholds, rate limits, autoscaling targets, pool sizes, flaky-test quarantine, batch timeouts, freshness SLAs, consumer lag, canary bake) across mainstream stacks.
- **Deliverable:** Ranked table (ID, stack, decisions/team-week, knob prevalence from >= 200 sampled repos by script, SO/GitHub question volume, incident share with Wilson CI, incumbent closure 4-bit vector propose/ratify/evidence/replay with price, oracle Y/N, text-needed Y/N); shortlist of 3 with thesis and pre-registered kill criterion.
- **Evidence bar:** Every row: >= 1 MEASURED frequency from a script/API/PDF read directly; incumbent column from a page read directly; artefacts under /tmp/dist-mean/. Search-digest rows are PLAUSIBLE and cannot enter the shortlist.

### ml-significant
- **Looking for:** 12-16 rare-but-costly ML/LLM decisions made with wrong or no statistics (model migration, eval noise, judge drift, long-horizon agent failure, early stopping, private-eval contamination, silent vendor changes, advise-to-enforce guarantees), scored with 'fits today's code' excluded.
- **Deliverable:** Ranked table (ID, decision owner title, recurrence/org/quarter, $/instance bound with arithmetic and label, incumbents with page URL read, rubric score, MEAN-overlap flag); top 4 with a falsifiable claim and pre-registered kill criterion each.
- **Evidence bar:** >= 1 MEASURED source per row and a bounded $/instance; kill criteria written before any measurement; overlaps with ml-mean flagged for adjudication.

### ml-mean
- **Looking for:** 15-25 weekly small-n statistical judgements the median prompt engineer / fine-tuner / eval owner / API app dev makes by eyeballing; >= 50% of rows must not touch the proxy or routing.
- **Deliverable:** Ranked table (ID, one-sentence problem in the practitioner's words, occurrences/practitioner-week, >= 1 MEASURED demand signal with query string and date — GitHub API normalised by stars, pypistats, HN Algolia, survey — incumbents, text-free feasibility flag, n = 1 local count labelled).
- **Evidence bar:** >= 15 rows with >= 1 MEASURED signal; top 5 with >= 2 independent signal types; raw API JSON cached under /tmp/ml-mean/; arXiv counts admitted for feasibility only, never demand.

### bi-demand
- **Looking for:** 15-25 existing budget lines adjacent to Policy/Capacity/Evidence across both lenses, demand-first, fit column blank; prior markets A-F as ordinary rows; >= 8 rows new.
- **Deliverable:** Demand ledger (ID, budget line, buyer title, budget owner, price anchor URL+date+label, count proxy from a different source, count x price bottom-up, trigger event); top 6 by count x price with one-line reason.
- **Evidence bar:** Every row: >= 1 MEASURED price anchor and >= 1 count proxy from a different source family, or marked UNMEASURABLE with reason; >= 5 rows per lens; pages cached under /tmp/bi-demand/1/.

### bi-landscape
- **Looking for:** >= 25 vendors across >= 8 segments outside A-F selling policy/governance/evidence/capacity/cost to the same personas; channels and one-builder admissibility; survivorship control per segment.
- **Deliverable:** Landscape table (ID, segment, vendor, persona, price anchor, funding/owner, GA status + named harnesses, documented gap quoted from own docs with URL, channels, one-builder admissible y/n + why); blind-scored shortlist of 3 segments scored before re-reading C material; dead/pivoted/absorbed vendors per segment.
- **Evidence bar:** Each seed: MEASURED price anchor or MEASURED absence of public pricing, a named payer with budget authority, >= 1 incumbent read on its primary page; no row rests only on a search digest.

**Orchestrator:** Before start: issue the shared taxonomy with stable IDs (D-xx distributed, M-xx ML, B-xx budget line, L-xx segment), the label vocabulary, the hygiene list (private project name = 'the POC'; no personal paths), per-loop budgets (web search: 25 calls per research loop in cycle 1, 25/40/15 per BI loop across cycles; GitHub API token; /tmp/<role>/ archive dirs; no repo code), and the pseudonymisation rejection reason ('not a good fit', content-handling in the data path is out) so S3/S4-shaped seeds are shaped or dropped early. BI loops are sealed from researchers and from each other. At end: (1) merge the four tables on IDs; (2) adjudicate significant-vs-mean overlaps by frequency (>= weekly per team goes to MEAN); (3) hand dist-mean frequencies to dist-significant and ml-mean recurrence to ml-significant; (4) label every C-shaped seed and route it to a dedicated C refutation track instead of scoring it; (5) run verify-claims on every number entering a shortlist; (6) lock the pre-registered kill criteria in the log (I hold them; they cannot change after data is seen); (7) ask the math loop for the one-page method-validity note (autocorrelated/non-stationary series, multiple comparisons, sample sizes in Redset/Snowset/cluster traces) due at cycle 2 start.

**Exit:** Merged table exists with every shortlisted row having >= 1 MEASURED number and a locked kill criterion; 3 candidates per research loop and 6 per BI loop named; overlaps adjudicated; C-shaped rows routed; zero UNSOURCED numbers; owner's cycle-0 questions answered or marked BLOCKED with the default I applied.

## Cycle 2 — DEPTH + EVIDENCE. One evidence card or dossier per shortlisted candidate, built from pages read in full and read-only probes/replays over public traces or own telemetry; incumbent closure dated; first adversarial refutation; BI ranks blind.

### dist-significant
- **Looking for:** Per top-3: three independent instances in 24 months with $ or clause; incumbent docs read end-to-end; a read-only replay over Redset/Snowset/Google/Alibaba traces or own telemetry reporting benefit vs false positives with Wilson CIs; a buyer sentence (title, install point, log read, who signs).
- **Deliverable:** Dossier <= 2 pages per candidate: instances table, gap quotes with URL+date, /tmp probe path + numbers with CIs labelled 'shape evidence', buyer sentence, verdict STANDS/WEAKENED/REFUTED, request list (external contact, expert, measurement agent).
- **Evidence bar:** Each candidate has a verdict from own evidence, a probe that ran to completion or BLOCKED with reason, and the cycle-3 kill thresholds fixed; load-bearing numbers MEASURED or the candidate is downgraded.

### dist-mean
- **Looking for:** Per top-3: pain ratio (hand-set value outside the data-supported interval) with Wilson CI and n >= 100; per-instance cost; one replay on a public trace (Borg 2019, Alibaba, Azure VM, Loghub, GitHub Actions runs) plus local telemetry as corroboration; incumbent closure re-tested on dated changelogs for the last 6 months.
- **Deliverable:** Evidence card per candidate: pain ratio + CI + n, cost range + source, replay card (verdict, rate, ci_low, ci_high, n, p, days_to_min_n) vs hand-set baseline, dated closure quotes, oracle verdict, $/yr per 50-engineer org with arithmetic, kill criterion restated.
- **Evidence bar:** >= 3 MEASURED entries and 0 UNSOURCED per card; replay script + inputs archived and rerunnable under /tmp/dist-mean/<class>/; n >= 30; CI versus baseline reported even if overlapping.

### ml-significant
- **Looking for:** Per top-3: a pre-registered measurement on existing data (own telemetry ~112k requests as n = 1; public leaderboard histories; a public eval set) showing the method flips the decision vs the naive baseline with an effect size and CI; incumbent gap quoted; one quantified public cost instance; one fresh adversarial refutation.
- **Deliverable:** Evidence card per candidate: claim, method, data, effect size + CI or INCONCLUSIVE with n needed, incumbents + quoted gap with date, cost instance, refutation verdict, labels on every number; scripts in /tmp read-only; ranked shortlist <= 2.
- **Evidence bar:** Effect CI excludes zero or INCONCLUSIVE with an n reachable in 14 days; every cited number fact-checked PASS or dropped; a local effect must replicate on a second source or stays INCONCLUSIVE.

### ml-mean
- **Looking for:** Per top-3: incumbent decision-coverage matrix (metric vs VERDICT shipped, free/paid, from docs read in full) for promptfoo/inspect/langfuse/braintrust/MLflow/W&B/Evidently; one local replay of a stdlib sequential gate over real score tables that flips a decision; the seed-to-seed variance measurement (spawned agent); cost per wrong call.
- **Deliverable:** Three evidence cards: demand (2+ dated source types), gap verdict (served free / served paid / unserved), replay n + effect, cost per wrong call, text-free PASS/FAIL, kill criteria pre-registered for cycle 3.
- **Evidence bar:** Every field filled with a label; replays reproducible from /tmp/ml-mean/; a shipped metric alone does not kill, a shipped free verdict does; n = 1 local data cannot carry a card alone.

### bi-demand
- **Looking for:** Per top-6: triangulation from three source families (pricing incl. Wayback 12/24-month history; hiring postings in last 90 days; procurement/marketplace/filings), procurement path, erosion trend, bundling by a platform owner (dated), segment where a one-builder local-first vendor can transact.
- **Deliverable:** Six demand cards: serviceable count low/base/high with derivation, price band with labels, procurement friction score, bundling risk (who, dated), 12-month attainable revenue at an explicit share, the single most load-bearing ASSUMED number; ranked by attainable revenue with confidence, handed in sealed.
- **Evidence bar:** >= 2 MEASURED price points and >= 1 MEASURED count from an independent source per card; a card is not ranked until three families are present; vendor case studies are PLAUSIBLE only.

### bi-landscape
- **Looking for:** Per 3 shortlisted segments: demand signals independent of vendor existence (postings, RFP/questionnaire/insurer templates, issue reactions, conference sessions); exact gap on official docs with pre-emption date estimate; channel entry evidence; price ceiling from >= 2 comparables; procurement walls.
- **Deliverable:** Three dossiers: demand counts + sources, gap quote + URL + date, pre-emption estimate with evidence, channel entry checklist, price band with two anchors, procurement-wall list, a designed kill test (<= $500, <= 14 days, pass/fail thresholds).
- **Evidence bar:** >= 2 MEASURED sources each for budget-exists, gap-exists, channel-exists, one from a non-vendor party; every number fact-checked on its primary page or labelled PLAUSIBLE inline.

**Orchestrator:** At start: deliver the math loop's method-validity note to both dist loops; confirm read-only /tmp scripts over ~/.apex telemetry are permitted (text never read); spawn the ml-mean seed-variance measurement agent (small open-weight model, 5 seeds, one public benchmark) and, if any research loop is quota- or time-limited, one measurement agent per public-trace replay (Redset/Snowset/Borg/Alibaba). At end: (1) spawn one fresh heavy-tier adversarial reviewer per evidence card, fed the card only (no ranking), plus one cross-family fact-checker for every BI price/funding/date figure; (2) run verify-claims on every number in every card; (3) downgrade any candidate whose load-bearing number is PLAUSIBLE; (4) collect the external-conversation requests (data-platform lead, multi-tenant DBA/SRE, ML practitioner who ran a migration or eval-driven ship, buyer personas) into ONE unified owner request with scripts, merged with BI's A1/A2/A3; (5) correlate the two sealed BI rankings with each other per persona (not per market) and hold the result sealed from researchers until cycle 3 end; (6) record each pre-registered kill criterion verbatim next to its card; (7) re-check every candidate against the six fit floors and the rejected-proxy pattern.

**Exit:** Every shortlisted candidate has a card/dossier with a verdict label, an executed-or-BLOCKED probe, fixed cycle-3 kill thresholds, and a first adversarial verdict recorded; BI rankings handed in sealed; the owner request list is sent; no PLAUSIBLE number is load-bearing in any surviving card.

## Cycle 3 — KILL TESTS + DECISION. Execute pre-registered kills, cross-family refutations, external conversations where permitted, vendor pre-emption re-reads dated to the day; then correlate research with BI once and produce one owner-facing recommendation with a 14-day falsifier.

### dist-significant
- **Looking for:** Kill tests on the strongest candidate: cross-family adversarial refutation on its own terms; one external conversation (data-platform lead or DBA/auditor) via the owner; base-rate check against an enforcement database or status-page history so postmortem instances are not survivorship; vendor changelog/roadmap re-read on the day.
- **Deliverable:** Statement v1: problem, buyer, $/instance MEASURED, incumbent gap MEASURED+dated, probe result, what the refutation weakened, confidence, next 14-day experiment with kill criterion — or 'no significant candidate in this lens' with the strongest near-miss and why it died.
- **Evidence bar:** Every kill test executed or BLOCKED with reason; refutation verdict verbatim; nothing rests on an ASSUMED number without saying so; a candidate liked by only one model family is reported as contested.

### dist-mean
- **Looking for:** Execute K1-K8 on survivors; one Claude-family and one cross-family (codex exec) refutation from the cards only; reconcile with the BI price anchors and buyer counts the orchestrator releases; decide GO / NO-GO / OSS-asset-only.
- **Deliverable:** Decision record: candidate, verdict, which kills fired, reviewer verdicts per clause (CONFIRMED/WEAKENED/REFUTED), strongest counter-evidence, two-week spike (data source, metric, pass and kill thresholds, cost), claims still PLAUSIBLE/ASSUMED.
- **Evidence bar:** Exactly one GO or every candidate carries a kill reason; no claim rests on a search digest; the spike names an external data source or user.

### ml-significant
- **Looking for:** Pre-registered kills on <= 2 survivors: vendor pre-emption (changelog read; does the platform owner hold the data?), recurrence floor, nameable buyer role, data-access feasibility for one builder, one external practitioner conversation via the owner; reconcile with ml-mean frequencies and BI WTP anchors.
- **Deliverable:** Decision record: one survivor with an RSI statement (variant chosen, why, next move, 14-day killer) or 'no survivor' with the kill that fired per candidate; plus the engine-shaped residual (exact computation: inputs, test, thresholds, verdict-card fields, ratification step).
- **Evidence bar:** Every kill criterion labelled PASS/FAIL/INCONCLUSIVE with evidence; conversation happened or BLOCKED; recommendation names owner title, $/instance, recurrence, 14-day falsifier; cross-family read recorded.

### ml-mean
- **Looking for:** Execute pre-registered kills; cross-family refutation per survivor; demand holds on a second population (not this machine, not one repo); the hybrid propose/ratify shape tested against a one-off script as the null (do closed issue threads end with a snippet?); overlap check with ml-significant and dist loops.
- **Deliverable:** Decision record: 0 or 1 candidate with form (plugin/proxy feature, OSS library, standalone), first measurement of a 2-week spike, and explicit list of still-ASSUMED evidence.
- **Evidence bar:** No INCONCLUSIVE left unexplained; refutation verbatim; no candidate whose demand rests on a single source or on n = 1 telemetry.

### bi-demand
- **Looking for:** Per top 2-3 cards: platform-bundling check (dated changelog), Wayback price-erosion diff, budget-owner existence (titles in last 90 days), a buyer-contact script and target list for the OWNER to execute, unified with the prior A1/A2/A3 tests.
- **Deliverable:** Verdict table with kill-test labels, surviving demand estimate, recommended owner tests (< $500, 14 days, pass/flip criteria), bottom-up ranking with kill flags, the one number that flips the ranking, a watchlist with cadence.
- **Evidence bar:** Each survivor passed all four kills or carries a named dated open flag; >= 1 candidate explicitly killed with evidence; 'no buyer contacted' stated if true.

### bi-landscape
- **Looking for:** Run (not design) the cheapest external kill test for the top 2 segments where contact is permitted; re-fetch the pre-emption watchlist; one fresh adversarial refutation on channel and procurement grounds from a reviewer who has not read cycles 1-2.
- **Deliverable:** Ranked decision: one segment + buyer + channel + price band + first-10-customers route + 12-month revenue range (labelled), kill results PASS/FAIL/INCONCLUSIVE/BLOCKED, flip conditions within 90 days, standing watchlist.
- **Evidence bar:** A kill test executed per top-2 or BLOCKED with the reason; refutation verdict recorded; a designed-but-unrun test is never implied PASS.

**Orchestrator:** At start: release BI's WTP anchors and buyer counts to researchers and researchers' per-instance values to BI, joined on IDs; arrange cross-family reviewers (codex exec) for every survivor and a fresh reviewer for the C-shaped track; relay owner permission status for each external conversation. At end: (1) the C refutation: one fresh reviewer refutes 'compliance-evidence readouts' on its own terms using BI-demand, BI-landscape and any C-shaped research seeds, before the final ranking; (2) correlate once: per candidate, researcher value x BI count x price; a disagreement > 3x triggers a measurement spawn, never an average; (3) rank survivors across lenses on the shared rubric with lens weights, fit now scored; (4) write the morning report: one recommended direction (or 'no survivor' + engine-shaped residual), confidence, the single cheapest 14-day experiment with pass/kill thresholds, the unified owner test set (A1/A2/A3 + new), the watchlist, and the honesty note (search quota, model families used, who was contacted); (5) verify-claims on every number in the report; hygiene grep before handoff.

**Exit:** Every candidate has GO, NO-GO, OSS-asset, or CONTESTED with the family split named; the C refutation verdict is recorded; the recommendation carries a confidence level, one 14-day experiment, and lists every ASSUMED number it rests on; the owner receives one test set, not six.

## Kill rules

- K-CLOSURE: the platform/vendor holding the data GA-shipped (or shipped in the last 6 months, or announced with a date) propose+ratify+evidence or replay-against-own-history for that exact decision, verified on its own docs/changelog with URL and date. 'Free OSS exists' is NOT a kill; it is a price-ceiling input.
- K-SHAPE: the decision is not policy-shaped (no rule a human could ratify: pure judgment or pure automation), or the practitioner wants a number not a verdict (closed issue threads end with a snippet and askers do not recur).
- K-TEXT: the predicate needs prompt/response/message/code content; or its inputs are reachable only by sitting inline in customer data (the rejected-proxy pattern) or via a DB/platform-vendor partnership.
- K-ORACLE: the counterfactual is not computable from the log alone (the v4 fidelity lesson); such classes are capped at OSS-asset and cannot reach GO.
- K-VALUE: SIGNIFICANT lens $/instance cannot be bounded above $10k from a MEASURED source AND the MEAN loop reports frequency < weekly; MEAN lens no paid incumbent, no priced incident class, no >= 2k-star OSS tool (cap at OSS-asset, not kill); BI price ceiling < $2k/yr with no services path.
- K-DEMAND: SIGNIFICANT exposure < 200 orgs or recurrence < 1/org/year; MEAN frequency below weekly per team after the second source, or pain-ratio Wilson CI includes 5%; BI: no pricing page, no job title, no procurement record ('should exist' is not demand), or the count rests on a single PLAUSIBLE source after two attempts.
- K-PROBE: the replay/measurement benefit CI includes zero on the public trace or second population, or MIN_N (30) is unreachable in 14 days at realistic per-team volume (days_to_min_n > 14), or the pre-registered criterion fails twice.
- K-REFUTED: a fresh adversarial reviewer returns REFUTED on the load-bearing clause and the loop cannot re-measure within the cycle; a candidate supported by only one model family is CONTESTED, never a survivor.
- K-LABEL: a candidate whose load-bearing number is PLAUSIBLE or ASSUMED at the end of cycle 2 is downgraded; INCONCLUSIVE at cycle 3 counts as a kill unless the orchestrator funds a measurement; UNSOURCED numbers are dropped on sight.
- K-PROCURE (BI): every buyer in the segment demands the seller's own SOC 2/DPA before a PO with no referral partner to carry paper; sales cycle > 6 months by comparables; price erosion > 50% over 24 months on Wayback; no named persona with budget authority.
- K-C-SHAPED: a seed that is compliance evidence under another name (tenant-isolation proof, erasure proof, residency evidence, recovery assurance, CMMC, insurer attestation, questionnaire packs, SOX/CC8.1 change evidence) is not scored as an independent win; it is routed to the single C refutation in cycle 3 and lives or dies with it.
- K-PREREG: a kill criterion or measurement threshold changed after data was seen voids that candidate's cycle; the orchestrator holds the pre-registration.

## Reconciliation rules

- Significant-vs-mean overlap: the MEAN loop owns a decision class when it recurs >= weekly per team; the SIGNIFICANT loop owns it when the per-instance floor is met and frequency is below weekly; a seed meeting both (eval noise, judge drift, tool-call reliability) is split by instance cost: >= $25k instances to SIGNIFICANT, the rest to MEAN; the orchestrator adjudicates at cycle 1 end and records the ruling.
- Cross-loop overlap (tool-call reliability ledger raised by ml-mean and dist-mean; silent-change detection raised by ml-significant and dist-mean alerting): one owner loop, the other supplies its evidence as an annex; the owner loop is the one whose public trace can measure it.
- Research vs BI: BI loops never see researcher output or each other until cycle 3 end; correlation is per candidate ID and per persona; researcher value per instance and BI count x price are compared on the same ID; a disagreement > 3x spawns a measurement or expert agent, never an average; a researcher candidate in a segment BI did not map gets one BI-landscape cycle on it before ranking.
- Conflicting verdicts between loops on the same candidate: MEASURED beats PLAUSIBLE regardless of which loop; two MEASURED disagreements go to a fresh cross-family reviewer with both cards; the result is recorded as CONTESTED if the families split.
- Method validity: the math/ML loop's method note is binding on the dist loops; a replay using a method flagged invalid for autocorrelated or non-stationary data is relabelled INCONCLUSIVE until re-run with an always-valid method.
- Spawn triggers: (a) a loop requests a measurement it cannot run within its quota/time (public-trace download/replay, seed-variance run) -> measurement agent with a disk/time budget and /tmp-only output; (b) end of cycle 2 -> one fresh heavy-tier adversarial reviewer per evidence card (card only, no ranking) and one cross-family fact-checker for BI figures; (c) cycle 3 -> one cross-family (codex exec) refutation per survivor, one fresh reviewer for the C refutation, one reviewer briefed on channel/procurement grounds for BI-landscape; (d) a > 3x research-vs-BI disagreement -> measurement spawn; (e) a buyer-persona check requested by BI-demand -> one expert-opinion agent per persona (former FinOps lead, data-platform lead, ML-platform lead), read-only; (f) a loop asks for an independent opinion on a ruling -> one advisor round, then act.
- External contact: researchers and BI prepare scripts and target lists only; the orchestrator merges them into one owner request per cycle; nobody sends messages; an unanswered request leaves the test BLOCKED, never implied PASS.
- Quota: web search front-loaded to cycle 1; cycles 2-3 use direct page fetches, APIs, Wayback and cached pages; a loop that goes blind writes a 'blind from here' marker and its later numbers are PLAUSIBLE at best.
- Hygiene: every deliverable grepped for the private project name and personal paths before handoff; the POC is referred to only as 'the POC'; throwaway code only under /tmp/<role>/; no repo code, no pushes, no settings edits during the loops.

## Idea seeds by lens

### distributed systems and data (significant + mean merged)

- D-01 Access-policy what-if for data platforms: replay a proposed RLS/masking/Unity/policy-tag change against the org's own query history; which principals and queries break or newly leak; ratified receipt before apply (significant).
- D-02 Warehouse admission and sizing policy with receipts: runaway-query kill, auto-size, credit caps proposed from query history, replayed on Redset/Snowset with $ saved vs false kills and CIs (significant; probe-able day one).
- D-03 Hybrid-authored data contracts as sequential gates: machine proposes schema/freshness/volume contracts, human ratifies, write-audit-publish promotion gated by a sequential test with an evidence card (merges S6 with dist-mean batch/freshness drift and BI-demand freshness seed).
- D-04 Ratified auto-remediation for SRE: failover/scale/restart/kill-connection rules computed from telemetry with confidence, replayed against past incidents, every execution carries a receipt (significant).
- D-05 Knob-rot engine: inventory hand-set numeric policies (retries, timeouts, limits, thresholds), replay candidates against local logs/metrics, sequential verdict card, human ratifies (mean umbrella; D-06..D-09 are its first adapters).
- D-06 Retry/backoff and rate-limit what-if: replay a proposed retry or quota policy over access logs or request streams; retry-storm risk, who gets throttled when, success-rate delta with CIs (mean; retry_ab and the 15-min 429 predictor generalised).
- D-07 Alert-threshold tuning with FDR control: proposed thresholds and for-durations from Prometheus history, would-have-fired and actioned ratio, BH across rules, human ratifies (mean).
- D-08 Flaky-test quarantine as a sequential gate: per-test failure rates with Wilson CIs and BH across the suite from CI run history; quarantine/un-quarantine proposals; no test content read (mean; local-first alternative to seat-priced CI visibility).
- D-09 Autoscaling target and pool-size replay plus canary bake-time as a sequential test (HPA targets, worker counts, DB pools, statement timeouts; Flagger/Argo analysis provider with a receipt per promotion) (mean).
- D-10 [C-shaped, routed to the C refutation] Lifecycle and isolation proofs: erasure/retention proof-of-effect across replicas/backups/compaction, tenant-isolation predicate over access logs, residency evidence per tenant, recovery-assurance readouts (S2/S3/S4/S8 merged; governs lifecycle, never content; shaped to avoid the rejected-proxy pattern).

### math and ML/AI (significant + mean merged)

- M-01 Model-migration and silent-change verdict: anytime-valid changepoint detection per model string on text-free telemetry (tool-call rate, output length, refusal/retry/JSON-failure rate, latency) plus a sequential non-inferiority test over the org's task mix on deprecation; cut over / hold / escalate (merges ml-sig seeds 1-2 with ml-mean seed 3).
- M-02 Eval-noise verdict card: peeking-safe paired sequential comparison (mSPRT/Wilson) at n = 20-200 with minimum detectable effect and seed-noise budget; PASS/FAIL/INCONCLUSIVE attached to every eval run and compare view (merges ml-sig 3 with ml-mean 1, 6, 8).
- M-03 LLM-as-judge calibration gate: judge-vs-gold agreement as a sequential process with drift alarm and a ratified re-calibration threshold, including when the judge model itself is updated (ml-sig 4 + ml-mean 2).
- M-04 Early-kill policy for fine-tuning and eval sweeps: learning-curve extrapolation plus a sequential stopping rule; $/instance in GPU-hours; buyer is an ML-platform lead outside the coding-agent orbit; the 29k-step P6 check is the prototype (ml-sig 7 + ml-mean 4).
- M-05 Quantization and local-model parity gate: paired non-inferiority test on the practitioner's own task scores with a cost delta before switching to Q4/Q5 or a local tier (ml-mean 5; routing-adjacent, flagged for builder bias).
- M-06 Long-horizon agent survival model: per-step hazard from text-free tool-call sequences (step count, retries, edit churn, test outcomes) to decide checkpoint / escalate / kill in unattended loops (ml-sig 5).
- M-07 Private-eval contamination gate: overlap and memorisation probes that a held-out eval has not leaked into fine-tuning data, prompts or retrieval corpora; ratified pass/fail before results are trusted (ml-sig 6; text-free feasibility must be proven in cycle 1).
- M-08 Tool-call reliability ledger: per model/version/tool error, retry and structured-output failure rates with changepoint detection; policy proposes retry or downshift, human ratifies (ml-mean 7; owner loop to be adjudicated with dist-mean at cycle 1 end).
- M-09 Advise-to-enforce guarantee layer: e-process/conformal error-rate control for promoting a policy from advise to enforce with a stated false-enforcement bound; the engine's mathematical differentiator, carried as the engine-shaped residual regardless of market outcome (ml-sig 8).

### business intelligence: bottom-up demand (budget lines)

- B-01 Data-freshness/SLA breach evidence for data teams below Monte Carlo/Bigeye prices: dbt Cloud/Airflow teams with a named data-reliability owner but no tool; stdlib sequential verdict on freshness/volume contracts with a ratified policy and evidence card (joins D-03).
- B-02 Shared inference-cluster capacity policy for mid-size ML-platform teams: GPU quota/scheduling advise-only shed/queue policies with receipts for the GPU budget owner; anchors Run:ai/NVIDIA, Cast AI, Kubecost (joins M-04 buyer).
- B-03 LLM-eval regression gate as a CI budget line: sequential A/B verdict card a release manager signs, priced like a CI add-on; anchors Braintrust/LangSmith/Promptfoo paid tiers (joins M-02).
- B-04 Experimentation statistics below the Statsig/Eppo floor: product teams on LaunchDarkly/GrowthBook/PostHog with no stats engine; stdlib sequential-test engine with ratified stopping rules and an audit card sold to the growth/analytics budget.
- B-05 Per-team AI-coding-spend budgets and chargeback for FinOps: budget gates, drift alarms, monthly card bought by the FinOps owner; anchors Cursor/Claude Code/Copilot enterprise seat prices (joins L-07; market B adjacent, must clear K-CLOSURE against Portkey/Prisma AIRS GA).
- B-06 Stream backpressure/consumer-lag policy with evidence for Kafka operators: anomaly/drift gates on lag with a signed incident card for on-call; anchors Confluent/Redpanda/Conduktor (joins D-05/D-06).
- B-07 [C-shaped] Schema/data-change approval evidence under SOX ITGC / SOC 2 CC8.1 for data platforms: machine-proposed, human-ratified change policy whose verdict card doubles as audit evidence; anchors Bytebase/Liquibase/Atlan (routed to the C refutation).
- B-08 Local-first model/feature drift monitoring for regulated small shops that cannot send data out: ratified thresholds and a per-model evidence card priced per model/month; anchors Arize/WhyLabs/Evidently (joins M-01).

### business intelligence: competitive landscape and channels

- L-01 Agent-observability OEM: a 'machine proposes, human ratifies' policy layer with sequential verdicts as a module or integration for Langfuse/Arize/Braintrust/Helicone/LangSmith, which observe but do not gate or ratify; map who lists guardrails/policies as roadmap.
- L-02 FinOps chargeback for AI agents: text-free usage ledger as a FOCUS-compatible showback data source for Vantage/CloudZero/Finout; buyer = FinOps practitioner; channel = FinOps Foundation directory (joins B-05).
- L-03 OSS foundations and maintainers facing AI-generated PR floods: policy on agent contributions plus provenance evidence per PR; buyer = foundation via grants/sponsorship; demand = maintainer policy pages and 'AI-generated PR' issue volume.
- L-04 MSSP/MSP white-label channel: sell the instrument (fleet readout + policy replay) to 3-5 managed providers rather than 50 end orgs; map which MSSPs already bundle AI governance (channel seed; attaches to whichever product survives).
- L-05 [C-shaped] Revenue-attached questionnaire evidence: security-questionnaire owners (SIG/CAIQ/custom) now asked how AI coding agents are governed; evidence pack sold to the vendor's questionnaire owner; incumbents Conveyor, Vanta Questionnaire Automation, SecurityPal (routed to the C refutation as its strongest buyer variant).
- L-06 [C-shaped] Cyber-insurance underwriting of AI-coding-agent risk: text-free telemetry attestation answering insurer AI-agent questions at renewal; channel = broker/insurer control-partner programs (routed to the C refutation).
- L-07 [C-shaped] Regulated SDLC tool qualification (IEC 62304 / ISO 26262 / DO-178C / FDA): AI agents as software tools needing tool-confidence evidence; buyer = quality/regulatory lead and consultancies; incumbents Ketryx, Jama, LDRA (routed to the C refutation; needs owner tolerance for regulated sectors).
- L-08 [C-shaped] CMMC 2.0 / CUI environments: proof that agents never ingested CUI and stayed inside boundaries; channel = C3PAOs and GCC-High MSPs (routed to the C refutation; needs owner tolerance for defense).

## Validation roles (added by the session lead, to be ratified)

The six generating roles and the orchestrator cannot validate their own output. Added roles, by what they close:

- **Refuter / red team** (cycle 2 on every shortlisted candidate, cycle 3 on survivors; cross-family where available): prompted only to kill — cheaper substitute, nice-to-have, demand that evaporates once the buyer is named. Majority-refute kills.
- **Buyer simulator** (cycle 3): role-plays the named buyer from public artefacts (job posts, procurement docs, pricing pages, forum threads); answers would-I-pay, from which line, what would I stop buying; marks where only a real conversation decides.
- **Evidence auditor** (end of cycles 1 and 2): re-opens every MEASURED and PLAUSIBLE label and checks the source says what is claimed; downgrades on miss. (The first programme had one wrong fact-check; this is the guard.)
- **Engine fit engineer** (cycle 2): per candidate: data in, policy computed, evidence out; does the current repo do it, could one builder in weeks, or not at all.
- **Data availability analyst** (cycle 2): the data the policy needs exists where the buyer is, who owns it, reachable read-only by a local-first plugin.
- **Pricing and unit economist** (cycle 3): cost to serve, price points from comparables, margin for one builder, pilot payback.
- **Channel strategist** (cycle 3): plugin marketplace / open source / services / direct — which converts for this buyer, with precedents.
- **Competitor shadow** (cycle 3, top candidate): what the two closest incumbents do within 12 months.
- **Experiment designer** (cycle 3): each kill test as a decidable protocol — sample, metric, threshold, cost, deadline — on the repo's verdict card.
- **Regulatory / compliance expert** (triggered only for C-shaped candidates): which framework clause actually demands the artefact.
- **Devil's advocate reconciler** (every cycle): reads the orchestrator's inputs independently and must disagree where it can.

Not added: ethics (nothing user-facing yet), a visionary (the significant lens does this), more BI angles (two independent ones suffice for correlation).

## Disagreements left to the owner

- Value floor for the significant lens: dist-significant set $100k/instance, ml-significant $25k. I unified at $25k (with $100k scoring value 3) so the two lenses compare; the owner may prefer the stricter $100k, which would thin the dist table to regulated/SLA cases only.
- Whether the C (compliance-evidence) market gets its refutation inside this programme or is kept out: 5 of 8 bi-landscape seeds and 4 of 8 dist-significant seeds are C-shaped; BI-demand insists C gets no head start; researchers insist it is not scored as a win. I routed all C-shaped seeds to one cycle-3 refutation; the owner may instead want C tested separately via A1/A2/A3 only, or dropped.
- ml-significant asks whether ML-platform / research-lab buyers (GPU-hour problems, M-04, B-02) are in scope for a plugin-first product; bi-landscape's one-person-winnable test (<= 10 builder-days from existing assets) would exclude them. Unresolved: I kept them in cycle 1 and let fit floors decide at cycle 2.
- ml-significant asks whether a publishable method (M-09 advise-to-enforce guarantees) counts as value; BI-demand counts only money; dist-mean caps zero-dollar mass demand at OSS-asset. I applied the OSS-asset cap; the owner decides whether an engine-shaped residual with no revenue is an acceptable programme outcome.
- External contact: every loop's cycle-3 kill tests weaken to BLOCKED without buyer/auditor/practitioner conversations; BI-landscape additionally asks whether datapce.com may list an offer. Nobody in the programme sends messages; the owner decides whether and who contacts.
- bi-landscape's admissibility constraints (services at all, hours/week, US vs EU triggers, defense/regulated sectors, ever obtaining own SOC 2) change which segments can be shortlisted; bi-demand assumes services-first stays on the table per the 10-13 correlation. Not reconcilable by me.

## Open questions for the owner

1. Confirm the hard constraints driving K-TEXT and K-ORACLE: datapce never stores prompt/response text; local-first; one builder, months not team-years. Also: is a candidate living in an eval harness plugin or CI step (not the proxy or the Claude Code plugin) inside 'plugin-first'?
2. Is the privacy-proxy rejection ('not a good fit') specifically about handling content inline in the data path, or about any product that touches PII lifecycle at all? This decides whether D-10 (erasure/residency proofs over metadata only) is shaped or dropped at cycle 1.
3. Permission for read-only /tmp scripts over ~/.apex telemetry (no text read, nothing committed) and for measurement agents to download public traces (Redset, Snowset, Google/Alibaba cluster traces) with a disk budget — how much disk and wall-clock may the loops use?
4. Web-search budget per loop per cycle (default I will apply: 25 per research loop in cycle 1, 25/40/15 per BI loop) and a GitHub API token for issue counts; confirm direct page fetches on vendor, procurement and job-board domains are permitted.
5. Will the owner run external conversations (one data-platform lead, one multi-tenant SaaS DBA/SRE, one ML practitioner who ran a 2026 model migration, five audit firms per A1, buyer-persona emails per BI card), and by when? Without an answer by cycle 2 end, cycle 3 proceeds with those tests BLOCKED.
6. May datapce.com list a provisional offer during cycle 3 (BI's A3 test), and is services revenue acceptable at all for the first path?
7. Scope rulings: GPU-hour / ML-platform buyers in or out; defense (CMMC) and regulated-device sectors in or out; US-only or EU regulation triggers (GDPR/DORA/EU Data Act) count as demand?
8. Is an engine-shaped residual (verdict-card schema, stdlib core, advise-to-enforce bound; no product) an acceptable outcome of this programme, so the 'no survivor' path still produces something the owner wants?
9. Cross-family reviewers: is codex exec available and budgeted for every survivor refutation (roughly 6-8 calls in cycles 2-3)? If not, all refutations are one family and every survivor is reported CONTESTED.
10. Should the prior β capacity-policy spike (same-arm replay vs persistence, external multi-key operator) run in parallel as a known-shape control, or be absorbed into D-06 and judged on the same rubric?
