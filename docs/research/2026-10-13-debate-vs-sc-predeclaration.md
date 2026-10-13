# Follow-up — full-context exchange round vs self-consistency: pre-declaration

**Status:** pre-declared 2026-10-13, before any call of this study. Harness `scripts/panel_debate.py`;
verdict via `apex_router.core.verdict` + `gate.run_gate`.

## Question

On the HotpotQA demo the best arm was `rho1` (J=3, everyone sees everything, one rationale-exchange
round): +0.0107 Brier vs six-sample self-consistency, CI [−0.0000, +0.0237], exploratory. Is that real, and
if so is it the *peers* or just *a second look*? Three flaws in that run are designed out here
(Codex findings #2, #4, #6): round 2 now shows the agent its own round-1 answer; every arm uses the same
neutral prompt; every arm is clipped identically.

## Benchmark

MuSiQue-Ans dev (2–4-hop, 20 fixed paragraphs, built to defeat single-hop shortcuts), binarised like the
HotpotQA bridge items: (question, proposed answer) → correct? n=360, 180 positive / 180 negative, stratified
by hop count, seed 11. Negative = the answer to the *previous* hop of the decomposition (the classic
multi-hop trap: an entity mentioned in a supporting paragraph), else the title of a non-supporting
paragraph; never equal to the answer or an alias. Split promo/confirm by sha256(id) parity, 4 hash windows.

## Diversity without temperature

`it-entra-gpt-5.6-luna` rejects `temperature`. Each independent sample s ∈ {0..5} sees the 20 paragraphs
in a fixed per-(item, s) permutation (s=0 is the original order). This is the diversity source for every
arm alike.

## Arms (same model, effort `low`, same prompt template)

| arm | calls | definition |
|---|---|---|
| `single` | 1 | sample 0 |
| `sc6` | 6 | samples 0–5, pooled |
| `rho1` | 6 | samples 0–2 = round 1; round 2: agent a (its own order) sees its own round-1 answer + the two peers' (p, rationale); final = pool of round 2 |
| `reflect` | 6 | samples 0–2 = round 1; round 2: agent a sees only its own round-1 answer; final = pool of round 2 |

`sc6`, `rho1` and `reflect` share round 1 (samples 0–2) call-for-call, so they differ only in what the
second three calls are: three more independent samples, three peer-informed revisions, or three
self-revisions. Pool = uniform logit pool with p clipped to [0.01, 0.99], for every arm incl. `single`.

## Pre-declared tests

Paired per-item Brier, bootstrap (4000), BH at 0.05 across:
- **C1 (primary)** `rho1` vs `sc6`
- **C2** `rho1` vs `reflect` — do peers add anything over a second look?
- **C3** `reflect` vs `sc6`
- **C4** `sc6` vs `single`

Gate: one cell `debate/musique/vs-sc6`, incumbent `sc6`, single candidate `rho1`, k=30, m_windows=2,
objective provenance, **no cost** (so no tiebreak can substitute a candidate).

## Decision rule (fixed now)

- **A — adopt a peer-exchange round**: gate promotes `rho1` AND C2 is `better` with BH reject.
- **B — the gain is a second look**: gate promotes `rho1`, C2 not `better`. Recommend a self-review round
  (cheaper to wire), not a peer exchange; C3 decides whether even that beats more samples.
- **C — close the thread**: gate does not promote `rho1`. Full-context exchange does not beat
  call-matched self-consistency; nothing is built.

What changes on each outcome: A → `cross-validate-codex` gets a documented one-round exchange step
(reviewer sees the producer's rebuttal once, then gives its final verdict) and the backlog records it;
B → the same skill gets a self-review step instead; C → the backlog records the closure, no skill change.
