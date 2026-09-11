# DESIGN / SIZING — shadow-A/B labeler for proxy-turn routing labels

**Status:** SPEC (sizing). Not built. Needs a Codex design cross-validation pass BEFORE implementation
(the confounded-label trap has sunk routing designs here before — K3 handoff guardrail #6).

## Problem this solves

The escalation-log auto-capture (shipped, commit `276898b`) produces routing labels **only for
pi-routed work** — the surface where apex controls model selection. The dominant cost (opus, 78.6% of
spend) flows through the **proxy path**, where the client (Claude Code / Codex) picks the model and
apex is passthrough. Proxy turns therefore have **no routing label**: we never learn "would a cheaper
model have sufficed on this turn?"

Live cheap-first dispatch on the proxy can't produce that label (sized in the prior analysis):
model-rewrite busts the prefix cache (forfeiting the 10× discount that IS most of the opus cost), and
the passthrough proxy has **no escalation signal** — the client drives the loop, so a worse-but-valid
cheap answer is invisible to apex. So the label must come from a **background counterfactual**, not
the live wire.

## Core idea

**The raw transcript already contains the real frontier answer that shipped** (verified: a Claude
project transcript pairs each user turn with the assistant answer that followed — 447 assistant answers
in one ml session). So we don't guess whether cheap would suffice — we **replay the same turn context
on a cheaper model offline and compare its output to the frontier answer that was actually accepted.** That is the exact counterfactual the
outcome-router needs (`P(cheap correct | features)`), obtained with:
- **zero live-wire risk** (measure-only, background, never touches a served request), and
- **no escalation mechanism** (the ground truth is the shipped frontier answer, not a live bounce).

This is the offload `codegen` adjudication pattern (`composed_adjudicate`), repointed from queued jobs
to historical proxy turns.

## Architecture

```
transcripts (Claude ~/.claude/projects, Codex ~/.codex/sessions)   ← the ONLY raw-content source
        │                                                            (proxy telemetry has NO body)
        ▼
  turn extractor  (context from fixtures.build_replay_corpus; the frontier ANSWER from the RAW
        │            transcript — see correction below: the builder's Request.content ENDS on the
        │            user turn and does NOT carry the assistant answer)
        │
        ▼   for a SAMPLE of frontier turns (opus/gpt-5) with a real answer:
  cheap replay  →  ornith_client.chat_messages(context, model=cheap_tier)   [local :11434, free]
        │
        ▼
  GRADER (tiered, objective-first — see below)
        │
        ▼
  route_log row:  (task_type, start_tier=cheap, ok|escalated|CANNOT-DECIDE)   ← same schema as pi capture
        │                                                                        (reuses read_rates/route-advise)
        ▼
  route-advise verdict (Wilson CI + n>=30 floor)  →  downshift decision, out-of-sample validated
```

## The grading problem (the hard part — where prior designs died)

An LLM-judge "does cheap ≈ frontier?" drifts toward vibes (the standing lesson; the offload lane only
trusts an EXECUTABLE gate). So grade **objective-first**, and prefer CANNOT-DECIDE to a faked label:

1. **Tool-call turns (objective, no judge).** If the frontier turn was a tool call (grep/read/edit
   args), and the cheap replay emits the *same* call (normalized args) → `ok`. Different/malformed →
   `escalated`. Covers a large share of agentic turns, zero judge risk. **Build this tier first.**
2. **Code-citation turns (deterministic oracle).** Grade the cheap answer's `file:line` citations with
   the codeqa grounding oracle (`codeqa ground --check`): all grounded → candidate `ok`; any stale →
   `escalated`. A fact, not a vote.
3. **Prose/answer turns (judge, lower confidence, flagged).** Use the codeqa paired-decide opus judge
   (`judge.py`, already hardened 30%→0% prose-reprompt error) to score cheap-vs-frontier semantic
   equivalence. Label ONLY on a confident verdict; otherwise CANNOT-DECIDE.
4. **Everything else → CANNOT-DECIDE**, excluded from the training denominator (the conformance
   honesty invariant: an unobservable outcome never fakes a rate).

**Honesty invariant:** only tiers 1–2 (objective) may promote a routing decision on their own. Tier-3
judge labels are recorded but flagged `grade=judge` so the router can down-weight or exclude them; a
downshift must never rest on judge-only evidence (the confounded-label guard).

## What to reuse (not rebuild)

- `fixtures.build_replay_corpus.build_streaming_corpus` — turn CONTEXT + content-class/stratum (its
  `Request.content` deliberately ends on the user turn, so it gives the context but NOT the frontier
  answer). **Correction found during sizing:** the frontier answer must be extracted from the RAW
  transcript (the assistant message following each user turn), NOT from the corpus builder — a small
  raw-.jsonl parser the extractor owns. This is the one piece that is genuinely new, not reuse.
- `ornith_client.chat_messages` — the cheap replay call (local model, free, already env-profiled).
- `route_log.log_outcome` / `read_rates` / `route-advise` — the label sink + the Wilson-CI/sample-floor
  verdict. **The shadow labeler writes the SAME `route_log.jsonl` the pi capture does** — one training
  table, two producers.
- `composed_adjudicate` / codeqa `judge` + `ground` — the grader tiers.
- `route_conformance` (with the Δ2 `reusable_tokens`/`cache_compat_id` fields) — per-turn features for
  the eventual router join.

## Sizing

| Piece | Effort | Notes |
|---|---|---|
| Turn extractor (context from corpus builder + frontier ANSWER from raw transcript) | ~1.5 days | NOT a thin wrapper — the answer needs a raw-.jsonl parser (builder omits it); the one genuinely-new piece |
| Cheap replay + tier-1 tool-call grader | ~2 days | the objective, highest-value core; ships alone |
| Tier-2 grounding grader | ~1 day | shell to `codeqa ground --check` |
| Tier-3 judge grader (flagged, optional) | ~2 days | reuse `judge.py`; the risky tier — gate behind a flag |
| route_log write + `grade` field + dedup | ~1 day | additive schema (like Δ2) |
| Nightly launchd job (batch over new transcript turns) | ~0.5 day | mirror `com.ornith.overnight` |
| Codex design xval + out-of-sample eval harness | ~1 day | MANDATORY before trusting any downshift |

**MVP (tiers 1–2 only, no judge): ~1–1.5 weeks** (the extractor correction adds ~half a day). That
already produces objective `ok/escalated` labels on tool-call + citation turns — the bulk of agentic
work — with zero vibes risk. Tier-3 (judge) is a later, flagged add. Full build incl. tier-3 + eval
harness: ~2–2.5 weeks.

## Guardrails (load-bearing — this problem class ships invalid stats without them)

1. **Counterfactual label, not perceived difficulty** — grade cheap output vs the shipped frontier
   answer, never a difficulty score.
2. **Objective-first; prefer CANNOT-DECIDE** — tiers 1–2 promote; tier-3 is recorded-but-flagged; the
   ungradable are excluded, never faked.
3. **Out-of-sample validation** — hold turns out; report AUC + cost-savings CI vs the current
   context-size rule. No in-sample gate (the 2026-07-31 design died here).
4. **Measure-only** — the labeler NEVER changes a served request; it replays historical turns in the
   background. Same doctrine as the rest of apex.
5. **Cache realism in the payoff** — a downshift the router recommends must be priced against the
   prefix-cache it would forfeit (opus cost is 66% cache-read); the labeler measures *correctness*, the
   downshift decision must also clear the *cost* counterfactual (cachesim), not just the accuracy bar.
6. **Codex design xval BEFORE building the grader** — the grading tiers are a decision-metric; cross-
   validate the design first.

## Kill criteria

- If tier-1 tool-call replays show the cheap model rarely matches (low `ok` rate across task-types),
  the downshift opportunity is small — say so and keep the rule; don't reach for the judge to
  manufacture a rosier number.
- If the only positive signal is tier-3 judge-graded, treat it as INCONCLUSIVE (judge-only can't
  promote) — the objective tiers must carry it.

## Relationship to the shipped work

- **pi auto-capture (shipped):** live labels where apex controls the model. Cheap, real, low-volume.
- **shadow-A/B labeler (this spec):** background labels for the proxy majority, from transcripts +
  cheap replay + objective grading. Higher-value, needs the ~1-week build + a Codex design pass.
- Both write ONE `route_log.jsonl`; `route-advise` consumes both. Together they cover interactive AND
  proxy routing evidence — the training corpus the K3 outcome-router needs before it can beat the
  context-size rule.
