# Lit scan 3 — decomposition, fan-out vs single context, tail-seeking (2026-10-12)
Model-produced scan (Opus 5.5 subagent; abstracts/blogs). Spot-check before quoting.

1. Towards a Science of Scaling Agent Systems (Kim, Liu et al., Google, arXiv 2512.08296 v3 2026-04-08; blog 2026-01-28): vs single agent +80.8% (decomposable finance) to −70.0% (sequential planning); independent parallel agents amplify errors 17.2×, orchestrator contains to 4.4×; predictor picks best architecture 87%; A_e = E_MAS/E_SAS.
2. Anthropic multi-agent research system (2025-06-13): Opus lead + Sonnet subagents +90.2% on internal research eval; tokens explain 80% of BrowseComp variance; ~15× chat tokens; coding named a poor fit.
3. Single-Agent > Multi-Agent at equal thinking tokens (Tran & Kiela, arXiv 2604.02460): 0.418 vs 0.379 sequential / 0.388 debate at 1k tokens; multi-agent wins only under heavy context degradation (α=0.7). → null hypothesis for W2; log context fill at dispatch.
4. Agent scaling via diversity (Yang et al., arXiv 2602.03794): 2 diverse agents ≈ 16 homogeneous; label-free effective channel count K*.
5. Runtime-Structured Task Decomposition (Asthana et al., IBM, arXiv 2605.15425): retry cost static 1,632±145 tokens vs monolithic 904±17 vs runtime-structured 436±132 (−73.2%). → log recovery cost per subtask.
6. Strategically Diverse Sampling (Gurung et al., arXiv 2609.31571): held-out pass@8 0.9 base → 6.7 GROOT / 7.6 VS; IID pass@64 7.9 vs 15.5/17.5 with 16× fewer samples.
7. TTT-Discover (Yuksekgonul et al., arXiv 2601.16175): test-time RL with entropic/max objective; kernels up to 2× prior art; "success is determined by the maximum" → score by max@k.
8. CORAL (Qu et al., arXiv 2604.01658): 3–10× higher improvement rate vs fixed evolutionary search; SOTA on 10 tasks; separated evaluator; shared persistent memory.
9. SWE-Master (Song et al., arXiv 2602.03411): 61.4% single → 70.8% TTS@8 (+9.4); selected-best tracks oracle pass@K.
10. Debate or Vote (Choi, Zhu, Li, NeurIPS 2025 spotlight, arXiv 2508.17536): voting explains most debate gains; debate is a belief martingale.
11. When Does a Second Model Help (Song, arXiv 2610.01471, 2026-10-01): same+cross review catches 56.7% vs 42.7% same+same (p=.006); reviewer overlap Jaccard 41.2%.
12. Cross-Model Code Review (Xiang et al., arXiv 2607.21656): asymmetric — Codex 71.6→89.7 reviewed by Claude; Claude 91.4→82.8 reviewed by Codex (p=.046). → gate review direction per (drafter, reviewer) pair.
Not used: 2609.19759, 2602.01485, 2606.06080 (unverified), MAST 2503.13657 (version drift).

Synthesis: fan-out helps on separable/breadth-first work and when the single context is degraded; most reported MAS gains are extra compute → W2 claims must be token-matched; unchecked parallelism amplifies errors (17.2× vs 4.4× with a central verifier); diversity > headcount; "significant result" = max@k / pass@k / improvement rate, realized only through the selector (oracle vs selected gap); debate ≈ vote; cross-family review finds different errors but can hurt a stronger drafter.
Experiments: 1 randomized W0-vs-W2 at matched tokens (100–150 tasks, within task type; A_e per type; covariates context fill, tokens, retry cost); 2 diverse-strategy best-of-k vs IID on the hard stratum (oracle pass@k, selected@k, max−mean); 3 reviewer-lane ablation with ~50 planted bugs (catch rate, Jaccard, right→wrong vs wrong→right flips per direction).
