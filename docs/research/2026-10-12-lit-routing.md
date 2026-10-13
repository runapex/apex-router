# Lit scan 1 — quality-optimised routing, cascades, ensembles, test-time compute (2026-10-12)
Model-produced scan (Opus 5.5 subagent, web-read); every number is as reported by the scan and still to be spot-checked before quoting in a decision.

1. The Routing Plateau (Lu et al., arXiv 2606.07587, v2 2026-09-28): 21 routers × 5 benchmarks; kNN 76.95% vs best trained 77.01%; top-5 within 0.22 pp; all 10.6–29.4 pp below oracle; 23k→302k queries adds +1.24 pp. → prompt-only routers plateau; kNN is enough at our scale.
2. Opportunity Is Not Realizability (Shihab et al., arXiv 2608.08265, 2026-08-08): oracle−best-fixed gap 9.7–30.7 pts; best prompt-only router captures 7.5–14.4% of it; CI lower bound 0 on every task. → report against best single tier with selection-valid CIs.
3. Few-Shot Router (Varangot-Reille et al., EACL 2026 SRW): retrieval/kNN routers generalise from ~1% history; adding a model needs a few labelled runs, no retraining.
4. DARS (Lai et al., EMNLP 2026, arXiv 2606.06924): single-sample labels flip the winning model on 64.5–97.0% of queries; repeated-sample labels improve 40/42 settings (kNN DROP 0.713→0.749) with 200 queries. → our labels are single-sample; re-run a subset 3× per tier.
5. SWE-Router (Son et al., ICML 2026 DL4Code ws, arXiv 2607.00053): route after K=2 cheap turns: Route-AUC 0.780 vs 0.627 description-only; value head (7B LoRA) on ~1.7k trajectories/pair; shift caveat 0.547 vs 0.626. → partial-trajectory routing is the relevant design; start with logistic/kNN on first-turn features.
6. Agent-as-a-Router / CodeRouterBench (Zhou et al., arXiv 2606.22902): per-dimension win-rate tables in the router context give +15.3% relative; online regret framing.
7. ReDAct (Piatrashyn et al., arXiv 2604.07036): per-step deferral on calibrated uncertainty; ~15% of steps to the large model matches large-only. Needs logprobs (not Claude).
8. Cheap Verifiers, Large Blind Spots (Rajput et al., arXiv 2609.01345): verifier false-accept 0.12→0.55 as student grows; in-loop metrics flat ~3% while true error up to 32%. → judge-graded dashboards cannot certify quality; keep cross-family gold.
9. Partially Correlated Verifier Cascades (Han, arXiv 2607.13918): independence assumption underestimates failure 20× at k=5, ~3000× at k=10; two repeated verdicts identify the correlation. → more same-family gates ≈ nothing; cross-family verifier is the lever.
10. Self-MoA (Li et al., TMLR 2026, arXiv 2502.00674): N samples from the top model + synthesis beat mixed MoA by 6.6% AlpacaEval / 3.8% avg.
11. When Agents Disagree (Maryanskyy et al., Appl. Sci. 16(10) 4914; arXiv 2603.20324): with a judge SELECTING one candidate, diverse team wins 0.810 vs 0.512 same-model (N=210); selection beats synthesis +0.631; threshold on selector quality.
12. Inverse Scaling in Test-Time Compute (Gema et al., TMLR Dec 2025, arXiv 2507.14417): longer reasoning can drop accuracy 70%→30% (R1) on distractor tasks; Opus 4 to ~85–90%. → effort is a routing arm with measured outcomes, not a monotone knob.
Also: Conformal Cascade (2607.25018, MC only); Semantic Agreement cascades (2509.21837; target quality at 40% cost); Predicting inference-time scaling gains (2606.02981; ρ=0.90).

Synthesis: at ~500 labels use kNN/retrieval + win-rate tables + one calibrated escalation threshold; prompt-only learned routers are not worth it; a trajectory value head needs ~3× our data (do the logistic version); label noise from single samples is probably our largest error source; cross-family only pays with judge SELECTION; in-loop same-family verification hides error; effort is non-monotone.
Experiments: A label-noise re-run (100 tasks × 3 × Haiku/Sonnet); B SWE-Router-lite (escalate? from first 1–3 turns, held-out AUC + success/cost vs description-only); C cross-family verifier (false-accept + correlation, Claude vs GPT judges on gold; best-of-2 cross-family on Opus failures).

## Spot-check (2026-10-12, Sonnet subagent, arXiv abstract pages only)

Ten numbers across the four scans were checked against the arXiv abstracts. CONFIRMED = the number
is in the abstract; PLAUSIBLE = paper and topic match, number is in the body (not checked);
PARTIAL = part confirmed.

| scan item | verdict | abstract evidence |
|---|---|---|
| Routing Plateau 2606.07587 | PARTIAL | "+1.24 pp on average" confirmed; 76.95/77.01 and 10.6–29.4 pp are body numbers |
| DARS 2606.06924 | PLAUSIBLE | abstract carries no numbers |
| SWE-Router 2607.00053 | PLAUSIBLE | abstract carries no numbers |
| Automata from Agent Traces 2608.23670 | PLAUSIBLE, flag | abstract says failure AUROC "up to 0.94"; the scan's 0.799 is a per-dataset body number, not contradicted but quote 0.94 as the headline |
| EarlyEval 2609.02783 | CONFIRMED | "89%–97% prediction accuracy", "13%–26% of agent steps" |
| Tran & Kiela 2604.02460 | PLAUSIBLE | matched-budget claim confirmed; 0.418/0.379 are body numbers |
| Harness Evolution 2607.12227 | CONFIRMED | "fails to outperform simple test-time scaling" (Terminal-Bench 2.1); ARC-AGI-3 "+80%" |
| SkillOpt 2605.23904 | CONFIRMED | "+24.8" Codex, "+19.1" Claude Code on GPT-5.5 |
| Scaling Agent Systems 2512.08296 | PARTIAL | "+80.8% … to −70.0%" confirmed; 17.2× / 4.4× are body numbers |
| Prediction ≠ Prevention 2602.03338 | CONFIRMED | "AUROC 0.94", "26 percentage point" drop |

All ten papers exist and match their topics. Numbers marked PLAUSIBLE are quoted from the scan,
not from the paper, until someone opens the PDF. The same caveat applies to every un-checked number
in the four scans.
