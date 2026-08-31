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

### 3b. Claude-family transferability — `proxy_engine/tuner/claude_driver_bench.py`

The DESIGN's opus open item ("live opus run not executed — no bearer in CI") closed via the
`claude` CLI (no raw Anthropic bearer needed), same model-agnostic RETRIEVE/ANSWER harness as
the codex bench: `claude -p --bare --output-format json` (`--bare` pins a ~1.7k system prompt
so marginal growth is the signal; same no-prefix-cache cost caveat as codex). Offline test:
`tests/test_claude_driver_bench.py`.

**Measured (live claude sonnet + haiku, 2026-08-31):**

- **Behavior A/B — a probe-validity bug surfaced and was fixed.** The original probe encoded
  each deploy window as `deploy-window-<service-name>`, guessable from the visible service
  key: sonnet & haiku retrieved only **2 of 6** refs and pattern-guessed the rest — *correctly*
  by luck, so the offline (scripted-model) bench could never catch it. Fixed by making each
  window an **opaque 6-letter code** (`driver_bench.WINDOW_CODES`) recoverable *only* by
  retrieval. Re-run: the transcript arm still early-stops at 2/6 and now **HALLUCINATES**
  plausible-wrong codes for the 4 it never fetched (verifiably incorrect); the state arm
  retrieves all 6 and reports the real codes. So at this horizon the state arm's edge is
  **correctness under an early-stopping model**, not just token profile — a stronger result
  than the GPT/offline parity finding.
- **Drift (re-measured 2026-08-31 on the FIXED harness).** The first-pass "sonnet ABSTAINS"
  reading was an artifact of a shared-resolver bug (P1 below): the transcript arm's revised-
  fragment swap leaked into the state arm, which then never experienced the drift. With a
  fresh per-arm resolver, sonnet **RECOVERS**, matching GPT. Aggregate over independent trials
  (each arm scored RECOVERED/ANCHORED/MIXED/NEITHER; no ANCHORED or NEITHER observed):
  - sonnet/plain (N=8): state RECOVERED 8/8; transcript RECOVERED 5/8, **MIXED 3/8** (the
    MIXED = the injection-refusal note, next bullet — stale kept in-table while the alert's
    revised value is named in prose).
  - sonnet/authoritative (N=5): state 5/5 and transcript 5/5 **RECOVERED** — the trusted-
    channel wording removes the injection-refusal MIXED entirely.
  So Claude does **not** anchor at this horizon/salience; the state arm is if anything *cleaner*
  (no injection-refusal MIXED). Token profile at the 4-round horizon: state ≈ 115–142% of
  transcript (Σ-resend overhead dominates; parity/robustness is the point here, not cost).
- **Local model (qwen3.8:27b via ollama, free) drift — same harness.** plain (N=4) and
  authoritative (N=3): **both arms RECOVERED every trial, zero protocol errors**. A reasoning
  model does not fall into sonnet's injection-refusal MIXED (it reasons past the alert), and
  its token profile is near-parity (state ≈ 97–99% of transcript). So the paper's robustness
  advantage does not separate the arms at this horizon on an open-weight local model either —
  consistent with the frontier result. (qwen is slow: ~9 min/trial, ~12 calls each.)
- **Injection-refusal confound (important; the sonnet/plain MIXED above).** sonnet treats the plain corrective alert as a
  **prompt-injection attempt** and refuses it in a prose note — a *safety* behavior that a
  naive verdict scored as anchoring. Added `build_drift(authoritative=True)` +
  `--authoritative`: SAME transport, wording only — it drops the injection cue (frames the
  change as a canonical retrieval update), which *reduces but does not eliminate* the confound
  (still confounded by wording/directiveness). The paper's cooperative-alert assumption does
  not hold for Claude by default.
- **Probe integrity (found in cross-validate).** `claude -p --bare` still exposes Read/Bash,
  so a model run inside this repo could read the opaque codes from source instead of
  retrieving. `claude_driver_bench` now passes a fail-closed empty allowlist (`--tools ""`, so
  even Task/Skill a denylist would miss are gone) and runs from a neutral cwd — retrieval is
  the only path to the codes.
- **Verdict-scoring bugs found live + across two codex cross-validate passes, all fixed**
  (`codex_driver_bench._drift_verdict` / `render_drift`), each with a pinning test: (1) whole-
  answer substring match scored a refuse-and-explain answer as MIXED — now **scoped to the
  service's record block** (contiguous non-blank lines, so a multi-line pretty-JSON record
  keeps host+value together while a blank-separated prose note is excluded), with a `scoped`
  low-confidence flag on fallback; (2) a model abbreviating `deploy-window-qxlmtv`→`qxlmtv`
  scored as *neither* (silent false-negative) — marks are now **distinct bare opaque codes**
  (no containment); (3) a NEITHER answer was mislabeled MIXED by a render fall-through —
  now a proper 4-way (RECOVERED/ANCHORED/MIXED/NEITHER); (4) `scoped=True` but neither value
  in the block (value split out of the record) → **self-contradiction guard** widens to the
  whole answer and downgrades to low-confidence rather than asserting a false NEITHER; (5)
  **P1: drift arms shared one resolver** — the transcript arm's revised-fragment swap leaked
  into the state arm (it never drifted), invalidating the first-pass "abstains" reading; each
  arm now gets a fresh content-addressed probe/resolver.

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
- **Claude-family + local run (2026-08-31)** closed the opus open item via
  `claude_driver_bench.py` and surfaced a guessable-probe bug (now opaque codes), an injection-
  refusal confound (now the `--authoritative` drift variant), and five verdict/harness bugs
  incl. the shared-resolver P1 — all fixed with pinning tests. Net drift findings **after the
  P1 fix, re-measured (sonnet N=13, qwen3.8:27b N=7)**: NEITHER model anchors at this horizon
  — both RECOVER; sonnet's only non-recovery is the plain-alert injection-refusal MIXED, gone
  under `--authoritative`; qwen recovers both arms every trial. Behavior transfers; early-
  stopping makes the state arm *more correct* on the un-guessable A/B probe.
- **Re-run triggers:** local model change or first-attempt failure rate rises (state lane);
  longer-horizon workloads appear (drivers); the harder drift variants (no explicit alert /
  multi-drift / distractor noise) remain the untested case (codex/claude/ollama bench
  `--drift` / `--authoritative`).
- **Open:** `/learn` contract-hold instrumentation (verdict-parse rate per run) + one live
  `/learn`; handoff hook's first natural fire, then `handoff_state validate` on the output;
  the HARDER drift variants (no explicit alert, multiple simultaneous drifts, distractor
  noise) are still untested on any model — the current alert is explicit and the horizon short
  (4–12 rounds), exactly where the paper predicts the two approaches converge, so a negative
  result here does not rule out the paper's robustness advantage at length/low-salience.
