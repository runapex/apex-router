# DESIGN — SKILL.state (arXiv:2608.26263) adaptation and evaluation record

Status: **shipped default-off** (2026-08-30). The harnesses are the durable product; the
measured verdict at apex's real horizons is *behavioral parity, token parity, robustness
parity* — so nothing is promoted to a default. Re-run the benches when the model, the
corpus, or the horizon changes.

## The paper

SKILL.state replaces the append-only conversational runtime with an explicit, mutable
execution state. Each step the model sees `(P, Σ_t, O_t)` — immutable procedural spec,
structured state, latest observation — and emits `(reasoning R_t, state patch ΔΣ_t, action
a_t)`; `R_t` is discarded after a validated state update (`Σ ⊕ ΔΣ`, null-deletion merge).
Claimed benefits: O(T) vs O(T²) cumulative tokens, distractor-noise immunity, and zero-step
recovery from silent environment drift (history runtimes anchored 5–8 turns). Open-weight
error taxonomy (§5.7): 68% premature state overwrite / 20% schema-type / 12% JSON syntax —
motivating patch-only updates, validation, and constrained decoding.

## What was adapted, and what was measured

### 1. Codegen state lane (local model) — `ornith/state_codegen.py`

The one-shot codegen lane (generate → run caller tests → escalate cold) becomes a bounded
self-repair loop: model emits **patch-only** ΔΣ (never the full state — the 68% overwrite
mode becomes structurally impossible), runtime validates against the lane schema (unknown
keys / wrong types / deleting `code` → rejected and counted under the paper's taxonomy
labels), merges server-side, re-runs tests; test output is the next observation. Verdict
doctrine unchanged (`ok` == tests pass == `gated`); usage summed across calls so retry cost
is booked honestly; escalation carries the structured state, not a transcript.

Dispatch wiring: `ORNITH_CODEGEN_STATE_LANE=on` (default off; injected stubs never bypassed).
Bench: `python -m apex_router.ornith.state_bench [--suite x.jsonl] [--fake]` — Wilson CIs,
escalation delta, tokens/pass, taxonomy, explicit POSITIVE/DEGRADATION verdict lines.
Suites: `benchmarks/codegen_hard.jsonl`, `benchmarks/codegen_probe.jsonl`.

**Measured (Ornith-1.5-35B-A3B, 17 tasks × 2 arms):** 100% first-attempt pass in BOTH arms
on all three suites — the repair loop never engaged; the state arm cost 1.9–2.9× tokens/pass
(JSON-patch contract overhead). JSON contract held 17/17 first attempts (no §5.7 failures on
this tier). **Verdict: default-off; pure overhead while first-attempt pass ≈ 100%.**
Break-even needs `first_attempt_failure_rate × rescue_rate` to cover ~2× per-job overhead.

### 2. (P, Σ, O) frontier driver — `proxy_engine/tuner/state_driver.py` + `driver_bench.py`

The behavioral gate's tool loop (append-only Anthropic Messages loop) got a state variant:
one user message per round = (original prompt, runtime-maintained Σ, latest results). The
runtime owns Σ (`ref → fragment` map) — the frontier model is never asked for structured
patches. `behavioral_driver` gained a `call_api` seam (production path unchanged) so the
bench swaps arms by name. Budget enforced between rounds; a forced tool-less answer round
fires when the cap is exhausted mid-retrieval.

**Measured (offline, scripted API, input from actual request bytes):** behavior parity all
tasks; token parity at the gate's 4-round horizon (state ≈ 102–113% of transcript — the
Σ-resend overhead dominates the discarded-narration savings at short T). Live opus run not
executed (no bearer in CI env); `--live` is ready.

### 3. GPT transferability + drift — `proxy_engine/tuner/codex_driver_bench.py`

Model-agnostic harness over `codex exec` (no Anthropic auth): RETRIEVE/ANSWER directive
protocol over the real json_crush/StubResolver probe; transcript vs (P, Σ, O) arms; tokens
from codex's own report. Plus the paper's experiment 3: silent mid-run fragment swap +
corrective alert — transcript keeps the stale value in history (anchor contest), Σ is
replaced.

**Measured (live GPT, 2026-08-30):** identical ref sequences, zero protocol errors,
character-identical *correct* answers — behavior transfer confirmed. Tokens parity
(~38k/arm; codex exec's ~4.7k/call fixed overhead dominates; prompt-size replay shows state
pulling ahead only as rounds grow). **Drift: both arms RECOVERED** — GPT did not anchor on
stale history given one explicit alert. The paper's robustness advantage does not transfer
to a frontier model at this horizon/salience (N=1/arm; harder untested variants: no-alert
drift, multi-drift, distractor noise).

### 4. Structured handoffs and chain contracts

- `handoff_state.py` + `hooks/cache-handoff-nudge.sh` — the expensive-session handoff is a
  6-field structured state block (goal/constraints/decisions/files_touched/open_issues/
  next_action) instead of prose; the session fills it before stopping (the hook stays
  LLM-free); `validate` CLI catches unfilled blocks; a drift-guard test pins the hook's
  embedded template to the module.
- `integrations/pi/learn.ts` — the /learn chain's VALIDATE slot emits a JSON verdict Σ
  (authoritative/weak/sections/focus_questions); EXPLAIN is prompted (P, Σ) with sources
  restated. Parse is contract-enforced (non-empty `authoritative` string array, known keys,
  typed values) and fails open to the legacy prompt.

## Decisions and open items

- **Everything default-off.** Three settings, parity everywhere; adoption is earned, not
  assumed — the same gate any routing change passes.
- **Cross-validated with GPT (codex-cli)** on the initial diff: 8 findings, all triaged —
  7 real (state-loop fragment loss, parity-by-count measurement, verdict starvation guard,
  mid-loop budget, cap-exhaustion answer, lax handoff validation, bytes-vs-tokens
  overclaim), each fixed by the author with a pinning test. Reviewer-proposed fixes were
  not adopted (per the cross-validate discipline).
- **Known bugs found by running, fixed before commit:** probe content tripping json_crush's
  Δ7 lexeme guard (zero elisions); identical leaves collapsing to one content-addressed ref;
  double-sending fragments in both Σ and observation.
- **Re-run triggers:** local model change or first-attempt failure rate rises (state lane);
  longer-horizon workloads appear (drivers); a subtler drift test is wanted (codex bench
  `--drift` variants).
- **Open:** `/learn` contract-hold instrumentation (verdict-parse rate per run) + one live
  `/learn`; handoff hook's first natural fire, then `handoff_state validate` on the output.
